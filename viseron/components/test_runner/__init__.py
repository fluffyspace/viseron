"""Test runner component.

Runs a set of pre-recorded video files through existing camera pipelines
and verifies that motion/object detection produces the expected outcomes.

The runner references cameras that already live under another camera
component (for example ``ffmpeg``) and must be configured with
``test_mode: true`` and a ``file_source`` pointing at the video to
exercise. Detections produced by those cameras are written with
``test=True`` and are kept out of the regular events view.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from .const import (
    COMPONENT,
    CONFIG_AUTO_START,
    CONFIG_CAMERA,
    CONFIG_CAMERA_READY_TIMEOUT,
    CONFIG_CASES,
    CONFIG_DURATION,
    CONFIG_EXPECTED,
    CONFIG_KIND,
    CONFIG_NAME,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    CONFIG_VIDEO_PATH,
    DEFAULT_AUTO_START,
    DEFAULT_CAMERA_READY_TIMEOUT,
    DEFAULT_DURATION,
    DEFAULT_SHUTDOWN_ON_COMPLETE,
    DESC_AUTO_START,
    DESC_CAMERA,
    DESC_CAMERA_READY_TIMEOUT,
    DESC_CASES,
    DESC_COMPONENT,
    DESC_DURATION,
    DESC_EXPECTED,
    DESC_KIND,
    DESC_NAME,
    DESC_SHUTDOWN_ON_COMPLETE,
    DESC_VIDEO_PATH,
    KIND_MOTION,
    KIND_OBJECT,
    KINDS,
)
from .runner import TestRunner, TestRunnerComponent, inject_db_cases

if TYPE_CHECKING:
    from viseron import Viseron

LOGGER = logging.getLogger(__name__)


CASE_SCHEMA = vol.Schema(
    {
        vol.Required(CONFIG_NAME, description=DESC_NAME): vol.All(
            str, vol.Length(min=1)
        ),
        vol.Required(CONFIG_CAMERA, description=DESC_CAMERA): vol.All(
            str, vol.Length(min=1)
        ),
        vol.Required(CONFIG_KIND, description=DESC_KIND): vol.In(KINDS),
        vol.Optional(
            CONFIG_DURATION,
            default=DEFAULT_DURATION,
            description=DESC_DURATION,
        ): vol.All(int, vol.Range(min=1, max=3600)),
        vol.Required(CONFIG_EXPECTED, description=DESC_EXPECTED): vol.Schema(
            {}, extra=vol.ALLOW_EXTRA
        ),
        vol.Optional(
            CONFIG_VIDEO_PATH,
            default=None,
            description=DESC_VIDEO_PATH,
        ): vol.Any(None, str),
    }
)


CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(COMPONENT, description=DESC_COMPONENT): {
            vol.Required(CONFIG_CASES, description=DESC_CASES): vol.All(
                [CASE_SCHEMA], vol.Length(min=1)
            ),
            vol.Optional(
                CONFIG_SHUTDOWN_ON_COMPLETE,
                default=DEFAULT_SHUTDOWN_ON_COMPLETE,
                description=DESC_SHUTDOWN_ON_COMPLETE,
            ): bool,
            vol.Optional(
                CONFIG_CAMERA_READY_TIMEOUT,
                default=DEFAULT_CAMERA_READY_TIMEOUT,
                description=DESC_CAMERA_READY_TIMEOUT,
            ): vol.All(int, vol.Range(min=1, max=3600)),
            vol.Optional(
                CONFIG_AUTO_START,
                default=DEFAULT_AUTO_START,
                description=DESC_AUTO_START,
            ): bool,
        },
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(vis: "Viseron", config: dict[str, Any]) -> bool:
    """Set up the test runner component.

    Runs in the ``PRE_PARALLEL`` tier so it can inject synthetic ffmpeg
    camera entries for DB-backed test cases before the ffmpeg component's
    parallel setup reads ``config``. The component holder is registered
    under ``vis.data[COMPONENT]`` so the CLI entrypoint and REST API can
    trigger additional runs during the lifetime of the process. A run is
    only auto-triggered at setup time when ``auto_start`` is explicitly
    enabled in the config.
    """
    db_cases = inject_db_cases(vis, config)
    component = TestRunnerComponent(vis, config[COMPONENT], db_cases=db_cases)
    vis.data[COMPONENT] = component
    if config[COMPONENT][CONFIG_AUTO_START]:
        component.trigger_run()
    return True


__all__ = [
    "COMPONENT",
    "CONFIG_SCHEMA",
    "TestRunner",
    "TestRunnerComponent",
    "KIND_MOTION",
    "KIND_OBJECT",
    "setup",
]
