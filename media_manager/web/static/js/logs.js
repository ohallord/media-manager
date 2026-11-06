// Log Viewer JS - Supports Last N Lines (default 75) & Live SSE Streaming
let evtSource = null;
let currentLogMode = 'recent'; // 'recent' or 'live'
let selectedCount = 75;

function stopLiveStream() {
    if (evtSource) {
        evtSource.close();
        evtSource = null;
    }
}

// escapeHtml is defined in utils.js (loaded first)

function appendLogLine(container, logItem) {
    const line = document.createElement('div');
    line.className = `log-line log-level-${(logItem.level || 'info').toLowerCase()}`;
    
    let timeStr = logItem.created_at || 'LIVE';
    if (timeStr && timeStr !== 'LIVE') {
        try {
            let cleanTimeStr = timeStr.replace(',', '.');
            const parsedDate = new Date(cleanTimeStr);
            if (!isNaN(parsedDate.getTime())) {
                timeStr = parsedDate.toLocaleString();
            }
        } catch (e) {
            // fallback
        }
    }
    
    const filterVal = (document.getElementById('log-filter-input')?.value || '').toLowerCase();
    const fullText = `[${timeStr}] [${logItem.level || 'INFO'}] ${logItem.event_tag || ''} ${logItem.message || ''}`;

    if (filterVal && !fullText.toLowerCase().includes(filterVal)) {
        line.style.display = 'none';
    }

    // Colorize log levels
    let levelColor = 'var(--text-secondary)';
    if (logItem.level === 'ERROR') levelColor = 'var(--danger)';
    else if (logItem.level === 'WARNING') levelColor = 'var(--warning)';
    else if (logItem.level === 'INFO') levelColor = 'var(--success)';

    line.innerHTML = `<span style="color: var(--text-secondary);">[${timeStr}]</span> <span style="color: ${levelColor}; font-weight: bold;">[${logItem.level || 'INFO'}]</span> <span style="color: var(--accent-color);">${logItem.event_tag || ''}</span> <span>${escapeHtml(logItem.message || '')}</span>`;
    container.appendChild(line);
}

window.loadRecentLogs = async function(count = 75) {
    stopLiveStream();
    currentLogMode = 'recent';
    if (typeof window.refreshWatcherHeartbeat === 'function') window.refreshWatcherHeartbeat();
    const box = document.getElementById('log-terminal-box');
    if (!box) return;

    box.innerHTML = `<div class="log-line text-muted">Fetching last ${count} log lines...</div>`;

    try {
        const res = await fetch(`/api/v1/logs/recent?count=${count}`);
        if (!res.ok) throw new Error('Failed to fetch logs');
        const data = await res.json();
        
        box.innerHTML = '';
        if (!data.logs || data.logs.length === 0) {
            box.innerHTML = '<div class="log-line text-muted">No log entries found.</div>';
            return;
        }

        data.logs.forEach(log => appendLogLine(box, log));
        box.scrollTop = box.scrollHeight;
    } catch (err) {
        box.innerHTML = `<div class="log-line" style="color: #ff6b6b;">Error loading log history: ${err.message}</div>`;
    }
};

window.initLogStream = function() {
    const box = document.getElementById('log-terminal-box');
    if (!box) return;

    if (currentLogMode === 'recent') {
        window.loadRecentLogs(selectedCount);
        return;
    }

    stopLiveStream();
    box.innerHTML = `<div class="log-line text-muted">Connecting to live SSE log stream (backfilling ${selectedCount} lines)...</div>`;

    evtSource = new EventSource(`/api/v1/logs/stream?backfill=${selectedCount}`);

    let backfilled = false;

    evtSource.onmessage = function(event) {
        try {
            if (!backfilled) {
                box.innerHTML = '';
                backfilled = true;
            }
            const data = JSON.parse(event.data);
            appendLogLine(box, data);
            box.scrollTop = box.scrollHeight;
        } catch (e) {
            console.error('Log parse error', e);
        }
    };

    evtSource.onerror = function() {
        console.warn('SSE log stream disconnected. Retrying...');
    };
};

document.addEventListener('DOMContentLoaded', () => {
    const btnLast75 = document.getElementById('btn-log-last-75');
    const btnLive = document.getElementById('btn-log-live');
    const countSelect = document.getElementById('log-count-select');
    const filterInput = document.getElementById('log-filter-input');
    const btnClear = document.getElementById('btn-clear-logs');

    if (btnLast75) {
        btnLast75.addEventListener('click', () => {
            btnLast75.classList.add('active');
            btnLive.classList.remove('active');
            currentLogMode = 'recent';
            btnLast75.textContent = `📜 Last ${selectedCount} Lines`;
            window.loadRecentLogs(selectedCount);
        });
    }

    if (btnLive) {
        btnLive.addEventListener('click', () => {
            btnLive.classList.add('active');
            btnLast75.classList.remove('active');
            currentLogMode = 'live';
            window.initLogStream();
        });
    }

    if (countSelect) {
        countSelect.addEventListener('change', (e) => {
            selectedCount = parseInt(e.target.value, 10) || 75;
            if (btnLast75) btnLast75.textContent = `📜 Last ${selectedCount} Lines`;
            if (currentLogMode === 'recent') {
                window.loadRecentLogs(selectedCount);
            } else {
                window.initLogStream();
            }
        });
    }

    if (filterInput) {
        filterInput.addEventListener('input', () => {
            const val = filterInput.value.toLowerCase();
            document.querySelectorAll('#log-terminal-box .log-line').forEach(line => {
                const text = line.textContent.toLowerCase();
                line.style.display = text.includes(val) ? 'block' : 'none';
            });
        });
    }

    if (btnClear) {
        btnClear.addEventListener('click', () => {
            const box = document.getElementById('log-terminal-box');
            if (box) box.innerHTML = '<div class="log-line text-muted">Console cleared.</div>';
        });
    }
});
