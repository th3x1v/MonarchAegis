"""Tar-stream receiver: the framed wire protocol, whole-tarball integrity check,
and the jail enforcement on extraction (the new remote-code surface).

The receiver is driven directly with in-memory byte streams — no ssh — so every
accept/reject path is exercised deterministically.
"""
import io
import json
import os
import struct
import tarfile

import xxhash

import tar_receiver


# --- helpers: build a tar in memory and frame it on the wire ---

def _make_tar(members) -> bytes:
    """members: list of (name, data) — data=None makes a directory member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in members:
            if data is None:
                info = tarfile.TarInfo(name); info.type = tarfile.DIRTYPE; info.mode = 0o755
                t.addfile(info)
            else:
                info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o644
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tar_with_symlink(link_name, target) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo(link_name); info.type = tarfile.SYMTYPE; info.linkname = target
        t.addfile(info)
    return buf.getvalue()


def _frame(tar_bytes, digest=None, size=None, algo="xxh64") -> bytes:
    header = json.dumps({
        "v": 1, "algo": algo,
        "digest": digest if digest is not None else xxhash.xxh64(tar_bytes).hexdigest(),
        "size": size if size is not None else len(tar_bytes),
    }).encode("utf-8")
    return struct.pack(">I", len(header)) + header + tar_bytes


def _run(jail, frame):
    out = io.BytesIO()
    rc = tar_receiver.tar_receive(str(jail), stdin=io.BytesIO(frame), stdout=out)
    reply = json.loads(out.getvalue().decode().strip())
    return rc, reply


# --- happy path ---

def test_valid_stream_extracts_all(tmp_path):
    jail = tmp_path / "jail"
    tar = _make_tar([("a.mkv", b"hello"), ("sub/b.mkv", b"world!!")])
    rc, reply = _run(jail, _frame(tar))
    assert rc == 0 and reply["status"] == "success" and reply["extracted"] == 2
    assert (jail / "a.mkv").read_bytes() == b"hello"
    assert (jail / "sub" / "b.mkv").read_bytes() == b"world!!"


def test_extracted_files_are_not_setuid(tmp_path):
    jail = tmp_path / "jail"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("evil.sh"); info.size = 2; info.mode = 0o4777  # setuid
        t.addfile(info, io.BytesIO(b"hi"))
    rc, reply = _run(jail, _frame(buf.getvalue()))
    assert rc == 0 and reply["status"] == "success"
    assert (os.stat(jail / "evil.sh").st_mode & 0o7000) == 0   # no setuid/setgid/sticky


# --- integrity (the user's real concern: dropped/truncated transfer) ---

def test_digest_mismatch_extracts_nothing(tmp_path):
    jail = tmp_path / "jail"
    tar = _make_tar([("a.mkv", b"hello")])
    rc, reply = _run(jail, _frame(tar, digest="0000000000000000"))
    assert rc == 3 and reply["status"] == "error" and "digest mismatch" in reply["message"]
    assert not (jail / "a.mkv").exists()
    assert list(jail.iterdir()) == []          # staging temp cleaned up too


def test_truncated_body_rejected(tmp_path):
    jail = tmp_path / "jail"
    tar = _make_tar([("a.mkv", b"hello")])
    # declare the true size but deliver fewer bytes
    frame = _frame(tar[:-10], digest=xxhash.xxh64(tar).hexdigest(), size=len(tar))
    rc, reply = _run(jail, frame)
    assert rc == 3 and "ended early" in reply["message"]
    assert list(jail.iterdir()) == []


# --- jail enforcement (hostile archive whose digest is honest) ---

def test_parent_ref_path_rejected(tmp_path):
    jail = tmp_path / "jail"
    tar = _make_tar([("ok.mkv", b"x"), ("../escape.mkv", b"pwned")])
    rc, reply = _run(jail, _frame(tar))
    assert rc == 3 and "parent-ref" in reply["message"]
    assert not (jail / "ok.mkv").exists()      # all-or-nothing: nothing written
    assert not (tmp_path / "escape.mkv").exists()


def test_absolute_path_rejected(tmp_path):
    jail = tmp_path / "jail"
    tar = _make_tar([("/etc/cron.d/evil", b"pwned")])
    rc, reply = _run(jail, _frame(tar))
    assert rc == 3 and "absolute path" in reply["message"]


def test_symlink_member_rejected(tmp_path):
    jail = tmp_path / "jail"
    tar = _tar_with_symlink("link", "/etc/passwd")
    rc, reply = _run(jail, _frame(tar))
    assert rc == 3 and "link member" in reply["message"]
    assert not (jail / "link").exists()


# --- framing guards ---

def test_oversized_header_rejected(tmp_path):
    jail = tmp_path / "jail"
    frame = struct.pack(">I", 1024 * 1024) + b"{}"     # header length way over the cap
    rc, reply = _run(jail, frame)
    assert rc == 3 and "header length" in reply["message"]


def test_unsupported_algo_rejected(tmp_path):
    jail = tmp_path / "jail"
    tar = _make_tar([("a.mkv", b"x")])
    rc, reply = _run(jail, _frame(tar, algo="md5"))
    assert rc == 3 and "algo" in reply["message"]


# --- launcher / constants coupling (drift guard) ---

def test_launcher_sentinel_matches_constant():
    import constants
    launcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "monarchaegis_recv.sh")
    body = open(launcher).read()
    assert constants.TAR_SENTINEL in body
    assert "exec /usr/local/bin/rrsync -wo" in body   # rsync fast path preserved
