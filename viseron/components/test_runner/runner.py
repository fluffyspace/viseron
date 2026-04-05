"""Orchestrate a single test run."""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, insert, select, update

from viseron.components.storage.const import COMPONENT as STORAGE_COMPONENT
from viseron.components.storage.models import (
    Motion,
    Objects,
    TestCase,
    TestResult,
    TestRun,
)
from viseron.domains.camera import AbstractCamera
from viseron.domains.camera.const import DOMAIN as CAMERA_DOMAIN
from viseron.exceptions import DomainNotRegisteredError
from viseron.helpers import utcnow

from .const import (
    CONFIG_CAMERA,
    CONFIG_CAMERA_READY_TIMEOUT,
    CONFIG_CASES,
    CONFIG_DURATION,
    CONFIG_EXPECTED,
    CONFIG_KIND,
    CONFIG_NAME,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    CONFIG_VIDEO_PATH,
    KIND_MOTION,
    KIND_OBJECT,
    STATUS_COMPLETE,
    STATUS_ERROR,
    STATUS_RUNNING,
)
from .evaluator import CaseOutcome, evaluate_case

if TYPE_CHECKING:
    from viseron import Viseron
    from viseron.components.storage import Storage

LOGGER = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """Final outcome of a test run."""

    run_id: int | None
    total: int
    passed: int
    failed: int
    status: str
    error: str | None = None

    @property
    def all_passed(self) -> bool:
        """Return True if the run completed with zero failures."""
        return self.status == STATUS_COMPLETE and self.failed == 0 and self.total > 0


def _synth_test_camera_id(camera_identifier: str, slug: str) -> str:
    """Return the synthetic ffmpeg camera identifier for a DB test case.

    Kept as a free function so both the injection step (setup time) and
    the pending_restart check (request time) produce identical ids
    without reaching into the ffmpeg component.
    """
    return f"test_{camera_identifier}_{slug}"


def _build_case_dict(db_case: TestCase) -> dict[str, Any] | None:
    """Convert a TestCase row into a runner-compatible case dict.

    Returns ``None`` if the DB row is malformed (shouldn't happen given
    the schema, but keeps the setup path defensive).
    """
    try:
        return {
            CONFIG_NAME: db_case.name,
            CONFIG_CAMERA: _synth_test_camera_id(
                db_case.camera_identifier, db_case.slug
            ),
            CONFIG_KIND: db_case.kind,
            CONFIG_DURATION: max(1, int(db_case.duration)),
            CONFIG_EXPECTED: dict(db_case.expected),
            CONFIG_VIDEO_PATH: db_case.video_path,
        }
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "failed to convert test case %s to a runner dict", db_case.id
        )
        return None


def inject_db_cases(
    vis: "Viseron", config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read the test_cases table and inject synthetic ffmpeg cameras.

    Called from :func:`test_runner.setup` during the pre-parallel tier so
    mutations to ``config['ffmpeg']['camera']`` are visible to the ffmpeg
    component's later setup. Returns a list of runner-compatible case
    dicts, one per DB row whose target camera lives under ffmpeg. Cases
    whose target isn't under ffmpeg are skipped with a warning — they
    stay in the catalog but are never picked up by the runner.
    """
    storage = vis.data.get(STORAGE_COMPONENT)
    if storage is None:
        LOGGER.warning(
            "storage component is not available; skipping db case injection"
        )
        return []

    ffmpeg_cameras = config.get("ffmpeg", {}).get("camera")
    if not isinstance(ffmpeg_cameras, dict):
        LOGGER.warning(
            "no ffmpeg camera config present; db-backed test cases will not "
            "be picked up. Declare at least one ffmpeg camera to enable the "
            "test harness."
        )
        return []

    with storage.get_session() as session:
        db_rows = session.execute(select(TestCase)).scalars().all()

    if not db_rows:
        return []

    injected_cases: list[dict[str, Any]] = []
    for row in db_rows:
        target_config = ffmpeg_cameras.get(row.camera_identifier)
        if target_config is None:
            LOGGER.warning(
                "skipping db test case %r: target camera %r is not under "
                "the ffmpeg component",
                row.name,
                row.camera_identifier,
            )
            continue

        test_camera_id = _synth_test_camera_id(row.camera_identifier, row.slug)
        case_dict = _build_case_dict(row)
        if case_dict is None:
            continue

        if test_camera_id not in ffmpeg_cameras:
            synthetic = {
                "name": f"Test {row.camera_identifier} — {row.name}",
                "host": "localhost",
                "port": 554,
                "path": "/",
                "test_mode": True,
                "file_source": row.video_path,
            }
            # Clone detector config from the target so detection behaviour
            # matches the live camera as closely as possible. Use deepcopy
            # so later validation passes can mutate freely without leaking
            # back into the target camera's dict.
            for key in ("motion_detector", "object_detector"):
                if key in target_config:
                    synthetic[key] = copy.deepcopy(target_config[key])
            ffmpeg_cameras[test_camera_id] = synthetic
            LOGGER.info(
                "injected test camera %r for db case %r (target: %r)",
                test_camera_id,
                row.name,
                row.camera_identifier,
            )

        injected_cases.append(case_dict)
    return injected_cases


class TestRunnerComponent:
    """Holder object stored on ``vis.data`` for the duration of the process.

    A single ``TestRunner`` instance represents one execution. This holder
    lets callers (the CLI, the REST endpoint) trigger fresh runs without
    re-instantiating the component, and surfaces the currently active run
    (if any) for status queries.
    """

    def __init__(
        self,
        vis: "Viseron",
        config: dict[str, Any],
        db_cases: list[dict[str, Any]] | None = None,
    ) -> None:
        self._vis = vis
        self._config = config
        self._db_cases: list[dict[str, Any]] = db_cases or []
        self._current_runner: "TestRunner | None" = None
        self._lock = threading.Lock()

    @property
    def config(self) -> dict[str, Any]:
        """Validated test_runner config block."""
        return self._config

    @property
    def db_cases(self) -> list[dict[str, Any]]:
        """Runner-compatible case dicts derived from the test_cases table.

        Populated at setup time by :func:`inject_db_cases`. The runner
        unions these with the config.yaml cases at trigger time and skips
        any whose synthesized camera isn't registered (which happens when
        a case was added after the process started and no restart has
        occurred yet).
        """
        return list(self._db_cases)

    def refresh_db_cases(self) -> None:
        """Reload DB cases without re-injecting ffmpeg config.

        Used by the REST layer so ``GET /tests/cases`` and
        ``trigger_run`` see newly inserted cases without waiting for a
        restart. Cases added here still require a restart to *run*
        (their synthetic ffmpeg camera isn't registered), but the
        pending_restart indicator picks them up immediately.
        """
        storage = self._vis.data.get(STORAGE_COMPONENT)
        if storage is None:
            return
        with storage.get_session() as session:
            rows = session.execute(select(TestCase)).scalars().all()
        refreshed: list[dict[str, Any]] = []
        for row in rows:
            case_dict = _build_case_dict(row)
            if case_dict is not None:
                refreshed.append(case_dict)
        self._db_cases = refreshed

    @property
    def current_runner(self) -> "TestRunner | None":
        """Return the most recently started runner (may be finished)."""
        return self._current_runner

    @property
    def is_running(self) -> bool:
        """True if a run is currently in flight."""
        runner = self._current_runner
        return runner is not None and not runner.completion_event.is_set()

    def _collect_runnable_cases(self) -> list[dict[str, Any]]:
        """Union config.yaml cases with DB cases that have live cameras.

        DB cases whose synthesized camera is not registered yet (pending
        restart) are skipped with a clear log line.
        """
        config_cases = list(self._config.get(CONFIG_CASES, []))
        runnable: list[dict[str, Any]] = list(config_cases)
        for case in self._db_cases:
            camera_id = case[CONFIG_CAMERA]
            try:
                self._vis.get_registered_domain(CAMERA_DOMAIN, camera_id)
            except DomainNotRegisteredError:
                LOGGER.info(
                    "skipping db case %r — camera %r is not registered "
                    "(restart Viseron to pick it up)",
                    case[CONFIG_NAME],
                    camera_id,
                )
                continue
            runnable.append(case)
        return runnable

    def trigger_run(self) -> "TestRunner":
        """Start a new run, refusing if one is already in flight."""
        with self._lock:
            if self.is_running:
                raise RuntimeError("a test run is already in progress")
            cases = self._collect_runnable_cases()
            if not cases:
                raise RuntimeError(
                    "no runnable test cases — configure cases under the "
                    "test_runner block in config.yaml or create one via "
                    "the 'Use for test' dialog and restart Viseron"
                )
            merged_config = {**self._config, CONFIG_CASES: cases}
            runner = TestRunner(self._vis, merged_config)
            self._current_runner = runner
            runner.start()
            return runner


class TestRunner:
    """Execute a set of test cases against running cameras."""

    def __init__(self, vis: "Viseron", config: dict[str, Any]) -> None:
        self._vis = vis
        self._config = config
        self._cases: list[dict[str, Any]] = config[CONFIG_CASES]
        self._storage: "Storage" = vis.data[STORAGE_COMPONENT]
        self._thread: threading.Thread | None = None
        self._completion = threading.Event()
        self._summary: RunSummary | None = None

    @property
    def completion_event(self) -> threading.Event:
        """Signalled once the run has written all results and finalised."""
        return self._completion

    @property
    def summary(self) -> RunSummary | None:
        """The final run summary, or None if the run has not finished."""
        return self._summary

    def start(self) -> None:
        """Kick off the run in a background thread."""
        self._thread = threading.Thread(
            target=self._run,
            name="viseron.test_runner",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        run_id: int | None = None
        try:
            # Clear stale test-mode detections for the cameras we are about to
            # exercise. Without this a detection from a previous run of the
            # same case would be counted as belonging to this one. Runs doing
            # this before resolving cameras so it happens even if the camera
            # component starts emitting frames before we get a chance to
            # observe them.
            self._clear_stale_detections()
            run_id = self._insert_run_row()
            LOGGER.info(
                "test_runner: starting run %d with %d case(s)",
                run_id,
                len(self._cases),
            )
            cameras = self._resolve_cameras()
            self._wait_for_observation_window()
            outcomes = self._evaluate_all(cameras)
            self._persist_results(run_id, outcomes)
            passed = sum(1 for (_case, outcome) in outcomes if outcome.passed)
            failed = len(outcomes) - passed
            self._finalize_run(run_id, passed, failed, STATUS_COMPLETE)
            self._summary = RunSummary(
                run_id=run_id,
                total=len(outcomes),
                passed=passed,
                failed=failed,
                status=STATUS_COMPLETE,
            )
            LOGGER.info(
                "test_runner: run %d complete — %d passed, %d failed",
                run_id,
                passed,
                failed,
            )
            for case, outcome in outcomes:
                marker = "PASS" if outcome.passed else "FAIL"
                LOGGER.info(
                    "test_runner: [%s] %s (%s) — %s",
                    marker,
                    case[CONFIG_NAME],
                    case[CONFIG_CAMERA],
                    outcome.message,
                )
            self._vis.exit_code = 0 if failed == 0 else 1
        except Exception as err:  # pylint: disable=broad-except
            LOGGER.exception("test_runner: run failed")
            if run_id is not None:
                try:
                    self._finalize_run(run_id, 0, 0, STATUS_ERROR, error=str(err))
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception("test_runner: failed to finalize errored run")
            self._summary = RunSummary(
                run_id=run_id,
                total=len(self._cases),
                passed=0,
                failed=len(self._cases),
                status=STATUS_ERROR,
                error=str(err),
            )
            self._vis.exit_code = 1
        finally:
            self._completion.set()
            if self._config.get(CONFIG_SHUTDOWN_ON_COMPLETE):
                LOGGER.info("test_runner: shutting down viseron")
                self._vis.shutdown()

    def _clear_stale_detections(self) -> None:
        """Remove existing test-mode rows for all referenced cameras."""
        identifiers = sorted({case[CONFIG_CAMERA] for case in self._cases})
        if not identifiers:
            return
        with self._storage.get_session() as session:
            session.execute(
                delete(Motion)
                .where(Motion.camera_identifier.in_(identifiers))
                .where(Motion.test.is_(True))
            )
            session.execute(
                delete(Objects)
                .where(Objects.camera_identifier.in_(identifiers))
                .where(Objects.test.is_(True))
            )
            session.commit()

    def _insert_run_row(self) -> int:
        with self._storage.get_session() as session:
            stmt = (
                insert(TestRun)
                .values(
                    total=len(self._cases),
                    passed=0,
                    failed=0,
                    status=STATUS_RUNNING,
                )
                .returning(TestRun.id)
            )
            run_id = session.execute(stmt).scalar_one()
            session.commit()
            return run_id

    def _resolve_cameras(self) -> dict[str, AbstractCamera]:
        """Wait until every referenced camera is registered and in test mode."""
        timeout = float(self._config[CONFIG_CAMERA_READY_TIMEOUT])
        deadline = time.monotonic() + timeout
        required = {case[CONFIG_CAMERA] for case in self._cases}
        resolved: dict[str, AbstractCamera] = {}
        while required:
            for identifier in list(required):
                try:
                    camera = self._vis.get_registered_domain(CAMERA_DOMAIN, identifier)
                except DomainNotRegisteredError:
                    continue
                if not camera.is_test_camera:
                    raise RuntimeError(
                        f"camera {identifier!r} is referenced by the test_runner "
                        f"but is not configured with test_mode=true — refusing to "
                        f"run to avoid polluting live events."
                    )
                resolved[identifier] = camera
                required.discard(identifier)
            if not required:
                break
            if time.monotonic() > deadline:
                missing = sorted(required)
                raise TimeoutError(
                    f"test_runner: cameras not ready within {timeout:.0f}s: "
                    f"{missing}"
                )
            time.sleep(0.5)
        return resolved

    def _wait_for_observation_window(self) -> None:
        """Sleep long enough that every case's video has played through.

        All cameras run in parallel, so the total wait is the max of per-case
        durations, not the sum.
        """
        max_duration = max(case[CONFIG_DURATION] for case in self._cases)
        LOGGER.info(
            "test_runner: observing cameras for %d second(s)", max_duration
        )
        time.sleep(max_duration)

    def _evaluate_all(
        self,
        cameras: dict[str, AbstractCamera],
    ) -> list[tuple[dict[str, Any], CaseOutcome]]:
        results: list[tuple[dict[str, Any], CaseOutcome]] = []
        for case in self._cases:
            camera = cameras[case[CONFIG_CAMERA]]
            outcome = evaluate_case(
                kind=case[CONFIG_KIND],
                get_session=self._storage.get_session,
                camera_identifier=camera.identifier,
                expected=case[CONFIG_EXPECTED],
            )
            results.append((case, outcome))
        return results

    def _persist_results(
        self,
        run_id: int,
        outcomes: list[tuple[dict[str, Any], CaseOutcome]],
    ) -> None:
        with self._storage.get_session() as session:
            for case, outcome in outcomes:
                stmt = insert(TestResult).values(
                    run_id=run_id,
                    case_name=case[CONFIG_NAME],
                    camera_identifier=case[CONFIG_CAMERA],
                    kind=case[CONFIG_KIND],
                    expected=case[CONFIG_EXPECTED],
                    actual=outcome.actual,
                    passed=outcome.passed,
                    video_path=case.get(CONFIG_VIDEO_PATH),
                    snapshot_path=outcome.snapshot_path,
                    message=outcome.message,
                )
                session.execute(stmt)
            session.commit()

    def _finalize_run(
        self,
        run_id: int,
        passed: int,
        failed: int,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._storage.get_session() as session:
            stmt = (
                update(TestRun)
                .where(TestRun.id == run_id)
                .values(
                    passed=passed,
                    failed=failed,
                    status=status,
                    finished_at=utcnow(),
                )
            )
            session.execute(stmt)
            session.commit()
        if error:
            LOGGER.error("test_runner: run %d errored: %s", run_id, error)


# Kinds are exported so tests and the CLI can inspect them without reaching
# into const.py.
__all__ = [
    "TestRunner",
    "TestRunnerComponent",
    "RunSummary",
    "KIND_MOTION",
    "KIND_OBJECT",
]
