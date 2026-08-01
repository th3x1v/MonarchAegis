# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

import os
from envcompat import env
import asyncio
import base64
import secrets
import time
import threading
import concurrent.futures
from collections import OrderedDict
from typing import List, Optional
import shutil
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel
import json

from config_manager import ConfigManager
from constants import RSYNC_NO_SECLUDED_ARGS
from log_router import LogRouter
from server_manager import ServerManager
from database import db
from hash_scanner import hash_scanner_daemon
import sync_engine

app = FastAPI(title="MonarchAegis Controller", description="MonarchAegis — self-hosted data-protection controller")

# --- Authentication ---
# Set MONARCHAEGIS_PASSWORD to enable HTTP Basic Auth. If unset, auth is disabled (default).
# Set MONARCHAEGIS_USERNAME to change the username (default: "admin").
# P2P endpoints used for machine-to-machine calls between Source/Client containers are exempt.
_AUTH_PASSWORD = env("MONARCHAEGIS_PASSWORD", "")
_AUTH_USERNAME = env("MONARCHAEGIS_USERNAME", "admin")
_AUTH_EXEMPT_PATHS = {"/api/client/pair", "/api/client/diff", "/api/client/register"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _AUTH_PASSWORD and request.url.path not in _AUTH_EXEMPT_PATHS:
        auth_header = request.headers.get("Authorization", "")
        authed = False
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
                authed = (
                    secrets.compare_digest(username, _AUTH_USERNAME)
                    and secrets.compare_digest(password, _AUTH_PASSWORD)
                )
            except Exception:
                pass
        if not authed:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="MonarchAegis"'},
            )
    return await call_next(request)

# Known-hosts file for SSH host key pinning (Trust on First Use)
KNOWN_HOSTS_PATH = env("MONARCHAEGIS_KNOWN_HOSTS", "/config/known_hosts")

# H1: Allowlist of base paths the local file browser may traverse.
# Override via MONARCHAEGIS_BROWSE_ALLOWED_PATHS (comma-separated).
_LOCAL_BROWSE_ALLOWED_PREFIXES = [
    p.strip()
    for p in env("MONARCHAEGIS_BROWSE_ALLOWED_PATHS", "/source_data,/mnt,/data").split(",")
    if p.strip()
]

def _path_browseable(real_path: str) -> bool:
    """Returns True if real_path may be listed by the local file browser.

    Allows two cases:
    1. The path is at or under a configured allowed prefix (destination access).
    2. The path is an ancestor of a configured prefix (navigation access) — so
       the UI can start at '/' and drill down to '/mnt/user/movies' without
       every intermediate directory needing to be explicitly listed.

    Example with prefix '/mnt/user':
      '/'          → allowed (ancestor)
      '/mnt'       → allowed (ancestor)
      '/mnt/user'  → allowed (exact match)
      '/mnt/user/movies' → allowed (under prefix)
      '/etc'       → denied (not ancestor of any prefix, not under any prefix)
    """
    for prefix in _LOCAL_BROWSE_ALLOWED_PREFIXES:
        clean = prefix.rstrip(os.sep) or os.sep
        # Case 1: real_path is within the allowed subtree.
        if real_path == clean or real_path.startswith(clean + os.sep):
            return True
        # Case 2: real_path is an ancestor of this prefix (navigation path).
        # e.g. '/' and '/mnt' are ancestors of '/mnt/user'.
        # NOTE: do NOT use `or os.sep` here — for root ('/'), rstrip yields '' and
        # the fallback would produce '//', which fails the startswith and wrongly
        # denies browsing from '/' (the destination's Add-Directory browser starts
        # there). Plain rstrip + sep normalizes '/' -> '/' and '/mnt' -> '/mnt/'.
        if clean != os.sep and (clean + os.sep).startswith(
            real_path.rstrip(os.sep) + os.sep
        ):
            return True
    return False

# Optional shared secret for authenticated machine-to-machine pair calls.
# Set the same value on both source and client containers via MONARCHAEGIS_PAIR_SECRET.
# Callers that present this secret via X-Pair-Secret header bypass the IP rate limit.
_PAIR_SECRET = env("MONARCHAEGIS_PAIR_SECRET", "")

# H2: In-memory rate limiter for /api/client/pair.
# Keyed on client IP only — caller-supplied fields (target_id) cannot mint new buckets.
# Uses an OrderedDict as an LRU cache: most-recently-seen IPs move to the end;
# eviction always removes expired buckets first, then the oldest (least-recently-seen) IP.
# Limits are configurable via env vars for sites with large bootstrap flows.
_pair_timestamps: OrderedDict = OrderedDict()
_PAIR_RATE_MAX = int(env("MONARCHAEGIS_PAIR_RATE_MAX", "10"))
_PAIR_RATE_WINDOW = float(env("MONARCHAEGIS_PAIR_RATE_WINDOW", "60"))
# Max distinct source IPs tracked. When full, expired IPs are evicted first;
# if all are active the oldest (LRU) IP is evicted so new IPs are never hard-blocked.
_PAIR_RATE_MAX_KEYS = int(env("MONARCHAEGIS_PAIR_RATE_MAX_KEYS", "2000"))

def _check_pair_rate_limit(client_ip: str) -> bool:
    now = time.monotonic()

    if client_ip not in _pair_timestamps and len(_pair_timestamps) >= _PAIR_RATE_MAX_KEYS:
        # Prefer evicting fully-expired buckets; fall back to evicting the oldest (LRU).
        expired = [k for k, ts in _pair_timestamps.items()
                   if not ts or now - max(ts) > _PAIR_RATE_WINDOW]
        if expired:
            for k in expired:
                del _pair_timestamps[k]
        else:
            _pair_timestamps.popitem(last=False)  # Evict LRU — never hard-blocks new IPs.

    # Prune stale timestamps and refresh LRU position.
    current = [t for t in _pair_timestamps.get(client_ip, []) if now - t < _PAIR_RATE_WINDOW]
    if len(current) >= _PAIR_RATE_MAX:
        _pair_timestamps[client_ip] = current
        _pair_timestamps.move_to_end(client_ip)
        return False

    current.append(now)
    _pair_timestamps[client_ip] = current
    _pair_timestamps.move_to_end(client_ip)
    return True

def _safe_key_path(key_path: str) -> str:
    """Resolves and validates that a key path is inside the configured keys directory.
    Raises ValueError if the resolved path escapes the directory."""
    keys_dir = os.path.realpath(env("MONARCHAEGIS_KEYS_DIR", "/config/keys"))
    real = os.path.realpath(key_path)
    if not real.startswith(keys_dir + os.sep) and real != keys_dir:
        raise ValueError(f"Key path escapes keys directory: {key_path}")
    return real

def _safe_port(port) -> str:
    """Validates that a port value is a plain integer string safe for SSH -p."""
    try:
        return str(int(port))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid port value: {port}")

# Initialize backend services
config_mgr = ConfigManager()
log_router = LogRouter()
server_mgr = ServerManager()

# --- Bounded background-job pool ---
# verify (P2P diff) and sync_missing (recovery push) run in the background so the
# HTTP call returns immediately. They used to each spawn an UNBOUNDED daemon
# thread per call; when the client is slow/blocked those threads pile up 1:1 with
# the number of calls and can exhaust the container's PID limit
# (fork: Resource temporarily unavailable / EAGAIN). Run them on a small fixed
# pool and dedupe per (target, job type) so a burst can never blow the PID budget.
_JOB_WORKERS = int(env("MONARCHAEGIS_JOB_WORKERS", "4"))
_job_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_JOB_WORKERS, thread_name_prefix="ma_job"
)
_active_jobs = set()  # {(target_id, job_type)}
_active_jobs_lock = threading.Lock()

def _submit_job(target_id: str, job_type: str, fn, *args) -> bool:
    """Runs fn(*args) on the bounded job pool. Returns False (and does not submit)
    if a job of the same type is already active for this target — this both caps
    concurrency and prevents pointless duplicate verify/sync runs piling up."""
    key = (target_id, job_type)
    with _active_jobs_lock:
        if key in _active_jobs:
            return False
        _active_jobs.add(key)

    def _wrapped():
        try:
            fn(*args)
        finally:
            with _active_jobs_lock:
                _active_jobs.discard(key)

    _job_executor.submit(_wrapped)
    return True

def get_current_role() -> str:
    """Returns the current container mode: 'source' or 'client'."""
    return db.get_setting("role", env("MONARCHAEGIS_ROLE", "source")).lower()


def _api_base_for_host(host_ip: str) -> str:
    """Client API root for a bare host, honoring the matching server profile's
    optional `api_port`. Used by the legacy verify/recovery paths, which only
    carry a host string; the DB-driven engine resolves this from the profile
    directly (sync_engine.client_api_base)."""
    try:
        for s in server_mgr.list_servers():
            if host_ip in (s.get("host"), f"{s.get('user')}@{s.get('host')}"):
                return sync_engine.client_api_base(s)
    except Exception:
        pass
    return f"http://{host_ip}:{sync_engine.CLIENT_API_PORT}"

# Ensure log paths exist before starting anything
import log_router as log_router_module
LOG_PATH = log_router_module.LOG_PATH
STATUS_PATH = log_router_module.STATUS_PATH

@app.on_event("startup")
async def startup_event():
    # 1. Create log directories and empty files if they don't exist
    log_dir = os.path.dirname(LOG_PATH)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    if not os.path.exists(LOG_PATH):
        open(LOG_PATH, 'a').close()
    if not os.path.exists(STATUS_PATH):
        open(STATUS_PATH, 'a').close()
        
    print(f"Ensured log files exist: {LOG_PATH}")

    # Ensure the config file exists
    config_mgr._ensure_config_exists()

    # Phase 1 (DB-driven replication): one-shot migrate the Lua targets + unpaired
    # sources into the authoritative `targets` table. Idempotent (guarded by the
    # 'targets_migrated' setting). Nothing reads this table yet — lsyncd still
    # drives replication — so this changes no behavior; it just prepares the store
    # that later phases will switch over to.
    try:
        # Run off the event loop: the DB is on the FUSE /config volume and the
        # commit's fsync can block. Gather inputs and do the write in a thread.
        def _run_targets_migration():
            parsed = config_mgr.parse_targets()
            servers = server_mgr.list_servers()
            n = db.migrate_targets_from_config(
                parsed, json.loads(db.get_setting("unpaired_sources", "[]")), servers)
            # One-time repair for targets migrated by the earlier host-based mapping,
            # which mis-assigned the server when several targets share a host. Now
            # resolved by key; preserves each target's schedule.
            corrected = db.reresolve_target_servers(parsed, servers)
            # One-time: upgrade existing rrsync forced-command keys to the tar
            # dispatcher so the tar-stream transport works without re-pairing.
            # Safe superset (dispatcher execs rrsync for rsync); guarded so it
            # runs once, and self-healing since it only touches rrsync entries.
            upgraded = 0
            if db.get_setting("authorized_keys_dispatcher_upgraded") != "1":
                upgraded = server_mgr.upgrade_authorized_keys_to_dispatcher()
                db.set_setting("authorized_keys_dispatcher_upgraded", "1")
            return n, corrected, upgraded
        migrated, corrected, upgraded = await asyncio.to_thread(_run_targets_migration)
        if migrated:
            print(f"Phase1 migration: populated {migrated} target(s) into the targets table.")
        if corrected:
            print(f"Server re-resolve: corrected {corrected} target(s) to their key-matched server.")
        if upgraded:
            print(f"Key upgrade: rewired {upgraded} paired key(s) to the tar dispatcher "
                  "(rsync unchanged; tar-stream now available without re-pairing).")
    except Exception as e:
        import traceback
        print(f"WARNING: Phase1 targets migration failed (non-fatal): {e}\n{traceback.format_exc()}")

    # SAFE MODE: set MONARCHAEGIS_SAFE_MODE=1 to bring up the web UI only — no
    # scheduler and no hash scanner. The hash scanner reads every file; on a huge
    # tree behind a FUSE share (Unraid /mnt/user) that I/O storm can make the
    # whole host unresponsive. Safe mode lets you reach the UI to narrow or delete
    # targets, then unset the variable and recreate the container.
    safe_mode = env("MONARCHAEGIS_SAFE_MODE", "").lower() in ("1", "true", "yes")
    if safe_mode:
        print("MONARCHAEGIS_SAFE_MODE is set — the scheduler and hash scanner will NOT "
              "start. Web UI is up for config recovery only.")

    # Phase 4: lsyncd is retired — replication is driven by the DB-target
    # scheduler (below), not a live daemon. No lsyncd launch, and no daemon
    # log-tailer: the per-target SSE log buckets are fed directly by sync_engine
    # / the recovery endpoints via log_router._add_log.

    # Task/FD/thread accounting monitor — runs in every mode so it can attribute
    # a pids.current climb correctly. Cheap; one log line per interval.
    asyncio.create_task(_resource_monitor())

    if safe_mode:
        return

    # Phase 3: per-target scheduled syncs (source-mode; gated inside the loop).
    asyncio.create_task(_scheduler_loop())

    # 4. Start the file hasher / watchdog daemon (deferred to background)
    #    This MUST run after the server is already listening so the UI is available
    #    while potentially millions of files are being hashed on first boot.
    async def _deferred_hash_init():
        await asyncio.sleep(1)  # Let uvicorn finish binding the port

        # Targets whose baseline the operator deferred stay deferred across
        # restarts — re-registering them must not trigger the scan they
        # explicitly postponed.
        def _defer(tid: str) -> bool:
            return hash_scanner_daemon.is_baseline_pending(tid)

        # Source mode: hash every target in the authoritative DB store (both
        # paired targets and disabled/unpaired sources carry a source_path). This
        # replaces the old parse_targets() + unpaired_sources loops now that the
        # targets table — not the Lua config — is the source of truth.
        for target in db.list_targets():
            if target.get("source_path"):
                hash_scanner_daemon.add_target(target["id"], target["source_path"],
                                               defer_baseline=_defer(target["id"]))

        # Client mode: also load any previously saved Client receiving directories
        raw_client_targets = db.get_setting("client_targets", "[]")
        try:
            client_targets = json.loads(raw_client_targets)
        except json.JSONDecodeError:
            client_targets = []
        for ct in client_targets:
            hash_scanner_daemon.add_target(ct["id"], ct["path"], defer_baseline=_defer(ct["id"]))

        hash_scanner_daemon.start()
        print("HashScanner: Background initialization complete.")

    asyncio.create_task(_deferred_hash_init())
    # NOTE: the lsyncd log-size guard (_log_size_guard) and _truncate_keep_tail
    # were removed in Phase 4 — with lsyncd retired there is no daemon log that
    # can run away and fill the disk.


async def _scheduler_tick() -> List[str]:
    """One scheduler pass: on the source, submit a sync for each due DB target on
    the bounded job pool (the "sync" job key dedups against a manual Sync Now /
    sync_missing already running). Returns the ids actually submitted (for tests
    and logging). Source-mode only — the targets table is the source's push list;
    on a client it's empty, but we gate explicitly."""
    if get_current_role() != "source":
        return []
    targets = await asyncio.to_thread(db.list_targets)
    submitted = []
    for tid in sync_engine.due_targets(targets, time.time()):
        def _run(tid=tid):
            log_router._ensure_bucket_exists(tid)
            log_router._add_log(tid, "[Scheduler] Interval elapsed — starting sync.")
            sync_engine.sync_now(tid, server_mgr=server_mgr,
                                 log=lambda m: log_router._add_log(tid, m))
        if _submit_job(tid, "sync", _run):
            submitted.append(tid)
    return submitted


async def _scheduler_loop():
    """(Phase 3) Drives _scheduler_tick on a fixed cadence. Timing/coordination
    only; the decision + submission live in _scheduler_tick. Cancelled cleanly on
    shutdown (uvicorn cancels the task during the sleep).

    Tick via MONARCHAEGIS_SCHEDULER_TICK_SEC (default 60; 0 disables the scheduler).
    """
    tick = int(env("MONARCHAEGIS_SCHEDULER_TICK_SEC", "60"))
    if tick <= 0:
        print("Scheduler disabled (MONARCHAEGIS_SCHEDULER_TICK_SEC=0).")
        return
    while True:
        await asyncio.sleep(tick)
        # NOTE: only Exception is caught — asyncio.CancelledError (BaseException)
        # propagates so the task ends cleanly on shutdown. A bad tick is logged
        # with a traceback and the loop continues (a background scheduler must not
        # die on one failed pass).
        try:
            await _scheduler_tick()
        except Exception as e:
            import traceback
            print(f"WARNING: scheduler tick failed (non-fatal): {e}\n{traceback.format_exc()}")


async def _resource_monitor():
    """Periodically logs task/FD/thread counts so a pids.current climb can be
    attributed correctly.

    This is the right instrument for the container pinning at its pids limit —
    NOT a memory profiler (memory is not the issue). Each line reports:
      py_threads          — Python threads in this process (our own thread usage)
      open_fds            — open file descriptors (file/socket leak detector)
      child_procs         — live subprocess descendants (rsync/ssh/lsyncd/ffprobe)
      container_real_tasks— actual live tasks across the whole container
      cgroup_pids_current — the kernel counter that actually gates fork()

    If cgroup_pids_current climbs while container_real_tasks stays flat, the
    kernel's per-container pids counter is leaking above the real task count
    (fed by process churn) — nothing is accumulating in our code. If instead
    py_threads / open_fds / child_procs climb, that's a real leak we can fix.

    Interval via MONARCHAEGIS_RESOURCE_MONITOR_SEC (default 60; 0 disables).
    """
    interval = int(env("MONARCHAEGIS_RESOURCE_MONITOR_SEC", "60"))
    if interval <= 0:
        return

    def _read_first(paths):
        for p in paths:
            try:
                with open(p) as f:
                    return f.read().strip()
            except OSError:
                continue
        return "?"

    while True:
        await asyncio.sleep(interval)
        try:
            py_threads = threading.active_count()
            try:
                open_fds = len(os.listdir("/proc/self/fd"))
            except OSError:
                open_fds = -1

            # Real live tasks (threads) across every process in the container's
            # PID namespace — compare this against cgroup_pids_current.
            real_tasks = 0
            try:
                for entry in os.listdir("/proc"):
                    if entry.isdigit():
                        try:
                            real_tasks += len(os.listdir(f"/proc/{entry}/task"))
                        except OSError:
                            pass
            except OSError:
                real_tasks = -1

            cgroup_pids = _read_first(
                ("/sys/fs/cgroup/pids.current", "/sys/fs/cgroup/pids/pids.current")
            )

            try:
                import psutil
                child_procs = len(psutil.Process().children(recursive=True))
            except Exception:
                child_procs = -1

            print(
                f"ResourceMonitor: py_threads={py_threads} open_fds={open_fds} "
                f"child_procs={child_procs} container_real_tasks={real_tasks} "
                f"cgroup_pids_current={cgroup_pids}",
                flush=True,
            )
        except Exception as e:
            print(f"ResourceMonitor error: {e}", flush=True)


@app.on_event("shutdown")
async def shutdown_event():
    hash_scanner_daemon.stop()

# Serve the static HTML/CSS/JS files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_dashboard():
    """Serve the main Global Command Center dashboard."""
    return FileResponse("static/index.html")

@app.get("/target")
async def serve_target_view():
    """Serve the detailed Target View page."""
    return FileResponse("static/target.html")

# --- API Endpoints --- #

class TargetModel(BaseModel):
    name: str
    source: str
    target: str
    unpaired_id: Optional[str] = None
    # The exact key this target pairs with. Pinning it (the pairing flow has it)
    # disambiguates multiple server profiles on the same host — without it the
    # config writer falls back to host-match and picks the first, cross-wiring
    # same-host targets onto one key/jail.
    key_id: Optional[str] = None

class SourceTargetModel(BaseModel):
    name: str
    source: str
    # False = register only; baseline hashing waits for an explicit start.
    scan_now: bool = True

class KeyModel(BaseModel):
    name: str
    private_key: str

class ServerModel(BaseModel):
    alias: str
    host: str
    user: str
    port: int
    key_id: str
    # The Client's web UI / API port. Optional — None means "use the default"
    # (MONARCHAEGIS_CLIENT_API_PORT, itself defaulting to 5000). Set per-profile
    # so several Clients on different ports can be paired from one Source.
    api_port: Optional[int] = None

# H3: Response schemas for P2P client API calls — validate before accessing fields.
# extra='ignore' tolerates unknown fields from newer peer versions.
# All fields are optional-with-defaults so a partial response from an older peer
# parses successfully rather than raising — callers check status and act accordingly.
class ConflictItem(BaseModel):
    class Config:
        extra = 'ignore'
    filepath: str = ""
    reason: str = ""
    client_mtime: Optional[float] = None
    source_mtime: Optional[float] = None

class ClientDiffResponse(BaseModel):
    class Config:
        extra = 'ignore'
    status: str = "unknown"
    missing_files: List[str] = []
    conflicts: List[ConflictItem] = []
    message: Optional[str] = None
    scan_progress: Optional[dict] = None

class ClientPairResponse(BaseModel):
    class Config:
        extra = 'ignore'
    status: str = "unknown"
    private_key: Optional[str] = None
    message: Optional[str] = None

@app.get("/api/health")
async def health_check():
    """Health endpoint. lsyncd is retired (Phase 4) — replication is driven by
    the DB-target scheduler — so there is no daemon to report on."""
    return {"daemon_status": "retired"}

@app.get("/api/config/role")
async def get_role():
    """Returns whether this container is acting as a 'source' or a 'client'."""
    return {"role": get_current_role()}

class RolePayload(BaseModel):
    role: str

@app.post("/api/config/role")
async def set_role(payload: RolePayload):
    """Sets the container's operating role and restarts services if necessary."""
    role = payload.role.lower()
    if role not in ["source", "client"]:
        raise HTTPException(status_code=400, detail="Invalid role. Must be 'source' or 'client'.")
        
    db.set_setting("role", role)
    # lsyncd is retired: nothing to stop/start on a role change. The scheduler
    # loop already gates on role each tick, so switching to client stops pushes
    # and switching to source resumes them on the next tick.
    return {"status": "success", "role": role}

# --- Scan Readiness API --- #

@app.get("/api/scan/status")
async def scan_status():
    """Returns the current hash scan progress for all targets."""
    status = hash_scanner_daemon.get_scan_status()
    all_complete = hash_scanner_daemon.is_scan_complete()
    return {
        "ready": all_complete,
        "targets": status
    }

@app.post("/api/scan/force")
async def force_rescan_all():
    """Clears all file hash records and forces a full re-baseline of every active target.
    Use this when one side has re-hashed and you need the other side to match."""
    count = hash_scanner_daemon.force_rescan()
    return {"status": "success", "targets_queued": count}

@app.post("/api/target/{target_id}/rescan")
async def force_rescan_target(target_id: str):
    """Clears hash records for a single target and forces a full re-baseline."""
    count = hash_scanner_daemon.force_rescan(target_id)
    if count > 0:
        return {"status": "success"}
    return {"status": "error", "message": "Target not active or not found."}

# --- Client Target Path Management --- #

class ClientTargetModel(BaseModel):
    alias: str
    path: str
    # False = register only; baseline hashing waits for an explicit start.
    # Critical for pre-seeded directories (in-situ takeover): an immediate
    # full-content scan of terabytes can saturate the host.
    scan_now: bool = True

@app.get("/api/client/targets")
async def list_client_targets():
    """Returns the Client's configured receiving directories, each annotated with
    `tracked` — how many files the ledger currently holds for it.

    Reported from the ledger rather than the scanner's counters because files
    received from a Source are registered directly into the DB; the scan counters
    only ever describe the last baseline pass, so transfers were invisible in the
    UI even though they had landed."""
    raw = db.get_setting("client_targets", "[]")
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        targets = []
    counts = await asyncio.to_thread(db.count_hashes_by_target)
    for t in targets:
        t["tracked"] = counts.get(t.get("id"), 0)
    return {"targets": targets}

@app.post("/api/client/targets")
async def add_client_target(payload: ClientTargetModel):
    """Adds a new receiving directory on the Client and starts hashing it."""
    raw = db.get_setting("client_targets", "[]")
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        targets = []
    
    import uuid
    target_id = f"client_{uuid.uuid4().hex[:8]}"
    new_target = {"id": target_id, "alias": payload.alias, "path": payload.path}
    targets.append(new_target)
    db.set_setting("client_targets", json.dumps(targets))

    # scan_now=False registers the directory without touching its contents —
    # start the baseline later via POST /api/target/{id}/rescan.
    hash_scanner_daemon.add_target(target_id, payload.path, defer_baseline=not payload.scan_now)

    return {"status": "success", "target": new_target}

@app.delete("/api/client/targets/{target_id}")
async def delete_client_target(target_id: str):
    """Removes a receiving directory from the Client."""
    raw = db.get_setting("client_targets", "[]")
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        targets = []
    
    targets = [t for t in targets if t["id"] != target_id]
    db.set_setting("client_targets", json.dumps(targets))
    hash_scanner_daemon.remove_target(target_id)
    
    return {"status": "success"}

# --- Client API --- #

class PairModel(BaseModel):
    target_id: str
    target_path: str

@app.post("/api/client/pair")
async def client_pair(request: Request, payload: PairModel):
    """
    (Client Mode Only)
    Generates a dedicated SSH keypair for the requesting Source target.
    The key is cryptographically scoped to ONLY allow rsync operations
    inside the specified target_path directory.
    Returns the private key so the Source can authenticate securely.
    """
    client_ip = request.client.host if request.client else "unknown"
    presented_secret = request.headers.get("X-Pair-Secret", "")
    secret_valid = bool(_PAIR_SECRET) and secrets.compare_digest(presented_secret, _PAIR_SECRET)

    # When a shared secret is configured, it is REQUIRED — callers without it are rejected
    # regardless of rate-limit state. This closes the trust boundary: an operator who sets
    # MONARCHAEGIS_PAIR_SECRET gets hard authentication; one who doesn't falls back to rate
    # limiting only (same behavior as before the secret was introduced).
    if _PAIR_SECRET and not secret_valid:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Pair-Secret header.")

    if not secret_valid and not _check_pair_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Max {_PAIR_RATE_MAX} pair requests per {int(_PAIR_RATE_WINDOW)}s per IP."
        )

    if get_current_role() != "client":
        return {"status": "error", "message": "Container is not running in Client mode."}

    # Validate target_path against the filesystem allowlist rather than the
    # client_targets DB table. DB-based validation breaks repair flows on existing
    # clients whose client_targets metadata is empty, stale, or was reset.
    # The allowlist check (same prefixes as the file browser) blocks key scoping to
    # sensitive paths like /etc or /root without requiring prior DB registration.
    real_req_path = os.path.realpath(payload.target_path)
    path_allowed = any(
        real_req_path == prefix.rstrip(os.sep) or
        real_req_path.startswith(prefix.rstrip(os.sep) + os.sep)
        for prefix in _LOCAL_BROWSE_ALLOWED_PREFIXES
    )
    if not path_allowed:
        return {"status": "error", "message": "target_path is not within a configured data directory."}

    try:
        private_key = server_mgr.generate_client_keypair(payload.target_id, payload.target_path)
        return {"status": "success", "private_key": private_key}
    except Exception as e:
        return {"status": "error", "message": str(e)}

class HashFileModel(BaseModel):
    filepath: str
    size: int
    mtime: float
    file_hash: str

class DiffPayloadModel(BaseModel):
    target_id: str
    target_path: Optional[str] = None
    source_hashes: List[HashFileModel]
    # The Source's hashing algorithm/mode. If it doesn't match the Client's,
    # every file would falsely appear different — reject with a clear message
    # rather than silently flagging the whole library for re-copy.
    source_hash_algo: Optional[str] = None

@app.post("/api/client/diff")
async def client_diff(payload: DiffPayloadModel):
    """
    (Client Mode Only)
    Receives a complete SQLite hash table from the Source.
    Compares it against the Client's local `file_hashes` tracking table.
    Returns:
      - missing_files: Files that the Source has, which the Client needs.
      - conflicts: Files the Client needs, BUT the Client's local user has modified them manually.
    """
    if get_current_role() != "client":
        return {"status": "error", "message": "Container is not running in Client mode."}

    # Guard against a hash-mode mismatch between Source and Client. Different
    # modes produce different hashes for identical files, which would flag the
    # whole library as missing. Fail loudly instead.
    if payload.source_hash_algo and payload.source_hash_algo != db.HASH_ALGO:
        return {
            "status": "error",
            "message": (f"Hash mode mismatch: Source uses '{payload.source_hash_algo}', "
                        f"Client uses '{db.HASH_ALGO}'. Set MONARCHAEGIS_HASH_MODE identically "
                        f"on both containers and re-scan.")
        }

    target_id = payload.target_id

    # Decoupling fix: Match the Source's requested folder path to our internal Client ID
    if payload.target_path:
        raw_client_targets = db.get_setting("client_targets", "[]")
        try:
            client_targets = json.loads(raw_client_targets)
            normalized_req_path = payload.target_path.rstrip('/')
            for ct in client_targets:
                if ct["path"].rstrip('/') == normalized_req_path:
                    target_id = ct["id"]
                    break
        except Exception:
            pass

    # READINESS GATE: Block diffs until THIS directory's baseline scan is done.
    # Checked per-target so a deferred/pending directory never blocks diffs
    # for other, fully-scanned directories.
    if not hash_scanner_daemon.is_scan_complete(target_id):
        scan_progress = hash_scanner_daemon.get_scan_status()
        return {
            "status": "not_ready",
            "message": "Client has not completed the baseline hash scan for this directory. "
                       "Start it from the Client UI (deferred baselines must be started manually).",
            "scan_progress": scan_progress
        }

    source_files = payload.source_hashes
    
    # Get the Client's current known state from SQLite
    client_files = db.get_all_hashes(target_id)
    
    # Index Client files by filepath for O(1) lookups
    client_dict = {f["filepath"]: f for f in client_files}
    
    missing_files = []
    conflicts = []
    
    # 1. Compare Source to Client (What do we need to download?)
    for src_file in source_files:
        path = src_file.filepath
        
        needs_update = False
        
        if path not in client_dict:
            # Entirely new file
            needs_update = True
        else:
            client_file = client_dict[path]
            # Mismatched hash means the Source version is different
            if src_file.file_hash != client_file["file_hash"]:
                needs_update = True
            else:
                # ---------------------------------------------
                # AUTO-HEALING VALID NETWORK TRANSFERS
                # ---------------------------------------------
                # If the hashes match perfectly, but the Client flagged it as "modified_locally",
                # it means the Source literally just finished an rsync push for this file 
                # (which inadvertently triggered our local Client watchdog). 
                # We can safely clear the flag so it doesn't block legitimate future upgrades!
                if client_file["modified_locally"]:
                    db.clear_modified_locally(target_id, path)
                
        if needs_update:
            # CONFLICT RESOLUTION (Client Protection Rule)
            # If the Client's local watchdog flagged this file as modified by a local user,
            # we MUST NOT overwrite it. We divert it to the conflicts list.
            if path in client_dict and client_dict[path]["modified_locally"]:
                conflicts.append({
                    "filepath": path,
                    "reason": "modified_locally",
                    "client_mtime": client_dict[path]["mtime"],
                    "source_mtime": src_file.mtime
                })
            else:
                missing_files.append(path)
                
    # 2. Compare Client to Source (What do we need to delete?)
    # For now, we are primarily concerned with pushing missing files.
    # We will rely on rsync's --delete flag during the actual push phase to handle deletions,
    # or implement a formal API deletion spec later if we move to 100% Python.
    
    return {
        "status": "success",
        "missing_files": missing_files,
        "conflicts": conflicts
    }

class RegisterFileModel(BaseModel):
    filepath: str
    size: int = 0
    mtime: float = 0.0
    file_hash: str

class RegisterPayloadModel(BaseModel):
    target_id: str
    target_path: Optional[str] = None
    files: List[RegisterFileModel]

@app.post("/api/client/register")
async def client_register(payload: RegisterPayloadModel):
    """
    (Client Mode Only)
    Records files the Source just successfully transferred into the Client's
    hash DB, using the SOURCE's hashes (the version that was sent). The next
    diff then sees those paths as present/matching instead of re-flagging them.

    This closes the loop deterministically. The old behaviour relied entirely on
    the Client's inotify watchdog to re-hash received files — but inotify does
    not fire on FUSE shares (Unraid /mnt/user), so received files were never
    recorded and were re-flagged as "missing" on every subsequent diff.
    """
    if get_current_role() != "client":
        return {"status": "error", "message": "Container is not running in Client mode."}

    target_id = payload.target_id
    # Resolve the requested path to the Client's internal target id — identical
    # to the mapping in /api/client/diff so hashes land under the id the diff reads.
    if payload.target_path:
        raw_client_targets = db.get_setting("client_targets", "[]")
        try:
            client_targets = json.loads(raw_client_targets)
            normalized_req_path = payload.target_path.rstrip('/')
            for ct in client_targets:
                if ct["path"].rstrip('/') == normalized_req_path:
                    target_id = ct["id"]
                    break
        except Exception:
            pass

    # modified_locally=False: these files now match the Source (we just received
    # them), so they must not be treated as locally-modified conflicts.
    rows = [(target_id, f.filepath, f.size, f.mtime, f.file_hash, False)
            for f in payload.files]
    if rows:
        db.batch_upsert_file_hashes(rows)
    return {"status": "success", "registered": len(rows)}

# --- Targets store (Phase 1: DB-driven replication) --- #

@app.get("/api/db_targets")
async def list_db_targets():
    """Read-only view of the authoritative `targets` table, each augmented with a
    computed `next_run` (epoch seconds, or null for manual/disabled) so the UI can
    show when a scheduled target fires next."""
    def _load():
        targets = db.list_targets()
        # Files transferred since local midnight, per target — the "today"
        # counter resets at midnight purely by the query window.
        today = db.transfers_since(sync_engine.local_midnight_utc())
        return targets, today
    targets, today = await asyncio.to_thread(_load)
    now = time.time()
    for t in targets:
        t["next_run"] = sync_engine.next_run_at(t, now)
        t["transferred_today"] = today.get(t["id"], 0)
    return {"status": "success", "targets": targets}

class SchedulePayload(BaseModel):
    interval_seconds: Optional[int] = None
    enabled: Optional[bool] = None

@app.put("/api/target/{target_id}/schedule")
async def set_target_schedule(target_id: str, payload: SchedulePayload):
    """(Phase 3) Set a target's auto-sync interval (seconds; 0 = manual, else
    >= MIN_INTERVAL_SECONDS) and/or enabled flag. The scheduler loop picks the
    change up on its next tick."""
    if payload.interval_seconds is not None:
        iv = payload.interval_seconds
        if iv < 0 or (0 < iv < sync_engine.MIN_INTERVAL_SECONDS):
            return {"status": "error",
                    "message": f"interval_seconds must be 0 (manual) or >= {sync_engine.MIN_INTERVAL_SECONDS}."}
    # Update first, then read back once: an update against a missing id is a no-op,
    # so a None result cleanly means "not found" (saves a pre-check read).
    await asyncio.to_thread(db.set_target_schedule, target_id,
                            payload.interval_seconds, payload.enabled)
    target = await asyncio.to_thread(db.get_target, target_id)
    if not target:
        return {"status": "error", "message": "Target not found in the DB store."}
    target["next_run"] = sync_engine.next_run_at(target, time.time())
    return {"status": "success", "target": target}

@app.post("/api/target/{target_id}/sync_now")
async def sync_now_endpoint(target_id: str):
    """(Phase 2) Run one DB-driven sync for a target: diff against the
    destination ledger -> rsync the missing set -> register on the destination
    -> stamp the run result on the target row. Runs on the bounded job pool;
    progress lines go to the target's log bucket. The legacy verify + sync_missing
    endpoints remain as the fallback path until lsyncd is removed in Phase 4."""
    import sync_engine
    if not db.get_target(target_id):
        return {"status": "error", "message": "Target not found in the DB store."}

    def _run():
        log_router._ensure_bucket_exists(target_id)
        sync_engine.sync_now(target_id, server_mgr=server_mgr,
                             log=lambda m: log_router._add_log(target_id, m))

    # Shares the "sync" job key with sync_missing so the two transfer paths can't
    # run concurrently against the same target.
    started = _submit_job(target_id, "sync", _run)
    if not started:
        return {"status": "success", "message": "A sync is already running for this target."}
    return {"status": "success", "message": "Sync started."}

# --- Keys API --- #

@app.post("/api/keys")
async def add_key(key: KeyModel):
    key_id = server_mgr.save_key(key.name, key.private_key)
    return {"status": "success", "id": key_id}

@app.get("/api/keys")
async def list_keys():
    return {"keys": server_mgr.list_keys()}

@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: str):
    try:
        server_mgr.delete_key(key_id)
        return {"status": "success"}
    except ValueError as e:
        return {"status": "error", "message": str(e)}

# --- Servers API --- #

@app.post("/api/servers")
async def add_server(server: ServerModel):
    server_id = server_mgr.add_server(server.alias, server.host, server.user, server.port,
                                      server.key_id, api_port=server.api_port)
    return {"status": "success", "id": server_id}

@app.get("/api/servers")
async def list_servers():
    return {"servers": server_mgr.list_servers()}

@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: str):
    server_mgr.delete_server(server_id)
    return {"status": "success"}

# --- File Browser API --- #
import stat
import asyncssh

@app.get("/api/fs/browse/local")
async def browse_local(path: str = "/source_data"):
    """Browses the local container filesystem."""
    if not os.path.isabs(path):
        return {"status": "error", "message": "Path must be absolute"}

    real_path = os.path.realpath(path)
    if not _path_browseable(real_path):
        return {"status": "error", "message": "Access denied: path outside allowed directories"}

    try:
        items = []
        for entry in os.scandir(real_path):
            norm_path = entry.path.replace('\\', '/')
            if entry.is_dir():
                items.append({"name": entry.name, "type": "directory", "path": norm_path})
            elif entry.is_file():
                items.append({"name": entry.name, "type": "file", "path": norm_path})

        # Sort directories first, then alphabetically
        items.sort(key=lambda x: (x["type"] == "file", x["name"].lower()))
        return {"status": "success", "path": real_path, "items": items}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/fs/browse/remote")
async def browse_remote(server_id: str, path: str = "/"):
    """Browses a remote server filesystem via SSH/SFTP."""
    server = server_mgr.get_server(server_id)
    if not server:
        return {"status": "error", "message": "Server not found"}
        
    key_path = server_mgr.get_key_path(server["key_id"])
    if not os.path.exists(key_path):
        return {"status": "error", "message": "SSH key not found"}
        
    try:
        # Load the private key
        client_keys = [asyncssh.read_private_key(key_path)]
        
        async with asyncssh.connect(
            host=server["host"], 
            port=server["port"], 
            username=server["user"], 
            client_keys=client_keys,
            known_hosts=KNOWN_HOSTS_PATH if os.path.exists(KNOWN_HOSTS_PATH) else None,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                items = []
                for attr in await sftp.readdir(path):
                    if attr.filename not in ('.', '..'):
                        remote_path = f"{path.rstrip('/')}/{attr.filename}"
                        is_dir = stat.S_ISDIR(attr.attrs.permissions) if attr.attrs.permissions else False
                        
                        if is_dir:
                            items.append({"name": attr.filename, "type": "directory", "path": remote_path})
                        elif stat.S_ISREG(attr.attrs.permissions) if attr.attrs.permissions else False:
                            items.append({"name": attr.filename, "type": "file", "path": remote_path})
                
                # Sort directories first, then alphabetically
                items.sort(key=lambda x: (x["type"] == "file", x["name"].lower()))
                return {"status": "success", "path": path, "items": items}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- Targets API --- #

@app.get("/api/targets")
async def get_all_targets():
    """Returns a list of all active sync targets parsed from Lua."""
    targets = config_mgr.parse_targets()
    log_router.update_target_mapping(targets)
    
    scan_status = hash_scanner_daemon.get_scan_status()

    # Inject active sync status parsed from logs
    for t in targets:
        t["status"] = log_router.target_status.get(t["id"], "In Sync")
        t["files_remaining"] = log_router.target_processing.get(t["id"], 0)
        t["files_queued"] = log_router.target_queues.get(t["id"], 0)
        t["missing_count"] = db.get_missing_count(t["id"])
        t["is_unpaired"] = False
        t["security_alert_host"] = log_router.target_security_alerts.get(t["id"])
        # Surface live baseline-hash progress so the Source UI can show counts
        # while scanning (the Client already does via /api/scan/status).
        sc = scan_status.get(t["id"], {})
        t["scan_status"] = sc.get("status")
        t["hashed"] = sc.get("hashed", 0)
        t["skipped"] = sc.get("skipped", 0)
        t["scan_total"] = sc.get("total", 0)
        t["mb_per_sec"] = sc.get("mb_per_sec", 0)
        t["files_per_sec"] = sc.get("files_per_sec", 0)
        t["hash_mode"] = sc.get("mode")
        if sc.get("status") == "scanning":
            t["status"] = "Scanning"

    # Inject unpaired local sources from database
    raw_unpaired = db.get_setting("unpaired_sources", "[]")
    try:
        unpaired_sources = json.loads(raw_unpaired)
    except json.JSONDecodeError:
        unpaired_sources = []
        
    for us in unpaired_sources:
        sc = scan_status.get(us["id"], {})
        scan_state = sc.get("status")
        if scan_state == "pending":
            status = "Pending Scan"
        elif not hash_scanner_daemon.is_scan_complete(us["id"]):
            status = "Scanning"
        else:
            status = "Ready to Link"
        targets.append({
            "id": us["id"],
            "name": us["name"],
            "source": us["path"],
            "target": "[Unpaired]",
            "status": status,
            "files_remaining": 0,
            "files_queued": 0,
            "missing_count": 0,
            "is_unpaired": True,
            "scan_status": scan_state,
            "hashed": sc.get("hashed", 0),
            "skipped": sc.get("skipped", 0),
            "scan_total": sc.get("total", 0),
            "mb_per_sec": sc.get("mb_per_sec", 0),
            "files_per_sec": sc.get("files_per_sec", 0),
            "hash_mode": sc.get("mode")
        })
        
    global_status = "In Sync"
    if any(t.get("status") == "Syncing" for t in targets):
        global_status = "Syncing"
    elif any(t.get("status") == "Scanning" for t in targets):
        global_status = "Scanning"
    
    return {"targets": targets, "global_status": global_status}

async def _mirror_target_to_db(source_path: str):
    """After config_mgr writes a target, mirror it into the authoritative targets
    table so the scheduler sees it. Finds the just-written target by source path
    (its config-derived id is what parse_targets produces) and upserts it as an
    enabled paired target. Returns the parsed target dict, or None if not found."""
    parsed = await asyncio.to_thread(config_mgr.parse_targets)
    found = next((t for t in parsed if t.get("source") == source_path), None)
    if found:
        servers = await asyncio.to_thread(server_mgr.list_servers)
        await asyncio.to_thread(db.upsert_target_from_config, found, servers, True)
    return found

@app.post("/api/target")
async def create_target(target: TargetModel):
    """Creates a new sync target block.

    All config-file and daemon work runs in a worker thread: /config lives on
    a host-mounted (often FUSE) volume where locks/fsync can stall for seconds
    (disk spin-up, share contention). Running it on the event loop would freeze
    the entire UI for every connected browser.
    """
    try:
        success = await asyncio.to_thread(config_mgr.add_target, target.dict())
        if success:
            created = await _mirror_target_to_db(target.source)

            if target.unpaired_id:
                # Remove from unpaired_sources
                raw_unpaired = await asyncio.to_thread(db.get_setting, "unpaired_sources", "[]")
                try:
                    unpaired_sources = json.loads(raw_unpaired)
                    unpaired_sources = [u for u in unpaired_sources if u["id"] != target.unpaired_id]
                    await asyncio.to_thread(db.set_setting, "unpaired_sources", json.dumps(unpaired_sources))
                except json.JSONDecodeError:
                    pass

                # The just-linked target now lives under a config-derived id (from
                # its name), but its already-computed hashes are still filed under
                # the temporary unpaired id. Re-key them so Preview Sync finds them
                # immediately — otherwise the Source reports zero files until a
                # restart re-registers and re-hashes the directory.
                if created and created["id"] != target.unpaired_id:
                    await asyncio.to_thread(db.migrate_target_id, target.unpaired_id, created["id"])
                    # Drop the now-obsolete disabled unpaired row from the store.
                    await asyncio.to_thread(db.delete_target_record, target.unpaired_id)
                    # Move the live hash-scanner registration to the new id without
                    # re-reading every file: the migrated hashes make the rescan a
                    # fast stat-and-skip pass, and it re-attaches the watchdog.
                    hash_scanner_daemon.remove_target(target.unpaired_id)
                    hash_scanner_daemon.add_target(created["id"], target.source)

            return {"status": "success", "message": "Target created."}
        return {"status": "error", "message": "Failed to write config."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/source_targets")
async def add_source_target(target: SourceTargetModel):
    """Adds a new unpaired source target to the database."""
    raw = db.get_setting("unpaired_sources", "[]")
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        targets = []
    
    import uuid
    target_id = f"source_{uuid.uuid4().hex[:8]}"
    new_target = {"id": target_id, "name": target.name, "path": target.source}
    targets.append(new_target)
    db.set_setting("unpaired_sources", json.dumps(targets))
    # Also record it in the authoritative targets table as DISABLED (unpaired: no
    # destination yet), so it shows up in the DB store and can't be scheduled.
    db.upsert_target(id=target_id, name=target.name, source_path=target.source,
                     server_id=None, dest_path=None, interval_seconds=0, enabled=False)

    # scan_now=False registers the directory without touching its contents.
    hash_scanner_daemon.add_target(target_id, target.source, defer_baseline=not target.scan_now)

    return {"status": "success", "target": new_target}

@app.delete("/api/source_targets/{target_id}")
async def delete_source_target(target_id: str):
    """Removes an unpaired source target from the database."""
    raw = db.get_setting("unpaired_sources", "[]")
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        targets = []
    
    targets = [t for t in targets if t["id"] != target_id]
    db.set_setting("unpaired_sources", json.dumps(targets))
    db.delete_target_record(target_id)
    hash_scanner_daemon.remove_target(target_id)

    return {"status": "success"}

@app.put("/api/target/{target_id}")
async def update_target(target_id: str, target: TargetModel):
    """Updates an existing sync target block.

    Runs in a worker thread — see create_target for why /config I/O must
    never block the event loop."""
    try:
        success = await asyncio.to_thread(config_mgr.update_target, target_id, target.dict())
        if success:
            await _mirror_target_to_db(target.source)   # keep the store in sync
            return {"status": "success", "message": "Target updated."}
        return {"status": "error", "message": "Failed to update target."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

import subprocess

@app.post("/api/target/{target_id}/trust_new_key")
async def trust_new_key(target_id: str):
    """
    Clears the offending SSH key from known_hosts and clears the security alert,
    allowing the next sync retry to cleanly accept the new server identity.
    """
    host_port = log_router.target_security_alerts.get(target_id)
    if not host_port:
        return {"status": "error", "message": "No security alert found for this target."}
        
    try:
        subprocess.run(["ssh-keygen", "-f", KNOWN_HOSTS_PATH, "-R", host_port], check=True, capture_output=True)
        # Clear the alert
        log_router.target_security_alerts[target_id] = None
        log_router.target_status[target_id] = "Syncing"
        # The new host key is accepted; the next scheduled/manual sync will retry.
        return {"status": "success"}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"Failed to remove key: {e.stderr.decode()}"}

@app.post("/api/target/{target_id}/repairkey")
async def repairkey_target(target_id: str):
    """
    Re-pairs the SSH key for an existing paired target without deleting it.
    Calls the client's /api/client/pair endpoint to regenerate a scoped keypair,
    saves the new private key, updates the server profile, and deletes the old key.
    """
    import requests

    targets = config_mgr.parse_targets()
    target = next((t for t in targets if t["id"] == target_id), None)
    if not target:
        return {"status": "error", "message": "Target not found."}

    dest_str = target.get("target", "")
    if '@' not in dest_str and ':/' not in dest_str:
        return {"status": "error", "message": "Target is not a remote destination — re-pairing is only needed for SSH targets."}

    # Extract host IP and remote path from user@host:/path
    host_ip = dest_str.split('@')[-1].split(':')[0]
    target_path = dest_str.split(':', 1)[-1]

    # Identify THIS target's server profile. Prefer its recorded server_id —
    # matching by host alone is ambiguous when several directory-jailed targets
    # share one destination host, and would rotate the WRONG server's key.
    servers_json_path = env("MONARCHAEGIS_SERVERS_JSON", "/config/servers.json")
    keys_dir = env("MONARCHAEGIS_KEYS_DIR", "/config/keys")
    old_key_id = None
    server_id = None

    db_row = db.get_target(target_id)
    recorded_server_id = db_row.get("server_id") if db_row else None
    try:
        with open(servers_json_path, 'r') as sf:
            servers = json.load(sf)
        srv = next((s for s in servers if s.get('id') == recorded_server_id), None)
        if not srv:
            # Older target with no recorded server: fall back to host, but only
            # when exactly one server serves it — otherwise the key is ambiguous.
            host_matches = [s for s in servers
                            if host_ip in (s.get('host'), f"{s.get('user')}@{s.get('host')}")]
            if len(host_matches) > 1:
                return {"status": "error", "message":
                        "Multiple server profiles share this host — can't tell which key "
                        "to repair. Re-pair this target from the source UI instead."}
            srv = host_matches[0] if host_matches else None
        if srv:
            server_id = srv.get('id')
            old_key_id = srv.get('key_id')
    except Exception as e:
        return {"status": "error", "message": f"Could not read server profiles: {e}"}

    # Call the client's pair API to regenerate a scoped keypair.
    # Include X-Pair-Secret if configured so this machine-to-machine call
    # bypasses the client's IP rate limit.
    api_base = sync_engine.client_api_base(srv) if srv else _api_base_for_host(host_ip)
    client_api_url = f"{api_base}/api/client/pair"
    pair_headers = {}
    if _PAIR_SECRET:
        pair_headers["X-Pair-Secret"] = _PAIR_SECRET
    try:
        response = requests.post(
            client_api_url,
            json={"target_id": target_id, "target_path": target_path},
            headers=pair_headers,
            timeout=15
        )
        if response.status_code != 200:
            return {"status": "error", "message": f"Client API unreachable (HTTP {response.status_code})."}
        try:
            data = ClientPairResponse(**response.json())
        except Exception:
            return {"status": "error", "message": "Client API returned unexpected response shape."}
        if data.status != "success":
            return {"status": "error", "message": f"Client pair failed: {data.message}"}
        private_key = data.private_key
    except Exception as e:
        return {"status": "error", "message": f"Failed to contact client API: {e}"}

    # Save the new private key
    new_key_id = server_mgr.save_key(f"repaired_{target_id}", private_key)

    # Update the server profile with the new key_id
    if server_id:
        try:
            with open(servers_json_path, 'r') as sf:
                servers = json.load(sf)
            for s in servers:
                if s.get('id') == server_id:
                    s['key_id'] = new_key_id
                    break
            with open(servers_json_path, 'w') as sf:
                json.dump(servers, sf, indent=4)
        except Exception as e:
            return {"status": "error", "message": f"Failed to update server profile: {e}"}

    # Rewrite the monarchaegis.conf.lua block so IdentityFile points to the new key.
    # The key path is baked into the Lua config at creation time — without this
    # step lsyncd continues using the old (now invalid) key path after restart.
    # Do this BEFORE deleting the old key so a config-rewrite failure leaves the
    # system in a recoverable state (old key still valid, server profile can be rolled back).
    try:
        config_mgr.update_target(target_id, {
            "name": target.get("name"),
            "source": target.get("source"),
            "target": target.get("target"),
            "key_id": new_key_id,   # pin the freshly-repaired key (no host-match guess)
        })
    except Exception as e:
        return {"status": "error", "message": f"Keys updated but failed to rewrite lsyncd config: {e}"}

    # Config rewrite succeeded — safe to delete the old key now.
    if old_key_id and old_key_id != new_key_id:
        try:
            server_mgr.delete_key(old_key_id)
        except Exception:
            pass  # Non-fatal — old key may already be gone

    return {"status": "success", "message": "SSH keys re-paired successfully."}


@app.delete("/api/target/{target_id}")
async def delete_target(target_id: str):
    """Deletes an existing sync target block. Worker thread — see create_target."""
    # Capture the paired server before deletion so we can clean it up if the
    # delete orphans it (profiles are per-key: one target owns one profile).
    row = await asyncio.to_thread(db.get_target, target_id)
    old_server_id = row.get("server_id") if row else None
    success = await asyncio.to_thread(config_mgr.delete_target, target_id)
    if success:
        # Drop the authoritative targets row too so the scheduler stops running it.
        await asyncio.to_thread(db.delete_target_record, target_id)
        # Prune the server profile (and its key) if no remaining target uses it.
        if old_server_id and not any(
                t.get("server_id") == old_server_id for t in db.list_targets()):
            await asyncio.to_thread(server_mgr.remove_server_and_key, old_server_id)
        return {"status": "success", "message": "Target deleted."}
    return {"status": "error", "message": "Failed to delete target."}

@app.post("/api/target/{target_id}/verify")
async def verify_target(target_id: str, ignore_existing: bool = False):
    """Triggers an rsync dry-run checksum verification for the given target."""
    import subprocess
    import threading
    import json
    
    # 1. Find the target configuration
    targets = config_mgr.parse_targets()
    target = next((t for t in targets if t["id"] == target_id), None)
    if not target:
        return {"status": "error", "message": "Target not found"}

    # 2. Spawns background thread to run the verification
    # (Verification is a pure P2P API diff — no SSH/rsync connection is made here.)
    def run_verify(t_id, target_config):
        log_router._ensure_bucket_exists(t_id)
        db.clear_missing_files(t_id) # Clear old DB entries before start
        db.clear_conflicts(t_id) # Clear old conflicts
        
        log_router._add_log(t_id, f"[CheckSum] ---------------------------------------")
        log_router._add_log(t_id, f"[CheckSum] Phase 2: High-Speed P2P Hash Verification")
        
        try:
            # 1. Gather local Source hashes
            log_router._add_log(t_id, f"[CheckSum] Compiling local SQLite hashes...")
            source_hashes = db.get_all_hashes(t_id)
            log_router._add_log(t_id, f"[CheckSum] Local DB compiled: {len(source_hashes)} files tracked.")
            
            # Extract host IP and API port from Destination
            dest_str = target_config.get("target")
            
            target_path = dest_str
            # The Client's API port comes from its server profile (optional
            # `api_port`, else the MONARCHAEGIS_CLIENT_API_PORT default) — see
            # _api_base_for_host below.
            if '@' in dest_str or ':/' in dest_str:
                host_ip = dest_str.split('@')[-1].split(':')[0]
                target_path = dest_str.split(':', 1)[-1]
            else:
                host_ip = "127.0.0.1" # Local test
                
            client_api_url = f"{_api_base_for_host(host_ip)}/api/client/diff"
            
            # 2. Fire payload to Client
            import requests
            log_router._add_log(t_id, f"[CheckSum] Pushing diff payload to Client API: {client_api_url}")
            
            response = requests.post(
                client_api_url,
                json={
                    "target_id": t_id,
                    "target_path": target_path,
                    "source_hashes": source_hashes,
                    "source_hash_algo": db.HASH_ALGO
                },
                timeout=30 # 30 seconds should be plenty for even a massive JSON payload
            )
            
            if response.status_code == 200:
                try:
                    data = ClientDiffResponse(**response.json())
                except Exception:
                    log_router._add_log(t_id, "[ERROR] Client API returned unexpected response shape.")
                    return

                if data.status == "success":
                    missing = data.missing_files
                    conflicts = data.conflicts

                    # 3. Save missing files to DB for the push phase
                    for m_file in missing:
                        db.add_missing_file(t_id, m_file)

                    log_router._add_log(t_id, f"[CheckSum] API Diff Complete. Found {len(missing)} missing/outdated files.")

                    if conflicts:
                        log_router._add_log(t_id, f"[WARNING] Client Protection Engaged! Blocked {len(conflicts)} locally modified files from overwrite.")
                        for c in conflicts:
                            db.add_conflict(t_id, c.filepath, c.reason)
                            log_router._add_log(t_id, f"[CONFLICT] Blocked: {c.filepath}")
                else:
                    log_router._add_log(t_id, f"[ERROR] Client API returned error: {data.message}")
            else:
                log_router._add_log(t_id, f"[ERROR] Client API unreachable. Status {response.status_code}.")

        except Exception as e:
            log_router._add_log(t_id, f"WARN: [CheckSum] Failed to run P2P verification: {e}")

    started = _submit_job(target_id, "verify", run_verify, target_id, target)
    if not started:
        return {"status": "success", "message": "Verification already in progress for this target."}
    return {"status": "success", "message": "Verification started."}

@app.post("/api/target/{target_id}/sync_missing")
async def sync_missing(target_id: str):
    """Triggers an rsync push for only tracked missing files using --files-from."""
    import subprocess
    import threading
    import json
    import tempfile
    
    missing_files = db.get_missing_files(target_id)
    if not missing_files:
        return {"status": "error", "message": "No missing files tracked. Run Verify Checksum first."}

    targets = config_mgr.parse_targets()
    target = next((t for t in targets if t["id"] == target_id), None)
    if not target:
        return {"status": "error", "message": "Target not found"}

    source = target.get("source")
    if not source.endswith('/'):
        source += '/'
    # Push to the PHYSICAL destination ("user@host:/" for rrsync-jailed targets —
    # the jail maps it to the real receiving directory). The logical path in
    # target["target"] is for display/diffing only; pushing to it would make
    # rrsync nest the path inside the jail root.
    dest = target.get("physical_target") or target.get("target")
    if not dest.endswith('/'):
        dest += '/'

    # 1. Write missing files to temp list
    fd, tmp_path = tempfile.mkstemp(prefix=f"missing_{target_id}_", suffix=".txt")
    with os.fdopen(fd, 'w') as f:
        for p in missing_files:
            # Strip leading slash so rsync resolves relative to the source dir
            f.write(f"{p.lstrip('/')}\n")
            
    # 2. Build explicit push command
    rsh_arg = None
    if '@' in dest or ':/' in dest:
        host_str = dest.split(':')[0]
        key_path = None
        client_port = "22"
        servers_json = env("MONARCHAEGIS_SERVERS_JSON", "/config/servers.json")
        if os.path.exists(servers_json):
            try:
                with open(servers_json, 'r') as sf:
                    servers = json.load(sf)
                for s in servers:
                    s_user_host = f"{s.get('user')}@{s.get('host')}"
                    if host_str == s_user_host or host_str == s.get('host'):
                        if s.get('port'):
                            client_port = str(s['port'])
                        key_id = s.get('key_id')
                        if key_id:
                            keys_dir = env("MONARCHAEGIS_KEYS_DIR", "/config/keys")
                            key_path = os.path.join(keys_dir, key_id).replace('\\', '/')
                            break
            except Exception:
                pass
        
        try:
            if key_path:
                key_path = _safe_key_path(key_path)
            client_port = _safe_port(client_port)
        except ValueError as e:
            return {"status": "error", "message": str(e)}

        rsh_val = f"ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={KNOWN_HOSTS_PATH}"
        if key_path:
            rsh_val += f" -o IdentityFile={key_path}"
        rsh_val += f" -p {client_port}"
        rsh_arg = f"--rsh={rsh_val}"

    # -r recursive, -v verbose, --files-from explicit list target.
    # -W (--whole-file): disable rsync's delta algorithm. Our destination files
    # are re-encoded (AV1), so a delta match is ~impossible — yet by default
    # (network transfer) rsync still READS every existing destination file as a
    # "basis" to diff against, doubling destination disk I/O for zero benefit
    # (observed: the receiver's generator reading hundreds of GB of re-encoded
    # media just to send the whole file anyway). -W sends whole files and skips
    # those basis reads. rrsync's jail allows W (short_no_arg allowlist).
    # NOTE: --log-file and --itemize-changes are intentionally excluded here.
    # When the destination is a remote rrsync-jailed endpoint, rrsync rejects
    # flags that write to the remote filesystem (--log-file) or are not in its
    # safe-flag allowlist (--itemize-changes), corrupting the protocol stream
    # and causing rsync exit code 12. Logging is handled via stdout instead.
    # Uses RSYNC_NO_SECLUDED_ARGS (see constants.py for why rrsync needs it).
    cmd = ["rsync", "-rvW", RSYNC_NO_SECLUDED_ARGS, f"--files-from={tmp_path}"]
    if rsh_arg:
        cmd.append(rsh_arg)
    cmd.append(source)
    cmd.append(dest)

    # Build register info: the SOURCE's hashes for the files we're about to push.
    # After a successful rsync we POST these to the destination's
    # /api/client/register so it records what it received (see that endpoint for
    # why the inotify watchdog can't be relied on). Uses the same host/target_path
    # derivation as the diff call so it lands under the same client target id.
    register_info = None
    logical_dest = target.get("target", "")
    if '@' in logical_dest or ':/' in logical_dest:
        reg_host_ip = logical_dest.split('@')[-1].split(':')[0]
        reg_target_path = logical_dest.split(':', 1)[-1] if ':' in logical_dest else logical_dest
        src_hash_by_path = {r["filepath"]: r for r in db.get_all_hashes(target_id)}
        reg_files = []
        for p in missing_files:
            rec = src_hash_by_path.get(p) or src_hash_by_path.get(p.lstrip('/'))
            if rec:
                reg_files.append({
                    "filepath": rec["filepath"], "size": rec["size"],
                    "mtime": rec["mtime"], "file_hash": rec["file_hash"],
                })
        if reg_files:
            register_info = {"host_ip": reg_host_ip, "target_path": reg_target_path, "files": reg_files}

    # 3. Spawns background thread
    def run_sync_missing(cmd_args, t_id, tmp_file_path, reg_info):
        log_router._ensure_bucket_exists(t_id)
        log_router._add_log(t_id, f"[Recovery] ---------------------------------------")
        log_router._add_log(t_id, f"[Recovery] Initiating targeted sync for missing files...")
        log_router._add_log(t_id, f"[Recovery] Executing explicit list push...")
        
        try:
            process = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            
            for line in iter(process.stdout.readline, ''):
                clean_line = line.strip()
                if not clean_line:
                    continue
                if clean_line.startswith("building file list") or clean_line.startswith("sent ") or clean_line.startswith("total size is") or "bytes/sec" in clean_line:
                    continue
                if clean_line.startswith("Warning: Permanently added"):
                    continue
                    
                msg = f"[Recovery] Pushing -> {clean_line}"
                # Route through parse_log_line to trigger security alerts if SSH key changed
                # Fake the log string format to ensure it gets picked up
                log_router.parse_log_line(f"source=\"{source}\" {msg}")
                
            process.stdout.close()
            process.wait()

            # Only clear the tracked missing list on a clean rsync exit. On
            # failure we keep the list so the next Sync Missing retries the same
            # set instead of the UI falsely showing "0 missing" after a failed
            # push. A later Verify Checksum re-derives the list either way.
            if process.returncode == 0:
                log_router._add_log(t_id, f"[Recovery] Targeted sync complete. Clearing tracked missing files DB.")
                db.clear_missing_files(t_id)
                # Count these in the per-day transfer counter too.
                db.record_transfers(t_id, missing_files, action="recovery")
            else:
                log_router._add_log(t_id, f"WARN: [Recovery] rsync exited {process.returncode}; keeping tracked missing files for retry.")

            # Best-effort: tell the destination what we just sent so its DB
            # records them (works even when its inotify watchdog can't see the
            # FUSE writes). Only after a clean rsync exit; failure here never
            # affects the transfer that already happened.
            if process.returncode == 0 and reg_info:
                try:
                    import requests
                    resp = requests.post(
                        f"{_api_base_for_host(reg_info['host_ip'])}/api/client/register",
                        json={"target_id": t_id, "target_path": reg_info["target_path"],
                              "files": reg_info["files"]},
                        timeout=30,
                    )
                    if resp.status_code == 200 and resp.json().get("status") == "success":
                        log_router._add_log(t_id, f"[Recovery] Destination recorded {resp.json().get('registered', 0)} received files.")
                    else:
                        log_router._add_log(t_id, f"WARN: [Recovery] Destination register returned HTTP {resp.status_code}.")
                except Exception as e:
                    log_router._add_log(t_id, f"WARN: [Recovery] Could not update destination DB (files still transferred): {e}")

        except Exception as e:
            log_router._add_log(t_id, f"WARN: [Recovery] Failed to run: {e}")
        finally:
            try:
                os.remove(tmp_file_path)
            except Exception:
                pass

    started = _submit_job(target_id, "sync", run_sync_missing, cmd, target_id, tmp_path, register_info)
    if not started:
        # Job deduped (one already running) — remove the temp list we just wrote.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return {"status": "success", "message": "Recovery sync already in progress for this target."}
    return {"status": "success", "message": "Targeted recovery sync started."}

@app.get("/api/target/{target_id}/export_csv")
async def export_missing_csv(target_id: str):
    """Exports the currently tracked missing files to a CSV download."""
    from fastapi.responses import StreamingResponse
    import io
    import csv
    
    missing_files = db.get_missing_files(target_id)
    
    # Grab the source/dest from the config to give the user better context
    targets = config_mgr.parse_targets()
    target_conf = next((t for t in targets if t["id"] == target_id), None)
    
    source_str = "Unknown"
    dest_str = "Unknown"
    if target_conf:
        source_str = target_conf.get("source", "Unknown")
        dest_str = target_conf.get("target", "Unknown")
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Target ID", "Relative Path", "Full Source Path", "Full Destination Path"])
    for filepath in missing_files:
        # filepath is typically something like "Action/Movie.mkv" or "/Action/Movie.mkv"
        # We need to securely join it with the base paths.
        clean_filepath = filepath.lstrip('/')
        
        # Ensure base paths end with a slash for clean concatenation
        s_base = source_str if source_str.endswith('/') else source_str + '/'
        d_base = dest_str if dest_str.endswith('/') else dest_str + '/'
        
        full_source = f"{s_base}{clean_filepath}"
        full_dest = f"{d_base}{clean_filepath}"
        
        writer.writerow([target_id, filepath, full_source, full_dest])
        
    output.seek(0)
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=preview_{target_id}.csv"}
    )

@app.post("/api/daemon/restart")
async def force_restart():
    """Retained for the (transitional) UI. lsyncd is retired — there is no daemon
    to restart; replication runs on the scheduler. Use Sync Now to force a run."""
    return {"status": "success", "message": "lsyncd is retired; use Sync Now to trigger a run."}

class FactoryResetModel(BaseModel):
    # When True, ALSO wipe the lsyncd config, the hash database, and the list of
    # registered directories — a complete blank slate. When False (default), only
    # the SSH auth state is cleared so you can re-pair without losing hashes or
    # having to re-scan terabytes.
    wipe_data: bool = False

def _do_factory_reset(wipe_data: bool) -> dict:
    """Blocking factory-reset worker. Runs off the event loop."""
    summary = {}

    # 1. Wipe SSH auth state (keys, authorized_keys, server profiles).
    summary["auth"] = server_mgr.factory_reset()

    # 2. Remove the host-key pin file so the next pairing re-pins cleanly.
    try:
        if os.path.exists(KNOWN_HOSTS_PATH):
            os.remove(KNOWN_HOSTS_PATH)
        summary["known_hosts_cleared"] = True
    except OSError:
        summary["known_hosts_cleared"] = False

    if wipe_data:
        # 3. Remove the lsyncd config and its timestamped backups.
        cfg = config_mgr.config_path
        cfg_dir = os.path.dirname(cfg) or "."
        try:
            if os.path.exists(cfg):
                os.remove(cfg)
            base = os.path.basename(cfg)
            for fname in os.listdir(cfg_dir):
                if fname.startswith(base + ".") and fname.endswith(".bak"):
                    try:
                        os.remove(os.path.join(cfg_dir, fname))
                    except OSError:
                        pass
        except OSError:
            pass

        # 4. Drop all hashes, missing files, conflicts, history, and registered dirs.
        db.factory_reset_data()

        # 5. Tear down live hash-scanner targets and in-memory log state.
        for tid in list(hash_scanner_daemon.active_targets.keys()):
            hash_scanner_daemon.remove_target(tid)
        log_router.clear_logs()

        # Recreate an empty default config so the app is immediately usable.
        config_mgr._ensure_config_exists()

    summary["wipe_data"] = wipe_data
    return summary

@app.post("/api/factory_reset")
async def factory_reset(payload: FactoryResetModel = FactoryResetModel()):
    """Clears stale pairing/auth state (and optionally all data) for a clean restart.

    Default (wipe_data=False): removes SSH keys, authorized_keys, known_hosts, and
    server profiles only — keeps your hash database and registered directories so
    you can re-pair without re-scanning. wipe_data=True is a full blank slate.
    """
    try:
        summary = await asyncio.to_thread(_do_factory_reset, payload.wipe_data)
        return {"status": "success", "summary": summary,
                "message": "Factory reset complete. Re-pair each target to reconnect."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/logs/clear")
async def clear_logs():
    """Clears the in-memory per-target log buckets. (lsyncd is retired, so there
    is no daemon log file to roll or tailer to restart.)"""
    log_router.clear_logs()
    return {"status": "success"}

@app.get("/api/target/{target_id}/logs/stream")
async def stream_logs(request: Request, target_id: str):
    """Server-Sent Event (SSE) endpoint to stream live logs to the frontend."""
    async def log_generator():
        last_update = -1
        while True:
            if await request.is_disconnected():
                break
            
            current_update = log_router.get_update_count(target_id)
            if current_update != last_update:
                logs = log_router.get_logs_for_target(target_id)
                # Manually yield standard SSE format to guarantee JSON perfection
                payload_str = json.dumps(logs)
                yield f"data: {payload_str}\n\n"
                last_update = current_update
                
            await asyncio.sleep(0.1)

    return StreamingResponse(
        log_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )
