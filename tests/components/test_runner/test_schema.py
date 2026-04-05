"""Tests for the test_runner config schema."""
from __future__ import annotations

import pytest
import voluptuous as vol

from viseron.components.test_runner import CONFIG_SCHEMA
from viseron.components.test_runner.const import (
    COMPONENT,
    CONFIG_CAMERA,
    CONFIG_CAMERA_READY_TIMEOUT,
    CONFIG_CASES,
    CONFIG_DURATION,
    CONFIG_EXPECTED,
    CONFIG_KIND,
    CONFIG_NAME,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    DEFAULT_CAMERA_READY_TIMEOUT,
    DEFAULT_DURATION,
    DEFAULT_SHUTDOWN_ON_COMPLETE,
)


def _base_config(**overrides):
    config = {
        COMPONENT: {
            CONFIG_CASES: [
                {
                    CONFIG_NAME: "person walks by",
                    CONFIG_CAMERA: "test_driveway",
                    CONFIG_KIND: "object",
                    CONFIG_EXPECTED: {"labels": ["person"]},
                },
            ],
        },
    }
    config[COMPONENT].update(overrides)
    return config


def test_minimal_config_applies_defaults() -> None:
    validated = CONFIG_SCHEMA(_base_config())
    block = validated[COMPONENT]
    case = block[CONFIG_CASES][0]
    assert case[CONFIG_DURATION] == DEFAULT_DURATION
    assert block[CONFIG_SHUTDOWN_ON_COMPLETE] is DEFAULT_SHUTDOWN_ON_COMPLETE
    assert block[CONFIG_CAMERA_READY_TIMEOUT] == DEFAULT_CAMERA_READY_TIMEOUT


def test_motion_case_is_accepted() -> None:
    config = _base_config()
    config[COMPONENT][CONFIG_CASES][0] = {
        CONFIG_NAME: "empty room",
        CONFIG_CAMERA: "test_living_room",
        CONFIG_KIND: "motion",
        CONFIG_EXPECTED: {"detected": False},
    }
    CONFIG_SCHEMA(config)


def test_empty_cases_rejected() -> None:
    config = _base_config()
    config[COMPONENT][CONFIG_CASES] = []
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)


def test_unknown_kind_rejected() -> None:
    config = _base_config()
    config[COMPONENT][CONFIG_CASES][0][CONFIG_KIND] = "face"
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)


def test_missing_camera_rejected() -> None:
    config = _base_config()
    del config[COMPONENT][CONFIG_CASES][0][CONFIG_CAMERA]
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)


def test_missing_expected_rejected() -> None:
    config = _base_config()
    del config[COMPONENT][CONFIG_CASES][0][CONFIG_EXPECTED]
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)


def test_invalid_duration_rejected() -> None:
    config = _base_config()
    config[COMPONENT][CONFIG_CASES][0][CONFIG_DURATION] = 0
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)
