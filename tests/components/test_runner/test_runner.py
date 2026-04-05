"""Integration tests for the TestRunner orchestration class."""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm.session import Session, sessionmaker

from viseron.components.storage.const import COMPONENT as STORAGE_COMPONENT
from viseron.components.storage.models import (
    Motion,
    Objects,
    TestCase,
    TestResult,
    TestRun,
)
from viseron.components.test_runner.const import (
    COMPONENT,
    CONFIG_CAMERA,
    CONFIG_CAMERA_READY_TIMEOUT,
    CONFIG_CASES,
    CONFIG_DURATION,
    CONFIG_EXPECTED,
    CONFIG_KIND,
    CONFIG_NAME,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    CONFIG_VIDEO_PATH,
    EXPECTED_DETECTED,
    EXPECTED_LABELS,
    KIND_MOTION,
    KIND_OBJECT,
    STATUS_COMPLETE,
)
from viseron.components.test_runner.runner import (
    TestRunner,
    TestRunnerComponent,
    inject_db_cases,
)
from viseron.domains.camera.const import DOMAIN as CAMERA_DOMAIN
from viseron.exceptions import DomainNotRegisteredError

CAMERA_A = "test_runner_cam_a"
CAMERA_B = "test_runner_cam_b"


class _FakeStorage:
    """Storage stub that returns real SQLAlchemy sessions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_session(self, *_args: Any, **_kwargs: Any) -> Session:
        return self._session_factory()


class _FakeViseron:
    """Minimal Viseron stand-in with the bits TestRunner touches."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cameras: dict[str, Any],
    ) -> None:
        self.data = {STORAGE_COMPONENT: _FakeStorage(session_factory)}
        self._cameras = cameras
        self.exit_code = 0
        self.shutdown_called = False

    def get_registered_domain(self, domain: str, identifier: str) -> Any:
        if domain != CAMERA_DOMAIN:
            raise DomainNotRegisteredError(domain, identifier=identifier)
        try:
            return self._cameras[identifier]
        except KeyError as err:
            raise DomainNotRegisteredError(domain, identifier=identifier) from err

    def shutdown(self) -> None:
        self.shutdown_called = True


def _test_camera(identifier: str, *, is_test: bool = True) -> SimpleNamespace:
    return SimpleNamespace(identifier=identifier, is_test_camera=is_test)


def _make_config(
    cases: list[dict[str, Any]],
    *,
    shutdown_on_complete: bool = False,
    camera_ready_timeout: int = 5,
) -> dict[str, Any]:
    return {
        CONFIG_CASES: cases,
        CONFIG_SHUTDOWN_ON_COMPLETE: shutdown_on_complete,
        CONFIG_CAMERA_READY_TIMEOUT: camera_ready_timeout,
    }


def _motion_row(camera: str, *, test: bool = True) -> dict[str, Any]:
    now = datetime.datetime(2026, 4, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return {
        "camera_identifier": camera,
        "start_time": now,
        "end_time": now + datetime.timedelta(seconds=5),
        "snapshot_path": f"/snapshots/{camera}.jpg",
        "test": test,
    }


def _object_row(camera: str, label: str, *, test: bool = True) -> dict[str, Any]:
    return {
        "camera_identifier": camera,
        "label": label,
        "confidence": 0.95,
        "width": 0.3,
        "height": 0.3,
        "x1": 0.1,
        "y1": 0.1,
        "x2": 0.4,
        "y2": 0.4,
        "snapshot_path": f"/snapshots/{camera}_{label}.jpg",
        "test": test,
    }


class TestClearStaleDetections:
    """Verify that _clear_stale_detections wipes prior test-mode rows."""

    def test_clears_only_test_rows_for_referenced_cameras(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        with get_db_session() as session:
            session.execute(insert(Motion).values(**_motion_row(CAMERA_A)))
            session.execute(insert(Motion).values(**_motion_row(CAMERA_A, test=False)))
            session.execute(insert(Motion).values(**_motion_row(CAMERA_B)))
            session.execute(
                insert(Objects).values(**_object_row(CAMERA_A, "person"))
            )
            session.execute(
                insert(Objects).values(
                    **_object_row(CAMERA_A, "person", test=False)
                )
            )
            session.commit()

        cameras = {CAMERA_A: _test_camera(CAMERA_A)}
        vis = _FakeViseron(get_db_session, cameras)
        runner = TestRunner(
            vis,
            _make_config(
                [
                    {
                        CONFIG_NAME: "case",
                        CONFIG_CAMERA: CAMERA_A,
                        CONFIG_KIND: KIND_MOTION,
                        CONFIG_DURATION: 1,
                        CONFIG_EXPECTED: {EXPECTED_DETECTED: True},
                    }
                ]
            ),
        )
        runner._clear_stale_detections()  # pylint: disable=protected-access

        with get_db_session() as session:
            motion_rows = session.execute(select(Motion)).scalars().all()
            object_rows = session.execute(select(Objects)).scalars().all()

        # Test rows for CAMERA_A gone; live rows for CAMERA_A preserved;
        # CAMERA_B rows untouched because it is not referenced.
        assert {(m.camera_identifier, m.test) for m in motion_rows} == {
            (CAMERA_A, False),
            (CAMERA_B, True),
        }
        assert {(o.camera_identifier, o.test) for o in object_rows} == {
            (CAMERA_A, False),
        }


class TestFullRun:
    """Drive a complete run (minus the observation sleep) end to end."""

    def _make_runner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        get_db_session: sessionmaker[Session],
        cameras: dict[str, Any],
        cases: list[dict[str, Any]],
        *,
        shutdown_on_complete: bool = False,
    ) -> tuple[TestRunner, _FakeViseron]:
        vis = _FakeViseron(get_db_session, cameras)
        runner = TestRunner(
            vis,
            _make_config(cases, shutdown_on_complete=shutdown_on_complete),
        )
        # Skip the wall-clock sleep so tests run instantly.
        monkeypatch.setattr(
            runner,
            "_wait_for_observation_window",
            lambda: None,
        )
        return runner, vis

    def test_mixed_pass_and_fail(
        self,
        monkeypatch: pytest.MonkeyPatch,
        get_db_session: sessionmaker[Session],
    ) -> None:
        # Simulate detections that the pipeline would have produced.
        with get_db_session() as session:
            session.execute(insert(Motion).values(**_motion_row(CAMERA_A)))
            session.execute(
                insert(Objects).values(**_object_row(CAMERA_B, "person"))
            )
            session.commit()

        cameras = {
            CAMERA_A: _test_camera(CAMERA_A),
            CAMERA_B: _test_camera(CAMERA_B),
        }
        cases = [
            {
                CONFIG_NAME: "motion present",
                CONFIG_CAMERA: CAMERA_A,
                CONFIG_KIND: KIND_MOTION,
                CONFIG_DURATION: 1,
                CONFIG_EXPECTED: {EXPECTED_DETECTED: True},
            },
            {
                CONFIG_NAME: "object missing car",
                CONFIG_CAMERA: CAMERA_B,
                CONFIG_KIND: KIND_OBJECT,
                CONFIG_DURATION: 1,
                CONFIG_EXPECTED: {EXPECTED_LABELS: ["person", "car"]},
            },
        ]
        # The _clear_stale_detections step will wipe the rows we just
        # inserted, so insert a second time *after* creating the runner
        # but from inside a monkeypatched clear that is a no-op.
        runner, vis = self._make_runner(
            monkeypatch, get_db_session, cameras, cases
        )
        monkeypatch.setattr(
            runner,
            "_clear_stale_detections",
            lambda: None,
        )

        runner._run()  # pylint: disable=protected-access

        assert runner.completion_event.is_set()
        assert runner.summary is not None
        assert runner.summary.total == 2
        assert runner.summary.passed == 1
        assert runner.summary.failed == 1
        assert runner.summary.status == STATUS_COMPLETE
        assert vis.exit_code == 1
        assert vis.shutdown_called is False  # shutdown_on_complete defaulted off

        with get_db_session() as session:
            runs = session.execute(select(TestRun)).scalars().all()
            assert len(runs) == 1
            run = runs[0]
            assert run.total == 2
            assert run.passed == 1
            assert run.failed == 1
            assert run.status == STATUS_COMPLETE
            assert run.finished_at is not None

            results = (
                session.execute(
                    select(TestResult).order_by(TestResult.case_name)
                )
                .scalars()
                .all()
            )
            assert len(results) == 2
            by_name = {r.case_name: r for r in results}
            motion_result = by_name["motion present"]
            assert motion_result.passed is True
            assert motion_result.camera_identifier == CAMERA_A
            assert motion_result.kind == KIND_MOTION
            assert motion_result.actual["count"] == 1
            assert motion_result.snapshot_path == f"/snapshots/{CAMERA_A}.jpg"

            object_result = by_name["object missing car"]
            assert object_result.passed is False
            assert object_result.camera_identifier == CAMERA_B
            assert "car" in (object_result.message or "")

    def test_all_passing_sets_exit_code_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        get_db_session: sessionmaker[Session],
    ) -> None:
        with get_db_session() as session:
            session.execute(insert(Motion).values(**_motion_row(CAMERA_A)))
            session.commit()

        cameras = {CAMERA_A: _test_camera(CAMERA_A)}
        cases = [
            {
                CONFIG_NAME: "motion present",
                CONFIG_CAMERA: CAMERA_A,
                CONFIG_KIND: KIND_MOTION,
                CONFIG_DURATION: 1,
                CONFIG_EXPECTED: {EXPECTED_DETECTED: True},
            }
        ]
        runner, vis = self._make_runner(
            monkeypatch,
            get_db_session,
            cameras,
            cases,
            shutdown_on_complete=True,
        )
        monkeypatch.setattr(
            runner, "_clear_stale_detections", lambda: None
        )

        runner._run()  # pylint: disable=protected-access

        assert vis.exit_code == 0
        assert runner.summary is not None and runner.summary.passed == 1
        assert runner.summary.failed == 0
        assert vis.shutdown_called is True

    def test_refuses_camera_without_test_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
        get_db_session: sessionmaker[Session],
    ) -> None:
        cameras = {CAMERA_A: _test_camera(CAMERA_A, is_test=False)}
        cases = [
            {
                CONFIG_NAME: "nope",
                CONFIG_CAMERA: CAMERA_A,
                CONFIG_KIND: KIND_MOTION,
                CONFIG_DURATION: 1,
                CONFIG_EXPECTED: {EXPECTED_DETECTED: True},
            }
        ]
        runner, vis = self._make_runner(
            monkeypatch, get_db_session, cameras, cases
        )
        monkeypatch.setattr(
            runner, "_clear_stale_detections", lambda: None
        )

        runner._run()  # pylint: disable=protected-access

        assert runner.summary is not None
        assert runner.summary.status == "error"
        assert vis.exit_code == 1

    def test_missing_camera_times_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
        get_db_session: sessionmaker[Session],
    ) -> None:
        cases = [
            {
                CONFIG_NAME: "ghost",
                CONFIG_CAMERA: "does_not_exist",
                CONFIG_KIND: KIND_MOTION,
                CONFIG_DURATION: 1,
                CONFIG_EXPECTED: {EXPECTED_DETECTED: True},
            }
        ]
        vis = _FakeViseron(get_db_session, cameras={})
        runner = TestRunner(
            vis,
            {
                CONFIG_CASES: cases,
                CONFIG_SHUTDOWN_ON_COMPLETE: False,
                CONFIG_CAMERA_READY_TIMEOUT: 1,
            },
        )
        monkeypatch.setattr(
            runner, "_clear_stale_detections", lambda: None
        )
        monkeypatch.setattr(
            runner, "_wait_for_observation_window", lambda: None
        )

        runner._run()  # pylint: disable=protected-access

        assert runner.summary is not None
        assert runner.summary.status == "error"
        assert "does_not_exist" in (runner.summary.error or "")


# --- Slice D: inject_db_cases + TestRunnerComponent case unioning ----------


def _insert_test_case(
    session_factory: sessionmaker[Session],
    *,
    name: str,
    camera_identifier: str,
    kind: str = "object",
    polarity: str = "positive",
    slug: str | None = None,
    expected: dict[str, Any] | None = None,
    video_path: str = "/config/test_videos/cam/object/positive/clip.mp4",
    duration: int = 20,
) -> int:
    """Insert a TestCase row and return its id."""
    with session_factory() as session:
        stmt = (
            insert(TestCase)
            .values(
                name=name,
                slug=slug or name.replace(" ", "_"),
                camera_identifier=camera_identifier,
                kind=kind,
                polarity=polarity,
                expected=expected or {"labels": ["person"]},
                video_path=video_path,
                duration=duration,
            )
            .returning(TestCase.id)
        )
        case_id = session.execute(stmt).scalar_one()
        session.commit()
        return case_id


class TestInjectDbCases:
    """Unit tests for runner.inject_db_cases."""

    def test_injects_synthetic_camera_and_clones_detector_config(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_test_case(
            get_db_session, name="walk", camera_identifier="driveway"
        )
        config = {
            "ffmpeg": {
                "camera": {
                    "driveway": {
                        "host": "10.0.0.1",
                        "port": 554,
                        "path": "/live",
                        "motion_detector": {"fps": 2, "threshold": 10},
                        "object_detector": {"labels": [{"label": "person"}]},
                    }
                }
            },
            "test_runner": {"cases": []},
        }
        vis = _FakeViseron(get_db_session, cameras={})
        cases = inject_db_cases(vis, config)

        assert len(cases) == 1
        assert cases[0][CONFIG_CAMERA] == "test_driveway_walk"
        assert cases[0][CONFIG_VIDEO_PATH].endswith(".mp4")

        synth = config["ffmpeg"]["camera"]["test_driveway_walk"]
        assert synth["test_mode"] is True
        assert synth["file_source"].endswith(".mp4")
        # Cloned detector config, not aliased.
        assert synth["motion_detector"] == {"fps": 2, "threshold": 10}
        assert synth["object_detector"] == {"labels": [{"label": "person"}]}
        synth["motion_detector"]["fps"] = 99
        assert (
            config["ffmpeg"]["camera"]["driveway"]["motion_detector"]["fps"]
            == 2
        )  # deepcopy, not reference

    def test_skips_cases_whose_target_is_not_under_ffmpeg(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_test_case(
            get_db_session, name="gstream_case", camera_identifier="backyard"
        )
        config = {
            "ffmpeg": {"camera": {"driveway": {"host": "10.0.0.1"}}},
            "test_runner": {"cases": []},
        }
        vis = _FakeViseron(get_db_session, cameras={})
        cases = inject_db_cases(vis, config)

        assert cases == []
        # No synthetic camera was created for the unknown target.
        assert "test_backyard_gstream_case" not in config["ffmpeg"]["camera"]

    def test_idempotent_when_synthetic_camera_already_present(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_test_case(
            get_db_session, name="walk", camera_identifier="driveway"
        )
        existing = {
            "name": "Pre-existing test camera",
            "host": "localhost",
            "port": 554,
            "path": "/",
            "test_mode": True,
            "file_source": "/config/test_videos/manual.mp4",
        }
        config = {
            "ffmpeg": {
                "camera": {
                    "driveway": {"host": "10.0.0.1"},
                    "test_driveway_walk": existing,
                }
            },
            "test_runner": {"cases": []},
        }
        vis = _FakeViseron(get_db_session, cameras={})
        cases = inject_db_cases(vis, config)
        # Case is still returned so the runner can pick it up.
        assert len(cases) == 1
        # But the pre-existing synthetic entry is not clobbered.
        assert (
            config["ffmpeg"]["camera"]["test_driveway_walk"] is existing
        )

    def test_no_ffmpeg_config_means_no_cases(
        self, get_db_session: sessionmaker[Session]
    ) -> None:
        _insert_test_case(
            get_db_session, name="walk", camera_identifier="driveway"
        )
        config = {"test_runner": {"cases": []}}
        vis = _FakeViseron(get_db_session, cameras={})
        cases = inject_db_cases(vis, config)
        assert cases == []


class TestRunnerComponentCaseUnioning:
    """TestRunnerComponent unions config cases + runnable DB cases."""

    def test_trigger_run_includes_both_config_and_db_cases(
        self,
        monkeypatch: pytest.MonkeyPatch,
        get_db_session: sessionmaker[Session],
    ) -> None:
        config_case = {
            CONFIG_NAME: "yaml case",
            CONFIG_CAMERA: "yaml_test_cam",
            CONFIG_KIND: KIND_MOTION,
            CONFIG_DURATION: 1,
            CONFIG_EXPECTED: {EXPECTED_DETECTED: True},
        }
        db_case = {
            CONFIG_NAME: "db case",
            CONFIG_CAMERA: "test_driveway_db",
            CONFIG_KIND: KIND_OBJECT,
            CONFIG_DURATION: 1,
            CONFIG_EXPECTED: {EXPECTED_LABELS: ["person"]},
            CONFIG_VIDEO_PATH: "/clip.mp4",
        }
        cameras = {
            "yaml_test_cam": _test_camera("yaml_test_cam"),
            "test_driveway_db": _test_camera("test_driveway_db"),
        }
        vis = _FakeViseron(get_db_session, cameras=cameras)
        component = TestRunnerComponent(
            vis,
            {
                CONFIG_CASES: [config_case],
                CONFIG_SHUTDOWN_ON_COMPLETE: False,
                CONFIG_CAMERA_READY_TIMEOUT: 1,
            },
            db_cases=[db_case],
        )
        merged = component._collect_runnable_cases()  # pylint: disable=protected-access
        assert [case[CONFIG_NAME] for case in merged] == [
            "yaml case",
            "db case",
        ]

    def test_db_case_skipped_when_synth_camera_not_registered(
        self,
        get_db_session: sessionmaker[Session],
    ) -> None:
        db_case = {
            CONFIG_NAME: "db case",
            CONFIG_CAMERA: "test_driveway_db",  # not in cameras below
            CONFIG_KIND: KIND_OBJECT,
            CONFIG_DURATION: 1,
            CONFIG_EXPECTED: {EXPECTED_LABELS: ["person"]},
            CONFIG_VIDEO_PATH: "/clip.mp4",
        }
        yaml_case = {
            CONFIG_NAME: "yaml case",
            CONFIG_CAMERA: "yaml_test_cam",
            CONFIG_KIND: KIND_MOTION,
            CONFIG_DURATION: 1,
            CONFIG_EXPECTED: {EXPECTED_DETECTED: True},
        }
        cameras = {"yaml_test_cam": _test_camera("yaml_test_cam")}
        vis = _FakeViseron(get_db_session, cameras=cameras)
        component = TestRunnerComponent(
            vis,
            {
                CONFIG_CASES: [yaml_case],
                CONFIG_SHUTDOWN_ON_COMPLETE: False,
                CONFIG_CAMERA_READY_TIMEOUT: 1,
            },
            db_cases=[db_case],
        )
        merged = component._collect_runnable_cases()  # pylint: disable=protected-access
        assert [case[CONFIG_NAME] for case in merged] == ["yaml case"]

    def test_trigger_run_errors_when_no_runnable_cases(
        self,
        get_db_session: sessionmaker[Session],
    ) -> None:
        vis = _FakeViseron(get_db_session, cameras={})
        component = TestRunnerComponent(
            vis,
            {
                CONFIG_CASES: [],
                CONFIG_SHUTDOWN_ON_COMPLETE: False,
                CONFIG_CAMERA_READY_TIMEOUT: 1,
            },
            db_cases=[],
        )
        with pytest.raises(RuntimeError, match="no runnable test cases"):
            component.trigger_run()
