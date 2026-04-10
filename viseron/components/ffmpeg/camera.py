"""FFmpeg camera."""

from __future__ import annotations

import contextlib
import copy
import multiprocessing as mp
import os
import signal
import threading
import time
from queue import Empty, Full
from typing import TYPE_CHECKING, Any

import cv2
import setproctitle
import voluptuous as vol

from viseron.const import ENV_CUDA_SUPPORTED, ENV_VAAPI_SUPPORTED
from viseron.domains.camera import AbstractCamera, EventFrameBytesData
from viseron.domains.camera.config import (
    BASE_CONFIG_SCHEMA as BASE_CAMERA_CONFIG_SCHEMA,
    DEFAULT_RECORDER,
    RECORDER_SCHEMA as BASE_RECORDER_SCHEMA,
)
from viseron.domains.camera.shared_frames import SharedFrame
from viseron.exceptions import DomainNotReady, FFprobeError, FFprobeTimeout
from viseron.helpers import escape_string, utcnow
from viseron.helpers.logs import SensitiveInformationFilter
from viseron.helpers.validators import (
    UNDEFINED,
    CameraIdentifier,
    CoerceNoneToDict,
    Deprecated,
    Maybe,
)
from viseron.watchdog.process_watchdog import RestartableProcess
from viseron.watchdog.thread_watchdog import RestartableThread

from .const import (
    COMPONENT,
    CONFIG_AUDIO_CODEC,
    CONFIG_CODEC,
    CONFIG_FFMPEG_LOGLEVEL,
    CONFIG_FFMPEG_RECOVERABLE_ERRORS,
    CONFIG_FFPROBE_LOGLEVEL,
    CONFIG_FILE_SOURCE,
    CONFIG_FPS,
    CONFIG_FRAME_TIMEOUT,
    CONFIG_GLOBAL_ARGS,
    CONFIG_HEIGHT,
    CONFIG_HOST,
    CONFIG_HWACCEL_ARGS,
    CONFIG_INPUT_ARGS,
    CONFIG_PASSWORD,
    CONFIG_PATH,
    CONFIG_PIX_FMT,
    CONFIG_PLAYBACK_MODE,
    CONFIG_PORT,
    CONFIG_PROTOCOL,
    CONFIG_RAW_COMMAND,
    CONFIG_RECORD_ONLY,
    CONFIG_RECORDER,
    CONFIG_RECORDER_AUDIO_CODEC,
    CONFIG_RECORDER_AUDIO_FILTERS,
    CONFIG_RECORDER_CODEC,
    CONFIG_RECORDER_HWACCEL_ARGS,
    CONFIG_RECORDER_OUPTUT_ARGS,
    CONFIG_RECORDER_VIDEO_FILTERS,
    CONFIG_RTSP_TRANSPORT,
    CONFIG_SEGMENTS_FOLDER,
    CONFIG_STREAM_FORMAT,
    CONFIG_SUBSTREAM,
    CONFIG_TEST_MODE,
    CONFIG_USERNAME,
    CONFIG_VIDEO_FILTERS,
    CONFIG_WIDTH,
    DEFAULT_AUDIO_CODEC,
    DEFAULT_CODEC,
    DEFAULT_FFMPEG_LOGLEVEL,
    DEFAULT_FFMPEG_RECOVERABLE_ERRORS,
    DEFAULT_FFPROBE_LOGLEVEL,
    DEFAULT_FILE_SOURCE,
    DEFAULT_FPS,
    DEFAULT_FRAME_TIMEOUT,
    DEFAULT_GLOBAL_ARGS,
    DEFAULT_HEIGHT,
    DEFAULT_HWACCEL_ARGS,
    DEFAULT_INPUT_ARGS,
    DEFAULT_PASSWORD,
    DEFAULT_PIX_FMT,
    DEFAULT_PLAYBACK_MODE,
    DEFAULT_PROTOCOL,
    DEFAULT_RAW_COMMAND,
    DEFAULT_RECORD_ONLY,
    DEFAULT_RECORDER_AUDIO_CODEC,
    DEFAULT_RECORDER_AUDIO_FILTERS,
    DEFAULT_RECORDER_HWACCEL_ARGS,
    DEFAULT_RECORDER_OUTPUT_ARGS,
    DEFAULT_RECORDER_VIDEO_FILTERS,
    DEFAULT_RTSP_TRANSPORT,
    DEFAULT_STREAM_FORMAT,
    DEFAULT_SUBSTREAM,
    DEFAULT_TEST_MODE,
    DEFAULT_USERNAME,
    DEFAULT_VIDEO_FILTERS,
    DEFAULT_WIDTH,
    DESC_AUDIO_CODEC,
    DESC_CODEC,
    DESC_FFMPEG_LOGLEVEL,
    DESC_FFMPEG_RECOVERABLE_ERRORS,
    DESC_FFPROBE_LOGLEVEL,
    DESC_FILE_SOURCE,
    DESC_FPS,
    DESC_FRAME_TIMEOUT,
    DESC_GLOBAL_ARGS,
    DESC_HEIGHT,
    DESC_HOST,
    DESC_HWACCEL_ARGS,
    DESC_INPUT_ARGS,
    DESC_PASSWORD,
    DESC_PATH,
    DESC_PIX_FMT,
    DESC_PLAYBACK_MODE,
    DESC_PORT,
    DESC_PROTOCOL,
    DESC_RAW_COMMAND,
    DESC_RECORD_ONLY,
    DESC_RECORDER,
    DESC_RECORDER_AUDIO_CODEC,
    DESC_RECORDER_AUDIO_FILTERS,
    DESC_RECORDER_CODEC,
    DESC_RECORDER_FFMPEG_LOGLEVEL,
    DESC_RECORDER_HWACCEL_ARGS,
    DESC_RECORDER_OUTPUT_ARGS,
    DESC_RECORDER_VIDEO_FILTERS,
    DESC_RTSP_TRANSPORT,
    DESC_SEGMENTS_FOLDER,
    DESC_STREAM_FORMAT,
    DESC_SUBSTREAM,
    DESC_TEST_MODE,
    DESC_USERNAME,
    DESC_VIDEO_FILTERS,
    DESC_WIDTH,
    FFMPEG_LOGLEVELS,
    HWACCEL_VAAPI,
    MAX_EMPTY_FRAMES,
    STREAM_FORMAT_MAP,
)
from .recorder import Recorder
from .stream import Stream

if TYPE_CHECKING:
    from viseron import Viseron
    from viseron.components.nvr.nvr import FrameIntervalCalculator
    from viseron.components.storage.models import TriggerTypes
    from viseron.domains.object_detector.detected_object import DetectedObject


def get_default_hwaccel_args() -> list[str]:
    """Return hardware acceleration args for FFmpeg."""
    # Dont enable VA-API if CUDA is available
    if (
        os.getenv(ENV_VAAPI_SUPPORTED) == "true"
        and os.getenv(ENV_CUDA_SUPPORTED) != "true"
    ):
        return HWACCEL_VAAPI
    return DEFAULT_HWACCEL_ARGS


STREAM_SCEHMA_DICT = {
    vol.Optional(
        CONFIG_FILE_SOURCE,
        default=DEFAULT_FILE_SOURCE,
        description=DESC_FILE_SOURCE,
    ): Maybe(vol.All(str, vol.Length(min=1))),
    vol.Required(CONFIG_PATH, description=DESC_PATH): vol.All(str, vol.Length(min=1)),
    vol.Required(CONFIG_PORT, description=DESC_PORT): vol.All(int, vol.Range(min=1)),
    vol.Optional(
        CONFIG_STREAM_FORMAT,
        default=DEFAULT_STREAM_FORMAT,
        description=DESC_STREAM_FORMAT,
    ): vol.In(STREAM_FORMAT_MAP.keys()),
    vol.Optional(
        CONFIG_PROTOCOL, default=DEFAULT_PROTOCOL, description=DESC_PROTOCOL
    ): Maybe(vol.Any("rtsp", "rtsps", "rtmp", "http", "https")),
    vol.Optional(CONFIG_WIDTH, default=DEFAULT_WIDTH, description=DESC_WIDTH): Maybe(
        int
    ),
    vol.Optional(CONFIG_HEIGHT, default=DEFAULT_HEIGHT, description=DESC_HEIGHT): Maybe(
        int
    ),
    vol.Optional(CONFIG_FPS, default=DEFAULT_FPS, description=DESC_FPS): Maybe(
        vol.All(int, vol.Range(min=1))
    ),
    vol.Optional(
        CONFIG_INPUT_ARGS, default=DEFAULT_INPUT_ARGS, description=DESC_INPUT_ARGS
    ): Maybe(list),
    vol.Optional(
        CONFIG_HWACCEL_ARGS,
        default=get_default_hwaccel_args(),
        description=DESC_HWACCEL_ARGS,
    ): Maybe(list),
    vol.Optional(CONFIG_CODEC, default=DEFAULT_CODEC, description=DESC_CODEC): str,
    vol.Optional(
        CONFIG_AUDIO_CODEC, default=DEFAULT_AUDIO_CODEC, description=DESC_AUDIO_CODEC
    ): Maybe(str),
    vol.Optional(
        CONFIG_RTSP_TRANSPORT,
        default=DEFAULT_RTSP_TRANSPORT,
        description=DESC_RTSP_TRANSPORT,
    ): vol.Any("tcp", "udp", "udp_multicast", "http"),
    vol.Optional(
        CONFIG_VIDEO_FILTERS,
        default=DEFAULT_VIDEO_FILTERS,
        description=DESC_VIDEO_FILTERS,
    ): list,
    vol.Optional(
        CONFIG_PIX_FMT, default=DEFAULT_PIX_FMT, description=DESC_PIX_FMT
    ): vol.Any("nv12", "yuv420p"),
    vol.Optional(
        CONFIG_FRAME_TIMEOUT,
        default=DEFAULT_FRAME_TIMEOUT,
        description=DESC_FRAME_TIMEOUT,
    ): vol.All(int, vol.Range(1, 60)),
    vol.Optional(
        CONFIG_RAW_COMMAND,
        default=DEFAULT_RAW_COMMAND,
        description=DESC_RAW_COMMAND,
    ): Maybe(str),
}

FFMPEG_LOGLEVEL_SCEHMA = vol.Schema(vol.In(FFMPEG_LOGLEVELS.keys()))

RECORDER_SCHEMA = BASE_RECORDER_SCHEMA.extend(
    {
        vol.Optional(
            CONFIG_RECORDER_HWACCEL_ARGS,
            default=DEFAULT_RECORDER_HWACCEL_ARGS,
            description=DESC_RECORDER_HWACCEL_ARGS,
        ): [str],
        vol.Optional(
            CONFIG_RECORDER_CODEC,
            default=UNDEFINED,
            description=DESC_RECORDER_CODEC,
        ): Maybe(str),
        vol.Optional(
            CONFIG_RECORDER_AUDIO_CODEC,
            default=DEFAULT_RECORDER_AUDIO_CODEC,
            description=DESC_RECORDER_AUDIO_CODEC,
        ): Maybe(str),
        vol.Optional(
            CONFIG_RECORDER_VIDEO_FILTERS,
            default=DEFAULT_RECORDER_VIDEO_FILTERS,
            description=DESC_RECORDER_VIDEO_FILTERS,
        ): [str],
        vol.Optional(
            CONFIG_RECORDER_AUDIO_FILTERS,
            default=DEFAULT_RECORDER_AUDIO_FILTERS,
            description=DESC_RECORDER_AUDIO_FILTERS,
        ): [str],
        vol.Optional(
            CONFIG_RECORDER_OUPTUT_ARGS,
            default=DEFAULT_RECORDER_OUTPUT_ARGS,
            description=DESC_RECORDER_OUTPUT_ARGS,
        ): [str],
        Deprecated(
            CONFIG_SEGMENTS_FOLDER,
            description=DESC_SEGMENTS_FOLDER,
        ): str,
        vol.Optional(
            CONFIG_FFMPEG_LOGLEVEL,
            default=DEFAULT_FFMPEG_LOGLEVEL,
            description=DESC_RECORDER_FFMPEG_LOGLEVEL,
        ): FFMPEG_LOGLEVEL_SCEHMA,
    }
)

_camera_schema_with_stream = BASE_CAMERA_CONFIG_SCHEMA.extend(STREAM_SCEHMA_DICT)

CAMERA_SCHEMA = _camera_schema_with_stream.extend(
    {
        vol.Required(CONFIG_HOST, description=DESC_HOST): str,
        vol.Optional(
            CONFIG_USERNAME, default=DEFAULT_USERNAME, description=DESC_USERNAME
        ): Maybe(str),
        vol.Optional(
            CONFIG_PASSWORD, default=DEFAULT_PASSWORD, description=DESC_PASSWORD
        ): Maybe(str),
        vol.Optional(
            CONFIG_GLOBAL_ARGS,
            default=DEFAULT_GLOBAL_ARGS,
            description=DESC_GLOBAL_ARGS,
        ): list,
        vol.Optional(
            CONFIG_SUBSTREAM, default=DEFAULT_SUBSTREAM, description=DESC_SUBSTREAM
        ): Maybe(vol.Schema(STREAM_SCEHMA_DICT)),
        vol.Optional(
            CONFIG_FFMPEG_LOGLEVEL,
            default=DEFAULT_FFMPEG_LOGLEVEL,
            description=DESC_FFMPEG_LOGLEVEL,
        ): FFMPEG_LOGLEVEL_SCEHMA,
        vol.Optional(
            CONFIG_FFMPEG_RECOVERABLE_ERRORS,
            default=DEFAULT_FFMPEG_RECOVERABLE_ERRORS,
            description=DESC_FFMPEG_RECOVERABLE_ERRORS,
        ): [str],
        vol.Optional(
            CONFIG_FFPROBE_LOGLEVEL,
            default=DEFAULT_FFPROBE_LOGLEVEL,
            description=DESC_FFPROBE_LOGLEVEL,
        ): FFMPEG_LOGLEVEL_SCEHMA,
        vol.Optional(
            CONFIG_RECORDER, default=DEFAULT_RECORDER, description=DESC_RECORDER
        ): vol.All(CoerceNoneToDict(), RECORDER_SCHEMA),
        vol.Optional(
            CONFIG_RECORD_ONLY,
            default=DEFAULT_RECORD_ONLY,
            description=DESC_RECORD_ONLY,
        ): bool,
        vol.Optional(
            CONFIG_TEST_MODE,
            default=DEFAULT_TEST_MODE,
            description=DESC_TEST_MODE,
        ): bool,
        vol.Optional(
            CONFIG_PLAYBACK_MODE,
            default=DEFAULT_PLAYBACK_MODE,
            description=DESC_PLAYBACK_MODE,
        ): bool,
    }
)


def _validate_playback_camera(config: dict[str, Any]) -> dict[str, Any]:
    """Reject incompatible options on playback cameras.

    Playback cameras dynamically swap their input file via the playback API.
    A few features break that lifecycle and must be rejected up front:

    - ``substream``: the substream's segment process is started with
      ``register=True``, so the watchdog will auto-restart it after EOF
      and defeat the playback stop logic.
    - ``raw_command``: bypasses the ``file_source`` plumbing entirely.
    - ``record_only``: skips the detection pipeline, which is the whole
      point of playback.
    - ``file_source``: required (we need an initial path to bootstrap the
      Stream's ffprobe step).
    """
    if not config.get(CONFIG_PLAYBACK_MODE):
        return config
    if config.get(CONFIG_SUBSTREAM):
        raise vol.Invalid(
            "playback_mode cameras cannot use substream. The substream's "
            "segment process auto-restarts on EOF, which conflicts with the "
            "playback stop lifecycle."
        )
    if config.get(CONFIG_RAW_COMMAND):
        raise vol.Invalid(
            "playback_mode cameras cannot use raw_command. raw_command "
            "bypasses the file_source plumbing required for playback."
        )
    if config.get(CONFIG_RECORD_ONLY):
        raise vol.Invalid(
            "playback_mode cameras cannot use record_only. The detection "
            "pipeline is required for playback."
        )
    if not config.get(CONFIG_FILE_SOURCE):
        raise vol.Invalid(
            "playback_mode cameras must declare an initial file_source. "
            "It is used as a bootstrap fixture for ffprobe at boot; the "
            "actual replay file is set at runtime via the playback API."
        )
    return config


CONFIG_SCHEMA = vol.Schema(
    {
        CameraIdentifier(): vol.All(CAMERA_SCHEMA, _validate_playback_camera),
    }
)


def setup(vis: Viseron, config: dict[str, Any], identifier: str, attempt: int) -> bool:
    """Set up the ffmpeg camera domain."""
    try:
        Camera(vis, config[identifier], identifier, attempt)
    except (FFprobeError, FFprobeTimeout) as error:
        raise DomainNotReady from error
    return True


class Camera(AbstractCamera):
    """Represents a camera which is consumed via FFmpeg."""

    def __init__(
        self, vis: Viseron, config: dict[str, Any], identifier: str, attempt: int
    ) -> None:
        # Add password to SensitiveInformationFilter.
        # It is done in AbstractCamera but since we are calling Stream before
        # super().__init__ we need to do it here as well.
        # For this reason we dont have to clear the SensitiveInformationFilter
        # on unload since AbstractCamera will do it in its unload method
        if config[CONFIG_PASSWORD]:
            SensitiveInformationFilter.add_sensitive_string(config[CONFIG_PASSWORD])
            SensitiveInformationFilter.add_sensitive_string(
                escape_string(config[CONFIG_PASSWORD])
            )

        self._poll_timer = utcnow().timestamp()
        self._frame_reader = None
        self._frame_relay = None
        # RLock so that swap_playback_source / stop helpers can re-enter
        # without deadlocking. Used by both the REST API and the EOF watcher
        # thread to serialize play / stop / natural-EOF transitions on
        # playback cameras. Always initialized so callers don't have to
        # branch on camera type, but only meaningfully held for playback.
        self._playback_lock = threading.RLock()
        self._playback_eof_watcher: threading.Thread | None = None
        self._playback_current_file: str | None = None
        self._playback_started_at: float | None = None
        # Stream must be initialized before super().__init__ is called as it raises
        # FFprobeError/FFprobeTimeout which is caught in setup() and re-raised as
        # DomainNotReady
        self.stream = Stream(config, self, identifier, attempt)

        super().__init__(vis, COMPONENT, config, identifier)
        self._frame_queue: mp.Queue[  # pylint: disable=unsubscriptable-object
            bytes
        ] = mp.Queue(maxsize=2)
        self._capture_frames = mp.Event()
        self._thread_stuck = False
        self.resolution = self.stream.width, self.stream.height
        self.decode_error = mp.Event()

        if cv2.ocl.haveOpenCL():
            cv2.ocl.setUseOpenCL(True)
        self._recorder = Recorder(vis, config, self)

        self._check_segment_process_thread: RestartableThread | None = None

        self._logger.debug(
            f"Resolution: {self.resolution[0]}x{self.resolution[1]} "
            f"@ {self.stream.fps} FPS"
        )
        self._logger.debug(f"Camera {self.name} initialized")

    def _create_frame_reader(self) -> tuple[RestartableProcess, RestartableThread]:
        """Return a frame reader thread."""
        if self._frame_queue:
            self._frame_queue.close()
        self._frame_queue = mp.Queue(maxsize=2)
        # Playback cameras must not auto-restart on poll-timeout: a finite
        # mp4 ends naturally and we want the camera to transition to
        # disconnected, not to loop the file. The EOF watcher (started in
        # _start_camera) is responsible for the explicit stop.
        relay_restart = None if self.is_playback_camera else self.start_camera
        # Start watchdogs for this process since it spawns a RestartablePopen
        return RestartableProcess(
            name="viseron.camera." + self.identifier,
            args=(self._frame_queue,),
            target=self.read_frames,
            daemon=True,
            register=True,
            start_watchdogs=True,
        ), RestartableThread(
            name="viseron.camera." + self.identifier + ".relay_frame",
            target=self.relay_frame,
            poll_method=self.poll_method,
            poll_target=self.poll_target,
            daemon=True,
            register=True,
            restart_method=relay_restart,
        )

    def _start_recording_only(self) -> None:
        """Record segments only.

        Used when output_frames is False which means we are only using the camera for
        storing recordings and not for image processing.
        """
        self._logger.debug("Starting recording only mode")

        def check_segment_process() -> None:
            while self._capture_frames.is_set():
                time.sleep(1)
                if (
                    self.stream.segment_process
                    and self.stream.segment_process.subprocess
                    and self.stream.segment_process.subprocess.poll() is None
                ):
                    self.connected = True
                    continue
                self.connected = False
            self.connected = False

        self._check_segment_process_thread = RestartableThread(
            name="viseron.camera." + self.identifier + ".segment_check",
            target=check_segment_process,
            daemon=True,
            register=True,
        )
        self._check_segment_process_thread.start()
        self.stream.record_only()

    def read_frames(
        self,
        frame_queue: mp.Queue[bytes],  # pylint: disable=unsubscriptable-object
    ) -> None:
        """Read frames from camera."""
        setproctitle.setproctitle("viseron.camera." + self.identifier + ".read_frames")
        self.decode_error.clear()
        empty_frames = 0
        self._thread_stuck = False

        self.stream.start_pipe()

        while self._capture_frames.is_set():
            if self.decode_error.is_set():
                time.sleep(5)
                self._logger.error("Restarting frame pipe")
                self.stream.close_pipe()
                self.stream.start_pipe()
                self.decode_error.clear()
                empty_frames = 0

            frame_bytes = self.stream.read()
            if frame_bytes:
                empty_frames = 0
                # Dont queue frames if consumer is not ready
                with contextlib.suppress(Full):
                    frame_queue.put_nowait(frame_bytes)
                continue

            if self._thread_stuck:
                return

            if self.stream.poll() is not None:
                if self._config.get(CONFIG_PLAYBACK_MODE):
                    # Playback cameras consume a finite file. ffmpeg exiting
                    # is the natural end-of-stream, NOT a decode error: do
                    # not restart the pipe (that would loop the file).
                    # Clear capture_frames so the parent's relay thread
                    # exits, and break out of the read loop. The EOF
                    # watcher in the parent will then call stop_camera to
                    # finalize cleanup (stop active recording, etc).
                    self._logger.info("Playback file finished, signalling stop")
                    self._capture_frames.clear()
                    break
                self._logger.error("Frame reader process has exited")
                self.decode_error.set()
                continue

            empty_frames += 1
            if empty_frames >= MAX_EMPTY_FRAMES:
                self._logger.error("Did not receive a frame")
                self.decode_error.set()

        self.stream.close_pipe()
        self._frame_queue.close()
        self._logger.debug("Frame reader stopped")
        os.kill(os.getpid(), signal.SIGKILL)

    def relay_frame(self) -> None:
        """Read from the frame queue and create a SharedFrame."""
        self._logger.debug("Starting frame relay")
        self._poll_timer = utcnow().timestamp()
        while self._capture_frames.is_set():
            if self.decode_error.is_set():
                self.connected = False
                self.still_image_available = self.still_image_configured

            try:
                frame_bytes = self._frame_queue.get(timeout=1)
            except Empty:
                continue

            self.connected = True
            self.still_image_available = True

            if len(frame_bytes) == self.stream.frame_bytes_size:
                shared_frame = SharedFrame(
                    self.stream.color_plane_width,
                    self.stream.color_plane_height,
                    self.stream.pixel_format,
                    (self.stream.width, self.stream.height),
                    self.identifier,
                )
            else:
                continue

            self._poll_timer = utcnow().timestamp()
            self.shared_frames.create(shared_frame, frame_bytes)
            self.current_frame = shared_frame
            self._vis.dispatch_event(
                self.frame_bytes_topic,
                EventFrameBytesData(
                    camera_identifier=self.identifier,
                    shared_frame=self.current_frame,
                ),
                store=False,
            )

        self.connected = False
        self.still_image_available = self.still_image_configured
        self._logger.debug("Frame relay stopped")

    def poll_target(self) -> None:
        """Close pipe when RestartableThread.poll_timeout has been reached."""
        self._logger.error("Timeout waiting for frame")
        self._thread_stuck = True
        self.stop_camera()

    def poll_method(self) -> bool:
        """Return true on frame timeout for RestartableThread to trigger a restart."""
        now = utcnow().timestamp()

        # Make sure we timeout at some point if we never get the first frame.
        if now - self._poll_timer > (DEFAULT_FRAME_TIMEOUT * 2):
            return True

        if not self.connected:
            return False

        return now - self._poll_timer > self._config[CONFIG_FRAME_TIMEOUT]

    def calculate_output_fps(self, scanners: list[FrameIntervalCalculator]) -> None:
        """Calculate the camera output fps based on registered frame scanners.

        Overrides AbstractCamera.calculate_output_fps since we can't use the default
        implementation if the user has entered a raw pipeline.
        """
        if self._config[CONFIG_RAW_COMMAND]:
            self.output_fps = self.stream.fps
            return None

        return super().calculate_output_fps(scanners)

    def _start_camera(self) -> None:
        """Start capturing frames from camera."""
        self._capture_frames.set()
        if self._config[CONFIG_RECORD_ONLY]:
            self._start_recording_only()
            return

        self._logger.debug("Starting capture thread")
        if not self._frame_reader or not self._frame_reader.is_alive():
            self._logger.debug("Creating new frame reader")
            self._frame_reader, self._frame_relay = self._create_frame_reader()
            self._frame_reader.start()
            self._frame_relay.start()
            if self.is_playback_camera:
                self._start_playback_eof_watcher()

    def _start_playback_eof_watcher(self) -> None:
        """Start a thread that calls stop_camera when ffmpeg exits naturally.

        For playback cameras the input file is finite, so ffmpeg eventually
        exits on EOF. The subprocess clears ``_capture_frames`` so the read
        loop and the relay thread both exit, but nothing in the parent calls
        ``stop_camera`` to finalize state (active recording, ``stopped``
        event, MQTT updates). This watcher polls the frame reader and, once
        it dies, schedules ``stop_camera`` from a separate helper thread so
        the join inside ``stop_camera`` does not deadlock.

        Each watcher is bound to the specific frame_reader instance that
        existed when it was started. If a subsequent play replaces the
        frame_reader, the stale watcher detects the swap (its captured
        ``frame_reader`` no longer matches ``self._frame_reader``) and
        exits without touching the new playback.
        """
        # Capture the frame_reader by identity so we don't act on a stale
        # one if the user starts another playback before this watcher
        # gets to acquire the lock.
        frame_reader = self._frame_reader

        def watch() -> None:
            while frame_reader is not None and frame_reader.is_alive():
                time.sleep(0.5)
            if not self.is_playback_camera:
                return
            with self._playback_lock:
                # If a newer play has already replaced our frame_reader,
                # we're a stale watcher from the previous session. Bail
                # out without touching the new playback.
                if self._frame_reader is not frame_reader:
                    return
                # Another thread (explicit stop) may have already called
                # stop_camera in the meantime; that is fine, stop_camera
                # is idempotent against an already-stopped state.
                if self.stopped.is_set():
                    return
                self._logger.debug(
                    "Playback EOF watcher: frame reader exited, stopping camera"
                )
                # stop_camera joins the relay thread and the frame reader.
                # We are NOT one of those threads (we are the watcher), so
                # there is no self-join deadlock.
                self.stop_camera()

        watcher = threading.Thread(
            target=watch,
            name=f"viseron.camera.{self.identifier}.playback_eof_watcher",
            daemon=True,
        )
        self._playback_eof_watcher = watcher
        watcher.start()

    def swap_playback_source(self, file_path: str) -> None:
        """Swap the file_source on a playback camera and restart it.

        ``Stream.__init__`` runs ffprobe and caches width/height/fps/codec
        in ``self.stream``; ``frame_bytes_size`` is derived from that. So
        we cannot just mutate ``_config[CONFIG_FILE_SOURCE]`` and call
        ``start_camera`` - the relay would silently drop frames whose
        size does not match the cached value. Instead, fully rebuild
        Stream and Recorder against the new file under the per-camera
        playback lock.

        Caller is expected to be the playback REST API, which has already
        validated the path. This method does not validate file existence.
        """
        if not self.is_playback_camera:
            raise RuntimeError(
                f"swap_playback_source called on non-playback camera "
                f"{self.identifier!r}"
            )

        with self._playback_lock:
            # Stop current playback if any. stop_camera is a no-op if the
            # camera is already stopped.
            if not self.stopped.is_set():
                self._logger.debug("Stopping current playback before swap")
                self.stop_camera()
                # stop_camera sets the stopped event synchronously.

            # Build a fresh config with the new file_source. Deep copy so
            # we don't mutate any object that may still be referenced by
            # the previous Stream/Recorder.
            new_config = copy.deepcopy(self._config)
            new_config[CONFIG_FILE_SOURCE] = file_path

            # Rebuild Stream against the new file. This re-runs ffprobe
            # so width/height/fps/codec/audio_codec are correct for the
            # new file. attempt=1 because we don't track retries here.
            self._logger.debug(f"Rebuilding Stream for playback file {file_path}")
            self.stream = Stream(new_config, self, self.identifier, 1)

            # Update derived values that hang off the Stream.
            self._config = new_config
            self.resolution = (self.stream.width, self.stream.height)

            # Recorder caches storage paths from config; rebuild it so any
            # change in resolution / codec is reflected. Folders are
            # identifier-keyed so the playback camera writes to its own
            # subdirectory automatically.
            self._recorder = Recorder(self._vis, new_config, self)

            self._playback_current_file = file_path
            self._playback_started_at = utcnow().timestamp()

            self._logger.info(f"Starting playback of {file_path}")
            self.start_camera()

    def _stop_camera(self) -> None:
        """Release the connection to the camera."""
        self._capture_frames.clear()
        if self._frame_relay:
            self._logger.debug("Stopping capture thread")
            self._frame_relay.stop()
            self._frame_relay.join(timeout=5)

        if self._frame_reader:
            self._logger.debug("Stopping frame reader process")
            self._frame_reader.stop()
            self._frame_reader.join(timeout=5)
            if self._frame_reader.is_alive():
                self._logger.debug("Timed out trying to stop camera. Killing pipe")
                self._frame_reader.kill()
                self._frame_reader.join(timeout=5)
                self._frame_reader = None
                self.stream.close_pipe()

        if self._config[CONFIG_RECORD_ONLY] and self._check_segment_process_thread:
            self._logger.debug("Stopping record-only process")
            self._check_segment_process_thread.stop()
            self._check_segment_process_thread.join(timeout=5)
            self._check_segment_process_thread = None
            self.stream.close_pipe()

    def start_recorder(
        self,
        shared_frame: SharedFrame,
        objects_in_fov: list[DetectedObject] | None,
        trigger_type: TriggerTypes,
    ) -> None:
        """Start camera recorder."""
        self._recorder.start(shared_frame, objects_in_fov or [], trigger_type)

    def stop_recorder(self) -> None:
        """Stop camera recorder."""
        self._recorder.stop(self.recorder.active_recording)

    @property
    def output_fps(self) -> int:
        """Set stream output fps."""
        return self.stream.output_fps

    @output_fps.setter
    def output_fps(self, fps: int) -> None:
        self.stream.output_fps = fps

    @property
    def resolution(self) -> tuple[int, int]:
        """Return stream resolution."""
        return self._resolution

    @resolution.setter
    def resolution(self, resolution: tuple[int, int]) -> None:
        """Set stream resolution."""
        self._resolution = resolution

    @property
    def mainstream_resolution(self) -> tuple[int, int]:
        """Return mainstream resolution."""
        return self.stream.mainstream.width, self.stream.mainstream.height

    @property
    def recorder(self) -> Recorder:
        """Return recorder instance."""
        return self._recorder

    @property
    def is_recording(self) -> bool:
        """Return recording status."""
        return self._recorder.is_recording

    @property
    def is_test_camera(self) -> bool:
        """Return True if this camera is a test-runner camera."""
        return bool(self._config.get(CONFIG_TEST_MODE, False))

    @property
    def is_playback_camera(self) -> bool:
        """Return True if this camera is a playback camera."""
        return bool(self._config.get(CONFIG_PLAYBACK_MODE, False))
