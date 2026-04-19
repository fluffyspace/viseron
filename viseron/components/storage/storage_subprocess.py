"""Parent-side handle for the storage tier-check subprocess.

The actual worker runs from ``subprocess_workers/storage_tier_subprocess.py``
(top-level, outside the viseron package) so it can stay lean — it does
not need opencv, scipy, or the rest of the viseron import graph to move
and delete files. This module contains only the parent-side glue:

* ``TierCheckWorker`` — spawns and talks to the subprocess via the
  shared manager queue.
* Re-exports the three ``DataItem*`` dataclasses from their new home so
  existing callers (``tier_handler``, ``check_tier``) keep working.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from subprocess_workers.storage_tier_messages import (
    DataItem,
    DataItemDeleteFile,
    DataItemMoveFile,
)

from viseron.helpers.subprocess_worker import SubProcessWorker
from viseron.watchdog.subprocess_watchdog import RestartablePopen

if TYPE_CHECKING:
    from collections.abc import Callable

    from viseron import Viseron

LOGGER = logging.getLogger(__name__)

__all__ = [
    "DataItem",
    "DataItemDeleteFile",
    "DataItemMoveFile",
    "TierCheckWorker",
]


class TierCheckWorker(SubProcessWorker):
    """Spawn and forward jobs to the storage tier-check subprocess."""

    def __init__(self, vis: "Viseron", cpulimit: int | None, workers: int) -> None:
        self._cpulimit = cpulimit
        self._workers = workers
        self._callbacks: dict[
            str,
            "Callable[[DataItem | DataItemMoveFile | DataItemDeleteFile], None]",
        ] = {}
        # Log callbacks dict size on power-of-2 growth so we can see if
        # pending callbacks are leaking (subprocess not returning items,
        # id() collisions, etc.) without spamming the log on each send.
        self._next_callbacks_log_threshold = 16
        super().__init__(vis, f"{__name__}.tier_check_worker", qsize=0)

    def spawn_subprocess(self) -> RestartablePopen:
        """Spawn the standalone subprocess entry script."""
        return RestartablePopen(
            (
                "python3 -u subprocess_workers/storage_tier_subprocess.py "
                f"--manager-port {self._server_port} "
                f"--manager-authkey {self._authkey_store.authkey} "
                f"--cpulimit {self._cpulimit} "
                f"--workers {self._workers} "
                "--loglevel DEBUG"
            ).split(" "),
            name=self.subprocess_name,
            stdout=self._log_pipe,
            stderr=self._log_pipe,
        )

    def send_command(
        self,
        item: DataItem | DataItemMoveFile | DataItemDeleteFile,
        callback: "Callable[[DataItem | DataItemMoveFile | DataItemDeleteFile], None]"
        | None,
    ) -> None:
        """Forward a job to the subprocess, remembering its callback."""
        if callback is not None:
            item.callback_id = str(id(callback))
            self._callbacks[item.callback_id] = callback
            size = len(self._callbacks)
            if size >= self._next_callbacks_log_threshold:
                LOGGER.warning(
                    "TierCheckWorker pending callbacks dict size: %d "
                    "(latest cmd=%s cam=%s)",
                    size,
                    getattr(item, "cmd", None),
                    getattr(item, "camera_identifier", None),
                )
                self._next_callbacks_log_threshold = size * 2
        self.input_queue.put(item)

    def work_output(
        self, item: DataItem | DataItemMoveFile | DataItemDeleteFile
    ) -> None:
        """Dispatch the subprocess's reply to the original caller."""
        if not item.callback_id:
            return
        callback = self._callbacks.pop(item.callback_id, None)
        if callback:
            callback(item)
