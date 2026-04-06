"""Unit tests for the timeline clip materialization helper.

The helper sits in the PRE_PARALLEL setup path so it can't rely on a
real Camera object — it talks straight to the ``Files`` table and
shells out to ``ffmpeg``. These tests stub both.
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Any

import pytest

from viseron.components.test_runner import timeline as timeline_module
from viseron.components.test_runner.timeline import (
    TimelineMaterializationError,
    cached_clip_path,
    materialize_timeline_clip,
)
from viseron.domains.camera.fragmenter import Fragment


class _FakeStorage:
    """Has a ``get_session`` attribute that the queries layer imports."""

    def __init__(self) -> None:
        self.get_session = lambda: None  # unused: we patch the query fn


def _fragment(path: str, seconds: float, when: datetime.datetime) -> Fragment:
    return Fragment(
        filename=os.path.basename(path),
        path=path,
        duration=seconds,
        creation_time=when,
    )


@pytest.fixture()
def patched_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> dict[str, Any]:
    """Redirect the helper's filesystem + ffmpeg dependencies to tmp_path.

    - ``CONFIG_DIR`` is rewritten to ``tmp_path`` so the cache directory
      becomes ``tmp_path/test_videos/_timeline``.
    - ``get_time_period_fragments`` is replaced with a stub that returns
      a caller-provided list.
    - ``_run_ffmpeg_concat`` is replaced with a stub that writes a
      marker file to ``destination`` so the test can assert a "clip"
      was produced without running real ffmpeg.
    """
    monkeypatch.setattr(
        "viseron.components.test_runner.timeline.CONFIG_DIR",
        str(tmp_path),
    )
    state: dict[str, Any] = {
        "fragments": [],
        "ffmpeg_calls": [],
        "init_mp4_exists": True,
    }

    def fake_get_fragments(
        camera_ids: list[str],
        start_ts: float,
        end_ts: float,
        _get_session: Any,
    ) -> list[Any]:
        state["query_args"] = (tuple(camera_ids), start_ts, end_ts)
        return state["fragments"]

    monkeypatch.setattr(
        "viseron.components.test_runner.timeline.get_time_period_fragments",
        fake_get_fragments,
    )

    def fake_ffmpeg(playlist: str, destination: str, **_kwargs: Any) -> None:
        state["ffmpeg_calls"].append((playlist, destination))
        with open(destination, "wb") as fp:
            fp.write(b"FAKE_MP4")

    monkeypatch.setattr(
        "viseron.components.test_runner.timeline._run_ffmpeg_concat",
        fake_ffmpeg,
    )
    return state


def test_errors_when_no_fragments(patched_helpers: dict[str, Any]) -> None:
    start = datetime.datetime(2026, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 1, 15, 14, 31, tzinfo=datetime.timezone.utc)
    with pytest.raises(TimelineMaterializationError, match="no recorded fragments"):
        materialize_timeline_clip(
            _FakeStorage(), "garage", start, end, "slug"
        )


def test_errors_when_init_mp4_missing(
    patched_helpers: dict[str, Any], tmp_path: Any
) -> None:
    # A fragment whose directory does not contain init.mp4.
    frag_dir = tmp_path / "segments" / "garage"
    frag_dir.mkdir(parents=True)
    frag_path = str(frag_dir / "00001.m4s")
    with open(frag_path, "wb") as fp:
        fp.write(b"")
    patched_helpers["fragments"] = [
        _fragment(
            frag_path,
            5.0,
            datetime.datetime(2026, 1, 15, 14, 30, tzinfo=datetime.timezone.utc),
        )
    ]
    start = datetime.datetime(2026, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 1, 15, 14, 31, tzinfo=datetime.timezone.utc)

    with pytest.raises(TimelineMaterializationError, match="init.mp4"):
        materialize_timeline_clip(
            _FakeStorage(), "garage", start, end, "slug"
        )


def test_materializes_and_caches(
    patched_helpers: dict[str, Any], tmp_path: Any
) -> None:
    frag_dir = tmp_path / "segments" / "garage"
    frag_dir.mkdir(parents=True)
    # Required init.mp4 next to the fragment.
    init_path = frag_dir / "init.mp4"
    init_path.write_bytes(b"INIT")
    frag_path = str(frag_dir / "00001.m4s")
    with open(frag_path, "wb") as fp:
        fp.write(b"FRAG")
    patched_helpers["fragments"] = [
        _fragment(
            frag_path,
            5.0,
            datetime.datetime(2026, 1, 15, 14, 30, tzinfo=datetime.timezone.utc),
        )
    ]
    start = datetime.datetime(2026, 1, 15, 14, 30, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 1, 15, 14, 31, tzinfo=datetime.timezone.utc)

    path = materialize_timeline_clip(
        _FakeStorage(), "garage", start, end, "walk"
    )

    expected_path = cached_clip_path("garage", "walk")
    assert path == expected_path
    assert os.path.exists(expected_path)
    # Sidecar meta records the range used.
    meta = json.loads(open(expected_path + ".meta.json", encoding="utf-8").read())
    assert meta["from"] == start.isoformat()
    assert meta["to"] == end.isoformat()
    assert len(patched_helpers["ffmpeg_calls"]) == 1

    # Re-running with the same range is a cache hit — ffmpeg is NOT
    # invoked a second time.
    path_again = materialize_timeline_clip(
        _FakeStorage(), "garage", start, end, "walk"
    )
    assert path_again == expected_path
    assert len(patched_helpers["ffmpeg_calls"]) == 1


def test_cache_is_refreshed_when_range_changes(
    patched_helpers: dict[str, Any], tmp_path: Any
) -> None:
    frag_dir = tmp_path / "segments" / "garage"
    frag_dir.mkdir(parents=True)
    (frag_dir / "init.mp4").write_bytes(b"INIT")
    frag_path = str(frag_dir / "00001.m4s")
    with open(frag_path, "wb") as fp:
        fp.write(b"FRAG")
    patched_helpers["fragments"] = [
        _fragment(
            frag_path,
            5.0,
            datetime.datetime(2026, 1, 15, 14, 30, tzinfo=datetime.timezone.utc),
        )
    ]

    start_a = datetime.datetime(
        2026, 1, 15, 14, 30, tzinfo=datetime.timezone.utc
    )
    end_a = datetime.datetime(
        2026, 1, 15, 14, 31, tzinfo=datetime.timezone.utc
    )
    materialize_timeline_clip(_FakeStorage(), "garage", start_a, end_a, "walk")
    assert len(patched_helpers["ffmpeg_calls"]) == 1

    # Same slug, different range → cache miss, ffmpeg re-invoked.
    start_b = datetime.datetime(
        2026, 1, 15, 15, 0, tzinfo=datetime.timezone.utc
    )
    end_b = datetime.datetime(
        2026, 1, 15, 15, 1, tzinfo=datetime.timezone.utc
    )
    materialize_timeline_clip(_FakeStorage(), "garage", start_b, end_b, "walk")
    assert len(patched_helpers["ffmpeg_calls"]) == 2
