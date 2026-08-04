# MonarchAegis — Setup & Operations Guide

Complete reference for deploying, configuring, and operating the `cthexiv/monarchaegis` Docker container. Covers single-node Source mode, P2P Source+Client mode, all environment variables, volume mappings, security hardening, and daily operations.

---

## Table of Contents

1. [What It Does](#1-what-it-does)
   - [Replication Model](#replication-model)
2. [Container Modes](#2-container-modes)
3. [Quick Start — Single Node (Source Only)](#3-quick-start--single-node-source-only)
4. [Quick Start — P2P Mode (Source + Client)](#4-quick-start--p2p-mode-source--client)
5. [Environment Variables Reference](#5-environment-variables-reference)
6. [Volume Mappings Reference](#6-volume-mappings-reference)
7. [Port Reference](#7-port-reference)
8. [Unraid Community Applications](#8-unraid-community-applications)
9. [Security Hardening](#9-security-hardening)
10. [Feature Reference](#10-feature-reference)
11. [Architecture Overview](#11-architecture-overview)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. What It Does

MonarchAegis is a containerized web GUI for **database-driven, scheduled file replication** over SSH/rsync. The Source hashes its files, diffs those hashes against the destination's **ledger** (the Client's hash database — not the destination filesystem), and pushes only the files that are new or changed, on a per-target schedule or on demand.

> **lsyncd retired.** Earlier versions ran the `lsyncd` daemon to live-watch directories with `inotify` and replicate in near-real-time. That daemon has been **removed**: replication is now driven by a per-target scheduler + a manual **Sync Now** button. This is more predictable on large media libraries behind FUSE shares (where inotify storms previously overloaded hosts) and, crucially, it **protects intentional destination-side changes** (e.g. AV1 re-encodes) from being overwritten — see [Replication Model](#replication-model).

Core capabilities:

- **Scheduled replication** — per-target interval presets (Manual / 1h / 6h / 12h / Daily) plus a **Sync Now** action; no live daemon
- **Ledger-based diffing** — the destination **database** is authoritative for "what's present"; the source only pushes files absent from, or superseding, the ledger, so re-encoded destination files are never re-pushed
- **Web UI** — pure HTML/JS dashboard; no framework dependencies
- **P2P hash verification** — Source and Client both maintain SQLite hash databases; the diff happens in-memory over HTTP, no destination filesystem scan
- **SSH key management** — UI generates per-target `ed25519` keypairs; public key is automatically written to `authorized_keys` with `rrsync` directory-jail restrictions
- **Client protection** — the diff will not overwrite a client file flagged as locally-modified (safety logic retained; the standalone "Conflicts" UI panel was removed)
- **Real-time log streaming** — SSE-backed terminal window showing live sync-run output per target
- **Transfer history** — every synced file recorded in SQLite

---

## Replication Model

Replication is **database-driven** and runs on a schedule (or on demand), not from a live daemon.

**How a sync runs** (Source → Client), per target:

1. The Source hashes its files (full / sampled / metadata per `MONARCHAEGIS_HASH_MODE`).
2. It sends those hashes to the Client's `/api/client/diff`, which compares them against the Client's **ledger** (its `file_hashes` DB) and returns the paths that are **absent** or whose fingerprint **supersedes** the ledger record.
3. The Source `rsync`-pushes only that set (`--whole-file`, into the `rrsync` jail).
4. On success it calls `/api/client/register`, writing the **Source's** fingerprints for the pushed files into the Client's ledger — so the ledger reflects exactly what was sent.
5. The run's outcome (last-run / next-run / result) is recorded on the target and shown in the UI.

**Why the ledger, not the filesystem?** The destination **database** — not the destination's actual files — is authoritative for "what's present." This is what lets you **re-encode files on the destination** (e.g. to AV1) without them being re-pushed: the diff compares the Source against the ledger record, never against the re-encoded file. It also means the Client's `inotify` watchdog is not relied on (it doesn't fire on FUSE shares like Unraid `/mnt/user`).

**Scheduling.** Each target has an interval: **Manual** (`0`, default) or `1h / 6h / 12h / Daily`. A background scheduler (source-mode only) fires due targets on each tick (`MONARCHAEGIS_SCHEDULER_TICK_SEC`, default 60s). A manual **Sync Now** button runs one pass immediately. Concurrent runs of the same target are de-duplicated.

**Deletions are never propagated** — the destination is ledger-authoritative; deleting a file on the Source does not delete it on the Client.

> ⚠️ **Destination full-rehash caveat.** If you ever rebuild the Client's ledger from its **actual on-disk files** (a destination full rehash), any file that was re-encoded on the destination will then differ from the Source and **will be re-pushed** on the next sync, overwriting the re-encode. A full rehash is the intended "reset to on-disk truth" escape hatch — use it knowingly. Normal operation (adopt-existing + register-after-transfer) preserves re-encodes.

---

## 2. Container Modes

The same Docker image runs as either a **Source** or a **Client**. The role is switched at any time from the **role dropdown in the web UI** (top of the dashboard) and is stored in the database. `MONARCHAEGIS_ROLE` only sets the **initial** role on first launch, before one has ever been chosen — once set in the UI, the stored value wins and the environment variable is ignored.

| Mode | Role | What runs |
|---|---|---|
| `source` (default) | Sender / origin server | hash scanner + **replication scheduler** + full UI |
| `client` | Receiver / destination server | `sshd` + hash scanner + Client UI (no scheduler — the Client only serves diff/register/pair and receives rsync) |

You must deploy one container per server. Both containers use the identical image `cthexiv/monarchaegis:latest`.

---

## 3. Quick Start — Single Node (Source Only)

Use this when you want to manage sync targets on a single server without P2P hash verification.

### Docker Run

```bash
docker run -d \
  --name monarchaegis \
  --restart unless-stopped \
  -p 5000:5000 \
  -v /mnt/user/appdata/monarchaegis/config:/config \
  -v /mnt/user/appdata/monarchaegis/logs:/logs \
  -v /mnt/user:/source_data \
  -e MONARCHAEGIS_ROLE=source \
  -e TZ=America/New_York \
  cthexiv/monarchaegis:latest
```

### Docker Compose

```yaml
version: '3.8'

services:
  monarchaegis:
    image: cthexiv/monarchaegis:latest
    container_name: monarchaegis
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /mnt/user/appdata/monarchaegis/config:/config:rw
      - /mnt/user/appdata/monarchaegis/logs:/logs:rw
      - /mnt/user:/source_data:rw
    environment:
      - MONARCHAEGIS_ROLE=source
      - TZ=America/New_York
```

Open `http://<host-ip>:5000` in a browser.

---

## 4. Quick Start — P2P Mode (Source + Client)

P2P mode enables sub-second hash diffing, conflict protection, and missing-file sync. Deploy one container on each server.

### Step 1 — Deploy the Client (destination server)

```bash
docker run -d \
  --name monarchaegis-client \
  --restart unless-stopped \
  -p 5000:5000 \
  -p 2222:2222 \
  -v /mnt/user/appdata/monarchaegis/config:/config:rw \
  -v /mnt/user/appdata/monarchaegis/logs:/logs:rw \
  -v /mnt/user:/source_data:rw \
  -e MONARCHAEGIS_ROLE=client \
  -e MONARCHAEGIS_PAIR_SECRET=your-shared-secret-here \
  -e TZ=America/New_York \
  cthexiv/monarchaegis:latest
```

> **Port 2222** is required on the Client. It is the SSH port that rsync uses to transfer files. The Source connects to the Client on this port.

> **Unraid tip:** for large one-shot bulk loads, map the Client's `/source_data` to a direct pool path (`/mnt/cache/...`) rather than `/mnt/user/...` to write at NVMe speed and skip the `shfs`/parity penalty — see [Volume Mappings Reference](#6-volume-mappings-reference).

### Step 2 — Deploy the Source (origin server)

```bash
docker run -d \
  --name monarchaegis-source \
  --restart unless-stopped \
  -p 5000:5000 \
  -v /mnt/user/appdata/monarchaegis/config:/config:rw \
  -v /mnt/user/appdata/monarchaegis/logs:/logs:rw \
  -v /mnt/user:/source_data:rw \
  -e MONARCHAEGIS_ROLE=source \
  -e MONARCHAEGIS_PAIR_SECRET=your-shared-secret-here \
  -e TZ=America/New_York \
  cthexiv/monarchaegis:latest
```

> The `MONARCHAEGIS_PAIR_SECRET` value must be identical on both containers. It authenticates the M2M pairing call. Use a long random string (e.g., `openssl rand -hex 32`).

### Step 3 — Pair via the UI

1. Open the **Client** UI → click **"+ Add Directory"** → select the receiving folder → click **Generate Key**.
2. Copy the private key from the modal that appears.
3. Open the **Source** UI → click **"Add New Target"**.
4. Fill in: name, local source path, Client IP, Client SSH port (2222), and paste the private key.
5. Click **Save**. The sync link is created (Manual by default).
6. Open the target and either click **Sync Now** for a one-off run, or set a **Schedule** (1h / 6h / 12h / Daily) to have it sync automatically. Nothing auto-syncs until you set a schedule.

The key is cryptographically scoped: it can only run rsync inside the specified directory and cannot open a shell, access other paths, or forward ports.

---

## 5. Environment Variables Reference

All variables are optional unless marked **Required**. Defaults shown are the container defaults.

### Core

| Variable | Default | Description |
|---|---|---|
| `MONARCHAEGIS_ROLE` | `source` | **Initial role only.** `source` or `client`. Applies on first launch; afterwards the role lives in the database and is changed from the UI dropdown (this variable is then ignored). |
| `MONARCHAEGIS_CONFIG_PATH` | `/config/monarchaegis.conf.lua` | Legacy Lua target file. lsyncd is retired; this is now only an internal target store (read once at upgrade to migrate targets into the DB). |
| `MONARCHAEGIS_DB_PATH` | `/config/monarchaegis.db` | SQLite database — the source of truth: `targets` (schedule/state), hashes, transfer history, settings. |
| `MONARCHAEGIS_SCHEDULER_TICK_SEC` | `60` | How often (seconds) the source-mode scheduler checks for due targets. `0` disables the scheduler (Manual/Sync Now still work). |
| `MONARCHAEGIS_LOG_PATH` | `/logs/monarchaegis.log` | Legacy log path (no daemon writes it anymore; per-target output streams via the UI). |
| `MONARCHAEGIS_STATUS_PATH` | `/logs/monarchaegis.status` | Legacy status path (unused since lsyncd was retired). |
| `RSYNC_LOG_PATH` | `/logs/rsync.log` | rsync transfer log. |
| `MONARCHAEGIS_SERVERS_JSON` | `/config/servers.json` | JSON file storing remote server profiles (host, port, key mappings). |
| `MONARCHAEGIS_KEYS_DIR` | `/config/keys` | Directory for SSH private keys. |
| `MONARCHAEGIS_KNOWN_HOSTS` | `/config/known_hosts` | SSH known_hosts file for TOFU host key pinning. |

### Security

| Variable | Default | Description |
|---|---|---|
| `MONARCHAEGIS_USERNAME` | `admin` | HTTP Basic Auth username for the web UI. Auth is disabled when `MONARCHAEGIS_PASSWORD` is not set. |
| `MONARCHAEGIS_PASSWORD` | _(unset)_ | HTTP Basic Auth password. Set to enable UI authentication. Leave unset for open access (trusted LAN only). |
| `MONARCHAEGIS_PAIR_SECRET` | _(unset)_ | Shared secret for M2M pairing calls between Source and Client. **When set, it is required** — callers without it receive HTTP 401. Set the same value on both containers. Use `openssl rand -hex 32` to generate. |
| `MONARCHAEGIS_SAFE_MODE` | _(unset)_ | Set to `1` to start the web UI **only** — no scheduler, no hash scanner. Recovery mode: lets you narrow or delete sync targets when a previous configuration overwhelms the host (see Troubleshooting). Unset and recreate the container to resume normal operation. |
| `MONARCHAEGIS_BROWSE_ALLOWED_PATHS` | `/source_data,/mnt,/data` | Comma-separated list of root paths the file browser may traverse. Ancestor paths (e.g. `/`) are allowed for navigation, but only configured prefixes and their children are accessible. |

### Networking

| Variable | Default | Description |
|---|---|---|
| `MONARCHAEGIS_SSH_PORT` | `2222` | Port the internal `sshd` listens on. Must match the Docker port mapping. Used by Client mode to receive rsync. |
| `TZ` | `America/New_York` | Container timezone for log timestamps. |

### Performance

| Variable | Default | Description |
|---|---|---|
| `MONARCHAEGIS_HASH_WORKERS` | `min(cpu_count, 16)` (floor 2) | Parallel threads for file hashing. `xxhash` releases the GIL during hashing, so threads genuinely parallelize across cores. On spinning disks, 4–8 often beats 16 (more threads cause seek thrashing). |
| `MONARCHAEGIS_HASH_MODE` | `full` | Baseline hashing strategy. `full` reads every byte (byte-exact, slowest). `sampled` reads only the first/last `MONARCHAEGIS_HASH_SAMPLE_MB` plus exact size — far faster on large files, content-based, works for all file types. `metadata` fingerprints **media** files from their technical metadata (codecs, resolution, channels, rounded duration + exact size) via `ffprobe` without reading the whole stream, and falls back to `sampled` for non-media files. **Must be set identically on Source and Client** — a mismatch is rejected at diff time. Changing the mode triggers a one-time re-hash. |
| `MONARCHAEGIS_HASH_SAMPLE_MB` | `16` | Bytes (in MB) read from the head and tail of each file in `sampled` mode (and the non-media fallback of `metadata` mode). |
| `MONARCHAEGIS_FFPROBE_TIMEOUT` | `30` | Seconds to wait for an `ffprobe` call in `metadata` mode before falling back to a content hash for that file. |
| `MONARCHAEGIS_TRANSPORT` | `rsync` | Push transport (**Source** mode). `rsync` = per-file, resumable, delta (default). `tar` = the whole diff as ONE integrity-checked tar over ONE SSH connection (see below). Global for now — a per-target selector is planned. |
| `MONARCHAEGIS_TAR_TMPDIR` | `/config/tmp` | Where the `tar` transport stages the whole-diff tarball **before** transfer — this is the staging volume, **not** the destination. The default system temp (`/tmp`) is the container's overlay filesystem (on Unraid, the small Docker vDisk), so a large diff fails with `No space left on device` there even when the array has room. Defaults to a mapped, persistent volume; point it at a larger mapped path (e.g. an array path) if a single diff can exceed the appdata pool. |
| `MONARCHAEGIS_PAIR_RATE_MAX` | `10` | Max pairing requests per IP per window (see below). |
| `MONARCHAEGIS_PAIR_RATE_WINDOW` | `60` | Rate limit window in seconds. |
| `MONARCHAEGIS_PAIR_RATE_MAX_KEYS` | `2000` | Max distinct source IPs tracked in memory. Oldest IP evicted when full so new IPs are never hard-blocked. |

**Hash worker scaling:**

| CPU cores | Default workers |
|---|---|
| 1–2 | 2 |
| 4 | 4 |
| 8 | 8 |
| 16+ | 16 |

To override on a high-core-count server: `-e MONARCHAEGIS_HASH_WORKERS=24`

**Batch transport (tar-stream).** With `MONARCHAEGIS_TRANSPORT=tar` on the **Source**, a sync sends the entire set of new/changed files as a **single tar stream over one SSH connection** instead of invoking rsync per file. This removes rsync's per-file protocol round-trips, which dominate wall-clock time when a diff is thousands of small files (subtitles, `.nfo`, artwork). The whole tarball's `xxh64` digest is verified on the destination **before** anything is unpacked — a dropped or truncated transfer lands nothing and is retried next run — and extraction is confined to the same jailed directory as the rsync path (regular files/dirs only; absolute paths, `..`, and symlinks are rejected).

Notes:
- Set it on the **Source** container only; recreate the container so the new env is read.
- Existing paired keys are **auto-upgraded** to accept the tar transport on first boot — no re-pairing needed (the forced command becomes a small dispatcher that runs rsync *or* the tar receiver).
- tar removes *protocol/network* overhead, **not** the destination's per-file create cost, so the win is largest when the bottleneck is round-trips/latency rather than destination disk I/O. On a 1GbE link a single stream already saturates the wire; the bandwidth payoff comes with the planned parallel-stream work on faster links.

---

## 6. Volume Mappings Reference

| Container path | Mode | Purpose |
|---|---|---|
| `/config` | `rw` | SQLite database (source of truth), internal Lua target store, SSH keys, server profiles, known_hosts |
| `/logs` | `rw` | Optional; retained for compatibility. No daemon writes here anymore — sync output streams to the UI. |
| `/source_data` | `rw` | Actual files to sync or receive. Map your NAS share here. |

> **Important:** `/source_data` must be `rw` even on the Source — the recovery/dry-run endpoints create temp files, and re-scans need write access.

> **Unraid — receive throughput for large bulk transfers.** On Unraid, `/mnt/user/<share>` is the `shfs` FUSE user-share merge: every write goes through a userspace layer *and* follows the share's cache/mover policy, both of which cap sustained write speed. The container **cannot see or choose** where a write physically lands (cache pool vs. parity array) — that is decided host-side and is invisible from inside the container. So when you need to land a **large batch all at once** at line rate, map the **destination (Client) `/source_data`** to a **direct pool path** in your Docker template — e.g. `-v /mnt/cache/media:/source_data:rw` instead of `-v /mnt/user/media:/source_data:rw`. That writes straight to the NVMe pool, bypassing `shfs` and the parity write penalty, and Unraid's **mover** relocates the data onto the array afterward on its schedule (the "get it across fast, deal with the disk later" pattern).
>
> Caveats: the batch occupies the cache pool until the mover runs, so the pool must have room (a bulk load bigger than free cache will fail); the pool path must already exist; and this is purely a host-side Docker-template mapping — nothing the container controls or can verify. For steady incremental syncs the normal `/mnt/user/...` mapping is fine.

The `/config` volume is persistent state. Back it up before upgrades — it contains your SSH keys, server profiles, and the SQLite database with all target definitions, hash records, and transfer history.

### Upgrading from an older (lsyncd) build

You can **slot the new image in over an existing deployment — no database rebuild, no re-hash** — provided you keep the same `MONARCHAEGIS_HASH_MODE`. On first boot the new build:

- adds a `targets` table to your existing `monarchaegis.db` (non-destructive) and runs a **one-shot, idempotent migration** that reads your existing Lua config + server profiles and populates it, reusing your existing target ids so your hashes stay linked;
- does **not** launch lsyncd — so replication becomes **scheduled** instead of live. Migrated targets come in as **Manual**, so nothing auto-syncs until you set a schedule (or click Sync Now). This means the upgrade won't trigger a surprise mass re-sync.

Rollback is clean: the new build only *adds* a table + a setting and keeps writing the Lua config, so reverting to the old image just works. **Snapshot `/config` on both nodes first**, and **do not change `MONARCHAEGIS_HASH_MODE`** during the upgrade (that intentionally wipes and re-hashes). After upgrading, confirm the migration landed (dashboard / `GET /api/db_targets` lists your targets), then flip each target from Manual to an interval.

---

## 7. Port Reference

| Host port | Container port | Protocol | Required on | Purpose |
|---|---|---|---|---|
| `5000` | `5000` | TCP | Both | Web GUI (FastAPI/uvicorn) |
| `2222` | `2222` | TCP | **Client only** | P2P SSH for incoming rsync transfers |

The Source does not need port 2222 exposed. Only the Client receives incoming rsync connections.

---

## 8. Unraid Community Applications

An Unraid XML template (`monarchaegis.xml`) is included. Import it via Community Applications to auto-populate ports, paths, and environment variables.

Manual template path: `https://raw.githubusercontent.com/Th3X1V/MonarchAegis/main/monarchaegis.xml`

The template exposes these fields in the Unraid UI:

- **WebGUI Port** → host port for the web interface (default 5000)
- **P2P SSH Port** → host port for incoming rsync (default 2222, Client only)
- **AppData Config Path** → maps to `/config`
- **AppData Logs Path** → maps to `/logs`
- **Source/Destination Data Path** → maps to `/source_data`
- **Container Role** → `source` or `client`
- **Hash Workers** → optional performance tuning
- **Timezone** → TZ string

---

## 9. Security Hardening

### Web UI Authentication

Set `MONARCHAEGIS_PASSWORD` to enable HTTP Basic Auth on all UI and API routes. Without it, the UI is open — suitable for isolated LANs only.

```bash
-e MONARCHAEGIS_USERNAME=admin \
-e MONARCHAEGIS_PASSWORD=your-password-here
```

The P2P endpoints (`/api/client/pair`, `/api/client/diff`) are exempt from Basic Auth because they are called machine-to-machine between containers. They use the `MONARCHAEGIS_PAIR_SECRET` mechanism instead.

### P2P Pair Secret

When `MONARCHAEGIS_PAIR_SECRET` is set, the `/api/client/pair` endpoint **requires** the `X-Pair-Secret` header to match. Calls without it receive HTTP 401 regardless of rate-limit state.

When no secret is configured, pairing falls back to IP-based rate limiting (`MONARCHAEGIS_PAIR_RATE_MAX` requests per `MONARCHAEGIS_PAIR_RATE_WINDOW` seconds per source IP).

Set the same secret on both Source and Client:

```bash
# Generate a secret
openssl rand -hex 32

# Apply to both containers
-e MONARCHAEGIS_PAIR_SECRET=abc123...
```

### SSH Key Jailing

Each generated SSH keypair is scoped to a single directory. The public key is written to `authorized_keys` with:

- `command="rrsync -wo <dir>"`: only rsync is allowed, confined to the jail directory, **write-only** (a Source can push into the directory but never read from it)
- `restrict` flag: no port forwarding, no agent forwarding, no X11, no PTY

A key generated for `/mnt/user/movies` cannot access `/mnt/user/tv` and cannot open a login shell. rrsync resolves paths relative to the jail root, so the Source's generated target config targets `host:/` — the real receiving directory is recorded as a `-- destpath:` comment in `monarchaegis.conf.lua`.

The canonical `authorized_keys` lives at `/config/authorized_keys` (persists across container recreates). Because sshd's `StrictModes` rejects key files on host-owned volumes, the container mirrors it to `/root/.ssh/authorized_keys` (root-owned, 600) at boot and on every pairing — that mirror is what sshd actually reads.

### SSH Host Key Pinning (TOFU)

On first connection to a remote host, the host key is saved to `/config/known_hosts` (Trust on First Use). Subsequent connections verify against this pinned key, preventing MITM substitution. The known_hosts file persists across container restarts via the `/config` volume.

The Client's own host keys are persisted too: on first boot they are snapshotted into `/config/ssh_host_keys/` and restored into the container on every subsequent boot. The server keeps one stable SSH identity across image updates, so pinned known_hosts entries on Sources never break after a `docker pull`.

### Path Allowlist

The local file browser and pair endpoint validate all paths against `MONARCHAEGIS_BROWSE_ALLOWED_PATHS`. Paths outside the allowlist return an error — the container cannot be used to browse `/etc`, `/root`, or any other sensitive path.

---

## 10. Feature Reference

### Sync Target Management

Add, edit, and delete sync targets through the UI. Each target is stored in the DB `targets` table (schedule, enabled, last-run) and mirrored to an internal `monarchaegis.conf.lua` used for SSH-key resolution and upgrade migration. Changes take effect immediately — the scheduler picks them up on its next tick; there is no daemon to restart.

Config writes are atomic: changes go to a temp file, fsynced, then renamed over the live file. An exclusive file lock prevents concurrent write races when multiple browser tabs are open.

### File Browser

Both the Source "Add Target" modal and the Client "Add Directory" modal include a file browser that traverses the local filesystem within the allowed path prefix. The browser shows directories only; navigation up to the filesystem root is allowed so you can drill down to any allowed prefix.

For remote targets, the browser uses `asyncssh` to traverse the remote filesystem without leaving the GUI.

### P2P Hash Verification

The **Sync Now** button (and the scheduler) run this whole flow automatically: diff the Source's hashes against the Client's ledger, push the delta, register what was sent. The steps below are the same mechanism, also exposed as manual audit tools:

1. **Baseline scan** — on startup the hash scanner walks every file in tracked directories and records `(path, size, mtime, xxHash3)` in SQLite; a lightweight watchdog marks changed files dirty for re-hashing. Parallel threads scale to available CPU cores.
2. **Diff** — the Source serializes its hash table and POSTs it to the Client's `/api/client/diff`. The Client compares against its **ledger** in-memory and returns the missing/superseded set (and any locally-modified files it is protecting).
3. **Push** — the Source runs `rsync --files-from -W` with only that set, pushing exactly those files without a full directory scan, then registers them in the Client's ledger.
4. **CSV export** — the manual audit path can download the missing-files list as CSV for review before syncing.

### Hashing Modes & Throughput

The baseline scanner records `(path, size, mtime, hash)` per file. The default `full` mode hashes the entire content (xxHash3-128). For large media libraries — where reading every byte is disk-bound and slow — two faster modes are available via `MONARCHAEGIS_HASH_MODE`:

- **`sampled`** — hashes exact size + the first/last `MONARCHAEGIS_HASH_SAMPLE_MB` (default 16) of each file. Reads ~32MB per file instead of the whole thing. Content-based, mtime-agnostic, works for every file type, catches truncation. Misses a change that lives only in the middle of a file with identical size (rare for media).
- **`metadata`** — for media files, builds a fingerprint from `ffprobe` technical metadata (container, video/audio codecs, resolution, channels, rounded duration) plus exact byte size, reading only container headers/index. Non-media files (`.nfo`, `.srt`, thumbnails) automatically fall back to `sampled`. Fastest for media, and mtime-agnostic so identical files placed by a previous tool still match.

**Both Source and Client must use the same mode** (and the same image, so `ffprobe` matches). A mismatch is detected at diff time and rejected with a clear message rather than silently flagging the whole library. Switching modes changes the stored hash algorithm and triggers a one-time re-hash.

The dashboard shows live throughput during a scan — files/second and MB/second — on both the Source and Client, and a completion summary is written to the log (e.g. `Baseline complete … Hashed 4210 (812.4 GB) … in 51.2s (82.2 files/s, 1620 MB/s)`). Use these numbers to tell whether you're disk-bound (try a faster mode, a direct disk path, or fewer workers) or actually CPU/probe-bound.

### Deferred Baseline (In-Situ Takeover)

When you add a Source directory or a Client receiving directory, the **"Start baseline hash scan immediately"** checkbox controls whether the full-content baseline runs now or later.

For a directory that already holds a large amount of data — e.g. taking over from an existing replication system without re-copying everything — **leave it unchecked**. The directory is registered but no files are read and no `inotify` watches are installed until you explicitly start the scan with the **▶ Start Scan** button. This avoids the full-content read storm that can saturate a NAS array (especially through Unraid's `/mnt/user` FUSE layer) the instant a populated directory is added.

What works while a baseline is deferred:

- **SSH key pairing** (**🔑 Generate Key** on the Client) — keys can be generated and exchanged immediately.
- **Pairing setup** — server profiles and keys can be created and exchanged so the target is fully linked and ready.

What waits until the scan completes:

- **Sync Now / scheduled sync / Preview / Sync Missing** — all need the target's baseline hashes to produce a complete diff; the Client also returns `not_ready` for that directory's diff until its baseline finishes. The gate is per-directory, so a deferred directory never blocks diffs for other directories that are already scanned.

The deferred state is persisted, so a container restart will **not** silently kick off a scan you postponed — it stays pending until you press **Start Scan**.

### Client Protection

If a file is modified locally on the Client, it is flagged in the Client's database. The diff then refuses to overwrite that file, protecting the local edit from a Source push. This protection is **server-side** in the diff logic; the standalone Conflicts UI panel was removed, since the ledger model makes unintended overwrites rare by design (the destination DB — not its files — is authoritative).

### SSH Key Repair

If a Source loses its private key (e.g., config volume wiped), use the **Repair Key** button. The Source requests a new keypair from the Client, updates its server profile, rewrites the target config to point to the new key, then deletes the old key — all in one atomic operation. If the config rewrite fails, the old key is retained so the failure is recoverable.

### Real-Time Log Streaming

The UI log window uses Server-Sent Events (SSE). The sync engine and recovery endpoints write progress lines directly into per-target log buckets, which are streamed to all connected browsers. (There are no daemon log files to tail — that machinery was removed with lsyncd.)

### Transfer History

Every completed rsync file transfer is parsed from the rsync log and inserted into the `transfer_history` SQLite table with timestamp, source path, and size. The History tab in the UI queries this table.

### Force Rescan

The **Force Rescan** button drops all existing hash records for a target and re-hashes the entire directory tree from scratch. Use this after a bulk copy, restore from backup, or any operation that may have bypassed the watchdog.

---

## 11. Architecture Overview

```
┌────────────────────────────────────────────────────┐
│                 Docker Container                     │
│                                                      │
│   ┌──────────┐                 ┌──────────┐          │
│   │  sshd    │                 │ uvicorn  │          │
│   │ :2222    │                 │  :5000   │          │
│   └──────────┘                 └────┬─────┘          │
│                                     │ FastAPI        │
│                              ┌──────┴───────────┐    │
│   scheduler / Sync Now  ───▶ │ main.py          │    │
│   diff → rsync -W → register │  sync_engine     │    │
│   ┌──────────┐   spawns      │  scheduler loop  │    │
│   │  rsync   │◀──────────────│  hash_scanner    │    │
│   │  (SSH)   │               │  server_mgr      │    │
│   └──────────┘               │  database  ◀── source│
│                              │  config_mgr    of truth
│                              └──────────────────┘    │
└────────────────────────────────────────────────────┘
         │                              │
    /config volume                 /source_data
    (monarchaegis.db = truth)         volume
```

No lsyncd daemon: replication is driven by the source-mode scheduler / **Sync Now**, which diffs against the destination ledger over HTTP and spawns `rsync` on demand.

**Component breakdown:**

| Component | File | Role |
|---|---|---|
| FastAPI app | `app/main.py` | All REST API endpoints, auth middleware, SSE log streaming, scheduler loop |
| Sync engine | `app/sync_engine.py` | One sync run (diff → transfer → register) + the scheduling logic (next-run/due) |
| Database | `app/database.py` | SQLite; the source of truth — `targets` (schedule/state), hashes, history, settings |
| Config manager | `app/config_manager.py` | Internal Lua target store: SSH-key resolution + one-shot upgrade migration parser |
| Log router | `app/log_router.py` | Per-target in-memory log buckets streamed over SSE (no file tailing) |
| Hash scanner | `app/hash_scanner.py` | Parallel file hashing engine; watchdog marks changed files dirty for re-hash |
| Server manager | `app/server_manager.py` | SSH key generation, storage, `authorized_keys` (`rrsync` jail) management |
| Frontend | `app/static/` | Vanilla HTML/CSS/JS; communicates via REST + SSE |

---

## 12. Troubleshooting

### Nothing syncs / no scheduled runs happen

Replication is initiated by the **source** role only. Check the role dropdown at the top of the UI — a container running as `client` receives pushes but never starts syncs itself. Switch it to `source` on the sending side (the dropdown is authoritative; `MONARCHAEGIS_ROLE` only applied on first launch). Then confirm the target has a schedule, or trigger **Sync Now**.

### "Access denied: path outside allowed directories" in file browser

The path you navigated to is outside `MONARCHAEGIS_BROWSE_ALLOWED_PATHS`. Either navigate to a path within `/source_data` (or your configured prefix), or add the root prefix to the env var:

```bash
-e MONARCHAEGIS_BROWSE_ALLOWED_PATHS=/source_data,/mnt,/data,/your/custom/path
```

### Pairing fails with HTTP 401

`MONARCHAEGIS_PAIR_SECRET` is set on the Client but the Source is not sending it (or sending the wrong value). Verify both containers have the exact same `MONARCHAEGIS_PAIR_SECRET` value.

### Pairing fails with HTTP 429

Rate limit hit. Either wait out the window (`MONARCHAEGIS_PAIR_RATE_WINDOW` seconds, default 60s) or set `MONARCHAEGIS_PAIR_SECRET` on both containers to bypass rate limiting.

### rsync exits with code 255 (SSH auth failure)

The private key on the Source doesn't match the public key in the Client's `authorized_keys`. Use the **Repair Key** button on the target in the Source UI to re-pair.

### rsync exits with code 23 (partial transfer)

Usually a permission issue on the Client's receiving directory. Ensure the user that `sshd` runs as has write access to the target directory.

### A destination file isn't overwritten / keeps getting re-sent

The destination **ledger** (not the filesystem) decides what's present: a file is (re)sent only when its path is absent from the ledger, or the source's fingerprint supersedes the ledger's record for that path. Intentional destination-side edits (e.g. AV1 re-encodes) are preserved by design and won't be clobbered. If you *want* the source to re-push everything, run a destination full-rehash to reconcile the ledger to on-disk truth.

### Hash scan never completes / high CPU

Reduce `MONARCHAEGIS_HASH_WORKERS` to leave headroom for other processes. On spinning-disk NAS arrays, disk I/O saturates before CPU — setting workers above 4–8 typically has no benefit on HDDs.

### Whole host becomes unresponsive when a target starts syncing (Unraid)

The baseline hash scanner reads every file under the source path (and the dirty-marking watchdog installs a light directory watch). If the source is mounted through Unraid's user-share layer (`/mnt/user/...`), that I/O funnels through the single shfs/FUSE process and can stall the entire server. (The old lsyncd startup inotify storm is gone — lsyncd is retired — so the remaining risk is the initial scan itself.)

Recovery: add `MONARCHAEGIS_SAFE_MODE=1` to the container, restart it — the UI comes up with the scheduler and the scanner disabled — then narrow or delete the offending target. Remove the variable to resume.

Prevention:
1. **Defer the baseline** on large pre-existing directories: uncheck "Start baseline hash scan immediately" when adding the directory (see Deferred Baseline above), then start the scan deliberately during a low-traffic window. Registration alone reads no files.
2. **Bypass shfs**: map the data volume to a direct disk path (`/mnt/cache/<share>` or `/mnt/diskN/<share>`) instead of `/mnt/user/<share>`. Makes the initial scan dramatically faster.
3. **Scope the source path** to the specific folder you sync, never a parent that contains everything.
4. **Lower `MONARCHAEGIS_HASH_WORKERS`** (4 is plenty on spinning disks) so baseline hashing doesn't saturate the array.

### Known_hosts mismatch error

The remote server's host key changed (e.g., OS reinstall, or the Client's `/config/ssh_host_keys/` was wiped). Use the **Trust New Key** button on the target, or delete the stale entry from `/config/known_hosts` and reconnect; TOFU will re-pin the new key. Note that normal image updates no longer change the host key — it persists in `/config/ssh_host_keys/`.

### All transfers fail with code 255 after upgrading

Older versions pointed sshd directly at `/config/authorized_keys`; sshd `StrictModes` silently rejects that file on host-owned volumes, failing all key auth. Current versions mirror the keys to `/root/.ssh/authorized_keys` at boot. Upgrade both containers to the same image and re-pair each target once (**Re-pair Keys** button on the Source).

### Config file corrupted after crash

A `.bak` timestamped backup is created before every config write. Find the most recent backup in `/config/` (e.g., `monarchaegis.conf.lua.20260501_143022.bak`) and restore:

```bash
cp /config/monarchaegis.conf.lua.20260501_143022.bak /config/monarchaegis.conf.lua
```

Then restart the container so the restored config is reloaded.
