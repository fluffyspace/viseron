"""Playback API Handler.

Drives interactive replay through ``playback_mode`` cameras: pick a saved
recording, ffmpeg streams its mp4 (or concatenated segments) at native FPS
into the camera pipeline so motion / object detection / NVR / MQTT all run
identically to a live feed. Used for end-to-end debugging without having
to physically generate live footage.

Endpoints:
- POST /api/v1/playback/{camera_identifier}/play  body {"recording_id": int}
- POST /api/v1/playback/{camera_identifier}/stop
- GET  /api/v1/playback/{camera_identifier}
"""
from __future__ import annotations

import datetime
import logging
import os
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from sqlalchemy import select

from viseron.components.storage.models import Recordings
from viseron.components.storage.queries import get_recording_fragments
from viseron.components.webserver.api.handlers import BaseAPIHandler
from viseron.components.webserver.auth import Role
from viseron.domains.camera.fragmenter import Fragment

if TYPE_CHECKING:
    from viseron.domains.camera import AbstractCamera

LOGGER = logging.getLogger(__name__)


class PlaybackAPIHandler(BaseAPIHandler):
    """Handler for interactive playback of saved recordings."""

    routes = [
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

    def _get_playback_camera(
        self, camera_identifier: str
    ) -> AbstractCamera | None:
        """Resolve camera and verify it is a playback camera.

        Returns None if not found OR not a playback camera, with the
        appropriate error response already sent.
        """
        camera = self._get_camera(camera_identifier)
        if camera is None:
            self.response_error(
                HTTPStatus.NOT_FOUND,
                reason=f"Camera {camera_identifier} not found",
            )
            return None
        if not camera.is_playback_camera:
            self.response_error(
                HTTPStatus.BAD_REQUEST,
                reason=(
                    f"Camera {camera_identifier} is not a playback camera "
                    f"(set playback_mode: true in its config)"
                ),
            )
            return None
        return camera

    def _resolve_recording_path(
        self, recording_id: int, target_camera: AbstractCamera
    ) -> tuple[str | None, str | None]:
        """Look up a recording's playable path.

        Returns ``(file_path, error_message)``. If file_path is set the
        caller can feed it to ``swap_playback_source``. Otherwise an
        error response should be sent with the message.

        If the recording has a ``clip_path`` and the file exists, that
        is used directly. Otherwise the source camera's fragmenter is
        used to concatenate the recording's segments into a temporary
        mp4 (matches the existing ``_concatenate_fragments`` flow used
        on natural recording end).
        """
        with self._get_session() as session:
            row = session.execute(
                select(Recordings).where(Recordings.id == recording_id)
            ).scalar_one_or_none()
            if row is None:
                return None, f"Recording {recording_id} not found"
            source_camera_id = row.camera_identifier
            clip_path = row.clip_path

        # If a permanent event clip exists on disk, use it directly.
        if clip_path and os.path.isfile(clip_path):
            return clip_path, None

        # Fall back to on-the-fly concatenation from segments. We need
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

        # Recreate the same Fragment list the recorder builds when it
        # finalizes a recording. Needs the source camera's lookback so
        # the leading segments are included.
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

    async def post_play(self, camera_identifier: str) -> None:
        """Start (or restart) playback of a recording on this camera."""
        camera = self._get_playback_camera(camera_identifier)
        if camera is None:
            return

        recording_id = self.json_body["recording_id"]

        file_path, err = await self.run_in_executor(
            self._resolve_recording_path, recording_id, camera
        )
        if file_path is None:
            self.response_error(HTTPStatus.NOT_FOUND, reason=err or "Not found")
            return

        try:
            await self.run_in_executor(camera.swap_playback_source, file_path)
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
                "recording_id": recording_id,
                "file": file_path,
                "started_at": _isoformat_now(),
            }
        )

    async def post_stop(self, camera_identifier: str) -> None:
        """Stop playback on this camera."""
        camera = self._get_playback_camera(camera_identifier)
        if camera is None:
            return

        def _stop() -> None:
            with camera._playback_lock:  # pylint: disable=protected-access
                if not camera.stopped.is_set():
                    camera.stop_camera()

        try:
            await self.run_in_executor(_stop)
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
        """Return playback state for this camera."""
        camera = self._get_playback_camera(camera_identifier)
        if camera is None:
            return

        await self.response_success(
            response={
                "camera_identifier": camera_identifier,
                "is_playing": bool(camera.connected),
                "is_on": bool(camera.is_on),
                "current_file": getattr(
                    camera, "_playback_current_file", None
                ),
                "started_at": _isoformat(
                    getattr(camera, "_playback_started_at", None)
                ),
            }
        )


def _isoformat_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _isoformat(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.datetime.fromtimestamp(
        timestamp, tz=datetime.timezone.utc
    ).isoformat()
