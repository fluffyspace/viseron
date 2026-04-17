"""Storage tier-check subprocess entry point.

Runs as ``python3 -u subprocess_workers/storage_tier_subprocess.py``.

Deliberately imports nothing from the ``viseron`` package. The work is
numpy + simple SQL against the ``files`` and ``recordings`` tables,
neither of which needs opencv, scipy, alembic, watchdog, or the domain
registry. Keeping the import graph narrow shrinks the subprocess's
resident memory from ~200 MiB to ~100 MiB.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import datetime
import gc
import logging
import multiprocessing as mp
import os
import shutil
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable
from queue import Empty, Queue
from typing import Any

import numpy as np
import psutil
import setproctitle
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    select,
)

from manager import connect
from subprocess_workers.storage_tier_compute import (
    FILES_COMPUTE_DTYPE,
    FILES_RESULT_DTYPE,
    RECORDINGS_DTYPE,
    RECORDINGS_RESULT_DTYPE,
    get_files_to_move,
    get_recordings_to_move,
)
from subprocess_workers.storage_tier_messages import (
    DataItem,
    DataItemDeleteFile,
    DataItemMoveFile,
)

LOGGER = logging.getLogger(__name__)

# Must match viseron.const.CAMERA_SEGMENT_DURATION. Copied (not
# imported) so this subprocess never touches the viseron package.
CAMERA_SEGMENT_DURATION = 5

ENV_PROFILE_MEMORY = "VISERON_PROFILE_MEMORY"


# SQLAlchemy Core Table definitions for the two tables this subprocess
# reads/writes. Kept deliberately minimal: only the columns actually
# queried. Schema changes on the main ORM side that add or remove
# columns we touch must be mirrored here.
_METADATA = MetaData()

FILES_TABLE = Table(
    "files",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("tier_id", Integer),
    Column("tier_path", String),
    Column("camera_identifier", String),
    Column("category", String),
    Column("subcategory", String),
    Column("path", String),
    Column("size", Integer),
    Column("orig_ctime", DateTime),
)

RECORDINGS_TABLE = Table(
    "recordings",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("camera_identifier", String),
    Column("start_time", DateTime),
    Column("end_time", DateTime),
    Column("adjusted_start_time", DateTime),
    Column("created_at", DateTime),
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


def _load_malloc_trim() -> Callable[[], None] | None:
    """Resolve glibc's malloc_trim so we can return freed heap to the OS.

    check_tier loads numpy arrays of file metadata that can briefly
    allocate tens of MiB. Python frees them, but glibc keeps the pages
    in per-thread arenas. Calling malloc_trim(0) after each cycle hands
    them back so the subprocess RSS actually shrinks.
    """
    libname = ctypes.util.find_library("c")
    if not libname:
        return None
    try:
        libc = ctypes.CDLL(libname)
    except OSError:
        return None
    trim = getattr(libc, "malloc_trim", None)
    if trim is None:
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return lambda: trim(0)


def _log_memory_summary() -> None:
    """Emit RSS and (if enabled) a tracemalloc top-N breakdown.

    Written line-by-line because the main process's LogPipe splits on
    newlines and strips leading whitespace before picking a level — a
    single multi-line message would get mangled.
    """
    try:
        rss_mib = psutil.Process().memory_info().rss / (1024 * 1024)
        LOGGER.info("storage subprocess memory summary")
        LOGGER.info("  RSS: %.1f MiB", rss_mib)
        if tracemalloc.is_tracing():
            snap = tracemalloc.take_snapshot()
            stats = snap.statistics("filename")[:10]
            for stat in stats:
                fname = str(stat.traceback).split("/")[-1][:60]
                LOGGER.info(
                    "  %-60s %6.2f MiB (%d allocs)",
                    fname,
                    stat.size / (1024 * 1024),
                    stat.count,
                )
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("failed to log memory summary")


def setup_logger(loglevel: str) -> None:
    """Log to stdout without formatting — the parent process formats."""
    root = logging.getLogger()
    root.setLevel(loglevel)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(loglevel)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(handler)


def get_parser() -> argparse.ArgumentParser:
    """Parser for the subprocess entry point CLI."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-port", required=True)
    parser.add_argument("--manager-authkey", required=True)
    parser.add_argument("--cpulimit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--loglevel",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def initializer(cpulimit: int | None) -> None:
    """Lower priority and optionally cap CPU via ``cpulimit``."""
    pid = mp.current_process().pid
    if pid:
        ps = psutil.Process(pid)
        ps.nice(20)
    if pid and cpulimit is not None:
        command = ["cpulimit", "-l", str(cpulimit), "-p", str(pid), "-z", "-q"]
        LOGGER.debug("Running command: %s", command)
        # Keep the cpulimit child alive as long as this process exists;
        # no watchdog needed — if it dies, behaviour just falls back to
        # unthrottled CPU which is the default everywhere else.
        import subprocess as sp  # pylint: disable=import-outside-toplevel

        sp.Popen(command, close_fds=True)
    setproctitle.setproctitle(f"viseron_storage_subprocess_{pid}")


class Worker:
    """Execute tier-check and file-move commands using SQLAlchemy Core."""

    def __init__(self, trim: Callable[[], None] | None = None) -> None:
        database_url = os.getenv(
            "POSTGRES_DATABASE_URL", "postgresql://postgres@localhost/viseron"
        )
        self._engine = create_engine(
            database_url,
            connect_args={"options": "-c timezone=UTC"},
            pool_size=1,
            max_overflow=1,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        self._trim = trim
        self._last_call: dict[str, float] = {}
        self._check_locks: dict[str, threading.Lock] = {}
        self._checks_in_progress: dict[str, bool] = {}

    def _load_tier(self, item: DataItem) -> np.ndarray:
        with self._engine.connect() as conn:
            stmt = select(
                FILES_TABLE.c.id, FILES_TABLE.c.size, FILES_TABLE.c.orig_ctime
            ).where(
                FILES_TABLE.c.camera_identifier == item.camera_identifier,
                FILES_TABLE.c.tier_id == item.tier_id,
                FILES_TABLE.c.category == item.category,
                FILES_TABLE.c.subcategory.in_(item.subcategories),
            )
            rows = conn.execute(stmt).fetchall()
            data = [
                (row.id, row.size, int(row.orig_ctime.timestamp()))
                for row in rows
            ]
        return np.array(data, dtype=FILES_COMPUTE_DTYPE)

    def _load_recordings(self, item: DataItem) -> np.ndarray:
        with self._engine.connect() as conn:
            stmt = select(
                RECORDINGS_TABLE.c.id,
                RECORDINGS_TABLE.c.start_time,
                RECORDINGS_TABLE.c.end_time,
                RECORDINGS_TABLE.c.adjusted_start_time,
                RECORDINGS_TABLE.c.created_at,
            ).where(RECORDINGS_TABLE.c.camera_identifier == item.camera_identifier)
            rows = conn.execute(stmt).fetchall()
            now_ts = _utcnow().timestamp()
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

    def _resolve_file_paths(self, file_ids: np.ndarray) -> np.ndarray:
        """Fetch paths for file IDs eligible to move."""
        ids_list = file_ids.tolist()
        with self._engine.connect() as conn:
            stmt = select(
                FILES_TABLE.c.id, FILES_TABLE.c.path, FILES_TABLE.c.tier_path
            ).where(FILES_TABLE.c.id.in_(ids_list))
            rows = conn.execute(stmt).fetchall()
        path_map = {row.id: (row.path, row.tier_path) for row in rows}
        data = [
            (fid, path_map[fid][0], path_map[fid][1])
            for fid in ids_list
            if fid in path_map
        ]
        return np.array(data, dtype=FILES_RESULT_DTYPE)

    def _resolve_recording_file_paths(self, ids_to_move: np.ndarray) -> np.ndarray:
        """Fetch paths for recording-file pairs, filtering to .m4s only."""
        file_ids_list = list({int(x) for x in ids_to_move["id"]})
        with self._engine.connect() as conn:
            stmt = select(
                FILES_TABLE.c.id, FILES_TABLE.c.path, FILES_TABLE.c.tier_path
            ).where(
                FILES_TABLE.c.id.in_(file_ids_list),
                FILES_TABLE.c.path.like("%.m4s"),
            )
            rows = conn.execute(stmt).fetchall()
        path_map = {row.id: (row.path, row.tier_path) for row in rows}
        data = [
            (
                int(rec["recording_id"]),
                int(rec["id"]),
                path_map[rec["id"]][0],
                path_map[rec["id"]][1],
            )
            for rec in ids_to_move
            if int(rec["id"]) in path_map
        ]
        if not data:
            return np.empty(0, dtype=RECORDINGS_RESULT_DTYPE)
        return np.array(data, dtype=RECORDINGS_RESULT_DTYPE)

    def _check_tier_files(self, item: DataItem) -> np.ndarray:
        data = self._load_tier(item)
        now = _utcnow()

        if item.min_age:
            min_age_ts = (now - item.min_age).timestamp()
        else:
            min_age_ts = (
                now - datetime.timedelta(seconds=CAMERA_SEGMENT_DURATION * 2)
            ).timestamp()

        max_age_ts = (now - item.max_age).timestamp() if item.max_age else 0

        ids_to_move = get_files_to_move(
            data,
            item.max_bytes,
            min_age_ts,
            item.min_bytes,
            max_age_ts,
            item.drain,
        )
        if ids_to_move.size == 0:
            return np.empty(0, dtype=FILES_RESULT_DTYPE)
        return self._resolve_file_paths(ids_to_move)

    def _check_tier_recordings(self, item: DataItem) -> np.ndarray:
        if (
            item.events_max_bytes is None
            or item.events_min_bytes is None
            or item.events_min_age is None
            or item.events_max_age is None
        ):
            raise ValueError(
                "events_max_bytes, events_min_bytes, events_min_age, and "
                "events_max_age must be set for check_tier_recordings"
            )

        files_data = self._load_tier(item)
        recordings_data = self._load_recordings(item)
        now = _utcnow()

        if item.events_min_age:
            min_age_ts = (now - item.events_min_age).timestamp()
        else:
            min_age_ts = (
                now - datetime.timedelta(seconds=CAMERA_SEGMENT_DURATION * 2)
            ).timestamp()

        max_age_ts = (
            (now - item.events_max_age).timestamp() if item.events_max_age else 0
        )

        # Ignore files younger than 5× segment duration — they may still
        # be referenced by an active HLS playlist.
        file_min_age_ts = (
            now - datetime.timedelta(seconds=CAMERA_SEGMENT_DURATION * 5)
        ).timestamp()

        ids_to_move = get_recordings_to_move(
            recordings_data,
            files_data,
            CAMERA_SEGMENT_DURATION,
            item.events_max_bytes,
            min_age_ts,
            item.events_min_bytes,
            max_age_ts,
            file_min_age_ts,
            drain=item.drain,
        )
        if ids_to_move.size == 0:
            return np.empty(0, dtype=RECORDINGS_RESULT_DTYPE)
        return self._resolve_recording_file_paths(ids_to_move)

    def _check_tier(self, item: DataItem) -> None:
        files = np.empty(0, dtype=FILES_RESULT_DTYPE)
        if item.files_enabled:
            files = self._check_tier_files(item)

        recordings = np.empty(0, dtype=RECORDINGS_RESULT_DTYPE)
        if item.events_enabled:
            recordings = self._check_tier_recordings(item)

        if item.events_enabled and not item.files_enabled:
            item.data = recordings
        elif item.files_enabled and not item.events_enabled:
            item.data = files
        elif item.events_enabled and item.files_enabled:
            if files.size > 0 and recordings.size > 0:
                overlapping_ids = np.intersect1d(files["id"], recordings["id"])
                if overlapping_ids.size > 0:
                    item.data = recordings[np.isin(recordings["id"], overlapping_ids)]
                else:
                    item.data = np.empty(0, dtype=recordings.dtype)
            else:
                item.data = np.empty(
                    0,
                    dtype=recordings.dtype if recordings.size > 0 else files.dtype,
                )
        else:
            item.data = np.empty(0, dtype=FILES_RESULT_DTYPE)

    def check_tier(self, item: DataItem) -> None:
        """Throttled entry point that runs at most one check per camera."""
        if item.camera_identifier not in self._check_locks:
            self._check_locks[item.camera_identifier] = threading.Lock()
        if item.throttle_key not in self._last_call:
            self._last_call[item.throttle_key] = 0
        if item.camera_identifier not in self._checks_in_progress:
            self._checks_in_progress[item.camera_identifier] = False

        with self._check_locks[item.camera_identifier]:
            if self._checks_in_progress[item.camera_identifier]:
                return
            now = _utcnow().timestamp()
            throttle = item.throttle_period.total_seconds()
            last_call = self._last_call[item.throttle_key]
            if throttle > 0 and (now - last_call) < throttle:
                item.data = None
                return
            self._checks_in_progress[item.camera_identifier] = True

        try:
            self._check_tier(item)
        finally:
            with self._check_locks[item.camera_identifier]:
                self._last_call[item.throttle_key] = _utcnow().timestamp()
                self._checks_in_progress[item.camera_identifier] = False
            # Each check allocates numpy arrays over the full per-camera
            # files set (~220K rows on this install). Python frees the
            # refs but glibc keeps the pages — run gc then malloc_trim so
            # RSS doesn't creep up indefinitely.
            gc.collect()
            if self._trim is not None:
                try:
                    self._trim()
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception("malloc_trim failed")

    def move_file(self, item: DataItemMoveFile) -> None:
        """Copy + unlink; falls back to DB cleanup if source is missing."""
        try:
            os.makedirs(os.path.dirname(item.dst), exist_ok=True)
            shutil.copy(item.src, item.dst)
            os.remove(item.src)
        except FileNotFoundError as error:
            self._delete_db_row(item.src)
            raise error
        except OSError as error:
            self._delete_db_row(item.src)
            try:
                os.remove(item.src)
            except FileNotFoundError:
                pass
            raise error

    def delete_file(self, item: DataItemDeleteFile) -> None:
        """Delete DB row and unlink the file."""
        self._delete_db_row(item.src)
        try:
            os.remove(item.src)
        except FileNotFoundError as error:
            raise error

    def _delete_db_row(self, path: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(FILES_TABLE).where(FILES_TABLE.c.path == path))

    def work_input(
        self, item: DataItem | DataItemMoveFile | DataItemDeleteFile
    ) -> None:
        """Dispatch input item to the right handler."""
        try:
            if item.cmd == "check_tier":
                self.check_tier(item)  # type: ignore[arg-type]
            elif item.cmd == "move_file":
                self.move_file(item)  # type: ignore[arg-type]
            elif item.cmd == "delete_file":
                self.delete_file(item)  # type: ignore[arg-type]
        except Exception as error:  # pylint: disable=broad-except
            LOGGER.error("Error processing command: %s, error: %s", item, error)
            item.error = str(error)


def worker_task_files(
    worker: Worker,
    file_queue: Queue[DataItemDeleteFile | DataItemMoveFile],
    output_queue: Queue[Any],
) -> None:
    """Thread that processes only file operations."""
    while True:
        try:
            job = file_queue.get(timeout=1)
            worker.work_input(job)
            output_queue.put(job)
        except Empty:
            continue
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("Error in file worker thread")


def worker_task_mixed(
    worker: Worker,
    check_queue: Queue[DataItem],
    file_queue: Queue[DataItemDeleteFile | DataItemMoveFile],
    output_queue: Queue[Any],
    name: str,
) -> None:
    """Thread that prioritises file operations but also runs check_tier.

    File moves must not be starved by slow check_tier jobs, so we poll
    the file queue non-blocking first before falling back to check.
    """
    while True:
        try:
            try:
                job: Any = file_queue.get_nowait()
            except Empty:
                job = check_queue.get(timeout=1)
            worker.work_input(job)
            output_queue.put(job)
        except Empty:
            continue
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("Error in mixed worker thread %s", name)


def dispatcher_task(
    process_queue: Queue[Any],
    check_queue: Queue[DataItem],
    file_queue: Queue[DataItemDeleteFile | DataItemMoveFile],
) -> None:
    """Route incoming jobs to the check or file queue."""
    while True:
        try:
            job = process_queue.get(timeout=1)
        except Empty:
            continue
        try:
            if job.cmd == "check_tier":
                check_queue.put(job)
            elif job.cmd in ("move_file", "delete_file"):
                file_queue.put(job)
            else:
                LOGGER.debug("Unknown command %s", job.cmd)
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("Dispatcher error routing job")


def main() -> None:
    """Run the storage-tier check worker until killed."""
    parser = get_parser()
    args = parser.parse_args()
    setup_logger(args.loglevel)

    if os.getenv(ENV_PROFILE_MEMORY) == "true" and not tracemalloc.is_tracing():
        tracemalloc.start()

    process_queue, output_queue = connect(
        "127.0.0.1", int(args.manager_port), args.manager_authkey
    )

    initializer(cpulimit=args.cpulimit)

    trim = _load_malloc_trim()
    if trim is None:
        LOGGER.debug("malloc_trim unavailable on this platform")
    worker = Worker(trim=trim)

    logging.getLogger("apscheduler.scheduler").setLevel(logging.ERROR)
    logging.getLogger("apscheduler.executors").setLevel(logging.ERROR)
    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    scheduler.start()

    scheduler.add_job(
        _log_memory_summary,
        "date",
        run_date=_utcnow() + datetime.timedelta(seconds=30),
    )
    scheduler.add_job(_log_memory_summary, "interval", minutes=10)

    check_queue: Queue[DataItem] = Queue()
    file_queue: Queue[DataItemDeleteFile | DataItemMoveFile] = Queue()

    threading.Thread(
        name="storage_subprocess.dispatcher",
        target=dispatcher_task,
        args=(process_queue, check_queue, file_queue),
        daemon=True,
    ).start()

    for i in range(args.workers):
        threading.Thread(
            name=f"storage_subprocess.mixed_worker.{i}",
            target=worker_task_mixed,
            args=(worker, check_queue, file_queue, output_queue, f"mixed_worker.{i}"),
            daemon=True,
        ).start()

    threading.Thread(
        name="storage_subprocess.file_worker",
        target=worker_task_files,
        args=(worker, file_queue, output_queue),
        daemon=True,
    ).start()

    while True:
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOGGER.debug("Storage tier check subprocess interrupted")
        sys.exit(0)
