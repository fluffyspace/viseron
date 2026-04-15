"""ORM-backed wrappers around the tier-check compute helpers.

The arithmetic lives in ``subprocess_workers.storage_tier_compute`` so
the storage subprocess can import it without pulling in the rest of
viseron. This module keeps the SQLAlchemy-ORM loaders that the test
suite uses (tests construct sessions from the ORM metadata, so keeping
a parallel ORM path here is cheaper than rewriting the tests to the
Core-level Table definitions).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import select

from subprocess_workers.storage_tier_compute import (
    FILES_COMPUTE_DTYPE,
    FILES_DTYPE,
    FILES_RESULT_DTYPE,
    RECORDINGS_DTYPE,
    RECORDINGS_FILES_COMPUTE_DTYPE,
    RECORDINGS_FILES_DTYPE,
    RECORDINGS_RESULT_DTYPE,
    get_files_to_move,
    get_recordings_to_move,
)
from viseron.components.storage.models import Files, Recordings
from viseron.helpers import utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "FILES_COMPUTE_DTYPE",
    "FILES_DTYPE",
    "FILES_RESULT_DTYPE",
    "RECORDINGS_DTYPE",
    "RECORDINGS_FILES_COMPUTE_DTYPE",
    "RECORDINGS_FILES_DTYPE",
    "RECORDINGS_RESULT_DTYPE",
    "get_files_to_move",
    "get_recordings_to_move",
    "load_recordings",
    "load_tier",
]


def load_tier(
    get_session: Callable[..., "Session"],
    category: str,
    subcategories: list[str],
    tier_id: int,
    camera_identifier: str,
) -> np.ndarray:
    """Load the tier files for a camera into the compute dtype."""
    with get_session() as session:
        stmt = select(Files.id, Files.size, Files.orig_ctime).where(
            Files.camera_identifier == camera_identifier,
            Files.tier_id == tier_id,
            Files.category == category,
            Files.subcategory.in_(subcategories),
        )
        rows = session.execute(stmt).yield_per(1000)
        data = [
            (row.id, row.size, int(row.orig_ctime.timestamp())) for row in rows
        ]
    return np.array(data, dtype=FILES_COMPUTE_DTYPE)


def load_recordings(
    get_session: Callable[..., "Session"],
    camera_identifier: str,
) -> np.ndarray:
    """Load the recordings for a camera into the compute dtype."""
    with get_session() as session:
        stmt = select(
            Recordings.id,
            Recordings.start_time,
            Recordings.end_time,
            Recordings.adjusted_start_time,
            Recordings.created_at,
        ).where(Recordings.camera_identifier == camera_identifier)
        rows = session.execute(stmt).yield_per(1000)
        now_ts = utcnow().timestamp()
        data = [
            (
                row.id,
                int(row.start_time.timestamp()),
                int(row.adjusted_start_time.timestamp()),
                int(row.end_time.timestamp() if row.end_time else now_ts),
                int(row.created_at.timestamp()),
            )
            for row in rows
        ]
    return np.array(data, dtype=RECORDINGS_DTYPE)
