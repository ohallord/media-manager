// My Requests - status of the current user's own past media requests
window.loadMyRequests = async function() {
    const container = document.getElementById('my-requests-container');
    if (!container) return;

    container.innerHTML = '<div class="loading-spinner">Loading your requests...</div>';

    try {
        const [reqRes, qRes] = await Promise.all([
            fetch('/api/v1/integrations/my-requests'),
            fetch('/api/v1/integrations/download-queue'),
        ]);
        const data = await reqRes.json();

        if (!reqRes.ok) {
            container.innerHTML = `<div class="section-card" style="text-align: center;"><p class="error-msg">${escapeHtml(data.detail || 'Failed to load your requests.')}</p></div>`;
            return;
        }

        let queue = [];
        if (qRes.ok) {
            const qData = await qRes.json();
            queue = qData.queue || [];
        }
        const byTitle = {};
        queue.forEach(q => {
            const key = (q.title || '').toLowerCase().split(' - s')[0].trim();
            if (key) byTitle[key] = q;
        });
        (data.requests || []).forEach(r => {
            const q = byTitle[(r.title || '').toLowerCase().trim()];
            if (q) {
                r.queue_progress = q.progress_pct;
                r.queue_status = q.status;
                if (r.status === 'requested' || r.status === 'downloading') {
                    r.status = 'downloading';
                }
            }
        });

        renderMyRequests(data);
    } catch (err) {
        container.innerHTML = `<p class="error-msg">Failed to load your requests: ${escapeHtml(err.message)}</p>`;
    }
};

function myRequestStatusBadge(status) {
    if (status === 'available') {
        return '<span class="badge badge-4k" style="background-color: var(--success);">🟢 Available</span>';
    }
    if (status === 'downloading') {
        return '<span class="badge" style="background-color: var(--accent-color); color: #fff;">⬇️ Downloading</span>';
    }
    return '<span class="badge" style="background-color: var(--text-secondary); color: #fff;">🔍 Requested</span>';
}

function renderMyRequests(data) {
    const container = document.getElementById('my-requests-container');
    const requests = data.requests || [];

    if (requests.length === 0) {
        container.innerHTML = `
            <div class="section-card" style="text-align: center;">
                <h4>No requests yet</h4>
                <p class="muted">Anything you request from Search or Coming Soon will show up here with its status.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = `<div class="poster-grid">${requests.map(r => {
        const dateObj = new Date(r.requested_at);
        const formattedDate = dateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
        const detailOnclick = r.tmdb_id ? `onclick="window.showMediaDetailModal({tmdb_id: ${r.tmdb_id}, type: '${r.media_type}'})"` : '';
        const progress = r.status === 'downloading' && r.queue_progress != null ? ` · ${r.queue_progress}%` : '';
        return `
            <article class="poster-tile my-request-card">
                ${r.poster_url ? `<img class="poster-tile-img" src="${r.poster_url}" alt="" loading="lazy" decoding="async" ${detailOnclick} onerror="this.style.display='none'">` : '<div class="poster-tile-img"></div>'}
                <div class="poster-tile-title" ${detailOnclick}>${escapeHtml(r.title)}</div>
                <div class="poster-tile-meta">Requested ${formattedDate}${progress}</div>
                <div class="poster-tile-action">${myRequestStatusBadge(r.status)}</div>
            </article>
        `;
    }).join('')}</div>`;
}
