"""envcompat: the MonarchAegis env-var back-compat shim.

Reads MONARCHAEGIS_* but falls back to the pre-rename LSYNCD_* / LIVESYNCD_*
names, so deployments configured before the rename keep working. The LIVESYNCD_
fallback is called out explicitly because a sweep bug once collapsed it onto
MONARCHAEGIS_ (silently breaking ROLE/PASSWORD/PAIR_SECRET/SAFE_MODE/USERNAME).
"""
import envcompat


def test_reads_current_name(monkeypatch):
    monkeypatch.setenv("MONARCHAEGIS_HASH_MODE", "metadata")
    assert envcompat.env("MONARCHAEGIS_HASH_MODE") == "metadata"


def test_falls_back_to_lsyncd_legacy(monkeypatch):
    monkeypatch.delenv("MONARCHAEGIS_HASH_MODE", raising=False)
    monkeypatch.setenv("LSYNCD_HASH_MODE", "sampled")
    assert envcompat.env("MONARCHAEGIS_HASH_MODE") == "sampled"


def test_falls_back_to_livesyncd_legacy(monkeypatch):
    # LIVESYNCD_ carried the app-level vars (ROLE/PASSWORD/...) — must still resolve.
    monkeypatch.delenv("MONARCHAEGIS_ROLE", raising=False)
    monkeypatch.setenv("LIVESYNCD_ROLE", "client")
    assert envcompat.env("MONARCHAEGIS_ROLE") == "client"


def test_current_name_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("MONARCHAEGIS_ROLE", "source")
    monkeypatch.setenv("LIVESYNCD_ROLE", "client")
    assert envcompat.env("MONARCHAEGIS_ROLE") == "source"


def test_default_when_all_absent(monkeypatch):
    for n in ("MONARCHAEGIS_NOPE", "LSYNCD_NOPE", "LIVESYNCD_NOPE"):
        monkeypatch.delenv(n, raising=False)
    assert envcompat.env("MONARCHAEGIS_NOPE", "fallback") == "fallback"


def test_empty_value_is_treated_as_unset(monkeypatch):
    """Unraid/compose pass blank optional fields through as empty strings. Taking
    "" literally crashed startup (int("") for HASH_WORKERS), so empty falls back."""
    monkeypatch.setenv("MONARCHAEGIS_EMPTY", "")
    assert envcompat.env("MONARCHAEGIS_EMPTY", "default") == "default"


def test_whitespace_only_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("MONARCHAEGIS_BLANK", "   ")
    assert envcompat.env("MONARCHAEGIS_BLANK", "default") == "default"


def test_empty_current_falls_back_to_legacy(monkeypatch):
    # blank new-style var must not shadow a real legacy value
    monkeypatch.setenv("MONARCHAEGIS_HASH_MODE", "")
    monkeypatch.setenv("LSYNCD_HASH_MODE", "metadata")
    assert envcompat.env("MONARCHAEGIS_HASH_MODE") == "metadata"


def test_int_setting_with_blank_env_uses_default(monkeypatch):
    """The exact startup crash: Unraid ships HASH_WORKERS with Default="" ."""
    monkeypatch.setenv("MONARCHAEGIS_HASH_WORKERS", "")
    assert int(envcompat.env("MONARCHAEGIS_HASH_WORKERS", 8)) == 8


def test_legacy_names_mapping():
    assert envcompat.legacy_names("MONARCHAEGIS_DB_PATH") == \
        ["LSYNCD_DB_PATH", "LIVESYNCD_DB_PATH"]
    assert envcompat.legacy_names("NOT_PREFIXED") == []
