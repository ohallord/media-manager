// Inventory Grid, Category Tabs, TV Season Grouping & A-Z Quick Jump JS
let currentOffset = 0;
const limit = 100;
let selectedMediaType = 'movie';
let selectedLetter = '';

window.loadInventoryCounts = async function() {
    try {
        const res = await fetch('/api/v1/inventory/counts');
        if (!res.ok) return;
        const data = await res.json();
        const movieBadge = document.getElementById('count-movie');
        const tvBadge = document.getElementById('count-tv');
        if (movieBadge) movieBadge.textContent = data.movie_count.toLocaleString();
        if (tvBadge) tvBadge.textContent = data.tv_show_count.toLocaleString();
    } catch (err) {
        // Non-critical - leave badges blank on failure
    }
};

window.loadInventory = async function(offset = 0) {
    currentOffset = offset;
    const container = document.getElementById('inventory-container');
    if (!container) return;

    const q = document.getElementById('search-input')?.value || '';
    const resolution = document.getElementById('filter-resolution')?.value || '';

    const pageLimit = (selectedLetter || selectedMediaType === 'tv') ? 5000 : limit;
    let url = `/api/v1/inventory/items?limit=${pageLimit}&offset=${offset}`;
    if (q) url += `&q=${encodeURIComponent(q)}`;
    if (selectedMediaType) url += `&media_type=${encodeURIComponent(selectedMediaType)}`;
    if (resolution) url += `&resolution=${encodeURIComponent(resolution)}`;
    if (selectedLetter) url += `&letter=${encodeURIComponent(selectedLetter)}`;

    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`Failed to fetch inventory (${res.status})`);
        const data = await res.json();

        if (selectedMediaType === 'tv') {
            renderTVShowsBySeason(data.items, container);
            const paginationElem = document.getElementById('pagination');
            if (paginationElem) paginationElem.innerHTML = '';
        } else {
            renderInventoryGrid(data.items, container);
            renderPagination(data.total, limit, offset);
        }
    } catch (err) {
        container.innerHTML = `<div class="error-msg">Error loading inventory: ${err.message}</div>`;
    }
};

function renderInventoryGrid(items, container) {
    if (!items || items.length === 0) {
        container.innerHTML = '<p class="muted">No media items found matching query.</p>';
        return;
    }

    const displayItems = selectedMediaType ? items.filter(item => item.media_type === selectedMediaType) : items;

    if (!displayItems || displayItems.length === 0) {
        container.innerHTML = '<p class="muted">No media items found matching query.</p>';
        return;
    }

    container.innerHTML = displayItems.map(item => {
        const badgeClass = item.resolution === '4K' ? 'badge-4k' : (item.resolution === '1080p' ? 'badge-1080p' : 'badge-sd');
        const sizeMb = formatGB(item.size_bytes) + ' GB';
        const typeLabel = item.media_type === 'tv' ? `S${item.season || 0}E${item.episode || 0}` : 'Movie';

        const detailClick = item.tmdb_id
            ? ` style="cursor: pointer;" onclick="window.showMediaDetailModal({tmdb_id: ${item.tmdb_id}, type: '${item.media_type === 'tv' ? 'tv' : 'movie'}'})"`
            : '';
        return `
            <div class="media-card" id="media-item-${item.id}">
                <div id="art-${item.id}" class="media-art-container"${detailClick}></div>
                <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.4rem;">
                    <span class="badge ${badgeClass}">${item.resolution}</span>
                    <span style="font-size: 0.8rem; color: var(--text-secondary);">${typeLabel}</span>
                </div>
                <div class="media-title" title="${escapeHtml(item.title)}"${detailClick}>${escapeHtml(item.title)}</div>
                <div id="meta-${item.id}"></div>
                ${renderPlaybackBadge(item.play_count, item.last_played_at, item.play_sources)}
                <div class="media-specs mt-2">
                    <div><b>Codec:</b> ${item.video_codec.toUpperCase()} / ${item.audio_codec.toUpperCase()} (${item.audio_channels}ch)</div>
                    <div><b>Size:</b> ${sizeMb} | <b>Score:</b> ${item.quality_score} pts</div>
                    <div style="font-size: 0.75rem; overflow-wrap: break-word; word-break: break-word;" title="${escapeHtml(item.path)}"><b>Path:</b> ${escapeHtml(item.path)}</div>
                </div>
                <div style="margin-top: 0.75rem; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 0.5rem; text-align: right;">
                    <button class="btn btn-secondary" style="font-size: 0.75rem; padding: 4px 10px; color: #f87171; border-color: rgba(239,68,68,0.25);" onclick="deleteSingleDuplicateItem('${item.id}', '${escapeHtml(item.path.replace(/'/g, "\\'"))}')">
                        🗑️ Delete File
                    </button>
                </div>
            </div>
        `;
    }).join('');

    // Trigger async artwork & TMDb metadata load for rendered cards in batched chunks
    loadCardArtworkBatched(displayItems);
}

function paintInventoryArtBox(artElem, poster, backdrop, logo, source) {
    if (!artElem || (!poster && !backdrop && !logo)) return;
    const rawPoster = poster || backdrop;
    const posterImage = rawPoster ? `/api/v1/integrations/imgproxy?url=${encodeURIComponent(rawPoster)}&w=400&h=600` : '';
    artElem.innerHTML = posterImage
        ? `<img class="poster-tile-img" src="${posterImage}" alt="" loading="lazy" decoding="async">`
        : '';
}

function paintInventoryMeta(metaElem, meta) {
    if (!metaElem || !meta) return;
    if (!(meta.rating || (meta.genres && meta.genres.length > 0) || meta.overview || meta.trailer_url || meta.cast || meta.requested_by)) return;
    let badgeHtml = '';
    if (meta.rating) {
        badgeHtml += `<span style="color: #f1c40f; font-weight: bold; font-size: 0.8rem; margin-right: 6px;">⭐ ${meta.rating}/10</span> `;
    }
    if (meta.trailer_url) {
        badgeHtml += `<a href="${escapeHtml(meta.trailer_url)}" target="_blank" rel="noopener noreferrer" class="trailer-link">▶ Trailer</a> `;
    }
    if (meta.genres && meta.genres.length > 0) {
        badgeHtml += meta.genres.slice(0, 2).map(g => `<span class="genre-badge">${escapeHtml(g)}</span>`).join('');
    }
    let overviewHtml = meta.overview ? `<div style="font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.35rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;" title="${escapeHtml(meta.overview)}">${escapeHtml(meta.overview)}</div>` : '';
    let castHtml = '';
    if (meta.cast && meta.cast.length > 0) {
        if (typeof meta.cast[0] === 'object') {
            castHtml = renderCastRow(meta.cast.slice(0, 5));
        } else {
            castHtml = `<div style="font-size: 0.78rem; color: var(--text-secondary); margin-top: 0.35rem;"><b>Cast:</b> ${escapeHtml(meta.cast.slice(0, 5).join(', '))}</div>`;
        }
    }
    let requestedHtml = meta.requested_by
        ? `<div class="requested-by-badge" style="margin-top: 6px;">👤 Requested by: <strong>${escapeHtml(meta.requested_by)}</strong></div>`
        : '';
    metaElem.innerHTML = `<div style="margin-top: 0.35rem;">${badgeHtml}${overviewHtml}${castHtml}${requestedHtml}</div>`;
}

async function loadCardArtworkBatched(items) {
    const needFetch = [];
    for (const item of items) {
        if (item.poster_url || item.backdrop_url || item.overview || item.rating) {
            const artElem = document.getElementById(`art-${item.id}`);
            const metaElem = document.getElementById(`meta-${item.id}`);
            paintInventoryArtBox(artElem, item.poster_url, item.backdrop_url, null, 'tmdb');
            paintInventoryMeta(metaElem, {
                rating: item.rating,
                genres: item.genres,
                overview: item.overview,
                trailer_url: item.trailer_url,
                cast: item.cast,
                requested_by: item.requested_by,
            });
        } else {
            needFetch.push(item);
        }
    }
    const BATCH_SIZE = 8;
    for (let i = 0; i < needFetch.length; i += BATCH_SIZE) {
        const chunk = needFetch.slice(i, i + BATCH_SIZE);
        await Promise.all(chunk.map(item => loadCardArtwork(item.id, item.title, item.media_type, item.path)));
    }
}

// Session-lifetime cache of the /artwork + /metadata JSON for cards already rendered,
// keyed by title|type|path. The backend already caches both server-side, but skipping
// the round-trip entirely avoids re-fetching for cards that were already drawn this
// session (e.g. re-rendered by a debounced search, a filter change, or pagination).
const _inventoryArtMetaCache = new Map();

async function loadCardArtwork(itemId, title, mediaType, path) {
    const artElem = document.getElementById(`art-${itemId}`);
    const metaElem = document.getElementById(`meta-${itemId}`);
    const cacheKey = `${title}|${mediaType}|${path || ''}`;

    try {
        let data, metaData;
        const cached = _inventoryArtMetaCache.get(cacheKey);
        if (cached) {
            ({ data, metaData } = cached);
        } else {
            const pathParam = path ? `&path=${encodeURIComponent(path)}` : '';
            const [artRes, metaRes] = await Promise.all([
                fetch(`/api/v1/integrations/artwork?title=${encodeURIComponent(title)}&type=${encodeURIComponent(mediaType)}${pathParam}`),
                fetch(`/api/v1/integrations/metadata?title=${encodeURIComponent(title)}&type=${encodeURIComponent(mediaType)}${pathParam}`)
            ]);
            data = artRes.ok ? await artRes.json() : null;
            metaData = metaRes.ok ? await metaRes.json() : null;
            _inventoryArtMetaCache.set(cacheKey, { data, metaData });
        }

        if (data && artElem) {
            const art = data.artwork || {};
            if (art.poster || art.background || art.logo) {
                const rawBg = art.background || art.poster;
                const rawPoster = art.poster || art.background;
                const bgImage = `/api/v1/integrations/imgproxy?url=${encodeURIComponent(rawBg)}&w=600&h=300`;
                const posterImage = rawPoster ? `/api/v1/integrations/imgproxy?url=${encodeURIComponent(rawPoster)}&w=250&h=380` : '';
                const logoImage = art.logo ? `/api/v1/integrations/imgproxy?url=${encodeURIComponent(art.logo)}&w=300&h=150` : '';

                artElem.innerHTML = `
                    <div style="position: relative; height: 130px; border-radius: var(--radius-md); overflow: hidden; margin-bottom: 0.75rem; background: #000; border: 1px solid var(--border-color);">
                        <img src="${bgImage}" loading="lazy" decoding="async" style="width: 100%; height: 100%; object-fit: cover; opacity: 0.7; filter: brightness(0.9);" onerror="this.style.display='none'">
                        ${logoImage ? `<img src="${logoImage}" loading="lazy" decoding="async" style="position: absolute; bottom: 8px; left: 8px; max-height: 38px; max-width: 65%; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.9));">` : ''}
                        ${posterImage ? `<img src="${posterImage}" loading="lazy" decoding="async" style="position: absolute; top: 6px; right: 6px; height: 70px; border-radius: 4px; box-shadow: 0 4px 10px rgba(0,0,0,0.8); border: 1px solid rgba(255,255,255,0.2);">` : ''}
                        ${art.source ? `<span style="position: absolute; bottom: 4px; right: 6px; font-size: 0.65rem; color: #fff; background: rgba(0,0,0,0.6); padding: 1px 4px; border-radius: 3px;">🎨 ${art.source}</span>` : ''}
                    </div>
                `;
            }
        }

        if (metaData && metaElem) {
            const meta = metaData.metadata || {};
            if (meta.rating || (meta.genres && meta.genres.length > 0) || meta.overview || meta.trailer_url || meta.cast) {
                let badgeHtml = '';
                if (meta.rating) {
                    badgeHtml += `<span style="color: #f1c40f; font-weight: bold; font-size: 0.8rem; margin-right: 6px;">⭐ ${meta.rating}/10</span> `;
                }
                if (meta.trailer_url) {
                    badgeHtml += `<a href="${meta.trailer_url}" target="_blank" rel="noopener noreferrer" style="color: var(--text-secondary); font-weight: 600; text-decoration: none; border: 1px solid var(--border-color); border-radius: 999px; padding: 1px 8px; font-size: 0.7rem; margin-right: 6px;">▶ Trailer</a> `;
                }
                if (meta.genres && meta.genres.length > 0) {
                    badgeHtml += meta.genres.slice(0, 2).map(g => `<span style="font-size: 0.7rem; background: rgba(255,255,255,0.1); color: var(--text-secondary); padding: 1px 5px; border-radius: 3px; margin-right: 3px;">${escapeHtml(g)}</span>`).join('');
                }
                let overviewHtml = meta.overview ? `<div style="font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.35rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;" title="${escapeHtml(meta.overview)}">${escapeHtml(meta.overview)}</div>` : '';

                let castHtml = '';
                if (meta.cast && meta.cast.length > 0) {
                    if (typeof meta.cast[0] === 'object') {
                        castHtml = renderCastRow(meta.cast.slice(0, 5));
                    } else {
                        castHtml = `<div style="font-size: 0.78rem; color: var(--text-secondary); margin-top: 0.35rem;"><b>Cast:</b> ${escapeHtml(meta.cast.slice(0, 5).join(', '))}</div>`;
                    }
                }

                let requestedHtml = '';
                if (meta.requested_by) {
                    requestedHtml = `<div class="requested-by-badge" style="margin-top: 6px;">👤 Requested by: <strong>${escapeHtml(meta.requested_by)}</strong></div>`;
                }
                metaElem.innerHTML = `<div style="margin-top: 0.4rem; padding-top: 0.4rem; border-top: 1px dashed var(--border-color);">${badgeHtml}${overviewHtml}${castHtml}${requestedHtml}</div>`;
            }
        }
    } catch (err) {
        // Ignore artwork fetch errors
    }
}

function getShowNameFromPath(item) {
    if (!item || !item.path) return item.title || 'Unknown Series';
    const parts = item.path.split('/');
    const seasonIdx = parts.findIndex(p => /^season\s*\d+/i.test(p));
    if (seasonIdx > 0) {
        return parts[seasonIdx - 1];
    }
    if (parts.length >= 3) {
        const parentDir = parts[parts.length - 2];
        if (parentDir && !parentDir.toLowerCase().includes('tv') && !parentDir.toLowerCase().includes('mnt') && !parentDir.toLowerCase().includes('media')) {
            return parentDir;
        }
    }
    return item.title || 'Unknown Series';
}

function renderTVShowsBySeason(items, container) {
    if (!items || items.length === 0) {
        container.innerHTML = '<p class="muted">No TV shows found matching query.</p>';
        return;
    }

    // Group items by Show Title -> Season -> Episode
    const shows = {};
    items.forEach(item => {
        const showTitle = getShowNameFromPath(item);
        const seasonNum = item.season !== null ? item.season : 0;

        if (!shows[showTitle]) shows[showTitle] = {};
        if (!shows[showTitle][seasonNum]) shows[showTitle][seasonNum] = [];

        shows[showTitle][seasonNum].push(item);
    });

    container.className = 'tv-inventory-container'; // Switch grid to full-width block container for TV cards
    const sortedShowTitles = Object.keys(shows).sort();

    container.innerHTML = sortedShowTitles.map((showTitle, showIdx) => {
        const seasons = shows[showTitle];
        const seasonNums = Object.keys(seasons).sort((a, b) => Number(a) - Number(b));
        let totalEpisodes = 0;
        let totalPlays = 0;
        let latestPlayedAt = null;
        const showSources = new Set();
        seasonNums.forEach(s => {
            totalEpisodes += seasons[s].length;
            seasons[s].forEach(ep => {
                totalPlays += ep.play_count || 0;
                if (ep.last_played_at && (!latestPlayedAt || ep.last_played_at > latestPlayedAt)) {
                    latestPlayedAt = ep.last_played_at;
                }
                (ep.play_sources || []).forEach(src => showSources.add(src));
            });
        });

        const firstSeasonKey = seasonNums[0];
        const firstEp = (seasons[firstSeasonKey] || [])[0] || {};
        const showDetail = firstEp.tmdb_id
            ? ` style="cursor: pointer;" onclick="window.showMediaDetailModal({tmdb_id: ${firstEp.tmdb_id}, type: 'tv'})"`
            : '';
        return `
            <div class="tv-show-card mb-3" id="tv-show-card-${showIdx}">
                <div id="tv-show-art-${showIdx}" class="tv-show-art-banner"${showDetail}></div>
                <div class="tv-show-header">
                    <div style="width: 100%;">
                        <h3 style="font-size: 1.3rem; font-weight: 700; color: var(--text-primary);"${showDetail}>📺 ${escapeHtml(showTitle)}</h3>
                        <span class="muted" style="font-size: 0.85rem;">${seasonNums.length} Season${seasonNums.length === 1 ? '' : 's'} • ${totalEpisodes} Total Episode${totalEpisodes === 1 ? '' : 's'}</span>
                        <div style="display: flex; align-items: center; gap: 4px;"><b style="font-size: 0.72rem; color: var(--text-secondary);">Series Total:</b>${renderPlaybackBadge(totalPlays, latestPlayedAt, Array.from(showSources))}</div>
                        <div id="tv-show-meta-${showIdx}"></div>
                    </div>
                </div>
                ${seasonNums.map(sNum => {
                    const episodes = seasons[sNum].sort((a, b) => (a.episode || 0) - (b.episode || 0));
                    return `
                        <details class="season-block" style="margin-bottom: 0.75rem;" ontoggle="if(this.open){ this.querySelectorAll('[data-ep-id]').forEach(el => loadTVEpisodeMetadata(el.dataset.show, el.dataset.season, el.dataset.episode, el.dataset.epId)); }">
                            <summary class="season-title" style="cursor: pointer; user-select: none; font-size: 1.05rem; padding: 0.4rem 0.6rem; background: rgba(255,255,255,0.03); border-radius: var(--radius-sm, 4px); margin-bottom: 0.5rem;">
                                📁 <b>Season ${sNum == 0 ? 'Specials / Unknown' : sNum}</b> <span style="font-size: 0.85rem; font-weight: normal; color: var(--text-secondary);">(${episodes.length} Episode${episodes.length === 1 ? '' : 's'})</span>
                            </summary>
                            <div class="episode-grid mt-2">
                                ${episodes.map(ep => {
                                    const badgeClass = ep.resolution === '4K' ? 'badge-4k' : (ep.resolution === '1080p' ? 'badge-1080p' : 'badge-sd');
                                    const sizeMb = formatGB(ep.size_bytes) + ' GB';
                                    const epCode = `S${sNum.toString().padStart(2, '0')}E${(ep.episode !== null ? ep.episode : 0).toString().padStart(2, '0')}`;
                                    const safeShowTitle = showTitle.replace(/"/g, '&quot;');
                                    
                                    return `
                                        <div class="media-card" id="media-item-${ep.id}" style="margin: 0; padding: 1rem;">
                                            <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 0.4rem; margin-bottom: 0.5rem;">
                                                <span class="badge ${badgeClass}">${ep.resolution}</span>
                                                <span style="font-weight: 700; color: var(--accent-color); font-size: 0.85rem; background: var(--accent-light); padding: 2px 8px;">${epCode}</span>
                                            </div>
                                            <div style="font-weight: 600; font-size: 0.95rem; color: var(--text-primary); margin-bottom: 0.35rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${ep.title}">${ep.title || 'Episode ' + ep.episode}</div>
                                            ${renderPlaybackBadge(ep.play_count, ep.last_played_at, ep.play_sources)}
                                            <div id="ep-meta-${ep.id}" data-ep-id="${ep.id}" data-show="${safeShowTitle}" data-season="${sNum}" data-episode="${ep.episode || 0}"></div>

                                            <div class="media-specs">
                                                <div><b>Codec:</b> ${ep.video_codec ? ep.video_codec.toUpperCase() : 'N/A'} / ${ep.audio_codec ? ep.audio_codec.toUpperCase() : 'N/A'} (${ep.audio_channels || 0}ch)</div>
                                                <div><b>Size:</b> ${sizeMb} | <b>Score:</b> ${ep.quality_score} pts</div>
                                                <div style="font-size: 0.72rem; word-break: break-all; margin-top: 0.35rem; color: var(--text-secondary);" title="${ep.path}"><b>Path:</b> ${ep.path}</div>
                                            </div>
                                            <div style="margin-top: 0.75rem; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 0.5rem; text-align: right;">
                                                <button class="btn btn-secondary" style="font-size: 0.75rem; padding: 4px 10px; color: #f87171; border-color: rgba(239,68,68,0.25);" onclick="deleteSingleDuplicateItem('${ep.id}', '${ep.path.replace(/'/g, "\\'")}')">
                                                    🗑️ Delete File
                                                </button>
                                            </div>
                                        </div>
                                    `;
                                }).join('')}
                            </div>
                        </details>
                    `;
                }).join('')}
            </div>
        `;
    }).join('');

    // Trigger async artwork & TMDb metadata load for TV Show banners in batched chunks
    loadTVShowArtworkBatched(sortedShowTitles.map((showTitle) => {
        const seasons = shows[showTitle];
        const firstKey = Object.keys(seasons).sort((a, b) => Number(a) - Number(b))[0];
        return { showTitle, item: (seasons[firstKey] || [])[0] };
    }));
}

async function loadTVShowArtworkBatched(showSeeds) {
    const BATCH_SIZE = 6;
    for (let i = 0; i < showSeeds.length; i += BATCH_SIZE) {
        const chunk = showSeeds.slice(i, i + BATCH_SIZE);
        await Promise.all(chunk.map((seed, chunkIdx) => {
            const showIdx = i + chunkIdx;
            const item = seed.item || {};
            if (item.poster_url || item.backdrop_url || item.overview) {
                const artContainer = document.getElementById(`tv-show-art-${showIdx}`);
                const metaContainer = document.getElementById(`tv-show-meta-${showIdx}`);
                paintInventoryArtBox(artContainer, item.poster_url, item.backdrop_url, null, 'tmdb');
                paintInventoryMeta(metaContainer, {
                    rating: item.rating,
                    genres: item.genres,
                    overview: item.overview,
                    trailer_url: item.trailer_url,
                    cast: item.cast,
                    requested_by: item.requested_by,
                });
                return Promise.resolve();
            }
            return loadTVShowArtwork(showIdx, seed.showTitle);
        }));
    }
}

async function loadTVShowArtwork(showIdx, showTitle) {
    const artContainer = document.getElementById(`tv-show-art-${showIdx}`);
    const metaContainer = document.getElementById(`tv-show-meta-${showIdx}`);
    if (!artContainer) return;

    try {
        const [artRes, metaRes] = await Promise.all([
            fetch(`/api/v1/integrations/artwork?title=${encodeURIComponent(showTitle)}&type=tv`),
            fetch(`/api/v1/integrations/metadata?title=${encodeURIComponent(showTitle)}&type=tv`)
        ]);

        if (artRes.ok && artContainer) {
            const data = await artRes.json();
            const art = data.artwork || {};

            if (art.poster || art.background || art.logo) {
                const rawBg = art.background || art.poster;
                const rawPoster = art.poster || art.background;
                const bgImage = `/api/v1/integrations/imgproxy?url=${encodeURIComponent(rawBg)}&w=1200&h=400`;
                const posterImage = rawPoster ? `/api/v1/integrations/imgproxy?url=${encodeURIComponent(rawPoster)}&w=250&h=380` : '';
                const logoImage = art.logo ? `/api/v1/integrations/imgproxy?url=${encodeURIComponent(art.logo)}&w=400&h=200` : '';

                artContainer.innerHTML = `
                    <div style="position: relative; height: 160px; border-radius: var(--radius-md); overflow: hidden; margin-bottom: 1rem; background: #000; border: 1px solid var(--border-color);">
                        <img src="${bgImage}" loading="lazy" decoding="async" style="width: 100%; height: 100%; object-fit: cover; opacity: 0.65; filter: brightness(0.95);" onerror="this.style.display='none'">
                        ${logoImage ? `<img src="${logoImage}" loading="lazy" decoding="async" style="position: absolute; bottom: 12px; left: 12px; max-height: 50px; max-width: 60%; filter: drop-shadow(0 2px 6px rgba(0,0,0,0.9));">` : ''}
                        ${posterImage ? `<img src="${posterImage}" loading="lazy" decoding="async" style="position: absolute; top: 10px; right: 10px; height: 90px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.85); border: 1px solid rgba(255,255,255,0.2);">` : ''}
                        ${art.source ? `<span style="position: absolute; bottom: 6px; right: 10px; font-size: 0.7rem; color: #fff; background: rgba(0,0,0,0.7); padding: 2px 6px; border-radius: 3px;">🎨 ${art.source}</span>` : ''}
                    </div>
                `;
            }
        }

        if (metaRes.ok && metaContainer) {
            const metaData = await metaRes.json();
            const meta = metaData.metadata || {};
            if (meta.rating || (meta.genres && meta.genres.length > 0) || meta.overview || meta.release_date || meta.status) {
                let badgeHtml = '';
                if (meta.rating) {
                    badgeHtml += `<span style="color: #f1c40f; font-weight: bold; font-size: 0.85rem; margin-right: 8px;">⭐ ${meta.rating}/10</span> `;
                }
                if (meta.release_date) {
                    const year = meta.release_date.split('-')[0];
                    badgeHtml += `<span style="font-size: 0.8rem; color: var(--text-secondary); margin-right: 8px;">📅 ${year}${meta.status ? ' (' + meta.status + ')' : ''}</span> `;
                }
                if (meta.trailer_url) {
                    badgeHtml += `<a href="${meta.trailer_url}" target="_blank" rel="noopener noreferrer" style="color: var(--text-secondary); font-weight: 600; text-decoration: none; border: 1px solid var(--border-color); border-radius: 999px; padding: 1px 10px; font-size: 0.75rem; margin-right: 8px;">▶ Watch Trailer</a> `;
                }
                if (meta.genres && meta.genres.length > 0) {
                    badgeHtml += meta.genres.map(g => `<span class="genre-badge" style="margin-right: 4px;">${escapeHtml(g)}</span>`).join('');
                }
                let overviewHtml = meta.overview ? `<div style="font-size: 0.85rem; color: var(--text-secondary); margin-top: 0.5rem; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;" title="${escapeHtml(meta.overview)}">${escapeHtml(meta.overview)}</div>` : '';

                let castHtml = '';
                if (meta.cast && meta.cast.length > 0) {
                    if (typeof meta.cast[0] === 'object') {
                        castHtml = renderCastRow(meta.cast.slice(0, 5));
                    } else {
                        castHtml = `<div style="font-size: 0.78rem; color: var(--text-secondary); margin-top: 0.35rem;"><b>Cast:</b> ${escapeHtml(meta.cast.slice(0, 5).join(', '))}</div>`;
                    }
                }

                metaContainer.innerHTML = `<div style="margin-top: 0.5rem; padding-top: 0.5rem; border-top: 1px dashed var(--border-color);">${badgeHtml}${overviewHtml}${castHtml}</div>`;
            }
        }
    } catch (err) {}
}

function renderPagination(total, limit, offset) {
    const pagContainer = document.getElementById('pagination-container');
    if (!pagContainer) return;

    const totalPages = Math.ceil(total / limit);
    const currentPage = Math.floor(offset / limit) + 1;

    if (totalPages <= 1) {
        pagContainer.innerHTML = '';
        return;
    }

    pagContainer.innerHTML = `
        <div style="display: flex; gap: 0.5rem; justify-content: center; margin-top: 1.5rem;">
            <button class="btn btn-secondary btn-sm" ${offset === 0 ? 'disabled' : ''} onclick="window.loadInventory(${Math.max(0, offset - limit)})">Previous</button>
            <span style="align-self: center; font-size: 0.9rem;">Page ${currentPage} of ${totalPages} (${total} total)</span>
            <button class="btn btn-secondary btn-sm" ${offset + limit >= total ? 'disabled' : ''} onclick="window.loadInventory(${offset + limit})">Next</button>
        </div>
    `;
}

// Attach Event Listeners
document.addEventListener('DOMContentLoaded', () => {
    const searchInput = document.getElementById('search-input');
    const filterRes = document.getElementById('filter-resolution');
    const rescanBtn = document.getElementById('rescan-btn');

    // Type Tabs (All / Movies / TV Shows)
    document.querySelectorAll('#view-inventory .type-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#view-inventory .type-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            selectedMediaType = btn.dataset.type || '';
            const container = document.getElementById('inventory-container');
            if (container) container.className = selectedMediaType === 'tv' ? 'tv-inventory-container' : 'inventory-grid';
            window.loadInventory(0);
        });
    });

    // A-Z Quick Jump Strip (Syncs active state across desktop and mobile/iPad strips)
    document.querySelectorAll('.az-letter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            selectedLetter = btn.dataset.letter || '';
            document.querySelectorAll('.az-letter-btn').forEach(b => {
                if ((b.dataset.letter || '') === selectedLetter) {
                    b.classList.add('active');
                } else {
                    b.classList.remove('active');
                }
            });
            window.loadInventory(0);
        });
    });

    let debounceTimer;
    if (searchInput) {
        searchInput.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => window.loadInventory(0), 300);
        });
    }

    if (filterRes) filterRes.addEventListener('change', () => window.loadInventory(0));

    if (rescanBtn) {
        rescanBtn.addEventListener('click', async () => {
            rescanBtn.disabled = true;
            rescanBtn.innerHTML = '<span>Scanning...</span>';
            try {
                await fetch('/api/v1/inventory/scan', { method: 'POST' });
                showToast('Library scan initiated in background!');
            } catch (err) {
                showToast('Failed to start scan.');
            } finally {
                rescanBtn.disabled = false;
                rescanBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg><span>Rescan Library</span>';
            }
        });
    }
});

async function loadTVEpisodeMetadata(showTitle, season, episode, itemId) {
    const metaContainer = document.getElementById(`ep-meta-${itemId}`);
    if (!metaContainer || metaContainer.dataset.loaded) return;
    metaContainer.dataset.loaded = 'true';

    try {
        const res = await fetch(`/api/v1/integrations/episode-metadata?show_title=${encodeURIComponent(showTitle)}&season=${season}&episode=${episode}`);
        if (!res.ok) return;

        const data = await res.json();
        const meta = data.metadata || {};

        let content = '';

        if (meta.poster_path) {
            const proxiedStill = `/api/v1/integrations/imgproxy?url=${encodeURIComponent(meta.poster_path)}&w=400&h=225`;
            content += `<div style="position: relative; height: 95px; border-radius: 4px; overflow: hidden; margin: 0.4rem 0 0.5rem 0; background: #000; border: 1px solid var(--border-color);">
                <img src="${proxiedStill}" loading="lazy" decoding="async" style="width: 100%; height: 100%; object-fit: cover; opacity: 0.85;" onerror="this.parentElement.style.display='none'">
            </div>`;
        }

        let badges = [];
        if (meta.rating) badges.push(`<span style="color: #f1c40f; font-weight: bold;">⭐ ${meta.rating}/10</span>`);
        if (meta.release_date) badges.push(`<span style="color: var(--text-secondary);">📅 ${meta.release_date}</span>`);

        if (badges.length > 0) {
            content += `<div style="font-size: 0.78rem; margin-bottom: 0.35rem; display: flex; gap: 8px;">${badges.join(' | ')}</div>`;
        }

        if (meta.title && meta.title.toLowerCase() !== showTitle.toLowerCase()) {
            content += `<div style="font-weight: 600; font-size: 0.88rem; color: var(--accent-color); margin-bottom: 0.25rem;">"${meta.title}"</div>`;
        }

        if (meta.overview) {
            content += `<div style="font-size: 0.78rem; color: var(--text-secondary); line-height: 1.3; max-height: 3.6rem; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; margin-bottom: 0.4rem;" title="${meta.overview}">${meta.overview}</div>`;
        }

        if (content) {
            metaContainer.innerHTML = content;
        }
    } catch (e) {
        console.warn('Failed to fetch episode metadata:', e);
    }
}
