// Local-First Unified Media Search & Direct Add JS
let activeSearchType = 'movie';

document.addEventListener('DOMContentLoaded', () => {
    // Media search initialization and deployment trigger
    const searchBtn = document.getElementById('arr-search-btn');
    const searchInput = document.getElementById('arr-search-input');

    if (searchBtn && searchInput) {
        searchBtn.addEventListener('click', performUnifiedSearch);
        searchInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') performUnifiedSearch();
        });
    }

    // Movie vs TV Show Search Tabs
    document.querySelectorAll('.search-type-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.search-type-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeSearchType = btn.dataset.searchType || 'movie';

            const input = document.getElementById('arr-search-input');
            if (input) {
                input.placeholder = activeSearchType === 'movie' 
                    ? 'Enter movie title (e.g. Inception)...' 
                    : 'Enter TV show title (e.g. Breaking Bad)...';
                if (input.value.trim()) {
                    performUnifiedSearch();
                }
            }
        });
    });
});

async function performUnifiedSearch() {
    const term = document.getElementById('arr-search-input').value.trim();
    const container = document.getElementById('arr-results-container');
    if (!term || !container) return;

    if (term.length < 2) {
        container.innerHTML = `
            <div class="section-card" style="text-align: center;">
                <p class="muted">Please enter at least 2 characters to search.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = '<div class="loading-spinner">Searching local server inventory & online indexers...</div>';

    try {
        const res = await fetch(`/api/v1/integrations/search?term=${encodeURIComponent(term)}&type=${activeSearchType}`);
        const data = await parseApiJson(res);

        if (!res.ok) {
            let errMsg = 'Failed to fetch search results';
            if (typeof data.detail === 'string') {
                errMsg = data.detail;
            } else if (Array.isArray(data.detail) && data.detail.length > 0) {
                errMsg = data.detail.map(e => e.msg).join(', ');
            } else if (data.message) {
                errMsg = data.message;
            }
            throw new Error(errMsg);
        }

        renderSearchResults(data, container);
    } catch (err) {
        container.innerHTML = `<p class="error-msg">Search error: ${err.message}</p>`;
    }
}

function renderSearchResults(data, container) {
    const results = data.results || [];
    if (results.length === 0) {
        const displayTerm = data.term || document.getElementById('arr-search-input')?.value.trim() || '';
        container.innerHTML = `
            <div class="section-card" style="text-align: center;">
                <h4>No results found for "${displayTerm}"</h4>
                <p class="muted">Title was not found on your local server or online indexers.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = `<div class="poster-grid">${results.map((item, idx) => {
        const isLocal = item.in_local_library;
        const year = item.year || '';
        let statusText = 'Not in the library';
        if (isLocal) statusText = 'On the server';
        else if (item.status === 'in_radarr') statusText = 'In Radarr';
        else if (item.status === 'in_sonarr') statusText = 'In Sonarr';

        let action = '';
        if (!isLocal && item.media_data) {
            const isAdded = item.status === 'in_radarr' || item.status === 'in_sonarr';
            action = isAdded
                ? `<button class="btn btn-primary btn-sm" onclick="triggerSearchNow(${idx})">Search again</button>`
                : `<button class="btn btn-primary btn-sm" onclick="promptAddMedia(${idx})">Add</button>`;
        } else if (isLocal && item.media_data) {
            action = `<button class="btn btn-secondary btn-sm" onclick="promptAddMedia(${idx})">Add another</button>`;
        }

        const posterUrl = item.media_data?.images?.find(i => i.coverType === 'poster')?.remoteUrl || '';
        const detailTmdbId = item.media_data?.tmdbId || null;
        const detailOnclick = detailTmdbId ? `onclick="window.showMediaDetailModal({tmdb_id: ${detailTmdbId}, type: '${activeSearchType}'})"` : '';

        return `
            <article class="poster-tile">
                <img id="search-art-${idx}" class="poster-tile-img" src="${posterUrl}" alt="" loading="lazy" decoding="async" ${detailOnclick} onerror="this.style.display='none'">
                <div class="poster-tile-title" ${detailOnclick}>${escapeHtml(item.title)}</div>
                <div class="poster-tile-meta">${year ? year + ' · ' : ''}${statusText}</div>
                <div class="poster-tile-action">${action}</div>
            </article>
        `;
    }).join('')}</div>`;

    // Load extra Fanart.tv artwork for cards missing images
    results.forEach((item, idx) => {
        if (!item.media_data?.images?.length) {
            loadSearchCardArtwork(idx, item.title, activeSearchType);
        }
    });

    window._lastSearchResults = results;
}

async function loadSearchCardArtwork(idx, title, type) {
    const img = document.getElementById(`search-art-${idx}`);
    if (!img) return;
    try {
        const res = await fetch(`/api/v1/integrations/artwork?title=${encodeURIComponent(title)}&type=${encodeURIComponent(type)}`);
        if (!res.ok) return;
        const data = await parseApiJson(res);
        const art = data.artwork || {};
        const poster = art.poster || art.background;
        if (poster) {
            img.src = poster;
            img.style.display = '';
        }
    } catch (e) {}
}

function promptAddMedia(index) {
    const searchResult = window._lastSearchResults[index];
    if (!searchResult || !searchResult.media_data) return;

    const mediaItem = searchResult.media_data;
    const serviceName = activeSearchType === 'movie' ? 'Radarr' : 'Sonarr';

    showAddMediaModal({
        title: mediaItem.title,
        year: mediaItem.year,
        serviceName,
        mediaType: activeSearchType,
        onConfirm: (opts) => addMediaToArr(mediaItem, serviceName, opts)
    });
}

window.showAddMediaModal = function({ title, year, serviceName, onConfirm, searchNowDefault, releaseDate, mediaType }) {
    let grabDefault = searchNowDefault;
    if (grabDefault === undefined && releaseDate) {
        grabDefault = !(String(releaseDate).length >= 10 && new Date(releaseDate) > new Date());
    }
    if (grabDefault === undefined) grabDefault = true;
    const isTv = serviceName === 'Sonarr' || mediaType === 'tv';

    const modal = document.createElement('div');
    modal.className = 'shelf-dialog';

    modal.innerHTML = `
        <div class="section-card" style="width: min(480px, 92vw); max-height: 90vh; overflow-y: auto; padding: clamp(1.25rem, 4vw, 2.25rem); border: 1px solid var(--border-color); text-align: center; box-shadow: none;">
            <div style="font-size: 3rem; margin-bottom: 1rem;">➕</div>
            <h3 style="font-size: 1.3rem; color: var(--text-primary); margin-bottom: 0.5rem;">Add to ${escapeHtml(serviceName)}?</h3>
            <p style="color: var(--text-secondary); font-size: 0.95rem; margin-bottom: 1rem;">
                <b>${escapeHtml(title)}</b>${year ? ` (${escapeHtml(year)})` : ''} will be added to ${escapeHtml(serviceName)}.
            </p>
            <div class="form-group" style="text-align: left; max-width: 22rem; margin: 0 auto 1rem;">
                <label for="add-quality-profile">Quality profile</label>
                <select id="add-quality-profile" class="form-select"><option value="">Loading…</option></select>
            </div>
            ${isTv ? `
            <div class="form-group" style="text-align: left; max-width: 22rem; margin: 0 auto 1rem; color: var(--text-secondary); font-size: 0.88rem;">
                <div style="margin-bottom: 0.35rem;"><strong>Monitor</strong></div>
                <label style="display: flex; gap: 0.4rem; align-items: center; margin-bottom: 0.25rem;"><input type="radio" name="add-season-monitor" value="all" checked> All seasons</label>
                <label style="display: flex; gap: 0.4rem; align-items: center;"><input type="radio" name="add-season-monitor" value="firstSeason"> First season only</label>
            </div>` : ''}
            <label style="display: flex; align-items: flex-start; gap: 0.5rem; text-align: left; color: var(--text-secondary); font-size: 0.88rem; margin: 0 auto 1.25rem; max-width: 22rem;">
                <input type="checkbox" id="add-search-now" ${grabDefault ? 'checked' : ''} style="margin-top: 0.2rem;">
                <span><strong>Grab now</strong> — search indexers immediately. Uncheck to <strong>monitor only</strong> until it is released.</span>
            </label>
            <div style="display: flex; gap: 12px; justify-content: center;">
                <button id="modal-cancel-btn" class="btn btn-secondary" style="padding: 8px 20px; font-size: 0.88rem;">Cancel</button>
                <button id="modal-confirm-btn" class="btn btn-primary" style="padding: 8px 22px; font-size: 0.88rem; font-weight: 600;">+ Add to ${escapeHtml(serviceName)}</button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    fetch(`/api/v1/integrations/add-options?type=${isTv ? 'tv' : 'movie'}`)
        .then(r => parseApiJson(r))
        .then(data => {
            const sel = modal.querySelector('#add-quality-profile');
            if (!sel) return;
            const profiles = data.profiles || [];
            if (!profiles.length) {
                sel.innerHTML = '<option value="">Default in *arr</option>';
                return;
            }
            sel.innerHTML = profiles.map(p =>
                `<option value="${p.id}"${p.id === data.default_profile_id ? ' selected' : ''}>${escapeHtml(p.name || ('Profile ' + p.id))}</option>`
            ).join('');
        })
        .catch(() => {
            const sel = modal.querySelector('#add-quality-profile');
            if (sel) sel.innerHTML = '<option value="">Default in *arr</option>';
        });

    modal.querySelector('#modal-cancel-btn').onclick = () => modal.remove();
    modal.querySelector('#modal-confirm-btn').onclick = () => {
        const searchNow = modal.querySelector('#add-search-now').checked;
        const qEl = modal.querySelector('#add-quality-profile');
        const qid = qEl && qEl.value ? parseInt(qEl.value, 10) : null;
        const seasonEl = modal.querySelector('input[name="add-season-monitor"]:checked');
        onConfirm({
            searchNow,
            qualityProfileId: Number.isFinite(qid) ? qid : null,
            seasonMonitor: seasonEl ? seasonEl.value : null
        });
        modal.remove();
    };
};

async function addMediaToArr(mediaItem, serviceName, opts) {
    if (typeof opts !== 'object' || opts === null) opts = { searchNow: opts };
    const searchNow = opts.searchNow === undefined ? true : opts.searchNow;
    try {
        const res = await fetch('/api/v1/integrations/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: activeSearchType,
                media_data: mediaItem,
                monitored: true,
                search_now: !!searchNow,
                quality_profile_id: opts.qualityProfileId || null,
                season_monitor: opts.seasonMonitor || null
            })
        });

        const data = await parseApiJson(res);
        if (data.success) {
            const msg = data.message || `Requested "${mediaItem.title}" — added to ${serviceName}. Track it under My Requests.`;
            showToast(msg, 'success');
            performUnifiedSearch();
        } else {
            showToast(`Failed to add title: ${data.error || data.detail || 'Unknown error'}`, 'error');
        }
    } catch (err) {
        showToast(`Error adding media: ${err.message}`, 'error');
    }
}

async function triggerSearchNow(idx) {
    const item = window._lastSearchResults[idx];
    if (!item || !item.media_data) return;

    const serviceName = activeSearchType === 'movie' ? 'Radarr' : 'Sonarr';
    const tmdbId = item.media_data.tmdbId || null;
    const tvdbId = item.media_data.tvdbId || null;

    if (confirm(`Are you sure you want to trigger a new search and download for "${item.title}" in ${serviceName}?`)) {
        try {
            const res = await fetch('/api/v1/integrations/search-trigger', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    type: activeSearchType,
                    tmdb_id: tmdbId,
                    tvdb_id: tvdbId
                })
            });
            const data = await parseApiJson(res);
            if (data.success) {
                showToast(`🚀 ${data.message}`);
            } else {
                showToast(`Failed to trigger search: ${data.error || data.detail || 'Unknown error'}`);
            }
        } catch (err) {
            showToast(`Error triggering search: ${err.message}`);
        }
    }
}
