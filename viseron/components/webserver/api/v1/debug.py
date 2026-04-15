"""Debug API handler."""

import logging
import os
import sys
import tracemalloc

from viseron.components.webserver.api.handlers import BaseAPIHandler
from viseron.components.webserver.auth import Role
from viseron.const import ENV_PROFILE_MEMORY
from viseron.helpers import (
    get_process_rss,
    shared_frames_report,
    tracemalloc_top_by_file,
)

LOGGER = logging.getLogger(__name__)


class DebugAPIHandler(BaseAPIHandler):
    """Handler for debug/diagnostic API calls."""

    routes = [
        {
            "requires_role": [Role.ADMIN],
            "path_pattern": r"/debug/memory",
            "supported_methods": ["GET"],
            "method": "get_memory",
        },
    ]

    async def get_memory(self) -> None:
        """Return memory snapshot for the main process."""
        rss = get_process_rss()
        tracing = tracemalloc.is_tracing()
        response = {
            "pid": os.getpid(),
            "python_version": sys.version.split()[0],
            "profile_memory_env": os.getenv(ENV_PROFILE_MEMORY, ""),
            "tracemalloc_active": tracing,
            "process": {
                "rss_bytes": rss["rss"],
                "vms_bytes": rss["vms"],
                "shared_bytes": rss["shared"],
                "uss_bytes": rss["uss"],
                "pss_bytes": rss["pss"],
                "rss_mib": round(rss["rss"] / 1024 / 1024, 1),
                "uss_mib": round(rss["uss"] / 1024 / 1024, 1),
            },
            "tracemalloc_top_by_file": tracemalloc_top_by_file(limit=25),
            "shared_frames": shared_frames_report(self._vis),
        }
        await self.response_success(response=response)
