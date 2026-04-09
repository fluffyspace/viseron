"""Object tracker component.

IoU-based multi-object tracker that assigns persistent track IDs
to detected objects across frames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from viseron.helpers.schemas import FLOAT_MIN_ZERO_MAX_ONE
from viseron.helpers.validators import CameraIdentifier, CoerceNoneToDict

from .const import (
    COMPONENT,
    CONFIG_CAMERAS,
    CONFIG_MAX_DISAPPEARED,
    CONFIG_MIN_IOU,
    DATA_OBJECT_TRACKER,
    DEFAULT_MAX_DISAPPEARED,
    DEFAULT_MIN_IOU,
    DESC_CAMERAS,
    DESC_COMPONENT,
    DESC_MAX_DISAPPEARED,
    DESC_MIN_IOU,
)
from .tracker import ObjectTracker

if TYPE_CHECKING:
    from viseron import Viseron

CAMERA_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONFIG_MIN_IOU,
            default=DEFAULT_MIN_IOU,
            description=DESC_MIN_IOU,
        ): FLOAT_MIN_ZERO_MAX_ONE,
        vol.Optional(
            CONFIG_MAX_DISAPPEARED,
            default=DEFAULT_MAX_DISAPPEARED,
            description=DESC_MAX_DISAPPEARED,
        ): vol.All(int, vol.Range(min=1)),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(COMPONENT, description=DESC_COMPONENT): vol.Schema(
            {
                vol.Required(CONFIG_CAMERAS, description=DESC_CAMERAS): {
                    CameraIdentifier(): vol.All(CoerceNoneToDict(), CAMERA_SCHEMA),
                },
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(vis: Viseron, config: dict[str, Any]) -> bool:
    """Set up the object_tracker component."""
    config = config[COMPONENT]

    if DATA_OBJECT_TRACKER not in vis.data:
        vis.data[DATA_OBJECT_TRACKER] = {}

    for camera_identifier, cam_config in config[CONFIG_CAMERAS].items():
        tracker = ObjectTracker(
            vis,
            camera_identifier,
            min_iou=cam_config[CONFIG_MIN_IOU],
            max_disappeared=cam_config[CONFIG_MAX_DISAPPEARED],
        )
        vis.data[DATA_OBJECT_TRACKER][camera_identifier] = tracker

    return True
