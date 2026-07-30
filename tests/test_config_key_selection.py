"""config_manager._build_sync_block SSH-key selection.

The regression this guards: several server profiles sharing one host+user (several
directory-jailed shares to a single destination). Resolving the key by host alone
picks the FIRST such server, so every same-host target gets one key/jail. The
pairing flow now pins the exact key_id; the writer must honor it.
"""
import json
import os

import pytest


def _three_same_host_servers(config_env):
    """Write three profiles on the SAME host, each with its own key, and create
    the key files. Returns nothing; mutates the fixture's servers.json/keys_dir."""
    for k in ("key_anime", "key_movies", "key_series"):
        with open(os.path.join(config_env.keys_dir, k), "w") as f:
            f.write("PRIVATE-KEY\n")
    servers = [
        {"id": "s_a", "alias": "Anime", "host": "192.168.1.10", "user": "root", "port": 2222, "key_id": "key_anime"},
        {"id": "s_m", "alias": "Movies", "host": "192.168.1.10", "user": "root", "port": 2200, "key_id": "key_movies"},
        {"id": "s_s", "alias": "Series", "host": "192.168.1.10", "user": "root", "port": 2222, "key_id": "key_series"},
    ]
    with open(os.environ["MONARCHAEGIS_SERVERS_JSON"], "w") as f:
        json.dump(servers, f)


def test_pinned_key_id_beats_host_match(config_env):
    _three_same_host_servers(config_env)
    block = config_env.mgr._build_sync_block({
        "name": "Movies", "source": "/src/",
        "target": "root@192.168.1.10:/", "key_id": "key_movies",
    })
    assert "key_movies" in block                       # the pinned key, not the first
    assert "key_anime" not in block and "key_series" not in block
    assert "-p 2200" in block                          # port from the pinned key's server


def test_ambiguous_host_without_pin_is_refused(config_env):
    # Several servers on one host + no pinned key -> REFUSE, rather than silently
    # grabbing the first (which was the whole cross-wiring bug).
    _three_same_host_servers(config_env)
    with pytest.raises(Exception) as ei:
        config_env.mgr._build_sync_block({
            "name": "X", "source": "/src/", "target": "root@192.168.1.10:/",
        })
    assert "mbiguous" in str(ei.value)


def test_single_server_host_match_still_works_without_pin(config_env):
    # One server on the host is unambiguous — host-match still resolves it, no pin
    # needed (backward compatible with the common single-destination case).
    with open(os.path.join(config_env.keys_dir, "key_solo"), "w") as f:
        f.write("PRIVATE-KEY\n")
    with open(os.environ["MONARCHAEGIS_SERVERS_JSON"], "w") as f:
        json.dump([{"id": "s1", "alias": "Solo", "host": "10.0.0.9",
                    "user": "root", "port": 2222, "key_id": "key_solo"}], f)
    block = config_env.mgr._build_sync_block({
        "name": "X", "source": "/src/", "target": "root@10.0.0.9:/",
    })
    assert "key_solo" in block


def test_unknown_pinned_key_is_rejected(config_env):
    _three_same_host_servers(config_env)
    with pytest.raises(Exception):
        config_env.mgr._build_sync_block({
            "name": "X", "source": "/src/",
            "target": "root@192.168.1.10:/", "key_id": "key_does_not_exist",
        })


def test_pinned_key_traversal_is_neutralized(config_env):
    # basename() strips path components, so a traversal key_id degrades to a plain
    # name inside keys_dir (here: nonexistent) and is rejected — never escapes.
    _three_same_host_servers(config_env)
    with pytest.raises(Exception):
        config_env.mgr._build_sync_block({
            "name": "X", "source": "/src/",
            "target": "root@192.168.1.10:/", "key_id": "../../etc/passwd",
        })
