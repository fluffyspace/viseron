"""API handler for test runs produced by the test_runner component."""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from sqlalchemy import delete as sql_delete, insert, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from viseron.components.storage.models import TestCase, TestResult, TestRun
from viseron.components.storage.queries import get_time_period_fragments
from viseron.components.test_runner import COMPONENT as TEST_RUNNER_COMPONENT
from viseron.components.test_runner.auto_tuner import AutoTuner
from viseron.components.test_runner.const import KINDS
from viseron.components.webserver.api.handlers import BaseAPIHandler
from viseron.components.webserver.auth import Role
from viseron.const import CONFIG_DIR, RESTART_EXIT_CODE
from viseron.domains.camera.fragmenter import Fragment
from viseron.helpers import create_directory

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from viseron.components.test_runner.runner import TestRunnerComponent
    from viseron.domains.camera import AbstractCamera

LOGGER = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 200

POLARITY_POSITIVE = "positive"
POLARITY_NEGATIVE = "negative"
POLARITIES = (POLARITY_POSITIVE, POLARITY_NEGATIVE)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(value: str) -> str:
    """Return a filesystem-safe slug for the given case name."""
    slug = _SLUG_RE.sub("_", value.strip()).strip("_.")
    return slug or "case"


def _test_videos_root() -> str:
    return os.path.join(CONFIG_DIR, "test_videos")


def _test_clip_path(
    camera_identifier: str,
    kind: str,
    polarity: str,
    slug: str,
) -> str:
    return os.path.join(
        _test_videos_root(),
        camera_identifier,
        kind,
        polarity,
        f"{slug}.mp4",
    )


def _is_within_test_videos(path: str) -> bool:
    """Return True if ``path`` resolves to a file under the test videos root.

    Used as a defence-in-depth check when serving or deleting files driven
    by a database row — prevents any rogue row from tricking the server
    into touching an arbitrary file on disk.
    """
    try:
        root = os.path.realpath(_test_videos_root())
        candidate = os.path.realpath(path)
    except OSError:
        return False
    return (
        candidate == root
        or candidate.startswith(root + os.sep)
    )


def _yaml_snippet(
    *,
    camera_identifier: str,
    kind: str,
    polarity: str,
    name: str,
    slug: str,
    clip_path: str,
    expected: dict[str, Any],
    duration: int,
) -> str:
    """Render a ready-to-paste tests.yaml snippet.

    Snippets are grouped the same way as tests.yaml itself — by real
    camera, then by kind/polarity — so the user can paste them straight
    into their file and merge adjacent blocks if they already have
    entries for the same camera. The synthetic ffmpeg camera is no
    longer the user's problem: ``test_runner`` auto-synthesizes one per
    case at setup time.
    """
    # ``slug`` / ``duration`` stay in the signature for wire compatibility
    # with the REST response payload (frontend consumers expect them) but
    # aren't needed in the tests.yaml snippet itself.
    del slug, duration

    if kind == "motion":
        return (
            f"# Append under cameras.{camera_identifier}.motion.{polarity} in "
            f"tests.yaml:\n"
            f"cameras:\n"
            f"  {camera_identifier}:\n"
            f"    motion:\n"
            f"      {polarity}:\n"
            f"        - {clip_path}  # {name}\n"
        )

    # kind == "object"
    if polarity == "negative":
        return (
            f"# Append under cameras.{camera_identifier}.object.negative in "
            f"tests.yaml:\n"
            f"cameras:\n"
            f"  {camera_identifier}:\n"
            f"    object:\n"
            f"      negative:\n"
            f"        - {clip_path}  # {name}\n"
        )

    # object / positive — group by label (fall back to "any" when no
    # specific label was requested).
    labels = expected.get("labels") or []
    if not labels:
        labels = ["any"]
    label_blocks: list[str] = []
    for label in labels:
        label_blocks.append(
            f"        {label}:\n"
            f"          - {clip_path}  # {name}\n"
        )
    return (
        f"# Append under cameras.{camera_identifier}.object.positive in "
        f"tests.yaml:\n"
        f"cameras:\n"
        f"  {camera_identifier}:\n"
        f"    object:\n"
        f"      positive:\n"
        + "".join(label_blocks)
    )


def _serialize_case(case: TestCase) -> dict[str, Any]:
    return {
        "id": case.id,
        "name": case.name,
        "slug": case.slug,
        "camera_identifier": case.camera_identifier,
        "kind": case.kind,
        "polarity": case.polarity,
        "expected": case.expected,
        "video_path": case.video_path,
        "duration": case.duration,
        "created_at": case.created_at,
    }


def _serialize_run(run: TestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "started_at": run.started_at,
        "started_timestamp": run.started_at.timestamp() if run.started_at else None,
        "finished_at": run.finished_at,
        "finished_timestamp": (
            run.finished_at.timestamp() if run.finished_at else None
        ),
        "total": run.total,
        "passed": run.passed,
        "failed": run.failed,
        "status": run.status,
    }


def _serialize_result(result: TestResult, subpath: str) -> dict[str, Any]:
    return {
        "id": result.id,
        "run_id": result.run_id,
        "case_name": result.case_name,
        "camera_identifier": result.camera_identifier,
        "kind": result.kind,
        "expected": result.expected,
        "actual": result.actual,
        "passed": result.passed,
        "video_path": result.video_path,
        "snapshot_path": (
            f"{subpath}/files{result.snapshot_path}"
            if result.snapshot_path
            else None
        ),
        "message": result.message,
        "created_at": result.created_at,
        "created_timestamp": (
            result.created_at.timestamp() if result.created_at else None
        ),
    }


class TestsAPIHandler(BaseAPIHandler):
    """API handler for the test_runner results and run control."""

    routes = [
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/tests/runs",
            "supported_methods": ["GET"],
            "method": "get_runs",
            "request_arguments_schema": vol.Schema(
                {
                    vol.Optional("limit"): vol.Coerce(int),
                }
            ),
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/tests/runs/latest",
            "supported_methods": ["GET"],
            "method": "get_latest_run",
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/tests/runs/(?P<run_id>\d+)",
            "supported_methods": ["GET"],
            "method": "get_run",
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": r"/tests/runs",
            "supported_methods": ["POST"],
            "method": "post_run",
            "json_body_schema": vol.Schema(
                {
                    vol.Optional("auto_correct", default=False): bool,
                    vol.Optional("max_repetitions", default=5): vol.All(
                        int, vol.Range(min=1, max=20)
                    ),
                }
            ),
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": r"/tests/clips",
            "supported_methods": ["POST"],
            "method": "post_clip",
            "json_body_schema": vol.Schema(
                {
                    vol.Required("camera_identifier"): str,
                    vol.Required("start"): vol.Coerce(int),
                    vol.Required("end"): vol.Coerce(int),
                    vol.Required("name"): vol.All(str, vol.Length(min=1, max=200)),
                    vol.Required("kind"): vol.In(KINDS),
                    vol.Required("polarity"): vol.In(POLARITIES),
                    vol.Optional("expected", default=dict): vol.Schema(
                        {}, extra=vol.ALLOW_EXTRA
                    ),
                    vol.Optional("duration"): vol.All(
                        int, vol.Range(min=1, max=3600)
                    ),
                }
            ),
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/tests/cases",
            "supported_methods": ["GET"],
            "method": "get_cases",
            "request_arguments_schema": vol.Schema(
                {
                    vol.Optional("camera_identifier"): str,
                }
            ),
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": r"/tests/cases/(?P<case_id>\d+)",
            "supported_methods": ["DELETE"],
            "method": "delete_case",
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/tests/cases/(?P<case_id>\d+)/clip",
            "supported_methods": ["GET"],
            "method": "get_case_clip",
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/tests/results/(?P<result_id>\d+)/clip",
            "supported_methods": ["GET"],
            "method": "get_result_clip",
        },
        {
            "requires_role": [Role.ADMIN],
            "path_pattern": r"/tests/restart",
            "supported_methods": ["POST"],
            "method": "post_restart",
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": r"/tests/auto-tune",
            "supported_methods": ["POST"],
            "method": "post_auto_tune",
            "json_body_schema": vol.Schema(
                {
                    vol.Optional("max_iterations", default=10): vol.All(
                        int, vol.Range(min=1, max=50)
                    ),
                }
            ),
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/tests/auto-tune",
            "supported_methods": ["GET"],
            "method": "get_auto_tune",
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": r"/tests/auto-tune/cancel",
            "supported_methods": ["POST"],
            "method": "post_cancel_auto_tune",
        },
    ]

    # --- query helpers ---

    def _query_runs(
        self,
        get_session: Callable[[], "Session"],
        limit: int,
    ) -> list[dict[str, Any]]:
        with get_session() as session:
            stmt = (
                select(TestRun).order_by(TestRun.id.desc()).limit(limit)
            )
            runs = session.execute(stmt).scalars().all()
            return [_serialize_run(run) for run in runs]

    def _query_run(
        self,
        get_session: Callable[[], "Session"],
        run_id: int,
    ) -> dict[str, Any] | None:
        with get_session() as session:
            run = session.execute(
                select(TestRun).where(TestRun.id == run_id)
            ).scalar_one_or_none()
            if run is None:
                return None
            results = (
                session.execute(
                    select(TestResult)
                    .where(TestResult.run_id == run_id)
                    .order_by(TestResult.case_name.asc())
                )
                .scalars()
                .all()
            )
            subpath = self.get_subpath()
            data: dict[str, Any] = {
                **_serialize_run(run),
                "results": [
                    _serialize_result(result, subpath) for result in results
                ],
                "progress": [],
                "recommendations": [],
            }

        # If there's a live runner for this run, attach progress and
        # recommendations from the in-memory state.
        component: "TestRunnerComponent | None" = self._vis.data.get(
            TEST_RUNNER_COMPONENT
        )
        if component and component.current_runner:
            runner = component.current_runner
            if runner.summary and runner.summary.run_id == run_id:
                data["recommendations"] = runner.recommendations
            if not runner.completion_event.is_set():
                data["progress"] = [
                    {
                        "source_camera": p.source_camera,
                        "status": p.status,
                        "total": p.total,
                        "passed": p.passed,
                        "failed": p.failed,
                    }
                    for p in runner.progress
                ]
            elif runner.summary and runner.summary.run_id == run_id:
                data["progress"] = [
                    {
                        "source_camera": p.source_camera,
                        "status": p.status,
                        "total": p.total,
                        "passed": p.passed,
                        "failed": p.failed,
                    }
                    for p in runner.progress
                ]

        return data

    def _query_latest_run(
        self,
        get_session: Callable[[], "Session"],
    ) -> dict[str, Any] | None:
        with get_session() as session:
            run = (
                session.execute(
                    select(TestRun).order_by(TestRun.id.desc()).limit(1)
                )
                .scalars()
                .first()
            )
            if run is None:
                return None
            return self._query_run(get_session, run.id)

    # --- handlers ---

    async def get_runs(self) -> None:
        """List recent test runs."""
        limit = int(self.request_arguments.get("limit", DEFAULT_LIST_LIMIT))
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        runs = await self.run_in_executor(
            self._query_runs, self._get_session, limit
        )
        await self.response_success(response={"runs": runs})

    async def get_run(self, run_id: str) -> None:
        """Return a single run with its results."""
        try:
            run_id_int = int(run_id)
        except ValueError:
            self.response_error(HTTPStatus.BAD_REQUEST, f"Invalid run id: {run_id}")
            return
        run = await self.run_in_executor(
            self._query_run, self._get_session, run_id_int
        )
        if run is None:
            self.response_error(
                HTTPStatus.NOT_FOUND, f"Test run {run_id_int} not found"
            )
            return
        await self.response_success(response={"run": run})

    async def get_latest_run(self) -> None:
        """Return the most recent run with its results, if any."""
        run = await self.run_in_executor(
            self._query_latest_run, self._get_session
        )
        if run is None:
            await self.response_success(response={"run": None})
            return
        await self.response_success(response={"run": run})

    async def post_run(self) -> None:
        """Trigger a new run via the test_runner component."""
        component: "TestRunnerComponent | None" = self._vis.data.get(
            TEST_RUNNER_COMPONENT
        )
        if component is None:
            self.response_error(
                HTTPStatus.NOT_FOUND,
                "test_runner component is not configured",
            )
            return
        body = self.request_body or {}
        auto_correct = bool(body.get("auto_correct", False))
        max_repetitions = int(body.get("max_repetitions", 5))
        try:
            runner = component.trigger_run(
                auto_correct=auto_correct,
                max_repetitions=max_repetitions,
            )
        except RuntimeError as err:
            self.response_error(HTTPStatus.CONFLICT, str(err))
            return
        await self.response_success(
            status=HTTPStatus.ACCEPTED,
            response={
                "started": True,
                "is_running": component.is_running,
                "completion_pending": not runner.completion_event.is_set(),
            },
        )

    # --- clip extraction ---

    def _extract_clip(
        self,
        camera: "AbstractCamera",
        start: int,
        end: int,
        kind: str,
        polarity: str,
        name: str,
        slug: str,
        expected: dict[str, Any],
        case_duration: int | None,
    ) -> tuple[str, int, int] | str:
        """Concatenate fragments for a time range, stash the clip, and
        upsert a catalog entry.

        Returns ``(clip_path, effective_duration, case_id)`` on success or
        an error string on failure.
        """
        files = get_time_period_fragments(
            [camera.identifier], start, end, self._get_session
        )
        if not files:
            return "No recorded fragments found for the requested time range"
        fragments = [
            Fragment(file.filename, file.path, file.duration, file.orig_ctime)
            for file in files
        ]
        # Retry once — the init.mp4 file referenced by the HLS playlist is
        # overwritten non-atomically each time a new segment is produced, so
        # ffmpeg can fail if it reads a partially-written init file.
        tmp_path = camera.fragmenter.concatenate_fragments(fragments)
        if not tmp_path:
            time.sleep(0.5)
            tmp_path = camera.fragmenter.concatenate_fragments(fragments)
        if not tmp_path:
            return "Failed to concatenate recorded fragments into a clip"

        destination = _test_clip_path(camera.identifier, kind, polarity, slug)
        create_directory(os.path.dirname(destination))
        shutil.move(tmp_path, destination)
        window_seconds = max(1, int(end - start))
        effective_duration = int(case_duration or (window_seconds + 5))
        case_id = self._upsert_case(
            name=name,
            slug=slug,
            camera_identifier=camera.identifier,
            kind=kind,
            polarity=polarity,
            expected=expected,
            video_path=destination,
            duration=effective_duration,
        )
        return destination, effective_duration, case_id

    def _upsert_case(
        self,
        *,
        name: str,
        slug: str,
        camera_identifier: str,
        kind: str,
        polarity: str,
        expected: dict[str, Any],
        video_path: str,
        duration: int,
    ) -> int:
        """Insert-or-update a catalog row, keyed on the unique constraint."""
        values = {
            "name": name,
            "slug": slug,
            "camera_identifier": camera_identifier,
            "kind": kind,
            "polarity": polarity,
            "expected": expected,
            "video_path": video_path,
            "duration": duration,
        }
        stmt = pg_insert(TestCase).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_test_cases_camera_kind_polarity_slug",
            set_={
                "name": stmt.excluded.name,
                "expected": stmt.excluded.expected,
                "video_path": stmt.excluded.video_path,
                "duration": stmt.excluded.duration,
            },
        ).returning(TestCase.id)
        with self._get_session() as session:
            case_id = session.execute(stmt).scalar_one()
            session.commit()
            return case_id

    # --- test case catalog ---

    def _query_cases(
        self,
        get_session: Callable[[], "Session"],
        camera_identifier: str | None,
    ) -> list[dict[str, Any]]:
        with get_session() as session:
            stmt = select(TestCase).order_by(
                TestCase.camera_identifier.asc(), TestCase.name.asc()
            )
            if camera_identifier:
                stmt = stmt.where(TestCase.camera_identifier == camera_identifier)
            cases = session.execute(stmt).scalars().all()
            serialized = []
            for case in cases:
                entry = _serialize_case(case)
                entry["snippet"] = _yaml_snippet(
                    camera_identifier=case.camera_identifier,
                    kind=case.kind,
                    polarity=case.polarity,
                    name=case.name,
                    slug=case.slug,
                    clip_path=case.video_path,
                    expected=dict(case.expected),
                    duration=case.duration,
                )
                serialized.append(entry)
            return serialized

    async def get_cases(self) -> None:
        """List all catalogued test cases."""
        camera_identifier = self.request_arguments.get("camera_identifier")
        cases = await self.run_in_executor(
            self._query_cases, self._get_session, camera_identifier
        )
        component_enabled = self._vis.data.get(
            TEST_RUNNER_COMPONENT
        ) is not None
        await self.response_success(
            response={"cases": cases, "component_enabled": component_enabled}
        )

    def _delete_case(
        self,
        get_session: Callable[[], "Session"],
        case_id: int,
    ) -> bool:
        with get_session() as session:
            case = session.execute(
                select(TestCase).where(TestCase.id == case_id)
            ).scalar_one_or_none()
            if case is None:
                return False
            video_path = case.video_path
            session.execute(
                sql_delete(TestCase).where(TestCase.id == case_id)
            )
            session.commit()
        if video_path and _is_within_test_videos(video_path):
            try:
                os.remove(video_path)
            except FileNotFoundError:
                pass
            except OSError as err:
                LOGGER.warning(
                    "failed to remove clip file %s: %s", video_path, err
                )
        return True

    async def delete_case(self, case_id: str) -> None:
        """Remove a catalogued test case and its on-disk clip."""
        try:
            case_id_int = int(case_id)
        except ValueError:
            self.response_error(HTTPStatus.BAD_REQUEST, f"Invalid case id: {case_id}")
            return
        removed = await self.run_in_executor(
            self._delete_case, self._get_session, case_id_int
        )
        if not removed:
            self.response_error(
                HTTPStatus.NOT_FOUND, f"Test case {case_id_int} not found"
            )
            return
        await self.response_success(response={"deleted": case_id_int})

    def _lookup_case_clip(
        self,
        get_session: Callable[[], "Session"],
        case_id: int,
    ) -> str | None:
        with get_session() as session:
            case = session.execute(
                select(TestCase).where(TestCase.id == case_id)
            ).scalar_one_or_none()
            if case is None:
                return None
            return case.video_path

    async def get_case_clip(self, case_id: str) -> None:
        """Stream the MP4 bytes for a test case's clip file.

        Only files under ``CONFIG_DIR/test_videos`` are served — any
        catalog row pointing elsewhere is rejected to prevent path
        traversal.
        """
        try:
            case_id_int = int(case_id)
        except ValueError:
            self.response_error(HTTPStatus.BAD_REQUEST, f"Invalid case id: {case_id}")
            return
        video_path = await self.run_in_executor(
            self._lookup_case_clip, self._get_session, case_id_int
        )
        if video_path is None:
            self.response_error(
                HTTPStatus.NOT_FOUND, f"Test case {case_id_int} not found"
            )
            return
        if not _is_within_test_videos(video_path):
            self.response_error(
                HTTPStatus.FORBIDDEN,
                "Clip path is outside the test videos directory",
            )
            return
        if not os.path.exists(video_path):
            self.response_error(
                HTTPStatus.NOT_FOUND,
                f"Clip file is missing on disk: {video_path}",
            )
            return
        self.set_header("Content-Type", "video/mp4")
        self.set_header("Cache-Control", "no-cache")

        def _read_bytes() -> bytes:
            with open(video_path, "rb") as fp:
                return fp.read()

        payload = await self.run_in_executor(_read_bytes)
        self.set_header("Content-Length", str(len(payload)))
        self.finish(payload)

    def _lookup_result_video_path(
        self,
        get_session: Callable[[], "Session"],
        result_id: int,
    ) -> str | None:
        with get_session() as session:
            result = session.execute(
                select(TestResult).where(TestResult.id == result_id)
            ).scalar_one_or_none()
            if result is None:
                return None
            return result.video_path

    # --- restart ---

    async def post_restart(self) -> None:
        """Trigger a graceful restart so newly-created DB cases get picked up.

        Mirrors the existing ``restart_viseron`` websocket command: set the
        process exit code to ``RESTART_EXIT_CODE`` so the supervisor
        (systemd, s6, docker restart policy) brings Viseron back up, then
        send SIGINT to kick off the graceful shutdown. Admin-only because
        a misbehaving read-only client shouldn't be able to bounce the
        whole process.
        """
        self._vis.exit_code = RESTART_EXIT_CODE
        await self.response_success(
            status=HTTPStatus.ACCEPTED,
            response={"restarting": True},
        )
        # Run *after* the response has been queued to the client so the
        # UI sees the 202 before the process starts tearing down.
        os.kill(os.getpid(), signal.SIGINT)

    async def get_result_clip(self, result_id: str) -> None:
        """Stream the clip that produced a particular test result."""
        try:
            result_id_int = int(result_id)
        except ValueError:
            self.response_error(
                HTTPStatus.BAD_REQUEST, f"Invalid result id: {result_id}"
            )
            return
        video_path = await self.run_in_executor(
            self._lookup_result_video_path, self._get_session, result_id_int
        )
        if video_path is None:
            self.response_error(
                HTTPStatus.NOT_FOUND,
                f"Test result {result_id_int} has no clip on file",
            )
            return
        if not _is_within_test_videos(video_path):
            self.response_error(
                HTTPStatus.FORBIDDEN,
                "Clip path is outside the test videos directory",
            )
            return
        if not os.path.exists(video_path):
            self.response_error(
                HTTPStatus.NOT_FOUND,
                f"Clip file is missing on disk: {video_path}",
            )
            return
        self.set_header("Content-Type", "video/mp4")
        self.set_header("Cache-Control", "no-cache")

        def _read_bytes() -> bytes:
            with open(video_path, "rb") as fp:
                return fp.read()

        payload = await self.run_in_executor(_read_bytes)
        self.set_header("Content-Length", str(len(payload)))
        self.finish(payload)

    # --- auto-tune ---

    # Class-level storage for the active auto-tuner instance.
    _auto_tuner: "AutoTuner | None" = None

    async def post_auto_tune(self) -> None:
        """Start an auto-tune session that iteratively adjusts detection
        parameters to make tests pass."""
        component: "TestRunnerComponent | None" = self._vis.data.get(
            TEST_RUNNER_COMPONENT
        )
        if component is None:
            self.response_error(
                HTTPStatus.NOT_FOUND,
                "test_runner component is not configured",
            )
            return
        if component.is_running:
            self.response_error(
                HTTPStatus.CONFLICT,
                "A test run is already in progress. Wait for it to finish.",
            )
            return
        if (
            TestsAPIHandler._auto_tuner is not None
            and TestsAPIHandler._auto_tuner.state.status == "running"
        ):
            self.response_error(
                HTTPStatus.CONFLICT,
                "An auto-tune session is already running.",
            )
            return

        body = self.json_body
        max_iterations = body.get("max_iterations", 10)

        tuner = AutoTuner(
            self._vis,
            component,
            max_iterations=max_iterations,
        )
        TestsAPIHandler._auto_tuner = tuner
        tuner.start()

        await self.response_success(
            status=HTTPStatus.ACCEPTED,
            response={
                "started": True,
                "max_iterations": max_iterations,
            },
        )

    async def get_auto_tune(self) -> None:
        """Return current auto-tune session state."""
        tuner = TestsAPIHandler._auto_tuner
        if tuner is None:
            await self.response_success(response={"auto_tune": None})
            return
        await self.response_success(
            response={"auto_tune": tuner.state.serialize()}
        )

    async def post_cancel_auto_tune(self) -> None:
        """Cancel a running auto-tune session."""
        tuner = TestsAPIHandler._auto_tuner
        if tuner is None or tuner.state.status != "running":
            self.response_error(
                HTTPStatus.NOT_FOUND,
                "No active auto-tune session to cancel.",
            )
            return
        tuner.cancel()
        await self.response_success(response={"cancelled": True})

    async def post_clip(self) -> None:
        """Clip a range of recorded footage and stash it as a test fixture."""
        body = self.json_body
        camera = self._get_camera(body["camera_identifier"], failed=False)
        if camera is None:
            self.response_error(
                HTTPStatus.NOT_FOUND,
                f"Camera {body['camera_identifier']!r} not found",
            )
            return
        if body["end"] <= body["start"]:
            self.response_error(
                HTTPStatus.BAD_REQUEST,
                "'end' must be strictly greater than 'start'",
            )
            return

        slug = _slugify(body["name"])
        result = await self.run_in_executor(
            self._extract_clip,
            camera,
            body["start"],
            body["end"],
            body["kind"],
            body["polarity"],
            body["name"],
            slug,
            body["expected"],
            body.get("duration"),
        )
        if isinstance(result, str):
            self.response_error(HTTPStatus.NOT_FOUND, result)
            return

        clip_path, effective_duration, case_id = result

        # Pick up the new case in the in-memory DB-case list. Cases are
        # pure data — the runner acquires a replay camera on demand at
        # run time, so no camera registration / restart is ever required.
        component: "TestRunnerComponent | None" = self._vis.data.get(
            TEST_RUNNER_COMPONENT
        )
        if component is not None:
            await self.run_in_executor(component.refresh_db_cases)

        snippet = _yaml_snippet(
            camera_identifier=body["camera_identifier"],
            kind=body["kind"],
            polarity=body["polarity"],
            name=body["name"],
            slug=slug,
            clip_path=clip_path,
            expected=body["expected"],
            duration=effective_duration,
        )
        await self.response_success(
            status=HTTPStatus.CREATED,
            response={
                "case_id": case_id,
                "clip_path": clip_path,
                "slug": slug,
                "snippet": snippet,
                "duration": effective_duration,
            },
        )
