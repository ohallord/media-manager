// Trending Now (powered by TMDb) - popular movies/TV independent of release date
let activeTrendingType = 'movie';
let activeTrendingWindow = 'week';

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.trending-type-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.trending-type-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeTrendingType = btn.dataset.trendingType || 'movie';
            loadTrendingList();
        });
    });

    document.querySelectorAll('.trending-window-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.trending-window-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeTrendingWindow = btn.dataset.trendingWindow || 'week';
            loadTrendingList();
        });
    });
});

window.loadTrending = function() {
    loadTrendingList();
};

async function loadTrendingList() {
    const container = document.getElementById('trending-container');
    if (!container) return;

    container.innerHTML = '<div class="loading-spinner">Loading trending titles...</div>';

    try {
        const res = await fetch(`/api/v1/integrations/trending?media_type=${activeTrendingType}&window=${activeTrendingWindow}`);
        const data = await parseApiJson(res);

        if (!res.ok) {
            container.innerHTML = `<div class="section-card" style="text-align: center;"><p class="error-msg">${escapeHtml(data.detail || 'Failed to load trending titles.')}</p></div>`;
            return;
        }

        renderTrending(data);
    } catch (err) {
        container.innerHTML = `<p class="error-msg">Failed to load trending titles: ${escapeHtml(err.message)}</p>`;
    }
}

function trendingActionHtmlForStatus(item, idx) {
    if (item.status === 'in_library') {
        return '<span class="badge badge-4k" style="background-color: var(--success);">🟢 Already In Library</span>';
    }
    if (item.status === 'in_radarr') {
        return '<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 Already In Radarr</span>';
    }
    if (item.status === 'in_sonarr') {
        return '<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 Already In Sonarr</span>';
    }
    return `<button class="btn btn-primary btn-sm" onclick="promptAddTrendingItem(${idx})">+ Add</button>`;
}

function renderTrending(data) {
    const container = document.getElementById('trending-container');
    const items = data.items || [];

    if (items.length === 0) {
        container.innerHTML = `
            <div class="section-card" style="text-align: center;">
                <h4>Nothing trending right now</h4>
                <p class="muted">TMDb didn't return any trending titles for this window.</p>
            </div>
        `;
        return;
    }

    window._lastTrendingItems = items;

    container.innerHTML = `<div class="poster-grid">${items.map((item, idx) => {
        const dateObj = item.release_date ? new Date(`${item.release_date}T00:00:00`) : null;
        const formattedDate = dateObj && !isNaN(dateObj) ? dateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
        return renderDiscoveryCard(item, {
            type: item.media_type,
            actionId: `trending-action-${item.tmdb_id}`,
            actionHtml: trendingActionHtmlForStatus(item, idx),
            metaHtml: formattedDate ? `Trending · ${formattedDate}` : 'Trending',
        });
    }).join('')}</div>`;
}

function promptAddTrendingItem(index) {
    const item = window._lastTrendingItems[index];
    if (!item || !window.showAddMediaModal) return;

    const serviceName = item.media_type === 'movie' ? 'Radarr' : 'Sonarr';
    window.showAddMediaModal({
        title: item.title,
        year: item.release_date ? item.release_date.slice(0, 4) : null,
        serviceName,
        releaseDate: item.release_date,
        onConfirm: (opts) => addTrendingItemToArr(item, opts)
    });
}

async function addTrendingItemToArr(item, opts) {
    await postAddByTmdb(item, opts, item.media_type, () => {
        item.status = item.media_type === 'movie' ? 'in_radarr' : 'in_sonarr';
        const actionEl = document.getElementById(`trending-action-${item.tmdb_id}`);
        if (actionEl) {
            const label = item.media_type === 'movie' ? 'Already In Radarr' : 'Already In Sonarr';
            actionEl.innerHTML = `<span class="badge" style="background-color: var(--accent-color); color: #fff;">📥 ${label}</span>`;
        }
    });
}
