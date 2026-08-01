"""server_manager profile management: per-key server profiles for the same host
(the multi-share-to-one-destination case) and orphan cleanup on target removal.

Uses the `server_env` fixture (temp KEYS_DIR / SERVERS_JSON, rrsync stub).
"""
import os


def test_same_host_distinct_keys_get_separate_profiles(server_env):
    """Three shares to one destination host, each with its own key, must NOT
    collapse onto one profile (that was the cross-wiring bug)."""
    mgr = server_env.mgr
    a = mgr.add_server("Anime", "192.168.1.10", "root", 2222, "key_anime")
    m = mgr.add_server("Movies", "192.168.1.10", "root", 2222, "key_movies")
    s = mgr.add_server("Series", "192.168.1.10", "root", 2222, "key_series")

    assert len({a, m, s}) == 3
    servers = mgr.list_servers()
    assert len(servers) == 3
    assert {sv["key_id"] for sv in servers} == {"key_anime", "key_movies", "key_series"}
    # each id resolves back to its own key
    assert mgr.get_server(m)["key_id"] == "key_movies"


def test_same_host_same_key_updates_in_place(server_env):
    """Re-linking the SAME key updates that profile (alias/port), not a duplicate."""
    mgr = server_env.mgr
    first = mgr.add_server("Movies", "192.168.1.10", "root", 2222, "key_movies")
    again = mgr.add_server("Movies (renamed)", "192.168.1.10", "root", 2299, "key_movies")

    assert first == again
    servers = mgr.list_servers()
    assert len(servers) == 1
    assert servers[0]["alias"] == "Movies (renamed)"
    assert servers[0]["port"] == 2299


def test_remove_server_and_key_cleans_orphan(server_env):
    mgr = server_env.mgr
    kid = mgr.save_key("movies", "PRIVATE-KEY-BODY")
    sid = mgr.add_server("Movies", "192.168.1.10", "root", 2222, kid)
    assert os.path.exists(mgr.get_key_path(kid))

    assert mgr.remove_server_and_key(sid) is True
    assert mgr.get_server(sid) is None
    assert not os.path.exists(mgr.get_key_path(kid))          # unused key removed too


def test_remove_server_keeps_key_shared_by_another_profile(server_env):
    mgr = server_env.mgr
    kid = mgr.save_key("shared", "PRIVATE-KEY-BODY")
    s1 = mgr.add_server("A", "host-a", "root", 2222, kid)     # different hosts -> 2 profiles
    s2 = mgr.add_server("B", "host-b", "root", 2222, kid)

    assert mgr.remove_server_and_key(s1) is True
    assert mgr.get_server(s1) is None
    assert mgr.get_server(s2) is not None
    assert os.path.exists(mgr.get_key_path(kid))              # still used by s2 -> kept


def test_remove_missing_server_is_noop(server_env):
    assert server_env.mgr.remove_server_and_key("srv_does_not_exist") is False


# --- optional per-profile Client API port ---

def test_api_port_omitted_when_not_specified(server_env):
    sid = server_env.mgr.add_server("A", "10.0.0.2", "root", 2222, "k1")
    assert "api_port" not in server_env.mgr.get_server(sid)   # absent, not 0/empty


def test_api_port_stored_when_specified(server_env):
    sid = server_env.mgr.add_server("A", "10.0.0.2", "root", 2222, "k1", api_port=5001)
    assert server_env.mgr.get_server(sid)["api_port"] == 5001


def test_relink_without_api_port_keeps_existing(server_env):
    """Re-linking with the optional field left blank must not wipe a custom port."""
    sid = server_env.mgr.add_server("A", "10.0.0.2", "root", 2222, "k1", api_port=5001)
    server_env.mgr.add_server("A renamed", "10.0.0.2", "root", 2222, "k1")   # blank
    assert server_env.mgr.get_server(sid)["api_port"] == 5001


def test_client_api_base_defaults_and_overrides():
    import sync_engine
    # not specified -> global default (5000)
    assert sync_engine.client_api_base({"host": "10.0.0.2"}) == \
        f"http://10.0.0.2:{sync_engine.CLIENT_API_PORT}"
    # specified -> profile wins
    assert sync_engine.client_api_base({"host": "10.0.0.2", "api_port": 5001}) == \
        "http://10.0.0.2:5001"
    # explicit null/0 is treated as unset, not as port 0
    assert sync_engine.client_api_base({"host": "10.0.0.2", "api_port": None}) == \
        f"http://10.0.0.2:{sync_engine.CLIENT_API_PORT}"
