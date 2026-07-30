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


def test_empty_value_counts_as_set(monkeypatch):
    monkeypatch.setenv("MONARCHAEGIS_EMPTY", "")
    assert envcompat.env("MONARCHAEGIS_EMPTY", "default") == ""


def test_legacy_names_mapping():
    assert envcompat.legacy_names("MONARCHAEGIS_DB_PATH") == \
        ["LSYNCD_DB_PATH", "LIVESYNCD_DB_PATH"]
    assert envcompat.legacy_names("NOT_PREFIXED") == []
