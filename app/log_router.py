# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

import os
from envcompat import env
import re
import asyncio
import aiofiles
from typing import Dict, List, Any
from database import db

# These point to the read-only mounted volume from unRAID
LOG_PATH = env("MONARCHAEGIS_LOG_PATH", "/logs/monarchaegis.log")
STATUS_PATH = env("MONARCHAEGIS_STATUS_PATH", "/logs/monarchaegis.status")

# Fallback for local testing outside of docker if no env var is provided
if "MONARCHAEGIS_LOG_PATH" not in os.environ and not os.path.exists(os.path.dirname(LOG_PATH)):
    local_mock_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "mock_data", "logs", "monarchaegis.log")
    LOG_PATH = local_mock_path
    STATUS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "mock_data", "logs", "monarchaegis.status")

class LogRouter:
    """Tails and routes multiplexed lsyncd logs into in-memory buckets per target."""
    
    def __init__(self):
        # Memory structure: {"target_1": []}
        self.target_logs: Dict[str, List[str]] = {}
        self.target_updates: Dict[str, int] = {}
        self.target_status: Dict[str, str] = {}
        # Stores the current live inotify delay count for each target
        self.target_queues: Dict[str, int] = {}
        # Snapshot of the queue count when a batch was dispatched to rsync
        self.target_processing: Dict[str, int] = {}
        # Stores the offending host/port string when a key mismatch occurs
        self.target_security_alerts: Dict[str, str] = {}

    def start_tailing(self):
        """Retired in Phase 4: there is no lsyncd/rsync daemon log to tail. The
        per-target SSE log buckets are now fed directly via _add_log() by the
        sync engine and the recovery endpoints. Kept as a no-op so any remaining
        caller is harmless."""
        return
        
    def _ensure_bucket_exists(self, target_id: str):
        if target_id not in self.target_logs:
            self.target_logs[target_id] = []
            self.target_updates[target_id] = 0
            self.target_status[target_id] = "In Sync"
            self.target_queues[target_id] = 0
            self.target_processing[target_id] = 0
            self.target_security_alerts[target_id] = None

    def _add_log(self, target_id: str, message: str, max_size=200):
        """Adds a log to the rotation queue (FIFO)."""
        self._ensure_bucket_exists(target_id)
        queue = self.target_logs[target_id]
        queue.append(message)
        if len(queue) > max_size:
            queue.pop(0)
            
        self.target_updates[target_id] += 1

    def update_target_mapping(self, targets: List[Dict]):
        """Updates the memory map of source paths to target IDs"""
        self.target_sources = {t["id"]: t["source"] for t in targets}

    def parse_log_line(self, line: str):
        """
        Extracts target by matching known source directories and routes the raw text line to the buckets.
        """
        line = line.strip()
        if not line:
            return
        
        target_ids = []
        # Find which targets this log belongs to by looking for their source path
        if hasattr(self, 'target_sources'):
            for tid, source in self.target_sources.items():
                # lsyncd usually logs source with trailing slash or inside quotes
                if source in line:
                    target_ids.append(tid)
                    
        if not target_ids:
            target_ids = ["global"]

        for tid in target_ids:
            self._ensure_bucket_exists(tid)
            self._add_log(tid, line)
            
            # Security Alert Detection
            if "ssh-keygen -f" in line and "-R" in line:
                match = re.search(r"ssh-keygen -f .*? -R '?([^'\s]+)'?", line)
                if match:
                    host_port = match.group(1).strip("'")
                    self.target_status[tid] = "Security Alert: Key Changed"
                    self.target_security_alerts[tid] = host_port
                    continue # Skip other status updates for this line

            # If the target is in a Security Alert state, ignore all other background status updates
            # until the alert is explicitly cleared by the user trusting the new key.
            if self.target_status.get(tid) == "Security Alert: Key Changed":
                continue
            
            if "recursive startup rsync" in line:
                self.target_status[tid] = "Scanning"
                self.target_processing[tid] = 0 # Cannot track files during scan
            elif "Calling rsync" in line or "Startup rsync" in line:
                self.target_status[tid] = "Syncing"
                # Snapshot the current queue count as the processing count
                self.target_processing[tid] = self.target_queues.get(tid, 0)
            elif ">f" in line or "*deleting" in line or "cd++++" in line:
                self.target_status[tid] = "Syncing"
                # Decrement the processing counter as files (or folders) finish
                if self.target_processing.get(tid, 0) > 0:
                    self.target_processing[tid] -= 1
                
                # Parse the standard --itemize-changes format
                action = None
                filepath = None
                
                if ">f" in line:
                    action = "Update"
                    filepath = line.split(" ", 1)[-1].strip()
                elif "*deleting" in line:
                    action = "Delete"
                    filepath = line.split(" ", 1)[-1].strip()
                    
                if action and filepath:
                    db.log_transfer(tid, filepath, action)
                    # Actively clear this file from the missing list if it was queued there!
                    db.remove_missing_file(tid, filepath)
                    
            elif "Finished" in line and "(list)" in line or "finished: 0" in line.lower():
                self.target_status[tid] = "In Sync"
                self.target_processing[tid] = 0
        
        # Always add to global for a unified view
        if "global" not in target_ids:
            self._ensure_bucket_exists("global")
            self._add_log("global", line)

    # tail_logs / tail_rsync_logs / poll_status_file were removed in Phase 4b:
    # with lsyncd retired there is no monarchaegis.log / rsync.log / monarchaegis.status to
    # read. parse_log_line() is kept — the legacy sync_missing recovery path still
    # routes its output through it to raise host-key security alerts.

    def get_logs_for_target(self, target_id: str) -> List[str]:
        """Returns the last logs for a specific target."""
        return self.target_logs.get(target_id, [])

    def clear_logs(self):
        """Empties all current logs from memory and increments update counters so UI reflects the clear."""
        self.target_logs = {}
        for target_id in self.target_updates:
            self.target_updates[target_id] += 1
        
    def get_update_count(self, target_id: str) -> int:
        """Returns the total number of logs processed for this target to track stream changes."""
        return self.target_updates.get(target_id, 0)
