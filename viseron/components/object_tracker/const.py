"""Object tracker constants."""

COMPONENT = "object_tracker"

CONFIG_CAMERAS = "cameras"
CONFIG_MIN_IOU = "min_iou"
CONFIG_MAX_DISAPPEARED = "max_disappeared"

DEFAULT_MIN_IOU = 0.3
DEFAULT_MAX_DISAPPEARED = 10

DESC_COMPONENT = "IoU-based multi-object tracker."
DESC_CAMERAS = "Camera-specific tracker configuration."
DESC_MIN_IOU = "Minimum IoU overlap to match a detection to an existing track."
DESC_MAX_DISAPPEARED = (
    "Number of consecutive frames a track can be missing before removal."
)

DATA_OBJECT_TRACKER = "object_tracker"

EVENT_OBJECT_TRACKER_RESULT = "{camera_identifier}/object_tracker/result"
