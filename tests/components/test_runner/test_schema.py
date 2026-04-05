"""Tests for the test_runner config schema and tests.yaml schema."""
from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol

from viseron.components.test_runner import CONFIG_SCHEMA
from viseron.components.test_runner.const import (
    COMPONENT,
    CONFIG_AUTO_START,
    CONFIG_CAMERA_READY_TIMEOUT,
    CONFIG_DEFAULT_DURATION,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    DEFAULT_AUTO_START,
    DEFAULT_CAMERA_READY_TIMEOUT,
    DEFAULT_DURATION,
    DEFAULT_SHUTDOWN_ON_COMPLETE,
    EXPECTED_DETECTED,
    EXPECTED_LABELS,
    KIND_MOTION,
    KIND_OBJECT,
    POLARITY_NEGATIVE,
    POLARITY_POSITIVE,
)
from viseron.components.test_runner.tests_yaml import (
    TESTS_YAML_SCHEMA,
    flatten_tests_yaml,
)


# --- config.yaml "test_runner:" block schema --------------------------------


def test_empty_block_applies_defaults() -> None:
    """An empty opt-in block is valid and populates all defaults."""
    validated = CONFIG_SCHEMA({COMPONENT: {}})
    block = validated[COMPONENT]
    assert block[CONFIG_SHUTDOWN_ON_COMPLETE] is DEFAULT_SHUTDOWN_ON_COMPLETE
    assert block[CONFIG_CAMERA_READY_TIMEOUT] == DEFAULT_CAMERA_READY_TIMEOUT
    assert block[CONFIG_AUTO_START] is DEFAULT_AUTO_START
    assert block[CONFIG_DEFAULT_DURATION] == DEFAULT_DURATION


def test_block_rejects_invalid_timeout() -> None:
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({COMPONENT: {CONFIG_CAMERA_READY_TIMEOUT: 0}})


def test_block_rejects_invalid_default_duration() -> None:
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({COMPONENT: {CONFIG_DEFAULT_DURATION: 0}})


# --- tests.yaml schema ------------------------------------------------------


def _base_tests_yaml(**camera_overrides: Any) -> dict[str, Any]:
    cameras: dict[str, Any] = {
        "garage": {
            KIND_MOTION: {
                POLARITY_POSITIVE: ["/videos/walk.mp4"],
            },
        }
    }
    cameras.update(camera_overrides)
    return {"cameras": cameras}


def test_tests_yaml_accepts_minimal_motion_case() -> None:
    validated = TESTS_YAML_SCHEMA(_base_tests_yaml())
    assert validated["cameras"]["garage"][KIND_MOTION][POLARITY_POSITIVE] == [
        "/videos/walk.mp4"
    ]


def test_tests_yaml_accepts_object_per_label() -> None:
    tests_yaml = _base_tests_yaml(
        frontdoor={
            KIND_OBJECT: {
                POLARITY_POSITIVE: {
                    "person": ["/videos/person.mp4"],
                    "car": ["/videos/car.mp4", "/videos/car2.mp4"],
                    "any": ["/videos/mixed.mp4"],
                },
                POLARITY_NEGATIVE: ["/videos/empty.mp4"],
            },
        }
    )
    validated = TESTS_YAML_SCHEMA(tests_yaml)
    positive = validated["cameras"]["frontdoor"][KIND_OBJECT][POLARITY_POSITIVE]
    assert list(positive.keys()) == ["person", "car", "any"]
    assert positive["car"] == ["/videos/car.mp4", "/videos/car2.mp4"]


def test_tests_yaml_accepts_timeline_source() -> None:
    tests_yaml = _base_tests_yaml(
        garage={
            KIND_MOTION: {
                POLARITY_POSITIVE: [
                    {
                        "from": "2026-01-15T14:30:00",
                        "to": "2026-01-15T14:31:00",
                    }
                ],
            },
        }
    )
    validated = TESTS_YAML_SCHEMA(tests_yaml)
    entry = validated["cameras"]["garage"][KIND_MOTION][POLARITY_POSITIVE][0]
    assert entry["from"] == "2026-01-15T14:30:00"
    assert entry["to"] == "2026-01-15T14:31:00"


def test_tests_yaml_rejects_empty_source_list() -> None:
    with pytest.raises(vol.Invalid):
        TESTS_YAML_SCHEMA(
            {
                "cameras": {
                    "garage": {KIND_MOTION: {POLARITY_POSITIVE: []}},
                },
            }
        )


def test_tests_yaml_rejects_non_path_source() -> None:
    with pytest.raises(vol.Invalid):
        TESTS_YAML_SCHEMA(
            {
                "cameras": {
                    "garage": {KIND_MOTION: {POLARITY_POSITIVE: [123]}},
                },
            }
        )


def test_tests_yaml_requires_cameras_key() -> None:
    with pytest.raises(vol.Invalid):
        TESTS_YAML_SCHEMA({"settings": {}})


# --- flattening -------------------------------------------------------------


def test_flatten_produces_cases_per_source() -> None:
    tests_yaml = TESTS_YAML_SCHEMA(
        {
            "cameras": {
                "garage": {
                    KIND_MOTION: {
                        POLARITY_POSITIVE: ["/videos/walk.mp4"],
                        POLARITY_NEGATIVE: ["/videos/empty.mp4"],
                    },
                    KIND_OBJECT: {
                        POLARITY_POSITIVE: {
                            "person": ["/videos/person.mp4"],
                            "any": ["/videos/mixed.mp4"],
                        },
                        POLARITY_NEGATIVE: ["/videos/empty.mp4"],
                    },
                }
            }
        }
    )
    cases = flatten_tests_yaml(tests_yaml, default_duration=20)
    # One per source leaf: 1 motion-positive + 1 motion-negative
    # + 1 object-positive/person + 1 object-positive/any + 1 object-negative
    assert len(cases) == 5

    by_key = {
        (c["kind"], c["polarity"], c["label"], c["slug"]): c for c in cases
    }
    assert (
        KIND_MOTION,
        POLARITY_POSITIVE,
        None,
        "walk",
    ) in by_key
    person_case = by_key[(KIND_OBJECT, POLARITY_POSITIVE, "person", "person_person")]
    assert person_case["expected"] == {EXPECTED_LABELS: ["person"]}
    any_case = by_key[(KIND_OBJECT, POLARITY_POSITIVE, "any", "any_mixed")]
    assert any_case["expected"] == {EXPECTED_DETECTED: True}
    negative_object = by_key[
        (KIND_OBJECT, POLARITY_NEGATIVE, None, "empty")
    ]
    assert negative_object["expected"] == {EXPECTED_DETECTED: False}
    # default_duration propagates to cases that don't override it
    assert all(c["duration"] == 20 for c in cases)


def test_flatten_dedupes_colliding_slugs() -> None:
    tests_yaml = TESTS_YAML_SCHEMA(
        {
            "cameras": {
                "garage": {
                    KIND_MOTION: {
                        POLARITY_POSITIVE: [
                            "/a/walk.mp4",
                            "/b/walk.mp4",  # same basename, different dir
                        ],
                    }
                }
            }
        }
    )
    cases = flatten_tests_yaml(tests_yaml)
    slugs = [c["slug"] for c in cases]
    assert slugs == ["walk", "walk_2"]


def test_flatten_timeline_source_yields_descriptor() -> None:
    tests_yaml = TESTS_YAML_SCHEMA(
        {
            "cameras": {
                "garage": {
                    KIND_MOTION: {
                        POLARITY_POSITIVE: [
                            {
                                "from": "2026-01-15T14:30:00",
                                "to": "2026-01-15T14:31:00",
                            }
                        ],
                    }
                }
            }
        }
    )
    cases = flatten_tests_yaml(tests_yaml)
    assert len(cases) == 1
    case = cases[0]
    assert isinstance(case["source"], dict)
    assert case["source"]["from"] == "2026-01-15T14:30:00"
    assert case["expected"] == {EXPECTED_DETECTED: True}
