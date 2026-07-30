"""server_manager.generate_client_keypair: rrsync jail entry format, re-pair
de-duplication, and the persistent -> live authorized_keys mirror.

Needs ssh-keygen on PATH (present in the runtime image). Uses the `server_env`
fixture (see conftest.py) for temp key/auth paths + an rrsync stub.
"""
import os

import pytest


def _recv_dir(env):
    return f"{env.base}/recv dir/My Movies"


def test_first_pairing_creates_jail_entry(server_env):
    import server_manager
    recv = _recv_dir(server_env)
    priv = server_env.mgr.generate_client_keypair("t1", recv)
    assert priv.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert os.path.isdir(recv)

    auth = open(server_env.auth_keys).read()
    # Forced command is now the dispatcher (execs rrsync or the tar receiver),
    # given the jail dir as its single quoted argument.
    expected_cmd = f'command="{server_manager.RECV_PATH} \\"{recv}\\""'
    assert expected_cmd in auth
    assert '",restrict ssh-ed25519' in auth
    assert "monarchaegis_client_t1" in auth
    # live mirror matches the persistent copy
    assert open(server_env.live_auth).read() == auth


def test_repair_replaces_not_appends(server_env):
    recv = _recv_dir(server_env)
    priv = server_env.mgr.generate_client_keypair("t1", recv)
    priv2 = server_env.mgr.generate_client_keypair("t1", recv)
    auth = open(server_env.auth_keys).read()
    assert priv2 != priv                                   # fresh key on re-pair
    assert auth.count("monarchaegis_client_t1") == 1          # entry replaced, not duplicated


def test_second_target_appends_alongside(server_env):
    server_env.mgr.generate_client_keypair("t1", _recv_dir(server_env))
    server_env.mgr.generate_client_keypair("t2", f"{server_env.base}/recv2")
    auth = open(server_env.auth_keys).read()
    assert auth.count("monarchaegis_client_t1") == 1
    assert auth.count("monarchaegis_client_t2") == 1
    assert open(server_env.live_auth).read() == auth       # live mirror updated


def test_hostile_path_rejected(server_env):
    with pytest.raises(ValueError):
        server_env.mgr.generate_client_keypair("t3", f'{server_env.base}/evil" injection')


def test_missing_rrsync_rejected(server_env):
    os.remove(server_env.rrsync)
    with pytest.raises(RuntimeError):
        server_env.mgr.generate_client_keypair("t4", f"{server_env.base}/recv4")


# --- one-time rrsync -> dispatcher forced-command upgrade (Option B) ---

def test_upgrade_rewrites_rrsync_entries_to_dispatcher(server_env):
    import server_manager
    recv = _recv_dir(server_env)
    # Simulate a key paired on the pre-tar image: forced command points at rrsync.
    legacy = (f'command="{server_manager.RRSYNC_PATH} -wo \\"{recv}\\"",restrict '
              f'ssh-ed25519 AAAAKEYDATA monarchaegis_client_t1')
    with open(server_env.auth_keys, "w") as f:
        f.write(legacy + "\n")

    assert server_env.mgr.upgrade_authorized_keys_to_dispatcher() == 1
    upgraded = open(server_env.auth_keys).read()
    # binary swapped, jail + pubkey + comment preserved, -wo dropped (dispatcher adds it)
    assert f'command="{server_manager.RECV_PATH} \\"{recv}\\""' in upgraded
    assert f"{server_manager.RRSYNC_PATH} -wo" not in upgraded
    assert "ssh-ed25519 AAAAKEYDATA monarchaegis_client_t1" in upgraded
    assert open(server_env.live_auth).read() == upgraded          # mirror synced

    # idempotent: nothing left to upgrade on a second pass
    assert server_env.mgr.upgrade_authorized_keys_to_dispatcher() == 0


def test_upgrade_leaves_dispatcher_and_other_lines_untouched(server_env):
    import server_manager
    recv = _recv_dir(server_env)
    # An already-dispatcher entry plus an unrelated line must both survive verbatim.
    already = f'command="{server_manager.RECV_PATH} \\"{recv}\\"",restrict ssh-ed25519 AAAANEW c2'
    unrelated = "# a comment line"
    with open(server_env.auth_keys, "w") as f:
        f.write(already + "\n" + unrelated + "\n")

    assert server_env.mgr.upgrade_authorized_keys_to_dispatcher() == 0
    body = open(server_env.auth_keys).read()
    assert already in body and unrelated in body


def test_upgrade_no_authorized_keys_file(server_env):
    # No file yet -> no-op, no crash.
    if os.path.exists(server_env.auth_keys):
        os.remove(server_env.auth_keys)
    assert server_env.mgr.upgrade_authorized_keys_to_dispatcher() == 0
