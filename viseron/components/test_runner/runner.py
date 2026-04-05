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
    CONFIG_DEFAULT_DURATION,
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


# --- synthetic camera synthesis --------------------------------------------


def _synth_test_camera_id(camera_identifier: str, slug: str) -> str:
    """Return a synthetic ffmpeg camera identifier for a test case.

    Used by both the tests.yaml injection path and the DB-catalog
    injection path. Deterministic so the pending_restart decoration in
    the REST API can compute the same id at request time.
    """
    return f"test_{camera_identifier}_{slug}"


def _clone_target_camera(target_config: dict[str, Any]) -> dict[str, Any]:
    """Deep-clone a real camera's ffmpeg config for use as a test camera.

    The clone is pruned of fields that don't apply to a file-sourced
    stream (substreams, passwords) and stamped with placeholders for
    network fields that ffmpeg still requires in its schema but ignores
    when ``file_source`` is set.
    """
    cloned = copy.deepcopy(target_config)
    # file_source-backed cameras don't use a substream — drop it if the
    # real camera had one so we don't spin up a second pipe to nowhere.
    cloned.pop("substream", None)
    # Credentials from the real camera are irrelevant for a local file.
    cloned.pop("username", None)
    cloned.pop("password", None)
    # The network triple is required by the schema but unused when
    # file_source is set; overwrite with safe placeholders.
    cloned["host"] = "localhost"
    cloned["port"] = 554
    cloned["path"] = "/"
    return cloned


def _build_synthetic_camera(
    target_config: dict[str, Any],
    *,
    display_name: str,
    file_source: str,
) -> dict[str, Any]:
    """Turn a real camera config into a synthetic test-mode camera config."""
    synth = _clone_target_camera(target_config)
    synth["name"] = display_name
    synth["test_mode"] = True
    synth["file_source"] = file_source
    return synth


# --- tests.yaml driven injection -------------------------------------------


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

    For every leaf source in tests.yaml this function:

    1. Resolves the source to an on-disk MP4 path (materializing timeline
       ranges from the ``Files`` table when needed).
    2. Deep-clones the parent real camera's ffmpeg config block.
    3. Registers the clone as a synthetic ffmpeg camera with
       ``test_mode=true`` and the resolved ``file_source``.
    4. Emits a runner case dict referencing the synthetic camera.

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

    injected: list[RunnerCase] = []
    for flat in flat_cases:
        parent_id: str = flat["source_camera"]
        target_config = ffmpeg_cameras.get(parent_id)
        if target_config is None:
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

        synth_id = _synth_test_camera_id(parent_id, flat["slug"])
        if synth_id not in ffmpeg_cameras:
            display_name = f"Test {parent_id} — {flat['name']}"
            ffmpeg_cameras[synth_id] = _build_synthetic_camera(
                target_config,
                display_name=display_name,
                file_source=resolved_path,
            )
            LOGGER.info(
                "test_runner: injected synthetic camera %r for case %r",
                synth_id,
                flat["name"],
            )

        injected.append(
            {
                CONFIG_NAME: flat["name"],
                CONFIG_CAMERA: synth_id,
                CONFIG_SOURCE_CAMERA: parent_id,
                CONFIG_KIND: flat["kind"],
                CONFIG_POLARITY: flat["polarity"],
                CONFIG_DURATION: _effective_duration(flat, default_duration),
                CONFIG_EXPECTED: dict(flat["expected"]),
                CONFIG_VIDEO_PATH: resolved_path,
            }
        )
    return injected


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


# --- DB-catalog driven injection (unchanged behavior, kept for the UI) -----


def _build_db_case_dict(db_case: TestCase) -> RunnerCase | None:
    """Convert a TestCase row into a runner-compatible case dict."""
    try:
        return {
            CONFIG_NAME: db_case.name,
            CONFIG_CAMERA: _synth_test_camera_id(
                db_case.camera_identifier, db_case.slug
            ),
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


def inject_db_cases(
    vis: "Viseron", config: dict[str, Any]
) -> list[RunnerCase]:
    """Read the ``test_cases`` table and inject synthetic ffmpeg cameras.

    Mirrors :func:`inject_yaml_cases` but sources its entries from the DB
    catalog populated by the ``Use for test`` dialog. Both paths coexist
    so the UI remains functional alongside the declarative tests.yaml
    flow.
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

    injected_cases: list[RunnerCase] = []
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
        case_dict = _build_db_case_dict(row)
        if case_dict is None:
            continue

        if test_camera_id not in ffmpeg_cameras:
            ffmpeg_cameras[test_camera_id] = _build_synthetic_camera(
                target_config,
                display_name=f"Test {row.camera_identifier} — {row.name}",
                file_source=row.video_path,
            )
            LOGGER.info(
                "injected test camera %r for db case %r (target: %r)",
                test_camera_id,
                row.name,
                row.camera_identifier,
            )

        injected_cases.append(case_dict)
    return injected_cases


# --- component holder ------------------------------------------------------


class TestRunnerComponent:
    """Holder object stored on ``vis.data`` for the duration of the process.

    Keeps two parallel sources of runnable cases:

    * ``yaml_cases`` — produced once at setup time from tests.yaml by
      :func:`inject_yaml_cases`. These have matching synthetic ffmpeg
      cameras registered before the ffmpeg component set up.
    * ``db_cases`` — produced from the ``test_cases`` catalog table,
      refreshed on demand by the REST layer when the user creates a
      case via the UI.

    Both are unioned at ``trigger_run`` time. DB cases whose synthesized
    camera hasn't been registered yet (pending restart) are skipped.
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
        refreshed: list[RunnerCase] = []
        for row in rows:
            case_dict = _build_db_case_dict(row)
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

    def _collect_runnable_cases(self) -> list[RunnerCase]:
        """Union tests.yaml cases with DB cases that have live cameras.

        YAML cases are always runnable — their synthetic cameras were
        registered at setup time. DB cases whose synthesized camera is
        not yet registered (pending restart) are skipped with a clear
        log line.
        """
        runnable: list[RunnerCase] = list(self._yaml_cases)
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
                    "no runnable test cases — declare them under 'cameras' "
                    "in tests.yaml or create one via the 'Use for test' "
                    "dialog (and restart Viseron for catalog cases)"
                )
            merged_config = dict(self._config)
            merged_config["cases"] = cases
            runner = TestRunner(self._vis, merged_config)
            self._current_runner = runner
            runner.start()
            return runner


# --- runner ----------------------------------------------------------------


class TestRunner:
    """Execute a set of test cases against running cameras."""

    def __init__(self, vis: "Viseron", config: dict[str, Any]) -> None:
        self._vis = vis
        self._config = config
        self._cases: list[RunnerCase] = config["cases"]
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
    ) -> list[tuple[RunnerCase, CaseOutcome]]:
        results: list[tuple[RunnerCase, CaseOutcome]] = []
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
        outcomes: list[tuple[RunnerCase, CaseOutcome]],
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


__all__ = [
    "RunSummary",
    "TestRunner",
    "TestRunnerComponent",
    "inject_db_cases",
    "inject_yaml_cases",
    "load_and_inject_tests_yaml",
    "KIND_MOTION",
    "KIND_OBJECT",
    "_synth_test_camera_id",
]
