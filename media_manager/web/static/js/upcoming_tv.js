// Upcoming TV Series Premieres (powered by TMDb) - list & one-click add to Sonarr
window.loadUpcomingTv = function(days) {
    loadUpcomingTvList(days || 30);
};

function upcomingTvCalendarOnMonthChange(year, month) {
    window.upcomingCalendarGridDate = new Date(year, month, 1);
    loadUpcomingActiveType();
}

async function loadUpcomingTvList(days) {
    const isCalendar = window.activeUpcomingView === 'calendar';
    const container = document.getElementById(isCalendar ? 'upcoming-calendar-container' : 'upcoming-tv-container');
    if (!container) return;

    container.innerHTML = '<div class="loading-spinner">Loading upcoming series premieres...</div>';

    try {
        const query = isCalendar ? window.formatMonthDateRangeParams(window.upcomingCalendarGridDate) : `days=${days}`;
        const res = await fetch(`/api/v1/integrations/upcoming-tv?${query}`);
        const data = await res.json();

        if (!res.ok) {
            container.innerHTML = `<div class="section-card" style="text-align: center;"><p class="error-msg">${escapeHtml(data.detail || 'Failed to load upcoming TV shows.')}</p></div>`;
            return;
        }

        window._lastUpcomingTv = data.shows || [];
        if (isCalendar) {
            const items = (data.shows || []).map(s => ({ ...s, type: 'tv' }));
            window.renderMediaCalendarGrid(items, 'upcoming-calendar-container', { onMonthChange: upcomingTvCalendarOnMonthChange });
        } else {
            renderUpcomingTv(data);
        }
    } catch (err) {
        container.innerHTML = `<p class="error-msg">Failed to load upcoming TV shows: ${escapeHtml(err.message)}</p>`;
    }
}

function tvActionHtmlForStatus(show, idx) {
    if (show.status === 'in_library') {
        return '<span class="badge badge-4k" style="background-color: var(--success);">🟢 Already In Library</span>';
    }
    if (show.status === 'in_sonarr') {
        return '<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 Already In Sonarr</span>';
    }
    return `<button class="btn btn-primary btn-sm" onclick="promptAddUpcomingTv(${idx})">+ Add to Sonarr</button>`;
}

function renderUpcomingTv(data) {
    const container = document.getElementById('upcoming-tv-container');
    const shows = data.shows || [];

    if (shows.length === 0) {
        container.innerHTML = `
            <div class="section-card" style="text-align: center;">
                <h4>No confirmed premieres found</h4>
                <p class="muted">Nothing with a confirmed US premiere date in the next ${data.days} days.</p>
            </div>
        `;
        return;
    }

    window._lastUpcomingTv = shows;

    container.innerHTML = `<div class="poster-grid">${shows.map((s, idx) => {
        const dateObj = new Date(`${s.release_date}T00:00:00`);
        const formattedDate = dateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
        const whereToWatch = [...new Set([...(s.networks || []), ...(s.streaming_providers || [])])];
        const providerLabel = whereToWatch.length ? escapeHtml(whereToWatch.join(', ')) : 'Premiere';
        return renderDiscoveryCard(s, {
            type: 'tv',
            actionId: `upcoming-tv-action-${s.tmdb_id}`,
            actionHtml: tvActionHtmlForStatus(s, idx),
            metaHtml: `${providerLabel} · ${formattedDate}`,
        });
    }).join('')}</div>`;
}

function promptAddUpcomingTv(index) {
    const show = window._lastUpcomingTv[index];
    if (!show || !window.showAddMediaModal) return;

    window.showAddMediaModal({
        title: show.title,
        year: show.release_date ? show.release_date.slice(0, 4) : null,
        serviceName: 'Sonarr',
        releaseDate: show.release_date,
        onConfirm: (opts) => addUpcomingTvToSonarr(show, opts)
    });
}

async function addUpcomingTvToSonarr(show, opts) {
    await postAddByTmdb(show, opts, 'tv', () => {
        show.status = 'in_sonarr';
        const actionEl = document.getElementById(`upcoming-tv-action-${show.tmdb_id}`);
        if (actionEl) {
            actionEl.innerHTML = '<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 Already In Sonarr</span>';
        }
    });
}
