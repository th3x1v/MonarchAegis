"""End-to-end tests for the register-after-transfer patch, calling the real
FastAPI endpoint coroutines directly (no httpx/TestClient needed).

The app runs in client mode (MONARCHAEGIS_ROLE=client, set in conftest) since
register/diff are client-only. Each test gets a clean DB via the autouse
`reset_state` fixture.
"""
import asyncio
import json

import pytest
import main

db = main.db

TARGET_ID = "target_client_xyz"
TARGET_PATH = "/mnt/user/media/tv"
FP = "abc123deadbeef"


def call(coro):
    """Run an endpoint coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def reset_state():
    """Isolate each test: wipe hashes/targets, register a single client target,
    and mark its baseline complete so /api/client/diff isn't gated."""
    for table in ("file_hashes", "missing_files", "conflicts"):
        with db.get_connection() as conn:
            conn.execute(f"DELETE FROM {table}")
            conn.commit()
    db.set_setting("client_targets", json.dumps([{"id": TARGET_ID, "path": TARGET_PATH}]))
    main.hash_scanner_daemon.scan_status[TARGET_ID] = {"status": "complete"}
    yield
    # Don't leak our scan-status entry into other tests that share the singleton.
    main.hash_scanner_daemon.scan_status.pop(TARGET_ID, None)


def test_container_is_client_mode():
    assert main.get_current_role() == "client"


def test_register_records_under_resolved_id():
    payload = main.RegisterPayloadModel(
        target_id="source_side_id_ignored",     # server resolves via target_path
        target_path=TARGET_PATH,
        files=[main.RegisterFileModel(filepath="Show/S01E01.mkv", size=1234, mtime=111.0, file_hash=FP)],
    )
    body = call(main.client_register(payload))
    assert body["status"] == "success"
    assert body["registered"] == 1

    rows = {r["filepath"]: r for r in db.get_all_hashes(TARGET_ID)}
    assert "Show/S01E01.mkv" in rows            # landed under the target_path-resolved id
    assert rows["Show/S01E01.mkv"]["file_hash"] == FP
    assert not rows["Show/S01E01.mkv"]["modified_locally"]   # not a conflict


def test_registered_file_not_reflagged():
    # register, then diff the same file -> must NOT be missing (the core fix)
    call(main.client_register(main.RegisterPayloadModel(
        target_id="x", target_path=TARGET_PATH,
        files=[main.RegisterFileModel(filepath="Show/S01E01.mkv", size=1234, mtime=111.0, file_hash=FP)])))

    diff = call(main.client_diff(main.DiffPayloadModel(
        target_id="x", target_path=TARGET_PATH, source_hash_algo=db.HASH_ALGO,
        source_hashes=[main.HashFileModel(filepath="Show/S01E01.mkv", size=1234, mtime=111.0, file_hash=FP)])))
    assert diff["status"] == "success"
    assert diff["missing_files"] == []


def test_reencoded_destination_not_repushed():
    """The core re-encode protection: once the destination has recorded a file
    in its ledger, re-encoding it on disk (AV1) must NOT cause a re-push, because
    the diff compares the Source's hash against the LEDGER — never against the
    destination's actual (now re-encoded) file. Nothing re-hashes the re-encoded
    file into the ledger (no watchdog on FUSE), so the ledger still holds H1 and
    the Source still sends H1 -> match -> not missing."""
    call(main.client_register(main.RegisterPayloadModel(
        target_id="x", target_path=TARGET_PATH,
        files=[main.RegisterFileModel(filepath="Show/S01E01.mkv", size=1234, mtime=111.0, file_hash="H1")])))

    diff = call(main.client_diff(main.DiffPayloadModel(
        target_id="x", target_path=TARGET_PATH, source_hash_algo=db.HASH_ALGO,
        source_hashes=[main.HashFileModel(filepath="Show/S01E01.mkv", size=1234, mtime=111.0, file_hash="H1")])))
    assert diff["missing_files"] == []   # re-encoded destination file is NOT re-pushed


def test_unregistered_file_still_flagged():
    diff = call(main.client_diff(main.DiffPayloadModel(
        target_id="x", target_path=TARGET_PATH, source_hash_algo=db.HASH_ALGO,
        source_hashes=[main.HashFileModel(filepath="Show/S01E02.mkv", size=9, mtime=1.0, file_hash="newhash")])))
    assert diff["missing_files"] == ["Show/S01E02.mkv"]


def test_clear_on_success_gate():
    # Mirrors the run_sync_missing gate: clear only on rsync rc==0.
    gate_tid = "gate_target"
    db.add_missing_file(gate_tid, "/x/a.mkv")
    db.add_missing_file(gate_tid, "/x/b.mkv")

    def apply_gate(returncode):
        if returncode == 0:
            db.clear_missing_files(gate_tid)

    apply_gate(1)
    assert len(db.get_missing_files(gate_tid)) == 2   # failure keeps the list
    apply_gate(0)
    assert len(db.get_missing_files(gate_tid)) == 0   # success clears it


# --- client directory listing reports the LIVE ledger count ---

def test_client_targets_report_tracked_count(monkeypatch):
    """Received files are registered straight into the ledger and never touch the
    scanner's baseline counters — the UI must read the ledger, or transfers stay
    invisible (client showed "48 total" while the ledger held 567)."""
    import asyncio
    import json as _json
    import main

    main.db.set_setting("client_targets", _json.dumps(
        [{"id": "client_abc", "alias": "Media2", "path": "/source_data/Music"}]))
    # 2 from a baseline, 3 more arriving later via register-after-transfer
    main.db.batch_upsert_file_hashes([
        ("client_abc", f"song{i}.flac", 10, 1.0, f"h{i}", False) for i in range(5)])

    res = asyncio.run(main.list_client_targets())
    t = next(x for x in res["targets"] if x["id"] == "client_abc")
    assert t["tracked"] == 5


def test_client_targets_tracked_zero_when_empty(monkeypatch):
    import asyncio
    import json as _json
    import main

    main.db.set_setting("client_targets", _json.dumps(
        [{"id": "client_empty", "alias": "New", "path": "/source_data/New"}]))
    res = asyncio.run(main.list_client_targets())
    t = next(x for x in res["targets"] if x["id"] == "client_empty")
    assert t["tracked"] == 0
