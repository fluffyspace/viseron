"""Message dataclasses exchanged with the storage-tier subprocess.

These dataclasses cross a ``multiprocessing.Manager`` queue, so both the
parent (``viseron.components.storage``) and the child subprocess must
import them from the same qualified name — otherwise pickle will raise
``ModuleNotFoundError`` on one side. Keeping them in a leaf module with
no ``viseron`` imports lets the subprocess load them without pulling
the rest of the project into memory.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import numpy as np


@dataclass
class DataItem:
    """A tier-check job for a single camera/tier/category/subcategory."""

    cmd: Literal["check_tier"]
    camera_identifier: str
    tier_id: int
    category: str
    subcategories: list[str]
    throttle_period: datetime.timedelta
    max_bytes: int
    min_age: datetime.timedelta
    max_age: datetime.timedelta
    min_bytes: int
    drain: bool
    files_enabled: bool = True
    events_enabled: bool = False
    events_max_bytes: int | None = None
    events_min_age: datetime.timedelta | None = None
    events_max_age: datetime.timedelta | None = None
    events_min_bytes: int | None = None
    callback_id: str | None = None
    data: "np.ndarray | None" = None
    error: str | None = None

    @property
    def throttle_key(self) -> str:
        """Generate a unique key for throttling."""
        return (
            f"{self.camera_identifier}_"
            f"{self.tier_id}_{self.category}_"
            f"{self.subcategories[0]}"
        )


@dataclass
class DataItemMoveFile:
    """A file-move job from one tier directory to another."""

    cmd: Literal["move_file"]
    src: str
    dst: str
    callback_id: str | None = None
    error: str | None = None


@dataclass
class DataItemDeleteFile:
    """A file-delete job for a tier-managed file."""

    cmd: Literal["delete_file"]
    src: str
    callback_id: str | None = None
    error: str | None = None
