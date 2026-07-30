document.addEventListener('DOMContentLoaded', () => {

    // --- Elements --- //
    const targetIdDisplay = document.getElementById('target-id-display');
    const targetStatus = document.getElementById('target-status');
    const terminalOutput = document.getElementById('terminal-output');
    const terminalJobName = document.getElementById('terminal-job-name');
    const clearLogsBtn = document.getElementById('clear-logs');
    const btnAckErrors = document.getElementById('btn-ack-errors');
    const errorCountDisplay = document.getElementById('error-count');
    const btnSyncNow = document.getElementById('btn-sync-now');
    const btnVerifyChecksum = document.getElementById('btn-verify-checksum');
    const chkIgnoreExisting = document.getElementById('chk-ignore-existing');
    const btnSyncMissing = document.getElementById('btn-sync-missing');
    const missingCountDisplay = document.getElementById('missing-count');
    const selSchedule = document.getElementById('sel-schedule');
    const scheduleNext = document.getElementById('schedule-next');
    const scheduleLast = document.getElementById('schedule-last');
    const scheduleToday = document.getElementById('schedule-today');

    let currentLogs = [];
    let ignoreLogsUpTo = null;

    // Initialize parsed known errors from Local Storage
    const urlParams = new URLSearchParams(window.location.search);
    const targetId = urlParams.get('id');
    const storageKey = `ack_errors_${targetId || 'global'}`;

    let acknowledgedErrors = new Set();
    try {
        const stored = localStorage.getItem(storageKey);
        if (stored) {
            const arr = JSON.parse(stored);
            arr.forEach(e => acknowledgedErrors.add(e));
        }
    } catch (e) {
        console.warn("Failed to load acknowledged errors from storage", e);
    }

    if (clearLogsBtn && terminalOutput) {
        clearLogsBtn.addEventListener('click', async () => {
            clearLogsBtn.disabled = true;
            terminalJobName.textContent = "Clearing logs...";
            try {
                await fetch('/api/logs/clear', { method: 'POST' });
                currentLogs = [];
                ignoreLogsUpTo = null;
                terminalOutput.innerHTML = '';
            } catch (error) {
                console.error("Failed to clear logs", error);
            } finally {
                clearLogsBtn.disabled = false;
                terminalJobName.textContent = `Streaming job: ${streamId}`;
            }
        });
    }

    if (btnAckErrors) {
        btnAckErrors.addEventListener('click', () => {
            const currentErrorLines = currentLogs.filter(l => /\b(?:error|warn):\b/i.test(l) || /failed/i.test(l));
            currentErrorLines.forEach(err => acknowledgedErrors.add(err.trim()));

            // Persist to local storage
            try {
                localStorage.setItem(storageKey, JSON.stringify(Array.from(acknowledgedErrors)));
            } catch (e) {
                console.warn("Failed to save acknowledged errors to storage", e);
            }

            renderLogs(currentLogs);
        });
    }

    // Sync Now: run one DB-driven sync (diff -> transfer -> register). Progress
    // streams into the terminal below via the SSE log bucket.
    if (btnSyncNow) {
        btnSyncNow.addEventListener('click', async () => {
            if (!targetId || targetId === 'global') return;
            btnSyncNow.disabled = true;
            btnSyncNow.textContent = "Syncing...";
            targetStatus.textContent = "Syncing...";
            targetStatus.className = 'status-indicator status-warning';
            try {
                await fetch(`/api/target/${targetId}/sync_now`, { method: 'POST' });
                // The SSE live stream picks up the sync progress automatically.
            } catch (error) {
                console.error("Failed to start sync", error);
                alert("Failed to start sync.");
            } finally {
                setTimeout(() => {
                    btnSyncNow.disabled = false;
                    btnSyncNow.textContent = "Sync Now";
                    loadSchedule();  // refresh last-run/next-run after a run kicks off
                }, 2500);
            }
        });
    }

    // Schedule selector: set this target's auto-sync interval (0 = manual).
    if (selSchedule) {
        selSchedule.addEventListener('change', async () => {
            if (!targetId || targetId === 'global') return;
            const interval = parseInt(selSchedule.value, 10);
            try {
                const res = await fetch(`/api/target/${targetId}/schedule`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ interval_seconds: interval, enabled: true }),
                });
                const data = await res.json();
                if (data.status !== 'success') {
                    alert(data.message || 'Failed to update schedule.');
                }
            } catch (error) {
                console.error("Failed to update schedule", error);
            } finally {
                loadSchedule();
            }
        });
    }

    // Pull this target's schedule row from the authoritative store and render the
    // selector value + next/last-run lines.
    async function loadSchedule() {
        if (!targetId || targetId === 'global' || !selSchedule) return;
        try {
            const res = await fetch('/api/db_targets');
            const data = await res.json();
            const t = (data.targets || []).find(x => x.id === targetId);
            if (!t) return;
            selSchedule.value = String(t.interval_seconds || 0);
            if (t.next_run) {
                const when = new Date(t.next_run * 1000);
                const due = t.next_run * 1000 <= Date.now();
                scheduleNext.textContent = due ? "Next run: due now" : `Next run: ${when.toLocaleString()}`;
            } else {
                scheduleNext.textContent = "Next run: manual (no schedule)";
            }
            scheduleLast.textContent = t.last_status
                ? `Last run: ${t.last_status}${t.last_summary ? ' — ' + t.last_summary : ''}`
                : "Last run: —";
            if (scheduleToday) {
                const n = t.transferred_today || 0;
                scheduleToday.textContent = `Today: ${n} file${n === 1 ? '' : 's'} transferred`;
            }
        } catch (error) {
            console.error("Failed to load schedule", error);
        }
    }
    loadSchedule();
    setInterval(loadSchedule, 15000);

    if (btnVerifyChecksum) {
        btnVerifyChecksum.addEventListener('click', async () => {
            if (!targetId || targetId === 'global') return;
            btnVerifyChecksum.disabled = true;
            btnVerifyChecksum.textContent = "Previewing...";

            const ignoreExisting = chkIgnoreExisting ? chkIgnoreExisting.checked : false;

            try {
                await fetch(`/api/target/${targetId}/verify?ignore_existing=${ignoreExisting}`, { method: 'POST' });
            } catch (error) {
                console.error("Failed to trigger checksum verification", error);
                alert("Failed to start verification.");
            } finally {
                setTimeout(() => {
                    btnVerifyChecksum.disabled = false;
                    btnVerifyChecksum.textContent = "Audit Missing Files (Dry Run)";
                }, 3000);
            }
        });
    }

    if (btnSyncMissing) {
        btnSyncMissing.addEventListener('click', async () => {
            if (!targetId || targetId === 'global') return;
            btnSyncMissing.disabled = true;
            btnSyncMissing.textContent = "Syncing...";
            try {
                await fetch(`/api/target/${targetId}/sync_missing`, { method: 'POST' });
            } catch (error) {
                console.error("Failed to start targeted missing sync", error);
            } finally {
                setTimeout(() => {
                    btnSyncMissing.textContent = "Recover Missing Files";
                }, 3000);
            }
        });
    }

    const btnExportCSV = document.getElementById('btn-export-csv');
    if (btnExportCSV) {
        btnExportCSV.addEventListener('click', () => {
            if (!targetId || targetId === 'global') return;
            window.location.href = `/api/target/${targetId}/export_csv`;
        });
    }

    if (!targetId) {
        targetIdDisplay.textContent = "Error: No Target ID Provided";
        targetStatus.textContent = "Offline";
        targetStatus.className = 'status-indicator status-offline';
        return;
    }

    targetIdDisplay.textContent = `Target: ${targetId}`;

    // For this generic scaffold, consider "global" if we couldn't parse properly
    // Real implementation requires perfect ID matching.
    const streamId = targetId || 'global';

    if (terminalJobName) {
        terminalJobName.textContent = `Streaming job: ${streamId}`;
    }

    // --- Establish SSE Connection for Live Logs --- //
    const eventSource = new EventSource(`/api/target/${streamId}/logs/stream`);

    eventSource.onmessage = function (event) {
        let logs;
        try {
            logs = JSON.parse(event.data);
        } catch (err) {
            console.error("Failed to parse SSE JSON payload:", event.data);
            return;
        }

        if (Array.isArray(logs) && terminalOutput) {
            currentLogs = logs;
            renderLogs(logs);
        }
    };

    function renderLogs(logs) {
        // Aggressively filter out verbose unraid logs, only keeping file transfers and errors
        let filteredLogs = logs.filter(log => {
            const isError = /\b(?:error|warn):\b/i.test(log) || /failed/i.test(log);
            const isTransfer = /\[<\]|\[>\]/.test(log);
            const isScanning = /recursive startup rsync/.test(log);
            const isFinished = /finished: 0|finished \(list\)/i.test(log);
            const isChecksum = /\[CheckSum\]/.test(log);
            const isRecovery = /\[Recovery\]/.test(log);
            return isError || isTransfer || isScanning || isFinished || isChecksum || isRecovery;
        });

        let visibleLogs = filteredLogs;
        if (ignoreLogsUpTo) {
            const idx = filteredLogs.lastIndexOf(ignoreLogsUpTo);
            if (idx !== -1) {
                visibleLogs = filteredLogs.slice(idx + 1);
            } else {
                ignoreLogsUpTo = null;
            }
        }

        const isScrolledToBottom = terminalOutput.scrollHeight - terminalOutput.scrollTop <= terminalOutput.clientHeight + 10;

        terminalOutput.innerHTML = '';
        visibleLogs.forEach(log => {
            const lineDiv = document.createElement('div');

            // Format nice string instead of verbose Exec log
            let displayString = log;
            const transferMatch = log.match(/\[[<>]\]\s+\[(.*?)\]/);
            if (transferMatch && transferMatch[1]) {
                displayString = `Syncing: ${transferMatch[1]}`;
            } else if (log.includes("recursive startup rsync")) {
                displayString = `[Scanning target directory for active changes...]`;
                lineDiv.style.color = "var(--text-muted)";
            } else if (log.toLowerCase().includes("finished: 0") || log.toLowerCase().includes("finished (list)")) {
                displayString = `[Sync Complete. Actively watching for file drops...]`;
                lineDiv.style.color = "var(--status-online)";
            } else if (log.includes("[CheckSum]")) {
                lineDiv.style.color = "#a78bfa";
                if (log.toLowerCase().includes("mismatch") || log.toLowerCase().includes("desynchronized")) {
                    lineDiv.style.color = "var(--status-error)";
                    displayString = "❌ " + displayString;
                } else if (log.toLowerCase().includes("100% mathematically")) {
                    lineDiv.style.color = "var(--status-online)";
                    displayString = "✅ " + displayString;
                }
            }

            lineDiv.textContent = displayString;
            // Only flag as error if it contains "Error:" or "Warn:" natively from lsyncd/rsync, skip flags like --ignore-errors
            if (/\b(?:error|warn):\b/i.test(log) || /failed/i.test(log)) {
                lineDiv.style.color = 'var(--status-error)';
            }
            terminalOutput.appendChild(lineDiv);
        });

        if (isScrolledToBottom) {
            terminalOutput.scrollTop = terminalOutput.scrollHeight;
        }

        const allErrors = logs.filter(l => /\b(?:error|warn):\b/i.test(l) || /failed/i.test(l));
        const activeErrors = allErrors.filter(err => !acknowledgedErrors.has(err.trim()));

        let currentState = "Idle";
        let stateClass = "status-online";

        for (let i = logs.length - 1; i >= 0; i--) {
            const l = logs[i].toLowerCase();
            if (l.includes('[checksum] verification complete')) {
                currentState = "Idle";
                stateClass = "status-online";
                break;
            } else if (l.includes('[checksum] initiating')) {
                currentState = '<span class="ascii-spinner" style="vertical-align: middle;"></span><span style="vertical-align: middle;">Verifying Hashes...</span>';
                stateClass = "status-warning";
                break;
            } else if (l.includes('finished: 0') || l.includes('finished (list)')) {
                currentState = "Idle";
                stateClass = "status-online";
                break;
            } else if (l.includes('[<]') || l.includes('[>]') || l.includes('calling rsync')) {
                currentState = "Syncing";
                stateClass = "status-success";
                break;
            } else if (l.includes('recursive startup rsync')) {
                currentState = "Scanning";
                stateClass = "status-warning";
                break;
            }
        }

        if (typeof errorCountDisplay !== 'undefined') {
            errorCountDisplay.textContent = activeErrors.length;
        }

        if (activeErrors.length > 0) {
            targetStatus.textContent = "Warning";
            targetStatus.className = 'status-indicator status-error';
        } else {
            targetStatus.innerHTML = currentState;
            targetStatus.className = `status-indicator ${stateClass}`;
        }
    }

    eventSource.onerror = function (err) {
        console.error("SSE connection error:", err);
        targetStatus.textContent = "Disconnected";
        targetStatus.className = 'status-indicator status-offline';
        eventSource.close();
    };

    // --- Metric Polling --- //
    const filesRemainingEl = document.getElementById('files-remaining');

    async function pollTargetMetrics() {
        if (!targetId || targetId === 'global') return;
        try {
            const res = await fetch('/api/targets');
            if (res.ok) {
                const data = await res.json();
                const targetInfo = data.targets.find(t => t.id === targetId);
                if (targetInfo && filesRemainingEl) {
                    // Update the remaining counter
                    filesRemainingEl.textContent = targetInfo.files_remaining || "0";

                    // You could also do something with targetInfo.files_queued depending on UI needs
                    if (targetInfo.files_remaining > 0) {
                        filesRemainingEl.style.color = 'var(--status-warning)';
                    } else {
                        filesRemainingEl.style.color = 'var(--status-online)';
                    }

                    if (missingCountDisplay) {
                        missingCountDisplay.textContent = targetInfo.missing_count || "0";
                        if (targetInfo.missing_count > 0) {
                            missingCountDisplay.style.color = 'var(--status-error)';
                            if (btnSyncMissing) btnSyncMissing.disabled = false;
                        } else {
                            missingCountDisplay.style.color = 'var(--text-muted)';
                            if (btnSyncMissing) btnSyncMissing.disabled = true;
                        }
                    }
                }
            }
        } catch (err) {
            console.error("Failed to poll target metrics:", err);
        }
    }

    if (targetId && targetId !== 'global') {
        pollTargetMetrics();
        setInterval(pollTargetMetrics, 1000);
    }
});
