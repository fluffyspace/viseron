"""Test runner constants."""
from __future__ import annotations

import os
from typing import Final

from viseron.const import CONFIG_DIR

COMPONENT: Final = "test_runner"

DESC_COMPONENT: Final = (
    "Run pre-recorded video files through configured camera pipelines and "
    "verify that motion/object detection behaves as expected. Test cases "
    "live in a separate <code>tests.yaml</code> file next to "
    "<code>config.yaml</code>; the <code>test_runner</code> block here only "
    "carries optional runtime settings. Results are stored in the "
    "<code>test_runs</code> and <code>test_results</code> tables and are "
    "kept separate from live events via the <code>test</code> flag."
)

# tests.yaml lives alongside config.yaml under VISERON_CONFIG_DIR.
TESTS_CONFIG_PATH: Final = os.path.join(CONFIG_DIR, "tests.yaml")

# Case kinds.
KIND_MOTION: Final = "motion"
KIND_OBJECT: Final = "object"
KINDS: Final = (KIND_MOTION, KIND_OBJECT)

# Polarities. A positive case expects the detection to happen; a negative
# case expects silence.
POLARITY_POSITIVE: Final = "positive"
POLARITY_NEGATIVE: Final = "negative"
POLARITIES: Final = (POLARITY_POSITIVE, POLARITY_NEGATIVE)

# Sentinel label under object/positive that translates to "any detection"
# rather than a specific label expectation.
LABEL_ANY: Final = "any"

# Test run status values.
STATUS_RUNNING: Final = "running"
STATUS_COMPLETE: Final = "complete"
STATUS_ERROR: Final = "error"

# Top-level keys for the test_runner block in config.yaml (settings only,
# no cases — those live in tests.yaml).
CONFIG_SHUTDOWN_ON_COMPLETE: Final = "shutdown_on_complete"
CONFIG_CAMERA_READY_TIMEOUT: Final = "camera_ready_timeout"
CONFIG_AUTO_START: Final = "auto_start"
CONFIG_DEFAULT_DURATION: Final = "default_duration"

DEFAULT_SHUTDOWN_ON_COMPLETE: Final = False
DEFAULT_CAMERA_READY_TIMEOUT: Final = 60
DEFAULT_AUTO_START: Final = False
DEFAULT_DURATION: Final = 15

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
DESC_DEFAULT_DURATION: Final = (
    "Default observation window (in seconds) applied to cases that do "
    "not override it. Should be at least as long as the longest source "
    "video plus a small grace period so detections land in the database "
    "before the runner evaluates them."
)

# tests.yaml schema keys.
TESTS_CAMERAS: Final = "cameras"
TESTS_SETTINGS: Final = "settings"
TESTS_KIND_MOTION: Final = KIND_MOTION
TESTS_KIND_OBJECT: Final = KIND_OBJECT
TESTS_POLARITY_POSITIVE: Final = POLARITY_POSITIVE
TESTS_POLARITY_NEGATIVE: Final = POLARITY_NEGATIVE
TESTS_SOURCE_FROM: Final = "from"
TESTS_SOURCE_TO: Final = "to"
TESTS_SOURCE_NAME: Final = "name"
TESTS_SOURCE_DURATION: Final = "duration"

# Per-case keys used inside the runner itself (after flattening).
CONFIG_NAME: Final = "name"
CONFIG_CAMERA: Final = "camera"
CONFIG_KIND: Final = "kind"
CONFIG_DURATION: Final = "duration"
CONFIG_EXPECTED: Final = "expected"
CONFIG_VIDEO_PATH: Final = "video_path"
CONFIG_POLARITY: Final = "polarity"
CONFIG_SOURCE_CAMERA: Final = "source_camera"

# Expected schema keys consumed by the evaluator.
EXPECTED_DETECTED: Final = "detected"
EXPECTED_LABELS: Final = "labels"

# On-disk cache for clips materialized from timeline ranges.
TIMELINE_CACHE_SUBDIR: Final = "_timeline"
