"""Evaluate collected detections against a test case's expectation.

The runner clears all ``test=true`` rows for referenced cameras at the
start of every run, so the evaluator can simply query every test-mode
row for a given camera_identifier — whatever it finds is guaranteed to
belong to the current run.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from viseron.components.storage.models import Motion, Objects

from .const import (
    EXPECTED_DETECTED,
    EXPECTED_LABELS,
    KIND_MOTION,
    KIND_OBJECT,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class CaseOutcome:
    """Outcome of evaluating a single test case."""

    passed: bool
    actual: dict[str, Any]
    message: str = ""
    snapshot_path: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _collect_motion(
    get_session: Callable[[], "Session"],
    camera_identifier: str,
) -> list[Motion]:
    with get_session() as session:
        stmt = (
            select(Motion)
            .where(Motion.camera_identifier == camera_identifier)
            .where(Motion.test.is_(True))
            .order_by(Motion.start_time.asc())
        )
        return list(session.execute(stmt).scalars().all())


def _collect_objects(
    get_session: Callable[[], "Session"],
    camera_identifier: str,
) -> list[Objects]:
    with get_session() as session:
        stmt = (
            select(Objects)
            .where(Objects.camera_identifier == camera_identifier)
            .where(Objects.test.is_(True))
            .order_by(Objects.created_at.asc())
        )
        return list(session.execute(stmt).scalars().all())


def evaluate_motion(
    get_session: Callable[[], "Session"],
    camera_identifier: str,
    expected: dict[str, Any],
) -> CaseOutcome:
    """Evaluate a motion detection case.

    Supported expectations:
        - {"detected": true}  -> at least one motion row.
        - {"detected": false} -> zero motion rows.
    """
    want_detected = bool(expected.get(EXPECTED_DETECTED, True))
    rows = _collect_motion(get_session, camera_identifier)
    count = len(rows)
    got_detected = count > 0

    snapshot_path: str | None = None
    if rows and rows[0].snapshot_path:
        snapshot_path = rows[0].snapshot_path

    actual = {"detected": got_detected, "count": count}
    passed = got_detected == want_detected
    if passed:
        message = (
            f"{count} motion event(s) observed"
            if got_detected
            else "no motion observed (as expected)"
        )
    else:
        message = (
            f"expected motion but observed none"
            if want_detected
            else f"expected no motion but observed {count} event(s)"
        )
    return CaseOutcome(
        passed=passed,
        actual=actual,
        message=message,
        snapshot_path=snapshot_path,
    )


def evaluate_object(
    get_session: Callable[[], "Session"],
    camera_identifier: str,
    expected: dict[str, Any],
) -> CaseOutcome:
    """Evaluate an object detection case.

    Supported expectations:
        - {"detected": false}     -> zero object rows.
        - {"labels": ["person"]}  -> every listed label appeared at least
                                     once. Passing labels implies
                                     ``detected: true``.
    """
    rows = _collect_objects(get_session, camera_identifier)
    observed_labels: dict[str, int] = {}
    snapshot_path: str | None = None
    for row in rows:
        observed_labels[row.label] = observed_labels.get(row.label, 0) + 1
        if snapshot_path is None and row.snapshot_path:
            snapshot_path = row.snapshot_path

    expected_labels = expected.get(EXPECTED_LABELS)
    if expected_labels is not None:
        expected_labels = list(expected_labels)

    if expected_labels:
        missing = [label for label in expected_labels if label not in observed_labels]
        passed = not missing
        actual = {"labels": observed_labels, "count": len(rows)}
        if passed:
            message = f"all expected labels observed: {sorted(expected_labels)}"
        else:
            message = (
                f"missing expected labels: {sorted(missing)} "
                f"(observed: {sorted(observed_labels.keys())})"
            )
        return CaseOutcome(
            passed=passed,
            actual=actual,
            message=message,
            snapshot_path=snapshot_path,
        )

    want_detected = bool(expected.get(EXPECTED_DETECTED, True))
    got_detected = bool(rows)
    actual = {
        "detected": got_detected,
        "labels": observed_labels,
        "count": len(rows),
    }
    passed = got_detected == want_detected
    if passed:
        message = (
            f"{len(rows)} object detection(s) observed"
            if got_detected
            else "no objects observed (as expected)"
        )
    else:
        message = (
            "expected object detection but observed none"
            if want_detected
            else f"expected no objects but observed {len(rows)}"
        )
    return CaseOutcome(
        passed=passed,
        actual=actual,
        message=message,
        snapshot_path=snapshot_path,
    )


def evaluate_case(
    kind: str,
    get_session: Callable[[], "Session"],
    camera_identifier: str,
    expected: dict[str, Any],
) -> CaseOutcome:
    """Dispatch evaluation by case kind."""
    if kind == KIND_MOTION:
        return evaluate_motion(get_session, camera_identifier, expected)
    if kind == KIND_OBJECT:
        return evaluate_object(get_session, camera_identifier, expected)
    return CaseOutcome(
        passed=False,
        actual={},
        message=f"unknown case kind: {kind!r}",
    )
