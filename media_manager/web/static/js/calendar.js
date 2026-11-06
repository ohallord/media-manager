// Calendar View JS - episodes/releases already tracked in Sonarr/Radarr
let activeCalendarView = window.matchMedia('(max-width: 768px)').matches ? 'list' : 'calendar';
let activeCalendarFilter = 'all';
// Month currently shown by the Calendar view. Only used in calendar mode -
// list mode always uses the backend's default -7/+23 day window instead.
let calendarGridDate = new Date();

window.loadCalendar = async function() {
    const container = document.getElementById('calendar-container');
    if (!container) return;
    container.innerHTML = `<div class="loading-spinner">Loading upcoming releases...</div>`;
    await fetchAndRenderCalendar(container);
};

async function fetchAndRenderCalendar(container) {
    try {
        const query = activeCalendarView === 'calendar' ? window.formatMonthDateRangeParams(calendarGridDate) : '';
        const url = query ? `/api/v1/integrations/calendar?${query}` : '/api/v1/integrations/calendar';
        const res = await fetch(url);
        const events = await res.json();

        window._lastCalendarEvents = events || [];
        renderCalendarShell(container);
    } catch (err) {
        container.innerHTML = `<p class="error-msg">Failed to load calendar: ${err.message}</p>`;
    }
}

function calendarOnMonthChange(year, month) {
    calendarGridDate = new Date(year, month, 1);
    const container = document.getElementById('calendar-container');
    if (container) fetchAndRenderCalendar(container);
}

function renderCalendarShell(container) {
    container.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1.5rem; border-bottom: 1px solid var(--border-color); padding-bottom: 1rem;">
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                <button class="btn btn-secondary btn-sm filter-cal${activeCalendarFilter === 'all' ? ' active' : ''}" data-filter="all">All</button>
                <button class="btn btn-secondary btn-sm filter-cal${activeCalendarFilter === 'movie' ? ' active' : ''}" data-filter="movie">🎬 Movies</button>
                <button class="btn btn-secondary btn-sm filter-cal${activeCalendarFilter === 'tv' ? ' active' : ''}" data-filter="tv">📺 TV Shows</button>
            </div>
            <div class="search-tabs-pills" style="margin: 0;">
                <button class="search-pill-btn calendar-view-tab-btn${activeCalendarView === 'list' ? ' active' : ''}" data-view="list">☰ List</button>
                <button class="search-pill-btn calendar-view-tab-btn${activeCalendarView === 'calendar' ? ' active' : ''}" data-view="calendar">🗓 Calendar</button>
            </div>
        </div>
        <div id="calendar-body"></div>
    `;

    container.querySelectorAll('.filter-cal').forEach(btn => {
        btn.addEventListener('click', () => {
            container.querySelectorAll('.filter-cal').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeCalendarFilter = btn.getAttribute('data-filter');
            renderCalendarBody();
        });
    });

    container.querySelectorAll('.calendar-view-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            container.querySelectorAll('.calendar-view-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeCalendarView = btn.dataset.view || 'list';
            // List and Calendar modes pull different date windows (a fixed
            // -7/+23 day range vs. whatever month is navigated to), so
            // switching modes needs a refetch, not just a re-render.
            fetchAndRenderCalendar(container);
        });
    });

    renderCalendarBody();
}

function filteredCalendarEvents() {
    const events = window._lastCalendarEvents || [];
    if (activeCalendarFilter === 'all') return events;
    return events.filter(ev => ev.type === activeCalendarFilter);
}

function calendarEventStatus(ev) {
    if (ev.has_file) return 'in_library';
    if (ev.monitored) return 'in_radarr';
    return 'not_added';
}

function renderCalendarBody() {
    const bodyEl = document.getElementById('calendar-body');
    if (!bodyEl) return;

    const events = filteredCalendarEvents();

    if (activeCalendarView === 'calendar') {
        bodyEl.innerHTML = '<div id="calendar-grid-container"></div>';
        const items = events.map(ev => ({
            tmdb_id: ev.tmdb_id,
            title: ev.title,
            release_date: ev.date,
            poster_url: ev.poster_url,
            status: calendarEventStatus(ev),
            type: ev.type
        }));
        window.renderMediaCalendarGrid(items, 'calendar-grid-container', { onMonthChange: calendarOnMonthChange });
    } else if (events.length === 0) {
        bodyEl.innerHTML = `<p class="muted" style="text-align: center; padding: 2rem;">No upcoming releases found in Radarr or Sonarr.</p>`;
    } else {
        bodyEl.innerHTML = `<div class="poster-grid" id="calendar-timeline">${events.map(ev => renderEventRow(ev)).join('')}</div>`;
    }
}

function renderEventRow(ev) {
    const hasFile = ev.has_file;
    const todayStr = new Date().toISOString().split('T')[0];
    let fileBadge = '';
    if (hasFile) {
        fileBadge = `<span class="calendar-status-badge downloaded">Downloaded</span>`;
    } else if (ev.date < todayStr) {
        fileBadge = `<span class="calendar-status-badge released">Released</span>`;
    } else {
        fileBadge = `<span class="calendar-status-badge upcoming">Upcoming</span>`;
    }

    const dateObj = new Date(ev.date + 'T00:00:00');
    const dayName = dateObj.toLocaleDateString('en-US', { weekday: 'short' });
    const formattedDate = dateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

    const canDetail = !!ev.tmdb_id;
    const detailType = ev.type === 'tv' ? 'tv' : 'movie';
    const detailClick = canDetail ? ` style="cursor: pointer;" onclick="window.showMediaDetailModal({tmdb_id: ${ev.tmdb_id}, type: '${detailType}'})"` : '';

    return `
        <article class="poster-tile" data-type="${ev.type}">
            ${ev.poster_url ? `<img class="poster-tile-img" src="${ev.poster_url}" alt="" loading="lazy" decoding="async" onerror="this.style.display='none'"${detailClick}>` : '<div class="poster-tile-img"></div>'}
            <div class="poster-tile-title" title="${escapeHtml(ev.title)}"${detailClick}>${escapeHtml(ev.title)}</div>
            <div class="poster-tile-meta">${escapeHtml(dayName)} ${escapeHtml(formattedDate.split(',')[0])} · ${fileBadge}</div>
        </article>
    `;
}

// renderCastRow is defined in utils.js (loaded first)
