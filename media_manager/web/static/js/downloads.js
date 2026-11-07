// SABnzbd & Syncthing Download Queue & History JS
window.loadDownloads = async function() {
    const container = document.getElementById('downloads-container');
    if (!container) return;

    try {
        // Only show spinner on initial load to prevent flicker during auto-refresh
        if (!container.querySelector('div[style*="grid"]')) {
            container.innerHTML = `<div class="loading-spinner">Loading download status...</div>`;
        }
        
        const daysParam = window._historyRangeDays === 'all' ? 365 : window._historyRangeDays;

        // Fetch queue, history, syncthing, and the Radarr/Sonarr download queue in parallel
        const [queueRes, historyRes, syncthingData, syncthingHistory, arrQueueRes] = await Promise.all([
            fetch('/api/v1/integrations/sabnzbd/queue').then(r => r.json()).catch(() => ({ warning: "SABnzbd error" })),
            fetch('/api/v1/integrations/sabnzbd/history').then(r => r.json()).catch(() => ({ slots: [], stats: {} })),
            fetch('/api/v1/integrations/syncthing/downloads').then(r => r.json()).catch(() => ({ enabled: false })),
            fetch(`/api/v1/integrations/syncthing/history?days=${daysParam}`).then(r => r.json()).catch(() => ({ history: [] })),
            fetch('/api/v1/integrations/download-queue').then(r => r.json()).catch(() => ({ count: 0, queue: [] }))
        ]);

        if (queueRes.warning && (!syncthingData || !syncthingData.enabled) && (arrQueueRes.queue || []).length === 0) {
            container.innerHTML = `<p class="muted">${queueRes.warning}</p>`;
            return;
        }

        renderDashboard(queueRes, historyRes, syncthingData, syncthingHistory, arrQueueRes, container);

        if (window.downloadsPollTimeout) clearTimeout(window.downloadsPollTimeout);

        // Smart Polling: If there are active downloads (SABnzbd, Syncthing, or the
        // Radarr/Sonarr queue), poll every 3 seconds while view is active
        const queue = queueRes.queue || [];
        const isSyncthingActive = syncthingData && syncthingData.enabled && (syncthingData.folders || []).some(f => f.state === 'syncing' || f.need_bytes > 0);
        const isArrQueueActive = (arrQueueRes.queue || []).length > 0;

        if (queue.length > 0 || isSyncthingActive || isArrQueueActive) {
            window.downloadsPollTimeout = setTimeout(() => {
                const view = document.getElementById('view-downloads');
                if (view && view.classList.contains('active') && document.visibilityState === 'visible') {
                    window.loadDownloads();
                }
            }, 3000);
        } else {
            // Idle: cheaply poll just the queue every 20s so a newly-started
            // download shows up without waiting on the 15-minute full refresh
            window.downloadsPollTimeout = setTimeout(window.checkQueueForActivity, 20000);
        }
    } catch (err) {
        container.innerHTML = `<p class="error-msg">Failed to load downloads status: ${escapeHtml(err.message)}</p>`;
    }
};

window.checkQueueForActivity = async function() {
    const view = document.getElementById('view-downloads');
    if (!view || !view.classList.contains('active') || document.visibilityState !== 'visible') return;

    try {
        const [sabRes, syncRes] = await Promise.all([
            fetch('/api/v1/integrations/sabnzbd/queue').then(r => r.json()).catch(() => ({ queue: [] })),
            fetch('/api/v1/integrations/syncthing/downloads').then(r => r.json()).catch(() => ({ folders: [] }))
        ]);
        const hasSabActivity = (sabRes.queue || []).length > 0;
        const hasSyncActivity = (syncRes.folders || []).some(f => f.state === 'syncing' || f.need_bytes > 0);
        if (hasSabActivity || hasSyncActivity) {
            // Something started downloading - do a full reload, which will
            // then take over with its own 3s fast-poll while active.
            window.loadDownloads();
        } else {
            window.downloadsPollTimeout = setTimeout(window.checkQueueForActivity, 20000);
        }
    } catch (err) {
        window.downloadsPollTimeout = setTimeout(window.checkQueueForActivity, 20000);
    }
};

// Selected Download History range in days, or 'all'. Persists across refreshes.
window._historyRangeDays = window._historyRangeDays || 7;

window.setHistoryRangeDays = function(value) {
    window._historyRangeDays = value === 'all' ? 'all' : parseInt(value, 10);
    window.loadDownloads();
};

function formatRelativeTime(unixTimestamp) {
    const diff = Math.floor(Date.now() / 1000 - unixTimestamp);
    if (diff < 60) return 'Just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 172800) return 'Yesterday';
    return new Date(unixTimestamp * 1000).toLocaleDateString(undefined, { 
        month: 'short', 
        day: 'numeric', 
        hour: '2-digit', 
        minute: '2-digit' 
    });
}

function renderHistoryList() {
    const listEl = document.getElementById('history-list');
    const badgesEl = document.getElementById('history-badges');
    if (!listEl) return;

    const allSlots = [
        ...(window._historySlots || []),
        ...(window._syncthingHistorySlots || [])
    ];
    
    // Sort all combined slots by completed time descending
    allSlots.sort((a, b) => (b.completed || 0) - (a.completed || 0));

    const days = window._historyRangeDays;
    const cutoff = days === 'all' ? 0 : (Date.now() / 1000) - (days * 86400);
    const slots = allSlots.filter(s => (s.completed || 0) >= cutoff);

    if (badgesEl) {
        const completedCount = slots.filter(s => s.status === 'Completed').length;
        const failedCount = slots.filter(s => s.status === 'Failed').length;
        badgesEl.innerHTML = `
            <span style="color: var(--success); font-weight: 600;">✔️ ${completedCount}</span>
            <span style="color: var(--danger); font-weight: 600;">❌ ${failedCount}</span>
        `;
    }

    listEl.innerHTML = slots.length === 0 ? `
        <div style="text-align: center; padding: 2.5rem 1rem; color: var(--text-secondary); font-size: 0.9rem;">
            <div>No completed downloads in this range</div>
        </div>
    ` : `
        <div style="max-height: 400px; overflow-y: auto; padding-right: 4px;">
            ${slots.map(s => {
                const timeStr = formatRelativeTime(s.completed);
                const fullTimeStr = new Date(s.completed * 1000).toLocaleString();
                const isFailed = s.status === 'Failed';
                const badgeColor = isFailed ? 'var(--danger)' : 'var(--success)';
                const sourceBadge = s.source === 'syncthing' 
                    ? `<span style="display: inline-block; font-size: 0.65rem; color: #fff; background: var(--accent-color); padding: 1px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase; margin-right: 6px;">Syncthing</span>`
                    : `<span style="display: inline-block; font-size: 0.65rem; color: #fff; background: var(--badge-4k); padding: 1px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase; margin-right: 6px;">Usenet</span>`;

                return `
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; padding: 0.65rem 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.8rem;">
                        <div style="flex: 1; min-width: 0; padding-right: 0.75rem;">
                            <div style="font-weight: 500; color: var(--text-primary); text-overflow: ellipsis; overflow: hidden; white-space: nowrap; word-break: break-all;" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</div>
                            <div style="font-size: 0.7rem; color: var(--text-secondary); margin-top: 2px; display: flex; align-items: center; flex-wrap: wrap; gap: 4px;">
                                ${sourceBadge}
                                <span title="${fullTimeStr}">${timeStr}</span> | <span>Category: ${escapeHtml(s.category || 'Default')}</span>
                            </div>
                        </div>
                        <div style="text-align: right; white-space: nowrap;">
                            <div style="font-weight: 600; color: var(--text-primary);">${s.size}</div>
                            <span style="display: inline-block; font-size: 0.65rem; color: #fff; background: ${badgeColor}; padding: 1px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase; margin-top: 2px;">${s.status}</span>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderDashboard(queueData, historyData, syncthingData, syncthingHistory, arrQueueData, container) {
    const queue = queueData.queue || [];
    const arrQueue = (arrQueueData && arrQueueData.queue) || [];
    window._historySlots = historyData.slots || [];
    
    // Map Syncthing history to history slots structure
    window._syncthingHistorySlots = (syncthingHistory.history || []).map(h => ({
        nzo_id: `syncthing-${h.id}`,
        name: h.filename,
        size: '',
        status: h.status,
        category: h.folder,
        completed: Math.floor(new Date(h.time).getTime() / 1000),
        source: 'syncthing'
    }));

    const stats = historyData.stats || {
        "7d": { "count": 0, "size_gb": 0 },
        "15d": { "count": 0, "size_gb": 0 },
        "30d": { "count": 0, "size_gb": 0 }
    };

    const hasSAB = !queueData.warning;
    const hasSync = syncthingData && syncthingData.enabled;

    let columnsHtml = '';
    
    // SABnzbd Active Downloads
    if (hasSAB) {
        columnsHtml += `
            <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; box-shadow: var(--card-shadow);">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 0.75rem; margin-bottom: 1rem;">
                    <h3 style="font-size: 1rem; font-weight: 600; margin: 0;">⚡ SABnzbd Queue</h3>
                    <span style="font-size: 0.8rem; background: var(--accent-light); color: var(--accent-color); padding: 2px 8px; border-radius: 20px; font-weight: 600;">Speed: ${queueData.speed || '0 B/s'}</span>
                </div>
                
                ${queue.length === 0 ? `
                    <div style="text-align: center; padding: 2.5rem 1rem; color: var(--text-secondary); font-size: 0.9rem;">
                        <div>📭 Queue is empty</div>
                    </div>
                ` : `
                    <div style="margin-bottom: 0.75rem; font-size: 0.85rem; color: var(--text-secondary);">Size remaining: <b>${queueData.size_remaining || '0 MB'}</b></div>
                    ${queue.map(item => `
                        <div style="background: rgba(0,0,0,0.15); border: 1px solid var(--border-color); padding: 0.75rem; border-radius: var(--radius-md); margin-bottom: 0.5rem;">
                            <div style="font-weight: 600; font-size: 0.85rem; word-break: break-all;">${escapeHtml(item.filename)}</div>
                            <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.5rem; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 0.5rem;">
                                <span>Progress: <b>${item.percentage}%</b> (${item.size})</span>
                                <span>Timeleft: <b>${item.timeleft}</b></span>
                            </div>
                        </div>
                    `).join('')}
                `}
            </div>
        `;
    } else {
        columnsHtml += `
            <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; box-shadow: var(--card-shadow); text-align: center; display: flex; align-items: center; justify-content: center;">
                <p class="muted" style="margin: 0;">SABnzbd not configured or unreachable.</p>
            </div>
        `;
    }

    // Queue actions (remove/retry/search) are admin-only server-side.
    const isQueueAdmin = !!(window.currentUser && window.currentUser.role === 'admin');
    // Radarr / Sonarr Download Queue (grabbed releases downloading/importing -
    // distinct from the raw SABnzbd NZB queue above, tracks per-movie/episode
    // status and import progress rather than the download job itself)
    columnsHtml += `
        <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; box-shadow: var(--card-shadow);">
            <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 0.75rem; margin-bottom: 1rem;">
                <h3 style="font-size: 1rem; font-weight: 600; margin: 0;">🎬 Radarr / Sonarr Queue</h3>
                <span style="font-size: 0.8rem; background: var(--accent-light); color: var(--accent-color); padding: 2px 8px; border-radius: 20px; font-weight: 600;">${arrQueue.length} item${arrQueue.length === 1 ? '' : 's'}</span>
            </div>

            ${arrQueue.length === 0 ? `
                <div style="text-align: center; padding: 2.5rem 1rem; color: var(--text-secondary); font-size: 0.9rem;">
                    <div>📭 Nothing grabbed right now</div>
                </div>
            ` : arrQueue.map(item => `
                <div style="background: rgba(0,0,0,0.15); border: 1px solid var(--border-color); padding: 0.75rem; border-radius: var(--radius-md); margin-bottom: 0.5rem;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 6px;">
                        <span style="font-weight: 600; font-size: 0.85rem; word-break: break-all;">${item.type === 'movie' ? '🎬' : '📺'} ${escapeHtml(item.title)}</span>
                        <span style="font-size: 0.65rem; color: #fff; background: ${item.error_message ? 'var(--danger)' : 'var(--accent-color)'}; padding: 1px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase; white-space: nowrap;">${escapeHtml(item.status || 'unknown')}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.5rem; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 0.5rem; flex-wrap: wrap; gap: 4px;">
                        <span>Progress: <b>${item.progress_pct}%</b>${item.quality ? ` &nbsp;·&nbsp; ${escapeHtml(item.quality)}` : ''}</span>
                        <span>${item.timeleft ? `Timeleft: <b>${item.timeleft}</b>` : ''}</span>
                    </div>
                    ${item.error_message ? `<div style="font-size: 0.72rem; color: var(--danger); margin-top: 0.4rem;">${escapeHtml(item.error_message)}</div>` : ''}
                    ${isQueueAdmin ? `<div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 0.45rem;">
                        <button class="btn btn-secondary btn-sm" style="font-size: 0.7rem; padding: 2px 8px;" onclick="window.arrQueueAction('remove', '${item.type === 'tv' ? 'tv' : 'movie'}', ${Number(item.id)}, ${Number(item.movie_id) || 'null'}, ${Number(item.series_id) || 'null'}, ${Number(item.episode_id) || 'null'})">Remove</button>
                        ${(item.error_message || (item.tracked_status && String(item.tracked_status).toLowerCase() === 'warning')) ? `
                        <button class="btn btn-secondary btn-sm" style="font-size: 0.7rem; padding: 2px 8px;" onclick="window.arrQueueAction('retry', '${item.type === 'tv' ? 'tv' : 'movie'}', ${Number(item.id)}, ${Number(item.movie_id) || 'null'}, ${Number(item.series_id) || 'null'}, ${Number(item.episode_id) || 'null'})">Retry</button>
                        ` : ''}
                        <button class="btn btn-secondary btn-sm" style="font-size: 0.7rem; padding: 2px 8px;" onclick="window.arrQueueAction('search', '${item.type === 'tv' ? 'tv' : 'movie'}', ${Number(item.id)}, ${Number(item.movie_id) || 'null'}, ${Number(item.series_id) || 'null'}, ${Number(item.episode_id) || 'null'})">Search now</button>
                    </div>` : ''}
                </div>
            `).join('')}
        </div>
    `;

    // Syncthing Active Folders
    if (hasSync) {
        columnsHtml += `
            <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; box-shadow: var(--card-shadow); display: flex; flex-direction: column;">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 0.75rem; margin-bottom: 1rem;">
                    <h3 style="font-size: 1rem; font-weight: 600; margin: 0;">🔄 Syncthing Status</h3>
                    <span style="font-size: 0.8rem; background: var(--accent-light); color: var(--accent-color); padding: 2px 8px; border-radius: 20px; font-weight: 600;">Remaining: ${formatGB(syncthingData.total_need_bytes)} GB</span>
                </div>
                <div style="display: flex; flex-direction: column; gap: 0.85rem; flex: 1; max-height: 400px; overflow-y: auto; padding-right: 4px;">
                    ${syncthingData.folders.length === 0 ? `
                        <div style="text-align: center; padding: 2.5rem 1rem; color: var(--text-secondary); font-size: 0.9rem;">
                            <div>No folders configured in Syncthing</div>
                        </div>
                    ` : syncthingData.folders.map(f => {
                        const isSyncing = f.state === 'syncing';
                        const badgeColor = isSyncing ? 'var(--accent-color)' : (f.state === 'idle' ? 'var(--success)' : 'var(--text-secondary)');
                        
                        return `
                            <div style="background: rgba(0,0,0,0.15); border: 1px solid var(--border-color); padding: 0.75rem; border-radius: var(--radius-md);">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.4rem;">
                                    <span style="font-weight: 600; font-size: 0.85rem; word-break: break-all;" title="${escapeHtml(f.path)}">${escapeHtml(f.label)}</span>
                                    <span style="font-size: 0.65rem; color: #fff; background: ${badgeColor}; padding: 1px 6px; border-radius: 4px; font-weight: 600; text-transform: uppercase;">${escapeHtml(f.state)}</span>
                                </div>
                                <div style="font-size: 0.75rem; color: var(--text-secondary); display: flex; justify-content: space-between; align-items: center;">
                                    <span>Sync Progress: <b>${f.completion}%</b></span>
                                    <span>${f.need_bytes > 0 ? (f.need_bytes / (1024**2)).toFixed(1) + ' MB left' : 'Up to date'}</span>
                                </div>
                                <div style="width: 100%; height: 6px; background: rgba(255,255,255,0.08); border-radius: 3px; overflow: hidden; margin-top: 0.4rem;">
                                    <div style="width: ${f.completion}%; height: 100%; background: linear-gradient(90deg, var(--accent-color), var(--badge-4k)); border-radius: 3px;"></div>
                                </div>
                                ${f.needed_files && f.needed_files.length > 0 ? `
                                    <div style="margin-top: 0.5rem; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 0.4rem;">
                                        <span style="font-size: 0.7rem; color: var(--text-secondary); font-weight: 600;">Files in progress:</span>
                                        <div style="display: flex; flex-direction: column; gap: 0.25rem; margin-top: 0.25rem;">
                                            ${f.needed_files.slice(0, 3).map(file => `
                                                <div style="font-size: 0.7rem; text-overflow: ellipsis; overflow: hidden; white-space: nowrap; color: var(--text-primary);" title="${escapeHtml(file.name)}">
                                                    • ${escapeHtml(file.name)}
                                                </div>
                                            `).join('')}
                                            ${f.needed_files.length > 3 ? `
                                                <div style="font-size: 0.65rem; color: var(--text-secondary); font-style: italic;">
                                                    ...and ${f.needed_files.length - 3} more files
                                                </div>
                                            ` : ''}
                                        </div>
                                    </div>
                                ` : ''}
                            </div>
                        `;
                    }).join('')}
                </div>
            </div>
        `;
    }

    // History Section
    columnsHtml += `
        <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; box-shadow: var(--card-shadow);">
            <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; gap: 8px;">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <div>
                        <h3 style="font-size: 1rem; font-weight: 600; margin: 0;">📜 Download History</h3>
                        <span style="font-size: 0.7rem; color: var(--text-secondary);">Last Refreshed: ${new Date().toLocaleTimeString()}</span>
                    </div>
                    <button onclick="window.loadDownloads()" onmouseover="this.style.background='rgba(255,255,255,0.1)'" onmouseout="this.style.background='transparent'" style="padding: 4px 8px; font-size: 0.7rem; display: flex; align-items: center; gap: 4px; border-radius: 4px; border: 1px solid var(--border-color); background: transparent; cursor: pointer; color: var(--text-primary); transition: all 0.2s; outline: none;">
                        🔄 Refresh
                    </button>
                </div>
                <div style="display: flex; align-items: center; gap: 10px;">
                    <select onchange="window.setHistoryRangeDays(this.value)" style="font-size: 0.75rem; padding: 3px 6px; border-radius: 4px; border: 1px solid var(--border-color); background: var(--bg-secondary); color: var(--text-primary);">
                        <option value="3" ${window._historyRangeDays === 3 ? 'selected' : ''}>Last 3 days</option>
                        <option value="7" ${window._historyRangeDays === 7 ? 'selected' : ''}>Last 7 days</option>
                        <option value="15" ${window._historyRangeDays === 15 ? 'selected' : ''}>Last 15 days</option>
                        <option value="30" ${window._historyRangeDays === 30 ? 'selected' : ''}>Last 30 days</option>
                        <option value="all" ${window._historyRangeDays === 'all' ? 'selected' : ''}>All time</option>
                    </select>
                    <div id="history-badges" style="display: flex; gap: 8px; font-size: 0.75rem;"></div>
                </div>
            </div>

            <div id="history-list"></div>
        </div>
    `;

    container.innerHTML = `
        <!-- History Stats Cards -->
        <div class="downloads-stats-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 1.5rem;">
            <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; display: flex; flex-direction: column; justify-content: space-between; box-shadow: var(--card-shadow);">
                <div style="font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600; letter-spacing: 0.05em;">Last 7 Days</div>
                <div style="font-size: 1.75rem; font-weight: 700; margin: 0.5rem 0; color: var(--accent-color);">${stats["7d"].size_gb} <span style="font-size: 1rem; font-weight: 500;">GB</span></div>
                <div style="font-size: 0.8rem; color: var(--text-secondary);">${stats["7d"].count} items downloaded</div>
            </div>
            <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; display: flex; flex-direction: column; justify-content: space-between; box-shadow: var(--card-shadow);">
                <div style="font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600; letter-spacing: 0.05em;">Last 15 Days</div>
                <div style="font-size: 1.75rem; font-weight: 700; margin: 0.5rem 0; color: var(--badge-4k);">${stats["15d"].size_gb} <span style="font-size: 1rem; font-weight: 500;">GB</span></div>
                <div style="font-size: 0.8rem; color: var(--text-secondary);">${stats["15d"].count} items downloaded</div>
            </div>
            <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 1.25rem; display: flex; flex-direction: column; justify-content: space-between; box-shadow: var(--card-shadow);">
                <div style="font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600; letter-spacing: 0.05em;">Last 30 Days</div>
                <div style="font-size: 1.75rem; font-weight: 700; margin: 0.5rem 0; color: var(--badge-hd);">${stats["30d"].size_gb} <span style="font-size: 1rem; font-weight: 500;">GB</span></div>
                <div style="font-size: 0.8rem; color: var(--text-secondary);">${stats["30d"].count} items downloaded</div>
            </div>
        </div>

        <div class="downloads-dashboard-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem;">
            ${columnsHtml}
        </div>
    `;

    renderHistoryList();
}

// Automatically refresh downloads every 15 minutes (900000 ms)
setInterval(() => {
    const view = document.getElementById('view-downloads');
    if (view && view.classList.contains('active') && document.visibilityState === 'visible') {
        window.loadDownloads();
    }
}, 900000);

window.arrQueueAction = async function(action, type, id, movieId, seriesId, episodeId) {
    try {
        const res = await fetch('/api/v1/integrations/download-queue/action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action, type, id,
                movie_id: movieId || null,
                series_id: seriesId || null,
                episode_id: episodeId || null
            })
        });
        const data = await res.json();
        if (!res.ok || data.success === false) {
            showToast(data.detail || data.error || 'Queue action failed', 'error');
            return;
        }
        showToast(data.message || 'Done', 'success');
        if (typeof window.loadDownloads === 'function') window.loadDownloads();
    } catch (err) {
        showToast(err.message || 'Queue action failed', 'error');
    }
};
