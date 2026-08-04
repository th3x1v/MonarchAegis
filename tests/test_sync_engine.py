"""Phase 2: the DB-target-driven sync engine.

Uses the real `db` fixture for the DB parts and monkeypatches the two I/O
seams (`_post_json` for the P2P diff/register, `_run_rsync` for the push) so the
orchestration is driven without a real peer or SSH.
"""
import io
import json
import struct

import sync_engine

SERVER = {"id": "srv1", "host": "192.168.1.10", "user": "root", "port": 2222, "key_id": "key_x"}


class FakeServerMgr:
    def __init__(self, server):
        self._s = server

    def get_server(self, sid):
        return self._s if (self._s and sid == self._s["id"]) else None


def _setup_target(db, enabled=True, server_id="srv1"):
    db.upsert_target(id="t1", name="T1", source_path="/src/", server_id=server_id,
                     dest_path="/dest", enabled=enabled)
    db.batch_upsert_file_hashes([("t1", "a.mkv", 10, 1.0, "h1", False),
                                 ("t1", "b.mkv", 20, 2.0, "h2", False)])


# --- pure-helper unit tests ---

def test_resolve_connection():
    host, api_base, physical_dest, rsh = sync_engine._resolve_connection(SERVER)
    assert host == "192.168.1.10"
    assert api_base == "http://192.168.1.10:5000"
    assert physical_dest == "root@192.168.1.10:/"          # rrsync jail root
    assert rsh.startswith("--rsh=ssh ")
    assert "IdentityFile=" in rsh and "-p 2222" in rsh


def test_build_push_command():
    cmd = sync_engine._build_push_command("/src", "root@h:/", "/tmp/list.txt", "--rsh=ssh x")
    assert cmd[0] == "rsync"
    assert "-rvW" in cmd                                   # -W whole-file
    assert "--no-secluded-args" in cmd
    assert "--files-from=/tmp/list.txt" in cmd
    assert cmd[-2] == "/src/" and cmd[-1] == "root@h:/"    # trailing slashes normalized


# --- orchestration tests ---

def test_up_to_date_transfers_nothing(db, monkeypatch):
    _setup_target(db)
    ran = {"rsync": False}
    monkeypatch.setattr(sync_engine, "_post_json",
                        lambda url, p, t: {"status": "success", "missing_files": []})
    monkeypatch.setattr(sync_engine, "_run_rsync",
                        lambda cmd, *a, **k: ran.__setitem__("rsync", True) or (0, ""))

    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "success" and res["transferred"] == 0
    assert ran["rsync"] is False                            # no push when nothing missing
    assert db.get_target("t1")["last_status"] == "success"


def test_transfers_and_registers(db, monkeypatch):
    _setup_target(db)
    calls = {}

    def fake_post(url, payload, timeout):
        if url.endswith("/api/client/diff"):
            return {"status": "success", "missing_files": ["a.mkv"]}
        if url.endswith("/api/client/register"):
            calls["register"] = payload
            return {"status": "success", "registered": 1}
        raise AssertionError(f"unexpected url {url}")

    def fake_rsync(cmd, *a, **k):
        ff = next(a.split("=", 1)[1] for a in cmd if a.startswith("--files-from="))
        calls["files_from"] = open(ff).read()
        return 0, "sent 1 file"

    monkeypatch.setattr(sync_engine, "_post_json", fake_post)
    monkeypatch.setattr(sync_engine, "_run_rsync", fake_rsync)

    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "success" and res["transferred"] == 1
    assert "a.mkv" in calls["files_from"] and "b.mkv" not in calls["files_from"]
    # registered the SOURCE's hash for exactly the transferred file
    reg = calls["register"]["files"]
    assert len(reg) == 1 and reg[0]["filepath"] == "a.mkv" and reg[0]["file_hash"] == "h1"
    row = db.get_target("t1")
    assert row["last_status"] == "success" and "1" in row["last_summary"]
    # the transferred file is recorded for the per-day counter
    assert db.transfers_since("1970-01-01 00:00:00").get("t1") == 1


# --- tar-stream transport (source side) ---

def test_build_tar_roundtrips_through_receiver(tmp_path):
    """The source's _build_tar and the destination's tar_receiver agree on the
    frame format and digest: files built on one side land intact on the other."""
    import os
    import tar_receiver
    src = tmp_path / "src"; (src / "sub").mkdir(parents=True)
    (src / "a.mkv").write_bytes(b"hello")
    (src / "sub" / "b.mkv").write_bytes(b"world!!")

    tar_path, digest, size, included, vanished = sync_engine._build_tar(
        str(src), ["a.mkv", "sub/b.mkv"])
    assert included == ["a.mkv", "sub/b.mkv"] and vanished == []
    body = open(tar_path, "rb").read(); os.remove(tar_path)
    assert len(body) == size

    header = json.dumps({"v": 1, "algo": "xxh64", "digest": digest, "size": size}).encode()
    frame = struct.pack(">I", len(header)) + header + body
    jail = tmp_path / "jail"
    rc = tar_receiver.tar_receive(str(jail), stdin=io.BytesIO(frame), stdout=io.BytesIO())
    assert rc == 0
    assert (jail / "a.mkv").read_bytes() == b"hello"
    assert (jail / "sub" / "b.mkv").read_bytes() == b"world!!"


def test_build_tar_stages_in_configured_tmpdir(tmp_path, monkeypatch):
    """The diff tarball must be staged in TAR_TMPDIR, not the system /tmp (which on
    Unraid is the small Docker vDisk — a large diff there fails with ENOSPC even
    though the array has room). Regression guard for that production bug."""
    import os
    src = tmp_path / "src"; src.mkdir()
    (src / "a.mkv").write_bytes(b"hello")
    staging = tmp_path / "staging"          # does not exist yet
    monkeypatch.setattr(sync_engine, "TAR_TMPDIR", str(staging))

    tar_path, *_ = sync_engine._build_tar(str(src), ["a.mkv"])
    try:
        assert staging.is_dir()             # created on demand
        assert os.path.dirname(tar_path) == str(staging)
    finally:
        os.remove(tar_path)


def test_run_tar_transfer_raises_on_receiver_error(tmp_path, monkeypatch):
    """A non-success receiver reply is surfaced as SyncError (recorded, retried)."""
    import pytest
    src = tmp_path / "src"; src.mkdir()
    (src / "a.mkv").write_bytes(b"x")
    monkeypatch.setattr(sync_engine, "_stream_to_receiver",
                        lambda argv, header, tar_path: (
                            3, '{"status":"error","message":"digest mismatch"}', ""))
    with pytest.raises(sync_engine.SyncError) as ei:
        sync_engine._run_tar_transfer("t1", str(src), SERVER, ["a.mkv"])
    assert "digest mismatch" in str(ei.value)


def test_vanished_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    """Media managers delete-and-replace on every upgrade, so a path from the diff
    routinely no longer exists at transfer time. Skip it, send the rest."""
    src = tmp_path / "src"; src.mkdir()
    (src / "here.mkv").write_bytes(b"data")
    monkeypatch.setattr(sync_engine, "_stream_to_receiver",
                        lambda argv, header, tar_path: (0, '{"status":"success","extracted":1}', ""))

    sent, vanished = sync_engine._run_tar_transfer(
        "t1", str(src), SERVER, ["here.mkv", "gone.mkv"])
    assert sent == ["here.mkv"]          # only the file that existed
    assert vanished == ["gone.mkv"]


def test_all_vanished_is_a_noop_not_an_error(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    monkeypatch.setattr(sync_engine, "_stream_to_receiver",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not stream")))
    sent, vanished = sync_engine._run_tar_transfer("t1", str(src), SERVER, ["gone.mkv"])
    assert sent == [] and vanished == ["gone.mkv"]


def test_mass_vanished_aborts_instead_of_pruning(tmp_path, monkeypatch):
    """A mostly-missing batch means the source is unmounted, not that the library
    changed — abort rather than transfer a fragment and prune good ledger rows."""
    import pytest
    src = tmp_path / "src"; src.mkdir()
    (src / "here.mkv").write_bytes(b"data")
    monkeypatch.setattr(sync_engine, "_stream_to_receiver",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not stream")))
    batch = ["here.mkv"] + [f"gone{i}.mkv" for i in range(12)]
    with pytest.raises(sync_engine.SyncError) as ei:
        sync_engine._run_tar_transfer("t1", str(src), SERVER, batch)
    assert "mounted" in str(ei.value).lower()


def test_prune_vanished_removes_stale_rows(db):
    _setup_target(db)
    assert any(h["filepath"] == "a.mkv" for h in db.get_all_hashes("t1"))
    n = sync_engine._prune_vanished(db, "t1", db.get_all_hashes("t1"), ["a.mkv"])
    assert n == 1
    paths = [h["filepath"] for h in db.get_all_hashes("t1")]
    assert "a.mkv" not in paths and "b.mkv" in paths     # only the vanished one forgotten


def test_sync_now_uses_tar_transport_when_selected(db, monkeypatch):
    _setup_target(db)
    used = {}
    monkeypatch.setattr(sync_engine, "TRANSPORT", "tar")
    monkeypatch.setattr(sync_engine, "_post_json", lambda url, p, t: (
        {"status": "success", "missing_files": ["a.mkv"]} if url.endswith("/diff")
        else {"status": "success", "registered": 1}))
    def fake_tar(tid, sp, server, missing, say=None):
        used["missing"] = missing
        return missing, []                    # (sent, vanished)
    monkeypatch.setattr(sync_engine, "_run_tar_transfer", fake_tar)
    # rsync must NOT be used when tar transport is selected
    monkeypatch.setattr(sync_engine, "_run_rsync",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("rsync path used")))

    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "success" and res["transferred"] == 1
    assert used["missing"] == ["a.mkv"]
    assert db.transfers_since("1970-01-01 00:00:00").get("t1") == 1


def test_sync_now_registers_only_files_actually_sent(db, monkeypatch):
    """The correctness trap: registering the whole diff would make the destination
    ledger claim files it never received, and they'd be skipped forever."""
    _setup_target(db)
    registered = {}
    monkeypatch.setattr(sync_engine, "TRANSPORT", "tar")

    def fake_post(url, payload, timeout):
        if url.endswith("/diff"):
            return {"status": "success", "missing_files": ["a.mkv", "b.mkv"]}
        registered["files"] = [f["filepath"] for f in payload["files"]]
        return {"status": "success", "registered": len(payload["files"])}
    monkeypatch.setattr(sync_engine, "_post_json", fake_post)
    # b.mkv vanished before transfer; only a.mkv was actually sent
    monkeypatch.setattr(sync_engine, "_run_tar_transfer",
                        lambda tid, sp, server, missing, say=None: (["a.mkv"], ["b.mkv"]))

    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "success" and res["transferred"] == 1
    assert registered["files"] == ["a.mkv"]                     # NOT b.mkv
    assert db.transfers_since("1970-01-01 00:00:00").get("t1") == 1
    # and the vanished file's stale row was pruned from the source ledger
    assert "b.mkv" not in [h["filepath"] for h in db.get_all_hashes("t1")]


def test_tar_transport_failure_records_error(db, monkeypatch):
    _setup_target(db)
    monkeypatch.setattr(sync_engine, "TRANSPORT", "tar")
    monkeypatch.setattr(sync_engine, "_post_json", lambda url, p, t: (
        {"status": "success", "missing_files": ["a.mkv"]} if url.endswith("/diff")
        else {"status": "success", "registered": 1}))

    def boom(*a, **k):
        raise sync_engine.SyncError("tar-stream failed; destination not updated. (digest mismatch)")
    monkeypatch.setattr(sync_engine, "_run_tar_transfer", boom)

    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "error" and res["transferred"] == 0
    assert db.get_target("t1")["last_status"] == "error"


def test_rsync_failure_records_error_and_skips_register(db, monkeypatch):
    _setup_target(db)
    calls = {"register": False}

    def fake_post(url, payload, timeout):
        if url.endswith("/diff"):
            return {"status": "success", "missing_files": ["a.mkv"]}
        calls["register"] = True
        return {"status": "success", "registered": 1}

    monkeypatch.setattr(sync_engine, "_post_json", fake_post)
    monkeypatch.setattr(sync_engine, "_run_rsync", lambda cmd, *a, **k: (23, "some files could not be transferred"))

    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "error" and res["transferred"] == 0
    assert calls["register"] is False                       # no register on failed push
    assert db.get_target("t1")["last_status"] == "error"


def test_diff_failure_records_error(db, monkeypatch):
    _setup_target(db)

    def boom(url, payload, timeout):
        raise ConnectionError("client unreachable")

    monkeypatch.setattr(sync_engine, "_post_json", boom)
    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "error"
    assert db.get_target("t1")["last_status"] == "error"


def test_missing_target(db):
    res = sync_engine.sync_now("nope", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "error" and "not found" in res["message"].lower()


def test_disabled_target(db):
    _setup_target(db, enabled=False)
    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "error" and "disabled" in res["message"].lower()


def test_unpaired_target_errors(db):
    _setup_target(db, server_id=None)
    res = sync_engine.sync_now("t1", db=db, server_mgr=FakeServerMgr(SERVER))
    assert res["status"] == "error" and "server" in res["message"].lower()
    assert db.get_target("t1")["last_status"] == "error"
