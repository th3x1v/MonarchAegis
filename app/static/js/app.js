document.addEventListener('DOMContentLoaded', () => {

    // --- Elements --- //
    const targetTbody = document.getElementById('target-tbody');

    // Modal Elements for Source
    const btnAddSource = document.getElementById('btn-add-source');
    const modalAddSource = document.getElementById('add-source-modal');
    const btnCancelSource = document.getElementById('btn-cancel-source');
    const formAddSource = document.getElementById('form-add-source');

    // Modal Elements for Link Client
    const modalLinkClient = document.getElementById('link-client-modal');
    const btnCancelLink = document.getElementById('btn-cancel-link');
    const formLinkClient = document.getElementById('form-link-client');

    // --- Modal Logic --- //
    const openSourceModal = () => modalAddSource.classList.add('active');
    const closeSourceModal = () => {
        modalAddSource.classList.remove('active');
        formAddSource.reset();
    };

    const closeLinkModal = () => {
        modalLinkClient.classList.remove('active');
        formLinkClient.reset();
        linkingSourceId = null;
        linkingSourceName = null;
        linkingSourcePath = null;
        relinkingExistingTargetId = null;
    };

    btnAddSource.addEventListener('click', openSourceModal);
    btnCancelSource.addEventListener('click', closeSourceModal);
    btnCancelLink.addEventListener('click', closeLinkModal);

    // Close modal on outside click
    modalAddSource.addEventListener('click', (e) => {
        if (e.target === modalAddSource) closeSourceModal();
    });
    modalLinkClient.addEventListener('click', (e) => {
        if (e.target === modalLinkClient) closeLinkModal();
    });

    // --- API Calls --- //

    // Fetch and Render Sync Targets, augmented with each target's schedule /
    // last-run from the authoritative DB store (/api/db_targets).
    async function fetchTargets() {
        try {
            const [res, dbRes] = await Promise.all([
                fetch('/api/targets'),
                fetch('/api/db_targets'),
            ]);
            const data = await res.json();
            const dbData = await dbRes.json().catch(() => ({ targets: [] }));
            const dbById = {};
            (dbData.targets || []).forEach(t => { dbById[t.id] = t; });

            // Update Global Status Indicator
            const globalSyncObj = document.getElementById('global-sync-status');
            if (globalSyncObj && data.global_status) {
                globalSyncObj.textContent = data.global_status;
                globalSyncObj.className = data.global_status === "Syncing"
                    ? "status-indicator status-success"
                    : "status-indicator status-online";
            }

            targetTbody.innerHTML = '';

            if (!data.targets || data.targets.length === 0) {
                targetTbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted">No sync targets configured.</td></tr>`;
                return;
            }

            // Store data globally to easily populate edit form later
            window._allTargets = data.targets;

            data.targets.forEach(target => {
                const tr = document.createElement('tr');
                const targetStatStatus = target.status || "Idle";
                let targetStatClass = "status-online";
                if (targetStatStatus === "Syncing") targetStatClass = "status-success";
                else if (targetStatStatus === "Scanning") targetStatClass = "status-warning";
                else if (targetStatStatus === "Pending Scan") targetStatClass = "status-offline";
                else if (targetStatStatus.includes("Security Alert")) targetStatClass = "status-error";

                let actionButtons = '';
                if (targetStatStatus.includes("Security Alert")) {
                    actionButtons = `
                        <button class="btn btn-sm btn-primary" style="background-color: var(--status-error); border-color: var(--status-error);" onclick="trustNewKey('${target.id}')">✅ Trust New Key</button>
                        <button class="btn btn-sm btn-secondary text-error" onclick="deleteTarget('${target.id}')">Delete</button>
                    `;
                } else if (target.is_unpaired) {
                    const scanBtn = targetStatStatus === "Pending Scan"
                        ? `<button class="btn btn-sm btn-secondary" onclick="startBaselineScan('${target.id}')">▶ Start Scan</button>`
                        : '';
                    actionButtons = `
                        <button class="btn btn-sm btn-primary" onclick="linkClient('${target.id}')">🔗 Link Client</button>
                        ${scanBtn}
                        <button class="btn btn-sm btn-secondary text-error" onclick="deleteSource('${target.id}')">Delete</button>
                    `;
                } else {
                    actionButtons = `
                        <button class="btn btn-sm btn-primary" onclick="relinkClient('${target.id}')">🔗 Update Client Link</button>
                        <button class="btn btn-sm" style="background: transparent; color: var(--text-muted); border: none; padding: 0.2rem 0.5rem; font-size: 0.8rem; text-decoration: underline;" onclick="deleteTarget('${target.id}')">Delete</button>
                    `;
                }

                // While hashing, show live progress + throughput (matches the Client UI).
                let scanProgress = '';
                if (target.scan_status === 'scanning') {
                    const done = (target.hashed || 0) + (target.skipped || 0);
                    const rate = target.files_per_sec
                        ? ` — ${target.files_per_sec}/s, ${target.mb_per_sec || 0} MB/s`
                        : '';
                    scanProgress = `<div class="text-muted" style="font-size:0.75rem;margin-top:0.2rem;">${target.hashed || 0} hashed${target.skipped ? `, ${target.skipped} cached` : ''} (${done} files)${rate}</div>`;
                }

                // Schedule / last-run summary from the authoritative DB store.
                const dbt = dbById[target.id];
                let scheduleLine = '';
                if (dbt) {
                    const iv = dbt.interval_seconds || 0;
                    const sched = ({ 0: 'Manual', 3600: 'Hourly', 21600: 'Every 6h', 43200: 'Every 12h', 86400: 'Daily' })[iv]
                        || `${Math.round(iv / 60)}m`;
                    let nextTxt = '';
                    if (dbt.next_run) {
                        nextTxt = (dbt.next_run * 1000 <= Date.now())
                            ? ' · next: due now'
                            : ` · next: ${new Date(dbt.next_run * 1000).toLocaleString()}`;
                    }
                    const lastTxt = dbt.last_status ? ` · last: ${dbt.last_status}` : '';
                    const todayTxt = ` · today: ${dbt.transferred_today || 0}`;
                    scheduleLine = `<div class="text-muted" style="font-size:0.72rem;margin-top:0.2rem;" title="'today' = files transferred since local midnight">⏱ ${sched}${nextTxt}${lastTxt}${todayTxt}</div>`;
                }

                tr.innerHTML = `
                    <td class="font-medium clickable" onclick="window.location.href='/target?id=${target.id}'">${target.name}</td>
                    <td class="text-muted"><code style="font-family: monospace;">${target.source}</code></td>
                    <td class="text-muted"><code style="font-family: monospace;">${target.target}</code></td>
                    <td><span class="status-indicator ${targetStatClass}">${targetStatStatus}</span>${scanProgress}${scheduleLine}</td>
                    <td>${actionButtons}</td>
                `;
                targetTbody.appendChild(tr);
            });

        } catch (error) {
            console.error('Error fetching targets:', error);
            targetTbody.innerHTML = `<tr><td colspan="5" class="text-center text-error">Failed to load configuration.</td></tr>`;
        }
    }

    // Target Management Helpers (Edit & Delete)
    // Target Management Helpers
    let linkingSourceId = null;
    let linkingSourceName = null;
    let linkingSourcePath = null;
    let relinkingExistingTargetId = null; // set when re-linking an already-paired target

    window.linkClient = (id) => {
        const target = window._allTargets.find(t => t.id === id);
        if (!target) return;

        relinkingExistingTargetId = null;
        linkingSourceId = id;
        linkingSourceName = target.name;
        linkingSourcePath = target.source;
        modalLinkClient.classList.add('active');
    };

    window.relinkClient = (id) => {
        const target = window._allTargets.find(t => t.id === id);
        if (!target) return;

        relinkingExistingTargetId = id;
        linkingSourceId = id;
        linkingSourceName = target.name;
        linkingSourcePath = target.source;

        // Pre-fill modal fields from existing target string (user@host:/path)
        const dest = target.target || '';
        const atIdx = dest.indexOf('@');
        const colonIdx = dest.indexOf(':');
        if (atIdx !== -1 && colonIdx !== -1) {
            document.getElementById('target-client-user').value = dest.substring(0, atIdx);
            document.getElementById('target-client-host').value = dest.substring(atIdx + 1, colonIdx);
            document.getElementById('target-client-dest').value = dest.substring(colonIdx + 1);
        }

        modalLinkClient.classList.add('active');
    };

    function showSavedToast() {
        const toast = document.getElementById('save-toast');
        if (toast) {
            toast.style.display = 'block';
            setTimeout(() => { toast.style.display = 'none'; }, 2000);
        }
    }

    window.repairKey = async (id) => {
        if (!confirm('Re-pair SSH keys with the client?\n\nThis will regenerate credentials on the client. Your sync target and hash database will not be affected.')) return;
        try {
            const res = await fetch(`/api/target/${id}/repairkey`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') {
                showSavedToast();
                fetchTargets();
            } else {
                alert(`Re-pair failed: ${data.message}`);
            }
        } catch (error) {
            console.error("Failed to re-pair keys:", error);
            alert("API connection error.");
        }
    };

    window.trustNewKey = async (id) => {
        if (!confirm('WARNING: Only click this if you intentionally reinstalled or changed the remote server. Doing this while under a Man-In-The-Middle attack could expose your files.\\n\\nAre you sure you want to trust the new fingerprint?')) return;
        try {
            const res = await fetch(`/api/target/${id}/trust_new_key`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') {
                showSavedToast();
                fetchTargets();
            } else {
                alert(`Failed to trust key: ${data.message}`);
            }
        } catch (error) {
            console.error("Failed to trust new key:", error);
            alert("API connection error.");
        }
    };

    window.deleteTarget = async (id) => {
        if (!confirm('Are you sure you want to completely remove this sync target?')) return;
        try {
            await fetch(`/api/target/${id}`, { method: 'DELETE' });
            showSavedToast();
            fetchTargets();
        } catch (error) {
            console.error("Failed to delete target:", error);
            alert("API connection error.");
        }
    };

    window.deleteSource = async (id) => {
        if (!confirm('Are you sure you want to delete this unpaired source directory?')) return;
        try {
            await fetch(`/api/source_targets/${id}`, { method: 'DELETE' });
            fetchTargets();
        } catch (error) {
            console.error("Failed to delete source:", error);
            alert("API connection error.");
        }
    };

    // (lsyncd retired in Phase 4: the Restart Daemon button + its handler and the
    //  daemon-health poll were removed; the scheduler drives replication now.)

    // 4. Handle Form Submission (Add Source)
    formAddSource.addEventListener('submit', async (e) => {
        e.preventDefault();

        const submitBtn = formAddSource.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        submitBtn.textContent = 'Saving...';

        try {
            const name = document.getElementById('source-name').value;
            const source = document.getElementById('source-path').value;
            const scanNow = document.getElementById('source-scan-now').checked;

            const payload = { name, source, scan_now: scanNow };

            const res = await fetch('/api/source_targets', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                closeSourceModal();
                fetchTargets();
            } else {
                alert("Failed to add source directory.");
            }
        } catch (error) {
            console.error("Error creating source:", error);
            alert("API connection error.");
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Add Source';
        }
    });

    // 5. Handle Form Submission (Link Client)
    formLinkClient.addEventListener('submit', async (e) => {
        e.preventDefault();

        const submitBtn = formLinkClient.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        submitBtn.textContent = 'Saving...';

        try {
            const clientHost = document.getElementById('target-client-host').value;
            const clientPort = document.getElementById('target-client-port').value || '2222';
            // Optional: blank means "use the default 5000".
            const clientApiPort = document.getElementById('target-client-api-port').value.trim();
            const clientUser = document.getElementById('target-client-user').value || 'root';
            const clientDest = document.getElementById('target-client-dest').value.trim();
            const clientKey = document.getElementById('target-client-key').value;
            
            if (!clientDest) {
                alert("Receiving Path is required to link.");
                submitBtn.disabled = false;
                submitBtn.textContent = 'Save & Start Syncing';
                return;
            }
            
            // Step 1: Save pasted SSH key
            let keyId = null;
            if (clientKey && clientKey.trim().length > 0) {
                const keyRes = await fetch('/api/keys', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: `${linkingSourceName}_key`, private_key: clientKey })
                });
                const keyData = await keyRes.json();
                keyId = keyData.id;
            }
            
            // Step 2: Auto-create a server profile
            if (keyId) {
                await fetch('/api/servers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        alias: linkingSourceName,
                        host: clientHost,
                        user: clientUser,
                        port: parseInt(clientPort),
                        key_id: keyId,
                        // null = not specified -> server falls back to the default
                        api_port: clientApiPort ? parseInt(clientApiPort) : null
                    })
                });
            }
            
            // Step 3: Build destination as user@host:/path
            const dest = `${clientUser}@${clientHost}:${clientDest}`;

            let res;
            if (relinkingExistingTargetId) {
                // Re-linking an already-paired target — update the existing lsyncd config block
                const payload = {
                    name: linkingSourceName,
                    source: linkingSourcePath,
                    target: dest,
                    key_id: keyId    // pin the exact key so same-host targets don't collapse
                };
                res = await fetch(`/api/target/${relinkingExistingTargetId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
            } else {
                // Fresh link from an unpaired source
                const payload = {
                    name: linkingSourceName,
                    source: linkingSourcePath,
                    target: dest,
                    unpaired_id: linkingSourceId,
                    key_id: keyId    // pin the exact key so same-host targets don't collapse
                };
                res = await fetch('/api/target', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
            }

            if (res.ok) {
                relinkingExistingTargetId = null;
                closeLinkModal();
                showSavedToast();
                fetchTargets();
            } else {
                alert("Failed to write target to configuration.");
            }
        } catch (error) {
            console.error("Error creating target:", error);
            alert("API connection error.");
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Save & Start Syncing';
        }
    });

    // --- Servers & Keys Logic --- //
    const modalServers = document.getElementById('servers-modal');
    const btnManageServers = document.getElementById('btn-manage-servers');
    const btnCloseServers = document.getElementById('btn-close-servers-modal');
    const tabServers = document.getElementById('tab-servers');
    const tabKeys = document.getElementById('tab-keys');
    const viewServers = document.getElementById('view-servers');
    const viewKeys = document.getElementById('view-keys');

    btnManageServers.addEventListener('click', () => {
        modalServers.classList.add('active');
        fetchServers();
        fetchKeys();
    });
    btnCloseServers.addEventListener('click', () => modalServers.classList.remove('active'));

    // Factory Reset (Danger Zone)
    const btnFactoryReset = document.getElementById('btn-factory-reset');
    if (btnFactoryReset) {
        btnFactoryReset.addEventListener('click', async () => {
            const wipeData = document.getElementById('factory-reset-wipe-data').checked;
            const msg = wipeData
                ? 'FULL WIPE\n\nThis deletes all SSH keys, authorized_keys, host-key pins, server profiles, the lsyncd config, the hash database, and every registered directory.\n\nYou will have to re-add and re-hash everything. Continue?'
                : 'RESET PAIRING\n\nThis deletes all SSH keys, authorized_keys, host-key pins, and server profiles so you can re-pair cleanly. Your hashes and directories are kept.\n\nContinue?';
            if (!confirm(msg)) return;
            btnFactoryReset.disabled = true;
            btnFactoryReset.textContent = 'Resetting...';
            try {
                const res = await fetch('/api/factory_reset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ wipe_data: wipeData })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    alert('Reset complete. Re-pair each target to reconnect.\n\n' + (data.message || ''));
                    modalServers.classList.remove('active');
                    fetchTargets();
                } else {
                    alert('Reset failed: ' + (data.message || 'unknown error'));
                }
            } catch (e) {
                alert('Reset request failed: ' + e);
            } finally {
                btnFactoryReset.disabled = false;
                btnFactoryReset.textContent = 'Reset Pairing State';
                document.getElementById('factory-reset-wipe-data').checked = false;
            }
        });
    }

    tabServers.addEventListener('click', () => {
        tabServers.className = 'btn btn-primary';
        tabKeys.className = 'btn btn-secondary';
        viewServers.style.display = 'block';
        viewKeys.style.display = 'none';
        document.getElementById('form-add-server').style.display = 'none';
    });
    tabKeys.addEventListener('click', () => {
        tabKeys.className = 'btn btn-primary';
        tabServers.className = 'btn btn-secondary';
        viewKeys.style.display = 'block';
        viewServers.style.display = 'none';
        document.getElementById('form-add-key').style.display = 'none';
    });

    // Keys Logic
    const btnShowAddKey = document.getElementById('btn-show-add-key');
    const formAddKey = document.getElementById('form-add-key');
    btnShowAddKey.addEventListener('click', () => { formAddKey.style.display = 'block'; });
    document.getElementById('btn-cancel-add-key').addEventListener('click', () => { formAddKey.style.display = 'none'; formAddKey.reset(); });

    async function fetchKeys() {
        const res = await fetch('/api/keys');
        const data = await res.json();
        const list = document.getElementById('keys-list');
        const select = document.getElementById('server-key');
        list.innerHTML = '';
        select.innerHTML = '<option value="">Select a saved key...</option>';
        data.keys.forEach(k => {
            list.innerHTML += `<div class="flex-between" style="padding: 0.5rem; border-bottom: 1px solid var(--border-color);">
                <span>🔑 ${k.name}</span>
                <button class="btn btn-sm btn-secondary text-error" onclick="deleteKey('${k.id}')">Delete</button>
            </div>`;
            select.innerHTML += `<option value="${k.id}">${k.name}</option>`;
        });
        if (data.keys.length === 0) list.innerHTML = '<div class="text-muted">No keys saved.</div>';
    }

    formAddKey.addEventListener('submit', async (e) => {
        e.preventDefault();
        const payload = {
            name: document.getElementById('key-name').value,
            private_key: document.getElementById('key-private').value
        };
        await fetch('/api/keys', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        formAddKey.reset();
        formAddKey.style.display = 'none';
        fetchKeys();
    });

    window.deleteKey = async (id) => {
        if (confirm('Delete this key?')) {
            try {
                const res = await fetch(`/api/keys/${id}`, { method: 'DELETE' });
                const json = await res.json();

                if (json.status === 'error') {
                    alert(json.message || 'Failed to delete key.');
                } else {
                    fetchKeys();
                }
            } catch (error) {
                console.error("Error deleting key:", error);
                alert("API connection error.");
            }
        }
    };

    // Servers Logic
    const btnShowAddServer = document.getElementById('btn-show-add-server');
    const formAddServer = document.getElementById('form-add-server');
    btnShowAddServer.addEventListener('click', () => { formAddServer.style.display = 'block'; });
    document.getElementById('btn-cancel-add-server').addEventListener('click', () => { formAddServer.style.display = 'none'; formAddServer.reset(); });

    let savedServers = [];
    async function fetchServers() {
        const res = await fetch('/api/servers');
        const data = await res.json();
        savedServers = data.servers || [];
        const list = document.getElementById('servers-list');
        const fbSelect = document.getElementById('fb-server-select');

        list.innerHTML = '';
        fbSelect.innerHTML = '<option value="">Select Server...</option>';

        savedServers.forEach(s => {
            list.innerHTML += `<div class="flex-between" style="padding: 0.5rem; border-bottom: 1px solid var(--border-color);">
                <div><strong>${s.alias}</strong> <span class="text-muted">(${s.user}@${s.host}:${s.port})</span></div>
                <button class="btn btn-sm btn-secondary text-error" onclick="deleteServer('${s.id}')">Delete</button>
            </div>`;
            fbSelect.innerHTML += `<option value="${s.id}">${s.alias} (${s.host})</option>`;
        });
        if (savedServers.length === 0) list.innerHTML = '<div class="text-muted">No servers saved.</div>';
    }

    formAddServer.addEventListener('submit', async (e) => {
        e.preventDefault();
        const payload = {
            alias: document.getElementById('server-alias').value,
            host: document.getElementById('server-host').value,
            port: parseInt(document.getElementById('server-port').value, 10),
            user: document.getElementById('server-user').value,
            key_id: document.getElementById('server-key').value
        };
        await fetch('/api/servers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        formAddServer.reset();
        formAddServer.style.display = 'none';
        fetchServers();
    });

    window.deleteServer = async (id) => {
        if (confirm('Delete this server?')) {
            await fetch(`/api/servers/${id}`, { method: 'DELETE' });
            fetchServers();
        }
    };

    // --- File Browser Logic --- //
    const modalFB = document.getElementById('file-browser-modal');
    let fbActiveInputId = null;
    let fbMode = 'local'; // 'local' or 'remote'

    window.openFileBrowser = (mode, inputId) => {
        fbMode = mode;
        fbActiveInputId = inputId;
        
        const isLocalRoot = mode === 'local-root';
        const displayMode = isLocalRoot ? 'local' : mode;
        
        document.getElementById('fb-title').innerText = displayMode === 'local' ? 'Browse Container Filesystem' : 'Browse Remote Server';
        document.getElementById('fb-remote-controls').style.display = mode === 'remote' ? 'flex' : 'none';
        
        const startPath = isLocalRoot ? '/' : (displayMode === 'local' ? '/source_data' : '/');
        document.getElementById('fb-current-path').value = startPath;
        document.getElementById('fb-list').innerHTML = '';
        document.getElementById('fb-error').style.display = 'none';

        fetchServers();

        if (displayMode === 'local') {
            loadDirectory(startPath);
        } else {
            document.getElementById('fb-server-select').value = '';
            document.getElementById('fb-list').innerHTML = '<div class="text-muted" style="padding: 2rem; text-align: center;">Select a server to browse...</div>';
        }

        modalFB.classList.add('active');
    };

    document.getElementById('btn-fb-cancel').addEventListener('click', () => modalFB.classList.remove('active'));

    document.getElementById('fb-server-select').addEventListener('change', (e) => {
        if (e.target.value) {
            document.getElementById('fb-current-path').value = '/';
            loadDirectory('/');
        }
    });

    document.getElementById('fb-up-btn').addEventListener('click', () => {
        let current = document.getElementById('fb-current-path').value;
        if (current === '/' || current.length <= 1) return;
        let parts = current.replace(/\/$/, '').split('/');
        parts.pop();
        let parent = parts.join('/') || '/';
        const isLocalRoot = fbMode === 'local-root';
        if (fbMode === 'local' && !isLocalRoot && !parent.startsWith('/source_data')) parent = '/source_data';

        document.getElementById('fb-current-path').value = parent;
        loadDirectory(parent);
    });

    async function loadDirectory(path) {
        document.getElementById('fb-loading').style.display = 'block';
        document.getElementById('fb-list').style.display = 'none';
        document.getElementById('fb-error').style.display = 'none';

        let url = `/api/fs/browse/local?path=${encodeURIComponent(path)}`;
        if (fbMode === 'remote') {
            const srvId = document.getElementById('fb-server-select').value;
            if (!srvId) {
                document.getElementById('fb-loading').style.display = 'none';
                return;
            }
            url = `/api/fs/browse/remote?server_id=${srvId}&path=${encodeURIComponent(path)}`;
        }

        try {
            const res = await fetch(url);
            const data = await res.json();

            if (data.status === 'error') {
                document.getElementById('fb-error').innerText = data.message;
                document.getElementById('fb-error').style.display = 'block';
            } else {
                renderDirectory(data.items, path);
            }
        } catch (e) {
            document.getElementById('fb-error').innerText = "Network Error";
            document.getElementById('fb-error').style.display = 'block';
        }

        document.getElementById('fb-loading').style.display = 'none';
        document.getElementById('fb-list').style.display = 'block';
    }

    function renderDirectory(items, currentPath) {
        const list = document.getElementById('fb-list');
        list.innerHTML = '';
        if (items.length === 0) {
            list.innerHTML = '<div class="text-muted" style="padding: 1rem;">Directory is empty</div>';
            return;
        }

        items.forEach(item => {
            const div = document.createElement('div');
            div.className = 'fb-item';
            div.style.cssText = 'padding: 0.5rem 1rem; cursor: pointer; border-bottom: 1px solid var(--border-color); display: flex; align-items: center; gap: 0.5rem;';

            const icon = item.type === 'directory' ? '📁' : '📄';
            div.innerHTML = `<span>${icon}</span> <span>${item.name}</span>`;

            div.onmouseover = () => div.style.background = 'var(--bg-color)';
            div.onmouseout = () => div.style.background = 'transparent';

            div.onclick = () => {
                document.getElementById('fb-current-path').value = item.path;
                if (item.type === 'directory') {
                    loadDirectory(item.path);
                }
            };
            list.appendChild(div);
        });
    }

    document.getElementById('btn-fb-select').addEventListener('click', () => {
        let path = document.getElementById('fb-current-path').value;
        if (fbMode === 'remote') {
            const srvId = document.getElementById('fb-server-select').value;
            const server = savedServers.find(s => s.id === srvId);
            if (server) {
                let portStr = server.port !== 22 ? ` -e "ssh -p ${server.port}"` : '';
                path = `${server.user}@${server.host}:${path}`;
            }
        }
        document.getElementById(fbActiveInputId).value = path;
        modalFB.classList.remove('active');
    });

    async function initRoleToggle() {
        try {
            const res = await fetch('/api/config/role');
            const data = await res.json();
            const isClient = data.role === 'client';
            
            const roleToggle = document.getElementById('role-toggle');
            if (roleToggle) {
                roleToggle.value = data.role;
                roleToggle.addEventListener('change', async (e) => {
                    const newRole = e.target.value;
                    roleToggle.disabled = true;
                    try {
                        const postRes = await fetch('/api/config/role', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ role: newRole })
                        });
                        if (postRes.ok) {
                            window.location.reload();
                        } else {
                            throw new Error("API rejected role switch");
                        }
                    } catch (err) {
                        console.error(err);
                        alert("Failed to switch Container Role.");
                        e.target.value = data.role;
                        roleToggle.disabled = false;
                    }
                });
            }

            document.getElementById('source-view').style.display = isClient ? 'none' : 'block';
            document.getElementById('client-view').style.display = isClient ? 'block' : 'none';

            if (isClient) {
                // Adjust header for Client Mode branding
                const brand = document.querySelector('.brand');
                brand.innerHTML = brand.innerHTML.replace('Controller', 'Client Node');
                
                // Start Client-specific polling
                fetchClientTargets();
                pollScanStatus();
                setInterval(fetchClientTargets, 10000);
                setInterval(pollScanStatus, 3000);
            }
        } catch (e) {
            console.error("Failed to fetch role", e);
        }
    }

    // --- Client Mode Functions --- //
    
    async function fetchClientTargets() {
        try {
            const res = await fetch('/api/client/targets');
            const data = await res.json();
            const tbody = document.getElementById('client-targets-tbody');
            if (!tbody) return;
            
            tbody.innerHTML = '';
            if (!data.targets || data.targets.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="text-muted" style="text-align:center;">No receiving directories configured. Click "+ Add Directory" to get started.</td></tr>';
                return;
            }
            
            const scanData = window._lastScanStatus || {};
            
            data.targets.forEach(t => {
                const scan = scanData[t.id] || {};
                const status = scan.status || 'pending';
                const statusClass = status === 'complete' ? 'status-online' : status === 'scanning' ? 'status-warning' : 'status-offline';
                const statusLabel = status === 'pending' ? 'NOT SCANNED' : status.toUpperCase();
                const rate = (status === 'scanning' && scan.files_per_sec)
                    ? ` — ${scan.files_per_sec}/s, ${scan.mb_per_sec || 0} MB/s`
                    : '';
                // Live ledger count — includes files received from a Source, which
                // never touch the scanner's baseline counters. While a scan is
                // actually running we still show its live progress instead.
                const tracked = (typeof t.tracked === 'number')
                    ? `${t.tracked.toLocaleString()} file${t.tracked === 1 ? '' : 's'} tracked`
                    : null;
                const scanCounters = scan.total
                    ? `${scan.hashed || 0} hashed, ${scan.skipped || 0} cached (${scan.total} total)${rate}`
                    : null;
                const progress = status === 'pending'
                    ? 'Baseline deferred — start when ready'
                    : status === 'scanning'
                        ? (scanCounters || 'Scanning…')
                        : (tracked || scanCounters || '—');

                // Offer a "Start Scan" button only while the baseline is still deferred.
                const scanBtn = status === 'pending'
                    ? `<button class="btn btn-sm btn-primary" onclick="startBaselineScan('${t.id}')">▶ Start Scan</button>`
                    : '';

                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td class="font-medium">${t.alias}</td>
                    <td class="text-muted"><code>${t.path}</code></td>
                    <td><span class="status-indicator ${statusClass}">${statusLabel}</span></td>
                    <td class="text-muted">${progress}</td>
                    <td style="display:flex;gap:0.5rem;">
                        ${scanBtn}
                        <button class="btn btn-sm btn-jewel" onclick="generateKeyForTarget('${t.id}', '${t.path}')">🔑 Generate Key</button>
                        <button class="btn btn-sm btn-secondary text-error" onclick="deleteClientTarget('${t.id}')">Remove</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        } catch (e) {
            console.error("Failed to fetch client targets:", e);
        }
    }
    
    window.addClientTarget = async function() {
        const alias = document.getElementById('client-target-alias').value.trim();
        const path = document.getElementById('client-target-path').value.trim();
        const scanNow = document.getElementById('client-target-scan-now').checked;
        if (!alias || !path) { alert("Both fields are required."); return; }

        try {
            const res = await fetch('/api/client/targets', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ alias, path, scan_now: scanNow })
            });
            if (res.ok) {
                document.getElementById('client-add-form').style.display = 'none';
                document.getElementById('client-target-alias').value = '';
                document.getElementById('client-target-path').value = '';
                document.getElementById('client-target-scan-now').checked = false;
                fetchClientTargets();
            }
        } catch (e) {
            alert("Failed to add target.");
        }
    };

    window.startBaselineScan = async function(id) {
        if (!confirm("Start the baseline hash scan now?\n\nThis reads every file in the directory once. On large directories it can take a long time and put sustained load on the disks.")) return;
        try {
            const res = await fetch(`/api/target/${id}/rescan`, { method: 'POST' });
            const data = await res.json();
            if (data.status !== 'success') {
                alert(data.message || 'Failed to start scan.');
            }
            fetchClientTargets();
            fetchTargets();
        } catch (e) {
            alert('Failed to start scan.');
        }
    };
    
    window.deleteClientTarget = async function(id) {
        if (!confirm("Remove this receiving directory?")) return;
        try {
            await fetch(`/api/client/targets/${id}`, { method: 'DELETE' });
            fetchClientTargets();
        } catch (e) {
            alert("Failed to remove target.");
        }
    };
    
    window.generateKeyForTarget = async function(targetId, targetPath) {
        try {
            const res = await fetch('/api/client/pair', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_id: targetId, target_path: targetPath })
            });
            const data = await res.json();
            if (data.status === 'success' && data.private_key) {
                document.getElementById('ssh-key-output').value = data.private_key;
                document.getElementById('ssh-key-modal').classList.add('active');
            } else {
                alert(data.message || 'Failed to generate key.');
            }
        } catch (e) {
            alert('Failed to generate SSH key.');
        }
    };
    
    window.copySSHKey = function() {
        const textarea = document.getElementById('ssh-key-output');
        textarea.select();
        document.execCommand('copy');
        alert('Private key copied to clipboard!');
    };
    
    async function pollScanStatus() {
        try {
            const res = await fetch('/api/scan/status');
            const data = await res.json();
            window._lastScanStatus = data.targets || {};
            
            const banner = document.getElementById('scan-readiness-text');
            if (banner) {
                if (data.ready) {
                    banner.innerHTML = '✅ <strong>All directories fully hashed.</strong> This Client is ready to accept Source connections.';
                    document.getElementById('scan-readiness-banner').style.borderColor = 'var(--status-success)';
                } else {
                    const targetKeys = Object.keys(data.targets || {});
                    if (targetKeys.length === 0) {
                        banner.innerHTML = '⏳ No directories configured yet. Add a receiving directory above to begin.';
                    } else {
                        banner.innerHTML = '⏳ <strong>Baseline scan in progress...</strong> Transfers are blocked until hashing completes.';
                        document.getElementById('scan-readiness-banner').style.borderColor = 'var(--status-warning)';
                    }
                }
            }
            
            // Re-render table with fresh scan data
            fetchClientTargets();
        } catch (e) {
            console.error("Failed to poll scan status:", e);
        }
    }

    // --- On Initialization --- //
    initRoleToggle();
    fetchTargets();
    fetchServers();

    // Poll active target sync statuses + schedule.
    setInterval(fetchTargets, 5000);
});
