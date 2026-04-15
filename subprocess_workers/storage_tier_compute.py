"""Pure-numpy helpers for deciding which files move between tiers.

Split out of the original ``viseron.components.storage.check_tier`` so
that both the subprocess entry point and the test suite can pull in the
tier arithmetic without importing the rest of the storage component
(alembic, tier handlers, viseron.domains.camera, opencv, …). The
functions in here touch numpy only — no SQLAlchemy, no viseron.
"""

from __future__ import annotations

import numpy as np

# Lightweight dtypes used while computing which files need to move.
# Paths are resolved separately for the small subset actually moving, so
# the "compute" arrays never carry string columns.
FILES_COMPUTE_DTYPE = np.dtype(
    [
        ("id", np.int64),
        ("size", np.int64),
        ("orig_ctime", np.int64),
    ]
)

RECORDINGS_DTYPE = np.dtype(
    [
        ("id", np.int64),
        ("start_time", np.int64),
        ("adjusted_start_time", np.int64),
        ("end_time", np.int64),
        ("created_at", np.int64),
    ]
)

RECORDINGS_FILES_COMPUTE_DTYPE = np.dtype(
    [
        ("recording_id", np.int64),
        ("id", np.int64),
        ("size", np.int64),
        ("orig_ctime", np.int64),
    ]
)

# Result dtypes — only used for the small subset of files/recordings
# that the caller will actually act on.
FILES_RESULT_DTYPE = np.dtype(
    [
        ("id", np.int64),
        ("path", "U256"),
        ("tier_path", "U256"),
    ]
)

RECORDINGS_RESULT_DTYPE = np.dtype(
    [
        ("recording_id", np.int64),
        ("id", np.int64),
        ("path", "U256"),
        ("tier_path", "U256"),
    ]
)

# Aliases kept for backward compatibility with existing test imports.
FILES_DTYPE = FILES_RESULT_DTYPE
RECORDINGS_FILES_DTYPE = RECORDINGS_RESULT_DTYPE


def get_files_to_move(
    data: np.ndarray,
    max_bytes: int,
    min_age_timestamp: float,
    min_bytes: int,
    max_age_timestamp: float,
    drain: bool,
) -> np.ndarray:
    """Return the IDs of files eligible to move to the next tier.

    The array is sorted newest-first, then ``np.cumsum`` builds a
    running total of sizes. Rows whose cumulative size exceeds
    ``max_bytes`` or whose ctime is older than ``max_age_timestamp`` are
    selected. Returns a 1D array of file IDs in chronological order.
    """
    sorted_indices = np.argsort(data["orig_ctime"])
    data = data[sorted_indices][::-1]

    cumulative_size = np.cumsum(data["size"])

    bytes_indices_to_move = np.empty(0, dtype=np.int64)
    if max_bytes > 0:
        bytes_indices_to_move = np.where(
            (cumulative_size >= max_bytes) & (data["orig_ctime"] <= min_age_timestamp)
        )[0]

    age_indices_to_move = np.empty(0, dtype=np.int64)
    if max_age_timestamp > 0:
        age_indices_to_move = np.where(
            (data["orig_ctime"] < max_age_timestamp) & (cumulative_size >= min_bytes)
        )[0]

    rows_to_move = np.empty(0, dtype=data.dtype)
    if drain and (bytes_indices_to_move.size > 0 or age_indices_to_move.size > 0):
        rows_to_move = data
    else:
        indices_to_move = np.unique(
            np.concatenate((bytes_indices_to_move, age_indices_to_move))
        )
        if indices_to_move.size > 0:
            rows_to_move = data[indices_to_move]

    return rows_to_move["id"][::-1]


def get_recordings_to_move(
    recordings_data: np.ndarray,
    files_data: np.ndarray,
    segment_length: int,
    max_bytes: int,
    min_age_timestamp: float,
    min_bytes: int,
    max_age_timestamp: float,
    file_min_age_timestamp: float,
    drain: bool,
) -> np.ndarray:
    """Return (recording_id, id) pairs of files eligible to move.

    Recordings are the grouping unit; a file moves when its recording
    does. Files that don't belong to any recording are grouped under
    recording_id ``-1`` and always considered. Paths are resolved by
    the caller for the subset returned.
    """
    sorted_indices_recordings = np.argsort(recordings_data["adjusted_start_time"])[::-1]
    recordings_data = recordings_data[sorted_indices_recordings]

    files_data.sort(order="orig_ctime")

    recordings_size = np.zeros(len(recordings_data), dtype=np.int64)
    associated_files_data_list = []

    for i, recording in enumerate(recordings_data):
        if files_data.size > 0:
            start_time_search = recording["adjusted_start_time"]
            end_time_search = recording["end_time"] + segment_length
            start_idx = np.searchsorted(
                files_data["orig_ctime"], start_time_search, side="left"
            )
            end_idx = np.searchsorted(
                files_data["orig_ctime"], end_time_search, side="right"
            )
            relevant_files_for_recording = files_data[start_idx:end_idx]
        else:
            relevant_files_for_recording = np.empty(0, dtype=files_data.dtype)

        current_recording_total_size = 0
        if relevant_files_for_recording.size > 0:
            for file_row in relevant_files_for_recording:
                associated_files_data_list.append(
                    (
                        recording["id"],
                        file_row["id"],
                        file_row["size"],
                        file_row["orig_ctime"],
                    )
                )
                current_recording_total_size += file_row["size"]
        recordings_size[i] = current_recording_total_size

    associated_file_ids_set = {tup[1] for tup in associated_files_data_list}

    other_files_data_list = []
    if files_data.size > 0:
        for file_row in files_data:
            if file_row["id"] not in associated_file_ids_set:
                other_files_data_list.append(
                    (
                        -1,
                        file_row["id"],
                        file_row["size"],
                        file_row["orig_ctime"],
                    )
                )

    combined_files_list = associated_files_data_list + other_files_data_list

    if not combined_files_list:
        return np.empty(0, dtype=RECORDINGS_FILES_COMPUTE_DTYPE)

    recordings_files = np.array(combined_files_list, dtype=RECORDINGS_FILES_COMPUTE_DTYPE)
    recordings_files.sort(order=["id", "orig_ctime"])

    recording_cumulative_sizes = np.cumsum(recordings_size)

    bytes_indices_to_move_recordings = np.empty(0, dtype=np.int64)
    if max_bytes > 0:
        bytes_indices_to_move_recordings = np.where(
            (recording_cumulative_sizes >= max_bytes)
            & (recordings_data["created_at"] <= min_age_timestamp)
        )[0]

    age_indices_to_move_recordings = np.empty(0, dtype=np.int64)
    if max_age_timestamp > 0:
        age_indices_to_move_recordings = np.where(
            (recordings_data["created_at"] < max_age_timestamp)
            & (recording_cumulative_sizes >= min_bytes)
        )[0]

    files_to_move_np = np.empty(0, dtype=RECORDINGS_FILES_COMPUTE_DTYPE)
    if drain and (
        bytes_indices_to_move_recordings.size > 0
        or age_indices_to_move_recordings.size > 0
    ):
        files_to_move_np = recordings_files
    else:
        recording_indices_to_move = np.unique(
            np.concatenate(
                (bytes_indices_to_move_recordings, age_indices_to_move_recordings)
            )
        )

        files_to_move_list = []

        if recording_indices_to_move.size > 0:
            moved_recording_ids = recordings_data[recording_indices_to_move]["id"]
            for r_file in recordings_files:
                if r_file["recording_id"] in moved_recording_ids:
                    if r_file["orig_ctime"] <= file_min_age_timestamp:
                        files_to_move_list.append(r_file)
                elif r_file["recording_id"] == -1:
                    if r_file["orig_ctime"] <= file_min_age_timestamp:
                        files_to_move_list.append(r_file)
        elif files_data.size > 0:
            for r_file in recordings_files:
                if (
                    r_file["recording_id"] == -1
                    and r_file["orig_ctime"] <= file_min_age_timestamp
                ):
                    files_to_move_list.append(r_file)

        if not files_to_move_list:
            return np.empty(0, dtype=RECORDINGS_FILES_COMPUTE_DTYPE)

        files_to_move_np = np.array(
            files_to_move_list, dtype=RECORDINGS_FILES_COMPUTE_DTYPE
        )

    if files_to_move_np.size > 0:
        _, unique_indices = np.unique(files_to_move_np["id"], return_index=True)
        files_to_move_np = files_to_move_np[unique_indices]
    else:
        return np.empty(0, dtype=RECORDINGS_FILES_COMPUTE_DTYPE)

    return files_to_move_np[["recording_id", "id"]]
