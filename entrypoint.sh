#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV
# MonarchAegis container entrypoint: prepares sshd state, then starts
# sshd (background) + uvicorn (foreground).

# Configure sshd to listen on the custom port (default 2222)
sed -i "s/^#\?Port .*/Port ${MONARCHAEGIS_SSH_PORT}/" /etc/ssh/sshd_config

# --- One-time rename migration (livesyncd/lsyncd -> MonarchAegis) ---
# Persistent /config artifacts created under the OLD name are moved to the new
# name BEFORE the app opens them — otherwise it would create empty new files and
# look like it lost every target, hash, and pairing. Idempotent: only moves when
# the old path exists and the new one does not, so it is a no-op on every boot
# after the first and on fresh installs.
_ma_migrate() { [ -e "$1" ] && [ ! -e "$2" ] && mv "$1" "$2" && echo "MonarchAegis migrate: $1 -> $2"; }
_ma_migrate /config/livesyncd.db          /config/monarchaegis.db
_ma_migrate /config/livesyncd.db-wal      /config/monarchaegis.db-wal
_ma_migrate /config/livesyncd.db-shm      /config/monarchaegis.db-shm
_ma_migrate /config/lsyncd.conf.lua       /config/monarchaegis.conf.lua
_ma_migrate /config/lsyncd.conf.lua.bak   /config/monarchaegis.conf.lua.bak
# Rewrite the dispatcher path + key comment baked into authorized_keys so
# pairings made under the old name run the renamed forced command (tar keeps
# working without re-pairing). Done before the live-mirror restore below.
if [ -s /config/authorized_keys ]; then
    sed -i 's#/usr/local/bin/livesyncd-recv#/usr/local/bin/monarchaegis-recv#g; s/livesyncd_client_/monarchaegis_client_/g' /config/authorized_keys
fi

# Persistent authorized_keys: the canonical copy lives in /config (survives
# container recreates), but sshd reads /root/.ssh/authorized_keys. The /config
# volume is host-owned (often nobody:users / 0777 on Unraid), which sshd
# StrictModes rejects — so we restore the persistent copy into /root/.ssh
# (root-owned, 600) at every boot. server_manager.py keeps both in sync.
mkdir -p /config
touch /config/authorized_keys
chmod 600 /config/authorized_keys 2>/dev/null || true
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cp /config/authorized_keys /root/.ssh/authorized_keys
chown root:root /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# Persistent SSH host keys: keys baked at image build change on every image
# update, breaking known_hosts pinning on all paired Sources (rsync code 255).
# First boot snapshots the image keys into /config; later boots restore them
# so this server keeps one stable SSH identity forever.
mkdir -p /config/ssh_host_keys
if ! ls /config/ssh_host_keys/ssh_host_*_key >/dev/null 2>&1; then
    cp /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub /config/ssh_host_keys/
fi
cp /config/ssh_host_keys/ssh_host_*_key /config/ssh_host_keys/ssh_host_*_key.pub /etc/ssh/
chown root:root /etc/ssh/ssh_host_*
chmod 600 /etc/ssh/ssh_host_*_key
chmod 644 /etc/ssh/ssh_host_*_key.pub

# Start SSH daemon in the background (Client mode needs this for incoming rsync)
/usr/sbin/sshd
echo "sshd: started on port ${MONARCHAEGIS_SSH_PORT}"

# Start the main web server in the foreground, under tini as PID 1.
# tini reaps orphaned zombie processes (e.g. ssh children left behind when an
# rsync exits first) that would otherwise accumulate until the container hits
# its PID limit and can no longer fork.
# -g forwards signals to the child's whole process group so rsync/ssh
# also get SIGTERM on `docker stop`, giving a clean, prompt shutdown.
exec tini -g -- uvicorn main:app --host 0.0.0.0 --port 5000 --workers 1 --no-access-log
