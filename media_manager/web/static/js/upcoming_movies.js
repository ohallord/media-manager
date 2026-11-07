// Upcoming Theatrical Movies (powered by TMDb) - list & one-click add to Radarr
let activeUpcomingDays = 30;
let activeUpcomingType = 'movie';
window.activeUpcomingView = window.matchMedia('(max-width: 768px)').matches ? 'list' : 'calendar';

// Month currently shown by the Calendar view (shared across the Movies/TV
// type tabs - and with upcoming_tv.js via window, since flipping type while
// browsing a month should keep showing that same month). Only used in
// calendar mode - list mode always drives its window off activeUpcomingDays
// instead. window.formatMonthDateRangeParams is defined in media_calendar_grid.js.
window.upcomingCalendarGridDate = new Date();

const UPCOMING_HERO_COPY = {
    movie: {
        title: 'Coming Soon to Theaters & Streaming',
        subtitle: 'Upcoming theatrical and digital releases from Netflix, Apple TV+, Disney+, Hulu, and Paramount+.'
    },
    tv: {
        title: 'New Series Coming Soon',
        subtitle: 'New TV series premiering soon that you don’t already have in Sonarr. For episode/season air dates on shows you already track, see Calendar.'
    }
};

document.addEventListener('DOMContentLoaded', () => {
    // Reflect the viewport-based default view (see above) in the toggle buttons
    // and container visibility before the first load fires.
    document.querySelectorAll('.upcoming-view-tab-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === window.activeUpcomingView);
    });
    updateUpcomingContainerVisibility();

    document.querySelectorAll('.upcoming-days-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.upcoming-days-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeUpcomingDays = parseInt(btn.dataset.days, 10) || 30;
            loadUpcomingActiveType();
        });
    });

    document.querySelectorAll('.upcoming-type-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.upcoming-type-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeUpcomingType = btn.dataset.upcomingType || 'movie';

            const copy = UPCOMING_HERO_COPY[activeUpcomingType];
            document.getElementById('upcoming-hero-title').textContent = copy.title;
            document.getElementById('upcoming-hero-subtitle').textContent = copy.subtitle;

            updateUpcomingContainerVisibility();
            loadUpcomingActiveType();
        });
    });

    document.querySelectorAll('.upcoming-view-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.upcoming-view-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            window.activeUpcomingView = btn.dataset.view || 'list';

            updateUpcomingContainerVisibility();
            loadUpcomingActiveType();
        });
    });
});

function updateUpcomingContainerVisibility() {
    const showList = window.activeUpcomingView === 'list';
    document.getElementById('upcoming-movies-container').style.display = (showList && activeUpcomingType === 'movie') ? '' : 'none';
    document.getElementById('upcoming-tv-container').style.display = (showList && activeUpcomingType === 'tv') ? '' : 'none';
    document.getElementById('upcoming-calendar-container').style.display = showList ? 'none' : '';
    // The day-window pills (30/60/90/120) only apply to List mode - Calendar mode
    // pulls whatever month is currently navigated to instead.
    const daysPills = document.getElementById('upcoming-days-pills');
    if (daysPills) daysPills.style.display = showList ? '' : 'none';
}

function loadUpcomingActiveType() {
    if (activeUpcomingType === 'tv') {
        window.loadUpcomingTv(activeUpcomingDays);
    } else {
        loadUpcomingMoviesList();
    }
}

window.loadUpcomingMovies = function() {
    loadUpcomingActiveType();
};

function upcomingCalendarOnMonthChange(year, month) {
    window.upcomingCalendarGridDate = new Date(year, month, 1);
    loadUpcomingActiveType();
}

async function loadUpcomingMoviesList() {
    const isCalendar = window.activeUpcomingView === 'calendar';
    const container = document.getElementById(isCalendar ? 'upcoming-calendar-container' : 'upcoming-movies-container');
    if (!container) return;

    container.innerHTML = '<div class="loading-spinner">Loading upcoming theatrical releases...</div>';

    try {
        const query = isCalendar ? window.formatMonthDateRangeParams(window.upcomingCalendarGridDate) : `days=${activeUpcomingDays}`;
        const res = await fetch(`/api/v1/integrations/upcoming-movies?${query}`);
        const data = await res.json();

        if (!res.ok) {
            container.innerHTML = `<div class="section-card" style="text-align: center;"><p class="error-msg">${escapeHtml(data.detail || 'Failed to load upcoming movies.')}</p></div>`;
            return;
        }

        window._lastUpcomingMovies = data.movies || [];
        if (isCalendar) {
            const items = (data.movies || []).map(m => ({ ...m, type: 'movie' }));
            window.renderMediaCalendarGrid(items, 'upcoming-calendar-container', { onMonthChange: upcomingCalendarOnMonthChange });
        } else {
            renderUpcomingMovies(data);
        }
    } catch (err) {
        container.innerHTML = `<p class="error-msg">Failed to load upcoming movies: ${escapeHtml(err.message)}</p>`;
    }
}

function actionHtmlForStatus(movie, idx) {
    if (movie.status === 'in_library') {
        return '<span class="badge badge-4k" style="background-color: var(--success);">🟢 Already In Library</span>';
    }
    if (movie.status === 'in_radarr') {
        return '<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 Already In Radarr</span>';
    }
    return `<button class="btn btn-primary btn-sm" onclick="promptAddUpcomingMovie(${idx})">+ Add to Radarr</button>`;
}

// renderCastRow is defined in utils.js (loaded first)

function renderUpcomingMovies(data) {
    const container = document.getElementById('upcoming-movies-container');
    const movies = data.movies || [];

    if (movies.length === 0) {
        container.innerHTML = `
            <div class="section-card" style="text-align: center;">
                <h4>No confirmed releases found</h4>
                <p class="muted">Nothing with a confirmed theatrical or streaming release date in the next ${data.days} days.</p>
            </div>
        `;
        return;
    }

    window._lastUpcomingMovies = movies;

    container.innerHTML = `<div class="poster-grid">${movies.map((m, idx) => {
        const dateObj = new Date(`${m.release_date}T00:00:00`);
        const formattedDate = dateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
        const releaseLabel = m.release_medium === 'Streaming' ? (m.release_location || 'Streaming') : 'In theaters';
        return renderDiscoveryCard(m, {
            type: 'movie',
            actionId: `upcoming-action-${m.tmdb_id}`,
            actionHtml: actionHtmlForStatus(m, idx),
            metaHtml: `${escapeHtml(releaseLabel)} · ${formattedDate}`,
        });
    }).join('')}</div>`;
}

function promptAddUpcomingMovie(index) {
    const movie = window._lastUpcomingMovies[index];
    if (!movie || !window.showAddMediaModal) return;

    window.showAddMediaModal({
        title: movie.title,
        year: movie.release_date ? movie.release_date.slice(0, 4) : null,
        serviceName: 'Radarr',
        releaseDate: movie.release_date,
        onConfirm: (opts) => addUpcomingMovieToRadarr(movie, opts)
    });
}

async function addUpcomingMovieToRadarr(movie, opts) {
    await postAddByTmdb(movie, opts, 'movie', () => {
        movie.status = 'in_radarr';
        const actionEl = document.getElementById(`upcoming-action-${movie.tmdb_id}`);
        if (actionEl) {
            actionEl.innerHTML = '<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 Already In Radarr</span>';
        }
    });
}
