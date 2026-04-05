"""Test runner constants."""
from __future__ import annotations

from typing import Final

COMPONENT: Final = "test_runner"

DESC_COMPONENT: Final = (
    "Run pre-recorded video files through configured camera pipelines and "
    "verify that motion/object detection behaves as expected. Results are "
    "stored in the <code>test_runs</code> and <code>test_results</code> "
    "tables and are kept separate from live events via the <code>test</code> "
    "flag."
)

# Case kinds.
KIND_MOTION: Final = "motion"
KIND_OBJECT: Final = "object"
KINDS: Final = (KIND_MOTION, KIND_OBJECT)

# Test run status values.
STATUS_RUNNING: Final = "running"
STATUS_COMPLETE: Final = "complete"
STATUS_ERROR: Final = "error"

# Top-level config keys.
CONFIG_CASES: Final = "cases"
CONFIG_SHUTDOWN_ON_COMPLETE: Final = "shutdown_on_complete"
CONFIG_CAMERA_READY_TIMEOUT: Final = "camera_ready_timeout"
CONFIG_AUTO_START: Final = "auto_start"

DEFAULT_SHUTDOWN_ON_COMPLETE: Final = False
DEFAULT_CAMERA_READY_TIMEOUT: Final = 60
DEFAULT_AUTO_START: Final = False

DESC_CASES: Final = "List of test cases to run."
DESC_SHUTDOWN_ON_COMPLETE: Final = (
    "If true, Viseron will shut down once the current test run is "
    "complete. Set when using the test_runner as a CI entrypoint so the "
    "process exits with a status code that reflects pass/fail."
)
DESC_CAMERA_READY_TIMEOUT: Final = (
    "How long to wait (seconds) for each referenced test camera to be "
    "registered by its owning component before failing the run."
)
DESC_AUTO_START: Final = (
    "Automatically trigger a test run as soon as Viseron finishes "
    "starting up. Leave disabled if you plan to drive runs from the UI "
    "or the REST API."
)

# Per-case config keys.
CONFIG_NAME: Final = "name"
CONFIG_CAMERA: Final = "camera"
CONFIG_KIND: Final = "kind"
CONFIG_DURATION: Final = "duration"
CONFIG_EXPECTED: Final = "expected"
CONFIG_VIDEO_PATH: Final = "video_path"

DEFAULT_DURATION: Final = 15

DESC_NAME: Final = "Human readable name of the test case."
DESC_CAMERA: Final = (
    "Identifier of the camera that should process the video. The camera "
    "must be declared under an existing camera component (e.g. <code>ffmpeg"
    "</code>) with <code>test_mode: true</code> and a <code>file_source"
    "</code> pointing at the video file."
)
DESC_KIND: Final = "Kind of detection to assert on. One of: motion, object."
DESC_DURATION: Final = (
    "How many seconds of wall clock time to observe the camera for before "
    "evaluating detections. Should be at least as long as the video itself "
    "plus a small grace period to let detections land in the database."
)
DESC_EXPECTED: Final = (
    "Expectation against which collected detections are evaluated. See the "
    "evaluator documentation for the exact schema per kind."
)
DESC_VIDEO_PATH: Final = (
    "Optional absolute path to the video used by this case. Only used for "
    "reporting — the actual source is determined by the referenced camera."
)

# Expected schema keys.
EXPECTED_DETECTED: Final = "detected"
EXPECTED_LABELS: Final = "labels"
