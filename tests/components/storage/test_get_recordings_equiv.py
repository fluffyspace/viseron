"""Differential equivalence harness for get_recordings_to_move.

Pure numpy, no DB. Imports both the vectorized ``get_recordings_to_move``
and the retained ``_get_recordings_to_move_legacy`` from
``subprocess_workers.storage_tier_compute`` and asserts byte-for-byte
identical output across thousands of randomized cases.

This is the equivalence proof: the real pytest suite cannot run here
(no PostgreSQL, no ``viseron`` package installed in the venv), so the
retained legacy function is the oracle.

Run:  python3 -m pytest tests/components/storage/test_get_recordings_equiv.py\n      (or: python3 tests/components/storage/test_get_recordings_equiv.py)
"""

from __future__ import annotations

import sys

import numpy as np

from subprocess_workers.storage_tier_compute import (
    FILES_COMPUTE_DTYPE,
    RECORDINGS_DTYPE,
    _get_recordings_to_move_legacy,
    get_recordings_to_move,
)

SEED = 1234567
N_CASES = 6000
rng = np.random.default_rng(SEED)


def _make_files(n: int, t_lo: int, t_hi: int, tie_pool: int) -> np.ndarray:
    arr = np.empty(n, dtype=FILES_COMPUTE_DTYPE)
    if n == 0:
        return arr
    # Mostly unique ids, but deliberately inject collisions so the
    # final np.unique(...,return_index=True) first-occurrence dedupe
    # path is exercised hard.
    if n > 3 and rng.random() < 0.3:
        arr["id"] = rng.integers(1, max(2, n // 2 + 1), size=n).astype(
            np.int64
        )
    else:
        arr["id"] = rng.permutation(np.arange(1, n + 1)) + rng.integers(0, 5)
    arr["size"] = rng.integers(0, 5000, size=n).astype(np.int64)
    # tie_pool small -> many duplicate orig_ctime values (ties).
    arr["orig_ctime"] = rng.integers(t_lo, t_lo + tie_pool + 1, size=n).astype(
        np.int64
    )
    if t_hi > t_lo + tie_pool:
        # mix in some spread-out times too
        spread = rng.integers(t_lo, t_hi, size=n).astype(np.int64)
        mask = rng.random(n) < 0.5
        arr["orig_ctime"][mask] = spread[mask]
    return arr


def _make_recordings(
    n: int, t_lo: int, t_hi: int, tie_pool: int
) -> np.ndarray:
    arr = np.empty(n, dtype=RECORDINGS_DTYPE)
    if n == 0:
        return arr
    arr["id"] = rng.permutation(np.arange(1, n + 1))
    ast = rng.integers(t_lo, t_lo + tie_pool + 1, size=n).astype(np.int64)
    if t_hi > t_lo + tie_pool:
        spread = rng.integers(t_lo, t_hi, size=n).astype(np.int64)
        mask = rng.random(n) < 0.5
        ast[mask] = spread[mask]
    arr["adjusted_start_time"] = ast
    arr["start_time"] = ast - rng.integers(0, 50, size=n).astype(np.int64)
    duration = rng.integers(0, 200, size=n).astype(np.int64)
    arr["end_time"] = ast + duration
    arr["created_at"] = rng.integers(t_lo, t_hi + 1, size=n).astype(np.int64)
    return arr


def _gen_case(idx: int) -> dict:
    """Generate one randomized scenario, biased to cover edge cases."""
    flavor = idx % 14

    if flavor == 0:  # empty files
        n_files, n_rec = 0, int(rng.integers(0, 6))
        t_lo, t_hi, tie = 1000, 2000, 3
    elif flavor == 1:  # empty recordings
        n_files, n_rec = int(rng.integers(0, 20)), 0
        t_lo, t_hi, tie = 1000, 2000, 3
    elif flavor == 2:  # both empty
        n_files, n_rec = 0, 0
        t_lo, t_hi, tie = 1000, 2000, 3
    elif flavor == 3:  # single recording
        n_files, n_rec = int(rng.integers(0, 15)), 1
        t_lo, t_hi, tie = 1000, 1100, 2
    elif flavor == 4:  # files entirely outside all windows
        n_files, n_rec = int(rng.integers(1, 20)), int(rng.integers(1, 6))
        t_lo, t_hi, tie = 5000, 6000, 5
    elif flavor == 5:  # heavy overlap: tiny time range, many windows
        n_files, n_rec = int(rng.integers(1, 30)), int(rng.integers(1, 20))
        t_lo, t_hi, tie = 1000, 1003, 1
    elif flavor == 6:  # all-orphan likely (windows far from files)
        n_files, n_rec = int(rng.integers(1, 15)), int(rng.integers(1, 5))
        t_lo, t_hi, tie = 1000, 1010, 2
    elif flavor == 7:  # heavy ties everywhere
        n_files, n_rec = int(rng.integers(1, 25)), int(rng.integers(1, 12))
        t_lo, t_hi, tie = 1000, 1001, 0
    elif flavor == 12:  # larger scale, heavy multi-window overlap
        n_files, n_rec = int(rng.integers(50, 400)), int(rng.integers(20, 80))
        t_lo = 1000
        t_hi = 1000 + int(rng.integers(5, 40))
        tie = int(rng.integers(0, 4))
    elif flavor == 13:  # drain-heavy small with disabled branches
        n_files, n_rec = int(rng.integers(0, 12)), int(rng.integers(0, 8))
        t_lo, t_hi, tie = 1000, 1050, 1
    else:  # general random
        n_files = int(rng.integers(0, 40))
        n_rec = int(rng.integers(0, 15))
        t_lo = int(rng.integers(0, 2000))
        t_hi = t_lo + int(rng.integers(10, 3000))
        tie = int(rng.integers(0, 8))

    files_data = _make_files(n_files, t_lo, t_hi, tie)
    recordings_data = _make_recordings(n_rec, t_lo, t_hi, tie)

    # Flavor 6: push windows away from files to force orphans.
    if flavor == 6 and n_rec > 0:
        recordings_data["adjusted_start_time"] += 100000
        recordings_data["end_time"] += 100000

    segment_length = int(rng.choice([0, 1, 5, 10, 30]))

    # Branch knobs — include zeros that disable a branch.
    max_bytes = int(rng.choice([0, 0, 1, 100, 5000, 50000, 500000]))
    min_bytes = int(rng.choice([0, 0, 1, 100, 5000, 50000]))

    # Timestamps drawn around the data range so comparisons land both
    # ways; 0 disables max_age branch.
    span_lo = t_lo - 50
    span_hi = t_hi + 300
    min_age_ts = float(rng.integers(span_lo, span_hi + 1))
    max_age_ts = float(
        rng.choice([0, 0, float(rng.integers(span_lo, span_hi + 1))])
    )
    file_min_age_ts = float(rng.integers(span_lo, span_hi + 1))

    drain = bool(rng.integers(0, 2))

    return {
        "recordings_data": recordings_data,
        "files_data": files_data,
        "segment_length": segment_length,
        "max_bytes": max_bytes,
        "min_age_timestamp": min_age_ts,
        "min_bytes": min_bytes,
        "max_age_timestamp": max_age_ts,
        "file_min_age_timestamp": file_min_age_ts,
        "drain": drain,
    }


def _call(fn, case: dict) -> np.ndarray:
    # Each implementation sorts files_data in-place; give each a private
    # copy so the harness compares like-for-like (and so the side effect
    # of one call cannot taint the other).
    kw = dict(case)
    kw["recordings_data"] = case["recordings_data"].copy()
    kw["files_data"] = case["files_data"].copy()
    return fn(**kw)


def _as_pair_set(arr: np.ndarray) -> set:
    return {
        (int(r["recording_id"]), int(r["id"]))
        for r in arr
    }


def main() -> None:
    passed = 0
    for idx in range(N_CASES):
        case = _gen_case(idx)
        legacy_out = _call(_get_recordings_to_move_legacy, case)
        new_out = _call(get_recordings_to_move, case)

        ok = True
        reason = ""

        if legacy_out.shape != new_out.shape:
            ok, reason = False, (
                f"shape mismatch: legacy={legacy_out.shape} "
                f"new={new_out.shape}"
            )
        elif legacy_out.dtype != new_out.dtype:
            ok, reason = False, (
                f"dtype mismatch: legacy={legacy_out.dtype} "
                f"new={new_out.dtype}"
            )
        else:
            for field in ("recording_id", "id"):
                if not np.array_equal(legacy_out[field], new_out[field]):
                    ok, reason = False, f"field '{field}' elementwise mismatch"
                    break
            if ok and _as_pair_set(legacy_out) != _as_pair_set(new_out):
                ok, reason = False, "pair-set mismatch"

        if not ok:
            print(f"\nFAIL on case idx={idx}: {reason}")
            print("--- INPUT ---")
            print("segment_length    =", case["segment_length"])
            print("max_bytes         =", case["max_bytes"])
            print("min_age_timestamp =", case["min_age_timestamp"])
            print("min_bytes         =", case["min_bytes"])
            print("max_age_timestamp =", case["max_age_timestamp"])
            print("file_min_age_ts   =", case["file_min_age_timestamp"])
            print("drain             =", case["drain"])
            print("recordings_data   =\n", case["recordings_data"])
            print("files_data        =\n", case["files_data"])
            print("--- LEGACY OUTPUT ---")
            print(repr(legacy_out))
            print("--- NEW OUTPUT ---")
            print(repr(new_out))
            print(f"\nTotal cases attempted: {idx + 1}")
            print("RESULT: FAIL")
            sys.exit(1)

        passed += 1

    print(f"Total cases: {passed}")
    print("RESULT: PASS")


def test_get_recordings_to_move_vectorized_matches_legacy() -> None:
    """Pytest entry point: differential equivalence over the corpus.

    ``main`` prints a report and ``sys.exit(1)`` on the first mismatch;
    translate that into an assertion failure for pytest.
    """
    try:
        main()
    except SystemExit as exc:
        raise AssertionError(
            "vectorized get_recordings_to_move diverged from the legacy "
            "oracle (see captured stdout for the failing case)"
        ) from exc


if __name__ == "__main__":
    main()
