// Reusable title detail modal - shows full detail + a "More Like This" row
// (Similar + Recommended). Wired as a click handler on posters/titles across
// Search, Coming Soon, Trending, and Calendar. Follows the same
// dynamically-created-modal pattern as showAddMediaModal in media_search.js.
window.showMediaDetailModal = function({ tmdb_id, type }) {
    const existing = document.getElementById('media-detail-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'media-detail-modal';
    modal.className = 'shelf-dialog shelf-dialog-top';
    modal.innerHTML = `
        <div class="section-card" style="width: min(760px, 95vw); border: 1px solid var(--border-color); position: relative; padding: 0; overflow: hidden;">
            <button id="media-detail-close-btn" style="position: absolute; top: 12px; right: 12px; z-index: 2; width: 32px; height: 32px; border-radius: 50%; border: none; background: rgba(0,0,0,0.5); color: #fff; font-size: 1.1rem; cursor: pointer; line-height: 1;">×</button>
            <div id="media-detail-body" style="padding: 1.5rem;"><div class="loading-spinner">Loading details...</div></div>
        </div>
    `;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
    modal.querySelector('#media-detail-close-btn').onclick = () => modal.remove();

    loadMediaDetail(tmdb_id, type);
};

async function loadMediaDetail(tmdb_id, type) {
    const body = document.getElementById('media-detail-body');
    if (!body) return;

    try {
        const res = await fetch(`/api/v1/integrations/media-detail?tmdb_id=${tmdb_id}&type=${type}`);
        const data = await res.json();

        if (!res.ok) {
            body.innerHTML = `<p class="error-msg">${escapeHtml(data.detail || 'Failed to load details.')}</p>`;
            return;
        }

        renderMediaDetailBody(data);
    } catch (err) {
        body.innerHTML = `<p class="error-msg">Failed to load details: ${escapeHtml(err.message)}</p>`;
    }
}

function mediaDetailActionHtml(data) {
    if (data.status === 'in_library') {
        return '<span class="badge badge-4k" style="background-color: var(--success);">🟢 Already In Library</span>';
    }
    if (data.status === 'in_radarr' || data.status === 'in_sonarr') {
        const label = data.status === 'in_radarr' ? 'Already In Radarr' : 'Already In Sonarr';
        return `<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 ${label}</span>`;
    }
    return '<button id="media-detail-add-btn" class="btn btn-primary btn-sm">+ Add</button>';
}

function mediaDetailPlaybackHtml(data) {
    const pb = data.playback;
    if (!pb || typeof renderPlaybackBadge !== 'function') return '';
    if (data.type === 'tv') {
        if (!pb.episode_count) return '';
        const label = `Total Plays across ${pb.episode_count} episode${pb.episode_count === 1 ? '' : 's'}`;
        return `<div style="margin-top: 0.4rem;"><b style="font-size: 0.72rem; color: var(--text-secondary);">${label}:</b>${renderPlaybackBadge(pb.total_plays, pb.last_played_at, pb.play_sources)}</div>`;
    }
    if (data.status !== 'in_library') return '';
    return `<div style="margin-top: 0.4rem;">${renderPlaybackBadge(pb.play_count, pb.last_played_at, pb.play_sources)}</div>`;
}

// Horizontal strip of clickable posters (each opens its own detail modal).
// `headingHtml` must already be escaped; item fields are escaped here.
function mediaDetailPosterRow(headingHtml, items, type) {
    const safeType = type === 'tv' ? 'tv' : 'movie';
    return `
        <div style="margin-top: 1.5rem; border-top: 1px solid var(--border-color); padding-top: 1rem;">
            <h4 style="margin: 0 0 0.75rem 0; font-size: 0.95rem;">${headingHtml}</h4>
            <div style="display: flex; gap: 0.75rem; overflow-x: auto; padding-bottom: 4px;">
                ${items.map(r => `
                    <div onclick="window.showMediaDetailModal({tmdb_id: ${Number(r.tmdb_id)}, type: '${safeType}'})" style="cursor: pointer; flex-shrink: 0; width: 90px;${r.is_current ? ' opacity: 0.6;' : ''}" title="${escapeHtml(r.title)}${r.year ? ` (${escapeHtml(r.year)})` : ''}">
                        ${r.poster_url
                            ? `<img src="${escapeHtml(r.poster_url)}" loading="lazy" decoding="async" style="width: 90px; height: 135px; object-fit: cover; border-radius: var(--radius-md); box-shadow: 0 2px 6px rgba(0,0,0,0.4);" onerror="this.style.display='none'">`
                            : `<div style="width: 90px; height: 135px; border-radius: var(--radius-md); background: var(--bg-primary); display: flex; align-items: center; justify-content: center; font-size: 1.5rem;">🎬</div>`}
                        <div style="font-size: 0.7rem; margin-top: 4px; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(r.title)}</div>
                        ${r.is_current ? '<div style="font-size: 0.62rem; color: var(--text-secondary);">This title</div>' : ''}
                        ${r.status !== 'not_added' ? `<div style="font-size: 0.62rem; color: var(--success);">✔ ${r.status === 'in_library' ? 'Owned' : 'Added'}</div>` : ''}
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

function mediaDetailCollectionHtml(data) {
    const c = data.collection;
    if (!c || !c.parts || !c.parts.length) return '';
    const pending = c.added_count - c.owned_count;
    const heading = `${escapeHtml(c.name)} <span style="font-weight: 400; font-size: 0.8rem; color: var(--text-secondary);">· you own ${c.owned_count} of ${c.total}${pending > 0 ? ` (${pending} requested)` : ''}</span>`;
    return mediaDetailPosterRow(heading, c.parts, 'movie');
}

function renderMediaDetailBody(data) {
    const body = document.getElementById('media-detail-body');
    const genreBadges = (data.genres || []).map(g => `
        <span class="genre-badge">${escapeHtml(g)}</span>
    `).join('');

    body.innerHTML = `
        ${data.backdrop_url ? `<div style="margin: -1.5rem -1.5rem 1rem -1.5rem; height: 220px; background-image: linear-gradient(to top, var(--bg-secondary), transparent), url('${data.backdrop_url}'); background-size: cover; background-position: center;"></div>` : ''}
        <div style="display: flex; gap: 1.25rem; flex-wrap: wrap;">
            <img src="${data.poster_url}" loading="lazy" decoding="async" class="media-detail-poster" style="width: 130px; height: 195px; object-fit: cover; border-radius: var(--radius-md); flex-shrink: 0; box-shadow: 0 4px 10px rgba(0,0,0,0.4);" onerror="this.style.display='none'">
            <div style="flex: 1; min-width: 0;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem; flex-wrap: wrap;">
                    <h3 style="margin: 0;">${escapeHtml(data.title)}</h3>
                    <span id="media-detail-action">${mediaDetailActionHtml(data)}</span>
                </div>
                <div style="font-size: 0.8rem; color: var(--accent-color); font-weight: 600; margin: 0.4rem 0; display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;">
                    <span>${data.release_date || ''}${data.runtime ? ` &nbsp;·&nbsp; ${data.runtime} min` : ''}${data.rating ? ` &nbsp;·&nbsp; ⭐ ${data.rating}/10` : ''}</span>
                    ${data.trailer_url ? `<a href="${data.trailer_url}" target="_blank" rel="noopener noreferrer" class="trailer-link">▶ Watch Trailer</a>` : ''}
                </div>
                ${data.networks && data.networks.length ? `<div style="font-size: 0.78rem; color: var(--text-secondary); margin-bottom: 0.4rem;">📡 Network: ${escapeHtml(data.networks.join(', '))}</div>` : ''}
                ${data.streaming_providers && data.streaming_providers.length ? `<div style="font-size: 0.78rem; color: var(--text-secondary); margin-bottom: 0.4rem;">📺 Streaming on: ${escapeHtml(data.streaming_providers.join(', '))}</div>` : ''}
                ${data.requested_by ? `<div style="font-size: 0.72rem; color: var(--text-secondary); margin-bottom: 0.4rem;">👤 Requested by: <strong>${escapeHtml(data.requested_by)}</strong></div>` : ''}
                ${mediaDetailPlaybackHtml(data)}
                <p style="font-size: 0.85rem; color: var(--text-secondary); margin: 0.5rem 0;">${escapeHtml(data.overview || '')}</p>
                ${genreBadges ? `<div style="display: flex; gap: 0.4rem; flex-wrap: wrap; margin-top: 0.5rem;">${genreBadges}</div>` : ''}
                ${data.local_files && data.local_files.length ? `
                    <div style="margin-top: 0.75rem; font-size: 0.78rem; color: var(--text-secondary);">
                        <strong>On disk (${data.local_files.length})</strong>
                        ${data.local_files.slice(0, 8).map(f => {
                            const se = (f.season != null || f.episode != null) ? ` S${String(f.season || 0).padStart(2,'0')}E${String(f.episode || 0).padStart(2,'0')}` : '';
                            const playInfo = f.play_count ? ` · ▶ ${f.play_count} play${f.play_count === 1 ? '' : 's'}` : '';
                            return `<div>${escapeHtml(f.resolution || '')}${se} · ${f.quality_score} pts · ${typeof formatGB === 'function' ? formatGB(f.size_bytes) + ' GB' : ''}${playInfo}</div>`;
                        }).join('')}
                    </div>` : ''}
            </div>
        </div>
        ${typeof renderCastRow === 'function' && data.cast && data.cast.length ? `<div style="margin-top: 1rem;">${renderCastRow(data.cast)}</div>` : ''}
        ${mediaDetailCollectionHtml(data)}
        ${data.related && data.related.length ? mediaDetailPosterRow('More Like This', data.related, data.type) : ''}
    `;

    const addBtn = document.getElementById('media-detail-add-btn');
    if (addBtn) {
        addBtn.onclick = () => promptAddFromMediaDetail(data);
    }
}

function promptAddFromMediaDetail(data) {
    if (!window.showAddMediaModal) return;
    const serviceName = data.type === 'movie' ? 'Radarr' : 'Sonarr';
    window.showAddMediaModal({
        title: data.title,
        year: data.release_date ? data.release_date.slice(0, 4) : null,
        serviceName,
        releaseDate: data.release_date,
        onConfirm: (opts) => addFromMediaDetail(data, opts)
    });
}

async function addFromMediaDetail(data, opts) {
    await postAddByTmdb(data, opts, data.type, () => {
        data.status = data.type === 'movie' ? 'in_radarr' : 'in_sonarr';
        const actionEl = document.getElementById('media-detail-action');
        if (actionEl) actionEl.innerHTML = mediaDetailActionHtml(data);
    });
}
