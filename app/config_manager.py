# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

import os
from envcompat import env
import re
import stat
import shutil
import tempfile
import fcntl
from contextlib import contextmanager
from typing import List, Dict, Optional
from datetime import datetime

from constants import RSYNC_NO_SECLUDED_ARGS

CONFIG_PATH = env("MONARCHAEGIS_CONFIG_PATH", "/config/monarchaegis.conf.lua")
KNOWN_HOSTS_PATH = env("MONARCHAEGIS_KNOWN_HOSTS", "/config/known_hosts")

# Fallback for local testing outside of docker if no env var is provided.
# Gated on /config actually being absent — inside the container the volume
# always exists, so an unset env var must NOT redirect writes to the mock
# path (lsyncd would keep running the stale real config forever).
if "MONARCHAEGIS_CONFIG_PATH" not in os.environ and not os.path.exists(os.path.dirname(CONFIG_PATH)):
    local_mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "mock_data", "config", "monarchaegis.conf.lua")
    CONFIG_PATH = local_mock_path
    
RSYNC_LOG_PATH = os.environ.get("RSYNC_LOG_PATH", "/logs/rsync.log")

class ConfigManager:
    """Manages reading, parsing, backing up, and rewriting the monarchaegis.conf.lua file."""
    
    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path

    def _atomic_write(self, content: str):
        """Writes content to the config file atomically via temp file + os.replace().

        Preserves the original file's mode and ownership so lsyncd retains
        access after every config mutation. fsync ensures the data is durable
        before the rename so a crash between write and replace doesn't leave a
        corrupt or zero-length config behind.

        Commit semantics: once os.replace() returns the config is live regardless
        of what happens next. The parent-directory fsync is best-effort durability
        hardening — its failure does NOT mean the write failed, and callers must
        not retry the mutation if this method raises after the rename.
        """
        config_dir = os.path.dirname(self.config_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
        committed = False
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            # Carry over mode and ownership from the existing config so lsyncd
            # (which may run as a different uid) keeps read access after the swap.
            # On first-create apply 0o644 — mkstemp's default 0600 would lock out
            # any uid other than the API process owner.
            if os.path.exists(self.config_path):
                src_stat = os.stat(self.config_path)
                os.chmod(tmp_path, stat.S_IMODE(src_stat.st_mode))
                try:
                    os.chown(tmp_path, src_stat.st_uid, src_stat.st_gid)
                except PermissionError:
                    pass  # Best-effort — non-root environments may not allow chown
            else:
                os.chmod(tmp_path, 0o644)

            os.replace(tmp_path, self.config_path)
            committed = True  # Config is live from this point forward.

            # Best-effort: flush the parent directory entry to disk.
            # Failure here does NOT mean the write failed — the config is already
            # in place. Log the warning and let callers proceed normally.
            try:
                parent_fd = os.open(config_dir, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError as e:
                print(f"WARNING: _atomic_write: parent dir fsync failed (config already committed): {e}")

        except Exception:
            if not committed:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    @contextmanager
    def _config_lock(self):
        """Exclusive interprocess lock around config read-modify-write cycles.

        Uses fcntl.flock on a sidecar .lock file so concurrent HTTP workers
        and separate processes cannot interleave reads and writes, preventing
        lost-update races on the Lua config file.
        """
        config_dir = os.path.dirname(self.config_path) or "."
        os.makedirs(config_dir, exist_ok=True)
        lock_path = self.config_path + ".lock"
        with open(lock_path, 'a') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _ensure_config_exists(self):
        """Creates a default blank configuration if none exists."""
        if not os.path.exists(self.config_path):
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            log_path = env("MONARCHAEGIS_LOG_PATH", "/logs/monarchaegis.log")
            status_path = env("MONARCHAEGIS_STATUS_PATH", "/logs/monarchaegis.status")
            default_content = (
                "-- Auto-generated by MonarchAegis Controller\n"
                f'settings {{\n    logfile = "{log_path}",\n    statusFile = "{status_path}",\n    statusInterval = 10\n}}\n\n'
            )
            self._atomic_write(default_content)

    def create_backup(self) -> str:
        """Creates a timestamped .bak copy of the config file."""
        self._ensure_config_exists()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{self.config_path}.{timestamp}.bak"
        shutil.copy2(self.config_path, backup_path)
        return backup_path

    def read_raw_config(self) -> str:
        """Reads the raw textual content of the config file."""
        self._ensure_config_exists()
        with open(self.config_path, "r") as f:
            return f.read()

    def parse_targets(self) -> List[Dict]:
        """
        Parses the Lua config file to extract individual `sync {}` blocks.
        """
        raw_content = self.read_raw_config()
        targets = []

        # Regex to match: optional comment name, optional destpath comment, then sync { ... }
        # Uses a non-greedy .*? to match until we hit a closing brace on a new line `\n}`
        # which indicates the end of a sync block in our generated/expected format.
        # `-- destpath:` records the real receiving directory for rrsync-jailed
        # targets whose physical rsync target is just "host:/".
        sync_pattern = re.compile(
            r'(?:--\s*name:\s*(.+?)\n)?(?:--\s*destpath:\s*(.+?)\n)?\s*sync\s*\{([\s\S]*?)\n\}',
            re.MULTILINE
        )

        matches = sync_pattern.finditer(raw_content)
        for i, match in enumerate(matches):
            name_comment = match.group(1)
            destpath_comment = match.group(2)
            block_content = match.group(3)

            target = {
                "id": f"target_{i}",
                "raw_block": match.group(0),
                "source": None,
                "target": None,
                "name": f"Sync Target {i+1}",
                "status": "Unknown"
            }

            # Extract basic properties using simple regex
            source_match = re.search(r'source\s*=\s*["\']([^"\']+)["\']', block_content)
            if source_match:
                target["source"] = source_match.group(1)

            host_match = re.search(r'host\s*=\s*["\']([^"\']+)["\']', block_content)
            target_match = re.search(r'(?:target|targetdir)\s*=\s*["\']([^"\']+)["\']', block_content)

            # The SSH key this target uses, pulled from the rsh line
            # (`rsh = "ssh -i /config/keys/<key_id> -p ..."`). This is what
            # uniquely identifies which server profile the target belongs to —
            # host+user alone is ambiguous when several rrsync-jailed targets live
            # on the same destination box.
            key_match = re.search(r'-i\s+(\S+)', block_content)
            if key_match:
                target["key_id"] = os.path.basename(key_match.group(1).strip().strip('"'))

            if target_match:
                dest = target_match.group(1)
                if host_match:
                    dest = f"{host_match.group(1)}:{dest}"
                # The physical destination is what rsync/lsyncd actually connect to.
                # For rrsync-jailed targets that is "user@host:/" — the jail maps it
                # to the real directory on the client.
                target["physical_target"] = dest
                # The logical destination (real receiving path) is what the UI and
                # the P2P diff API care about.
                if destpath_comment and ':' in dest:
                    dest = f"{dest.split(':', 1)[0]}:{destpath_comment.strip()}"
                target["target"] = dest

            # Try to use the extracted name/alias
            if name_comment:
                target["name"] = name_comment.strip()
                target["id"] = "".join(x for x in target["name"] if x.isalnum() or x in "_-").lower()

            targets.append(target)

        return targets

    def _build_sync_block(self, target_data: Dict) -> str:
        """Builds the Lua sync block string for a target without touching the config file.

        Reads servers.json to resolve the SSH key and port. Raises if no key
        is found for a remote destination — caller must handle before acquiring
        the config lock so the lock is never held during I/O-heavy lookups.

        Remote targets use default.rsync with rsync.rsh (NOT default.rsyncssh):
        rsyncssh performs moves/deletes via plain `ssh mv` / `ssh rm`, which the
        client's rrsync jail rejects — with default.rsync every operation,
        including deletes, travels inside the rsync protocol that the jail allows.

        The physical rsync target for a paired client is "user@host:/" because
        rrsync resolves paths relative to its jail root. The real receiving
        directory is recorded in a `-- destpath:` comment so parse_targets can
        reconstruct the logical destination for the UI and the P2P diff API.
        """
        import json

        alias = target_data.get('name', 'Unnamed Target')
        source = target_data.get('source', '')
        target_dest = target_data.get('target', '')

        destpath_comment = ''
        rsh_line = ''

        if ('@' in target_dest or ':/' in target_dest) and ':' in target_dest:
            parts = target_dest.rsplit(':', 1)
            host_str = parts[0]
            dir_str = parts[1]

            key_path = None
            client_port = 22
            keys_dir = env("MONARCHAEGIS_KEYS_DIR", "/config/keys")
            servers_json = env("MONARCHAEGIS_SERVERS_JSON", "/config/servers.json")
            servers = []
            if os.path.exists(servers_json):
                try:
                    with open(servers_json, 'r') as sf:
                        servers = json.load(sf)
                except Exception as e:
                    print(f"WARNING: Failed to read {servers_json}: {e}")

            # Prefer an explicitly pinned key (the pairing flow passes the exact key
            # it just created). This is REQUIRED for correctness when several server
            # profiles share one host+user (multiple directory-jailed shares to one
            # destination): matching by host alone below picks the FIRST such server,
            # so every same-host target would be stamped with one key/jail. basename()
            # neutralizes path trickery; the realpath jail check below is the backstop.
            explicit_key_id = os.path.basename((target_data.get('key_id') or '').strip())
            if explicit_key_id:
                key_path = os.path.join(keys_dir, explicit_key_id).replace('\\', '/')
                # Port comes from the server that owns this key, else the first on the host.
                owner = next((s for s in servers if s.get('key_id') == explicit_key_id), None)
                host_srv = next((s for s in servers
                                 if host_str in (f"{s.get('user')}@{s.get('host')}", s.get('host'))), None)
                port_src = owner or host_srv
                if port_src and port_src.get('port'):
                    client_port = port_src.get('port')
                if not os.path.exists(key_path):
                    raise Exception(f"Pinned SSH key '{explicit_key_id}' not found in keys directory.")
            else:
                # No pinned key: resolve by host. Safe ONLY when a single server
                # serves the host. With several (multi-share to one destination),
                # host-match is ambiguous and silently taking the first cross-wires
                # every same-host target onto one key/jail — the whole class of bug
                # we chased. Refuse instead, so the caller pins key_id.
                host_matches = [s for s in servers
                                if s.get('key_id') and host_str in
                                (f"{s.get('user')}@{s.get('host')}", s.get('host'))]
                if len(host_matches) > 1:
                    raise Exception(
                        f"Ambiguous SSH key for host '{host_str}': {len(host_matches)} "
                        "server profiles share it. Re-pair this target so its exact key "
                        "is pinned (hard-refresh the browser first to load the current UI).")
                if host_matches:
                    s = host_matches[0]
                    if s.get('port'):
                        client_port = s.get('port')
                    key_path = os.path.join(keys_dir, s['key_id']).replace('\\', '/')

            if not key_path:
                print(f"WARNING: No SSH key found for host '{host_str}'. "
                      f"Rsync will fail with code 255 (SSH auth failure).")
                raise Exception(f"No SSH key found for remote host '{host_str}'. Please add a server profile or key first.")

            # Sanity-check values that get embedded in the rsh command string.
            keys_dir_real = os.path.realpath(keys_dir)
            key_real = os.path.realpath(key_path)
            if key_real != keys_dir_real and not key_real.startswith(keys_dir_real + os.sep):
                raise Exception(f"Key path escapes keys directory: {key_path}")
            try:
                client_port = int(client_port)
            except (TypeError, ValueError):
                raise Exception(f"Invalid SSH port for host '{host_str}': {client_port}")

            # rrsync jails the key to the receiving directory and resolves paths
            # relative to it, so the physical target is the jail root "/".
            target_string = f'    target = "{host_str}:/",'
            destpath_comment = f"\n-- destpath: {dir_str}"
            rsh_line = (
                f',\n        rsh = "ssh -i {key_path} -p {client_port} -o BatchMode=yes'
                f' -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={KNOWN_HOSTS_PATH}"'
            )
        else:
            target_string = f'    target = "{target_dest}",'

        # Assemble the rsync _extra flags in Python, then render as a Lua array —
        # keeps the data composition separate from the brace-heavy template.
        # --whole-file: destination copies are re-encoded (AV1), so rsync's delta
        # algorithm never matches and only wastes destination disk I/O reading
        # every file as a basis. Send whole files instead (rrsync jail permits W).
        extra_flags = [RSYNC_NO_SECLUDED_ARGS, "--whole-file", "--verbose",
                       "--itemize-changes", f"--log-file={RSYNC_LOG_PATH}"]
        lua_extra = ", ".join(f'"{flag}"' for flag in extra_flags)

        return f"""
-- name: {alias}{destpath_comment}
sync {{
    default.rsync,
    source = "{source}",
{target_string}
    delay = 15,
    init = false,
    rsync = {{
        _extra = {{{lua_extra}}}{rsh_line}
    }}
}}
"""

    def add_target(self, target_data: Dict) -> bool:
        """Safely appends a new sync target."""
        # Build the block before acquiring the lock — reads servers.json and may raise.
        # Backup is taken under the lock so it captures the exact preimage being replaced.
        sync_block = self._build_sync_block(target_data)
        with self._config_lock():
            self.create_backup()
            self._atomic_write(self.read_raw_config() + sync_block)
        return True

    def delete_target(self, target_id: str) -> bool:
        """Removes a sync block by ID."""
        with self._config_lock():
            targets = self.parse_targets()
            target_to_delete = next((t for t in targets if t["id"] == target_id), None)
            if not target_to_delete:
                return False
            self.create_backup()
            raw_content = self.read_raw_config()
            new_content = raw_content.replace(target_to_delete["raw_block"], "")
            new_content = re.sub(r'\n{3,}', '\n\n', new_content)
            self._atomic_write(new_content)
        return True

    def update_target(self, target_id: str, new_target_data: Dict) -> bool:
        """Replaces an existing sync block atomically under a single lock."""
        # Build the replacement block before acquiring the lock.
        sync_block = self._build_sync_block(new_target_data)
        with self._config_lock():
            targets = self.parse_targets()
            target_to_delete = next((t for t in targets if t["id"] == target_id), None)
            if not target_to_delete:
                return False
            self.create_backup()
            raw_content = self.read_raw_config()
            new_content = raw_content.replace(target_to_delete["raw_block"], "")
            new_content = re.sub(r'\n{3,}', '\n\n', new_content)
            self._atomic_write(new_content + sync_block)
        return True
