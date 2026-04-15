"""On-demand replay camera manager.

A *replay camera* is an ffmpeg camera cloned from a real camera at runtime
for the purpose of re-playing a recorded clip through the full detection
pipeline. Two callers use them: the playback REST API (user-initiated
replay of a recording) and the test runner (batch replay of saved test
cases). A replay camera is spawned on :meth:`ReplayCameraManager.acquire`
and fully torn down on :meth:`ReplayCameraManager.release`, so nothing
resides in memory between uses.

Mutual exclusion: at most one replay camera per real camera may be held
at a time. A second :meth:`acquire` for the same real camera while held
raises :class:`ReplayBusy` — the caller decides whether to surface that
as HTTP 409, a test-runner skip, or a queued retry.
"""

from __future__ import annotations

import copy
import datetime
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from viseron.components.ffmpeg.const import (
    COMPONENT as FFMPEG_COMPONENT,
    CONFIG_CAMERA,
)
from viseron.components.nvr import optional_domains as nvr_optional_domains
from viseron.components.nvr.const import (
    COMPONENT as NVR_COMPONENT,
    DOMAIN as NVR_DOMAIN,
)
from viseron.domain_registry import DomainState
from viseron.domains import (
    RequireDomain,
    get_unload_order,
    setup_domain,
    setup_domains,
    unload_domain,
)
from viseron.domains.camera.const import (
    DOMAIN as CAMERA_DOMAIN,
    EVENT_CAMERA_STOPPED,
)

if TYPE_CHECKING:
    from viseron import Viseron
    from viseron.domains.camera import AbstractCamera

LOGGER = logging.getLogger(__name__)

Purpose = Literal["playback", "test"]

_DETECTION_DOMAINS = ("motion_detector", "object_detector")
_MANAGER_KEY = "replay_camera_manager"


class ReplayBusy(RuntimeError):
    """Raised when the replay camera for a real camera is already held."""

    def __init__(self, real_camera_id: str, holder: "ReplayHolder") -> None:
        self.real_camera_id = real_camera_id
        self.holder = holder
        super().__init__(
            f"replay camera for {real_camera_id!r} is in use by "
            f"{holder.purpose} since {holder.acquired_at.isoformat()}"
        )


def replay_camera_id(real_camera_id: str) -> str:
    """Canonical replay-camera identifier for a given real camera."""
    return f"replay_{real_camera_id}"


@dataclass
class ReplayHolder:
    """Record of who currently holds a replay camera."""

    token: str
    real_camera_id: str
    replay_camera_id: str
    purpose: Purpose
    source_path: str
    acquired_at: datetime.datetime
    # Unsubscribe function for the EVENT_CAMERA_STOPPED listener so we can
    # auto-release on ffmpeg EOF. Set after acquire completes.
    unsubscribe_stopped: Any = None


@dataclass
class ReplayHandle:
    """Returned by :meth:`ReplayCameraManager.acquire`."""

    token: str
    replay_camera_id: str


class ReplayCameraManager:
    """Lifecycle manager for on-demand replay cameras.

    The full pipeline (camera + motion/object detectors + NVR) is
    registered on acquire via :func:`setup_domain` + :func:`setup_domains`,
    and unregistered on release via :func:`get_unload_order` +
    :func:`unload_domain`. No domain entries survive a release, so a
    subsequent acquire for the same real camera re-registers from scratch.
    """

    def __init__(self, vis: "Viseron", config: dict[str, Any]) -> None:
        self._vis = vis
        # Canonical reference to the live config dict so we can mutate the
        # ffmpeg / detection / nvr sub-configs in place the same way the
        # boot-time setup does.
        self._config = config
        self._global_lock = threading.Lock()
        self._per_camera_locks: dict[str, threading.Lock] = {}
        self._holders: dict[str, ReplayHolder] = {}

    # ------------------------------------------------------------------ public

    def acquire(
        self,
        real_camera_id: str,
        purpose: Purpose,
        source_path: str,
    ) -> ReplayHandle:
        """Spawn and acquire the replay camera for a real camera.

        Raises :class:`ReplayBusy` if a replay camera is already held for
        the same real camera, :class:`ValueError` if the real camera isn't
        configured under ffmpeg, :class:`RuntimeError` if the pipeline
        fails to reach LOADED.
        """
        lock = self._get_per_camera_lock(real_camera_id)
        with lock:
            with self._global_lock:
                existing = self._holders.get(real_camera_id)
                if existing is not None:
                    raise ReplayBusy(real_camera_id, existing)

            replay_id = replay_camera_id(real_camera_id)
            LOGGER.info(
                "acquire: replay camera %s for %s (purpose=%s, source=%s)",
                replay_id,
                real_camera_id,
                purpose,
                source_path,
            )
            self._spawn(real_camera_id, replay_id, purpose, source_path)

            token = str(uuid.uuid4())
            holder = ReplayHolder(
                token=token,
                real_camera_id=real_camera_id,
                replay_camera_id=replay_id,
                purpose=purpose,
                source_path=source_path,
                acquired_at=datetime.datetime.now(tz=datetime.timezone.utc),
            )
            # Auto-release on natural EOF: the ffmpeg EOF watcher in the
            # playback camera pipeline calls stop_camera() which dispatches
            # EVENT_CAMERA_STOPPED. We release from that listener so the
            # manager's holder state reflects reality without the caller
            # needing to poll.
            holder.unsubscribe_stopped = self._vis.listen_event(
                EVENT_CAMERA_STOPPED.format(camera_identifier=replay_id),
                lambda *_: self._on_camera_stopped(token),
            )
            with self._global_lock:
                self._holders[real_camera_id] = holder
            return ReplayHandle(token=token, replay_camera_id=replay_id)

    def _on_camera_stopped(self, token: str) -> None:
        """Event-listener callback for EVENT_CAMERA_STOPPED."""
        try:
            self.release(token)
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("auto-release failed for token %s", token)

    def release(self, token: str) -> None:
        """Release a replay camera by token. Idempotent."""
        holder: ReplayHolder | None = None
        real_id: str | None = None
        with self._global_lock:
            for rid, h in self._holders.items():
                if h.token == token:
                    holder = h
                    real_id = rid
                    break
        if holder is None or real_id is None:
            LOGGER.debug("release: no holder with token %s", token)
            return

        lock = self._get_per_camera_lock(real_id)
        with lock:
            with self._global_lock:
                current = self._holders.get(real_id)
                if current is None or current.token != token:
                    return
                # Remove from holders and unsubscribe stop-listener *before*
                # teardown. Teardown will re-dispatch EVENT_CAMERA_STOPPED
                # when unload_domain calls AbstractCamera.unload, and we
                # don't want the listener to re-enter release.
                self._holders.pop(real_id, None)
            if holder.unsubscribe_stopped is not None:
                try:
                    holder.unsubscribe_stopped()
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "failed to unsubscribe stop-listener for %s",
                        holder.replay_camera_id,
                    )
            LOGGER.info(
                "release: replay camera %s for %s (purpose=%s)",
                holder.replay_camera_id,
                real_id,
                holder.purpose,
            )
            self._teardown(holder.replay_camera_id)

    def get_holder(self, real_camera_id: str) -> ReplayHolder | None:
        """Return the current holder for a real camera, if any."""
        with self._global_lock:
            return self._holders.get(real_camera_id)

    def list_holders(self) -> list[ReplayHolder]:
        """Return all current holders (snapshot)."""
        with self._global_lock:
            return list(self._holders.values())

    # --------------------------------------------------------------- internals

    def _get_per_camera_lock(self, real_camera_id: str) -> threading.Lock:
        with self._global_lock:
            lock = self._per_camera_locks.get(real_camera_id)
            if lock is None:
                lock = threading.Lock()
                self._per_camera_locks[real_camera_id] = lock
            return lock

    def _spawn(
        self,
        real_camera_id: str,
        replay_id: str,
        purpose: Purpose,
        source_path: str,
    ) -> None:
        cameras = self._config.setdefault(FFMPEG_COMPONENT, {}).setdefault(
            CONFIG_CAMERA, {}
        )
        target = cameras.get(real_camera_id)
        if target is None:
            raise ValueError(
                f"replay camera source {real_camera_id!r} is not configured "
                f"under ffmpeg"
            )
        if replay_id in cameras:
            # Leftover from a prior crashed spawn — scrub it.
            self._teardown(replay_id)

        replay_config = self._build_replay_config(target, purpose, source_path)
        cameras[replay_id] = replay_config

        try:
            self._inject_detection_configs(real_camera_id, replay_id)
            self._inject_nvr_config(replay_id)

            # Queue the replay camera + its detection + nvr domains for setup.
            setup_domain(
                self._vis,
                FFMPEG_COMPONENT,
                CAMERA_DOMAIN,
                self._config[FFMPEG_COMPONENT],
                identifier=replay_id,
            )
            for domain_name in _DETECTION_DOMAINS:
                parent_entry = self._vis.domain_registry.get(
                    domain_name, real_camera_id
                )
                if not parent_entry or parent_entry.state != DomainState.LOADED:
                    continue
                setup_domain(
                    self._vis,
                    parent_entry.component_name,
                    domain_name,
                    parent_entry.config,
                    identifier=replay_id,
                    require_domains=[
                        RequireDomain(
                            domain=CAMERA_DOMAIN, identifier=replay_id
                        )
                    ],
                )
            setup_domain(
                self._vis,
                NVR_COMPONENT,
                NVR_DOMAIN,
                {replay_id: {}},
                identifier=replay_id,
                require_domains=[
                    RequireDomain(domain=CAMERA_DOMAIN, identifier=replay_id)
                ],
                optional_domains=nvr_optional_domains(replay_id),
            )

            setup_domains(self._vis)

            camera_entry = self._vis.domain_registry.get(CAMERA_DOMAIN, replay_id)
            if camera_entry is None or camera_entry.state != DomainState.LOADED:
                state = camera_entry.state if camera_entry else "MISSING"
                raise RuntimeError(
                    f"replay camera {replay_id!r} failed to reach LOADED "
                    f"(state={state})"
                )

            camera: "AbstractCamera" = camera_entry.instance
            camera.start_camera()
        except Exception:
            LOGGER.exception(
                "spawn failed for replay camera %s; rolling back", replay_id
            )
            self._teardown(replay_id)
            raise

    def _teardown(self, replay_id: str) -> None:
        """Unload every registered domain for this replay id and scrub config.

        Safe to call on a partially-spawned replay camera; each step
        tolerates missing entries.
        """
        camera_entry = self._vis.domain_registry.get(CAMERA_DOMAIN, replay_id)
        if camera_entry is not None:
            unload_order = get_unload_order(
                self._vis, CAMERA_DOMAIN, replay_id
            )
            for entry in unload_order:
                try:
                    unload_domain(self._vis, entry.domain, entry.identifier)
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "unload_domain failed for %s:%s",
                        entry.domain,
                        entry.identifier,
                    )

        # Scrub config so a later acquire rebuilds cleanly.
        cameras = self._config.get(FFMPEG_COMPONENT, {}).get(CONFIG_CAMERA, {})
        cameras.pop(replay_id, None)
        self._remove_detection_configs(replay_id)
        self._remove_nvr_config(replay_id)

    @staticmethod
    def _build_replay_config(
        target: dict[str, Any],
        purpose: Purpose,
        source_path: str,
    ) -> dict[str, Any]:
        cloned = copy.deepcopy(target)
        # file_source cameras don't use a substream — pruning matches the
        # test_runner's existing _clone_target_camera convention.
        cloned.pop("substream", None)
        cloned.pop("username", None)
        cloned.pop("password", None)
        cloned["host"] = "127.0.0.1"
        cloned["port"] = 554
        cloned["path"] = "/"
        cloned["file_source"] = source_path
        # Always mark as playback so the EOF watcher and swap_playback_source
        # semantics kick in — test purpose additionally stamps test_mode so
        # detections produced by the run are tagged test=True in the DB and
        # don't pollute live events.
        cloned["playback_mode"] = True
        cloned["test_mode"] = purpose == "test"
        return cloned

    def _inject_detection_configs(
        self, real_camera_id: str, replay_id: str
    ) -> None:
        for domain_name in _DETECTION_DOMAINS:
            parent_entry = self._vis.domain_registry.get(
                domain_name, real_camera_id
            )
            if not parent_entry or parent_entry.state != DomainState.LOADED:
                continue
            domain_config = parent_entry.config.get(domain_name, {})
            cameras = domain_config.setdefault("cameras", {})
            if real_camera_id in cameras and replay_id not in cameras:
                cameras[replay_id] = copy.deepcopy(cameras[real_camera_id])

    def _remove_detection_configs(self, replay_id: str) -> None:
        # Parent config lives under the real camera's detection domain entry;
        # the replay id's own domain entry is gone by now (unloaded above).
        real_camera_id = replay_id.removeprefix("replay_")
        for domain_name in _DETECTION_DOMAINS:
            parent_entry = self._vis.domain_registry.get(
                domain_name, real_camera_id
            )
            if parent_entry is None:
                continue
            cameras = parent_entry.config.get(domain_name, {}).get(
                "cameras", {}
            )
            cameras.pop(replay_id, None)

    def _inject_nvr_config(self, replay_id: str) -> None:
        nvr_root = self._config.setdefault(NVR_COMPONENT, {}).setdefault(
            NVR_COMPONENT, {}
        )
        if isinstance(nvr_root, dict) and replay_id not in nvr_root:
            nvr_root[replay_id] = None

    def _remove_nvr_config(self, replay_id: str) -> None:
        nvr_root = self._config.get(NVR_COMPONENT, {}).get(NVR_COMPONENT, {})
        if isinstance(nvr_root, dict):
            nvr_root.pop(replay_id, None)


def get_or_create_manager(
    vis: "Viseron", config: dict[str, Any] | None = None
) -> ReplayCameraManager:
    """Return the (singleton) replay-camera manager for this Viseron instance.

    The first caller must pass the live config dict so the manager can
    mutate ffmpeg/detection/nvr sub-configs on spawn/teardown. Subsequent
    calls ignore ``config`` and return the existing manager.
    """
    manager = vis.data.get(_MANAGER_KEY)
    if manager is not None:
        return manager
    if config is None:
        raise RuntimeError(
            "ReplayCameraManager has not been initialised yet — the first "
            "get_or_create_manager() call must pass the live config dict."
        )
    manager = ReplayCameraManager(vis, config)
    vis.data[_MANAGER_KEY] = manager
    return manager


def get_manager(vis: "Viseron") -> ReplayCameraManager | None:
    """Return the replay-camera manager if one has been initialised, else None."""
    return vis.data.get(_MANAGER_KEY)
