# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

"""Phase 2: DB-target-driven sync engine (manual "Sync Now").

Runs one replication pass for a target taken from the `targets` table:

  1. gather the Source's hashes for the target,
  2. diff them against the DESTINATION LEDGER via the Client's /api/client/diff
     (the ledger — the Client's file_hashes DB — is authoritative, NOT the
     destination filesystem; that is what keeps re-encoded destination files
     from being re-pushed),
  3. rsync-push only the missing/superseded files (--whole-file, into the
     rrsync jail),
  4. register the transferred files on the destination so its ledger records
     exactly what it received (works even when the Client's inotify watchdog
     can't see FUSE writes),
  5. stamp the outcome on the target row (last_run / last_status / last_summary).

lsyncd and the legacy /verify + /sync_missing endpoints remain the fallback
path until Phase 4; this engine is the new DB-driven path. The HTTP and rsync
calls are isolated in small module functions (_post_json / _run_rsync) so tests
can drive the orchestration without a real peer or SSH.
"""
import os
from envcompat import env
import time
import json
import shutil
import struct
import tarfile
import tempfile
import threading
import subprocess
from datetime import datetime, timezone
from typing import List, Optional

import database
import server_manager
from constants import RSYNC_NO_SECLUDED_ARGS, TAR_SENTINEL, TAR_DIGEST_ALGO

KNOWN_HOSTS_PATH = env("MONARCHAEGIS_KNOWN_HOSTS", "/config/known_hosts")
CLIENT_API_PORT = int(env("MONARCHAEGIS_CLIENT_API_PORT", "5000"))
DIFF_TIMEOUT = int(env("MONARCHAEGIS_DIFF_TIMEOUT", "30"))
REGISTER_TIMEOUT = int(env("MONARCHAEGIS_REGISTER_TIMEOUT", "30"))
# Transport for the push step: "rsync" (per-file, resumable, delta) or "tar"
# (one tar over one ssh connection — far less per-file overhead for a diff that
# is many small files). Phase 1 selects it globally via env; a per-target
# selector + an automatic size/count heuristic land in Phase 2.
TRANSPORT = env("MONARCHAEGIS_TRANSPORT", "rsync").lower()
# Guard against pruning good ledger rows when the SOURCE is unmounted rather than
# genuinely changed: abort if this many files are missing AND they are more than
# this fraction of the batch. A few vanished files is normal media-manager churn.
VANISHED_ABORT_MIN = int(env("MONARCHAEGIS_VANISHED_ABORT_MIN", "10"))
VANISHED_ABORT_RATIO = float(env("MONARCHAEGIS_VANISHED_ABORT_RATIO", "0.5"))


# --- Isolated I/O (monkeypatched in tests) ---

def _post_json(url: str, payload: dict, timeout: int) -> dict:
    """POST JSON and return the parsed response dict. Raises on transport error
    or non-200 status."""
    import requests
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _run_rsync(cmd: list, say=lambda _m: None) -> tuple:
    """Run the rsync push, streaming each output line to `say` (so the UI shows the
    live per-file transfer), and return (returncode, combined_output)."""
    lines = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip("\n")
        if not line:
            continue
        lines.append(line)
        say(f"[Sync] rsync: {line}")
    proc.stdout.close()
    proc.wait()
    return proc.returncode, "\n".join(lines)


# --- Connection resolution ---

def _resolve_connection(server: dict):
    """From a servers.json profile, derive the pieces a push needs.

    Returns (host_ip, api_base, physical_dest, rsh_arg). The physical rsync
    destination is the rrsync jail root "user@host:/" (the jail maps it to the
    real receiving dir); the Client API lives at http://host:5000.
    """
    host = server["host"]
    user = server.get("user", "root")
    port = int(server.get("port", 22))
    key_id = server.get("key_id")

    api_base = f"http://{host}:{CLIENT_API_PORT}"
    physical_dest = f"{user}@{host}:/"

    rsh = f"ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={KNOWN_HOSTS_PATH}"
    if key_id:
        key_path = os.path.join(server_manager.KEYS_DIR, key_id).replace("\\", "/")
        rsh += f" -o IdentityFile={key_path}"
    rsh += f" -p {port}"
    return host, api_base, physical_dest, f"--rsh={rsh}"


def _build_push_command(source_path: str, physical_dest: str, files_from: str, rsh_arg: str) -> list:
    """rsync push of an explicit file list into the rrsync jail. Mirrors the
    legacy sync_missing builder: -W (whole-file, destinations are re-encoded so
    delta is useless), --no-secluded-args (rrsync rejects -s), --files-from.
    Logging flags are omitted — rrsync rejects remote-writing flags (code 12)."""
    src = source_path if source_path.endswith("/") else source_path + "/"
    dest = physical_dest if physical_dest.endswith("/") else physical_dest + "/"
    return ["rsync", "-rvW", RSYNC_NO_SECLUDED_ARGS, f"--files-from={files_from}",
            rsh_arg, src, dest]


# --- Tar-stream transport (source side) ---

def _ssh_base_argv(server: dict) -> list:
    """The ssh argv (minus the remote command) for a direct connection to the
    destination — same host/user/port/key/known-hosts policy the rsync --rsh
    uses, but as a plain argv so we can pipe a tar stream through it."""
    host = server["host"]
    user = server.get("user", "root")
    port = int(server.get("port", 22))
    key_id = server.get("key_id")
    argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}"]
    if key_id:
        key_path = os.path.join(server_manager.KEYS_DIR, key_id).replace("\\", "/")
        argv += ["-o", f"IdentityFile={key_path}"]
    argv += ["-p", str(port), f"{user}@{host}"]
    return argv


def _build_tar(source_path: str, missing: list):
    """Tar exactly the `missing` files (relative to the source root) into a temp
    archive and hash the finished archive.
    Returns (tar_path, hex_digest, size, included, vanished).

    Files that have disappeared since the diff are SKIPPED, not fatal: media
    managers (Sonarr/Radarr) delete-and-replace on every quality upgrade, so a
    path from the diff routinely no longer exists by transfer time. rsync just
    warns and carries on; we do the same rather than losing the whole batch to
    one stale path. `included` is what actually made it into the archive — the
    caller must register ONLY those, or the destination ledger would claim files
    it never received.

    Regular files only: symlinked source files are dereferenced to their content
    (matching rsync's default), so the archive never carries a link member the
    receiver would reject. The caller must delete tar_path."""
    src_root = source_path.rstrip("/")
    included, vanished = [], []
    fd, tar_path = tempfile.mkstemp(prefix="synctar_", suffix=".tar")
    os.close(fd)
    try:
        with tarfile.open(tar_path, "w") as tar:
            for p in missing:
                rel = p.lstrip("/")
                full = os.path.join(src_root, rel)
                try:
                    if os.path.islink(full):
                        full = os.path.realpath(full)
                    tar.add(full, arcname=rel, recursive=False)
                    included.append(p)
                except (FileNotFoundError, NotADirectoryError):
                    # Renamed/deleted since the diff. Other OSErrors (permission,
                    # I/O) still propagate — those are real problems, not churn.
                    vanished.append(p)
    except BaseException:
        _quiet_remove(tar_path)
        raise
    h = _hash_file(tar_path)
    return tar_path, h, os.path.getsize(tar_path), included, vanished


def _quiet_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _hash_file(path: str) -> str:
    import xxhash
    h = xxhash.xxh64()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stream_to_receiver(argv: list, header: bytes, tar_path: str):
    """Run the ssh session and pipe [header][tar] to the receiver. stdin is fed
    on a separate thread while stdout/stderr are read here, so a large archive
    can't deadlock against a full output pipe. Returns (rc, stdout, stderr)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _feed():
        try:
            proc.stdin.write(struct.pack(">I", len(header)))
            proc.stdin.write(header)
            with open(tar_path, "rb") as f:
                shutil.copyfileobj(f, proc.stdin, 1024 * 1024)
        except (BrokenPipeError, OSError):
            pass   # receiver rejected the stream and closed early; reason is on stdout
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=_feed, daemon=True)
    writer.start()
    out = proc.stdout.read().decode("utf-8", "replace")
    err = proc.stderr.read().decode("utf-8", "replace").strip()
    rc = proc.wait()
    writer.join()
    return rc, out, err


def _run_tar_transfer(target_id, source_path, server, missing, say=lambda _m: None):
    """Push the `missing` set as a single integrity-checked tar over one ssh
    connection into the destination's jailed tar receiver.

    Returns (included, vanished): the files actually sent, and the ones that had
    disappeared before transfer. Raises SyncError if the receiver reports
    anything but success (digest mismatch, rejected member, transport failure),
    or if so much of the batch is missing that the source looks unmounted."""
    try:
        tar_path, digest, size, included, vanished = _build_tar(source_path, missing)
    except OSError as e:
        raise SyncError(f"tar-stream build failed: {e}")

    if vanished:
        say(f"WARN: [Sync] {len(vanished)} file(s) vanished since the diff "
            f"(renamed/upgraded?) — skipping them.")
    # A handful of vanished files is normal churn. A batch that is mostly gone
    # means the source is unmounted/unreachable, NOT that the library changed —
    # bail out rather than transferring a fragment and pruning good ledger rows.
    if len(vanished) >= VANISHED_ABORT_MIN and len(vanished) > VANISHED_ABORT_RATIO * len(missing):
        _quiet_remove(tar_path)
        raise SyncError(
            f"{len(vanished)} of {len(missing)} source files are missing — "
            "aborting (is the source share mounted?). Nothing transferred or pruned.")
    if not included:
        _quiet_remove(tar_path)
        say("[Sync] tar-stream: nothing left to send (all candidates vanished).")
        return [], vanished

    header = json.dumps({"v": 1, "algo": TAR_DIGEST_ALGO,
                         "digest": digest, "size": size}).encode("utf-8")
    argv = _ssh_base_argv(server) + [TAR_SENTINEL]
    say(f"[Sync] tar-stream: {len(included)} file(s), {size} bytes, {TAR_DIGEST_ALGO}={digest}")
    try:
        rc, out, err = _stream_to_receiver(argv, header, tar_path)
    finally:
        _quiet_remove(tar_path)

    result = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
            except ValueError:
                pass
    if rc != 0 or not result or result.get("status") != "success":
        detail = (result or {}).get("message") or err or f"ssh/tar exited {rc}"
        raise SyncError(f"tar-stream failed; destination not updated. ({detail})")
    say(f"[Sync] tar-stream: destination extracted {result.get('extracted')} file(s).")
    return included, vanished


def _prune_vanished(db, target_id, source_hashes, vanished, say=lambda _m: None) -> int:
    """Forget source ledger rows whose files no longer exist, so they stop being
    offered on every subsequent diff.

    A hard remove (not a tombstone): tombstoning is the CLIENT-side "don't let the
    source restore what I deleted" marker, whereas on the SOURCE a file that is
    gone should simply not be advertised. This is what keeps the ledger honest on
    Unraid, where the inotify watchdog never sees deletes through the FUSE share
    and orphans would otherwise survive until a full rescan."""
    by_path = {r["filepath"]: r for r in source_hashes}
    pruned = 0
    for p in vanished:
        rec = _source_hash_for(p, by_path)
        target_path = rec["filepath"] if rec else p.lstrip("/")
        try:
            db.remove_file_hash(target_id, target_path)
            pruned += 1
        except Exception as e:
            say(f"WARN: [Sync] could not prune stale ledger row {target_path}: {e}")
    if pruned:
        say(f"[Sync] Pruned {pruned} stale ledger row(s) for files no longer on disk.")
    return pruned


# --- Orchestration ---

class SyncError(Exception):
    """A step failed in a way that aborts the sync run (recorded as last_status=error)."""


def _source_hash_for(path: str, by_path: dict):
    """Look up the Source's hash record for a diff path, tolerating the leading
    slash the diff may or may not carry."""
    return by_path.get(path) or by_path.get(path.lstrip("/"))


def _get_missing_files(api_base, target_id, dest_path, source_hashes, algo) -> list:
    """Diff the Source's hashes against the destination ledger; return the paths
    the destination is missing or has an older version of. Raises SyncError."""
    try:
        diff = _post_json(f"{api_base}/api/client/diff", {
            "target_id": target_id, "target_path": dest_path,
            "source_hashes": source_hashes, "source_hash_algo": algo,
        }, DIFF_TIMEOUT)
    except Exception as e:
        raise SyncError(f"Diff request failed: {e}")
    if diff.get("status") != "success":
        raise SyncError(f"Client diff not ready/failed: {diff.get('message') or diff.get('status')}")
    return diff.get("missing_files", [])


def _run_transfer(target_id, source_path, physical_dest, missing, rsh_arg, say=lambda _m: None):
    """rsync-push exactly the `missing` set into the rrsync jail via --files-from.
    Streams live output; raises SyncError on a non-zero rsync exit with the most
    telling line (auth / host-key / rrsync errors) so the failure is diagnosable
    from the UI instead of a bare exit code."""
    fd, tmp = tempfile.mkstemp(prefix=f"syncnow_{target_id}_", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            for p in missing:
                f.write(f"{p.lstrip('/')}\n")   # relative to the source dir
        rc, out = _run_rsync(_build_push_command(source_path, physical_dest, tmp, rsh_arg), say)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if rc != 0:
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        hint = next((ln for ln in reversed(lines) if any(k in ln.lower() for k in (
                    "denied", "rrsync", "host key", "identity file", "connection",
                    "refused", "unauthorized", "no such"))),
                    lines[-1] if lines else "")
        raise SyncError(f"rsync exited {rc}; destination not updated."
                        + (f" ({hint})" if hint else ""))


def _register_results(api_base, target_id, dest_path, source_hashes, missing, say) -> tuple:
    """Tell the destination which files it just received (with the SOURCE's
    hashes) so its ledger records them. Best-effort — returns (registered_count,
    note); a failure is surfaced in `note`, not raised, since the transfer already
    succeeded."""
    by_path = {r["filepath"]: r for r in source_hashes}
    reg_files = []
    for p in missing:
        rec = _source_hash_for(p, by_path)
        if rec:
            reg_files.append({"filepath": rec["filepath"], "size": rec["size"],
                              "mtime": rec["mtime"], "file_hash": rec["file_hash"]})
    if not reg_files:
        return 0, ""
    try:
        r = _post_json(f"{api_base}/api/client/register", {
            "target_id": target_id, "target_path": dest_path, "files": reg_files,
        }, REGISTER_TIMEOUT)
        return r.get("registered", 0), ""
    except Exception as e:
        say(f"WARN: [Sync] register failed: {e}")
        return 0, f" (WARNING: ledger register failed: {e})"


def sync_now(target_id: str, db=None, server_mgr=None, log=None) -> dict:
    """Run one manual sync for a DB target — a straight pipeline of diff ->
    transfer -> register -> record. Returns {"status", "transferred", "message"}.
    `db`/`server_mgr` default to the singletons (injected in tests); `log` is an
    optional callable(str) for progress lines."""
    db = db or database.db
    server_mgr = server_mgr or server_manager.ServerManager()
    say = log or (lambda _m: None)

    target = db.get_target(target_id)
    if not target:
        return {"status": "error", "transferred": 0, "message": "Target not found."}
    if not target["enabled"]:
        return {"status": "error", "transferred": 0, "message": "Target is disabled."}
    server = server_mgr.get_server(target["server_id"]) if target.get("server_id") else None
    if not server:
        msg = "Target has no paired server; cannot sync (pair it first)."
        db.record_target_run(target_id, "error", msg)
        return {"status": "error", "transferred": 0, "message": msg}

    _host, api_base, physical_dest, rsh_arg = _resolve_connection(server)
    dest_path, source_path = target.get("dest_path"), target["source_path"]
    source_hashes = db.get_all_hashes(target_id)

    try:
        say(f"[Sync] Diffing {len(source_hashes)} source files against destination ledger...")
        missing = _get_missing_files(api_base, target_id, dest_path, source_hashes, db.HASH_ALGO)
        if not missing:
            db.record_target_run(target_id, "success", "Up to date: 0 files to transfer.")
            say("[Sync] Destination already up to date — nothing to transfer.")
            return {"status": "success", "transferred": 0, "message": "Up to date."}

        say(f"[Sync] {len(missing)} file(s) to transfer via {TRANSPORT}.")
        if TRANSPORT == "tar":
            sent, vanished = _run_tar_transfer(target_id, source_path, server, missing, say)
        else:
            _run_transfer(target_id, source_path, physical_dest, missing, rsh_arg, say)
            sent, vanished = missing, []
    except SyncError as e:
        db.record_target_run(target_id, "error", str(e))
        say(f"WARN: [Sync] {e}")
        return {"status": "error", "transferred": 0, "message": str(e)}

    # Self-heal: forget rows for files that are gone, so they stop being offered
    # on every future diff (see _prune_vanished — the watchdog can't see deletes
    # through a FUSE share).
    if vanished:
        _prune_vanished(db, target_id, source_hashes, vanished, say)

    # Record what was ACTUALLY sent — feeds the per-day transfer counter in the UI.
    db.record_transfers(target_id, sent)

    # Register only the files that really transferred; registering the whole diff
    # would make the destination ledger claim files it never received, and they
    # would then be skipped forever.
    registered, reg_note = _register_results(api_base, target_id, dest_path,
                                             source_hashes, sent, say)
    summary = f"Transferred {len(sent)} file(s); destination recorded {registered}.{reg_note}"
    if vanished:
        summary += f" Skipped {len(vanished)} vanished file(s)."
    db.record_target_run(target_id, "success", summary)
    say(f"[Sync] {summary}")
    return {"status": "success", "transferred": len(sent), "message": summary}


# --- Scheduling (Phase 3) ---
#
# Interval presets the UI offers (seconds); interval_seconds == 0 means manual.
# Consumed by the Phase 5 schedule-selector UI (and asserted by the tests); kept
# here as the single source of truth for the preset -> seconds mapping.
INTERVAL_PRESETS = {"manual": 0, "1h": 3600, "6h": 21600, "12h": 43200, "daily": 86400}
# Smallest non-manual interval. Sub-tick intervals are pointless (the scheduler
# ticks ~every 60s) and risk hammering the destination, so they're rejected.
MIN_INTERVAL_SECONDS = 60


def _resolve_now(now: Optional[float] = None) -> float:
    return time.time() if now is None else now


def local_midnight_utc(now: Optional[float] = None) -> str:
    """Start of TODAY in the container's local timezone (TZ env), expressed as a
    UTC 'YYYY-MM-DD HH:MM:SS' string comparable to SQLite CURRENT_TIMESTAMP.
    Used for the "transferred today" counter — querying since this instant makes
    the count reset at local midnight with no reset job."""
    local_now = datetime.fromtimestamp(_resolve_now(now)).astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_sql_timestamp(ts: str) -> float:
    """SQLite CURRENT_TIMESTAMP is naive UTC 'YYYY-MM-DD HH:MM:SS' -> epoch secs."""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def next_run_at(target: dict, now: Optional[float] = None) -> Optional[float]:
    """Epoch seconds when this target is next due to auto-run, or None if it never
    auto-runs (disabled, or manual interval_seconds <= 0). A target that has never
    run is due immediately."""
    now = _resolve_now(now)
    if not target.get("enabled") or (target.get("interval_seconds") or 0) <= 0:
        return None
    last = target.get("last_run")
    if not last:
        return now
    try:
        return _parse_sql_timestamp(last) + target["interval_seconds"]
    except (ValueError, TypeError):
        # A corrupt/wrong-typed last_run -> treat as due now (fail toward syncing,
        # not toward silently never syncing). This self-heals: the resulting sync
        # calls record_target_run(), overwriting last_run with a valid timestamp,
        # so the next tick parses cleanly. The "sync" job dedup prevents any
        # hammering in the meantime.
        return now


def is_due(target: dict, now: Optional[float] = None) -> bool:
    now = _resolve_now(now)
    nr = next_run_at(target, now)
    return nr is not None and nr <= now


def due_targets(targets: List[dict], now: Optional[float] = None) -> List[str]:
    """The ids of all targets due for an auto-run right now."""
    now = _resolve_now(now)
    return [t["id"] for t in targets if is_due(t, now)]
