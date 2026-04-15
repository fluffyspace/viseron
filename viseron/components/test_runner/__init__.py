"""Test runner component.

Drives motion/object detection regression tests against pre-recorded
video clips. Cases live in a separate ``tests.yaml`` file next to
``config.yaml`` and are grouped by the real camera they should run
against — the runner deep-clones each real camera's ffmpeg config into a
synthetic test-mode camera automatically, so users never have to declare
file-sourced cameras by hand.

Opt-in via a ``test_runner:`` block in ``config.yaml`` (an empty mapping
is fine). Settings in the config.yaml block are used as fallbacks;
values under ``settings:`` in tests.yaml take precedence.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from .const import (
    COMPONENT,
    CONFIG_CAMERA_READY_TIMEOUT,
    CONFIG_DEFAULT_DURATION,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    DEFAULT_CAMERA_READY_TIMEOUT,
    DEFAULT_DURATION,
    DEFAULT_SHUTDOWN_ON_COMPLETE,
    DESC_CAMERA_READY_TIMEOUT,
    DESC_COMPONENT,
    DESC_DEFAULT_DURATION,
    DESC_SHUTDOWN_ON_COMPLETE,
    KIND_MOTION,
    KIND_OBJECT,
    KINDS,
    TESTS_CONFIG_PATH,
    TESTS_SETTINGS,
)
from .runner import (
    TestRunner,
    TestRunnerComponent,
    inject_yaml_cases,
    load_and_inject_tests_yaml,
    load_db_cases,
)
from .tests_yaml import load_tests_yaml

if TYPE_CHECKING:
    from viseron import Viseron

LOGGER = logging.getLogger(__name__)


# Every key here is optional. The block only has to exist to opt the
# component in — all real configuration lives in tests.yaml.
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(COMPONENT, description=DESC_COMPONENT): vol.Schema(
            {
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
                    CONFIG_DEFAULT_DURATION,
                    default=DEFAULT_DURATION,
                    description=DESC_DEFAULT_DURATION,
                ): vol.All(int, vol.Range(min=1, max=3600)),
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


def _merge_settings(
    block: dict[str, Any],
    tests_yaml: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a copy of ``block`` with any tests.yaml ``settings`` overlaid.

    tests.yaml is the preferred place for runtime settings since it
    already travels with the cases it governs, but users who split
    settings out of tests.yaml (or who want a single source of truth)
    can still set them on the config.yaml block.
    """
    merged = dict(block)
    if not tests_yaml:
        return merged
    settings = tests_yaml.get(TESTS_SETTINGS) or {}
    for key in (
        CONFIG_SHUTDOWN_ON_COMPLETE,
        CONFIG_CAMERA_READY_TIMEOUT,
        CONFIG_DEFAULT_DURATION,
    ):
        if key in settings:
            merged[key] = settings[key]
    return merged


def setup(vis: "Viseron", config: dict[str, Any]) -> bool:
    """Set up the test runner component.

    Runs in the ``PRE_PARALLEL`` tier so it can inject synthetic ffmpeg
    camera entries for every case in tests.yaml (and the DB catalog)
    *before* the ffmpeg component's parallel setup reads ``config``. The
    component holder is registered under ``vis.data[COMPONENT]`` so the
    CLI entrypoint and REST API can trigger additional runs during the
    lifetime of the process. A run is only auto-triggered at setup time
    when ``auto_start`` is enabled.
    """
    # Load tests.yaml first so its settings can override the config.yaml
    # block before we key off values like default_duration below.
    try:
        tests_yaml = load_tests_yaml(TESTS_CONFIG_PATH)
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception(
            "test_runner: failed to load %s; continuing with no yaml cases",
            TESTS_CONFIG_PATH,
        )
        tests_yaml = None

    block = _merge_settings(config[COMPONENT], tests_yaml)
    config[COMPONENT] = block
    default_duration = int(block.get(CONFIG_DEFAULT_DURATION, DEFAULT_DURATION))

    yaml_cases = (
        inject_yaml_cases(
            vis, config, tests_yaml, default_duration=default_duration
        )
        if tests_yaml is not None
        else []
    )
    db_cases = load_db_cases(vis)

    component = TestRunnerComponent(
        vis,
        block,
        yaml_cases=yaml_cases,
        db_cases=db_cases,
    )
    vis.data[COMPONENT] = component

    LOGGER.info(
        "test_runner: loaded %d yaml case(s) and %d db case(s)",
        len(yaml_cases),
        len(db_cases),
    )
    return True


__all__ = [
    "COMPONENT",
    "CONFIG_SCHEMA",
    "TestRunner",
    "TestRunnerComponent",
    "KIND_MOTION",
    "KIND_OBJECT",
    "inject_yaml_cases",
    "load_and_inject_tests_yaml",
    "load_db_cases",
    "setup",
]
