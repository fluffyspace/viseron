"""Tests for DedupCheckQueue from storage_tier_subprocess.

Run with:
  python3 tests/components/storage/test_dedup_check_queue.py
or via pytest.

Validates:
  * Same-key items replace (only the newest survives, queue stays small).
  * Distinct-key items all retained, FIFO order on get().
  * .get(timeout=) raises queue.Empty on timeout.
  * Blocked .get() wakes up when put() arrives.
  * Concurrent multi-writer / single-reader stress: no lost items
    across distinct keys, no duplicate emissions, no deadlocks.
  * drain_metrics returns correct values and resets counters.
"""

from __future__ import annotations

import dataclasses
import random
import threading
import time
from queue import Empty

from subprocess_workers.storage_tier_subprocess import DedupCheckQueue


@dataclasses.dataclass
class FakeItem:
    camera_identifier: str
    throttle_key: str
    seq: int


def test_replace_same_key() -> None:
    q = DedupCheckQueue()
    q.put(FakeItem("camA", "k1", seq=1))
    q.put(FakeItem("camA", "k1", seq=2))
    q.put(FakeItem("camA", "k1", seq=3))
    assert q.qsize() == 1
    item = q.get(timeout=0.01)
    assert item.seq == 3
    current, max_size, replaced = q.drain_metrics()
    assert current == 0
    assert max_size == 1
    assert replaced == 2


def test_distinct_keys_all_present() -> None:
    q = DedupCheckQueue()
    for i in range(10):
        q.put(FakeItem(f"cam{i}", "k", seq=i))
    assert q.qsize() == 10
    out = [q.get(timeout=0.01) for _ in range(10)]
    assert [it.seq for it in out] == list(range(10))
    try:
        q.get(timeout=0.01)
        assert False, "expected Empty after draining"
    except Empty:
        pass


def test_get_timeout_raises_empty() -> None:
    q = DedupCheckQueue()
    t0 = time.monotonic()
    try:
        q.get(timeout=0.05)
        assert False, "expected Empty"
    except Empty:
        pass
    elapsed = time.monotonic() - t0
    assert 0.04 < elapsed < 0.2


def test_get_blocks_until_put() -> None:
    q = DedupCheckQueue()
    got: list[FakeItem] = []

    def consumer() -> None:
        got.append(q.get(timeout=1.0))

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.05)
    q.put(FakeItem("camA", "k", seq=42))
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert len(got) == 1 and got[0].seq == 42


def test_drain_metrics_resets() -> None:
    q = DedupCheckQueue()
    q.put(FakeItem("a", "k", 1))
    q.put(FakeItem("a", "k", 2))
    q.put(FakeItem("b", "k", 3))
    assert q.drain_metrics() == (2, 2, 1)
    assert q.drain_metrics() == (2, 2, 0)


def test_concurrent_distinct_keys_no_loss() -> None:
    q = DedupCheckQueue()
    N_WRITERS = 8
    M_PER = 500
    seen_keys: dict[tuple[str, str], int] = {}
    stop = threading.Event()
    lock = threading.Lock()
    errors: list[str] = []

    def writer(wid: int) -> None:
        rng = random.Random(wid)
        for j in range(M_PER):
            key = (f"cam{wid}", f"k{j}")
            q.put(FakeItem(*key, seq=j))
            if j > 0 and rng.random() < 0.05:
                older = (f"cam{wid}", f"k{rng.randrange(j)}")
                q.put(FakeItem(*older, seq=j + 100_000))

    def reader() -> None:
        empties = 0
        while not stop.is_set() or q.qsize() > 0:
            try:
                item = q.get(timeout=0.05)
            except Empty:
                empties += 1
                if empties > 200:
                    return
                continue
            empties = 0
            k = (item.camera_identifier, item.throttle_key)
            with lock:
                prev = seen_keys.get(k)
                if prev is not None and item.seq <= prev:
                    errors.append(f"out-of-order for {k}: prev={prev} now={item.seq}")
                seen_keys[k] = item.seq

    writers = [threading.Thread(target=writer, args=(i,)) for i in range(N_WRITERS)]
    r = threading.Thread(target=reader)
    r.start()
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    r.join(timeout=5.0)
    assert not r.is_alive(), "reader hung"
    assert not errors, errors
    assert len(seen_keys) == N_WRITERS * M_PER


def test_no_deadlock_two_blocked_consumers() -> None:
    q = DedupCheckQueue()
    got: list[int] = []
    lock = threading.Lock()

    def consumer() -> None:
        try:
            item = q.get(timeout=1.0)
            with lock:
                got.append(item.seq)
        except Empty:
            pass

    t1 = threading.Thread(target=consumer)
    t2 = threading.Thread(target=consumer)
    t1.start()
    t2.start()
    time.sleep(0.05)
    q.put(FakeItem("camA", "k", seq=1))
    q.put(FakeItem("camB", "k", seq=2))
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(got) == [1, 2]


if __name__ == "__main__":
    test_replace_same_key()
    test_distinct_keys_all_present()
    test_get_timeout_raises_empty()
    test_get_blocks_until_put()
    test_drain_metrics_resets()
    test_concurrent_distinct_keys_no_loss()
    test_no_deadlock_two_blocked_consumers()
    print("ALL TESTS PASSED")
