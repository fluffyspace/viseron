"""Tests for the TestsAPIHandler REST endpoints."""
from __future__ import annotations

import datetime
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm.session import Session, sessionmaker

from viseron.components.storage.models import TestCase, TestResult, TestRun
from viseron.components.test_runner import COMPONENT as TEST_RUNNER_COMPONENT

from tests.common import MockCamera
from tests.components.webserver.common import TestAppBaseNoAuth


def _insert_run(
    session: Session,
    *,
    total: int,
    passed: int,
    failed: int,
    status: str = "complete",
    started_at: datetime.datetime | None = None,
    finished_at: datetime.datetime | None = None,
) -> int:
    if started_at is None:
        started_at = datetime.datetime(
            2026, 4, 5, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
    stmt = (
        insert(TestRun)
        .values(
            total=total,
            passed=passed,
            failed=failed,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
        )
        .returning(TestRun.id)
    )
    return session.execute(stmt).scalar_one()


def _insert_result(
    session: Session,
    run_id: int,
    *,
    case_name: str,
    passed: bool,
    kind: str = "motion",
    camera_identifier: str = "test_cam",
    expected: dict | None = None,
    actual: dict | None = None,
    snapshot_path: str | None = None,
    message: str | None = None,
) -> None:
    session.execute(
        insert(TestResult).values(
            run_id=run_id,
            case_name=case_name,
            camera_identifier=camera_identifier,
            kind=kind,
            expected=expected or {"detected": True},
            actual=actual or {"detected": passed, "count": 1 if passed else 0},
            passed=passed,
            snapshot_path=snapshot_path,
            message=message,
        )
    )


class TestListAndDetail(TestAppBaseNoAuth):
    """Tests for GET /api/v1/tests/runs and sibling endpoints."""

    @pytest.fixture(scope="function", autouse=True)
    def prepare_and_mock(self, get_db_session: sessionmaker[Session]):
        """Seed the DB with two runs and patch the handler session."""
        with get_db_session() as session:
            run_a = _insert_run(
                session, total=2, passed=2, failed=0, status="complete"
            )
            _insert_result(
                session, run_a, case_name="motion a", passed=True
            )
            _insert_result(
                session,
                run_a,
                case_name="object a",
                passed=True,
                kind="object",
                snapshot_path="/snapshots/a.jpg",
            )

            run_b = _insert_run(
                session,
                total=2,
                passed=1,
                failed=1,
                status="complete",
                started_at=datetime.datetime(
                    2026, 4, 5, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            )
            _insert_result(
                session, run_b, case_name="motion b", passed=True
            )
            _insert_result(
                session,
                run_b,
                case_name="object b",
                passed=False,
                kind="object",
                message="expected person but observed nothing",
            )
            session.commit()
            self._run_a = run_a  # pylint: disable=attribute-defined-outside-init
            self._run_b = run_b  # pylint: disable=attribute-defined-outside-init

        with patch(
            (
                "viseron.components.webserver.request_handler.ViseronRequestHandler."
                "_get_session"
            ),
            return_value=get_db_session(),
        ):
            yield

    def test_list_runs_returns_newest_first(self) -> None:
        response = self.fetch("/api/v1/tests/runs")
        assert response.code == 200
        body = json.loads(response.body)
        runs = body["runs"]
        assert len(runs) == 2
        assert runs[0]["id"] == self._run_b
        assert runs[0]["failed"] == 1
        assert runs[1]["id"] == self._run_a

    def test_list_runs_honours_limit(self) -> None:
        response = self.fetch("/api/v1/tests/runs?limit=1")
        assert response.code == 200
        runs = json.loads(response.body)["runs"]
        assert len(runs) == 1
        assert runs[0]["id"] == self._run_b

    def test_get_run_returns_results(self) -> None:
        response = self.fetch(f"/api/v1/tests/runs/{self._run_a}")
        assert response.code == 200
        run = json.loads(response.body)["run"]
        assert run["id"] == self._run_a
        assert run["passed"] == 2
        names = {result["case_name"] for result in run["results"]}
        assert names == {"motion a", "object a"}

    def test_get_missing_run_404s(self) -> None:
        response = self.fetch("/api/v1/tests/runs/99999")
        assert response.code == 404

    def test_latest_run(self) -> None:
        response = self.fetch("/api/v1/tests/runs/latest")
        assert response.code == 200
        run = json.loads(response.body)["run"]
        assert run["id"] == self._run_b


class TestTriggerRun(TestAppBaseNoAuth):
    """POST /api/v1/tests/runs kicks off the runner if configured."""

    def test_no_component_returns_404(self) -> None:
        response = self.fetch(
            "/api/v1/tests/runs", method="POST", body="", allow_nonstandard_methods=True
        )
        assert response.code == 404

    def test_trigger_invokes_component(self) -> None:
        fake_runner = SimpleNamespace(
            completion_event=MagicMock(is_set=MagicMock(return_value=False))
        )
        fake_component = MagicMock()
        fake_component.trigger_run.return_value = fake_runner
        fake_component.is_running = True
        self.vis.data[TEST_RUNNER_COMPONENT] = fake_component

        response = self.fetch(
            "/api/v1/tests/runs", method="POST", body="", allow_nonstandard_methods=True
        )
        assert response.code == 202
        body = json.loads(response.body)
        assert body["started"] is True
        assert body["is_running"] is True
        fake_component.trigger_run.assert_called_once()

    def test_trigger_conflict_when_already_running(self) -> None:
        fake_component = MagicMock()
        fake_component.trigger_run.side_effect = RuntimeError(
            "a test run is already in progress"
        )
        self.vis.data[TEST_RUNNER_COMPONENT] = fake_component

        response = self.fetch(
            "/api/v1/tests/runs", method="POST", body="", allow_nonstandard_methods=True
        )
        assert response.code == 409


class TestCreateClip(TestAppBaseNoAuth):
    """POST /api/v1/tests/clips extracts a timespan into the test videos dir."""

    @pytest.fixture(autouse=True)
    def _fake_camera(self, tmp_path, get_db_session: sessionmaker[Session]):
        """Provide a camera with a mocked fragmenter + pretend source video."""
        fake_mp4 = tmp_path / "raw_concat.mp4"
        fake_mp4.write_bytes(b"fake mp4 bytes")

        mocked_camera = MockCamera(identifier="driveway")
        # Make concatenate_fragments produce a fresh file every call so the
        # happy path can move it without clashing on repeat invocations.
        call_counter = {"n": 0}

        def _fake_concat(_fragments):
            call_counter["n"] += 1
            path = tmp_path / f"raw_concat_{call_counter['n']}.mp4"
            path.write_bytes(b"fake mp4 bytes")
            return str(path)

        mocked_camera.fragmenter.concatenate_fragments.side_effect = _fake_concat

        test_videos_root = tmp_path / "config" / "test_videos"

        fragment_files = [
            SimpleNamespace(
                filename="segment.m4s",
                path="/segments/segment.m4s",
                duration=5.0,
                orig_ctime=datetime.datetime(
                    2026, 4, 5, 10, 0, 0, tzinfo=datetime.timezone.utc
                ),
            )
        ]

        with patch(
            (
                "viseron.components.webserver.request_handler.ViseronRequestHandler."
                "_get_camera"
            ),
            return_value=mocked_camera,
        ), patch(
            (
                "viseron.components.webserver.request_handler.ViseronRequestHandler."
                "_get_session"
            ),
            return_value=get_db_session(),
        ), patch(
            "viseron.components.webserver.api.v1.tests.get_time_period_fragments",
            return_value=fragment_files,
        ), patch(
            "viseron.components.webserver.api.v1.tests.CONFIG_DIR",
            str(tmp_path / "config"),
        ):
            yield mocked_camera, test_videos_root, get_db_session

    def _post_clip(self, **overrides) -> tuple[int, dict]:
        payload = {
            "camera_identifier": "driveway",
            "start": 1_700_000_000,
            "end": 1_700_000_015,
            "name": "person walks by",
            "kind": "object",
            "polarity": "positive",
            "expected": {"labels": ["person"]},
        }
        payload.update(overrides)
        response = self.fetch(
            "/api/v1/tests/clips",
            method="POST",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        body = json.loads(response.body) if response.body else {}
        return response.code, body

    def test_happy_path_moves_clip_and_returns_snippet(self, _fake_camera) -> None:
        camera, test_videos_root, get_db_session = _fake_camera
        code, body = self._post_clip()
        assert code == 201
        expected_path = os.path.join(
            str(test_videos_root),
            "driveway",
            "object",
            "positive",
            "person_walks_by.mp4",
        )
        assert body["clip_path"] == expected_path
        assert os.path.exists(expected_path)
        assert body["slug"] == "person_walks_by"
        assert body["case_id"] > 0
        # tests.yaml-style snippet: grouped by camera/kind/polarity with
        # the clip path appearing under the matching label bucket.
        assert "cameras:" in body["snippet"]
        assert "  driveway:" in body["snippet"]
        assert "    object:" in body["snippet"]
        assert "      positive:" in body["snippet"]
        assert "person:" in body["snippet"]
        assert expected_path in body["snippet"]
        camera.fragmenter.concatenate_fragments.assert_called_once()

        # The happy path also persists a catalog row.
        with get_db_session() as session:
            cases = session.execute(select(TestCase)).scalars().all()
            assert len(cases) == 1
            case = cases[0]
            assert case.id == body["case_id"]
            assert case.camera_identifier == "driveway"
            assert case.kind == "object"
            assert case.polarity == "positive"
            assert case.slug == "person_walks_by"
            assert case.video_path == expected_path

    def test_repeated_post_is_idempotent(self, _fake_camera) -> None:
        _camera, _root, get_db_session = _fake_camera
        code_a, body_a = self._post_clip()
        assert code_a == 201
        code_b, body_b = self._post_clip(name="Person Walks By")
        # Same slug, same kind, same polarity → upsert onto the same row.
        assert code_b == 201
        assert body_a["case_id"] == body_b["case_id"]
        with get_db_session() as session:
            count = session.execute(select(TestCase)).scalars().all()
            assert len(count) == 1
            # Updated name takes effect.
            assert count[0].name == "Person Walks By"

    def test_invalid_range_returns_400(self, _fake_camera) -> None:
        code, body = self._post_clip(end=1_700_000_000)
        assert code == 400
        assert "end" in body.get("error", "").lower()

    def test_missing_fragments_returns_404(self, _fake_camera) -> None:
        with patch(
            "viseron.components.webserver.api.v1.tests.get_time_period_fragments",
            return_value=[],
        ):
            code, _body = self._post_clip()
        assert code == 404

    def test_missing_camera_returns_404(self, _fake_camera) -> None:
        with patch(
            (
                "viseron.components.webserver.request_handler.ViseronRequestHandler."
                "_get_camera"
            ),
            return_value=None,
        ):
            code, _body = self._post_clip()
        assert code == 404


class TestCasesCatalog(TestAppBaseNoAuth):
    """GET/DELETE /api/v1/tests/cases and the clip serving endpoints."""

    @pytest.fixture(autouse=True)
    def _seed_catalog(self, tmp_path, get_db_session: sessionmaker[Session]):
        """Seed a catalog entry with a matching on-disk clip file."""
        test_videos_root = tmp_path / "config" / "test_videos"
        case_clip_dir = (
            test_videos_root / "driveway" / "object" / "positive"
        )
        case_clip_dir.mkdir(parents=True)
        case_clip_path = case_clip_dir / "person_walks_by.mp4"
        case_clip_path.write_bytes(b"case mp4 bytes")

        rogue_clip = tmp_path / "rogue.mp4"
        rogue_clip.write_bytes(b"rogue file not in test videos")

        with get_db_session() as session:
            stmt = (
                insert(TestCase)
                .values(
                    name="person walks by",
                    slug="person_walks_by",
                    camera_identifier="driveway",
                    kind="object",
                    polarity="positive",
                    expected={"labels": ["person"]},
                    video_path=str(case_clip_path),
                    duration=20,
                )
                .returning(TestCase.id)
            )
            case_id = session.execute(stmt).scalar_one()
            rogue_stmt = (
                insert(TestCase)
                .values(
                    name="rogue",
                    slug="rogue",
                    camera_identifier="driveway",
                    kind="motion",
                    polarity="positive",
                    expected={"detected": True},
                    video_path=str(rogue_clip),
                    duration=10,
                )
                .returning(TestCase.id)
            )
            rogue_id = session.execute(rogue_stmt).scalar_one()
            session.commit()

        with patch(
            (
                "viseron.components.webserver.request_handler.ViseronRequestHandler."
                "_get_session"
            ),
            return_value=get_db_session(),
        ), patch(
            "viseron.components.webserver.api.v1.tests.CONFIG_DIR",
            str(tmp_path / "config"),
        ):
            yield {
                "case_id": case_id,
                "rogue_id": rogue_id,
                "case_clip_path": str(case_clip_path),
                "rogue_clip_path": str(rogue_clip),
                "test_videos_root": test_videos_root,
                "get_db_session": get_db_session,
            }

    def test_list_cases_includes_snippet(self, _seed_catalog) -> None:
        response = self.fetch("/api/v1/tests/cases")
        assert response.code == 200
        cases = json.loads(response.body)["cases"]
        assert len(cases) == 2
        first = next(c for c in cases if c["id"] == _seed_catalog["case_id"])
        assert first["kind"] == "object"
        assert first["polarity"] == "positive"
        # tests.yaml-style snippet rather than raw ffmpeg config.
        assert "cameras:" in first["snippet"]

    def test_list_cases_filters_by_camera(self, _seed_catalog) -> None:
        response = self.fetch(
            "/api/v1/tests/cases?camera_identifier=other_camera"
        )
        assert response.code == 200
        assert json.loads(response.body)["cases"] == []

    def test_delete_case_removes_row_and_file(self, _seed_catalog) -> None:
        case_id = _seed_catalog["case_id"]
        clip_path = _seed_catalog["case_clip_path"]
        assert os.path.exists(clip_path)
        response = self.fetch(
            f"/api/v1/tests/cases/{case_id}", method="DELETE"
        )
        assert response.code == 200
        assert json.loads(response.body)["deleted"] == case_id
        assert not os.path.exists(clip_path)
        with _seed_catalog["get_db_session"]() as session:
            remaining = session.execute(
                select(TestCase).where(TestCase.id == case_id)
            ).scalar_one_or_none()
            assert remaining is None

    def test_delete_case_preserves_rogue_file_outside_test_videos(
        self, _seed_catalog
    ) -> None:
        rogue_id = _seed_catalog["rogue_id"]
        rogue_path = _seed_catalog["rogue_clip_path"]
        response = self.fetch(
            f"/api/v1/tests/cases/{rogue_id}", method="DELETE"
        )
        # Row goes away but the file stays because it lives outside the
        # test videos sandbox. Defence in depth.
        assert response.code == 200
        assert os.path.exists(rogue_path)

    def test_delete_missing_case_returns_404(self, _seed_catalog) -> None:
        response = self.fetch(
            "/api/v1/tests/cases/99999", method="DELETE"
        )
        assert response.code == 404

    def test_get_case_clip_happy_path(self, _seed_catalog) -> None:
        response = self.fetch(
            f"/api/v1/tests/cases/{_seed_catalog['case_id']}/clip"
        )
        assert response.code == 200
        assert response.headers["Content-Type"] == "video/mp4"
        assert response.body == b"case mp4 bytes"

    def test_get_case_clip_rejects_path_outside_test_videos(
        self, _seed_catalog
    ) -> None:
        response = self.fetch(
            f"/api/v1/tests/cases/{_seed_catalog['rogue_id']}/clip"
        )
        assert response.code == 403

    def test_get_case_clip_404_for_missing_case(self, _seed_catalog) -> None:
        response = self.fetch("/api/v1/tests/cases/99999/clip")
        assert response.code == 404


class TestResultClipEndpoint(TestAppBaseNoAuth):
    """GET /api/v1/tests/results/:id/clip streams the clip a result came from."""

    @pytest.fixture(autouse=True)
    def _seed_result(self, tmp_path, get_db_session: sessionmaker[Session]):
        test_videos_root = tmp_path / "config" / "test_videos"
        clip_dir = test_videos_root / "cam" / "motion" / "positive"
        clip_dir.mkdir(parents=True)
        clip_path = clip_dir / "walking.mp4"
        clip_path.write_bytes(b"result mp4 bytes")

        with get_db_session() as session:
            run_id = session.execute(
                insert(TestRun)
                .values(total=1, passed=1, failed=0, status="complete")
                .returning(TestRun.id)
            ).scalar_one()
            result_id = session.execute(
                insert(TestResult)
                .values(
                    run_id=run_id,
                    case_name="walking",
                    camera_identifier="cam",
                    kind="motion",
                    expected={"detected": True},
                    actual={"detected": True, "count": 1},
                    passed=True,
                    video_path=str(clip_path),
                )
                .returning(TestResult.id)
            ).scalar_one()
            session.commit()

        with patch(
            (
                "viseron.components.webserver.request_handler.ViseronRequestHandler."
                "_get_session"
            ),
            return_value=get_db_session(),
        ), patch(
            "viseron.components.webserver.api.v1.tests.CONFIG_DIR",
            str(tmp_path / "config"),
        ):
            yield {"result_id": result_id, "clip_path": str(clip_path)}

    def test_happy_path(self, _seed_result) -> None:
        response = self.fetch(
            f"/api/v1/tests/results/{_seed_result['result_id']}/clip"
        )
        assert response.code == 200
        assert response.body == b"result mp4 bytes"

    def test_missing_result_404s(self, _seed_result) -> None:
        response = self.fetch("/api/v1/tests/results/99999/clip")
        assert response.code == 404


class TestPendingRestartFlag(TestAppBaseNoAuth):
    """GET /tests/cases decorates entries with pending_restart."""

    @pytest.fixture(autouse=True)
    def _seed(self, tmp_path, get_db_session: sessionmaker[Session]):
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"mp4")
        with get_db_session() as session:
            registered_id = session.execute(
                insert(TestCase)
                .values(
                    name="registered",
                    slug="registered",
                    camera_identifier="cam_a",
                    kind="object",
                    polarity="positive",
                    expected={"labels": ["person"]},
                    video_path=str(clip_path),
                    duration=10,
                )
                .returning(TestCase.id)
            ).scalar_one()
            pending_id = session.execute(
                insert(TestCase)
                .values(
                    name="pending",
                    slug="pending",
                    camera_identifier="cam_b",
                    kind="motion",
                    polarity="negative",
                    expected={"detected": False},
                    video_path=str(clip_path),
                    duration=10,
                )
                .returning(TestCase.id)
            ).scalar_one()
            session.commit()

        # Register only the cam_a synthetic camera.
        synth_camera = MagicMock(
            is_test_camera=True, identifier="test_cam_a_registered"
        )
        from viseron.exceptions import DomainNotRegisteredError

        def stub_get_registered_domain(domain, identifier):
            if identifier == "test_cam_a_registered":
                return synth_camera
            raise DomainNotRegisteredError(domain, identifier=identifier)

        with patch.object(
            self.vis,
            "get_registered_domain",
            side_effect=stub_get_registered_domain,
        ), patch(
            (
                "viseron.components.webserver.request_handler.ViseronRequestHandler."
                "_get_session"
            ),
            return_value=get_db_session(),
        ), patch(
            "viseron.components.webserver.api.v1.tests.CONFIG_DIR",
            str(tmp_path),
        ):
            yield {"registered": registered_id, "pending": pending_id}

    def test_cases_marked_with_pending_restart(self, _seed) -> None:
        response = self.fetch("/api/v1/tests/cases")
        assert response.code == 200
        cases = json.loads(response.body)["cases"]
        by_id = {case["id"]: case for case in cases}
        assert by_id[_seed["registered"]]["pending_restart"] is False
        assert by_id[_seed["pending"]]["pending_restart"] is True


class TestRestartEndpoint(TestAppBaseNoAuth):
    """POST /tests/restart triggers the supervisor-driven restart."""

    def test_restart_sets_exit_code_and_sends_sigint(self) -> None:
        with patch(
            "viseron.components.webserver.api.v1.tests.os.kill"
        ) as mocked_kill:
            response = self.fetch(
                "/api/v1/tests/restart",
                method="POST",
                body="",
                allow_nonstandard_methods=True,
            )
        assert response.code == 202
        body = json.loads(response.body)
        assert body["restarting"] is True
        from viseron.const import RESTART_EXIT_CODE

        assert self.vis.exit_code == RESTART_EXIT_CODE
        mocked_kill.assert_called_once()
