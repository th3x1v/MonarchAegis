# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

"""Shared, domain-neutral constants.

Kept in its own module so unrelated domains (config generation in
config_manager, runtime execution in main) can share a value without importing
each other — avoiding the cross-module coupling that would come from defining
it in one of those modules.
"""

# rsync flag that disables secluded/protected args (`-s`). The destination's
# rrsync jail hard-disables `-s`, so every rsync push — the generated lsyncd
# config (config_manager) and the recovery push (main) — must pass this to
# guarantee the client never sends it and the jail can't reject the transfer.
RSYNC_NO_SECLUDED_ARGS = "--no-secluded-args"

# --- Tar-stream transport (batch transfer for many-small-file diffs) ---
# For a diff that is thousands of small files, rsync's per-file protocol
# round-trips dominate the wall-clock time. The tar-stream transport instead
# sends the whole transfer set as ONE tar over ONE ssh connection.
#
# The Source opens an ssh session whose remote command is exactly this sentinel.
# The destination's forced-command dispatcher (app/monarchaegis_recv.sh) matches it
# against SSH_ORIGINAL_COMMAND and routes to the jailed tar receiver
# (tar_receiver.py); ANY other command falls through to rrsync unchanged, so the
# rsync path is never touched. If this literal changes, update the launcher too
# (a test asserts they stay in sync).
TAR_SENTINEL = "monarchaegis-tar-v1"
# Digest algorithm for the whole-tarball integrity check. xxhash is already a
# runtime dependency and is used only for corruption/truncation detection here
# (NOT authentication — the SSH channel authenticates), so a fast non-crypto
# hash is the right tool.
TAR_DIGEST_ALGO = "xxh64"
