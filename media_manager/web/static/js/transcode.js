// H.265 Migration page (admin): probe backfill status, encode settings, the
// dry-run savings report, the top candidates, and the job queue (pilot/full,
// start/pause). Files are only changed by the host worker, never this page.
const TRANSCODE_CODEC_CHOICES = ['h264', 'mpeg4', 'msmpeg4v3', 'msmpeg4v2', 'msmpeg4v1', 'vc1', 'mpeg2video'];
const TRANSCODE_SETTING_LABELS = {
    enabled: 'Convert this media type',
    cq_1080: 'Quality 1080p (CQ, lower = better)',
    cq_720: 'Quality 720p (CQ)',
    cq_sd: 'Quality SD (CQ)',
    cq_4k_downscale: 'Quality 4K→1080p (CQ)',
    preset: 'NVENC preset (p1 fast … p7 best)',
    multipass: 'Multipass (disabled / qres / fullres)',
    output_10bit: '10-bit output (less banding, smaller)',
    min_savings_pct: 'Keep only if at least N% smaller',
    min_kbps_1080: 'Skip 1080p below (kbps)',
    min_kbps_720: 'Skip 720p below (kbps)',
    min_kbps_sd: 'Skip SD below (kbps)',
    skip_4k: 'Skip 4K',
    skip_hdr: 'Skip HDR / Dolby Vision',
    downscale_4k_to_1080: 'Downscale 4K to 1080p',
    allow_interlaced: 'Convert interlaced (deinterlace)',
    retain_originals_days: 'Keep originals for N days',
};

window.loadTranscodeView = async function() {
    const el = document.getElementById('transcode-container');
    if (!el) return;
    el.innerHTML = '<div class="loading-spinner">Loading migration status...</div>';
    try {
        const [backfill, settings, control, report, candidates, status, jobs] = await Promise.all([
            transcodeGet('/api/v1/transcode/probe-backfill'),
            transcodeGet('/api/v1/transcode/settings'),
            transcodeGet('/api/v1/transcode/control'),
            transcodeGet('/api/v1/transcode/dry-run'),
            transcodeGet('/api/v1/transcode/candidates?limit=25'),
            transcodeGet('/api/v1/transcode/status'),
            transcodeGet('/api/v1/transcode/jobs?limit=25'),
        ]);
        el.innerHTML = `
            ${renderTranscodeSteps(backfill, status)}
            <div id="transcode-savings-card">${renderTranscodeSavings(status)}</div>
            <div id="transcode-queue-card">${renderTranscodeQueue(status, jobs)}</div>
            ${renderTranscodeReport(report)}
            ${renderTranscodeCandidates(candidates)}
            ${renderTranscodeBackfill(backfill)}
            ${renderTranscodeControl(control)}
            ${['movie', 'tv'].map(t => renderTranscodeSettings(t, settings[t])).join('')}
        `;
        bindTranscodeHandlers();
        startTranscodeAutoRefresh();
    } catch (err) {
        el.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
    }
};

// Live progress: re-render just the savings and queue cards every 10 s while this page is open
// (the rest of the page holds forms that a refresh would wipe).
let _transcodeRefreshTimer = null;
function startTranscodeAutoRefresh() {
    if (_transcodeRefreshTimer) clearInterval(_transcodeRefreshTimer);
    _transcodeRefreshTimer = setInterval(async () => {
        const card = document.getElementById('transcode-queue-card');
        if (location.hash !== '#transcode' || !card || document.hidden) {
            if (location.hash !== '#transcode') { clearInterval(_transcodeRefreshTimer); _transcodeRefreshTimer = null; }
            return;
        }
        try {
            const [status, jobs] = await Promise.all([
                transcodeGet('/api/v1/transcode/status'),
                transcodeGet('/api/v1/transcode/jobs?limit=25'),
            ]);
            card.innerHTML = renderTranscodeQueue(status, jobs);
            const savingsCard = document.getElementById('transcode-savings-card');
            if (savingsCard) savingsCard.innerHTML = renderTranscodeSavings(status);
            bindTranscodeHandlers();
        } catch (err) { /* transient; the next tick retries */ }
    }, 10000);
}

async function transcodeGet(url) {
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Request failed: ${url}`);
    return data;
}

async function transcodeSend(url, method, body) {
    const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    return data;
}

function transcodeCard(title, inner) {
    return `<div class="section-card mb-3" style="padding: 1rem 1.25rem;"><h3 style="margin-top: 0;">${title}</h3>${inner}</div>`;
}

function transcodeTable(headers, rows) {
    return `<div style="overflow-x: auto;"><table style="width: 100%; border-collapse: collapse; font-size: 0.85rem; text-align: left;">
        <thead><tr>${headers.map(h => `<th style="padding: 4px 8px;">${h}</th>`).join('')}</tr></thead>
        <tbody>${rows.map(r => `<tr>${r.map(c => `<td style="padding: 4px 8px; border-top: 1px solid rgba(255,255,255,0.05);">${c}</td>`).join('')}</tr>`).join('')}</tbody>
    </table></div>`;
}

function renderTranscodeSteps(backfill, status) {
    const probed = backfill.coverage.pending_items === 0;
    const mode = status.control.mode;
    const steps = [
        { label: '1. Probe library', done: probed },
        { label: '2. Dry-run report', done: probed },
        { label: '3. Pilot', done: mode === 'pilot' || mode === 'full' },
        { label: '4. Full migration', done: mode === 'full' },
    ];
    return `<div class="section-card mb-3" style="padding: 0.75rem 1.25rem; display: flex; gap: 0.5rem; flex-wrap: wrap; font-size: 0.85rem;">
        ${steps.map(s => `<span style="padding: 4px 12px; border-radius: 999px; font-weight: 600; ${
            s.done ? 'background: var(--accent-color); color: #fff;'
                   : `background: rgba(255,255,255,0.06); color: var(--text-secondary);${s.disabled ? ' opacity: 0.6;' : ''}`
        }">${s.done ? '✓ ' : ''}${s.label}</span>`).join('')}
    </div>`;
}

function renderTranscodeReport(report) {
    const t = report.totals;
    const pct = t.eligible_bytes ? Math.round(100 * t.estimated_savings_bytes / t.eligible_bytes) : 0;
    const buckets = report.by_bucket.map(b => [
        escapeHtml(b.media_type === 'tv' ? 'TV' : 'Movie'),
        escapeHtml(b.bucket === '4k_down' ? '4K → 1080p' : (b.bucket === 'sd' ? 'SD' : `${b.bucket}p`)),
        escapeHtml(b.video_codec), b.count, `${formatGB(b.bytes)} GB`, `${formatGB(b.estimated_savings_bytes)} GB`,
    ]);
    const reasons = report.by_reason.map(r => [escapeHtml(r.label), r.count, `${formatGB(r.bytes)} GB`]);
    return transcodeCard('Dry-run: projected savings', `
        <div style="display: flex; gap: 2rem; flex-wrap: wrap; margin-bottom: 1rem;">
            <div><div class="muted" style="font-size: 0.8rem;">Files to convert</div><div style="font-size: 1.4rem; font-weight: 700;">${t.eligible.toLocaleString()}</div></div>
            <div><div class="muted" style="font-size: 0.8rem;">Their current size</div><div style="font-size: 1.4rem; font-weight: 700;">${formatGB(t.eligible_bytes)} GB</div></div>
            <div><div class="muted" style="font-size: 0.8rem;">Estimated space saved</div><div style="font-size: 1.4rem; font-weight: 700; color: var(--success, #22c55e);">${formatGB(t.estimated_savings_bytes)} GB (~${pct}%)</div></div>
            <div><div class="muted" style="font-size: 0.8rem;">Estimated encode time</div><div style="font-size: 1.4rem; font-weight: 700;">${(t.estimated_encode_hours / 24).toFixed(1)} days</div></div>
        </div>
        <p class="muted" style="font-size: 0.8rem;">Estimates are heuristic until real encodes calibrate them. Generated ${escapeHtml(new Date(report.generated_at + 'Z').toLocaleString())}.
            <a href="#" id="transcode-refresh-report">Refresh</a></p>
        <h4>Will convert</h4>
        ${transcodeTable(['Type', 'Resolution', 'Codec', 'Files', 'Size', 'Est. saved'], buckets)}
        <h4 class="mt-3">Skipped</h4>
        ${transcodeTable(['Reason', 'Files', 'Size'], reasons)}
    `);
}

function transcodeSize(bytes) {
    const gb = (bytes || 0) / (1024 * 1024 * 1024);
    return Math.abs(gb) >= 1024 ? `${(gb / 1024).toFixed(2)} TB` : `${gb.toFixed(1)} GB`;
}

function transcodeStat(label, value, sub, color) {
    return `<div><div class="muted" style="font-size: 0.8rem;">${label}</div>
        <div style="font-size: 1.4rem; font-weight: 700;${color ? ` color: ${color};` : ''}">${value}</div>
        ${sub ? `<div class="muted" style="font-size: 0.75rem;">${sub}</div>` : ''}</div>`;
}

function renderTranscodeSavings(status) {
    const s = status.savings;
    if (!s) return '';
    const kept = status.retained_originals || { count: 0, bytes: 0 };
    const pct = s.source_bytes ? Math.round(100 * s.saved_bytes / s.source_bytes) : 0;
    const types = Object.entries(s.by_type || {}).map(([t, e]) =>
        transcodeStat(t === 'tv' ? 'TV' : 'Movies', transcodeSize(e.saved_bytes), `${e.files.toLocaleString()} file(s)`));
    return transcodeCard('Space saved', `
        <div style="display: flex; gap: 2rem; flex-wrap: wrap;">
            ${transcodeStat('Total saved', transcodeSize(s.saved_bytes),
                `${s.files.toLocaleString()} file(s): ${transcodeSize(s.source_bytes)} → ${transcodeSize(s.output_bytes)} (${pct}% smaller)`,
                'var(--success, #22c55e)')}
            ${transcodeStat('Freed on disk now', s.freed_bytes > 0 ? transcodeSize(s.freed_bytes) : 'Not yet',
                kept.count ? `${kept.count.toLocaleString()} original(s) (${transcodeSize(kept.bytes)}) kept until the retention period ends` : 'no originals waiting to be purged')}
            ${types.join('')}
            ${transcodeStat('Still to come', `~${transcodeSize(s.remaining_estimated_bytes)}`,
                `${s.remaining_files.toLocaleString()} file(s) queued (estimate)`)}
        </div>
    `);
}

const TRANSCODE_JOB_ACTIONS = { skipped: 'requeue', failed: 'requeue', cancelled: 'requeue', pending: 'cancel', claimed: 'cancel', running: 'cancel' };

function renderTranscodeQueue(status, jobs) {
    const c = status.control;
    const w = status.worker;
    const counts = status.counts || {};
    const workerLine = w
        ? `${w.online ? '🟢' : '🔴'} Worker <strong>${escapeHtml(w.worker_id || '?')}</strong> v${escapeHtml(w.version || '?')}
           - last seen ${w.seconds_since_beat}s ago${w.version_matches ? '' : ` <span style="color: var(--warning, #f59e0b);">(server is v${escapeHtml(status.server_version)})</span>`}${
           w.online && w.detail && w.detail.gpu_error ? `<br><span style="color: var(--danger, #ef4444);">⚠ GPU unavailable: ${escapeHtml(w.detail.gpu_error)}
           - waiting${w.detail.auto_reboot ? '; host reboots at 03:30 if it is a driver mismatch' : ' (reboot needed?)'}</span>` : ''}`
        : '⚪ No transcode worker has checked in yet.';
    const current = (status.current || []).map(j => `<div style="font-size: 0.85rem;">▶ ${escapeHtml(j.title || j.source_path)} - ${escapeHtml(j.phase)}
        ${j.progress_pct != null ? `${j.progress_pct.toFixed(1)}%` : ''}${j.fps ? ` @ ${Math.round(j.fps)} fps` : ''}${j.eta_seconds ? `, ~${Math.round(j.eta_seconds / 60)} min left` : ''}</div>`).join('');
    const rows = jobs.items.map(j => [
        `<span title="${escapeHtml(j.source_path)}">${escapeHtml(j.title || j.source_path)}</span>${j.is_pilot ? ' <span class="muted">(pilot)</span>' : ''}`,
        escapeHtml(j.state) + (j.state === 'running' && j.progress_pct != null ? ` ${j.progress_pct.toFixed(0)}%` : ''),
        j.saved_bytes != null ? `${formatGB(j.saved_bytes)} GB` : `<span class="muted">~${formatGB(j.est_savings_bytes)} GB</span>`,
        j.error_code ? `<span title="${escapeHtml(j.error_message || '')}">${escapeHtml(j.error_code)}</span>` : '',
        TRANSCODE_JOB_ACTIONS[j.state] ? `<button class="btn btn-secondary btn-sm" data-job-action="${TRANSCODE_JOB_ACTIONS[j.state]}" data-job-id="${escapeHtml(j.id)}">${TRANSCODE_JOB_ACTIONS[j.state] === 'cancel' ? 'Cancel' : 'Requeue'}</button>` : '',
    ]);
    const countLine = Object.entries(counts).map(([k, v]) => `${v} ${escapeHtml(k)}`).join(' · ') || 'Queue is empty';
    return transcodeCard('Migration queue', `
        <p style="font-size: 0.85rem; margin-top: 0;">${workerLine}</p>
        <div style="display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.75rem;">
            <button class="btn ${c.run_state === 'running' ? 'btn-secondary' : 'btn-primary'} btn-sm" id="transcode-toggle-run">${c.run_state === 'running' ? '⏸ Pause' : '▶ Start'}</button>
            <label style="font-size: 0.85rem;">Mode
                <select id="transcode-mode" class="search-input" style="width: auto;">
                    ${['off', 'pilot', 'full'].map(m => `<option value="${m}" ${c.mode === m ? 'selected' : ''}>${m === 'off' ? 'Off' : m === 'pilot' ? 'Pilot only' : 'Full library'}</option>`).join('')}
                </select></label>
            <label style="font-size: 0.85rem;">Yield GPU
                <select id="transcode-pause-on" class="search-input" style="width: auto;">
                    ${[['transcode_only', 'when Plex/Jellyfin/Emby transcode'], ['any_stream', 'whenever anything plays'], ['never', 'never']]
                        .map(([v, l]) => `<option value="${v}" ${c.pause_on === v ? 'selected' : ''}>${l}</option>`).join('')}
                </select></label>
            <button class="btn btn-secondary btn-sm" id="transcode-enqueue-all">Queue whole library…</button>
            <span class="muted" style="font-size: 0.85rem;">${countLine}${c.mode === 'full' ? ' · new downloads are queued automatically (hourly)' : ''}</span>
        </div>
        ${status.streams ? `<p class="muted" style="font-size: 0.8rem;">Media servers: ${status.streams.streams} stream(s), ${status.streams.video_transcodes} video transcode(s)${status.streams.pause_reason ? ` - <strong>worker yielding: ${escapeHtml(status.streams.pause_reason)}</strong>` : ''}${status.streams.errors.length ? ` (unreachable: ${escapeHtml(status.streams.errors.join(', '))})` : ''}</p>` : ''}
        ${current}
        ${jobs.items.length ? transcodeTable(['File', 'State', 'Saved', 'Error', ''], rows) : ''}
    `);
}

function renderTranscodeCandidates(candidates) {
    const rows = candidates.items.map(i => [
        `<input type="checkbox" data-pilot-id="${escapeHtml(i.id)}">`,
        `<span title="${escapeHtml(i.path)}">${escapeHtml(i.title)}${i.season != null ? ` S${String(i.season).padStart(2, '0')}E${String(i.episode).padStart(2, '0')}` : ''}</span>`,
        escapeHtml(i.video_codec), `${formatGB(i.size_bytes)} GB`, `${formatGB(i.estimated_savings_bytes)} GB`,
        `CQ ${i.plan.cq}${i.plan.scale_width ? ' · →1080p' : ''}`,
        `<button class="btn btn-secondary btn-sm" data-preview-id="${escapeHtml(i.id)}">Command</button>`,
    ]);
    return transcodeCard(`Biggest wins first (${candidates.total.toLocaleString()} files)`, `
        <p class="muted" style="font-size: 0.8rem; margin-top: 0;">Tick ~10 varied files (a movie, a TV episode, an XviD, an MP4 with subtitles, a NAS file) and queue them as the pilot. Originals are kept for the retention period so you can compare.</p>
        ${transcodeTable(['', 'Title', 'Codec', 'Size', 'Est. saved', 'Plan', ''], rows)}
        <button class="btn btn-primary btn-sm mt-2" id="transcode-queue-pilot">Queue selected as pilot</button>
        <pre id="transcode-preview" style="display: none; white-space: pre-wrap; word-break: break-all; font-size: 0.75rem; margin-top: 0.75rem; padding: 0.75rem; background: rgba(0,0,0,0.3); border-radius: 6px;"></pre>
    `);
}

function renderTranscodeBackfill(backfill) {
    const c = backfill.coverage, s = backfill.status;
    const running = backfill.running;
    return transcodeCard('Codec metadata', `
        <p style="font-size: 0.85rem;">${c.probed_items.toLocaleString()} of ${c.total_items.toLocaleString()} files probed
            (probe v${backfill.probe_version}). ${c.pending_items ? `<strong>${c.pending_items.toLocaleString()} pending.</strong>` : ''}
            ${s.state !== 'idle' ? `Last run: ${escapeHtml(s.state)} - ${s.done} probed, ${s.failed} failed, ${s.skipped_empty} empty, ${s.skipped_missing} missing.` : ''}</p>
        <button class="btn btn-primary btn-sm" id="transcode-run-backfill" ${running ? 'disabled' : ''}>${running ? `Probing… ${s.done + s.failed}/${s.total}` : 'Probe pending files'}</button>
        <button class="btn btn-secondary btn-sm" id="transcode-rescore">Recompute quality scores</button>
    `);
}

function renderTranscodeControl(control) {
    const codecs = TRANSCODE_CODEC_CHOICES.map(c => `
        <label style="margin-right: 1rem; font-size: 0.85rem;"><input type="checkbox" data-codec="${c}" ${control.source_codecs.includes(c) ? 'checked' : ''}> ${c}</label>`).join('');
    return transcodeCard('What to convert', `
        <div style="margin-bottom: 0.75rem;"><div class="muted" style="font-size: 0.8rem; margin-bottom: 0.25rem;">Source codecs</div>${codecs}</div>
        <div class="muted" style="font-size: 0.8rem;">Excluded folders (one absolute path per line)</div>
        <textarea id="transcode-excluded" rows="3" class="search-input" style="width: 100%; font-family: monospace;">${escapeHtml(control.excluded_prefixes.join('\n'))}</textarea>
        <p class="muted" style="font-size: 0.75rem;">Always excluded (watcher download/Syncthing folders): ${escapeHtml(control.always_excluded_prefixes.join(', ') || 'none')}</p>
        <div class="muted" style="font-size: 0.8rem; margin-top: 0.5rem;">Email alerts to (comma-separated; blank = all admins)</div>
        <input id="transcode-notify-emails" class="search-input" style="width: 100%;" value="${escapeHtml((control.notify_emails || []).join(', '))}">
        <label style="font-size: 0.85rem; display: block; margin: 0.5rem 0;">Alert after
            <input type="number" min="1" max="50" id="transcode-streak" class="search-input" style="width: 60px;" value="${control.failure_streak_threshold || 3}">
            failures in a row (plus a daily summary, and an alert if the worker stops responding)</label>
        <button class="btn btn-primary btn-sm" id="transcode-save-control">Save</button>
    `);
}

function renderTranscodeSettings(type, settings) {
    const fields = Object.keys(TRANSCODE_SETTING_LABELS).map(key => {
        const value = settings[key];
        const id = `transcode-${type}-${key}`;
        const input = typeof value === 'boolean'
            ? `<input type="checkbox" id="${id}" data-setting="${key}" ${value ? 'checked' : ''}>`
            : `<input type="${typeof value === 'number' ? 'number' : 'text'}" min="0" id="${id}" data-setting="${key}" value="${escapeHtml(String(value))}" class="search-input" style="width: 90px;">`;
        return `<label style="display: flex; justify-content: space-between; align-items: center; gap: 1rem; font-size: 0.85rem; padding: 3px 0;">
            <span>${TRANSCODE_SETTING_LABELS[key]}</span>${input}</label>`;
    }).join('');
    return transcodeCard(`${type === 'tv' ? 'TV' : 'Movie'} encode settings`, `
        <div data-settings-type="${type}" style="max-width: 520px;">${fields}</div>
        <button class="btn btn-primary btn-sm mt-2" data-save-settings="${type}">Save ${type === 'tv' ? 'TV' : 'movie'} settings</button>
    `);
}

function bindTranscodeHandlers() {
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
    const control = (body) => transcodeSend('/api/v1/transcode/control', 'PUT', body).then(window.loadTranscodeView);
    on('transcode-toggle-run', async (e) => {
        try { await control({ run_state: e.target.textContent.includes('Pause') ? 'paused' : 'running' }); }
        catch (err) { showToast(err.message, 'error'); }
    });
    const modeSelect = document.getElementById('transcode-mode');
    if (modeSelect) modeSelect.onchange = async () => {
        if (modeSelect.value === 'full' && !confirm('Release the full library queue? Files are converted biggest-savings first.')) {
            return window.loadTranscodeView();
        }
        try { await control({ mode: modeSelect.value }); } catch (err) { showToast(err.message, 'error'); }
    };
    const pauseSelect = document.getElementById('transcode-pause-on');
    if (pauseSelect) pauseSelect.onchange = async () => {
        try { await control({ pause_on: pauseSelect.value }); } catch (err) { showToast(err.message, 'error'); }
    };
    on('transcode-enqueue-all', async () => {
        if (!confirm('Queue every eligible file in the library? (Nothing runs until mode is Full and the queue is started.)')) return;
        try {
            const r = await transcodeSend('/api/v1/transcode/jobs/enqueue-all', 'POST', { confirm: true });
            showToast(`Queued ${r.created_count} files`, 'success');
            window.loadTranscodeView();
        } catch (err) { showToast(err.message, 'error'); }
    });
    on('transcode-queue-pilot', async () => {
        const ids = [...document.querySelectorAll('[data-pilot-id]')].filter(c => c.checked).map(c => c.dataset.pilotId);
        if (!ids.length) return showToast('Tick some files first', 'error');
        try {
            const r = await transcodeSend('/api/v1/transcode/jobs', 'POST', { media_item_ids: ids, pilot: true });
            showToast(`Queued ${r.created.length} pilot file(s)${r.skipped.length ? `, ${r.skipped.length} skipped` : ''}`, 'success');
            window.loadTranscodeView();
        } catch (err) { showToast(err.message, 'error'); }
    });
    document.querySelectorAll('[data-job-action]').forEach(btn => {
        btn.onclick = async () => {
            try {
                await transcodeSend(`/api/v1/transcode/jobs/${encodeURIComponent(btn.dataset.jobId)}/${btn.dataset.jobAction}`, 'POST');
                window.loadTranscodeView();
            } catch (err) { showToast(err.message, 'error'); }
        };
    });
    on('transcode-refresh-report', async (e) => {
        e.preventDefault();
        await transcodeGet('/api/v1/transcode/dry-run?force_refresh=true');
        window.loadTranscodeView();
    });
    on('transcode-run-backfill', async () => {
        try {
            await transcodeSend('/api/v1/transcode/probe-backfill', 'POST');
            showToast('Probe backfill started', 'success');
            setTimeout(window.loadTranscodeView, 3000);
        } catch (err) { showToast(err.message, 'error'); }
    });
    on('transcode-rescore', async () => {
        try {
            const r = await transcodeSend('/api/v1/transcode/rescore', 'POST');
            showToast(`Rescored ${r.total} files (${r.changed} changed)`, 'success');
        } catch (err) { showToast(err.message, 'error'); }
    });
    on('transcode-save-control', async () => {
        try {
            const codecs = [...document.querySelectorAll('[data-codec]')].filter(c => c.checked).map(c => c.dataset.codec);
            const excluded = document.getElementById('transcode-excluded').value.split('\n').map(s => s.trim()).filter(Boolean);
            const emails = document.getElementById('transcode-notify-emails').value.split(',').map(s => s.trim()).filter(Boolean);
            const streak = parseInt(document.getElementById('transcode-streak').value, 10) || 3;
            await transcodeSend('/api/v1/transcode/control', 'PUT',
                { source_codecs: codecs, excluded_prefixes: excluded, notify_emails: emails, failure_streak_threshold: streak });
            showToast('Saved', 'success');
            window.loadTranscodeView();
        } catch (err) { showToast(err.message, 'error'); }
    });
    document.querySelectorAll('[data-save-settings]').forEach(btn => {
        btn.onclick = async () => {
            const type = btn.dataset.saveSettings;
            const updates = {};
            document.querySelectorAll(`[data-settings-type="${type}"] [data-setting]`).forEach(input => {
                const key = input.dataset.setting;
                updates[key] = input.type === 'checkbox' ? input.checked
                    : (input.type === 'number' ? parseInt(input.value, 10) : input.value.trim());
            });
            try {
                await transcodeSend(`/api/v1/transcode/settings/${type}`, 'PUT', updates);
                showToast('Settings saved', 'success');
                window.loadTranscodeView();
            } catch (err) { showToast(err.message, 'error'); }
        };
    });
    document.querySelectorAll('[data-preview-id]').forEach(btn => {
        btn.onclick = async () => {
            const pre = document.getElementById('transcode-preview');
            pre.style.display = 'block';
            pre.textContent = 'Probing file…';
            try {
                const r = await transcodeGet(`/api/v1/transcode/preview-command/${encodeURIComponent(btn.dataset.previewId)}`);
                pre.textContent = r.eligible
                    ? `${r.command.map(a => (/\s/.test(a) ? `'${a}'` : a)).join(' ')}\n\nOutput: ${r.output_path}${r.dropped_streams.length ? `\nDropped: ${r.dropped_streams.join(', ')}` : ''}`
                    : `Not eligible: ${r.reason_label}`;
            } catch (err) { pre.textContent = err.message; }
        };
    });
}
