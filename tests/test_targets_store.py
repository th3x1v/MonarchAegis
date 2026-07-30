"""Phase 1: the `targets` table + CRUD + one-shot Lua/unpaired migration.

The `db` fixture (see conftest.py) provides a fresh, isolated DatabaseManager
per test.
"""
import database


# --- _split_logical_target helper ---

def test_split_logical_target_remote():
    assert database.DatabaseManager._split_logical_target(
        "root@192.168.1.10:/source_data/Anime") == ("root@192.168.1.10", "/source_data/Anime")

def test_split_logical_target_local():
    assert database.DatabaseManager._split_logical_target("/mnt/backup/docs") == (None, "/mnt/backup/docs")

def test_split_logical_target_empty():
    # empty/missing destination normalizes to None (consistent NULL convention)
    assert database.DatabaseManager._split_logical_target("") == (None, None)


# --- _server_index / upsert_target_from_config (live create/update path) ---

def test_server_index_keys_by_key_and_host():
    idx = database.DatabaseManager._server_index(
        [{"id": "s1", "host": "192.168.1.10", "user": "root", "key_id": "k1"},
         {"id": "s2", "host": "h2", "key_id": "k2"}])
    assert idx["key:k1"] == "s1"          # key is the unique, preferred identifier
    assert idx["key:k2"] == "s2"
    assert idx["host:192.168.1.10"] == "s1"
    assert idx["host:root@192.168.1.10"] == "s1"

def test_resolve_server_prefers_key_when_hosts_collide():
    # Two servers on the SAME host+user (the real-world rrsync case) — host is
    # ambiguous, so resolution MUST use the key to pick the right one.
    servers = [
        {"id": "srv_anime", "host": "192.168.1.10", "user": "root", "key_id": "key_anime"},
        {"id": "srv_movies", "host": "192.168.1.10", "user": "root", "key_id": "key_movies"},
    ]
    idx = database.DatabaseManager._server_index(servers)
    assert database.DatabaseManager._resolve_server_id(idx, "key_anime", "root@192.168.1.10") == "srv_anime"
    assert database.DatabaseManager._resolve_server_id(idx, "key_movies", "root@192.168.1.10") == "srv_movies"
    # no key -> host fallback (first server on that host)
    assert database.DatabaseManager._resolve_server_id(idx, None, "root@192.168.1.10") == "srv_anime"

def test_upsert_target_from_config_paired(db):
    parsed = {"id": "tv", "name": "TV", "source": "/src/", "target": "root@192.168.1.10:/dest"}
    servers = [{"id": "srv", "host": "192.168.1.10", "user": "root"}]
    db.upsert_target_from_config(parsed, servers, enabled=True)
    row = db.get_target("tv")
    assert row["server_id"] == "srv"
    assert row["dest_path"] == "/dest"
    assert row["source_path"] == "/src/"
    assert row["enabled"] == 1

def test_upsert_target_from_config_local(db):
    db.upsert_target_from_config({"id": "loc", "name": "L", "source": "/a/", "target": "/mnt/b"},
                                 [], enabled=True)
    row = db.get_target("loc")
    assert row["server_id"] is None and row["dest_path"] == "/mnt/b"


# --- CRUD round-trip ---

def test_upsert_and_get(db):
    db.upsert_target(id="tv_series", name="TV Series", source_path="/source_data/library/Series/",
                     server_id="srv_abc", dest_path="/source_data/Anime")
    row = db.get_target("tv_series")
    assert row is not None
    assert row["name"] == "TV Series"
    assert row["server_id"] == "srv_abc"
    assert row["dest_path"] == "/source_data/Anime"
    assert row["enabled"] == 1 and row["interval_seconds"] == 0

def test_upsert_updates_in_place(db):
    db.upsert_target(id="t", name="Original", source_path="/s")
    db.upsert_target(id="t", name="Renamed", source_path="/s")
    assert len(db.list_targets()) == 1
    assert db.get_target("t")["name"] == "Renamed"

def test_get_missing_returns_none(db):
    assert db.get_target("nope") is None

def test_set_schedule_preserves_run_result(db):
    db.upsert_target(id="t", name="T", source_path="/s")
    db.record_target_run("t", status="success", summary="12 files, 3.4 GB")
    db.set_target_schedule("t", interval_seconds=3600, enabled=False)
    row = db.get_target("t")
    assert row["interval_seconds"] == 3600 and row["enabled"] == 0
    # schedule change must NOT clobber the recorded run result
    assert row["last_status"] == "success" and row["last_summary"] == "12 files, 3.4 GB"
    assert row["last_run"] is not None

def test_delete_target_record(db):
    db.upsert_target(id="t", name="T", source_path="/s")
    db.delete_target_record("t")
    assert db.get_target("t") is None


# --- Migration ---

def _sample_config():
    paired = [
        {"id": "tvseries", "name": "TV Series", "source": "/source_data/library/Series/",
         "target": "root@192.168.1.10:/source_data/Anime", "physical_target": "root@192.168.1.10:/"},
        {"id": "localbackup", "name": "Local Backup", "source": "/data/docs/",
         "target": "/mnt/backup/docs"},  # local target, no server
    ]
    unpaired = [{"id": "source_deadbeef", "name": "New Movies", "path": "/source_data/Movies/"}]
    servers = [{"id": "srv_dest", "host": "192.168.1.10", "user": "root", "port": 2222, "key_id": "key_x"}]
    return paired, unpaired, servers

def test_migration_maps_paired_and_unpaired(db):
    paired, unpaired, servers = _sample_config()
    # a hash row under the paired id proves ids stay associated post-migration
    db.batch_upsert_file_hashes([("tvseries", "Show/S01E01.mkv", 10, 1.0, "h", False)])

    assert db.migrate_targets_from_config(paired, unpaired, servers) == 3

    tv = db.get_target("tvseries")
    assert tv["server_id"] == "srv_dest"          # resolved from root@192.168.1.10
    assert tv["dest_path"] == "/source_data/Anime"  # parsed from the logical target
    assert tv["enabled"] == 1

    lb = db.get_target("localbackup")
    assert lb["server_id"] is None and lb["dest_path"] == "/mnt/backup/docs"

    up = db.get_target("source_deadbeef")
    assert up["enabled"] == 0                        # unpaired -> disabled
    assert up["server_id"] is None and up["dest_path"] is None

    # existing file_hashes stay linked to the migrated target id
    assert any(h["filepath"] == "Show/S01E01.mkv" for h in db.get_all_hashes("tvseries"))

def test_migration_is_idempotent(db):
    paired, unpaired, servers = _sample_config()
    assert db.migrate_targets_from_config(paired, unpaired, servers) == 3
    assert db.migrate_targets_from_config(paired, unpaired, servers) == 0  # guard flag

def test_factory_reset_rebuilds(db):
    paired, unpaired, servers = _sample_config()
    db.migrate_targets_from_config(paired, unpaired, servers)
    db.factory_reset_data()
    assert db.list_targets() == []
    assert db.get_setting("targets_migrated") is None
    assert db.migrate_targets_from_config(paired, unpaired, servers) == 3  # runs again


# --- per-day transfer counter (record_transfers / transfers_since) ---

def test_record_transfers_and_count_since(db):
    db.record_transfers("tv", ["a.mkv", "b.mkv"])
    db.record_transfers("movies", ["c.mkv"], action="recovery")
    counts = db.transfers_since("1970-01-01 00:00:00")
    assert counts == {"tv": 2, "movies": 1}

def test_transfers_since_excludes_older_rows(db):
    # a row stamped yesterday must not count toward a since-today window
    with db.get_connection() as conn:
        conn.execute("INSERT INTO transfer_history (target_id, filepath, action, timestamp) "
                     "VALUES ('tv', 'old.mkv', 'sync', datetime('now', '-1 day'))")
        conn.commit()
    db.record_transfers("tv", ["new.mkv"])
    counts = db.transfers_since(_utc_hours_ago(1))
    assert counts.get("tv") == 1                      # only the fresh row

def test_record_transfers_prunes_ancient_rows(db):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO transfer_history (target_id, filepath, action, timestamp) "
                     "VALUES ('tv', 'ancient.mkv', 'sync', datetime('now', '-60 days'))")
        conn.commit()
    db.record_transfers("tv", ["fresh.mkv"])          # prune runs inside
    counts = db.transfers_since("1970-01-01 00:00:00")
    assert counts.get("tv") == 1                      # ancient row pruned

def _utc_hours_ago(hours):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def test_reresolve_fixes_wrong_server_by_key(db):
    # The real bug: two servers on the SAME host+user; an earlier host-based
    # migration wrongly assigned the target to the last server. Re-resolve fixes
    # it by key WITHOUT disturbing the schedule/last-run.
    servers = [
        {"id": "srv_anime", "host": "192.168.1.10", "user": "root", "key_id": "key_anime"},
        {"id": "srv_movies", "host": "192.168.1.10", "user": "root", "key_id": "key_movies"},
    ]
    db.upsert_target(id="anime", name="Anime", source_path="/src/anime",
                     server_id="srv_movies", dest_path="/dest/anime",   # WRONG server
                     interval_seconds=3600, enabled=True)
    db.record_target_run("anime", "error", "old run")
    # the Lua target for anime carries its real key
    paired = [{"id": "anime", "name": "Anime", "source": "/src/anime",
               "target": "root@192.168.1.10:/dest/anime", "key_id": "key_anime"}]

    assert db.reresolve_target_servers(paired, servers) == 1
    row = db.get_target("anime")
    assert row["server_id"] == "srv_anime"      # corrected by key
    assert row["interval_seconds"] == 3600      # schedule preserved
    assert row["last_status"] == "error"        # last-run preserved
    assert db.reresolve_target_servers(paired, servers) == 0   # idempotent
