"""Orchestrate a single test run."""
from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
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
from viseron.helpers import utcnow
from viseron.helpers.replay_camera import (
    ReplayBusy,
    get_manager,
    replay_camera_id,
)

from .const import (
    CONFIG_DURATION,
    CONFIG_EXPECTED,
    CONFIG_KIND,
    CONFIG_NAME,
    CONFIG_POLARITY,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    CONFIG_SOURCE_CAMERA,
    CONFIG_VIDEO_PATH,
    DEFAULT_DURATION,
    KIND_MOTION,
    KIND_OBJECT,
    STATUS_COMPLETE,
    STATUS_ERROR,
    STATUS_RUNNING,
)
from .evaluator import CaseOutcome, evaluate_case
from .tests_yaml import (
    _parse_datetime,
    _slugify,
    flatten_tests_yaml,
    load_tests_yaml,
)
from .timeline import (
    TimelineMaterializationError,
    materialize_timeline_clip,
)

if TYPE_CHECKING:
    from viseron import Viseron
    from viseron.components.storage import Storage

LOGGER = logging.getLogger(__name__)


# A runnable case is a plain dict with these keys, kept as loose
# duck-typing so the runner can accept both tests.yaml-derived and
# DB-derived descriptors without a shared class hierarchy.
RunnerCase = dict[str, Any]


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


@dataclass
class CameraGroupProgress:
    """Progress for a single source-camera group during sequential execution."""

    source_camera: str
    status: str = "pending"  # "pending" | "running" | "done"
    total: int = 0
    passed: int = 0
    failed: int = 0


# --- tests.yaml driven case loading ----------------------------------------


def _resolve_source_to_file(
    storage: "Storage | None",
    camera_identifier: str,
    slug: str,
    source: Any,
) -> str | None:
    """Return an on-disk MP4 path for a flattened source entry.

    Strings are returned verbatim (must be absolute paths). Mapping
    sources are materialized from recorded fragments via the storage
    layer. Returns ``None`` on any failure so the caller can skip the
    case with a logged warning rather than aborting startup.
    """
    if isinstance(source, str):
        return source
    if not isinstance(source, dict):
        LOGGER.warning(
            "test_runner: unsupported source entry %r (expected path or "
            "mapping with from/to)",
            source,
        )
        return None
    if storage is None:
        LOGGER.warning(
            "test_runner: cannot materialize timeline clip for camera %r — "
            "storage component is not available",
            camera_identifier,
        )
        return None
    try:
        start = _parse_datetime(source["from"])
        end = _parse_datetime(source["to"])
    except (KeyError, ValueError) as err:
        LOGGER.warning(
            "test_runner: invalid timeline range for camera %r: %s",
            camera_identifier,
            err,
        )
        return None
    if end <= start:
        LOGGER.warning(
            "test_runner: timeline 'to' must be after 'from' for camera %r",
            camera_identifier,
        )
        return None
    try:
        return materialize_timeline_clip(
            storage, camera_identifier, start, end, slug
        )
    except TimelineMaterializationError as err:
        LOGGER.warning(
            "test_runner: could not materialize timeline clip for %r: %s",
            camera_identifier,
            err,
        )
        return None


def _effective_duration(flat_case: dict[str, Any], default_duration: int) -> int:
    """Clamp to at least 1 and fall back to default_duration when missing."""
    raw = flat_case.get("duration") or default_duration
    return max(1, int(raw))


def inject_yaml_cases(
    vis: "Viseron",
    config: dict[str, Any],
    tests_yaml: dict[str, Any],
    *,
    default_duration: int = DEFAULT_DURATION,
) -> list[RunnerCase]:
    """Flatten tests.yaml, synthesize ffmpeg test cameras, return runner cases.

    Returns a list of runner-case dicts — pure data, no camera registration.
    The runner acquires a replay camera on demand per case at run time.

    Cases whose parent camera isn't under the ffmpeg component are
    skipped with a warning. Cases whose source can't be resolved are
    skipped with a warning. Neither is fatal — startup still proceeds,
    the user just sees fewer runnable cases.
    """
    ffmpeg_cameras = config.get("ffmpeg", {}).get("camera")
    if not isinstance(ffmpeg_cameras, dict):
        LOGGER.warning(
            "test_runner: no ffmpeg camera config present; tests.yaml cases "
            "cannot be wired up. Declare at least one ffmpeg camera to "
            "enable the test harness."
        )
        return []

    storage = vis.data.get(STORAGE_COMPONENT)
    flat_cases = flatten_tests_yaml(tests_yaml, default_duration=default_duration)
    if not flat_cases:
        return []

    cases: list[RunnerCase] = []
    for flat in flat_cases:
        parent_id: str = flat["source_camera"]
        if parent_id not in ffmpeg_cameras:
            LOGGER.warning(
                "test_runner: skipping case %r — parent camera %r is not "
                "declared under the ffmpeg component",
                flat["name"],
                parent_id,
            )
            continue

        resolved_path = _resolve_source_to_file(
            storage, parent_id, flat["slug"], flat["source"]
        )
        if resolved_path is None:
            continue

        cases.append(
            {
                CONFIG_NAME: flat["name"],
                CONFIG_SOURCE_CAMERA: parent_id,
                CONFIG_KIND: flat["kind"],
                CONFIG_POLARITY: flat["polarity"],
                CONFIG_DURATION: _effective_duration(flat, default_duration),
                CONFIG_EXPECTED: dict(flat["expected"]),
                CONFIG_VIDEO_PATH: resolved_path,
            }
        )
    return cases


def load_and_inject_tests_yaml(
    vis: "Viseron",
    config: dict[str, Any],
    path: str,
    *,
    default_duration: int = DEFAULT_DURATION,
) -> list[RunnerCase]:
    """Convenience wrapper used by setup(): load tests.yaml then inject."""
    try:
        tests_yaml = load_tests_yaml(path)
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("test_runner: failed to load tests.yaml at %s", path)
        return []
    if tests_yaml is None:
        return []
    return inject_yaml_cases(
        vis, config, tests_yaml, default_duration=default_duration
    )


# --- DB-catalog driven case loading ----------------------------------------


def _build_db_case_dict(db_case: TestCase) -> RunnerCase | None:
    """Convert a TestCase row into a runner-compatible case dict."""
    try:
        return {
            CONFIG_NAME: db_case.name,
            CONFIG_SOURCE_CAMERA: db_case.camera_identifier,
            CONFIG_KIND: db_case.kind,
            CONFIG_POLARITY: db_case.polarity,
            CONFIG_DURATION: max(1, int(db_case.duration)),
            CONFIG_EXPECTED: dict(db_case.expected),
            CONFIG_VIDEO_PATH: db_case.video_path,
        }
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "failed to convert test case %s to a runner dict", db_case.id
        )
        return None


def load_db_cases(vis: "Viseron") -> list[RunnerCase]:
    """Read the ``test_cases`` table and produce runner-case dicts.

    Pure data — cases are not validated against the live camera list
    here. The runner tries to acquire a replay camera per case at run
    time; cases for unknown cameras fail individually with a log line
    rather than blocking other cases.
    """
    storage = vis.data.get(STORAGE_COMPONENT)
    if storage is None:
        LOGGER.warning(
            "storage component is not available; skipping db case load"
        )
        return []

    with storage.get_session() as session:
        rows = session.execute(select(TestCase)).scalars().all()

    out: list[RunnerCase] = []
    for row in rows:
        case_dict = _build_db_case_dict(row)
        if case_dict is None:
            continue
        out.append(case_dict)
    return out


# --- component holder ------------------------------------------------------


class TestRunnerComponent:
    """Holder object stored on ``vis.data`` for the duration of the process.

    Keeps two parallel sources of runnable cases, both as pure data:

    * ``yaml_cases`` — produced once at setup time from tests.yaml.
    * ``db_cases`` — read from the ``test_cases`` catalog table,
      re-read on demand by the REST layer when the user creates or
      deletes a case via the UI.

    At :meth:`trigger_run`, cases are unioned and handed to a
    :class:`TestRunner` which acquires replay cameras on demand — no
    camera synthesis or restart required.
    """

    def __init__(
        self,
        vis: "Viseron",
        config: dict[str, Any],
        *,
        yaml_cases: list[RunnerCase] | None = None,
        db_cases: list[RunnerCase] | None = None,
    ) -> None:
        self._vis = vis
        self._config = config
        self._yaml_cases: list[RunnerCase] = yaml_cases or []
        self._db_cases: list[RunnerCase] = db_cases or []
        self._current_runner: "TestRunner | None" = None
        self._lock = threading.Lock()

    @property
    def config(self) -> dict[str, Any]:
        """Validated test_runner config block."""
        return self._config

    @property
    def yaml_cases(self) -> list[RunnerCase]:
        """Runner-ready case dicts generated from tests.yaml at setup time."""
        return list(self._yaml_cases)

    @property
    def db_cases(self) -> list[RunnerCase]:
        """Runner-ready case dicts derived from the test_cases table."""
        return list(self._db_cases)

    def refresh_db_cases(self) -> None:
        """Reload DB cases. No-op if storage is unavailable."""
        self._db_cases = load_db_cases(self._vis)

    @property
    def current_runner(self) -> "TestRunner | None":
        """Return the most recently started runner (may be finished)."""
        return self._current_runner

    @property
    def is_running(self) -> bool:
        """True if a run is currently in flight."""
        runner = self._current_runner
        return runner is not None and not runner.completion_event.is_set()

    def _collect_runnable_cases(self) -> list[RunnerCase]:
        """Union tests.yaml cases with DB cases — both are pure data now."""
        return list(self._yaml_cases) + list(self._db_cases)

    def trigger_run(
        self,
        *,
        auto_correct: bool = False,
        max_repetitions: int = 5,
    ) -> "TestRunner":
        """Start a new run, refusing if one is already in flight."""
        with self._lock:
            if self.is_running:
                raise RuntimeError("a test run is already in progress")
            cases = self._collect_runnable_cases()
            if not cases:
                raise RuntimeError(
                    "no runnable test cases — declare them under 'cameras' "
                    "in tests.yaml or create one via the 'Use for test' "
                    "dialog"
                )
            merged_config = dict(self._config)
            merged_config["cases"] = cases
            runner = TestRunner(
                self._vis,
                merged_config,
                auto_correct=auto_correct,
                max_repetitions=max_repetitions,
            )
            self._current_runner = runner
            runner.start()
            return runner


# --- runner ----------------------------------------------------------------


class TestRunner:
    """Execute test cases sequentially, one source-camera group at a time.

    For each source camera the runner:
    1. Starts the dormant test cameras.
    2. Waits for the observation window.
    3. Evaluates the cases.
    4. Stops the cameras (freeing ffmpeg processes and memory).
    """

    def __init__(
        self,
        vis: "Viseron",
        config: dict[str, Any],
        *,
        auto_correct: bool = False,
        max_repetitions: int = 5,
    ) -> None:
        self._vis = vis
        self._config = config
        self._cases: list[RunnerCase] = config["cases"]
        self._storage: "Storage" = vis.data[STORAGE_COMPONENT]
        self._thread: threading.Thread | None = None
        self._completion = threading.Event()
        self._summary: RunSummary | None = None
        self._auto_correct = auto_correct
        self._max_repetitions = max_repetitions
        self._progress: list[CameraGroupProgress] = []
        self._recommendations: list[dict[str, Any]] = []

    @property
    def completion_event(self) -> threading.Event:
        """Signalled once the run has written all results and finalised."""
        return self._completion

    @property
    def summary(self) -> RunSummary | None:
        """The final run summary, or None if the run has not finished."""
        return self._summary

    @property
    def progress(self) -> list[CameraGroupProgress]:
        """Per-camera-group progress for sequential execution."""
        return list(self._progress)

    @property
    def recommendations(self) -> list[dict[str, Any]]:
        """Recommended parameter adjustments from the last run."""
        return list(self._recommendations)

    def start(self) -> None:
        """Kick off the run in a background thread."""
        self._thread = threading.Thread(
            target=self._run,
            name="viseron.test_runner",
            daemon=True,
        )
        self._thread.start()

    # -- grouping helpers --------------------------------------------------

    def _group_cases_by_source(self) -> dict[str, list[RunnerCase]]:
        """Group cases by CONFIG_SOURCE_CAMERA, preserving insertion order."""
        groups: dict[str, list[RunnerCase]] = collections.OrderedDict()
        for case in self._cases:
            src = case[CONFIG_SOURCE_CAMERA]
            groups.setdefault(src, []).append(case)
        return groups

    # -- sequential run ----------------------------------------------------

    def _run(self) -> None:
        run_id: int | None = None
        try:
            run_id = self._insert_run_row()
            groups = self._group_cases_by_source()

            # Initialize progress tracking.
            self._progress = [
                CameraGroupProgress(
                    source_camera=src,
                    total=len(cases),
                )
                for src, cases in groups.items()
            ]

            LOGGER.info(
                "test_runner: starting run %d with %d case(s) across "
                "%d camera group(s)",
                run_id,
                len(self._cases),
                len(groups),
            )

            manager = get_manager(self._vis)
            if manager is None:
                raise RuntimeError(
                    "ReplayCameraManager is not initialised — test_runner "
                    "cannot spawn replay cameras"
                )

            all_outcomes: list[tuple[RunnerCase, CaseOutcome]] = []

            for group_idx, (source_camera, cases) in enumerate(
                groups.items()
            ):
                prog = self._progress[group_idx]
                prog.status = "running"
                replay_id = replay_camera_id(source_camera)

                # Purge any prior test rows on this replay identifier so
                # evaluate_case sees only detections produced by this run.
                self._clear_stale_detections({replay_id})

                # Cases for the same source camera must run sequentially —
                # only one replay camera exists per real camera at a time.
                for case in cases:
                    duration = int(case[CONFIG_DURATION])
                    LOGGER.info(
                        "test_runner: acquiring replay for %s (case=%r, dur=%ds)",
                        source_camera,
                        case[CONFIG_NAME],
                        duration,
                    )
                    try:
                        handle = manager.acquire(
                            source_camera, "test", case[CONFIG_VIDEO_PATH]
                        )
                    except ReplayBusy as exc:
                        LOGGER.warning(
                            "test_runner: skipping case %r — %s",
                            case[CONFIG_NAME],
                            exc,
                        )
                        prog.failed += 1
                        continue
                    except Exception:  # pylint: disable=broad-except
                        LOGGER.exception(
                            "test_runner: failed to acquire replay for case %r",
                            case[CONFIG_NAME],
                        )
                        prog.failed += 1
                        continue

                    try:
                        time.sleep(duration)
                        outcome = evaluate_case(
                            kind=case[CONFIG_KIND],
                            get_session=self._storage.get_session,
                            camera_identifier=handle.replay_camera_id,
                            expected=case[CONFIG_EXPECTED],
                        )
                        all_outcomes.append((case, outcome))
                        if outcome.passed:
                            prog.passed += 1
                        else:
                            prog.failed += 1
                    finally:
                        try:
                            manager.release(handle.token)
                        except Exception:  # pylint: disable=broad-except
                            LOGGER.exception(
                                "test_runner: error releasing replay for %r",
                                case[CONFIG_NAME],
                            )

                prog.status = "done"
                LOGGER.info(
                    "test_runner: %s done — %d passed, %d failed",
                    source_camera,
                    prog.passed,
                    prog.failed,
                )

            # Persist results.
            self._persist_results(run_id, all_outcomes)
            total_passed = sum(
                1 for (_, o) in all_outcomes if o.passed
            )
            total_failed = len(all_outcomes) - total_passed

            # Generate recommendations from failures.
            self._recommendations = self._generate_recommendations(
                all_outcomes
            )

            self._finalize_run(run_id, total_passed, total_failed, STATUS_COMPLETE)
            self._summary = RunSummary(
                run_id=run_id,
                total=len(all_outcomes),
                passed=total_passed,
                failed=total_failed,
                status=STATUS_COMPLETE,
            )

            # Log results.
            for case, outcome in all_outcomes:
                marker = "PASS" if outcome.passed else "FAIL"
                LOGGER.info(
                    "test_runner: [%s] %s (%s) — %s",
                    marker,
                    case[CONFIG_NAME],
                    case[CONFIG_SOURCE_CAMERA],
                    outcome.message,
                )
            LOGGER.info(
                "test_runner: run %d complete — %d passed, %d failed",
                run_id,
                total_passed,
                total_failed,
            )
            if self._recommendations:
                LOGGER.info(
                    "test_runner: %d recommendation(s) generated",
                    len(self._recommendations),
                )

            self._vis.exit_code = 0 if total_failed == 0 else 1

            # Auto-correct: apply recommendations and restart.
            if (
                self._auto_correct
                and total_failed > 0
                and self._recommendations
            ):
                self._apply_auto_correct(run_id)

        except Exception as err:  # pylint: disable=broad-except
            LOGGER.exception("test_runner: run failed")
            if run_id is not None:
                try:
                    self._finalize_run(
                        run_id, 0, 0, STATUS_ERROR, error=str(err)
                    )
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "test_runner: failed to finalize errored run"
                    )
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

    # -- helpers -----------------------------------------------------------

    def _clear_stale_detections(self, cam_ids: set[str]) -> None:
        """Remove existing test-mode rows for the given cameras."""
        if not cam_ids:
            return
        identifiers = sorted(cam_ids)
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

    def _persist_results(
        self,
        run_id: int,
        outcomes: list[tuple[RunnerCase, CaseOutcome]],
    ) -> None:
        with self._storage.get_session() as session:
            for case, outcome in outcomes:
                stmt = insert(TestResult).values(
                    run_id=run_id,
                    case_name=case[CONFIG_NAME],
                    camera_identifier=replay_camera_id(
                        case[CONFIG_SOURCE_CAMERA]
                    ),
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

    # -- recommendations ---------------------------------------------------

    def _generate_recommendations(
        self, outcomes: list[tuple[RunnerCase, CaseOutcome]]
    ) -> list[dict[str, Any]]:
        """Generate parameter adjustment recommendations from failures."""
        from .auto_tuner import Adjustment, propose_adjustments

        results = [
            {
                "case_name": case[CONFIG_NAME],
                "passed": outcome.passed,
                "actual": outcome.actual,
                "expected": case[CONFIG_EXPECTED],
            }
            for case, outcome in outcomes
        ]

        try:
            from viseron.components.test_runner.auto_tuner import _load_config

            config = _load_config()
        except Exception:  # pylint: disable=broad-except
            LOGGER.warning(
                "test_runner: could not load config for recommendations"
            )
            return []

        adjustments = propose_adjustments(self._cases, results, config)
        return [
            {
                "source_camera": a.source_camera,
                "domain": a.domain,
                "component": a.component,
                "param_path": a.param_path,
                "old_value": a.old_value,
                "new_value": a.new_value,
                "reason": a.reason,
            }
            for a in adjustments
        ]

    # -- auto-correct ------------------------------------------------------

    def _apply_auto_correct(self, run_id: int) -> None:
        """Apply recommended adjustments to config.yaml and restart."""
        import os
        import signal

        from viseron.const import RESTART_EXIT_CODE

        from .auto_tuner import Adjustment, apply_adjustments

        adjustments = [
            Adjustment(
                source_camera=r["source_camera"],
                domain=r["domain"],
                component=r["component"],
                param_path=r["param_path"],
                old_value=r["old_value"],
                new_value=r["new_value"],
                reason=r["reason"],
            )
            for r in self._recommendations
        ]

        LOGGER.info(
            "test_runner: auto-correct applying %d adjustment(s) and "
            "requesting restart",
            len(adjustments),
        )
        apply_adjustments(adjustments)

        # Store auto-correct state so the next boot can resume.
        with self._storage.get_session() as session:
            stmt = (
                update(TestRun)
                .where(TestRun.id == run_id)
                .values(
                    auto_correct_state={
                        "enabled": True,
                        "max_repetitions": self._max_repetitions,
                        "current_repetition": 1,
                    },
                )
            )
            session.execute(stmt)
            session.commit()

        self._vis.exit_code = RESTART_EXIT_CODE
        os.kill(os.getpid(), signal.SIGINT)


__all__ = [
    "CameraGroupProgress",
    "RunSummary",
    "TestRunner",
    "TestRunnerComponent",
    "inject_yaml_cases",
    "load_and_inject_tests_yaml",
    "load_db_cases",
    "KIND_MOTION",
    "KIND_OBJECT",
]
