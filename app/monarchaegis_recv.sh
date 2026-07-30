#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV
# Forced-command dispatcher for a paired Source SSH key.
#
# This is the ONLY command an incoming Source key may run (see
# server_manager.generate_client_keypair, which writes it into authorized_keys as
#   command="/usr/local/bin/monarchaegis-recv \"<jail>\"",restrict <pubkey>
# ). sshd puts the Source's real request in $SSH_ORIGINAL_COMMAND and passes the
# jail directory as $1.
#
# Fast path: anything that is NOT our tar sentinel is a normal rsync push and
# goes straight to the proven, upstream-audited rrsync jail — no python, rsync
# behaviour byte-for-byte unchanged. Only the tar-stream transport runs python.
#
# The sentinel literal below MUST equal constants.TAR_SENTINEL; a test asserts
# it (test_tar_receiver.test_launcher_sentinel_matches_constant).
if [ "$SSH_ORIGINAL_COMMAND" = "monarchaegis-tar-v1" ]; then
    exec /usr/local/bin/python3 /app/tar_receiver.py "$1"
fi
exec /usr/local/bin/rrsync -wo "$1"
