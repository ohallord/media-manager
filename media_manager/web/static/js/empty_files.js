// Empty Files card (Cleanup page): lists library files that are 0-byte
// placeholders and, on an explicit admin click, asks Radarr/Sonarr to
// re-download them (stale 0-byte file record deleted first, then searched).
const EMPTY_FILE_STATUS_LABELS = {
    radarr_search: 'Will search in Radarr',
    radarr_add: 'Will add to Radarr',
    sonarr_search: 'Will search in Sonarr',
    sonarr_add: 'Will add series to Sonarr',
    added_pending: 'Series added - run again shortly',
    episode_not_found: 'Episode not in Sonarr',
    unavailable: 'Service unavailable',
    no_longer_empty: 'Already replaced',
    requested: 'Download requested',
    not_found: 'No Radarr match',
    failed: 'Failed',
};
const EMPTY_FILE_ACTIONABLE = new Set(['radarr_search', 'radarr_add', 'sonarr_search', 'sonarr_add']);

window.loadEmptyFiles = async function() {
    const el = document.getElementById('empty-files-card');
    if (!el) return;
    el.innerHTML = '<div class="loading-spinner">Checking for empty (0-byte) files...</div>';
    try {
        const res = await fetch('/api/v1/redownload/empty-files');
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Failed to load empty files.');
        renderEmptyFiles(el, data.items || []);
    } catch (err) {
        el.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
    }
};

function renderEmptyFiles(el, items, resultCounts) {
    const isAdmin = window.currentUser && window.currentUser.role === 'admin';
    const actionable = items.filter(i => EMPTY_FILE_ACTIONABLE.has(i.status)).length;
    const rows = items.map(i => `
        <tr>
            <td>${escapeHtml(i.media_type === 'tv' ? 'TV' : 'Movie')}</td>
            <td title="${escapeHtml(i.path)}">${escapeHtml(i.detail || i.title)}</td>
            <td>${escapeHtml(EMPTY_FILE_STATUS_LABELS[i.status] || i.status)}</td>
        </tr>`).join('');
    const summary = resultCounts
        ? `<div style="color: var(--success, #22c55e); font-weight: 600; margin-bottom: 0.5rem;">✅ ${escapeHtml(Object.entries(resultCounts).map(([k, v]) => `${v} ${EMPTY_FILE_STATUS_LABELS[k] || k}`).join(', '))}</div>`
        : '';
    el.innerHTML = `
        <h3 style="margin-top: 0;">Empty Files (${items.length})</h3>
        <p class="muted" style="font-size: 0.85rem;">Library files that are 0 bytes - unplayable placeholders. Re-downloading deletes the stale record in Radarr/Sonarr, searches again, and removes the empty placeholder.</p>
        ${summary}
        ${items.length ? `
            <div style="max-height: 320px; overflow-y: auto;">
                <table style="width: 100%; border-collapse: collapse; font-size: 0.85rem; text-align: left;">
                    <thead><tr><th>Type</th><th>Title</th><th>Action</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            ${isAdmin && actionable && !resultCounts ? `<button class="btn btn-primary" id="empty-files-redownload-btn" style="margin-top: 0.75rem;">⬇️ Re-download ${actionable} item${actionable === 1 ? '' : 's'}</button>` : ''}
        ` : '<p class="muted">No empty files found. 🎉</p>'}
    `;
    const btn = document.getElementById('empty-files-redownload-btn');
    if (btn) btn.onclick = () => onEmptyFilesRedownload(el, btn);
}

async function onEmptyFilesRedownload(el, btn) {
    btn.disabled = true;
    btn.textContent = 'Requesting downloads...';
    try {
        const res = await fetch('/api/v1/redownload/empty-files', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ all: true }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Re-download failed');
        renderEmptyFiles(el, data.results || [], data.counts || {});
        showToast(`Requested ${(data.counts || {}).requested || 0} re-download(s)`, 'success');
    } catch (err) {
        showToast(err.message, 'error');
        btn.disabled = false;
        btn.textContent = 'Retry Re-download';
    }
}
