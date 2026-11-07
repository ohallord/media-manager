// Duplicate Groups Analysis & Cleanup View JS
window.loadDuplicates = async function(forceRefresh = false) {
    const container = document.getElementById('duplicates-container');
    if (!container) return;

    container.innerHTML = '<div class="loading-spinner">Scanning and ranking duplicate files...</div>';

    try {
        const url = forceRefresh ? '/api/v1/inventory/duplicates?force_refresh=true' : '/api/v1/inventory/duplicates';
        const res = await fetch(url);
        if (!res.ok) throw new Error('Failed to fetch duplicates');
        const data = await res.json();

        window._lastDuplicatesData = data;
        renderDuplicatesView(data, container);
    } catch (err) {
        container.innerHTML = `<div class="error-msg">Error loading duplicates: ${escapeHtml(err.message)}</div>`;
    }
};

function renderDuplicatesView(data, container) {
    const groups = data.groups || [];
    const isCached = data.cached || false;
    const cacheTier = data.cache_tier || 'computed';

    if (groups.length === 0) {
        container.innerHTML = `
            <div class="section-card" style="text-align: center; padding: 3rem 1.5rem;">
                <div style="font-size: 3rem; margin-bottom: 1rem;">🎉</div>
                <h3 style="font-size: 1.4rem; margin-bottom: 0.5rem;">No Duplicate Files Detected</h3>
                <p class="muted" style="max-width: 500px; margin: 0 auto 1.5rem auto;">Your media library is clean! All movies and TV episodes are unique.</p>
                <button class="btn btn-secondary" onclick="window.loadDuplicates(true)">🔄 Force Re-check</button>
            </div>
        `;
        return;
    }

    const totalWorseCount = data.total_worse_files || 0;
    const totalReclaimGb = formatGB(data.total_reclaimable_bytes);

    container.innerHTML = `
        <!-- Summary & Global Cleanup Card -->
        <div class="section-card" style="padding: 1.75rem; margin-bottom: 2rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.25rem;">
                <div>
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <h3 style="font-size: 1.35rem; margin: 0;">Duplicate Detection Summary</h3>
                        <span style="font-size: 0.75rem; font-weight: 600; padding: 3px 10px; background: ${isCached ? 'rgba(125, 154, 106, 0.2)' : 'var(--accent-light)'}; color: ${isCached ? 'var(--success)' : 'var(--accent-color)'}; border: 1px solid ${isCached ? 'var(--success)' : 'var(--accent-color)'};">
                            ${isCached ? '⚡ Cached (24h TTL)' : '🔄 Computed'}
                        </span>
                    </div>
                    <p class="muted" style="font-size: 0.9rem; margin-top: 0.35rem;">
                        Found <b>${data.total_groups}</b> duplicate groups with <b>${totalWorseCount}</b> lower-quality redundant files (${totalReclaimGb} GB reclaimable space).
                    </p>
                </div>

                <div style="display: flex; gap: 10px;">
                    <button class="btn btn-secondary" style="font-size: 0.85rem; padding: 8px 16px;" onclick="window.loadDuplicates(true)">
                        🔄 Refresh
                    </button>
                    ${totalWorseCount > 0 ? `
                        <button class="btn btn-danger" style="font-size: 0.88rem; padding: 8px 18px; font-weight: 600; background: linear-gradient(135deg, #ef4444, #dc2626);" onclick="deleteWorseDuplicates('all')">
                            🗑️ Clean All Worse Duplicates (${totalWorseCount} Files)
                        </button>
                    ` : ''}
                </div>
            </div>
        </div>

        <!-- Individual Duplicate Groups -->
        ${groups.map((g, idx) => {
            const best = g.best_item;
            const worseItems = g.worse_items || g.candidates || [];
            const bestSizeGb = best ? formatGB(best.size_bytes) : 0;

            return `
                <div class="section-card mb-3" style="border: 1px solid var(--border-color); border-radius: var(--radius-md); padding: 1.25rem;">
                    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1rem; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 0.75rem;">
                        <div>
                            <h4 style="font-size: 1.15rem; margin: 0; display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                                ${g.media_title} 
                                <span class="badge badge-4k">${g.season_episode}</span>
                            </h4>
                            <div class="muted" style="font-size: 0.82rem; margin-top: 2px;">Group ID: ${g.id} | ${g.total_count} total copies</div>
                        </div>

                        ${worseItems.length > 0 ? `
                            <button class="btn btn-secondary" style="font-size: 0.8rem; padding: 6px 12px; color: #f87171; border-color: rgba(239,68,68,0.3);" onclick="deleteWorseDuplicates('${g.id}')">
                                🗑️ Delete Worse Versions in Group
                            </button>
                        ` : ''}
                    </div>

                    <!-- BEST VERSION (RECOMMENDED TO KEEP) -->
                    ${best ? `
                        <div style="background: rgba(16, 185, 129, 0.08); padding: 1rem; border: 1px solid rgba(16, 185, 129, 0.3); margin-bottom: 1rem;">
                            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.35rem;">
                                <span style="font-weight: 700; color: #10b981; font-size: 0.92rem; display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                                    ⭐ RECOMMENDED TO KEEP (${escapeHtml(best.resolution)} • Score: ${escapeHtml(best.quality_score)} pts)
                                </span>
                                <span style="font-size: 0.85rem; font-weight: 600; color: var(--text-primary);">${bestSizeGb} GB</span>
                            </div>
                            <div style="font-size: 0.82rem; color: var(--text-secondary);">
                                <b>Codec:</b> ${best.video_codec ? best.video_codec.toUpperCase() : 'N/A'} / ${best.audio_codec ? best.audio_codec.toUpperCase() : 'N/A'}
                            </div>
                            <div style="font-size: 0.78rem; word-break: break-all; margin-top: 0.35rem; color: var(--text-secondary); opacity: 0.8;" title="${escapeHtml(best.path)}">
                                <b>Path:</b> ${escapeHtml(best.path)}
                            </div>
                        </div>
                    ` : ''}

                    <!-- WORSE VERSIONS (RECOMMENDED TO DELETE) -->
                    <div style="font-size: 0.88rem; font-weight: 600; margin-bottom: 0.6rem; color: #f87171; display: flex; align-items: center; gap: 6px;">
                        ⚠️ Recommended for Deletion (${worseItems.length} Lower Quality File${worseItems.length === 1 ? '' : 's'}):
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 0.6rem;">
                        ${worseItems.map(c => {
                            const cSizeGb = formatGB(c.size_bytes);
                            return `
                                <div style="background: rgba(239, 68, 68, 0.06); padding: 0.85rem; border: 1px solid rgba(239, 68, 68, 0.2); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
                                    <div style="flex: 1; min-width: 200px;">
                                        <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 4px;">
                                            <span class="badge badge-sd" style="background: rgba(239,68,68,0.2); color: #f87171;">${escapeHtml(c.resolution)}</span>
                                            <span style="font-weight: 600; font-size: 0.88rem;">Score: ${c.quality_score} pts</span>
                                            <span style="font-size: 0.82rem; color: var(--text-secondary);">${cSizeGb} GB</span>
                                        </div>
                                        <div style="font-size: 0.78rem; color: var(--text-secondary); word-break: break-all;" title="${escapeHtml(c.path)}">
                                            <b>Path:</b> ${escapeHtml(c.path)}
                                        </div>
                                    </div>
                                    <button class="btn btn-danger" style="font-size: 0.78rem; padding: 6px 12px; white-space: nowrap;" data-id="${escapeHtml(c.id)}" data-path="${escapeHtml(c.path)}" onclick="deleteSingleDuplicateItem(this.dataset.id, this.dataset.path)">
                                        🗑️ Delete File
                                    </button>
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        }).join('')}
    `;
}

// Helper: Show Admin Confirmation Modal
window.showConfirmDeleteModal = function({ title, message, path, onConfirm }) {
    const isAdmin = window.currentUser && window.currentUser.role === 'admin';

    const modal = document.createElement('div');
    modal.className = 'shelf-dialog';
    
    modal.innerHTML = `
        <div class="section-card" style="width: min(550px, 92vw); max-height: 90vh; overflow-y: auto; padding: clamp(1.25rem, 4vw, 2.25rem); border: 1px solid rgba(239, 68, 68, 0.35); text-align: center; box-shadow: none;">
            <div style="font-size: 3rem; margin-bottom: 1rem;">⚠️</div>
            <h3 style="font-size: 1.4rem; color: var(--text-primary); margin-bottom: 0.75rem;">${title}</h3>
            <p style="color: var(--text-secondary); font-size: 0.95rem; margin-bottom: 1.25rem;">${message}</p>
            
            ${path ? `
                <div style="border: 1px solid var(--border-color); padding: 0.75rem; font-family: monospace; font-size: 0.78rem; text-align: left; word-break: break-all; margin-bottom: 1.5rem; color: var(--text-primary);">
                    ${escapeHtml(path)}
                </div>
            ` : ''}

            <div style="display: flex; gap: 12px; justify-content: center;">
                <button id="modal-cancel-btn" class="btn btn-secondary" style="padding: 8px 20px; font-size: 0.88rem;">
                    Cancel
                </button>
                <button id="modal-confirm-btn" class="btn btn-danger" style="padding: 8px 22px; font-size: 0.88rem; font-weight: 600; background: linear-gradient(135deg, #ef4444, #dc2626);">
                    🗑️ Confirm Delete
                </button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    modal.querySelector('#modal-cancel-btn').onclick = () => modal.remove();
    modal.querySelector('#modal-confirm-btn').onclick = () => {
        if (!isAdmin) {
            showToast('🚫 Access Denied: Admin authorization required. Please log in as an administrator to delete media library files.');
            if (window.openAuthModal) {
                window.openAuthModal();
            }
        } else {
            onConfirm();
        }
        modal.remove();
    };
};

// Action: Delete worse duplicates for a specific group or all with parallel backend engine
window.deleteWorseDuplicates = async function(groupId) {
    const isAll = groupId === 'all';
    const lastData = window._lastDuplicatesData || {};
    const groups = lastData.groups || [];

    let targetItems = [];
    if (isAll) {
        groups.forEach(g => {
            const worse = g.worse_items || g.candidates || [];
            targetItems.push(...worse);
        });
    } else {
        const targetGroup = groups.find(g => g.id === groupId);
        if (targetGroup) {
            targetItems = targetGroup.worse_items || targetGroup.candidates || [];
        }
    }

    const totalCount = targetItems.length;
    const title = isAll ? '⚠️ Confirm Bulk Duplicate Deletion' : '⚠️ Confirm Duplicate Deletion';
    const msg = isAll 
        ? `Are you sure you want to permanently delete ALL ${totalCount} recommended worse quality duplicate files across your entire library?` 
        : `Are you sure you want to permanently delete the recommended worse quality file(s) in this group?`;

    window.showConfirmDeleteModal({
        title: title,
        message: msg,
        onConfirm: async () => {
            const container = document.getElementById('duplicates-container');
            if (!container) return;

            container.innerHTML = `
                <div class="section-card" style="text-align: center; padding: 3rem 2rem; max-width: 750px; margin: 2rem auto;">
                    <div style="font-size: 2.5rem; margin-bottom: 0.75rem;">⚡</div>
                    <h3 style="font-size: 1.5rem; color: var(--text-primary); margin-bottom: 0.5rem;">Cleaning Redundant Duplicates (High-Speed Parallel Engine)</h3>
                    <p style="color: var(--text-secondary); font-size: 0.95rem; margin-bottom: 1.5rem;">
                        Deleting redundant files concurrently on disk and updating database...
                    </p>

                    <div style="display: flex; justify-content: space-between; font-weight: 700; font-size: 1.1rem; margin-bottom: 0.5rem;">
                        <span id="del-progress-count" style="color: var(--text-primary);">Processing ${totalCount > 0 ? totalCount : ''} items...</span>
                        <span id="del-progress-percent" style="color: var(--accent-color);">50%</span>
                    </div>

                    <div style="width: 100%; height: 16px; background: var(--bg-primary); overflow: hidden; border: 1px solid var(--border-color); margin-bottom: 1rem;">
                        <div id="del-progress-bar" style="width: 50%; height: 100%; background: var(--accent-color); transition: width 0.3s ease-in-out;"></div>
                    </div>

                    <div style="display: flex; justify-content: space-between; font-size: 0.85rem; color: var(--text-secondary);">
                        <div id="del-current-file" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%; text-align: left;">Executing parallel disk unlinking...</div>
                        <div id="del-freed-storage" style="font-weight: 600; color: #10b981;">Reclaiming Storage...</div>
                    </div>
                </div>
            `;

            try {
                const res = await fetch(`/api/v1/inventory/duplicates/delete-worse?group_id=${groupId}`, { method: 'POST' });
                if (!res.ok) throw new Error('Failed to delete worse duplicates');

                const data = await res.json();
                const countElem = document.getElementById('del-progress-count');
                const percentElem = document.getElementById('del-progress-percent');
                const barElem = document.getElementById('del-progress-bar');
                const freedElem = document.getElementById('del-freed-storage');

                if (countElem) countElem.textContent = `${data.deleted_count} / ${data.deleted_count + (data.permission_denied_count || 0)} files deleted`;
                if (percentElem) percentElem.textContent = `100%`;
                if (barElem) barElem.style.width = `100%`;
                if (freedElem) freedElem.textContent = `${formatGB(data.freed_bytes)} GB Reclaimed`;

                // Force re-fetch fresh duplicates (invalidating cache & re-computing groups)
                await window.loadDuplicates(true);

                // Prepend success/warning banner to fresh view
                const freshContainer = document.getElementById('duplicates-container');
                if (freshContainer) {
                    const banner = document.createElement('div');
                    banner.className = 'section-card';
                    const failedCount = data.permission_denied_count || 0;
                    const isPartial = failedCount > 0;

                    banner.style.cssText = isPartial 
                        ? 'background: rgba(245, 158, 11, 0.15); border: 1px solid #f59e0b; color: #f59e0b; padding: 1.25rem; border-radius: 12px; margin-bottom: 1.5rem; display: flex; justify-content: space-between; align-items: center;'
                        : 'background: rgba(16, 185, 129, 0.15); border: 1px solid #10b981; color: #10b981; padding: 1.25rem; border-radius: 12px; margin-bottom: 1.5rem; display: flex; justify-content: space-between; align-items: center;';

                    const statusMsg = isPartial 
                        ? `Successfully deleted <b>${data.deleted_count}</b> file(s) (${formatGB(data.freed_bytes)} GB). ⚠️ <b>${failedCount} file(s) failed due to disk permission denied</b> (owned by root).`
                        : `Successfully deleted <b>${data.deleted_count}</b> redundant file(s) and reclaimed <b>${formatGB(data.freed_bytes)} GB</b> of disk space in record time.`;

                    banner.innerHTML = `
                        <div>
                            <h4 style="margin: 0; font-size: 1.1rem;">${isPartial ? '⚠️ Cleanup Completed with Permission Warnings' : '⚡ High-Speed Deletion Complete!'}</h4>
                            <p style="margin: 4px 0 0 0; font-size: 0.9rem;">${statusMsg}</p>
                        </div>
                        <button class="btn btn-secondary" style="font-size: 0.8rem; padding: 6px 12px; border-color: currentColor; color: currentColor;" onclick="this.parentElement.remove()">Dismiss</button>
                    `;
                    freshContainer.insertBefore(banner, freshContainer.firstChild);
                }
            } catch (err) {
                showToast(`Error deleting duplicate files: ${err.message}`);
                window.loadDuplicates(true);
            }
        }
    });
};

// Action: Delete a single specific duplicate item
window.deleteSingleDuplicateItem = async function(itemId, path) {
    window.showConfirmDeleteModal({
        title: '⚠️ Confirm Permanent Deletion',
        message: 'Are you sure you want to permanently delete this file? This operation cannot be undone.',
        path: path,
        onConfirm: async () => {
            const card = document.getElementById(`media-item-${itemId}`);
            if (card) {
                card.style.opacity = '0.5';
                card.style.pointerEvents = 'none';
            }

            try {
                const res = await fetch(`/api/v1/inventory/items/${itemId}`, { method: 'DELETE' });
                if (!res.ok) throw new Error('Failed to delete item');

                // Force re-fetch fresh duplicates & inventory list
                await window.loadDuplicates(true);
                if (window.loadInventory) {
                    window.loadInventory();
                }
            } catch (err) {
                showToast(`Error deleting file: ${err.message}`);
                window.loadDuplicates(true);
            }
        }
    });
};
