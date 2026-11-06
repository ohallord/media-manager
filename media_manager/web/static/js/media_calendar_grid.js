// Shared month-grid calendar renderer (Radarr/Sonarr-style), used by both the
// Coming Soon page (movies/shows not yet added) and the Calendar page
// (episodes/releases already tracked in Sonarr/Radarr). Each item is a plain
// object: { tmdb_id, title, release_date, poster_url, status, type }. `type`
// ('movie' | 'tv') travels per-item so a single grid can mix both, as the
// Calendar page's combined feed does.
let mediaCalendarRefDate = null; // set to the 1st of the currently-shown month

function mediaCalendarBorderColor(status) {
    if (status === 'in_library') return 'var(--success)';
    if (status === 'in_radarr' || status === 'in_sonarr') return 'var(--accent-color)';
    return 'var(--border-color)';
}

function resetMediaCalendarToToday() {
    mediaCalendarRefDate = new Date();
    mediaCalendarRefDate.setHours(0, 0, 0, 0);
    mediaCalendarRefDate.setDate(1);
}

// Shared by every page that fetches per-navigated-month data (Coming Soon,
// Calendar): builds the start_date/end_date query params for the whole month
// containing refDate.
window.formatMonthDateRangeParams = function(refDate) {
    const start = new Date(refDate.getFullYear(), refDate.getMonth(), 1);
    const end = new Date(refDate.getFullYear(), refDate.getMonth() + 1, 0);
    const fmt = d => d.toISOString().slice(0, 10);
    return `start_date=${fmt(start)}&end_date=${fmt(end)}`;
};

// items: see file header. containerId: element to render into. opts.onMonthChange
// (year, month), when given, is called instead of self-rendering on prev/next/today -
// used by pages (Coming Soon) whose data is scoped to a lookahead window and needs
// re-fetching per navigated month rather than a page already holding everything.
// Pages with a fixed, already-fully-loaded dataset (the Calendar page) omit it and
// get the default behavior of just re-rendering from the same items on navigation.
window.renderMediaCalendarGrid = function(items, containerId, opts) {
    window._lastMediaCalendarItems = items || [];
    window._lastMediaCalendarContainerId = containerId;
    window._lastMediaCalendarOpts = opts || {};
    if (!mediaCalendarRefDate) resetMediaCalendarToToday();
    renderMediaCalendarGridInternal();
};

function notifyMediaCalendarMonthChangeOrRender() {
    const opts = window._lastMediaCalendarOpts || {};
    if (opts.onMonthChange) {
        opts.onMonthChange(mediaCalendarRefDate.getFullYear(), mediaCalendarRefDate.getMonth());
    } else {
        renderMediaCalendarGridInternal();
    }
}

window.shiftMediaCalendarMonth = function(delta) {
    mediaCalendarRefDate.setMonth(mediaCalendarRefDate.getMonth() + delta);
    notifyMediaCalendarMonthChangeOrRender();
};

window.jumpMediaCalendarToToday = function() {
    resetMediaCalendarToToday();
    notifyMediaCalendarMonthChangeOrRender();
};

function renderMediaCalendarGridInternal() {
    const containerId = window._lastMediaCalendarContainerId;
    const container = containerId && document.getElementById(containerId);
    if (!container) return;

    const items = window._lastMediaCalendarItems || [];

    const byDate = {};
    items.forEach((it) => {
        if (!it.release_date) return;
        const key = it.release_date.slice(0, 10);
        (byDate[key] = byDate[key] || []).push(it);
    });

    const year = mediaCalendarRefDate.getFullYear();
    const month = mediaCalendarRefDate.getMonth();
    const monthLabel = mediaCalendarRefDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });

    const firstOfMonth = new Date(year, month, 1);
    const startOffset = firstOfMonth.getDay(); // 0 = Sunday
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const gridStart = new Date(year, month, 1 - startOffset);

    const todayStr = new Date().toISOString().split('T')[0];
    const weeksNeeded = Math.ceil((startOffset + daysInMonth) / 7);

    const dayHeaders = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
        .map(d => `<div class="upcal-weekday">${d}</div>`).join('');

    const cells = [];
    for (let i = 0; i < weeksNeeded * 7; i++) {
        const cellDate = new Date(gridStart);
        cellDate.setDate(gridStart.getDate() + i);
        const cellKey = cellDate.toISOString().split('T')[0];
        const inMonth = cellDate.getMonth() === month;
        const dayItems = byDate[cellKey] || [];

        const chips = dayItems.slice(0, 3).map(it => {
            const clickable = !!it.tmdb_id;
            const onclick = clickable ? `window.showMediaDetailModal({tmdb_id: ${it.tmdb_id}, type: '${it.type || 'movie'}'})` : '';
            return `
                <div class="upcal-item${clickable ? '' : ' upcal-item-static'}" style="border-left-color: ${mediaCalendarBorderColor(it.status)};" ${onclick ? `onclick="${onclick}"` : ''}>
                    ${it.poster_url ? `<img src="${it.poster_url}" loading="lazy" decoding="async" alt="" onerror="this.style.display='none'">` : ''}
                    <span title="${(it.title || '').replace(/"/g, '&quot;')}">${it.title}</span>
                </div>
            `;
        }).join('');
        const more = dayItems.length > 3 ? `<div class="upcal-more">+${dayItems.length - 3} more</div>` : '';

        cells.push(`
            <div class="upcal-day${inMonth ? '' : ' upcal-other-month'}${cellKey === todayStr ? ' upcal-today' : ''}">
                <div class="upcal-day-num">${cellDate.getDate()}</div>
                <div class="upcal-day-items">${chips}${more}</div>
            </div>
        `);
    }

    const header = `
        <div class="upcal-header">
            <button class="btn btn-secondary btn-sm" onclick="window.shiftMediaCalendarMonth(-1)">‹ Prev</button>
            <h4 class="upcal-month-label">${monthLabel}</h4>
            <div style="display: flex; gap: 0.5rem;">
                <button class="btn btn-secondary btn-sm" onclick="window.jumpMediaCalendarToToday()">Today</button>
                <button class="btn btn-secondary btn-sm" onclick="window.shiftMediaCalendarMonth(1)">Next ›</button>
            </div>
        </div>
    `;

    if (items.length === 0) {
        container.innerHTML = `
            ${header}
            <div class="section-card" style="text-align: center;">
                <p class="muted">No confirmed releases found in this window.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = `
        ${header}
        <div class="upcal-grid">
            ${dayHeaders}
            ${cells.join('')}
        </div>
    `;
}
