"""Materialize a slice of recorded footage into a standalone MP4.

At PRE_PARALLEL setup time the test_runner may need to turn a datetime
range on a real camera into an on-disk video file that an injected ffmpeg
test camera can replay. The clip catalog REST endpoint does something
similar at request time by going through the owning camera's
``Fragmenter``, but at setup time no ``Camera`` object exists yet — the
ffmpeg component hasn't been set up. This module does the concatenation
standalone, driven only by the ``Files`` table and a subprocess ffmpeg
call.

Results are cached under ``$CONFIG_DIR/test_videos/_timeline/<cam>/<slug>.mp4``
so subsequent startups reuse the materialized file instead of redoing
the concatenation. A small sidecar ``.meta`` file records the
``from``/``to`` pair used to produce the clip; if a later run references
the same range the cached file is returned verbatim.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from viseron.components.storage.queries import get_time_period_fragments
from viseron.const import CONFIG_DIR
from viseron.domains.camera.fragmenter import Fragment, generate_playlist
from viseron.helpers import create_directory

from .const import TIMELINE_CACHE_SUBDIR

if TYPE_CHECKING:
    from viseron.components.storage import Storage

LOGGER = logging.getLogger(__name__)


class TimelineMaterializationError(RuntimeError):
    """Raised when a timeline slice cannot be turned into a clip file."""


def timeline_cache_root() -> str:
    """Return the directory where materialized timeline clips live."""
    return os.path.join(CONFIG_DIR, "test_videos", TIMELINE_CACHE_SUBDIR)


def cached_clip_path(camera_identifier: str, slug: str) -> str:
    """Return the expected on-disk path for a materialized clip."""
    return os.path.join(
        timeline_cache_root(), camera_identifier, f"{slug}.mp4"
    )


def _meta_path(clip_path: str) -> str:
    return clip_path + ".meta.json"


def _cache_hit(
    clip_path: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> bool:
    """Return True if clip_path is a usable cache of the given range."""
    if not os.path.exists(clip_path):
        return False
    meta_file = _meta_path(clip_path)
    if not os.path.exists(meta_file):
        return False
    try:
        with open(meta_file, encoding="utf-8") as fp:
            meta = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        meta.get("from") == start.isoformat()
        and meta.get("to") == end.isoformat()
    )


def _write_meta(
    clip_path: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> None:
    meta = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(_meta_path(clip_path), "w", encoding="utf-8") as fp:
        json.dump(meta, fp, indent=2)


def _fragments_for_range(
    storage: "Storage",
    camera_identifier: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[Fragment]:
    """Query the DB for fragments covering [start, end] on ``camera_identifier``."""
    files = get_time_period_fragments(
        [camera_identifier],
        start.timestamp(),
        end.timestamp(),
        storage.get_session,
    )
    return [
        Fragment(file.filename, file.path, file.duration, file.orig_ctime)
        for file in files
    ]


def _find_init_mp4(fragments: list[Fragment]) -> str | None:
    """Locate the init.mp4 for a set of fragments.

    ``Fragment.path`` is an absolute path to an ``.m4s`` file under the
    camera's segments folder; ``init.mp4`` sits next to them. We walk a
    few fragments in case the first one lives in a subdirectory and only
    return a path that actually exists on disk.
    """
    seen_dirs: set[str] = set()
    for fragment in fragments:
        directory = os.path.dirname(fragment.path)
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        candidate = os.path.join(directory, "init.mp4")
        if os.path.exists(candidate):
            return candidate
    return None


def _run_ffmpeg_concat(
    playlist: str,
    destination: str,
    *,
    loglevel: str = "error",
) -> None:
    """Feed a HLS playlist to ffmpeg to concatenate into a standalone mp4."""
    create_directory(os.path.dirname(destination))
    # ffmpeg is invoked directly, matching Fragmenter.concatenate_fragments
    # except we don't have a LogPipe at PRE_PARALLEL time, so stderr is
    # captured and surfaced through an exception on failure.
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        loglevel,
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        "-",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        destination,
    ]
    LOGGER.debug("timeline materialize ffmpeg cmd: %s", " ".join(ffmpeg_cmd))
    try:
        result = subprocess.run(
            ffmpeg_cmd,
            input=playlist.encode("utf-8"),
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as err:
        raise TimelineMaterializationError(
            "ffmpeg binary is not available on PATH"
        ) from err
    if result.returncode != 0:
        raise TimelineMaterializationError(
            "ffmpeg failed to concatenate timeline fragments: "
            + result.stderr.decode("utf-8", errors="replace").strip()
        )


def materialize_timeline_clip(
    storage: "Storage",
    camera_identifier: str,
    start: datetime.datetime,
    end: datetime.datetime,
    slug: str,
) -> str:
    """Produce an on-disk clip for a timeline range and return its path.

    Uses a local cache keyed by (camera, slug); if the cache already
    contains a clip whose sidecar meta records the same ``from``/``to``
    values, returns that path without re-encoding. The sidecar is
    rewritten on every fresh materialization so stale caches (different
    range, same slug) are corrected automatically.
    """
    clip_path = cached_clip_path(camera_identifier, slug)
    if _cache_hit(clip_path, start, end):
        LOGGER.debug(
            "timeline cache hit for %s (%s) at %s",
            camera_identifier,
            slug,
            clip_path,
        )
        return clip_path

    fragments = _fragments_for_range(storage, camera_identifier, start, end)
    if not fragments:
        raise TimelineMaterializationError(
            f"no recorded fragments found for camera {camera_identifier!r} "
            f"between {start.isoformat()} and {end.isoformat()}"
        )

    init_file = _find_init_mp4(fragments)
    if init_file is None:
        raise TimelineMaterializationError(
            f"unable to locate init.mp4 alongside fragments for camera "
            f"{camera_identifier!r}"
        )

    playlist = generate_playlist(
        fragments,
        init_file,
        media_sequence=0,
        end=True,
        file_directive=True,
    )

    # Write through a temporary file so a crashed ffmpeg doesn't leave a
    # half-written cache entry behind.
    create_directory(os.path.dirname(clip_path))
    with tempfile.NamedTemporaryFile(
        prefix=f"{slug}_", suffix=".mp4", dir=os.path.dirname(clip_path), delete=False
    ) as tmp:
        tmp_path = tmp.name
    try:
        _run_ffmpeg_concat(playlist, tmp_path)
        shutil.move(tmp_path, clip_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    _write_meta(clip_path, start, end)
    LOGGER.info(
        "materialized timeline clip for %s: %s (%s → %s)",
        camera_identifier,
        clip_path,
        start.isoformat(),
        end.isoformat(),
    )
    return clip_path


__all__ = [
    "TimelineMaterializationError",
    "cached_clip_path",
    "materialize_timeline_clip",
    "timeline_cache_root",
]


# Expose a hook for tests to replace: keeping it as a module-level name
# avoids patching `subprocess.run` globally.
run_ffmpeg_concat: Any = _run_ffmpeg_concat
