"""Memory regression tests.

This project has a history of memory/PID blowups (zombie leak, event-loop
blocking), so we guard the hot DB paths against unbounded growth:

  - tracemalloc: assert repeated batch upserts don't retain Python memory.
  - pytest-memray (@pytest.mark.limit_memory): cap peak allocation of the
    bulk migration path. The marker is a no-op if pytest-memray is absent, so
    the suite still runs without it, but the test image installs it.
"""
import tracemalloc

import pytest

pytestmark = pytest.mark.memory

# A real leak across 5k upserts would be 100s of MB; this generous bound catches
# leaks without flaking on allocator/SQLite-cache noise.
MAX_HEAP_GROWTH_BYTES = 15 * 1024 * 1024


def test_batch_upsert_no_unbounded_growth(db):
    """10 rounds of 500-row batch upserts must not retain a growing heap — the
    rows go to SQLite and the Python tuples should be freed each round."""
    def do_round(base):
        rows = [("tid", f"dir/file_{i}.mkv", 100, 1.0, "hash", False)
                for i in range(base, base + 500)]
        db.batch_upsert_file_hashes(rows)

    do_round(0)  # warm up caches/connections before measuring
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for k in range(1, 11):
        do_round(k * 500)
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    grew = sum(s.size_diff for s in after.compare_to(before, "filename"))
    assert grew < MAX_HEAP_GROWTH_BYTES, f"heap grew {grew/1024/1024:.1f} MB across 5k upserts"


@pytest.mark.limit_memory("100 MB")
def test_migration_memory_bounded(db):
    """Bulk migration of 1,000 targets builds all rows in memory before one
    executemany; verify peak allocation stays well bounded."""
    paired = [{"id": f"t{i}", "name": f"Target {i}", "source": f"/src/{i}",
               "target": f"root@host:/dest/{i}"} for i in range(1000)]
    servers = [{"id": "srv", "host": "host", "user": "root"}]
    assert db.migrate_targets_from_config(paired, [], servers) == 1000
