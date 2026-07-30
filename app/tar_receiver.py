#!/usr/local/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV
"""Destination-side jailed tar receiver (the tar-stream transport's receive end).

This runs ONLY when the forced-command dispatcher (app/monarchaegis_recv.sh) sees
the tar sentinel in SSH_ORIGINAL_COMMAND; every other request execs rrsync
directly and never reaches this file. So this is a new remote-code surface
reachable only by an already-authenticated, directory-jailed Source key, and it
is deliberately narrow:

  * the Source controls ZERO argv here — it only pipes bytes on stdin (the jail
    directory comes from the trusted forced command, argv[1], never the peer);
  * the whole tarball is buffered and its digest verified BEFORE anything is
    unpacked (the design goal is catching a dropped/truncated transfer, not
    tampering inside the tar, which the SSH channel already authenticates);
  * extraction is hand-rolled — NOT tarfile.extractall — so every member is
    checked before a single byte lands: regular files and directories only,
    with a normalized path that stays inside the jail. Absolute paths, `..`,
    symlinks, hardlinks and device/fifo members are rejected outright, matching
    the guarantees rrsync gives the rsync path. On any rejection nothing is
    written.

Wire protocol on stdin (big-endian lengths):
    [4 bytes]  header length H (<= 64 KiB)
    [H bytes]  header JSON: {"v":1,"algo":"xxh64","digest":"<hex>","size":<n>}
    [n bytes]  the tar archive
One JSON line is written to stdout: {"status":"success","extracted":N} or
{"status":"error","message":"..."}. Exit status is 0 on success and non-zero on
any rejection so the Source's ssh call fails loudly.
"""
import os
from envcompat import env
import sys
import json
import struct
import shutil
import tarfile
import tempfile

# tar_receiver ships in /app alongside constants.py; make it importable when the
# launcher invokes us by absolute path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from constants import TAR_DIGEST_ALGO
except Exception:  # pragma: no cover - constants always ships alongside
    TAR_DIGEST_ALGO = "xxh64"

# Hard cap on a single batch in bytes; 0 disables. Guards the staging filesystem
# against a peer that declares an enormous size.
MAX_TAR_BYTES = int(env("MONARCHAEGIS_TAR_MAX_BYTES", "0"))
MAX_HEADER_BYTES = 64 * 1024
_CHUNK = 1024 * 1024


class Reject(Exception):
    """A validation failure. Nothing is written to the jail when this is raised."""


def _new_digest():
    import xxhash
    if TAR_DIGEST_ALGO != "xxh64":
        raise Reject(f"unsupported digest algo {TAR_DIGEST_ALGO}")
    return xxhash.xxh64()


def _read_exact(stream, n: int) -> bytes:
    """Read exactly n bytes; a short read means the stream was truncated."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise Reject(f"stream ended early: got {len(buf)} of {n} header bytes")
        buf.extend(chunk)
    return bytes(buf)


def _recv_to_tempfile(stream, size: int, staging_dir: str):
    """Stream exactly `size` bytes into a temp file inside the jail, hashing as we
    go. Returns (temp_path, hex_digest). Removes the temp file on any failure."""
    h = _new_digest()
    fd, path = tempfile.mkstemp(prefix=".monarchaegis-recv-", suffix=".tar", dir=staging_dir)
    remaining = size
    ok = False
    try:
        with os.fdopen(fd, "wb") as out:
            while remaining > 0:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    raise Reject(f"stream ended early: {remaining} of {size} tar bytes missing")
                out.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
        ok = True
        return path, h.hexdigest()
    finally:
        if not ok:                       # any failure (Reject, I/O, interrupt) leaves no temp behind
            _quiet_remove(path)


def _reject_unsafe(member: "tarfile.TarInfo", jail_real: str):
    """Enforce the jail on a single tar member before it is extracted."""
    if member.issym() or member.islnk():
        raise Reject(f"link member rejected: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise Reject(f"special member rejected: {member.name}")
    name = (member.name or "").replace("\\", "/")
    if not name or name.startswith("/") or os.path.isabs(member.name):
        raise Reject(f"absolute path rejected: {member.name!r}")
    if any(part == ".." for part in name.split("/")):
        raise Reject(f"parent-ref path rejected: {member.name!r}")
    dest = os.path.realpath(os.path.join(jail_real, name))
    if dest != jail_real and not dest.startswith(jail_real + os.sep):
        raise Reject(f"path escapes jail: {member.name!r}")


def _validate_and_extract(tar_path: str, jail_real: str) -> int:
    """Validate EVERY member first, then extract — so a hostile member anywhere
    in the archive means nothing at all is written. Returns files extracted."""
    extracted = 0
    with tarfile.open(tar_path, "r:*") as tar:
        members = tar.getmembers()
        for m in members:
            _reject_unsafe(m, jail_real)
        for m in members:
            dest = os.path.join(jail_real, m.name)
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest) or jail_real, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                raise Reject(f"unreadable member: {m.name!r}")
            with open(dest, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            os.chmod(dest, 0o644)   # never honor the peer's mode bits (no setuid/setgid)
            extracted += 1
    return extracted


def _quiet_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _reply(stdout, obj: dict):
    stdout.write((json.dumps(obj) + "\n").encode("utf-8"))
    stdout.flush()


def tar_receive(jail: str, stdin=None, stdout=None) -> int:
    """Receive one framed tar stream and extract it under `jail`. `stdin`/`stdout`
    are injectable byte streams for testing (default: the process's own)."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    try:
        hlen = struct.unpack(">I", _read_exact(stdin, 4))[0]
        if not (0 < hlen <= MAX_HEADER_BYTES):
            raise Reject(f"bad header length {hlen}")
        header = json.loads(_read_exact(stdin, hlen).decode("utf-8"))
        size = int(header["size"])
        expected = str(header["digest"])
        algo = header.get("algo", TAR_DIGEST_ALGO)
        if algo != TAR_DIGEST_ALGO:
            raise Reject(f"unsupported digest algo {algo!r}")
        if size < 0 or (MAX_TAR_BYTES and size > MAX_TAR_BYTES):
            raise Reject(f"declared size {size} out of bounds")

        jail_real = os.path.realpath(jail)
        os.makedirs(jail_real, exist_ok=True)
        tar_path, got = _recv_to_tempfile(stdin, size, jail_real)
        try:
            if got != expected:
                raise Reject(f"digest mismatch: expected {expected}, got {got} "
                             "(transfer corrupted/truncated) — nothing extracted")
            extracted = _validate_and_extract(tar_path, jail_real)
        finally:
            _quiet_remove(tar_path)
        _reply(stdout, {"status": "success", "extracted": extracted})
        return 0
    except Reject as e:
        _reply(stdout, {"status": "error", "message": str(e)})
        return 3
    except Exception as e:   # noqa: BLE001 - report anything, never leak a traceback to the peer
        _reply(stdout, {"status": "error", "message": f"receiver error: {e}"})
        return 4


def main(argv) -> int:
    if len(argv) < 2 or not argv[1]:
        sys.stderr.write("usage: tar_receiver.py <jail_dir>\n")
        return 2
    return tar_receive(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
