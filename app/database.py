# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

import sqlite3
from envcompat import env
import os
from typing import List, Optional

DB_PATH = env("MONARCHAEGIS_DB_PATH", "/config/monarchaegis.db")

# --- Hashing mode (shared by database + hash_scanner) ---
# "full"    : hash the entire file content (default; safest).
# "sampled" : hash only size + the first/last SAMPLE_MB of each file. Far faster
#             on large media (reads ~2*SAMPLE_MB instead of the whole file) and,
#             unlike size+mtime, is mtime-agnostic so identical files placed by a
#             different tool still match. MUST be identical on Source and Client.
HASH_MODE = env("MONARCHAEGIS_HASH_MODE", "full").lower()
HASH_SAMPLE_MB = max(1, int(env("MONARCHAEGIS_HASH_SAMPLE_MB", "16")))
HASH_SAMPLE_BYTES = HASH_SAMPLE_MB * 1024 * 1024
# The algo string is persisted; changing mode/sample-size changes it, which
# triggers the automatic re-hash migration below. Source and Client must use
# the same string or every file will appear different in the diff.
if HASH_MODE == "metadata":
    HASH_ALGO_STRING = "xxh3_128_meta"
elif HASH_MODE == "sampled":
    HASH_ALGO_STRING = f"xxh3_128_s{HASH_SAMPLE_MB}"
else:
    HASH_ALGO_STRING = "xxh3_128"

# Fallback for local testing outside of docker
if "MONARCHAEGIS_DB_PATH" not in os.environ and not os.path.exists("/config"):
    local_mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "mock_data", "config", "monarchaegis.db")
    os.makedirs(os.path.dirname(local_mock_path), exist_ok=True)
    DB_PATH = local_mock_path

class DatabaseManager:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # Current hash algorithm — changing this triggers an automatic re-scan.
    # Reflects the active hashing mode (full/sampled/metadata) so switching modes
    # cleanly invalidates and rebuilds the hash database.
    HASH_ALGO = HASH_ALGO_STRING
    # Expose the hashing-mode config on the instance so hash_scanner can read it
    # via the shared `db` singleton (these mirror the module-level constants).
    HASH_MODE = HASH_MODE
    HASH_SAMPLE_MB = HASH_SAMPLE_MB
    HASH_SAMPLE_BYTES = HASH_SAMPLE_BYTES

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS missing_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(target_id, filepath)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(target_id, filepath)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transfer_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    action TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS file_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    file_hash TEXT NOT NULL,
                    modified_locally BOOLEAN DEFAULT FALSE,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(target_id, filepath)
                )
            ''')
            
            # --- Phase 1 (DB-driven replication): the `targets` table ---
            # Authoritative store for replication targets, replacing the Lua
            # config as the source of truth. `id` deliberately reuses the same
            # target_id already used in file_hashes/missing_files (config-derived
            # for paired targets, "source_xxx" for unpaired) so existing hash rows
            # stay associated after migration. `server_id` references a profile in
            # servers.json (NULL for local/unpaired). `interval_seconds` = 0 means
            # manual-only. Nothing reads this table yet — populated by the one-shot
            # migration; lsyncd still drives behavior until later phases.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    server_id TEXT,
                    dest_path TEXT,
                    interval_seconds INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run TIMESTAMP,
                    last_status TEXT,
                    last_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_targets_name ON targets(name)')

            # Create indexes for fast differential querying
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_hashes_target ON file_hashes(target_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_hashes_filepath ON file_hashes(filepath)')

            # Migration: rename sha256_hash column to file_hash for existing databases
            cursor.execute("PRAGMA table_info(file_hashes)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'sha256_hash' in columns and 'file_hash' not in columns:
                print("DatabaseManager: Renaming sha256_hash -> file_hash column.")
                cursor.execute("ALTER TABLE file_hashes RENAME COLUMN sha256_hash TO file_hash")

            conn.commit()

        # Migration: if the stored hash algorithm doesn't match the current one,
        # wipe file_hashes so the scanner re-hashes everything with the new algo.
        stored_algo = self.get_setting("hash_algo")
        if stored_algo != self.HASH_ALGO:
            print(f"DatabaseManager: Hash algo changed ({stored_algo!r} -> {self.HASH_ALGO!r}). Clearing file_hashes for re-scan.")
            with self.get_connection() as conn:
                conn.execute("DELETE FROM file_hashes")
                conn.commit()
            self.set_setting("hash_algo", self.HASH_ALGO)

    # --- Settings Methods --- #

    def get_setting(self, key: str, default: str = None) -> str:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return row[0]
            return default

    def set_setting(self, key: str, value: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''', (key, value))
            conn.commit()

    # --- Hash Database Methods --- #
    
    def upsert_file_hash(self, target_id: str, filepath: str, size: int, mtime: float, file_hash: str, modified_locally: bool = False):
        """Inserts or updates a tracked file's metadata and hash."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO file_hashes (target_id, filepath, size, mtime, file_hash, modified_locally, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(target_id, filepath) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    file_hash=excluded.file_hash,
                    modified_locally=excluded.modified_locally,
                    last_updated=CURRENT_TIMESTAMP
            ''', (target_id, filepath, size, mtime, file_hash, modified_locally))
            conn.commit()

    def batch_upsert_file_hashes(self, rows: list):
        """
        Inserts or updates a batch of file hashes in a single transaction.
        rows: list of (target_id, filepath, size, mtime, file_hash, modified_locally)
        This collapses N individual commits into one — critical for baseline scan performance.
        """
        if not rows:
            return
        with self.get_connection() as conn:
            conn.executemany('''
                INSERT INTO file_hashes (target_id, filepath, size, mtime, file_hash, modified_locally, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(target_id, filepath) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    file_hash=excluded.file_hash,
                    modified_locally=excluded.modified_locally,
                    last_updated=CURRENT_TIMESTAMP
            ''', rows)
            conn.commit()

    def clear_file_hashes(self, target_id: str = None):
        """Deletes all hash records for a specific target, or ALL targets if target_id is None.
        Used by force-rescan to make the baseline scanner treat every file as new."""
        with self.get_connection() as conn:
            if target_id:
                conn.execute("DELETE FROM file_hashes WHERE target_id = ?", (target_id,))
            else:
                conn.execute("DELETE FROM file_hashes")
            conn.commit()

    def migrate_target_id(self, old_id: str, new_id: str):
        """Re-keys all per-target records from old_id to new_id.

        Used when an unpaired source (registered under a temporary id) is linked
        and adopts the config-derived target id — without this, the already-computed
        hashes stay under the old id and Preview Sync sees zero source files.
        UPDATE OR REPLACE collapses any (rare) collision on the UNIQUE(target_id,
        filepath) constraint onto the migrated row.
        """
        if old_id == new_id:
            return
        with self.get_connection() as conn:
            for table in ("file_hashes", "missing_files", "conflicts"):
                conn.execute(
                    f"UPDATE OR REPLACE {table} SET target_id = ? WHERE target_id = ?",
                    (new_id, old_id),
                )
            conn.commit()

    def factory_reset_data(self):
        """Clears all per-target operational data (hashes, missing files, conflicts,
        transfer history) and the registered-directory settings. Auth files on disk
        are wiped separately by the API layer."""
        with self.get_connection() as conn:
            for table in ("file_hashes", "missing_files", "conflicts", "transfer_history", "targets"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM settings WHERE key IN ('client_targets', 'unpaired_sources', 'pending_baseline_ids', 'targets_migrated', 'target_servers_reresolved', 'authorized_keys_dispatcher_upgraded')")
            conn.commit()

    # --- Targets table (Phase 1: DB-driven replication) --- #

    # Shared upsert statement for the targets table, used by both the single-row
    # upsert_target and the bulk migrate path so the column list / conflict rule
    # only live in one place. Column order: id, name, source_path, server_id,
    # dest_path, interval_seconds, enabled.
    _TARGET_UPSERT_SQL = '''
        INSERT INTO targets (id, name, source_path, server_id, dest_path,
                             interval_seconds, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            source_path=excluded.source_path,
            server_id=excluded.server_id,
            dest_path=excluded.dest_path,
            interval_seconds=excluded.interval_seconds,
            enabled=excluded.enabled
    '''

    @staticmethod
    def _server_index(servers: List[dict]) -> dict:
        """Index servers.json profiles for O(1) resolution. Entries:
          "key:<key_id>"       -> server_id   (UNIQUE — the reliable identifier)
          "host:<host>"        -> server_id   (fallback; first server on a host wins)
          "host:<user@host>"   -> server_id   (fallback)
        Keying by key_id matters because several rrsync-jailed targets can share the
        same host+user (distinguished only by their key), and host alone is ambiguous."""
        idx = {}
        for s in servers:
            sid = s.get('id')
            if s.get('key_id'):
                idx[f"key:{s['key_id']}"] = sid
            if s.get('host'):
                idx.setdefault(f"host:{s['host']}", sid)
                if s.get('user'):
                    idx.setdefault(f"host:{s['user']}@{s['host']}", sid)
        return idx

    @staticmethod
    def _resolve_server_id(idx: dict, key_id, host_part):
        """Prefer the target's key (unique per server); fall back to host only when
        the target has no key recorded."""
        if key_id and f"key:{key_id}" in idx:
            return idx[f"key:{key_id}"]
        if host_part:
            return idx.get(f"host:{host_part}") or idx.get(f"host:{host_part.split('@')[-1]}")
        return None

    def upsert_target_from_config(self, parsed: dict, servers: List[dict], enabled: bool = True):
        """Upsert a single parsed target (a config_manager.parse_targets() dict)
        into the targets table, resolving server_id (by key, then host) and dest_path
        from its logical target. Used by create/update so a newly paired target
        appears to the scheduler immediately (the migration uses the batch path)."""
        host_part, dest_path = self._split_logical_target(parsed.get("target") or "")
        server_id = self._resolve_server_id(self._server_index(servers), parsed.get("key_id"), host_part)
        self.upsert_target(id=parsed["id"], name=parsed.get("name") or parsed["id"],
                           source_path=parsed.get("source") or "",
                           server_id=server_id, dest_path=dest_path, enabled=enabled)

    @staticmethod
    def _split_logical_target(logical_target: str):
        """Splits a logical target into (host_part, dest_path).

        Targets in this app are only ever a local path or an rsync-over-ssh
        'user@host:/path'. Returns (host_part, dest_path) for the remote form
        (host_part is 'user@host' or 'host'), or (None, path) for a local path.
        An empty/missing destination normalizes to None (not "") so the NULL
        convention is consistent across paired and unpaired rows.
        """
        lt = logical_target or ""
        if ('@' in lt or ':/' in lt) and ':' in lt:
            host_part, dest_path = lt.split(':', 1)
            return host_part, (dest_path or None)
        return None, (lt or None)

    def upsert_target(self, *, id: str, name: str, source_path: str, server_id: Optional[str] = None,
                      dest_path: Optional[str] = None, interval_seconds: int = 0, enabled: bool = True):
        """Insert or update a replication target, keyed by id (which mirrors the
        target_id used in file_hashes). Leaves last_run/last_status/last_summary
        untouched on update — those are owned by record_target_run()."""
        with self.get_connection() as conn:
            conn.execute(self._TARGET_UPSERT_SQL,
                         (id, name, source_path, server_id, dest_path,
                          int(interval_seconds), 1 if enabled else 0))
            conn.commit()

    def get_target(self, target_id: str):
        """Returns one target row as a dict, or None."""
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM targets WHERE id = ?", (target_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_targets(self) -> List[dict]:
        """Returns all targets as dicts, ordered by name."""
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM targets ORDER BY name")
            return [dict(r) for r in cursor.fetchall()]

    def set_target_schedule(self, target_id: str, interval_seconds: Optional[int] = None,
                            enabled: Optional[bool] = None):
        """Updates only the scheduling fields, leaving the rest of the row intact."""
        sets, params = [], []
        if interval_seconds is not None:
            sets.append("interval_seconds = ?"); params.append(int(interval_seconds))
        if enabled is not None:
            sets.append("enabled = ?"); params.append(1 if enabled else 0)
        if not sets:
            return
        params.append(target_id)
        with self.get_connection() as conn:
            conn.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()

    def record_target_run(self, target_id: str, status: str, summary: Optional[str] = None):
        """Stamps the outcome of a sync run. Used by the sync engine in later phases."""
        with self.get_connection() as conn:
            conn.execute('''
                UPDATE targets SET last_run = CURRENT_TIMESTAMP,
                    last_status = ?, last_summary = ? WHERE id = ?
            ''', (status, summary, target_id))
            conn.commit()

    def delete_target_record(self, target_id: str):
        """Removes a target row (does not touch its file_hashes)."""
        with self.get_connection() as conn:
            conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
            conn.commit()

    def update_target_server(self, target_id: str, server_id: Optional[str], dest_path: Optional[str]):
        """Update ONLY a target's server_id + dest_path, leaving schedule / enabled /
        last-run intact. Used to correct a server mapping without a re-migration."""
        with self.get_connection() as conn:
            conn.execute("UPDATE targets SET server_id = ?, dest_path = ? WHERE id = ?",
                         (server_id, dest_path, target_id))
            conn.commit()

    def reresolve_target_servers(self, paired_targets: List[dict], servers: List[dict]) -> int:
        """One-time repair: re-resolve each existing paired target's server_id (BY KEY)
        from the current Lua config + servers, correcting rows that an earlier
        host-based migration mapped to the wrong server when several targets share a
        host. Preserves schedule/last-run. Guarded by 'target_servers_reresolved'.
        Returns the number of rows corrected."""
        if self.get_setting("target_servers_reresolved") == "1":
            return 0
        idx = self._server_index(servers)
        fixed = 0
        for t in paired_targets:
            existing = self.get_target(t["id"])
            if not existing:
                continue
            host_part, dest_path = self._split_logical_target(t.get("target") or "")
            correct = self._resolve_server_id(idx, t.get("key_id"), host_part)
            if correct and correct != existing.get("server_id"):
                self.update_target_server(t["id"], correct, dest_path)
                fixed += 1
        self.set_setting("target_servers_reresolved", "1")
        return fixed

    def migrate_targets_from_config(self, paired_targets: List[dict], unpaired_sources: List[dict],
                                    servers: List[dict]) -> int:
        """One-shot: populate the `targets` table from the parsed Lua targets and the
        unpaired_sources list. Idempotent — guarded by the 'targets_migrated' setting
        (and upsert-on-id), so the bulk pass only runs once. Returns rows written.

        paired_targets:   dicts from config_manager.parse_targets() (id/name/source/target).
        unpaired_sources: dicts {id, name, path} from the unpaired_sources setting.
        servers:          dicts {id, host, user, ...} from servers.json.
        """
        if self.get_setting("targets_migrated") == "1":
            return 0

        # Index servers (by key, then host) so each target resolves in O(1).
        idx = self._server_index(servers)

        # Build every row first, then write them in ONE transaction. The DB lives
        # on the FUSE /config volume where each commit's fsync is expensive, so a
        # per-row upsert (N commits) is avoided in favor of a single executemany.
        rows = []
        for t in paired_targets:
            host_part, dest_path = self._split_logical_target(t.get("target") or "")
            server_id = self._resolve_server_id(idx, t.get("key_id"), host_part)
            rows.append((t["id"], t.get("name") or t["id"], t.get("source") or "",
                         server_id, dest_path, 0, 1))
        for us in unpaired_sources:
            # Unpaired = a registered source with no destination yet -> disabled.
            rows.append((us["id"], us.get("name") or us["id"], us.get("path") or "",
                         None, None, 0, 0))

        # Write the rows AND set the guard flag in ONE transaction. If they were
        # separate commits, a crash between them would leave the flag unset and
        # re-run the migration on next boot — which, once targets become editable
        # in later phases, would overwrite user edits back to the config state.
        with self.get_connection() as conn:
            if rows:
                conn.executemany(self._TARGET_UPSERT_SQL, rows)
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('targets_migrated', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.commit()
        return len(rows)

    def remove_file_hash(self, target_id: str, filepath: str):
        """Removes a file from the hash tracking table (e.g. when deleted from disk)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM file_hashes WHERE target_id = ? AND filepath = ?', (target_id, filepath))
            conn.commit()

    def tombstone_file_hash(self, target_id: str, filepath: str):
        """
        Instead of fundamentally forgetting a file existed, we mark it as locally deleted/modified.
        This forces the API diffing engine to explicitly check the "modified_locally" flag and 
        block the Source from forcefully restoring a file the Client manually renamed or deleted.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO file_hashes (target_id, filepath, size, mtime, file_hash, modified_locally, last_updated)
                VALUES (?, ?, 0, 0, '', TRUE, CURRENT_TIMESTAMP)
                ON CONFLICT(target_id, filepath) DO UPDATE SET
                    size=0,
                    mtime=0,
                    file_hash='',
                    modified_locally=TRUE,
                    last_updated=CURRENT_TIMESTAMP
            ''', (target_id, filepath))
            conn.commit()

    def clear_modified_locally(self, target_id: str, filepath: str):
        """Clears the modified_locally flag when a local file perfectly matches the Source hash."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE file_hashes 
                SET modified_locally = FALSE 
                WHERE target_id = ? AND filepath = ?
            ''', (target_id, filepath))
            conn.commit()

    def get_all_hashes(self, target_id: str) -> List[dict]:
        """Returns the complete hash table for a specific target to be sent across the network."""
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT filepath, size, mtime, file_hash, modified_locally
                FROM file_hashes
                WHERE target_id = ?
            ''', (target_id,))
            return [dict(row) for row in cursor.fetchall()]

    def clear_missing_files(self, target_id: str):
        """Purges all tracked missing files for a specific target."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM missing_files WHERE target_id = ?", (target_id,))
            conn.commit()

    def add_missing_file(self, target_id: str, filepath: str):
        """Adds a single missing filepath to the database."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO missing_files (target_id, filepath)
                VALUES (?, ?)
            ''', (target_id, filepath))
            conn.commit()

    def remove_missing_file(self, target_id: str, filepath: str):
        """Removes a single missing filepath from the database, e.g. when synced."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM missing_files WHERE target_id = ? AND filepath = ?
            ''', (target_id, filepath))
            conn.commit()

    def get_missing_files(self, target_id: str) -> List[str]:
        """Returns a list of all filepaths tagged as missing for this target."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT filepath FROM missing_files WHERE target_id = ?", (target_id,))
            rows = cursor.fetchall()
            return [row[0] for row in rows]

    def get_missing_count(self, target_id: str) -> int:
        """Returns the scalar count of missing files for the UI."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM missing_files WHERE target_id = ?", (target_id,))
            return cursor.fetchone()[0]

    # --- Conflicts --- #

    def clear_conflicts(self, target_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM conflicts WHERE target_id = ?", (target_id,))
            conn.commit()

    def add_conflict(self, target_id: str, filepath: str, reason: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO conflicts (target_id, filepath, reason)
                VALUES (?, ?, ?)
            ''', (target_id, filepath, reason))
            conn.commit()

    def get_conflict_count(self, target_id: str) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM conflicts WHERE target_id = ?", (target_id,))
            return cursor.fetchone()[0]

    def get_conflicts(self, target_id: str) -> List[dict]:
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT filepath, reason, detected_at FROM conflicts WHERE target_id = ?", (target_id,))
            return [dict(row) for row in cursor.fetchall()]

    def log_transfer(self, target_id: str, filepath: str, action: str):
        """Passively records a successful file transfer."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO transfer_history (target_id, filepath, action)
                VALUES (?, ?, ?)
            ''', (target_id, filepath, action))
            conn.commit()

    def record_transfers(self, target_id: str, filepaths: List[str], action: str = "sync"):
        """Batch-records successfully transferred files in ONE transaction (a
        commit per file would crawl on the FUSE /config volume). Feeds the
        per-day transfer counter shown in the UI."""
        if not filepaths:
            return
        with self.get_connection() as conn:
            conn.executemany(
                "INSERT INTO transfer_history (target_id, filepath, action) VALUES (?, ?, ?)",
                [(target_id, p, action) for p in filepaths])
            # Keep the table bounded — rows older than 30 days serve neither the
            # daily counter nor any recent-history view.
            conn.execute("DELETE FROM transfer_history WHERE timestamp < datetime('now', '-30 days')")
            conn.commit()

    def transfers_since(self, since_utc: str) -> dict:
        """Per-target count of files transferred at/after `since_utc` — a UTC
        'YYYY-MM-DD HH:MM:SS' string, directly comparable to the table's
        CURRENT_TIMESTAMP values. Returns {target_id: count}."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT target_id, COUNT(*) FROM transfer_history "
                "WHERE timestamp >= ? GROUP BY target_id", (since_utc,))
            return {row[0]: row[1] for row in cursor.fetchall()}

db = DatabaseManager()
