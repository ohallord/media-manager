// Shared helpers used across the other view scripts. Loaded first in index.html so
// every other file can rely on these being defined.

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Always renders in GB (unlike formatBytes, which picks the best-fit unit) - for the
// several places that specifically want a GB figure regardless of magnitude.
function formatGB(bytes) {
    return ((bytes || 0) / (1024 * 1024 * 1024)).toFixed(2);
}

function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// The Quality Scoring Guide card is byte-identical on the Inventory and Duplicates
// tabs (only the container id differs); render it into whichever container id is given
// instead of keeping two copies of the markup in index.html.
function renderScoringGuideCard(containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = `
        <div class="scoring-guide-header" onclick="document.getElementById('${containerId}').classList.toggle('collapsed')">
            <div style="display: flex; align-items: center; gap: 8px; font-weight: 600;">
                <span>📐</span> Quality Scoring Guide (TRaSH Guides Standard)
            </div>
            <svg class="chevron-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 9l-7 7-7-7"/></svg>
        </div>
        <div class="scoring-guide-body">
            <div class="scoring-table-grid">
                <div class="scoring-col">
                    <h4>🖥️ Resolution Base</h4>
                    <ul>
                        <li><strong>4K / 2160p</strong>: +1000 pts</li>
                        <li><strong>1080p</strong>: +500 pts</li>
                        <li><strong>720p</strong>: +200 pts</li>
                        <li><strong>SD</strong>: +50 pts</li>
                    </ul>
                </div>
                <div class="scoring-col">
                    <h4>💿 Source / Release</h4>
                    <ul>
                        <li><strong>Remux</strong>: +500 pts</li>
                        <li><strong>Bluray</strong>: +400 pts</li>
                        <li><strong>WEB-DL</strong>: +300 pts</li>
                        <li><strong>WEBRip</strong>: +200 pts</li>
                        <li><strong>HDTV</strong>: +100 pts</li>
                        <li><strong>DVD</strong>: +50 pts</li>
                        <li><strong>CAM / TS</strong>: -1000 pts</li>
                    </ul>
                </div>
                <div class="scoring-col">
                    <h4>🎨 Color Space & HDR</h4>
                    <ul>
                        <li><strong>DV + HDR10+</strong>: +300 pts</li>
                        <li><strong>Dolby Vision</strong>: +250 pts</li>
                        <li><strong>HDR10+</strong>: +200 pts</li>
                        <li><strong>HDR10</strong>: +150 pts</li>
                        <li><strong>SDR</strong>: +0 pts</li>
                    </ul>
                </div>
                <div class="scoring-col">
                    <h4>🔊 Audio Codec</h4>
                    <ul>
                        <li><strong>Atmos</strong>: +300 pts</li>
                        <li><strong>TrueHD/DTS-X</strong>: +250 pts</li>
                        <li><strong>DTS-HD</strong>: +200 pts</li>
                        <li><strong>DTS</strong>: +150 pts</li>
                        <li><strong>DD+ / EAC3</strong>: +100 pts</li>
                        <li><strong>DD / AC3</strong>: +50 pts</li>
                        <li><strong>AAC</strong>: +20 pts</li>
                    </ul>
                </div>
            </div>
            <div style="margin-top: 12px; font-size: 11px; color: var(--text-secondary); border-top: 1px solid var(--border-color); padding-top: 8px;">
                💡 <strong>Repack/Proper bonus</strong>: +50 pts. <strong>Bitrate tie-breaker</strong>: +5 pts per h264-equivalent Mbps (capped at +150) - HEVC/AV1 video counts double, VP9 ×1.6, VC-1 ×0.9, XviD/DivX ×0.7, MPEG-2 ×0.6, so an efficient re-encode isn't scored as the worse copy.
            </div>
        </div>
    `;
}

document.addEventListener('DOMContentLoaded', () => {
    renderScoringGuideCard('scoring-guide-inv');
    renderScoringGuideCard('scoring-guide-dup');
});

// Cast strip (avatar + name, with a name/character/bio tooltip) used on Calendar,
// Upcoming Movies/TV, Trending, My Requests, and the Media Detail modal.
function renderCastRow(cast) {
    if (!cast || cast.length === 0) return '';
    return `
        <div style="display: flex; gap: 0.6rem; margin-top: 0.6rem; flex-wrap: wrap;">
            ${cast.map(c => {
                const tooltip = `${c.name || ''}${c.character ? ' as ' + c.character : ''}${c.bio ? '\n\n' + c.bio : ''}`;
                return `
                    <div style="text-align: center; width: 56px;" title="${escapeHtml(tooltip)}">
                        ${c.photo_url
                            ? `<img src="${escapeHtml(c.photo_url)}" loading="lazy" decoding="async" style="width: 48px; height: 48px; border-radius: 50%; object-fit: cover; border: 1px solid var(--border-color);" onerror="this.style.display='none'">`
                            : `<div style="width: 48px; height: 48px; background: var(--accent-light); display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">🎭</div>`
                        }
                        <div style="font-size: 0.65rem; color: var(--text-secondary); margin-top: 2px; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(c.name)}</div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

// Plex/Emby/Jellyfin brand-ish colors for the small source badges below -
// shared across Inventory, the title detail modal, and the Admin playback table
// so a given player always reads the same everywhere.
const PLAYBACK_SOURCE_STYLES = {
    plex: { label: 'Plex', color: '#e5a00d' },
    emby: { label: 'Emby', color: '#52b54b' },
    jellyfin: { label: 'Jellyfin', color: '#aa5cc3' },
};

function renderSourceBadges(sources) {
    if (!sources || sources.length === 0) return '';
    return sources.map(s => {
        const style = PLAYBACK_SOURCE_STYLES[s] || { label: s, color: 'var(--text-secondary)' };
        return `<span style="font-size: 0.62rem; font-weight: 600; color: ${style.color}; border: 1px solid ${style.color}; border-radius: 3px; padding: 0 4px; margin-right: 3px; white-space: nowrap;">${escapeHtml(style.label)}</span>`;
    }).join('');
}

function formatPlaybackDate(iso) {
    if (!iso) return '';
    try {
        return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (e) {
        return '';
    }
}

// Small inline "N plays · Last: <date> · <player badges>" line, or "Never played"
// when playCount is 0/falsy. playCount/lastPlayedAt are the already-merged
// (max-across-sources) values the backend computes; sources is the list of
// players that recorded a play - see media_manager/web/services/playback_lookup.py.
// One shared renderer so Inventory cards, the title detail modal, and the Admin
// playback table all look identical instead of drifting per file.
function renderPlaybackBadge(playCount, lastPlayedAt, sources) {
    if (!playCount) {
        return `<div class="playback-badge muted" style="font-size: 0.72rem; margin-top: 0.35rem;">▶ Never played</div>`;
    }
    const dateStr = formatPlaybackDate(lastPlayedAt);
    return `
        <div class="playback-badge" style="font-size: 0.72rem; color: var(--text-secondary); margin-top: 0.35rem; display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
            <span>▶ ${playCount} play${playCount === 1 ? '' : 's'}${dateStr ? ' · Last: ' + dateStr : ''}</span>
            ${renderSourceBadges(sources)}
        </div>
    `;
}

window.showToast = function(message, type) {
    type = type || 'info';
    let host = document.getElementById('toast-stack');
    if (!host) {
        host = document.createElement('div');
        host.id = 'toast-stack';
        host.setAttribute('role', 'status');
        host.setAttribute('aria-live', 'polite');
        document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.textContent = String(message == null ? '' : message);
    host.appendChild(el);
    setTimeout(() => {
        el.classList.add('toast-out');
        setTimeout(() => el.remove(), 250);
    }, 4200);
};

window.renderWatcherHeartbeat = function(data, el) {
    if (!el) return;
    if (!data) {
        el.innerHTML = '';
        return;
    }
    const ok = !!data.alive;
    const label = ok ? 'Watcher heartbeat: alive' : 'Watcher heartbeat: stale / missing';
    const extra = data.hostname ? ` · ${escapeHtml(data.hostname)}` : '';
    const age = data.age_seconds != null ? ` · ${data.age_seconds}s ago` : '';
    el.innerHTML = `<span class="badge" style="background:${ok ? 'var(--success)' : 'var(--danger)'}; color:#fff;">${ok ? '●' : '○'} ${label}${extra}${age}</span>`;
    if (data.detail && !ok) {
        el.innerHTML += `<span class="muted" style="margin-left:8px;font-size:0.8rem;">${escapeHtml(data.detail)}</span>`;
    }
};

// Parse JSON from an API response. Gateway 504/502 pages are HTML, which used to
// surface as "JSON.parse: unexpected character at line 1 column 1".
async function parseApiJson(res) {
    const text = await res.text();
    try {
        return JSON.parse(text);
    } catch (e) {
        if (res.status === 504 || res.status === 502) {
            throw new Error('The server timed out. Try again in a moment.');
        }
        throw new Error(res.status ? `Request failed (${res.status}).` : e.message);
    }
}

function renderDiscoveryCard(item, opts) {
    const type = (opts.type || item.media_type) === 'tv' ? 'tv' : 'movie';
    const tmdbId = Number(item.tmdb_id);
    const open = `window.showMediaDetailModal({tmdb_id: ${tmdbId}, type: '${type}'})`;
    return `
        <article class="poster-tile">
            <img class="poster-tile-img" src="${item.poster_url || ''}" alt="" loading="lazy" decoding="async" onclick="${open}" onerror="this.style.display='none'">
            <div class="poster-tile-title" onclick="${open}">${escapeHtml(item.title)}</div>
            <div class="poster-tile-meta">${opts.metaHtml || ''}</div>
            <div class="poster-tile-action" id="${opts.actionId}">${opts.actionHtml || ''}</div>
        </article>`;
}

async function postAddByTmdb(item, opts, mediaType, onSuccess) {
    if (typeof opts !== 'object' || opts === null) opts = { searchNow: opts };
    const searchNow = opts.searchNow === undefined ? true : opts.searchNow;
    const type = mediaType || item.media_type || item.type || 'movie';
    const endpoint = type === 'movie' ? '/api/v1/integrations/add-by-tmdb' : '/api/v1/integrations/add-tv-by-tmdb';
    const body = {
        tmdb_id: item.tmdb_id,
        monitored: true,
        search_now: !!searchNow,
        quality_profile_id: opts.qualityProfileId || null,
    };
    if (type !== 'movie') body.season_monitor = opts.seasonMonitor || null;
    try {
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.success) {
            if (onSuccess) onSuccess();
            showToast(`Requested "${item.title || item.name}". Track it under My Requests.`, 'success');
        } else {
            showToast(`Failed to add title: ${data.error || data.detail || 'Unknown error'}`);
        }
    } catch (err) {
        showToast(`Error adding title: ${err.message}`);
    }
}

window.refreshWatcherHeartbeat = async function() {
    const els = document.querySelectorAll('.watcher-heartbeat');
    if (!els.length) return;
    try {
        const res = await fetch('/api/v1/integrations/watcher-status');
        const data = res.ok ? await res.json() : null;
        els.forEach(el => window.renderWatcherHeartbeat(data, el));
    } catch (e) {
        els.forEach(el => window.renderWatcherHeartbeat({ alive: false, stale: true, detail: 'Could not load watcher status.' }, el));
    }
};
