# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

import os
from envcompat import env
import json
import uuid
import stat
import shutil
import subprocess
from typing import List, Dict

# Paths
KEYS_DIR = env("MONARCHAEGIS_KEYS_DIR", "/config/keys")
SERVERS_JSON = env("MONARCHAEGIS_SERVERS_JSON", "/config/servers.json")
# Canonical authorized_keys lives in /config so it survives container
# restarts/rebuilds. sshd cannot read it from there directly: /config is a
# host-owned volume and sshd StrictModes rejects key files whose parent
# directories are not root-owned. So every mutation is mirrored to the live
# path in /root/.ssh (root-owned, 600), which is what sshd actually reads.
# The entrypoint performs the same restore at boot.
AUTH_KEYS_PATH = env("MONARCHAEGIS_AUTH_KEYS", "/config/authorized_keys")
LIVE_AUTH_KEYS_PATH = env("MONARCHAEGIS_LIVE_AUTH_KEYS", "/root/.ssh/authorized_keys")
# rrsync (restricted rsync) enforces the directory jail. Installed by the Dockerfile.
RRSYNC_PATH = env("MONARCHAEGIS_RRSYNC_PATH", "/usr/local/bin/rrsync")
# The paired key's forced command is a small dispatcher (app/monarchaegis_recv.sh,
# installed here by the Dockerfile). It execs rrsync for a normal rsync push, or
# the jailed tar receiver for a tar-stream batch — so ONE key serves both
# transports. Existing keys paired before this shipped point straight at rrsync
# and keep working for rsync; re-pair to gain the tar-stream capability.
RECV_PATH = env("MONARCHAEGIS_RECV_PATH", "/usr/local/bin/monarchaegis-recv")

# Fallback for local testing outside of docker
if "MONARCHAEGIS_KEYS_DIR" not in os.environ and not os.path.exists(os.path.dirname(KEYS_DIR)):
    local_base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "mock_data")
    KEYS_DIR = os.path.join(local_base, "keys")
    SERVERS_JSON = os.path.join(local_base, "servers.json")

class ServerManager:
    """Manages remote server profiles and their associated SSH private keys."""

    def __init__(self):
        self._ensure_directories()
        self._ensure_servers_file()

    def _ensure_directories(self):
        os.makedirs(KEYS_DIR, exist_ok=True)

    def _ensure_servers_file(self):
        if not os.path.exists(SERVERS_JSON):
            with open(SERVERS_JSON, "w") as f:
                json.dump([], f)

    # --- Client API Pairing --- #
    def _sync_live_authorized_keys(self):
        """Mirrors the persistent /config authorized_keys to the live root-owned
        path that sshd actually reads (StrictModes-safe)."""
        live_dir = os.path.dirname(LIVE_AUTH_KEYS_PATH)
        if live_dir:
            os.makedirs(live_dir, exist_ok=True)
            os.chmod(live_dir, stat.S_IRWXU)
        shutil.copyfile(AUTH_KEYS_PATH, LIVE_AUTH_KEYS_PATH)
        os.chmod(LIVE_AUTH_KEYS_PATH, stat.S_IRUSR | stat.S_IWUSR)

    def generate_client_keypair(self, target_id: str, target_path: str) -> str:
        """
        Generates an Ed25519 SSH keypair specifically for an incoming Source Target.
        The key is SCOPED to only allow rsync operations inside `target_path`.

        The authorized_keys entry forces the dispatcher (RECV_PATH) plus the
        `restrict` directive: no shell, no port/agent/X11 forwarding, no PTY.
        The dispatcher routes a normal rsync push to `rrsync -wo <jail>`
        (write-only, jailed — a Source can push into the directory but never read
        it; rrsync safely parses SSH_ORIGINAL_COMMAND, rejecting `..` and
        disallowed flags) and a tar-stream batch to the jailed tar receiver.

        NOTE: rrsync resolves the requested path RELATIVE to the jail root, so
        the Source must target `host:/` — config_manager generates exactly that.

        Returns the raw private key string to be handed back to the Source.
        """
        if not os.path.exists(RRSYNC_PATH):
            raise RuntimeError(
                f"rrsync not found at {RRSYNC_PATH} — cannot create a directory-jailed key. "
                "Rebuild the container image (rrsync is installed by the Dockerfile)."
            )

        # Resolve and validate the jail path. Quotes/newlines would corrupt the
        # authorized_keys entry, so reject them outright instead of silently
        # stripping (stripping would jail a different path than the one created).
        real_path = os.path.realpath(target_path)
        if any(c in real_path for c in ('"', "'", '\n', '\r', '\\')):
            raise ValueError(f"target_path contains unsupported characters: {target_path}")

        tmp_dir = "/root/.ssh"
        os.makedirs(tmp_dir, exist_ok=True)
        os.chmod(tmp_dir, stat.S_IRWXU)

        # Ensure the target receiving directory exists
        os.makedirs(real_path, exist_ok=True)

        # Temp path for key generation (cleaned up immediately after)
        key_path = os.path.join(tmp_dir, f"id_ed25519_client_{target_id}")

        # If a key somehow already exists for this exact target_id, remove it first
        if os.path.exists(key_path):
            os.remove(key_path)
            if os.path.exists(f"{key_path}.pub"):
                os.remove(f"{key_path}.pub")

        key_comment = f"monarchaegis_client_{target_id}"

        # Generate the Ed25519 keypair without a passphrase (-N "")
        subprocess.run([
            "ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-q",
            "-C", key_comment
        ], check=True)

        # Read the public key
        with open(f"{key_path}.pub", "r") as pub_file:
            pub_key = pub_file.read().strip()

        # Directory-jailed entry. The forced command is the dispatcher, which
        # receives the jail path as its single argument and internally execs
        # `rrsync -wo <jail>` (rsync) or the jailed tar receiver (tar-stream).
        # The \" escapes survive sshd's option parsing so the shell receives a
        # properly quoted jail path (spaces are safe).
        jail_entry = f'command="{RECV_PATH} \\"{real_path}\\"",restrict {pub_key}'

        # Rewrite the persistent authorized_keys: drop any stale entries for this
        # same target (re-pairing previously appended forever), then add the new one.
        os.makedirs(os.path.dirname(AUTH_KEYS_PATH) or ".", exist_ok=True)
        existing_lines = []
        if os.path.exists(AUTH_KEYS_PATH):
            with open(AUTH_KEYS_PATH, "r") as auth_file:
                existing_lines = [
                    line for line in auth_file.read().splitlines()
                    if line.strip() and key_comment not in line
                ]
        with open(AUTH_KEYS_PATH, "w") as auth_file:
            auth_file.write("\n".join(existing_lines + [jail_entry]) + "\n")
        os.chmod(AUTH_KEYS_PATH, stat.S_IRUSR | stat.S_IWUSR)

        # Mirror to the live path sshd reads.
        self._sync_live_authorized_keys()

        # Read the private key to return it to the Source
        with open(key_path, "r") as priv_file:
            private_key = priv_file.read()

        # Cleanup the temporary files as they are no longer needed locally on disk
        # aside from being embedded in authorized_keys
        os.remove(key_path)
        if os.path.exists(f"{key_path}.pub"):
            os.remove(f"{key_path}.pub")

        return private_key

    def upgrade_authorized_keys_to_dispatcher(self) -> int:
        """One-time, in-place upgrade of existing forced-command entries from the
        bare rrsync jail to the dispatcher (RECV_PATH), preserving each entry's
        public key AND jail path untouched.

        Keys paired before the tar-stream transport shipped run
        `command="<rrsync> -wo \\"<jail>\\""`; the tar sentinel hits rrsync and is
        rejected. The dispatcher execs `rrsync -wo <jail>` verbatim for rsync
        pushes (a strict superset), so rewriting the forced-command binary lets
        the tar transport work WITHOUT re-pairing, and rsync keeps working
        exactly as before. Returns the number of entries upgraded.

        Idempotent: only lines still carrying the `<rrsync> -wo ` forced command
        are rewritten, so this is safe to run every boot; the caller additionally
        guards it with a settings flag so it normally runs just once.
        """
        if not os.path.exists(AUTH_KEYS_PATH):
            return 0
        needle = f"{RRSYNC_PATH} -wo "        # exactly what generate_client_keypair wrote
        replacement = f"{RECV_PATH} "
        with open(AUTH_KEYS_PATH, "r") as f:
            lines = f.read().splitlines()
        upgraded = 0
        new_lines = []
        for line in lines:
            if line.strip().startswith("command=") and needle in line:
                new_lines.append(line.replace(needle, replacement))
                upgraded += 1
            else:
                new_lines.append(line)
        if upgraded:
            with open(AUTH_KEYS_PATH, "w") as f:
                f.write("\n".join(new_lines) + "\n")
            os.chmod(AUTH_KEYS_PATH, stat.S_IRUSR | stat.S_IWUSR)
            self._sync_live_authorized_keys()   # mirror to the path sshd reads
        return upgraded

    # --- Key Management --- #

    def save_key(self, name: str, private_key: str) -> str:
        """Saves a private key to disk with strict 600 permissions."""
        key_id = f"key_{uuid.uuid4().hex[:8]}"
        filepath = os.path.join(KEYS_DIR, key_id)
        
        # Write the key securely
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = stat.S_IRUSR | stat.S_IWUSR  # 600 permissions
        
        with os.fdopen(os.open(filepath, flags, mode), "w") as f:
            f.write(private_key.strip('\r\n') + '\n')
            
        # Store metadata in a related .meta file
        meta_path = f"{filepath}.meta"
        with open(meta_path, "w") as f:
            json.dump({"id": key_id, "name": name}, f)
            
        return key_id

    def list_keys(self) -> List[Dict]:
        """Returns a list of saved keys and their names."""
        keys = []
        if os.path.exists(KEYS_DIR):
            for file in os.listdir(KEYS_DIR):
                if file.endswith(".meta"):
                    try:
                        with open(os.path.join(KEYS_DIR, file), "r") as f:
                            keys.append(json.load(f))
                    except (json.JSONDecodeError, OSError):
                        pass
        return keys

    def get_key_path(self, key_id: str) -> str:
        """Returns the absolute file path for a private key."""
        return os.path.join(KEYS_DIR, key_id)

    def delete_key(self, key_id: str) -> bool:
        """Deletes a key and its metadata, preventing deletion if in use."""
        # Check if any server is using this key
        servers = self._read_servers()
        for server in servers:
            if server.get("key_id") == key_id:
                raise ValueError(f"Cannot delete key. It is currently used by server: {server.get('alias')}")
                
        filepath = os.path.join(KEYS_DIR, key_id)
        meta_path = f"{filepath}.meta"
        
        deleted = False
        if os.path.exists(filepath):
            os.remove(filepath)
            deleted = True
        if os.path.exists(meta_path):
            os.remove(meta_path)
            
        return deleted

    # --- Server Management --- #

    def _read_servers(self) -> List[Dict]:
        with open(SERVERS_JSON, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []

    def _write_servers(self, servers: List[Dict]):
        with open(SERVERS_JSON, "w") as f:
            json.dump(servers, f, indent=4)

    def add_server(self, alias: str, host: str, user: str, port: int, key_id: str) -> str:
        """Add a remote server profile, or update the one for the same destination
        AND key. Identity is (host, user, key_id) — NOT host+user alone.

        A directory-jailed setup that replicates several shares to a single
        destination host uses a DISTINCT key per share (each key = one rrsync/tar
        jail). Deduping by host+user only would collapse those onto whichever key
        was paired last, so every target would push into one jail — the exact
        cross-wiring this avoids. Same host+user+key means the same link being
        re-linked, so we update its alias/port; a new key is a new destination and
        gets its own profile.
        """
        servers = self._read_servers()

        # Same destination AND key -> update the existing profile's metadata.
        for s in servers:
            if (s.get("host") == host and s.get("user") == user
                    and s.get("key_id") == key_id):
                s["alias"] = alias
                s["port"] = port
                self._write_servers(servers)
                return s["id"]

        # New key (or new host) -> its own profile.
        server_id = f"srv_{uuid.uuid4().hex[:8]}"
        servers.append({
            "id": server_id,
            "alias": alias,
            "host": host,
            "user": user,
            "port": port,
            "key_id": key_id,
        })
        self._write_servers(servers)
        return server_id

    def remove_server_and_key(self, server_id: str) -> bool:
        """Delete one server profile and its private key, unless another surviving
        profile still uses that key. Cleans up the orphan a deleted target leaves
        behind now that profiles are per-key. Returns True if a profile was removed.
        """
        servers = self._read_servers()
        target = next((s for s in servers if s.get("id") == server_id), None)
        if not target:
            return False
        remaining = [s for s in servers if s.get("id") != server_id]
        self._write_servers(remaining)
        key_id = target.get("key_id")
        if key_id and all(s.get("key_id") != key_id for s in remaining):
            try:
                self.delete_key(key_id)   # its own guard re-checks nothing uses it
            except (ValueError, OSError):
                pass
        return True

    def list_servers(self) -> List[Dict]:
        """Returns all saved server profiles."""
        return self._read_servers()

    def get_server(self, server_id: str) -> Dict:
        """Returns a specific server profile."""
        servers = self._read_servers()
        for s in servers:
            if s["id"] == server_id:
                return s
        return None

    def delete_server(self, server_id: str):
        """Deletes a server profile."""
        servers = self._read_servers()
        servers = [s for s in servers if s["id"] != server_id]
        self._write_servers(servers)

    def factory_reset(self) -> dict:
        """Wipes all SSH auth state: private keys, authorized_keys (both the
        persistent /config copy and the live /root/.ssh mirror), and server
        profiles. Used to clear stale/broken pairings so a clean re-pair on the
        current image starts from zero. Best-effort per item; returns a summary.
        """
        removed = {"keys": 0, "authorized_keys": False, "servers": False}

        # 1. Private keys + their .meta sidecars
        if os.path.isdir(KEYS_DIR):
            for fname in os.listdir(KEYS_DIR):
                try:
                    os.remove(os.path.join(KEYS_DIR, fname))
                    if not fname.endswith(".meta"):
                        removed["keys"] += 1
                except OSError:
                    pass

        # 2. authorized_keys — empty both the persistent copy and the live mirror
        for path in (AUTH_KEYS_PATH, LIVE_AUTH_KEYS_PATH):
            try:
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(path, "w"):
                    pass
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                removed["authorized_keys"] = True
            except OSError:
                pass

        # 3. Server profiles
        try:
            with open(SERVERS_JSON, "w") as f:
                json.dump([], f)
            removed["servers"] = True
        except OSError:
            pass

        return removed

