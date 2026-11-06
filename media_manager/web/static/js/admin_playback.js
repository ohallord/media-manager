// Admin "Playback" table: a flat, sortable, read-only list of every movie and
// TV series with its merged play stats - reuses the existing /api/v1/playback
// endpoints rather than a new one. No delete/mutation actions yet (explicit
// constraint - automated pruning is a separate, not-yet-built feature).
let _adminPlaybackRows = [];
let _adminPlaybackSort = { key: 'name', dir: 'asc' };
let _adminPlaybackQuery = '';

window.loadAdminPlayback = async function() {
    const container = document.getElementById('admin-playback-list');
    if (!container) return;
    container.innerHTML = '<div class="loading-spinner">Loading playback stats...</div>';

    try {
        const [itemsRes, seriesRes] = await Promise.all([
            fetch('/api/v1/playback/items?media_type=movie&limit=5000'),
            fetch('/api/v1/playback/series')
        ]);
        if (!itemsRes.ok || !seriesRes.ok) throw new Error('Failed to load playback data.');

        const itemsData = await itemsRes.json();
        const seriesData = await seriesRes.json();

        _adminPlaybackRows = [
            ...itemsData.items.map(i => ({
                name: i.title,
                type: 'movie',
                play_count: i.play_count || 0,
                last_played_at: i.last_played_at,
                sources: (i.sources || []).filter(s => s.play_count > 0).map(s => s.source),
                added_at: i.added_at,
            })),
            ...seriesData.series.map(s => ({
                name: s.show_name,
                type: 'show',
                play_count: s.total_plays || 0,
                last_played_at: s.last_played_at,
                sources: s.sources || [],
                added_at: s.added_at,
            })),
        ];

        renderAdminPlaybackTable(container);
    } catch (err) {
        container.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
    }
};

function _sortAdminPlaybackRows(rows) {
    const { key, dir } = _adminPlaybackSort;
    const mult = dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
        let av = a[key];
        let bv = b[key];
        if (key === 'sources') {
            av = (av || []).join(', ');
            bv = (bv || []).join(', ');
        }
        if (av === null || av === undefined) av = '';
        if (bv === null || bv === undefined) bv = '';
        if (typeof av === 'string') av = av.toLowerCase();
        if (typeof bv === 'string') bv = bv.toLowerCase();
        if (av < bv) return -1 * mult;
        if (av > bv) return 1 * mult;
        return 0;
    });
}

const ADMIN_PLAYBACK_COLUMNS = [
    { key: 'name', label: 'Media Name' },
    { key: 'type', label: 'Type' },
    { key: 'play_count', label: 'Total Play Count' },
    { key: 'last_played_at', label: 'Last Play' },
    { key: 'sources', label: 'Player(s) Played On' },
    { key: 'added_at', label: 'Added to Library' },
];

function renderAdminPlaybackTable(container) {
    const q = _adminPlaybackQuery.trim().toLowerCase();
    const rows = _sortAdminPlaybackRows(_adminPlaybackRows).filter(r => !q || String(r.name || '').toLowerCase().includes(q));

    const headerHtml = ADMIN_PLAYBACK_COLUMNS.map(col => {
        const active = _adminPlaybackSort.key === col.key;
        const arrow = active ? (_adminPlaybackSort.dir === 'asc' ? ' ▲' : ' ▼') : '';
        return `<th style="padding: 0.6rem 0.5rem; cursor: pointer; user-select: none; white-space: nowrap;" onclick="window.sortAdminPlayback('${col.key}')">${col.label}${arrow}</th>`;
    }).join('');

    const rowsHtml = rows.map(r => `
        <tr style="border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.85rem;">
            <td style="padding: 0.6rem 0.5rem;"><b>${escapeHtml(r.name)}</b></td>
            <td style="padding: 0.6rem 0.5rem;"><span class="badge ${r.type === 'movie' ? 'badge-1080p' : 'badge-4k'}">${r.type === 'movie' ? 'Movie' : 'Show'}</span></td>
            <td style="padding: 0.6rem 0.5rem;">${r.play_count}</td>
            <td style="padding: 0.6rem 0.5rem; color: var(--text-secondary);">${formatPlaybackDate(r.last_played_at) || 'Never'}</td>
            <td style="padding: 0.6rem 0.5rem;">${renderSourceBadges(r.sources) || '<span class="muted">—</span>'}</td>
            <td style="padding: 0.6rem 0.5rem; color: var(--text-secondary);">${formatPlaybackDate(r.added_at) || '—'}</td>
        </tr>
    `).join('');

    container.innerHTML = `
        <div class="muted mb-2" style="font-size: 0.85rem;">${rows.length.toLocaleString()} shown</div>
        <div class="playback-scroll responsive-table-wrapper">
            <table style="width: 100%; border-collapse: collapse;">
                <thead>
                    <tr style="border-bottom: 1px solid var(--border-color); text-align: left;" class="muted">${headerHtml}</tr>
                </thead>
                <tbody>${rowsHtml || '<tr><td colspan="6" class="muted" style="padding: 1rem;">No playback data yet - connect Plex/Emby/Jellyfin and run a sync.</td></tr>'}</tbody>
            </table>
        </div>
    `;
}

window.filterAdminPlayback = function(value) {
    _adminPlaybackQuery = value || '';
    const container = document.getElementById('admin-playback-list');
    if (container) renderAdminPlaybackTable(container);
};

window.sortAdminPlayback = function(key) {
    if (_adminPlaybackSort.key === key) {
        _adminPlaybackSort.dir = _adminPlaybackSort.dir === 'asc' ? 'desc' : 'asc';
    } else {
        // Numeric/date columns read better sorted highest/most-recent first on
        // the initial click; name/type default to ascending (A-Z).
        _adminPlaybackSort = { key, dir: (key === 'name' || key === 'type') ? 'asc' : 'desc' };
    }
    const container = document.getElementById('admin-playback-list');
    if (container) renderAdminPlaybackTable(container);
};
