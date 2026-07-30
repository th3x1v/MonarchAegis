# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

import os
from envcompat import env
import re
import stat
import json
import time
import asyncio
import subprocess
import concurrent.futures
import xxhash
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from database import db

# --- Paths that must never be catalogued or replicated ---
#
# In-flight temp copies and our own version store. Cataloguing a temp file
# guarantees an orphan ledger row the moment it is renamed away — Sonarr/Radarr
# write `*.partial~` while copying an import, and rsync writes `.<name>.XXXXXX`
# (we have seen exactly that land in a ledger). Cataloguing `.versions/` would
# replicate retired copies to the destination and then start versioning the
# versions.
EXCLUDED_DIRS = {".versions"}
_EXCLUDED_FILE_RE = re.compile(
    r"\.partial~$"                  # Sonarr/Radarr in-flight import copy
    r"|\.part$"                     # generic / browser partial
    r"|\.!qB$"                      # qBittorrent
    r"|\.crdownload$"               # chromium
    r"|^\..+\.[A-Za-z0-9]{6}$",     # rsync temp: .<name>.XXXXXX
    re.IGNORECASE,
)


def is_excluded_path(rel_path: str) -> bool:
    """True if this target-relative path is an in-flight temp file or lives inside
    the version store — such files must not be hashed, tracked, or replicated."""
    parts = (rel_path or "").replace("\\", "/").strip("/").split("/")
    if any(p in EXCLUDED_DIRS for p in parts[:-1]):
        return True
    return bool(_EXCLUDED_FILE_RE.search(parts[-1])) if parts else False

# Extensions treated as media for metadata-fingerprint mode (MONARCHAEGIS_HASH_MODE=metadata).
# Anything not listed falls back to a sampled content hash.
MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts",
    ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".ogv", ".3gp", ".divx",
    ".mp3", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".alac",
}
# Max seconds to wait for an ffprobe call before falling back to a content hash.
FFPROBE_TIMEOUT = float(env("MONARCHAEGIS_FFPROBE_TIMEOUT", "30"))

# DB settings key holding the list of target ids whose baseline scan is
# deferred (registered but never hashed). Persisted so a container restart
# does not silently kick off a scan the operator explicitly postponed.
PENDING_BASELINE_KEY = "pending_baseline_ids"

# Configuration
HASH_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks — minimises syscall overhead on large media files
SCAN_DEBOUNCE_SECONDS = 5.0
DB_BATCH_SIZE = 500  # Flush to SQLite every N files during baseline scan

# Scale worker count to available CPU cores (configurable via env var)
# xxhash releases the GIL during C-level hashing, so threads
# genuinely parallelize across cores for this workload.
HASH_WORKERS = int(env("MONARCHAEGIS_HASH_WORKERS", max(min(os.cpu_count() or 4, 16), 2)))

# Thread pool dedicated to file I/O so the event loop is NEVER blocked
_io_executor = concurrent.futures.ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="hash_io")

class HashEventHandler(FileSystemEventHandler):
    """
    Listens for filesystem events (created, modified, deleted, moved)
    and relays them to the HashScanner for processing.
    """
    def __init__(self, scanner, target_id: str, base_path: str):
        self.scanner = scanner
        self.target_id = target_id
        self.base_path = base_path

    def _get_relative_path(self, absolute_path: str) -> str:
        try:
            return os.path.relpath(absolute_path, self.base_path)
        except ValueError:
            return absolute_path

    def on_created(self, event):
        if not event.is_directory:
            rel_path = self._get_relative_path(event.src_path)
            self.scanner.queue_file_update(self.target_id, self.base_path, rel_path)

    def on_modified(self, event):
        if not event.is_directory:
            rel_path = self._get_relative_path(event.src_path)
            self.scanner.queue_file_update(self.target_id, self.base_path, rel_path)

    def on_deleted(self, event):
        if not event.is_directory:
            rel_path = self._get_relative_path(event.src_path)
            self.scanner.queue_file_deletion(self.target_id, rel_path)

    def on_moved(self, event):
        if not event.is_directory:
            # A move is a delete of the old path and a create of the new path
            old_rel_path = self._get_relative_path(event.src_path)
            new_rel_path = self._get_relative_path(event.dest_path)
            
            self.scanner.queue_file_deletion(self.target_id, old_rel_path)
            self.scanner.queue_file_update(self.target_id, self.base_path, new_rel_path)


class HashScanner:
    """
    Manages background hashing of configured target directories.
    Handles startup baseline scans and ongoing watchdog events.
    
    ALL blocking file I/O runs in a dedicated ThreadPoolExecutor
    so the asyncio event loop (and therefore the web GUI) is never blocked.
    """
    def __init__(self):
        self.observer = Observer()
        self.active_targets = {} # {target_id: {'path': str, 'watch': watch_object}}
        
        # We use an asyncio queue to process file hashing sequentially
        # so we don't completely lock up the container's IO or CPU.
        self.update_queue = asyncio.Queue()
        self.is_running = False
        
        # Scan readiness tracking: {target_id: {status, hashed, skipped, total}}
        self.scan_status = {}

    def start(self):
        """Starts the watchdog observer and the background processing task."""
        if not self.is_running:
            self.observer.start()
            self.is_running = True
            asyncio.create_task(self._process_queue())
            print("HashScanner: Watchdog daemon started.")

    def stop(self):
        """Stops the observer."""
        if self.is_running:
            self.observer.stop()
            self.observer.join()
            self.is_running = False
            print("HashScanner: Watchdog daemon stopped.")

    # --- Deferred-baseline persistence --- #

    def _get_pending_ids(self) -> set:
        try:
            return set(json.loads(db.get_setting(PENDING_BASELINE_KEY, "[]")))
        except (json.JSONDecodeError, TypeError):
            return set()

    def _set_pending(self, target_id: str, pending: bool):
        ids = self._get_pending_ids()
        if pending:
            ids.add(target_id)
        else:
            ids.discard(target_id)
        db.set_setting(PENDING_BASELINE_KEY, json.dumps(sorted(ids)))

    def is_baseline_pending(self, target_id: str) -> bool:
        return target_id in self._get_pending_ids()

    def add_target(self, target_id: str, base_path: str, defer_baseline: bool = False):
        """Registers a target directory to be scanned and watched.

        With defer_baseline=True the directory is only REGISTERED: no inotify
        watches are installed and no files are read until the operator
        explicitly starts the baseline (force_rescan). This is essential for
        in-situ takeovers where the directory already holds terabytes —
        walking and hashing it immediately can saturate the host (especially
        through FUSE layers like Unraid's /mnt/user). The deferred state
        persists across restarts.
        """
        if target_id in self.active_targets:
            # Already watching
            return

        if not os.path.exists(base_path) or not os.path.isdir(base_path):
            print(f"HashScanner: Cannot watch target {target_id}, path does not exist: {base_path}")
            return

        if defer_baseline:
            print(f"HashScanner: Registered {target_id} ({base_path}) with DEFERRED baseline — "
                  f"no scanning until explicitly started.")
            self.active_targets[target_id] = {'path': base_path, 'watch': None}
            self.scan_status[target_id] = {"status": "pending", "hashed": 0, "skipped": 0, "total": 0}
            self._set_pending(target_id, True)
            return

        print(f"HashScanner: Adding target {target_id} ({base_path})")

        # 1. Attach the real-time watchdog Event Handler
        event_handler = HashEventHandler(self, target_id, base_path)
        watch = self.observer.schedule(event_handler, base_path, recursive=True)

        self.active_targets[target_id] = {
            'path': base_path,
            'watch': watch
        }

        # 2. Queue a complete baseline scan in the background
        asyncio.create_task(self._perform_baseline_scan(target_id, base_path))

    def force_rescan(self, target_id: str = None) -> int:
        """Clears the hash DB and re-queues a full baseline scan.

        Also serves as the explicit "start baseline now" trigger for targets
        registered with a deferred baseline — the inotify watch is attached
        here if it wasn't installed at registration time.

        If target_id is given, only that target is rescanned.
        If target_id is None, ALL active targets are rescanned.
        Returns the number of targets queued.
        """
        targets = [target_id] if target_id else list(self.active_targets.keys())
        queued = 0
        for tid in targets:
            if tid not in self.active_targets:
                continue
            base_path = self.active_targets[tid]['path']
            # Deferred target starting its first scan: attach the watchdog now
            # so changes arriving during/after the baseline are tracked.
            if self.active_targets[tid].get('watch') is None:
                event_handler = HashEventHandler(self, tid, base_path)
                self.active_targets[tid]['watch'] = self.observer.schedule(
                    event_handler, base_path, recursive=True
                )
            db.clear_file_hashes(tid)
            self.scan_status[tid] = {"status": "scanning", "hashed": 0, "skipped": 0, "total": 0}
            asyncio.create_task(self._perform_baseline_scan(tid, base_path))
            queued += 1
        return queued

    def remove_target(self, target_id: str):
        """Unregisters a target from being watched."""
        if target_id in self.active_targets:
            if self.active_targets[target_id].get('watch') is not None:
                self.observer.unschedule(self.active_targets[target_id]['watch'])
            del self.active_targets[target_id]
            self.scan_status.pop(target_id, None)
            self._set_pending(target_id, False)
            print(f"HashScanner: Removed target {target_id}")

    # --- Hashing & Processing --- #

    def _full_content_hash(self, filepath: str) -> str:
        """xxHash3-128 over the entire file. Slowest but byte-exact."""
        hasher = xxhash.xxh3_128()
        with open(filepath, "rb") as f:
            for byte_block in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
                hasher.update(byte_block)
        return hasher.hexdigest()

    def _sampled_content_hash(self, filepath: str, size: int) -> str:
        """xxHash3-128 over size + first/last SAMPLE_BYTES only.

        Reads ~2*SAMPLE_BYTES instead of the whole file. Content-based and
        mtime-agnostic; catches truncation and any head/tail difference. Files
        at or below 2*SAMPLE_BYTES are hashed in full (same as _full_content_hash).
        """
        sample = db.HASH_SAMPLE_BYTES
        hasher = xxhash.xxh3_128()
        hasher.update(str(size).encode())
        with open(filepath, "rb") as f:
            if size <= 2 * sample:
                for byte_block in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
                    hasher.update(byte_block)
            else:
                hasher.update(f.read(sample))            # head
                f.seek(-sample, os.SEEK_END)
                hasher.update(f.read(sample))            # tail
        return hasher.hexdigest()

    def _metadata_fingerprint(self, filepath: str, size: int) -> str:
        """Fingerprint a MEDIA file from its technical metadata (codecs,
        resolution, channels, rounded duration) plus exact byte size — read via
        ffprobe, which only parses container headers/index, not the whole stream.

        mtime-agnostic and fast. Returns None for non-media files or any ffprobe
        failure so the caller can fall back to a content hash. Both Source and
        Client must run the same image (same ffprobe) for fingerprints to match.
        """
        ext = os.path.splitext(filepath)[1].lower()
        if ext not in MEDIA_EXTENSIONS:
            return None
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", filepath],
                capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            data = json.loads(proc.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            return None

        # Build a canonical, version-stable fingerprint string. Exact size comes
        # from stat (authoritative); duration is rounded to whole seconds to avoid
        # sub-second jitter; bitrate is intentionally excluded (reporting varies).
        parts = [f"size={size}"]
        fmt = data.get("format", {})
        parts.append(f"fmt={fmt.get('format_name', '')}")
        dur = fmt.get("duration")
        if dur:
            try:
                parts.append(f"dur={round(float(dur))}")
            except (TypeError, ValueError):
                pass
        for s in data.get("streams", []):
            ct = s.get("codec_type")
            if ct == "video":
                parts.append(f"v:{s.get('codec_name')}:{s.get('width')}x{s.get('height')}")
            elif ct == "audio":
                parts.append(f"a:{s.get('codec_name')}:{s.get('channels')}:{s.get('sample_rate')}")
            elif ct == "subtitle":
                parts.append(f"s:{s.get('codec_name')}")
        fingerprint = "|".join(str(p) for p in parts)
        return xxhash.xxh3_128(fingerprint.encode("utf-8")).hexdigest()

    def _compute_hash(self, filepath: str, size: int) -> str:
        """Dispatches to the active hashing strategy (MONARCHAEGIS_HASH_MODE).

        full     -> entire file content
        sampled  -> size + head/tail
        metadata -> ffprobe fingerprint for media, sampled content hash otherwise

        This is a BLOCKING call — always invoke via run_in_executor().
        """
        try:
            mode = db.HASH_MODE
            if mode == "metadata":
                fp = self._metadata_fingerprint(filepath, size)
                if fp is not None:
                    return fp
                return self._sampled_content_hash(filepath, size)  # non-media fallback
            if mode == "sampled":
                return self._sampled_content_hash(filepath, size)
            return self._full_content_hash(filepath)
        except (IOError, OSError) as e:
            print(f"HashScanner: Hash failed for {filepath}: {e}")
            return ""

    def _stat_and_hash_file(self, abs_path: str, is_symlink_check: bool = False) -> dict:
        """Blocking helper: stats a file and computes its hash.

        Returns a dict with size, mtime, hash, is_link — or None on error.
        Always run via run_in_executor().
        """
        try:
            stat_info = os.lstat(abs_path)
            size = stat_info.st_size
            mtime = stat_info.st_mtime
            is_link = stat.S_ISLNK(stat_info.st_mode)

            if is_link:
                target_path = os.readlink(abs_path)
                file_hash = xxhash.xxh3_128(target_path.encode('utf-8')).hexdigest()
            else:
                file_hash = self._compute_hash(abs_path, size)

            return {"size": size, "mtime": mtime, "hash": file_hash, "is_link": is_link}
        except Exception as e:
            print(f"HashScanner: stat/hash failed for {abs_path}: {e}")
            return None

    def queue_file_update(self, target_id: str, base_path: str, rel_path: str):
        """Called by watchdog when a file is created/modified."""
        if is_excluded_path(rel_path):
            return                    # temp copy / version store — never track it
        self.update_queue.put_nowait({
            'action': 'update',
            'target_id': target_id,
            'base_path': base_path,
            'rel_path': rel_path
        })

    def queue_file_deletion(self, target_id: str, rel_path: str):
        """Called by watchdog when a file is deleted."""
        # Must skip excluded paths here too: tombstoning INSERTs a row, so a
        # deleted temp file would otherwise *create* the very ledger entry the
        # exclusion exists to prevent.
        if is_excluded_path(rel_path):
            return
        self.update_queue.put_nowait({
            'action': 'delete',
            'target_id': target_id,
            'rel_path': rel_path
        })

    async def _process_queue(self):
        """
        Background infinite loop that pops paths off the queue, hashes them
        in a thread executor, and saves them to the SQLite database.
        """
        loop = asyncio.get_event_loop()
        
        while self.is_running:
            item = await self.update_queue.get()
            
            try:
                target_id = item['target_id']
                action = item['action']
                rel_path = item['rel_path']

                if action == 'delete':
                    db.tombstone_file_hash(target_id, rel_path)
                    print(f"HashScanner: Local deletion tombstoned for {rel_path}")
                elif action == 'update':
                    base_path = item['base_path']
                    abs_path = os.path.join(base_path, rel_path)
                    
                    # Run ALL blocking I/O in the thread executor
                    result = await loop.run_in_executor(
                        _io_executor, self._stat_and_hash_file, abs_path
                    )
                    
                    if result and result["hash"]:
                        db.upsert_file_hash(
                            target_id=target_id,
                            filepath=rel_path,
                            size=result["size"],
                            mtime=result["mtime"],
                            file_hash=result["hash"],
                            modified_locally=True
                        )
                        print(f"HashScanner: Hashed {rel_path} -> {result['hash'][:8]}...")
            except Exception as e:
                print(f"HashScanner error processing {item}: {e}")
            finally:
                self.update_queue.task_done()

    def is_scan_complete(self, target_id: str = None) -> bool:
        """Returns True if all (or a specific) target baseline scans are finished."""
        if target_id:
            status = self.scan_status.get(target_id, {})
            return status.get("status") == "complete"
        # If no specific target, check ALL targets
        if not self.scan_status:
            return False
        return all(s.get("status") == "complete" for s in self.scan_status.values())
    
    def get_scan_status(self) -> dict:
        """Returns the scan status for all targets."""
        return self.scan_status

    def _blocking_walk_and_stat(self, base_path: str) -> list:
        """Walks the directory tree and returns (rel_path, abs_path, stat_info) tuples.
        
        This is a BLOCKING call — always invoke via run_in_executor().
        """
        results = []
        for root, dirs, files in os.walk(base_path):
            # Prune excluded directories in place so we never descend into them
            # (also keeps the version store off the walk entirely).
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for file in files:
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, base_path)
                if is_excluded_path(rel_path):
                    continue          # in-flight temp copy — tracking it creates an orphan row
                try:
                    stat_info = os.lstat(abs_path)
                    results.append((rel_path, abs_path, stat_info))
                except Exception as e:
                    print(f"HashScanner: stat failed during walk for {rel_path}: {e}")
        return results

    async def _perform_baseline_scan(self, target_id: str, base_path: str):
        """
        Walks the entire directory on startup, hashing any file that 
        does not exist in the DB or has out-of-date sizing/timestamps.
        
        All blocking I/O runs in a thread executor so the web GUI stays responsive.
        Files are hashed in parallel across HASH_WORKERS threads for maximum throughput.
        """
        loop = asyncio.get_event_loop()
        
        print(f"HashScanner: Generating baseline for {target_id} using {HASH_WORKERS} parallel "
              f"workers (mode={db.HASH_MODE})...")
        scan_start = time.monotonic()
        self.scan_status[target_id] = {
            "status": "scanning", "hashed": 0, "skipped": 0, "total": 0,
            "bytes_hashed": 0, "elapsed": 0.0, "files_per_sec": 0.0, "mb_per_sec": 0.0,
            "mode": db.HASH_MODE,
        }
        bytes_hashed = 0
        
        # 1. Pull the existing known-good baseline from SQLite
        existing_hashes = { row['filepath']: row for row in db.get_all_hashes(target_id) }
        
        # 2. Walk the directory tree in a thread (this can take a while on huge dirs)
        print(f"HashScanner: Walking directory tree for {target_id}...")
        file_entries = await loop.run_in_executor(_io_executor, self._blocking_walk_and_stat, base_path)
        print(f"HashScanner: Found {len(file_entries)} files for {target_id}.")
        
        scanned_paths = set()
        hashed_count = 0
        skipped_count = 0
        
        # Concurrency gate: limits how many hash tasks run simultaneously
        semaphore = asyncio.Semaphore(HASH_WORKERS)
        
        # Shared counters protected by a lock (coroutines on the same loop
        # don't truly race, but this keeps the pattern explicit and safe)
        counter_lock = asyncio.Lock()
        
        # Accumulator for batch DB writes — flushed every DB_BATCH_SIZE files
        pending_rows = []

        async def flush_pending(force: bool = False):
            """Write accumulated rows to DB in one transaction."""
            nonlocal pending_rows
            if pending_rows and (force or len(pending_rows) >= DB_BATCH_SIZE):
                rows_to_write = pending_rows
                pending_rows = []
                await loop.run_in_executor(_io_executor, db.batch_upsert_file_hashes, rows_to_write)

        async def hash_single_file(rel_path, abs_path, stat_info):
            """Hash one file under the semaphore, accumulate result for batch DB write."""
            nonlocal hashed_count, skipped_count, bytes_hashed

            async with semaphore:
                try:
                    current_size = stat_info.st_size
                    current_mtime = stat_info.st_mtime

                    # Compare against known baseline — only rehash if changed
                    needs_hash = True
                    if rel_path in existing_hashes:
                        db_row = existing_hashes[rel_path]
                        if db_row['size'] == current_size and abs(db_row['mtime'] - current_mtime) < 1.0:
                            needs_hash = False

                    if needs_hash:
                        if stat.S_ISLNK(stat_info.st_mode):
                            target_path = os.readlink(abs_path)
                            file_hash = xxhash.xxh3_128(target_path.encode('utf-8')).hexdigest()
                        else:
                            file_hash = await loop.run_in_executor(
                                _io_executor, self._compute_hash, abs_path, current_size
                            )

                        if file_hash:
                            was_local = existing_hashes.get(rel_path, {}).get('modified_locally', False)
                            pending_rows.append((target_id, rel_path, current_size, current_mtime, file_hash, was_local))
                            async with counter_lock:
                                hashed_count += 1
                                bytes_hashed += current_size
                                elapsed = time.monotonic() - scan_start
                                st = self.scan_status[target_id]
                                st["hashed"] = hashed_count
                                st["total"] = hashed_count + skipped_count
                                st["bytes_hashed"] = bytes_hashed
                                st["elapsed"] = round(elapsed, 1)
                                if elapsed > 0:
                                    st["files_per_sec"] = round(hashed_count / elapsed, 1)
                                    st["mb_per_sec"] = round(bytes_hashed / elapsed / (1024 * 1024), 1)

                            await flush_pending()
                    else:
                        async with counter_lock:
                            skipped_count += 1
                            self.scan_status[target_id]["skipped"] = skipped_count
                            self.scan_status[target_id]["total"] = hashed_count + skipped_count

                except Exception as e:
                    print(f"HashScanner: Error baseline scanning {rel_path}: {e}")

        # 3. Fire all hash tasks concurrently, bounded by the semaphore
        for rel_path, *_ in file_entries:
            scanned_paths.add(rel_path)

        # Build all tasks and run them in parallel
        tasks = [
            hash_single_file(rel_path, abs_path, stat_info)
            for rel_path, abs_path, stat_info in file_entries
        ]

        # Process in batches to keep memory bounded and yield to the event loop
        BATCH_SIZE = HASH_WORKERS * 4
        for i in range(0, len(tasks), BATCH_SIZE):
            batch = tasks[i:i + BATCH_SIZE]
            await asyncio.gather(*batch)
            await asyncio.sleep(0)

        # Flush any remaining rows that didn't fill a full batch
        await flush_pending(force=True)
                    
        # 4. Clean up deleted paths from DB (files that exist in DB but weren't walked)
        deleted_paths = set(existing_hashes.keys()) - scanned_paths
        for rel_path in deleted_paths:
            db.remove_file_hash(target_id, rel_path)
        
        elapsed = max(time.monotonic() - scan_start, 1e-6)
        fps = round(hashed_count / elapsed, 1)
        mbps = round(bytes_hashed / elapsed / (1024 * 1024), 1)
        self.scan_status[target_id] = {
            "status": "complete",
            "hashed": hashed_count,
            "skipped": skipped_count,
            "total": hashed_count + skipped_count,
            "bytes_hashed": bytes_hashed,
            "elapsed": round(elapsed, 1),
            "files_per_sec": fps,
            "mb_per_sec": mbps,
            "mode": db.HASH_MODE,
        }
        # A completed baseline clears any deferred flag so future restarts
        # resume normal incremental scanning for this target.
        self._set_pending(target_id, False)
        gb = bytes_hashed / (1024 ** 3)
        print(f"HashScanner: Baseline complete for {target_id} (mode={db.HASH_MODE}). "
              f"Hashed {hashed_count} ({gb:.2f} GB), Skipped {skipped_count}, "
              f"Removed {len(deleted_paths)} orphans in {elapsed:.1f}s "
              f"({fps} files/s, {mbps} MB/s).")

# Instantiate global singleton
hash_scanner_daemon = HashScanner()

