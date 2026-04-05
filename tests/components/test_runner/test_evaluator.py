"""Tests for the test_runner evaluator."""
from __future__ import annotations

import datetime

from sqlalchemy import insert
from sqlalchemy.orm.session import Session, sessionmaker

from viseron.components.storage.models import Motion, Objects
from viseron.components.test_runner.const import (
    EXPECTED_DETECTED,
    EXPECTED_LABELS,
    KIND_MOTION,
    KIND_OBJECT,
)
from viseron.components.test_runner.evaluator import (
    evaluate_case,
    evaluate_motion,
    evaluate_object,
)

CAMERA = "test_runner_camera"
OTHER_CAMERA = "unrelated_camera"


def _insert_motion(
    session_factory: sessionmaker[Session],
    *,
    camera_identifier: str = CAMERA,
    test: bool = True,
) -> None:
    with session_factory() as session:
        session.execute(
            insert(Motion).values(
                camera_identifier=camera_identifier,
                start_time=datetime.datetime(
                    2026, 4, 5, 10, 0, 0, tzinfo=datetime.timezone.utc
                ),
                end_time=datetime.datetime(
                    2026, 4, 5, 10, 0, 5, tzinfo=datetime.timezone.utc
                ),
                snapshot_path="/snapshots/motion.jpg",
                test=test,
            )
        )
        session.commit()


def _insert_object(
    session_factory: sessionmaker[Session],
    label: str,
    *,
    camera_identifier: str = CAMERA,
    test: bool = True,
    snapshot_path: str | None = "/snapshots/object.jpg",
) -> None:
    with session_factory() as session:
        session.execute(
            insert(Objects).values(
                camera_identifier=camera_identifier,
                label=label,
                confidence=0.9,
                width=0.5,
                height=0.5,
                x1=0.1,
                y1=0.1,
                x2=0.6,
                y2=0.6,
                snapshot_path=snapshot_path,
                test=test,
            )
        )
        session.commit()


class TestEvaluateMotion:
    """Tests for evaluate_motion."""

    def test_expected_detected_and_present(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_motion(get_db_session)
        outcome = evaluate_motion(
            get_db_session, CAMERA, {EXPECTED_DETECTED: True}
        )
        assert outcome.passed is True
        assert outcome.actual["detected"] is True
        assert outcome.actual["count"] == 1
        assert outcome.snapshot_path == "/snapshots/motion.jpg"

    def test_expected_detected_but_missing(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        outcome = evaluate_motion(
            get_db_session, CAMERA, {EXPECTED_DETECTED: True}
        )
        assert outcome.passed is False
        assert outcome.actual["count"] == 0

    def test_expected_no_motion_and_none(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        outcome = evaluate_motion(
            get_db_session, CAMERA, {EXPECTED_DETECTED: False}
        )
        assert outcome.passed is True
        assert "as expected" in outcome.message

    def test_expected_no_motion_but_present(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_motion(get_db_session)
        outcome = evaluate_motion(
            get_db_session, CAMERA, {EXPECTED_DETECTED: False}
        )
        assert outcome.passed is False

    def test_ignores_non_test_rows(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        # Live (non-test) rows for the same camera must not count.
        _insert_motion(get_db_session, test=False)
        outcome = evaluate_motion(
            get_db_session, CAMERA, {EXPECTED_DETECTED: True}
        )
        assert outcome.passed is False
        assert outcome.actual["count"] == 0

    def test_ignores_other_cameras(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_motion(get_db_session, camera_identifier=OTHER_CAMERA)
        outcome = evaluate_motion(
            get_db_session, CAMERA, {EXPECTED_DETECTED: True}
        )
        assert outcome.passed is False


class TestEvaluateObject:
    """Tests for evaluate_object."""

    def test_all_expected_labels_present(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_object(get_db_session, "person")
        _insert_object(get_db_session, "car")
        outcome = evaluate_object(
            get_db_session, CAMERA, {EXPECTED_LABELS: ["person", "car"]}
        )
        assert outcome.passed is True
        assert outcome.actual["labels"] == {"person": 1, "car": 1}

    def test_missing_label_fails(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_object(get_db_session, "person")
        outcome = evaluate_object(
            get_db_session, CAMERA, {EXPECTED_LABELS: ["person", "car"]}
        )
        assert outcome.passed is False
        assert "car" in outcome.message

    def test_expected_detected_false(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        outcome = evaluate_object(
            get_db_session, CAMERA, {EXPECTED_DETECTED: False}
        )
        assert outcome.passed is True

    def test_expected_detected_false_but_present(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_object(get_db_session, "person")
        outcome = evaluate_object(
            get_db_session, CAMERA, {EXPECTED_DETECTED: False}
        )
        assert outcome.passed is False

    def test_snapshot_path_from_first_row(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_object(
            get_db_session, "person", snapshot_path="/snapshots/first.jpg"
        )
        _insert_object(
            get_db_session, "car", snapshot_path="/snapshots/second.jpg"
        )
        outcome = evaluate_object(
            get_db_session, CAMERA, {EXPECTED_LABELS: ["person"]}
        )
        assert outcome.snapshot_path == "/snapshots/first.jpg"


class TestEvaluateCase:
    """Tests for the evaluate_case dispatcher."""

    def test_motion_kind(self, get_db_session: sessionmaker[Session]) -> None:
        _insert_motion(get_db_session)
        outcome = evaluate_case(
            KIND_MOTION, get_db_session, CAMERA, {EXPECTED_DETECTED: True}
        )
        assert outcome.passed is True

    def test_object_kind(self, get_db_session: sessionmaker[Session]) -> None:
        _insert_object(get_db_session, "person")
        outcome = evaluate_case(
            KIND_OBJECT, get_db_session, CAMERA, {EXPECTED_LABELS: ["person"]}
        )
        assert outcome.passed is True

    def test_unknown_kind(self, get_db_session: sessionmaker[Session]) -> None:
        outcome = evaluate_case(
            "unknown", get_db_session, CAMERA, {}
        )
        assert outcome.passed is False
        assert "unknown" in outcome.message
