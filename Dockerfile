FROM python:3.11-slim

# Image metadata + license (OCI standard labels).
LABEL org.opencontainers.image.title="MonarchAegis" \
      org.opencontainers.image.description="Self-hosted, DB-driven data-protection and replication platform" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.authors="cTheXIV" \
      org.opencontainers.image.vendor="Crimson Elegy" \
      org.opencontainers.image.source="https://github.com/Th3X1V/MonarchAegis" \
      org.opencontainers.image.url="https://github.com/Th3X1V/MonarchAegis"

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Default SSH port for P2P rsync transfers (avoids conflicting with host port 22)
ENV MONARCHAEGIS_SSH_PORT=2222

# Install system dependencies: rsync + ssh/sshd for the P2P transport.
# perl is required to run rrsync (it is a Perl script).
# ffmpeg provides ffprobe, used by MONARCHAEGIS_HASH_MODE=metadata to fingerprint
# media files from their technical metadata instead of reading full contents.
# tini is used as PID 1 to reap orphaned zombies — see entrypoint.sh for why.
# (The lsyncd daemon was retired — no longer installed.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc rsync openssh-client openssh-server perl ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

# Setup rrsync (restricted rsync) for directory-scoped SSH key jailing.
# Search common locations across Debian versions — extract .gz if needed.
# The shebang is rewritten to /usr/local/bin/python3 because the python-slim
# base image has no /usr/bin/python3 — Debian's stock shebang would make every
# jailed connection die with exit code 127 (the perl variant is left untouched).
# Build fails if rrsync is missing: key jailing depends on it.
RUN RRSYNC_SRC=$(find /usr/share/doc/rsync /usr/share/rsync /usr/lib/rsync /usr/bin -name "rrsync*" 2>/dev/null | head -1) \
    && test -n "$RRSYNC_SRC" \
    && case "$RRSYNC_SRC" in \
        *.gz) gunzip -c "$RRSYNC_SRC" > /usr/local/bin/rrsync ;; \
        *)    cp "$RRSYNC_SRC" /usr/local/bin/rrsync ;; \
    esac \
    && sed -i '1s|^#!.*python.*|#!/usr/local/bin/python3|' /usr/local/bin/rrsync \
    && chmod +x /usr/local/bin/rrsync \
    && echo "rrsync installed from $RRSYNC_SRC"

# Prepare sshd: generate host keys and create the privilege separation directory
RUN mkdir -p /var/run/sshd \
    && ssh-keygen -A \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config \
    && sed -i 's/#StrictModes yes/StrictModes no/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && echo "PasswordAuthentication no" >> /etc/ssh/sshd_config

# Install python dependencies
RUN pip install --no-cache-dir fastapi uvicorn aiofiles sse-starlette psutil asyncssh watchdog requests xxhash

# Copy the application code
COPY ./app /app/
COPY ./docs /docs/

# License + attribution shipped in the image (AGPL-3.0-or-later).
COPY LICENSE NOTICE README.md /app/

# Install the paired-key forced-command dispatcher. authorized_keys entries run
# this (server_manager.RECV_PATH); it execs rrsync for a normal rsync push or
# the jailed tar receiver (app/tar_receiver.py) for a tar-stream batch. Copied
# out of /app so it has a stable path independent of the app working dir; CRLF
# strip guards against Windows checkouts.
RUN cp /app/monarchaegis_recv.sh /usr/local/bin/monarchaegis-recv \
    && sed -i 's/\r$//' /usr/local/bin/monarchaegis-recv \
    && chmod +x /usr/local/bin/monarchaegis-recv

# Entrypoint: prepares persistent sshd state (authorized_keys, host keys),
# then starts sshd (background) + uvicorn (foreground).
# CRLF strip guards against Windows checkouts of the repo.
COPY ./entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Expose the API port and the custom SSH port
EXPOSE 5000 2222

# Start both sshd and uvicorn
CMD ["/entrypoint.sh"]
