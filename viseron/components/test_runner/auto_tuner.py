"""Automatic parameter tuning based on test results.

Analyzes test run failures and iteratively adjusts detection parameters
(motion thresholds, object confidence, size filters, etc.) to make tests
pass.  Each iteration:

1. Runs all test cases via the existing TestRunner.
2. Collects failures and classifies them (false-negative / false-positive).
3. Proposes parameter adjustments using a binary-search-style step toward
   the boundary that separates positives from negatives.
4. Applies changes to ``config.yaml`` through the Tune API handlers.
5. Triggers a Viseron restart so the new config takes effect.
6. Repeats until all tests pass or the iteration budget is exhausted.
"""

from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML

from viseron.const import CONFIG_PATH, RESTART_EXIT_CODE

from .const import (
    CONFIG_CAMERA,
    CONFIG_EXPECTED,
    CONFIG_KIND,
    CONFIG_NAME,
    CONFIG_POLARITY,
    CONFIG_SOURCE_CAMERA,
    EXPECTED_DETECTED,
    EXPECTED_LABELS,
    KIND_MOTION,
    KIND_OBJECT,
    POLARITY_NEGATIVE,
    POLARITY_POSITIVE,
    STATUS_COMPLETE,
)

if TYPE_CHECKING:
    from viseron import Viseron

    from .runner import RunSummary, RunnerCase, TestRunnerComponent

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Adjustment limits — we never push parameters beyond these boundaries.
# ---------------------------------------------------------------------------
MOTION_THRESHOLD_MIN = 1
MOTION_THRESHOLD_MAX = 254
MOTION_AREA_MIN = 0.001
MOTION_AREA_MAX = 0.99
MOTION_LEARNING_RATE_MIN = 0.001
MOTION_LEARNING_RATE_MAX = 0.5
MOTION_ALPHA_MIN = 0.01
MOTION_ALPHA_MAX = 0.5

OBJECT_CONFIDENCE_MIN = 0.05
OBJECT_CONFIDENCE_MAX = 0.99
OBJECT_SIZE_MIN = 0.0
OBJECT_SIZE_MAX = 1.0

# How far toward the boundary we move per iteration (0.5 = bisect).
STEP_FACTOR = 0.5

DEFAULT_MAX_ITERATIONS = 10


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Adjustment:
    """A single proposed parameter change."""

    source_camera: str
    domain: str  # "motion_detector" or "object_detector"
    component: str  # e.g. "mog2", "codeprojectai"
    param_path: list[str]  # e.g. ["threshold"] or ["labels", "person", "confidence"]
    old_value: float
    new_value: float
    reason: str


@dataclass
class IterationResult:
    """Outcome of one tune-then-test cycle."""

    iteration: int
    run_id: int | None
    total: int
    passed: int
    failed: int
    adjustments: list[Adjustment]
    status: str  # "improved", "no_change", "complete", "error"
    message: str = ""


@dataclass
class AutoTuneState:
    """Mutable state of an in-flight auto-tune session."""

    max_iterations: int = DEFAULT_MAX_ITERATIONS
    iterations: list[IterationResult] = field(default_factory=list)
    status: str = "running"  # "running", "complete", "error", "cancelled"
    message: str = ""
    started_at: float = 0.0
    finished_at: float | None = None

    @property
    def current_iteration(self) -> int:
        return len(self.iterations)

    def serialize(self) -> dict[str, Any]:
        """Return a JSON-safe dict for the REST API."""
        return {
            "status": self.status,
            "message": self.message,
            "max_iterations": self.max_iterations,
            "current_iteration": self.current_iteration,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "iterations": [
                {
                    "iteration": it.iteration,
                    "run_id": it.run_id,
                    "total": it.total,
                    "passed": it.passed,
                    "failed": it.failed,
                    "status": it.status,
                    "message": it.message,
                    "adjustments": [
                        {
                            "source_camera": adj.source_camera,
                            "domain": adj.domain,
                            "component": adj.component,
                            "param_path": adj.param_path,
                            "old_value": adj.old_value,
                            "new_value": adj.new_value,
                            "reason": adj.reason,
                        }
                        for adj in it.adjustments
                    ],
                }
                for it in self.iterations
            ],
        }


# ---------------------------------------------------------------------------
# Config introspection
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, Any]:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.load(fh) or {}


def _save_config(config: dict[str, Any]) -> None:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        yaml.dump(config, fh)


def _find_motion_component(config: dict[str, Any]) -> str | None:
    """Return the first motion detector component name found in config."""
    for comp in ("mog2", "background_subtractor"):
        if comp in config and isinstance(config[comp], dict):
            if "motion_detector" in config[comp]:
                return comp
    return None


def _find_object_component(config: dict[str, Any]) -> str | None:
    """Return the first object detector component name found in config."""
    for comp in (
        "codeprojectai",
        "yolo",
        "darknet",
        "edgetpu",
        "hailo",
        "deepstack",
    ):
        if comp in config and isinstance(config[comp], dict):
            if "object_detector" in config[comp]:
                return comp
    return None


def _get_motion_camera_config(
    config: dict[str, Any], component: str, camera_id: str
) -> dict[str, Any] | None:
    """Return the motion detector camera config block, or None."""
    try:
        return config[component]["motion_detector"]["cameras"][camera_id]
    except (KeyError, TypeError):
        return None


def _get_object_camera_config(
    config: dict[str, Any], component: str, camera_id: str
) -> dict[str, Any] | None:
    """Return the object detector camera config block, or None."""
    try:
        return config[component]["object_detector"]["cameras"][camera_id]
    except (KeyError, TypeError):
        return None


def _ensure_motion_camera_config(
    config: dict[str, Any], component: str, camera_id: str
) -> dict[str, Any]:
    """Ensure the motion camera config block exists, creating it if needed."""
    comp_cfg = config.setdefault(component, {})
    md_cfg = comp_cfg.setdefault("motion_detector", {})
    cams = md_cfg.setdefault("cameras", {})
    if camera_id not in cams:
        cams[camera_id] = {}
    return cams[camera_id]


def _ensure_object_camera_config(
    config: dict[str, Any], component: str, camera_id: str
) -> dict[str, Any]:
    """Ensure the object camera config block exists, creating it if needed."""
    comp_cfg = config.setdefault(component, {})
    od_cfg = comp_cfg.setdefault("object_detector", {})
    cams = od_cfg.setdefault("cameras", {})
    if camera_id not in cams:
        cams[camera_id] = {}
    return cams[camera_id]


# ---------------------------------------------------------------------------
# Failure analysis
# ---------------------------------------------------------------------------


@dataclass
class FailureInfo:
    """Parsed information about a single test-case failure."""

    case_name: str
    source_camera: str
    kind: str  # "motion" or "object"
    polarity: str  # "positive" or "negative"
    expected: dict[str, Any]
    actual: dict[str, Any]


def _classify_failures(
    cases: list[RunnerCase],
    results: list[dict[str, Any]],
) -> list[FailureInfo]:
    """Match failed results back to their source cases and extract metadata."""
    case_map: dict[str, RunnerCase] = {c[CONFIG_NAME]: c for c in cases}
    failures: list[FailureInfo] = []
    for result in results:
        if result.get("passed"):
            continue
        case = case_map.get(result.get("case_name", ""))
        if case is None:
            continue
        failures.append(
            FailureInfo(
                case_name=case[CONFIG_NAME],
                source_camera=case[CONFIG_SOURCE_CAMERA],
                kind=case[CONFIG_KIND],
                polarity=case[CONFIG_POLARITY],
                expected=case[CONFIG_EXPECTED],
                actual=result.get("actual", {}),
            )
        )
    return failures


# ---------------------------------------------------------------------------
# Adjustment proposals
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _step_toward(current: float, target: float, factor: float = STEP_FACTOR) -> float:
    """Move ``current`` toward ``target`` by ``factor`` of the distance."""
    return current + factor * (target - current)


def _round_param(value: float, precision: int = 4) -> float:
    return round(value, precision)


def _propose_motion_adjustments(
    failure: FailureInfo,
    config: dict[str, Any],
) -> list[Adjustment]:
    """Propose motion-detector parameter adjustments for a single failure."""
    component = _find_motion_component(config)
    if component is None:
        LOGGER.warning(
            "auto_tuner: no motion detector component found in config"
        )
        return []

    cam_cfg = _get_motion_camera_config(
        config, component, failure.source_camera
    )

    # Default values depend on the component
    if component == "mog2":
        default_threshold = 15
        default_area = 0.08
    else:
        default_threshold = 15
        default_area = 0.08

    current_threshold = float(
        (cam_cfg or {}).get("threshold", default_threshold)
    )
    current_area = float((cam_cfg or {}).get("area", default_area))

    adjustments: list[Adjustment] = []

    if failure.polarity == POLARITY_POSITIVE:
        # False negative: motion expected but not detected.
        # -> Lower threshold (more sensitive), lower area (smaller motion triggers).
        new_threshold = _round_param(
            _step_toward(current_threshold, MOTION_THRESHOLD_MIN), 1
        )
        if new_threshold != current_threshold:
            adjustments.append(
                Adjustment(
                    source_camera=failure.source_camera,
                    domain="motion_detector",
                    component=component,
                    param_path=["threshold"],
                    old_value=current_threshold,
                    new_value=_clamp(
                        new_threshold, MOTION_THRESHOLD_MIN, MOTION_THRESHOLD_MAX
                    ),
                    reason=(
                        f"False negative on {failure.case_name!r}: lowering "
                        f"threshold to increase sensitivity"
                    ),
                )
            )
        new_area = _round_param(
            _step_toward(current_area, MOTION_AREA_MIN)
        )
        if new_area != current_area:
            adjustments.append(
                Adjustment(
                    source_camera=failure.source_camera,
                    domain="motion_detector",
                    component=component,
                    param_path=["area"],
                    old_value=current_area,
                    new_value=_clamp(new_area, MOTION_AREA_MIN, MOTION_AREA_MAX),
                    reason=(
                        f"False negative on {failure.case_name!r}: lowering "
                        f"area requirement to catch smaller motion"
                    ),
                )
            )
    else:
        # False positive: no motion expected but it was detected.
        # -> Raise threshold, raise area.
        new_threshold = _round_param(
            _step_toward(current_threshold, MOTION_THRESHOLD_MAX), 1
        )
        if new_threshold != current_threshold:
            adjustments.append(
                Adjustment(
                    source_camera=failure.source_camera,
                    domain="motion_detector",
                    component=component,
                    param_path=["threshold"],
                    old_value=current_threshold,
                    new_value=_clamp(
                        new_threshold, MOTION_THRESHOLD_MIN, MOTION_THRESHOLD_MAX
                    ),
                    reason=(
                        f"False positive on {failure.case_name!r}: raising "
                        f"threshold to reduce sensitivity"
                    ),
                )
            )
        new_area = _round_param(
            _step_toward(current_area, MOTION_AREA_MAX)
        )
        if new_area != current_area:
            adjustments.append(
                Adjustment(
                    source_camera=failure.source_camera,
                    domain="motion_detector",
                    component=component,
                    param_path=["area"],
                    old_value=current_area,
                    new_value=_clamp(new_area, MOTION_AREA_MIN, MOTION_AREA_MAX),
                    reason=(
                        f"False positive on {failure.case_name!r}: raising "
                        f"area requirement to ignore small noise"
                    ),
                )
            )

    return adjustments


def _propose_object_adjustments(
    failure: FailureInfo,
    config: dict[str, Any],
) -> list[Adjustment]:
    """Propose object-detector parameter adjustments for a single failure."""
    component = _find_object_component(config)
    if component is None:
        LOGGER.warning(
            "auto_tuner: no object detector component found in config"
        )
        return []

    cam_cfg = _get_object_camera_config(
        config, component, failure.source_camera
    )
    current_labels: list[dict[str, Any]] = (cam_cfg or {}).get("labels", [])
    label_map: dict[str, dict[str, Any]] = {
        lbl.get("label", ""): dict(lbl) for lbl in current_labels
    }

    adjustments: list[Adjustment] = []

    if failure.polarity == POLARITY_POSITIVE:
        # False negative: expected objects not detected.
        expected_labels = failure.expected.get(EXPECTED_LABELS, [])
        if not expected_labels:
            # "any detection" expectation — lower confidence on all labels
            for label_name, label_cfg in label_map.items():
                current_conf = float(label_cfg.get("confidence", 0.8))
                new_conf = _round_param(
                    _step_toward(current_conf, OBJECT_CONFIDENCE_MIN)
                )
                if new_conf != current_conf:
                    adjustments.append(
                        Adjustment(
                            source_camera=failure.source_camera,
                            domain="object_detector",
                            component=component,
                            param_path=["labels", label_name, "confidence"],
                            old_value=current_conf,
                            new_value=_clamp(
                                new_conf,
                                OBJECT_CONFIDENCE_MIN,
                                OBJECT_CONFIDENCE_MAX,
                            ),
                            reason=(
                                f"False negative on {failure.case_name!r}: "
                                f"lowering confidence for {label_name!r}"
                            ),
                        )
                    )
            # Also check scan_on_motion_only
            if cam_cfg and cam_cfg.get("scan_on_motion_only", True):
                adjustments.append(
                    Adjustment(
                        source_camera=failure.source_camera,
                        domain="object_detector",
                        component=component,
                        param_path=["scan_on_motion_only"],
                        old_value=1.0,
                        new_value=0.0,
                        reason=(
                            f"False negative on {failure.case_name!r}: disabling "
                            f"scan_on_motion_only so objects are detected even "
                            f"without motion trigger"
                        ),
                    )
                )
        else:
            # Specific labels expected — check each
            observed_labels = set(failure.actual.get("labels", {}).keys())
            for label_name in expected_labels:
                if label_name in observed_labels:
                    continue  # This label was detected, skip
                if label_name not in label_map:
                    # Label not configured at all — we'll add it
                    adjustments.append(
                        Adjustment(
                            source_camera=failure.source_camera,
                            domain="object_detector",
                            component=component,
                            param_path=["labels", label_name, "confidence"],
                            old_value=0.8,
                            new_value=0.5,
                            reason=(
                                f"Missing label {label_name!r} on "
                                f"{failure.case_name!r}: adding it with "
                                f"confidence 0.5"
                            ),
                        )
                    )
                else:
                    current_conf = float(
                        label_map[label_name].get("confidence", 0.8)
                    )
                    new_conf = _round_param(
                        _step_toward(current_conf, OBJECT_CONFIDENCE_MIN)
                    )
                    if new_conf != current_conf:
                        adjustments.append(
                            Adjustment(
                                source_camera=failure.source_camera,
                                domain="object_detector",
                                component=component,
                                param_path=[
                                    "labels",
                                    label_name,
                                    "confidence",
                                ],
                                old_value=current_conf,
                                new_value=_clamp(
                                    new_conf,
                                    OBJECT_CONFIDENCE_MIN,
                                    OBJECT_CONFIDENCE_MAX,
                                ),
                                reason=(
                                    f"False negative for {label_name!r} on "
                                    f"{failure.case_name!r}: lowering "
                                    f"confidence"
                                ),
                            )
                        )
                    # Also relax size filters if they're restrictive
                    for dim in ("height_min", "width_min"):
                        current_dim = float(
                            label_map[label_name].get(dim, 0)
                        )
                        if current_dim > 0.01:
                            new_dim = _round_param(
                                _step_toward(current_dim, OBJECT_SIZE_MIN)
                            )
                            adjustments.append(
                                Adjustment(
                                    source_camera=failure.source_camera,
                                    domain="object_detector",
                                    component=component,
                                    param_path=[
                                        "labels",
                                        label_name,
                                        dim,
                                    ],
                                    old_value=current_dim,
                                    new_value=_clamp(
                                        new_dim,
                                        OBJECT_SIZE_MIN,
                                        OBJECT_SIZE_MAX,
                                    ),
                                    reason=(
                                        f"Relaxing {dim} for {label_name!r} "
                                        f"on {failure.case_name!r}"
                                    ),
                                )
                            )
    else:
        # False positive: objects detected when none were expected.
        # -> Raise confidence on all labels for this camera.
        actual_labels = failure.actual.get("labels", {})
        for label_name in actual_labels:
            if label_name in label_map:
                current_conf = float(
                    label_map[label_name].get("confidence", 0.8)
                )
                new_conf = _round_param(
                    _step_toward(current_conf, OBJECT_CONFIDENCE_MAX)
                )
                if new_conf != current_conf:
                    adjustments.append(
                        Adjustment(
                            source_camera=failure.source_camera,
                            domain="object_detector",
                            component=component,
                            param_path=[
                                "labels",
                                label_name,
                                "confidence",
                            ],
                            old_value=current_conf,
                            new_value=_clamp(
                                new_conf,
                                OBJECT_CONFIDENCE_MIN,
                                OBJECT_CONFIDENCE_MAX,
                            ),
                            reason=(
                                f"False positive for {label_name!r} on "
                                f"{failure.case_name!r}: raising confidence"
                            ),
                        )
                    )

    return adjustments


def propose_adjustments(
    cases: list[RunnerCase],
    results: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[Adjustment]:
    """Analyze failures and return a deduplicated list of proposed adjustments."""
    failures = _classify_failures(cases, results)
    if not failures:
        return []

    all_adjustments: list[Adjustment] = []
    for failure in failures:
        if failure.kind == KIND_MOTION:
            all_adjustments.extend(
                _propose_motion_adjustments(failure, config)
            )
        elif failure.kind == KIND_OBJECT:
            all_adjustments.extend(
                _propose_object_adjustments(failure, config)
            )

    # Deduplicate: if we get conflicting adjustments for the same param
    # (one failure wants to raise, another wants to lower), pick the
    # average — it's the safest compromise.
    return _deduplicate_adjustments(all_adjustments)


def _deduplicate_adjustments(adjustments: list[Adjustment]) -> list[Adjustment]:
    """Merge adjustments that target the same parameter."""
    by_key: dict[str, list[Adjustment]] = {}
    for adj in adjustments:
        key = (
            f"{adj.source_camera}:{adj.domain}:{adj.component}"
            f":{'.'.join(adj.param_path)}"
        )
        by_key.setdefault(key, []).append(adj)

    merged: list[Adjustment] = []
    for group in by_key.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        # Average the proposed new values
        avg_new = sum(a.new_value for a in group) / len(group)
        base = group[0]
        reasons = "; ".join(a.reason for a in group)
        merged.append(
            Adjustment(
                source_camera=base.source_camera,
                domain=base.domain,
                component=base.component,
                param_path=base.param_path,
                old_value=base.old_value,
                new_value=_round_param(avg_new),
                reason=f"Compromise of {len(group)} adjustments: {reasons}",
            )
        )
    return merged


# ---------------------------------------------------------------------------
# Apply adjustments to config.yaml
# ---------------------------------------------------------------------------


def apply_adjustments(adjustments: list[Adjustment]) -> None:
    """Write all proposed adjustments to config.yaml."""
    if not adjustments:
        return

    config = _load_config()

    for adj in adjustments:
        if adj.domain == "motion_detector":
            cam_cfg = _ensure_motion_camera_config(
                config, adj.component, adj.source_camera
            )
            # Simple scalar params: threshold, area, learning_rate, alpha
            param_name = adj.param_path[0]
            # Use int for threshold since it's an integer param
            if param_name == "threshold":
                cam_cfg[param_name] = int(round(adj.new_value))
            else:
                cam_cfg[param_name] = adj.new_value

        elif adj.domain == "object_detector":
            cam_cfg = _ensure_object_camera_config(
                config, adj.component, adj.source_camera
            )
            if adj.param_path[0] == "scan_on_motion_only":
                cam_cfg["scan_on_motion_only"] = bool(adj.new_value)
            elif adj.param_path[0] == "labels" and len(adj.param_path) >= 3:
                label_name = adj.param_path[1]
                param_name = adj.param_path[2]
                labels_list: list[dict[str, Any]] = cam_cfg.get("labels", [])
                label_cfg = None
                for lbl in labels_list:
                    if lbl.get("label") == label_name:
                        label_cfg = lbl
                        break
                if label_cfg is None:
                    # Add a new label entry
                    label_cfg = {"label": label_name}
                    labels_list.append(label_cfg)
                    cam_cfg["labels"] = labels_list
                label_cfg[param_name] = adj.new_value

    _save_config(config)
    LOGGER.info("auto_tuner: applied %d adjustment(s) to config.yaml", len(adjustments))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class AutoTuner:
    """Run the tune-test-repeat loop in a background thread."""

    def __init__(
        self,
        vis: "Viseron",
        component: "TestRunnerComponent",
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self._vis = vis
        self._component = component
        self._state = AutoTuneState(
            max_iterations=max_iterations,
            started_at=time.time(),
        )
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def state(self) -> AutoTuneState:
        return self._state

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop,
            name="viseron.auto_tuner",
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _loop(self) -> None:
        try:
            for i in range(self._state.max_iterations):
                if self._cancel.is_set():
                    self._state.status = "cancelled"
                    self._state.message = f"Cancelled after {i} iteration(s)"
                    break

                LOGGER.info(
                    "auto_tuner: starting iteration %d/%d",
                    i + 1,
                    self._state.max_iterations,
                )

                # 1. Run tests
                try:
                    runner = self._component.trigger_run()
                except RuntimeError as err:
                    # A run is already in flight — wait for it
                    LOGGER.warning("auto_tuner: waiting for existing run: %s", err)
                    existing = self._component.current_runner
                    if existing is not None:
                        existing.completion_event.wait(timeout=600)
                    runner = self._component.trigger_run()

                runner.completion_event.wait(timeout=600)
                summary = runner.summary
                if summary is None:
                    self._state.iterations.append(
                        IterationResult(
                            iteration=i + 1,
                            run_id=None,
                            total=0,
                            passed=0,
                            failed=0,
                            adjustments=[],
                            status="error",
                            message="Test run produced no summary",
                        )
                    )
                    continue

                # 2. Check if all passed
                if summary.all_passed:
                    self._state.iterations.append(
                        IterationResult(
                            iteration=i + 1,
                            run_id=summary.run_id,
                            total=summary.total,
                            passed=summary.passed,
                            failed=summary.failed,
                            adjustments=[],
                            status="complete",
                            message="All tests passed!",
                        )
                    )
                    self._state.status = "complete"
                    self._state.message = (
                        f"All {summary.total} tests passed after "
                        f"{i + 1} iteration(s)"
                    )
                    break

                # 3. Collect results from DB
                from viseron.components.storage.const import (
                    COMPONENT as STORAGE_COMPONENT,
                )
                from viseron.components.storage.models import TestResult, TestRun

                from sqlalchemy import select

                storage = self._vis.data[STORAGE_COMPONENT]
                with storage.get_session() as session:
                    results_rows = (
                        session.execute(
                            select(TestResult).where(
                                TestResult.run_id == summary.run_id
                            )
                        )
                        .scalars()
                        .all()
                    )
                    results = [
                        {
                            "case_name": r.case_name,
                            "passed": r.passed,
                            "actual": r.actual,
                            "expected": r.expected,
                        }
                        for r in results_rows
                    ]

                cases = self._component._collect_runnable_cases()
                config = _load_config()

                # 4. Propose adjustments
                adjustments = propose_adjustments(cases, results, config)

                if not adjustments:
                    self._state.iterations.append(
                        IterationResult(
                            iteration=i + 1,
                            run_id=summary.run_id,
                            total=summary.total,
                            passed=summary.passed,
                            failed=summary.failed,
                            adjustments=[],
                            status="no_change",
                            message=(
                                f"{summary.failed} test(s) still failing but "
                                f"no further adjustments could be proposed"
                            ),
                        )
                    )
                    self._state.status = "complete"
                    self._state.message = (
                        f"Stopped: {summary.failed} failure(s) remain but no "
                        f"further parameter adjustments are possible"
                    )
                    break

                # 5. Apply adjustments
                apply_adjustments(adjustments)

                self._state.iterations.append(
                    IterationResult(
                        iteration=i + 1,
                        run_id=summary.run_id,
                        total=summary.total,
                        passed=summary.passed,
                        failed=summary.failed,
                        adjustments=adjustments,
                        status="improved",
                        message=(
                            f"{summary.failed} failure(s); applied "
                            f"{len(adjustments)} adjustment(s)"
                        ),
                    )
                )

                # 6. Restart Viseron so new config takes effect
                LOGGER.info(
                    "auto_tuner: requesting restart for iteration %d", i + 1
                )
                self._vis.exit_code = RESTART_EXIT_CODE
                os.kill(os.getpid(), signal.SIGINT)
                # After sending SIGINT, the process will shut down and come back.
                # The auto-tune state is lost — the frontend must re-trigger on
                # the next boot if it wants to continue. We store progress in the
                # iterations list so the last state can be serialized before
                # shutdown.
                self._state.status = "restarting"
                self._state.message = (
                    f"Restarting Viseron after iteration {i + 1} to apply "
                    f"{len(adjustments)} config change(s). The auto-tune will "
                    f"resume when you trigger it again after restart."
                )
                break
            else:
                # Exhausted all iterations without full success
                last = self._state.iterations[-1] if self._state.iterations else None
                remaining = last.failed if last else 0
                self._state.status = "complete"
                self._state.message = (
                    f"Reached maximum {self._state.max_iterations} iterations "
                    f"with {remaining} failure(s) remaining"
                )
        except Exception as err:
            LOGGER.exception("auto_tuner: unexpected error")
            self._state.status = "error"
            self._state.message = str(err)
        finally:
            self._state.finished_at = time.time()
