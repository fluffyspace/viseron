"""Playback API Handler.

Drives interactive replay through ``playback_mode`` cameras: pick a saved
recording, ffmpeg streams its mp4 (or concatenated segments) at native FPS
into the camera pipeline so motion / object detection / NVR / MQTT all run
identically to a live feed. Used for end-to-end debugging without having
to physically generate live footage.

Endpoints:
- POST   /api/v1/playback/favorites               body {"recording_id": int}
- GET    /api/v1/playback/favorites
- DELETE /api/v1/playback/favorites/{src}/{id}
- POST   /api/v1/playback/{camera_identifier}/play  body {"recording_id": int,
                                                          "source_camera_identifier": str?}
- POST   /api/v1/playback/{camera_identifier}/stop
- GET    /api/v1/playback/{camera_identifier}

Favorites are persisted under ``/favorites/<source_camera>/R<id>.mp4`` plus a
sidecar ``R<id>.json`` carrying the original event metadata + a base64
thumbnail. The directory is mounted from the host outside any storage tier
path so the tier sweeper never touches it — favorites stay playable
indefinitely even after the original recording is purged from tier 3.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import shutil
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from sqlalchemy import select

from viseron.components.storage.models import Recordings
from viseron.components.storage.queries import get_recording_fragments
from viseron.components.webserver.api.handlers import BaseAPIHandler
from viseron.components.webserver.auth import Role
from viseron.domains.camera.fragmenter import Fragment
from viseron.helpers.replay_camera import (
    ReplayBusy,
    get_manager,
)

if TYPE_CHECKING:
    from viseron.domains.camera import AbstractCamera

LOGGER = logging.getLogger(__name__)

FAVORITES_DIR = "/favorites"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _favorite_dir(source_camera: str) -> str:
    if not _SAFE_NAME_RE.match(source_camera):
        raise ValueError(f"Invalid camera identifier: {source_camera!r}")
    return os.path.join(FAVORITES_DIR, source_camera)


def _favorite_video_path(source_camera: str, recording_id: int) -> str:
    return os.path.join(_favorite_dir(source_camera), f"R{recording_id}.mp4")


def _favorite_sidecar_path(source_camera: str, recording_id: int) -> str:
    return os.path.join(_favorite_dir(source_camera), f"R{recording_id}.json")


class PlaybackAPIHandler(BaseAPIHandler):
    """Handler for interactive playback of saved recordings."""

    routes = [
        # Favorites routes must precede the catch-all camera routes so the
        # word "favorites" is not interpreted as a camera identifier.
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": r"/playback/favorites",
            "supported_methods": ["POST"],
            "method": "post_favorite",
            "json_body_schema": vol.Schema(
                {
                    vol.Required("recording_id"): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    ),
                }
            ),
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": r"/playback/favorites",
            "supported_methods": ["GET"],
            "method": "get_favorites",
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": (
                r"/playback/favorites/(?P<source_camera>[A-Za-z0-9_]+)/"
                r"(?P<recording_id>[0-9]+)"
            ),
            "supported_methods": ["DELETE"],
            "method": "delete_favorite",
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": (
                r"/playback/(?P<camera_identifier>[A-Za-z0-9_]+)/play"
            ),
            "supported_methods": ["POST"],
            "method": "post_play",
            "json_body_schema": vol.Schema(
                {
                    vol.Required("recording_id"): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    ),
                    vol.Optional("source_camera_identifier"): vol.All(
                        str, vol.Match(_SAFE_NAME_RE)
                    ),
                }
            ),
        },
        {
            "requires_role": [Role.ADMIN, Role.WRITE],
            "path_pattern": (
                r"/playback/(?P<camera_identifier>[A-Za-z0-9_]+)/stop"
            ),
            "supported_methods": ["POST"],
            "method": "post_stop",
        },
        {
            "requires_role": [Role.ADMIN, Role.READ, Role.WRITE],
            "path_pattern": (
                r"/playback/(?P<camera_identifier>[A-Za-z0-9_]+)"
            ),
            "supported_methods": ["GET"],
            "method": "get_status",
        },
    ]

    # ------------------------------------------------------------------ #
    # camera + recording resolution
    # ------------------------------------------------------------------ #

    def _get_real_camera(
        self, camera_identifier: str
    ) -> AbstractCamera | None:
        """Resolve camera and verify it's a real camera (not a replay camera).

        Playback acquires an on-demand *replay* camera for a real camera —
        the URL path takes the real camera's identifier. If the caller
        passes a replay camera id by mistake (or a non-existent id), we
        respond with a clear error and return None.
        """
        camera = self._get_camera(camera_identifier)
        if camera is None:
            self.response_error(
                HTTPStatus.NOT_FOUND,
                reason=f"Camera {camera_identifier} not found",
            )
            return None
        if camera.is_playback_camera or camera.is_test_camera:
            self.response_error(
                HTTPStatus.BAD_REQUEST,
                reason=(
                    f"Camera {camera_identifier} is itself a replay camera; "
                    f"playback endpoints take a real camera id and spawn a "
                    f"replay camera on demand."
                ),
            )
            return None
        return camera

    def _resolve_recording_path(
        self,
        recording_id: int,
        target_camera: AbstractCamera,
        source_camera_hint: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Look up a recording's playable path.

        Resolution order:
        1. If a favorite blob exists at ``/favorites/<src>/R<id>.mp4`` (using
           either ``source_camera_hint`` or the DB row's camera), use that.
           This keeps favorites playable after tier purge wipes the DB row.
        2. The DB row's ``clip_path`` if the file still exists.
        3. On-the-fly fragment concatenation via the source camera's
           fragmenter.
        """
        # 1. Favorite blob (cheap to check, survives DB purge).
        if source_camera_hint:
            fav_path = _favorite_video_path(source_camera_hint, recording_id)
            if os.path.isfile(fav_path):
                LOGGER.debug(
                    "Resolved recording %d via favorite blob %s",
                    recording_id,
                    fav_path,
                )
                return fav_path, None

        with self._get_session() as session:
            row = session.execute(
                select(Recordings).where(Recordings.id == recording_id)
            ).scalar_one_or_none()
            if row is None:
                # DB row gone — try the favorite as a last resort using
                # whatever camera dirs we have on disk.
                fav_blob = _scan_for_favorite(recording_id)
                if fav_blob:
                    return fav_blob, None
                return None, f"Recording {recording_id} not found"
            source_camera_id = row.camera_identifier
            clip_path = row.clip_path

        # Recheck favorite using the DB-derived source camera, in case the
        # caller didn't pass a hint.
        if not source_camera_hint:
            fav_path = _favorite_video_path(source_camera_id, recording_id)
            if os.path.isfile(fav_path):
                return fav_path, None

        # 2. Permanent event clip on disk.
        if clip_path and os.path.isfile(clip_path):
            return clip_path, None

        # 3. Fall back to on-the-fly concatenation from segments. We need
        # the SOURCE camera (the one that recorded it), because that is
        # the camera whose fragmenter knows the segments folder layout.
        source_camera = self._get_camera(source_camera_id)
        if source_camera is None:
            return None, (
                f"Recording {recording_id} belongs to camera "
                f"{source_camera_id!r} which is not currently registered, "
                f"so its segments cannot be located"
            )
        if not hasattr(source_camera, "fragmenter") or not hasattr(
            source_camera, "recorder"
        ):
            return None, (
                f"Source camera {source_camera_id!r} does not expose a "
                f"fragmenter (unsupported backend)"
            )

        files = get_recording_fragments(
            recording_id,
            source_camera.recorder.lookback,
            self._get_session,
        )
        if not files:
            return None, (
                f"Recording {recording_id} has no segments available on "
                f"disk to concatenate"
            )
        fragments = [
            Fragment(file.filename, file.path, file.duration, file.orig_ctime)
            for file in files
        ]
        clip_temp_path = source_camera.fragmenter.concatenate_fragments(fragments)
        if not clip_temp_path:
            return None, (
                f"Failed to concatenate segments for recording "
                f"{recording_id}"
            )

        LOGGER.debug(
            "Resolved recording %d to temporary clip %s "
            "(via on-the-fly concatenation; target=%s, source=%s)",
            recording_id,
            clip_temp_path,
            target_camera.identifier,
            source_camera_id,
        )
        return clip_temp_path, None

    # ------------------------------------------------------------------ #
    # play / stop / status
    # ------------------------------------------------------------------ #

    async def post_play(self, camera_identifier: str) -> None:
        """Start playback of a recording against a real camera.

        The URL's ``camera_identifier`` is the *real* camera whose config
        should drive detection. A dedicated replay camera
        (``replay_<real>``) is spawned on demand from the real camera's
        config with ``file_source`` set to the resolved clip path, and is
        torn down completely when playback finishes (EOF or explicit stop).
        """
        real_camera = self._get_real_camera(camera_identifier)
        if real_camera is None:
            return

        recording_id = self.json_body["recording_id"]
        source_camera_hint = self.json_body.get("source_camera_identifier")

        file_path, err = await self.run_in_executor(
            self._resolve_recording_path,
            recording_id,
            real_camera,
            source_camera_hint,
        )
        if file_path is None:
            self.response_error(HTTPStatus.NOT_FOUND, reason=err or "Not found")
            return

        manager = get_manager(self._vis)
        if manager is None:
            self.response_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                reason="Replay camera manager is not initialised",
            )
            return

        def _acquire():
            return manager.acquire(camera_identifier, "playback", file_path)

        try:
            handle = await self.run_in_executor(_acquire)
        except ReplayBusy as exc:
            self.response_error(
                HTTPStatus.CONFLICT,
                reason=str(exc),
            )
            return
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.exception("Playback start failed for %s", camera_identifier)
            self.response_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                reason=f"Failed to start playback: {exc}",
            )
            return

        await self.response_success(
            response={
                "status": "playing",
                "camera_identifier": camera_identifier,
                "replay_camera_id": handle.replay_camera_id,
                "token": handle.token,
                "recording_id": recording_id,
                "file": file_path,
                "started_at": _isoformat_now(),
            }
        )

    async def post_stop(self, camera_identifier: str) -> None:
        """Stop any replay camera currently held for this real camera."""
        real_camera = self._get_real_camera(camera_identifier)
        if real_camera is None:
            return

        manager = get_manager(self._vis)
        if manager is None:
            self.response_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                reason="Replay camera manager is not initialised",
            )
            return

        holder = manager.get_holder(camera_identifier)
        if holder is None or holder.purpose != "playback":
            # Nothing to stop, or the replay camera is held by a test run
            # — we don't preempt tests.
            self.response_error(
                HTTPStatus.NOT_FOUND,
                reason=(
                    f"No playback is currently active for "
                    f"{camera_identifier!r}"
                ),
            )
            return

        token = holder.token
        try:
            await self.run_in_executor(manager.release, token)
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.exception("Playback stop failed for %s", camera_identifier)
            self.response_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                reason=f"Failed to stop playback: {exc}",
            )
            return

        await self.response_success(
            response={
                "status": "stopped",
                "camera_identifier": camera_identifier,
            }
        )

    async def get_status(self, camera_identifier: str) -> None:
        """Return replay state for this real camera."""
        real_camera = self._get_real_camera(camera_identifier)
        if real_camera is None:
            return

        manager = get_manager(self._vis)
        if manager is None:
            self.response_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                reason="Replay camera manager is not initialised",
            )
            return

        holder = manager.get_holder(camera_identifier)
        if holder is None:
            await self.response_success(
                response={
                    "camera_identifier": camera_identifier,
                    "is_playing": False,
                }
            )
            return

        await self.response_success(
            response={
                "camera_identifier": camera_identifier,
                "is_playing": True,
                "purpose": holder.purpose,
                "replay_camera_id": holder.replay_camera_id,
                "current_file": holder.source_path,
                "started_at": holder.acquired_at.isoformat(),
            }
        )

    # ------------------------------------------------------------------ #
    # favorites
    # ------------------------------------------------------------------ #

    async def post_favorite(self) -> None:
        """Persist a recording as a favorite.

        Copies the resolved video file (and the thumbnail, if present) into
        ``/favorites/<source_camera>/`` outside any storage tier so it
        survives tier purges. Writes a sidecar JSON with the original
        ``CameraRecordingEvent`` so the favorites list is fully self-contained
        — the frontend can render it without needing the original DB row.
        """
        recording_id = self.json_body["recording_id"]

        try:
            entry, err = await self.run_in_executor(
                self._create_favorite, recording_id
            )
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.exception("Favorite creation failed for recording %d", recording_id)
            self.response_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                reason=f"Failed to create favorite: {exc}",
            )
            return

        if entry is None:
            self.response_error(
                HTTPStatus.NOT_FOUND, reason=err or "Recording not found"
            )
            return

        await self.response_success(response=entry)

    async def get_favorites(self) -> None:
        """List all persisted favorites by scanning the favorites directory."""
        entries = await self.run_in_executor(_list_favorites)
        await self.response_success(response={"favorites": entries})

    async def delete_favorite(
        self, source_camera: str, recording_id: str
    ) -> None:
        """Remove a persisted favorite (video + sidecar)."""
        try:
            rec_id = int(recording_id)
        except ValueError:
            self.response_error(
                HTTPStatus.BAD_REQUEST,
                reason=f"Invalid recording id {recording_id!r}",
            )
            return

        try:
            removed = await self.run_in_executor(
                _remove_favorite, source_camera, rec_id
            )
        except ValueError as exc:
            self.response_error(HTTPStatus.BAD_REQUEST, reason=str(exc))
            return

        if not removed:
            self.response_error(
                HTTPStatus.NOT_FOUND,
                reason=(
                    f"Favorite for {source_camera!r} R{rec_id} does not exist"
                ),
            )
            return

        await self.response_success(
            response={
                "status": "deleted",
                "camera_identifier": source_camera,
                "recording_id": rec_id,
            }
        )

    def _create_favorite(
        self, recording_id: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Synchronous favorite-creation worker (runs in executor)."""
        with self._get_session() as session:
            row = session.execute(
                select(Recordings).where(Recordings.id == recording_id)
            ).scalar_one_or_none()
            if row is None:
                return None, f"Recording {recording_id} not found"
            event = {
                "id": row.id,
                "camera_identifier": row.camera_identifier,
                "type": "recording",
                "trigger_type": row.trigger_type.value
                if row.trigger_type is not None
                else None,
                "start_time": _utc_iso(row.start_time),
                "start_timestamp": row.start_time.replace(
                    tzinfo=datetime.timezone.utc
                ).timestamp()
                if row.start_time
                else None,
                "end_time": _utc_iso(row.end_time),
                "end_timestamp": row.end_time.replace(
                    tzinfo=datetime.timezone.utc
                ).timestamp()
                if row.end_time
                else None,
                "duration": (
                    (row.end_time - row.start_time).total_seconds()
                    if row.end_time
                    else None
                ),
                "created_at": _utc_iso(row.created_at),
                "created_at_timestamp": row.created_at.replace(
                    tzinfo=datetime.timezone.utc
                ).timestamp()
                if row.created_at
                else None,
                "lookback": 0,
                "hls_url": "",
                "thumbnail_path": "",
            }
            disk_thumbnail_path = row.thumbnail_path

        # Resolve a playable source file. Reuse the same logic the play
        # endpoint uses (clip_path → fragment concat) so favorites work for
        # any recording the play endpoint can play.
        source_camera_id = event["camera_identifier"]
        source_camera = self._get_camera(source_camera_id)
        if source_camera is None or not hasattr(source_camera, "fragmenter"):
            return None, (
                f"Source camera {source_camera_id!r} for recording "
                f"{recording_id} is not registered with a fragmenter"
            )

        # We pass source_camera as both target and source — _resolve only
        # uses target_camera for logging.
        playable_path, err = self._resolve_recording_path(
            recording_id, source_camera, source_camera_id
        )
        if playable_path is None:
            return None, err or "Could not resolve playable file"

        os.makedirs(_favorite_dir(source_camera_id), exist_ok=True)
        dest_video = _favorite_video_path(source_camera_id, recording_id)
        # Copy (not move): the source may be the original tier-managed clip
        # which we must leave alone, or a temp concatenation which the
        # fragmenter will clean up. Either way, copy is the safe choice.
        shutil.copyfile(playable_path, dest_video)

        # Embed thumbnail as data URL so the frontend can render it without
        # a separate auth-aware endpoint.
        thumbnail_data_url: str | None = None
        if disk_thumbnail_path and os.path.isfile(disk_thumbnail_path):
            try:
                with open(disk_thumbnail_path, "rb") as fh:
                    raw = fh.read()
                thumbnail_data_url = "data:image/jpeg;base64," + base64.b64encode(
                    raw
                ).decode("ascii")
            except OSError:
                LOGGER.warning(
                    "Could not read thumbnail %s for favorite %d",
                    disk_thumbnail_path,
                    recording_id,
                )

        sidecar = {
            **event,
            "favorited_at": _isoformat_now(),
            "video_path": dest_video,
            "video_size": os.path.getsize(dest_video),
            "thumbnail_path": thumbnail_data_url or "",
        }
        with open(
            _favorite_sidecar_path(source_camera_id, recording_id),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(sidecar, fh)

        LOGGER.info(
            "Saved favorite recording %d (%s) to %s",
            recording_id,
            source_camera_id,
            dest_video,
        )
        return sidecar, None


def _scan_for_favorite(recording_id: int) -> str | None:
    """Walk /favorites/* looking for any RNNN.mp4 matching recording_id.

    Used as a last-resort lookup when the DB row is gone (e.g. after tier
    purge) but the user is replaying from the favorites tab.
    """
    if not os.path.isdir(FAVORITES_DIR):
        return None
    target = f"R{recording_id}.mp4"
    for sub in os.listdir(FAVORITES_DIR):
        candidate = os.path.join(FAVORITES_DIR, sub, target)
        if os.path.isfile(candidate):
            return candidate
    return None


def _list_favorites() -> list[dict[str, Any]]:
    """Read every R*.json sidecar under /favorites/ and return them sorted.

    Sorted newest-first by ``created_at_timestamp`` (or ``favorited_at`` as
    a fallback). Sidecars whose mp4 has been removed out-of-band are
    skipped — the favorite is treated as gone.
    """
    if not os.path.isdir(FAVORITES_DIR):
        return []
    entries: list[dict[str, Any]] = []
    for sub in sorted(os.listdir(FAVORITES_DIR)):
        sub_dir = os.path.join(FAVORITES_DIR, sub)
        if not os.path.isdir(sub_dir):
            continue
        for name in sorted(os.listdir(sub_dir)):
            if not name.startswith("R") or not name.endswith(".json"):
                continue
            sidecar_path = os.path.join(sub_dir, name)
            try:
                with open(sidecar_path, "r", encoding="utf-8") as fh:
                    sidecar = json.load(fh)
            except (OSError, json.JSONDecodeError):
                LOGGER.warning("Skipping unreadable favorite %s", sidecar_path)
                continue
            video_path = sidecar.get("video_path")
            if not video_path or not os.path.isfile(video_path):
                continue
            entries.append(sidecar)
    entries.sort(
        key=lambda e: e.get("created_at_timestamp")
        or e.get("favorited_at")
        or 0,
        reverse=True,
    )
    return entries


def _remove_favorite(source_camera: str, recording_id: int) -> bool:
    """Delete the video + sidecar for a favorite. Returns True if anything
    was actually removed."""
    video = _favorite_video_path(source_camera, recording_id)
    sidecar = _favorite_sidecar_path(source_camera, recording_id)
    removed = False
    for path in (video, sidecar):
        if os.path.isfile(path):
            try:
                os.unlink(path)
                removed = True
            except OSError:
                LOGGER.exception("Failed to remove favorite file %s", path)
    # Clean up empty camera dir.
    cam_dir = _favorite_dir(source_camera)
    if os.path.isdir(cam_dir) and not os.listdir(cam_dir):
        try:
            os.rmdir(cam_dir)
        except OSError:
            pass
    return removed


def _isoformat_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _isoformat(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.datetime.fromtimestamp(
        timestamp, tz=datetime.timezone.utc
    ).isoformat()


def _utc_iso(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.isoformat()
