"""Shared pytest fixtures + import bootstrap for the MonarchAegis test suite.

Tests run IN-CONTAINER (local Windows Python lacks the runtime deps — xxhash,
fastapi, asyncssh — and memray is Linux-only). Easiest: `./run_tests.sh`.
Manually: build with `docker build -f Dockerfile.test -t monarchaegis-test .` then
`docker run --rm -v "$PWD/app":/app -v "$PWD/tests":/tests -w /app monarchaegis-test pytest /tests`
(cwd must be /app so the app's relative static/ mount resolves).

This conftest makes `app/` importable, sets baseline env for the singleton
modules (database.db / main), and provides isolated per-test fixtures for the
DB, config manager, and server manager.
"""
import json
import os
import sys
import tempfile
import types
from types import SimpleNamespace

# --- Make app/ importable (parent-of-tests / app) ---
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP)

# --- Cross-platform stubs (harmless in-container; needed if ever run on Windows) ---
if "fcntl" not in sys.modules and os.name == "nt":
    _fake = types.ModuleType("fcntl")
    _fake.LOCK_EX, _fake.LOCK_UN, _fake.flock = 2, 8, (lambda *a, **k: None)
    sys.modules["fcntl"] = _fake
if not hasattr(os, "chown"):
    os.chown = lambda *a, **k: None

# --- Baseline env for the singleton modules, BEFORE any app import ---
_SESSION_TMP = tempfile.mkdtemp(prefix="monarchaegis_pytest_session_")
os.environ.setdefault("MONARCHAEGIS_DB_PATH", os.path.join(_SESSION_TMP, "monarchaegis.db"))
os.environ.setdefault("MONARCHAEGIS_CONFIG_PATH", os.path.join(_SESSION_TMP, "monarchaegis.conf.lua"))
os.environ.setdefault("MONARCHAEGIS_SERVERS_JSON", os.path.join(_SESSION_TMP, "servers.json"))
os.environ.setdefault("MONARCHAEGIS_KEYS_DIR", os.path.join(_SESSION_TMP, "keys"))
os.environ.setdefault("MONARCHAEGIS_ROLE", "client")
os.environ.setdefault("MONARCHAEGIS_SAFE_MODE", "1")
os.makedirs(os.environ["MONARCHAEGIS_KEYS_DIR"], exist_ok=True)

import pytest


@pytest.fixture
def db(tmp_path):
    """A fresh, isolated DatabaseManager backed by a per-test SQLite file."""
    import database
    return database.DatabaseManager(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """A fresh ConfigManager plus a servers.json + key so paired-target
    generation resolves an SSH key. Returns a namespace with .mgr and .keys_dir."""
    import config_manager
    keys_dir = tmp_path / "keys"; keys_dir.mkdir()
    servers_json = tmp_path / "servers.json"
    config_path = tmp_path / "monarchaegis.conf.lua"
    (keys_dir / "key_abc123").write_text("FAKE KEY\n")
    servers_json.write_text(json.dumps([{
        "id": "srv_1", "alias": "client", "host": "192.168.1.50",
        "user": "root", "port": 2222, "key_id": "key_abc123",
    }]))
    monkeypatch.setenv("MONARCHAEGIS_SERVERS_JSON", str(servers_json))
    monkeypatch.setenv("MONARCHAEGIS_KEYS_DIR", str(keys_dir))
    monkeypatch.setattr(config_manager, "CONFIG_PATH", str(config_path), raising=False)
    mgr = config_manager.ConfigManager(str(config_path))
    return SimpleNamespace(mgr=mgr, keys_dir=str(keys_dir).replace("\\", "/"))


@pytest.fixture
def server_env(tmp_path, monkeypatch):
    """A fresh ServerManager pointed at temp key/auth paths, with an rrsync stub.
    Returns a namespace with .mgr, .base, .auth_keys, .live_auth, .rrsync."""
    import server_manager
    base = str(tmp_path).replace("\\", "/")
    rrsync = os.path.join(str(tmp_path), "rrsync")
    with open(rrsync, "w") as f:
        f.write("#!/usr/local/bin/python3\n# stub\n")
    auth = f"{base}/authorized_keys"
    live = f"{base}/live/authorized_keys"
    monkeypatch.setattr(server_manager, "KEYS_DIR", f"{base}/keys")
    monkeypatch.setattr(server_manager, "SERVERS_JSON", f"{base}/servers.json")
    monkeypatch.setattr(server_manager, "AUTH_KEYS_PATH", auth)
    monkeypatch.setattr(server_manager, "LIVE_AUTH_KEYS_PATH", live)
    monkeypatch.setattr(server_manager, "RRSYNC_PATH", rrsync.replace("\\", "/"))
    return SimpleNamespace(mgr=server_manager.ServerManager(), base=base,
                           auth_keys=auth, live_auth=live, rrsync=rrsync.replace("\\", "/"))
