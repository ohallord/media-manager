// Library Cleanup Wizard: suggests media nobody watches (grouped by type,
// using configurable "unwatched for N days" thresholds), reviewed and
// explicitly confirmed by a human - this never deletes anything on its own.
let _cleanupStep = 1;
let _cleanupSettings = null;
let _cleanupSuggestions = null;
let _cleanupSelectedMovies = new Set();
let _cleanupSelectedShows = new Set();

window.loadCleanupWizard = function() {
    _cleanupStep = 1;
    _cleanupSelectedMovies = new Set();
    _cleanupSelectedShows = new Set();
    renderCleanupStep();
};

function renderCleanupStepIndicator() {
    const el = document.getElementById('cleanup-wizard-steps');
    if (!el) return;
    const steps = [{ n: 1, label: '1. Settings' }, { n: 2, label: '2. Review' }, { n: 3, label: '3. Confirm' }];
    el.innerHTML = steps.map((s, i) => `
        ${i > 0 ? '<span style="align-self: center; color: var(--text-secondary);">&rarr;</span>' : ''}
        <span style="padding: 4px 12px; border-radius: 999px; font-weight: 600; ${
            s.n === _cleanupStep
                ? 'background: var(--accent-color); color: #fff;'
                : 'background: rgba(255,255,255,0.06); color: var(--text-secondary);'
        }">${s.label}</span>
    `).join('');
}

async function renderCleanupStep() {
    renderCleanupStepIndicator();
    const container = document.getElementById('cleanup-wizard-container');
    if (!container) return;
    if (_cleanupStep === 1) return renderCleanupSettingsStep(container);
    if (_cleanupStep === 2) return renderCleanupReviewStep(container);
    if (_cleanupStep === 3) return renderCleanupConfirmStep(container);
}

// ---------- Step 1: Settings ----------

async function renderCleanupSettingsStep(container) {
    container.innerHTML = '<div class="loading-spinner">Loading current settings & disk usage...</div>';
    try {
        const [settingsRes, dirsRes] = await Promise.all([
            fetch('/api/v1/cleanup/settings'),
            fetch('/api/v1/integrations/directories'),
        ]);
        if (!settingsRes.ok) throw new Error('Failed to load cleanup settings.');
        _cleanupSettings = await settingsRes.json();
        const dirs = dirsRes.ok ? await dirsRes.json() : [];

        const diskHtml = (dirs || [])
            .filter(d => d.disk_space && d.disk_space.total)
            .map(d => {
                const ds = d.disk_space;
                const usedPct = (100 - ds.free_percent).toFixed(0);
                return `
                    <div style="display: flex; justify-content: space-between; font-size: 0.8rem; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                        <span class="muted">${escapeHtml(d.path)}</span>
                        <span>${usedPct}% used &middot; ${formatBytes(ds.free)} free</span>
                    </div>
                `;
            }).join('');

        container.innerHTML = `
            <div class="section-card mb-3">
                <h3 style="margin-top: 0;">📊 Current Disk Usage</h3>
                ${diskHtml || '<p class="muted">No monitored directories configured.</p>'}
            </div>
            <div class="section-card mb-3">
                <h3 style="margin-top: 0;">⚙️ Cleanup Thresholds</h3>
                <p class="muted" style="font-size: 0.85rem;">
                    A title is suggested when it's <b>never been played</b> and was added more than the grace
                    period ago, or when it <b>was played but not recently</b>. TV shows are suggested as a whole
                    series only once every episode has been synced by a playback source. Defaults follow common
                    practice from comparable tools - adjust to taste.
                </p>
                ${['movie', 'tv'].map(type => `
                    <div style="border-top: 1px solid var(--border-color); padding-top: 0.75rem; margin-top: 0.75rem;">
                        <h4 style="margin: 0 0 0.5rem 0;">${type === 'movie' ? '🎬 Movies' : '📺 TV Shows (whole series)'}</h4>
                        <div style="display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center;">
                            <label style="font-size: 0.85rem; display: flex; align-items: center; gap: 6px;">
                                Never played, added more than
                                <input type="number" min="0" id="cleanup-grace-${type}" value="${_cleanupSettings[type].never_played_grace_days}" style="width: 70px;" class="search-input">
                                days ago
                            </label>
                            <label style="font-size: 0.85rem; display: flex; align-items: center; gap: 6px;">
                                Not played in more than
                                <input type="number" min="0" id="cleanup-stale-${type}" value="${_cleanupSettings[type].stale_after_days}" style="width: 70px;" class="search-input">
                                days
                            </label>
                        </div>
                    </div>
                `).join('')}
            </div>
            <button class="btn btn-primary" id="cleanup-find-btn">Find Cleanup Candidates &rarr;</button>
        `;

        document.getElementById('cleanup-find-btn').onclick = onCleanupFindCandidates;
    } catch (err) {
        container.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
    }
}

async function onCleanupFindCandidates() {
    const btn = document.getElementById('cleanup-find-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving settings...'; }
    try {
        for (const type of ['movie', 'tv']) {
            const grace = parseInt(document.getElementById(`cleanup-grace-${type}`).value, 10) || 0;
            const stale = parseInt(document.getElementById(`cleanup-stale-${type}`).value, 10) || 0;
            const res = await fetch(`/api/v1/cleanup/settings/${type}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ never_played_grace_days: grace, stale_after_days: stale }),
            });
            if (!res.ok) throw new Error(`Failed to save ${type} settings.`);
        }
        _cleanupStep = 2;
        await renderCleanupStep();
    } catch (err) {
        showToast('Failed to save settings: ' + err.message);
        if (btn) { btn.disabled = false; btn.textContent = 'Find Cleanup Candidates →'; }
    }
}

// ---------- Step 2: Review ----------

async function renderCleanupReviewStep(container) {
    container.innerHTML = '<div class="loading-spinner">Finding unwatched media...</div>';
    try {
        const res = await fetch('/api/v1/cleanup/suggestions?force_refresh=true');
        if (!res.ok) throw new Error('Failed to load suggestions.');
        _cleanupSuggestions = await res.json();
        renderCleanupReviewGroups(container);
    } catch (err) {
        container.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
    }
}

function cleanupSelectedBytes() {
    let total = 0;
    (_cleanupSuggestions.movie.suggestions || []).forEach(s => {
        if (_cleanupSelectedMovies.has(s.media_item_id)) total += s.size_bytes;
    });
    (_cleanupSuggestions.tv.suggestions || []).forEach(s => {
        if (_cleanupSelectedShows.has(s.show_name)) total += s.size_bytes;
    });
    return total;
}

function renderCleanupSuggestionRow(type, s) {
    const isMovie = type === 'movie';
    const checked = isMovie ? _cleanupSelectedMovies.has(s.media_item_id) : _cleanupSelectedShows.has(s.show_name);
    const dataAttr = isMovie
        ? `data-cleanup-movie-id="${escapeHtml(s.media_item_id)}"`
        : `data-cleanup-show-name="${escapeHtml(s.show_name)}"`;
    const title = isMovie
        ? escapeHtml(s.title)
        : `${escapeHtml(s.show_name)} <span class="muted" style="font-size: 0.75rem;">(${s.episode_count} episodes)</span>`;
    const reasonText = s.reason === 'never_played'
        ? `Never played &middot; added ${formatPlaybackDate(s.added_at) || 'unknown'}`
        : `Not played since ${formatPlaybackDate(s.last_played_at) || 'unknown'}`;

    return `
        <label style="display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem; border-radius: 6px; background: rgba(255,255,255,0.03); cursor: pointer;">
            <input type="checkbox" ${dataAttr} ${checked ? 'checked' : ''} style="width: 16px; height: 16px; flex-shrink: 0;">
            <div style="flex: 1; min-width: 0;">
                <div style="font-weight: 600;">${title}</div>
                <div class="muted" style="font-size: 0.78rem;">${reasonText}</div>
            </div>
            <div style="font-size: 0.85rem; color: var(--text-secondary); white-space: nowrap;">${formatGB(s.size_bytes)} GB</div>
        </label>
    `;
}

function renderCleanupGroupSection(type, label, group) {
    if (!group) return '';
    const suggestions = group.suggestions || [];
    if (suggestions.length === 0) {
        return `<div class="section-card mb-3"><h3 style="margin-top: 0;">${label}</h3><p class="muted">Nothing to suggest here - nice and tidy 🎉</p></div>`;
    }
    return `
        <div class="section-card mb-3">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.75rem;">
                <h3 style="margin: 0;">${label} <span class="muted" style="font-size: 0.8rem; font-weight: normal;">(${suggestions.length} suggested &middot; ${formatGB(group.total_reclaimable_bytes)} GB)</span></h3>
                <button class="btn btn-secondary btn-sm" id="cleanup-select-all-${type}">Select all</button>
            </div>
            <div style="display: flex; flex-direction: column; gap: 0.5rem;">
                ${suggestions.map(s => renderCleanupSuggestionRow(type, s)).join('')}
            </div>
        </div>
    `;
}

function renderCleanupReviewGroups(container) {
    const movieGroup = _cleanupSuggestions.movie;
    const tvGroup = _cleanupSuggestions.tv;
    const totalSelectedBytes = cleanupSelectedBytes();
    const totalSelectedCount = _cleanupSelectedMovies.size + _cleanupSelectedShows.size;

    container.innerHTML = `
        <div class="section-card mb-3" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
            <div><b>${totalSelectedCount}</b> selected &middot; <b>${formatGB(totalSelectedBytes)} GB</b> would be reclaimed</div>
            <div style="display: flex; gap: 0.5rem;">
                <button class="btn btn-secondary" id="cleanup-back-btn">&larr; Back to Settings</button>
                <button class="btn btn-primary" id="cleanup-review-next-btn" ${totalSelectedCount === 0 ? 'disabled' : ''}>Review Selection &rarr;</button>
            </div>
        </div>
        ${renderCleanupGroupSection('movie', '🎬 Movies', movieGroup)}
        ${renderCleanupGroupSection('tv', '📺 TV Shows', tvGroup)}
    `;

    document.getElementById('cleanup-back-btn').onclick = () => { _cleanupStep = 1; renderCleanupStep(); };
    const nextBtn = document.getElementById('cleanup-review-next-btn');
    if (nextBtn) nextBtn.onclick = () => { _cleanupStep = 3; renderCleanupStep(); };

    container.querySelectorAll('[data-cleanup-movie-id]').forEach(cb => {
        cb.addEventListener('change', () => {
            const id = cb.dataset.cleanupMovieId;
            if (cb.checked) _cleanupSelectedMovies.add(id); else _cleanupSelectedMovies.delete(id);
            renderCleanupReviewGroups(container);
        });
    });
    container.querySelectorAll('[data-cleanup-show-name]').forEach(cb => {
        cb.addEventListener('change', () => {
            const name = cb.dataset.cleanupShowName;
            if (cb.checked) _cleanupSelectedShows.add(name); else _cleanupSelectedShows.delete(name);
            renderCleanupReviewGroups(container);
        });
    });

    const selectAllMovies = document.getElementById('cleanup-select-all-movie');
    if (selectAllMovies) selectAllMovies.onclick = () => {
        (movieGroup.suggestions || []).forEach(s => _cleanupSelectedMovies.add(s.media_item_id));
        renderCleanupReviewGroups(container);
    };
    const selectAllTv = document.getElementById('cleanup-select-all-tv');
    if (selectAllTv) selectAllTv.onclick = () => {
        (tvGroup.suggestions || []).forEach(s => _cleanupSelectedShows.add(s.show_name));
        renderCleanupReviewGroups(container);
    };
}

// ---------- Step 3: Confirm ----------

function cleanupCollectSelectedMediaItemIds() {
    const ids = [];
    (_cleanupSuggestions.movie.suggestions || []).forEach(s => {
        if (_cleanupSelectedMovies.has(s.media_item_id)) ids.push(s.media_item_id);
    });
    (_cleanupSuggestions.tv.suggestions || []).forEach(s => {
        if (_cleanupSelectedShows.has(s.show_name)) ids.push(...s.episode_media_item_ids);
    });
    return ids;
}

function renderCleanupConfirmStep(container) {
    const isAdmin = window.currentUser && window.currentUser.role === 'admin';
    const selectedMovies = (_cleanupSuggestions.movie.suggestions || []).filter(s => _cleanupSelectedMovies.has(s.media_item_id));
    const selectedShows = (_cleanupSuggestions.tv.suggestions || []).filter(s => _cleanupSelectedShows.has(s.show_name));
    const totalBytes = cleanupSelectedBytes();
    const totalCount = selectedMovies.length + selectedShows.length;

    container.innerHTML = `
        <div class="section-card mb-3">
            <h3 style="margin-top: 0;">Confirm Deletion</h3>
            <p class="muted">This permanently deletes the files below from disk and removes them from the library. This cannot be undone.</p>
            <div style="display: flex; flex-direction: column; gap: 0.4rem; max-height: 400px; overflow-y: auto; margin: 1rem 0; padding-right: 4px;">
                ${selectedMovies.map(s => `
                    <div style="display: flex; justify-content: space-between; font-size: 0.85rem;">
                        <span>🎬 ${escapeHtml(s.title)}</span>
                        <span class="muted">${formatGB(s.size_bytes)} GB</span>
                    </div>
                `).join('')}
                ${selectedShows.map(s => `
                    <div style="display: flex; justify-content: space-between; font-size: 0.85rem;">
                        <span>📺 ${escapeHtml(s.show_name)} (${s.episode_count} episodes)</span>
                        <span class="muted">${formatGB(s.size_bytes)} GB</span>
                    </div>
                `).join('')}
            </div>
            <div style="font-weight: 600; margin-bottom: 1rem;">Total: ${totalCount} item${totalCount === 1 ? '' : 's'} &middot; ${formatGB(totalBytes)} GB</div>
            <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                <button class="btn btn-secondary" id="cleanup-back-to-review-btn">&larr; Back to Review</button>
                ${isAdmin
                    ? `<button class="btn btn-danger" id="cleanup-delete-btn">🗑️ Delete ${totalCount} item${totalCount === 1 ? '' : 's'} (${formatGB(totalBytes)} GB)</button>`
                    : `<span class="muted">Only an admin can perform the deletion.</span>`
                }
            </div>
            <div id="cleanup-delete-result" class="mt-3"></div>
        </div>
    `;

    document.getElementById('cleanup-back-to-review-btn').onclick = () => { _cleanupStep = 2; renderCleanupStep(); };
    const deleteBtn = document.getElementById('cleanup-delete-btn');
    if (deleteBtn) deleteBtn.onclick = onCleanupConfirmDelete;
}

async function onCleanupConfirmDelete() {
    const btn = document.getElementById('cleanup-delete-btn');
    const resultEl = document.getElementById('cleanup-delete-result');
    if (btn) { btn.disabled = true; btn.textContent = 'Deleting...'; }
    try {
        const ids = cleanupCollectSelectedMediaItemIds();
        const res = await fetch('/api/v1/cleanup/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ media_item_ids: ids }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Delete failed');

        if (resultEl) {
            const permWarning = data.permission_denied_count
                ? ` ⚠️ ${data.permission_denied_count} skipped due to permission errors.`
                : '';
            resultEl.innerHTML = `<div style="color: var(--success, #22c55e); font-weight: 600;">✅ Deleted ${data.deleted_count} file(s), freed ${formatGB(data.freed_bytes)} GB.${permWarning}</div>`;
        }
        showToast(`Deleted ${data.deleted_count} item(s), freed ${formatGB(data.freed_bytes)} GB`, 'success');
        _cleanupSelectedMovies = new Set();
        _cleanupSelectedShows = new Set();
        if (btn) btn.textContent = 'Done';
    } catch (err) {
        if (resultEl) resultEl.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
        if (btn) { btn.disabled = false; btn.textContent = 'Retry Delete'; }
    }
}
