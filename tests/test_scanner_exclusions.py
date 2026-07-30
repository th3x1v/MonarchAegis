"""hash_scanner path exclusions: in-flight temp copies and the version store.

Cataloguing a temp file guarantees a stale ledger row the moment it's renamed to
its final name — Sonarr/Radarr write `*.partial~` during a copy-mode import, and
rsync writes `.<name>.XXXXXX` (we have seen exactly that land in a real ledger).
Cataloguing `.versions/` would replicate retired copies and version the versions.
"""
import hash_scanner as hs


# --- excluded ---

def test_sonarr_radarr_partial_excluded():
    assert hs.is_excluded_path("Show/Season 1/Episode.mkv.partial~") is True


def test_rsync_temp_excluded():
    # the real-world shape: .<original name>.<6 random chars>
    assert hs.is_excluded_path(
        "Newer Movies/They Will Kill You (2026)/"
        ".They Will Kill You (2026) Bluray-2160p.mkv.3ulxnx") is True


def test_download_client_temps_excluded():
    assert hs.is_excluded_path("x/file.mkv.part") is True
    assert hs.is_excluded_path("x/file.mkv.!qB") is True
    assert hs.is_excluded_path("x/file.mkv.crdownload") is True


def test_version_store_excluded_at_any_depth():
    assert hs.is_excluded_path(".versions/20260722T143000Z/Episode.mkv") is True
    assert hs.is_excluded_path(
        "Call of the Night/Season 2/.versions/20260722T143000Z/Episode.mkv") is True


# --- NOT excluded (regression guards: don't over-match real library files) ---

def test_normal_media_not_excluded():
    assert hs.is_excluded_path("Call of the Night/Season 2/Episode Bluray-1080p.mkv") is False


def test_ordinary_dotfiles_not_excluded():
    # .plexmatch / .nfo style sidecars are real, tracked files
    assert hs.is_excluded_path("Show/.plexmatch") is False
    assert hs.is_excluded_path("Show/Season 1/episode.nfo") is False


def test_filename_containing_versions_not_excluded():
    # only a `.versions` DIRECTORY component is excluded, not a similarly-named file
    assert hs.is_excluded_path("Show/all.versions.mkv") is False


def test_empty_path_is_safe():
    assert hs.is_excluded_path("") is False
