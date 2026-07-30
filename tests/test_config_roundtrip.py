"""config_manager sync-block generation/parsing round-trips.

Uses the `config_env` fixture (see conftest.py) for a fresh config file +
servers.json + key per test.
"""

PAIRED = {
    "name": "Movies Sync",
    "source": "/source_data/movies",
    "target": "root@192.168.1.50:/source_data/backup/movies",
}


def test_paired_target_generates_rrsync_config(config_env):
    mgr, keys_dir = config_env.mgr, config_env.keys_dir
    mgr.add_target(dict(PAIRED))
    raw = mgr.read_raw_config()

    assert "default.rsync," in raw
    assert "rsyncssh" not in raw
    assert 'target = "root@192.168.1.50:/"' in raw            # physical target = jail root
    assert "-- destpath: /source_data/backup/movies" in raw   # real dir in comment
    assert ("-i " + keys_dir in raw.replace("\\", "/")) or ("key_abc123" in raw)
    assert "-p 2222" in raw
    assert "BatchMode=yes" in raw
    assert "StrictHostKeyChecking=accept-new" in raw
    assert "--whole-file" in raw                              # -W patch present


def test_parse_reconstructs_logical_and_physical(config_env):
    mgr = config_env.mgr
    mgr.add_target(dict(PAIRED))
    targets = mgr.parse_targets()
    assert len(targets) == 1
    t = targets[0]
    assert t["name"] == "Movies Sync"
    assert t["id"] == "moviessync"
    assert t["source"] == "/source_data/movies"
    assert t["target"] == "root@192.168.1.50:/source_data/backup/movies"
    assert t["physical_target"] == "root@192.168.1.50:/"


def test_update_target_roundtrip(config_env):
    mgr = config_env.mgr
    mgr.add_target(dict(PAIRED))
    t = mgr.parse_targets()[0]
    assert mgr.update_target(t["id"], {"name": t["name"], "source": t["source"], "target": t["target"]})
    targets2 = mgr.parse_targets()
    assert len(targets2) == 1
    assert targets2[0]["target"] == "root@192.168.1.50:/source_data/backup/movies"


def test_legacy_rsyncssh_block_parses(config_env):
    mgr = config_env.mgr
    mgr.add_target(dict(PAIRED))
    old_block = '''
-- name: Legacy Target
sync {
    default.rsyncssh,
    source = "/source_data/tv",
    host = "root@10.0.0.9",
    targetdir = "/data/tv",
    delay = 15,
    init = false,
    rsync = {
        _extra = {"--verbose"}
    },
    ssh = {
        port = 2222,
        options = {
            StrictHostKeyChecking = "accept-new"
        }
    }
}
'''
    mgr._atomic_write(mgr.read_raw_config() + old_block)
    targets = mgr.parse_targets()
    assert len(targets) == 2
    legacy = next((x for x in targets if x["name"] == "Legacy Target"), None)
    assert legacy is not None
    assert legacy["target"] == "root@10.0.0.9:/data/tv"
    assert legacy["physical_target"] == legacy["target"]


def test_delete_target(config_env):
    mgr = config_env.mgr
    mgr.add_target(dict(PAIRED))
    assert mgr.delete_target("moviessync")
    assert len(mgr.parse_targets()) == 0
    assert "-- destpath:" not in mgr.read_raw_config()


def test_missing_key_raises(config_env):
    mgr = config_env.mgr
    import pytest
    with pytest.raises(Exception):
        mgr._build_sync_block({"name": "x", "source": "/s", "target": "root@9.9.9.9:/d"})


def test_local_target_uses_target_field(config_env):
    mgr = config_env.mgr
    block = mgr._build_sync_block({"name": "loc", "source": "/a", "target": "/b"})
    assert 'target = "/b",' in block
    assert "targetdir" not in block
