"""Parse and flatten ``tests.yaml`` into runner-ready case descriptors.

The on-disk layout is grouped by the real camera the user wants to
exercise, then by kind (motion/object) and polarity (positive/negative),
and finally by label (only for ``object/positive``). Each leaf is a
sequence of *sources* that should all satisfy the same expectation.

A source is either:

* a **string** — an absolute path to an MP4 file readable by ffmpeg, or
* a **mapping** with ``from`` and ``to`` keys — a datetime range on the
  parent camera that the runner materializes from recorded fragments
  before the test run.

The flattener produces a plain list of dicts that downstream code (the
synthetic-camera injector and the runner) consumes without caring about
the grouping.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
from typing import Any

import voluptuous as vol
import yaml

from .const import (
    CONFIG_AUTO_START,
    CONFIG_CAMERA_READY_TIMEOUT,
    CONFIG_DEFAULT_DURATION,
    CONFIG_SHUTDOWN_ON_COMPLETE,
    DEFAULT_DURATION,
    EXPECTED_DETECTED,
    EXPECTED_LABELS,
    KIND_MOTION,
    KIND_OBJECT,
    LABEL_ANY,
    POLARITY_NEGATIVE,
    POLARITY_POSITIVE,
    TESTS_CAMERAS,
    TESTS_SETTINGS,
    TESTS_SOURCE_DURATION,
    TESTS_SOURCE_FROM,
    TESTS_SOURCE_NAME,
    TESTS_SOURCE_TO,
)

LOGGER = logging.getLogger(__name__)


# --- schema -----------------------------------------------------------------


_TIMELINE_SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required(TESTS_SOURCE_FROM): str,
        vol.Required(TESTS_SOURCE_TO): str,
        vol.Optional(TESTS_SOURCE_NAME): vol.All(str, vol.Length(min=1)),
        vol.Optional(TESTS_SOURCE_DURATION): vol.All(
            int, vol.Range(min=1, max=3600)
        ),
    }
)


def _source_schema(value: Any) -> Any:
    """Accept either a path string or a timeline range mapping."""
    if isinstance(value, str):
        if not value.strip():
            raise vol.Invalid("source path must not be empty")
        return value
    if isinstance(value, dict):
        return _TIMELINE_SOURCE_SCHEMA(value)
    raise vol.Invalid(
        "source must be a string path or a mapping with 'from'/'to' keys"
    )


_SOURCE_LIST = vol.All([_source_schema], vol.Length(min=1))


_MOTION_BLOCK_SCHEMA = vol.Schema(
    {
        vol.Optional(POLARITY_POSITIVE): _SOURCE_LIST,
        vol.Optional(POLARITY_NEGATIVE): _SOURCE_LIST,
    }
)


_OBJECT_POSITIVE_SCHEMA = vol.Schema(
    {
        # Each key is a label name; the "any" sentinel means "expect any
        # detection". Values are source lists.
        str: _SOURCE_LIST,
    }
)


_OBJECT_BLOCK_SCHEMA = vol.Schema(
    {
        vol.Optional(POLARITY_POSITIVE): _OBJECT_POSITIVE_SCHEMA,
        vol.Optional(POLARITY_NEGATIVE): _SOURCE_LIST,
    }
)


_CAMERA_BLOCK_SCHEMA = vol.Schema(
    {
        vol.Optional(KIND_MOTION): _MOTION_BLOCK_SCHEMA,
        vol.Optional(KIND_OBJECT): _OBJECT_BLOCK_SCHEMA,
    }
)


# Settings in tests.yaml are optional *and* defaults-free: the merge
# step in __init__.setup() only overlays keys the user explicitly set,
# so the config.yaml block remains authoritative for anything tests.yaml
# doesn't mention.
_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONFIG_SHUTDOWN_ON_COMPLETE): bool,
        vol.Optional(CONFIG_CAMERA_READY_TIMEOUT): vol.All(
            int, vol.Range(min=1, max=3600)
        ),
        vol.Optional(CONFIG_AUTO_START): bool,
        vol.Optional(CONFIG_DEFAULT_DURATION): vol.All(
            int, vol.Range(min=1, max=3600)
        ),
    }
)


TESTS_YAML_SCHEMA = vol.Schema(
    {
        vol.Required(TESTS_CAMERAS): vol.Schema(
            {
                str: _CAMERA_BLOCK_SCHEMA,
            }
        ),
        vol.Optional(TESTS_SETTINGS, default=dict): _SETTINGS_SCHEMA,
    }
)


# --- dataclasses ------------------------------------------------------------


class FlatCase(dict):
    """A case descriptor after flattening tests.yaml.

    Behaves like a plain dict for JSON-friendliness but documents the
    expected keys in one place so mypy / readers can follow:

    * ``source_camera`` — the real camera the test should inherit config from
    * ``kind`` — motion | object
    * ``polarity`` — positive | negative
    * ``label`` — object-positive only; either a real label or ``"any"``
    * ``source`` — str path or dict {from, to}
    * ``name`` — human-readable case name, auto-generated if omitted
    * ``slug`` — filesystem/id-safe slug derived from ``name``
    * ``duration`` — observation window seconds (runner uses this)
    * ``expected`` — JSON structure the evaluator checks
    """


# --- helpers ----------------------------------------------------------------


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(value: str) -> str:
    """Return a filesystem/id-safe slug for the given name."""
    slug = _SLUG_RE.sub("_", value.strip()).strip("_.")
    return slug.lower() or "case"


def _parse_datetime(value: str) -> datetime.datetime:
    """Parse a user-supplied ISO-ish datetime.

    Accepts:
    * ``YYYY-MM-DDTHH:MM:SS`` and ``YYYY-MM-DD HH:MM:SS``
    * optional timezone suffix via ``+HH:MM`` / ``-HH:MM`` / ``Z``

    Naive datetimes are treated as local time and converted to UTC so
    downstream queries against ``Files.orig_ctime`` (UTC) match.
    """
    normalized = value.strip().replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError as err:
        raise ValueError(
            f"unable to parse datetime {value!r}: expected ISO 8601 format"
        ) from err
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(datetime.timezone.utc)


def _name_from_source(source: Any) -> str:
    """Derive a readable case name from a source entry."""
    if isinstance(source, str):
        basename = os.path.basename(source)
        stem, _ = os.path.splitext(basename)
        return stem or basename or source
    if isinstance(source, dict):
        # Honor an explicit name if the user gave one.
        if source.get(TESTS_SOURCE_NAME):
            return str(source[TESTS_SOURCE_NAME])
        start = source.get(TESTS_SOURCE_FROM, "start")
        end = source.get(TESTS_SOURCE_TO, "end")
        return f"timeline {start}..{end}"
    return "case"


def _expected_for(
    kind: str,
    polarity: str,
    label: str | None,
) -> dict[str, Any]:
    """Return the evaluator expectation matching a (kind, polarity, label).

    * motion/positive → ``{detected: true}``
    * motion/negative → ``{detected: false}``
    * object/positive with a concrete label → ``{labels: [label]}``
    * object/positive with ``any`` → ``{detected: true}``
    * object/negative → ``{detected: false}``
    """
    if kind == KIND_MOTION:
        return {EXPECTED_DETECTED: polarity == POLARITY_POSITIVE}
    # kind == KIND_OBJECT
    if polarity == POLARITY_NEGATIVE:
        return {EXPECTED_DETECTED: False}
    if label is None or label == LABEL_ANY:
        return {EXPECTED_DETECTED: True}
    return {EXPECTED_LABELS: [label]}


# --- loader / flattener -----------------------------------------------------


def load_tests_yaml(path: str) -> dict[str, Any] | None:
    """Load, parse, and validate tests.yaml.

    Returns the validated dict, or ``None`` if the file does not exist.
    Raises :class:`voluptuous.Invalid` for a malformed file so startup
    fails loudly with the same diagnostics as config.yaml errors.
    """
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fp:
        raw = yaml.safe_load(fp)
    if raw is None:
        LOGGER.warning("tests.yaml at %s is empty; no test cases loaded", path)
        return None
    return TESTS_YAML_SCHEMA(raw)


def flatten_tests_yaml(
    tests_yaml: dict[str, Any],
    *,
    default_duration: int = DEFAULT_DURATION,
) -> list[dict[str, Any]]:
    """Flatten a validated tests.yaml dict into a list of case descriptors.

    Every descriptor carries enough information for the synthetic-camera
    injector to build a unique ffmpeg entry and for the runner to evaluate
    the case afterwards. Sources that need materializing (timeline ranges)
    are emitted unchanged here; resolving them to on-disk paths happens
    later in the pipeline, once the storage layer is available.
    """
    out: list[dict[str, Any]] = []
    cameras = tests_yaml.get(TESTS_CAMERAS) or {}

    # Collisions on (camera, kind, polarity, label, slug) get suffixed so
    # each case produces a distinct synthetic ffmpeg identifier.
    used_slugs: dict[tuple[str, str, str, str | None], set[str]] = {}

    def _unique_slug(
        key: tuple[str, str, str, str | None], base: str
    ) -> str:
        used = used_slugs.setdefault(key, set())
        slug = base
        idx = 1
        while slug in used:
            idx += 1
            slug = f"{base}_{idx}"
        used.add(slug)
        return slug

    for camera_id, camera_block in cameras.items():
        for kind in (KIND_MOTION, KIND_OBJECT):
            kind_block = (camera_block or {}).get(kind)
            if not kind_block:
                continue

            if kind == KIND_MOTION:
                for polarity in (POLARITY_POSITIVE, POLARITY_NEGATIVE):
                    sources = kind_block.get(polarity) or []
                    for source in sources:
                        name = _name_from_source(source)
                        base = _slugify(name)
                        slug = _unique_slug(
                            (camera_id, kind, polarity, None), base
                        )
                        out.append(
                            _build_case(
                                source_camera=camera_id,
                                kind=kind,
                                polarity=polarity,
                                label=None,
                                source=source,
                                name=name,
                                slug=slug,
                                default_duration=default_duration,
                            )
                        )
                continue

            # kind == KIND_OBJECT
            positive = kind_block.get(POLARITY_POSITIVE) or {}
            for label, sources in positive.items():
                for source in sources or []:
                    name = _name_from_source(source)
                    base = _slugify(f"{label}_{name}")
                    slug = _unique_slug(
                        (camera_id, kind, POLARITY_POSITIVE, label), base
                    )
                    out.append(
                        _build_case(
                            source_camera=camera_id,
                            kind=kind,
                            polarity=POLARITY_POSITIVE,
                            label=label,
                            source=source,
                            name=name,
                            slug=slug,
                            default_duration=default_duration,
                        )
                    )

            negative = kind_block.get(POLARITY_NEGATIVE) or []
            for source in negative:
                name = _name_from_source(source)
                base = _slugify(name)
                slug = _unique_slug(
                    (camera_id, kind, POLARITY_NEGATIVE, None), base
                )
                out.append(
                    _build_case(
                        source_camera=camera_id,
                        kind=kind,
                        polarity=POLARITY_NEGATIVE,
                        label=None,
                        source=source,
                        name=name,
                        slug=slug,
                        default_duration=default_duration,
                    )
                )

    return out


def _build_case(
    *,
    source_camera: str,
    kind: str,
    polarity: str,
    label: str | None,
    source: Any,
    name: str,
    slug: str,
    default_duration: int,
) -> dict[str, Any]:
    """Build a flattened case descriptor dict."""
    duration = default_duration
    if isinstance(source, dict) and TESTS_SOURCE_DURATION in source:
        duration = int(source[TESTS_SOURCE_DURATION])

    return {
        "source_camera": source_camera,
        "kind": kind,
        "polarity": polarity,
        "label": label,
        "source": source,
        "name": name,
        "slug": slug,
        "duration": duration,
        "expected": _expected_for(kind, polarity, label),
    }


__all__ = [
    "TESTS_YAML_SCHEMA",
    "FlatCase",
    "flatten_tests_yaml",
    "load_tests_yaml",
    "_parse_datetime",
    "_slugify",
]
