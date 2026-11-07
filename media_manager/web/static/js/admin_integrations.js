// Admin Integrations Setup JS (Radarr, Sonarr, SABnzbd with Test Connection)
window.loadAdminIntegrations = async function() {
    const container = document.getElementById('admin-integrations-list');
    if (!container) return;

    try {
        const res = await fetch('/api/v1/integrations/config');
        if (res.status === 401 || res.status === 403) {
            container.innerHTML = `
                <div style="background-color: var(--bg-primary); padding: 1.5rem; border-radius: var(--radius-md); text-align: center;">
                    <h4>🔒 Admin Authentication Required</h4>
                    <p class="muted" style="margin: 0.5rem 0;">Please log in with an Admin account to manage service integrations and API keys.</p>
                    <button class="btn btn-primary btn-sm" onclick="window.openAuthModal()">Sign In as Admin</button>
                </div>
            `;
            return;
        }
        if (!res.ok) throw new Error('Failed to load integration configurations.');
        const data = await res.json();

        renderAdminIntegrations(data.configs, container);
        if (typeof window.refreshWatcherHeartbeat === 'function') window.refreshWatcherHeartbeat();
    } catch (err) {
        container.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
    }
};

function renderAdminIntegrations(configs, container) {
    const services = [
        { name: 'radarr', title: 'Radarr', defaultUrl: 'http://localhost:7878', keyHelp: 'Radarr Settings -> General -> API Key' },
        { name: 'sonarr', title: 'Sonarr', defaultUrl: 'http://localhost:8989', keyHelp: 'Sonarr Settings -> General -> API Key' },
        { name: 'sabnzbd', title: 'SABnzbd', defaultUrl: 'http://localhost:8080', keyHelp: 'SABnzbd General Settings -> API Key' },
        { name: 'fanart', title: 'Fanart.tv', defaultUrl: 'https://webservice.fanart.tv/v3', keyHelp: 'Project API Key from fanart.tv/get-an-api-key/' },
        { name: 'tmdb', title: 'TMDb', defaultUrl: 'https://api.themoviedb.org/3', keyHelp: 'API Key (v3) or Read Access Token (v4) from themoviedb.org/settings/api' },
        { name: 'email', title: 'Email', defaultUrl: 'smtp.example.com:587:you@example.com:true', keyHelp: 'Google App Password or SMTP Authentication Password' },
        { name: 'syncthing', title: 'Syncthing', defaultUrl: 'http://127.0.0.1:8384', keyHelp: 'Syncthing API Key (Actions -> Settings -> GUI -> API Key)' }
    ];
    const configMap = {};
    (configs || []).forEach(c => configMap[c.service_name] = c);

    container.innerHTML = services.map(srv => {
        const conf = configMap[srv.name] || { api_url: srv.defaultUrl, api_key: '', enabled: false, status: 'NOT_CONFIGURED' };
        const statusBadgeClass = conf.status === 'HEALTHY' ? 'badge-4k' : (conf.status === 'UNREACHABLE' ? 'badge-sd' : 'badge-sd');
        const displayUrl = conf.api_url || srv.defaultUrl;

        if (srv.name === 'email') {
            let smtpHost = 'smtp.example.com';
            let smtpPort = '587';
            let smtpSender = 'you@example.com';
            let smtpUseTls = 'true';
            
            if (conf.api_url && conf.api_url.includes(':')) {
                const parts = conf.api_url.split(':');
                if (parts.length >= 4) {
                    smtpHost = parts[0];
                    smtpPort = parts[1];
                    smtpSender = parts[2];
                    smtpUseTls = parts[3];
                }
            }

            return integrationRow(srv, statusBadgeClass, conf.status, `
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px;">
                            <div>
                                <label style="font-size: 0.85rem; color: var(--text-secondary); display: block; margin-bottom: 0.25rem;">SMTP Server Host</label>
                                <input type="text" id="cfg-email-host" placeholder="smtp.example.com" value="${smtpHost}" required class="search-input" style="width: 100%;">
                            </div>
                            <div>
                                <label style="font-size: 0.85rem; color: var(--text-secondary); display: block; margin-bottom: 0.25rem;">Port</label>
                                <input type="number" id="cfg-email-port" placeholder="587" value="${smtpPort}" required class="search-input" style="width: 100%;">
                            </div>
                        </div>
                        <div>
                            <label style="font-size: 0.85rem; color: var(--text-secondary); display: block; margin-bottom: 0.25rem;">Sender Email Address</label>
                            <input type="email" id="cfg-email-sender" placeholder="you@example.com" value="${smtpSender}" required class="search-input" style="width: 100%;">
                        </div>
                        <div>
                            <label style="font-size: 0.85rem; color: var(--text-secondary); display: block; margin-bottom: 0.25rem;">SMTP Password / Google App Password</label>
                            <input type="password" id="cfg-email-password" placeholder="Enter Password" value="${conf.api_key || ''}" class="search-input" style="width: 100%;">
                        </div>
                        <div style="display: flex; align-items: center; gap: 0.5rem;">
                            <input type="checkbox" id="cfg-email-tls" ${smtpUseTls === 'true' ? 'checked' : ''} style="cursor: pointer;">
                            <label for="cfg-email-tls" style="font-size: 0.85rem; color: var(--text-secondary); cursor: pointer; user-select: none;">Use TLS Security (Recommended for Port 587)</label>
                        </div>
                        <div style="display: flex; align-items: center; gap: 0.5rem;">
                            <input type="checkbox" id="cfg-email-enabled" ${conf.enabled ? 'checked' : ''} style="cursor: pointer;">
                            <label for="cfg-email-enabled" style="font-size: 0.85rem; color: var(--text-secondary); cursor: pointer; user-select: none;">Enable Email Notifications (2FA codes & password resets)</label>
                        </div>

                        <div id="extra-options-${srv.name}" class="mt-2"></div>

                        <div style="display: flex; gap: 0.75rem; margin-top: 0.5rem; flex-wrap: wrap;">
                            <button type="submit" class="btn btn-primary btn-sm">Save</button>
                            <button type="button" class="btn btn-secondary btn-sm" onclick="testIntegrationConnection('${srv.name}')">Test</button>
                        </div>
            `);
        }

        return integrationRow(srv, statusBadgeClass, conf.status, `
                    <div>
                        <label style="font-size: 0.85rem; color: var(--text-secondary); display: block; margin-bottom: 0.25rem;">Server URL / API Endpoint</label>
                        <input type="text" id="cfg-url-${srv.name}" placeholder="${srv.defaultUrl}" value="${displayUrl}" required class="search-input" style="width: 100%;">
                    </div>
                    <div>
                        <label style="font-size: 0.85rem; color: var(--text-secondary); display: block; margin-bottom: 0.25rem;">API Key (${srv.keyHelp})</label>
                        <input type="password" id="cfg-key-${srv.name}" placeholder="Enter API Key" value="${conf.api_key || ''}" required class="search-input" style="width: 100%;">
                    </div>

                    <div id="extra-options-${srv.name}" class="mt-2"></div>

                    <div style="display: flex; gap: 0.75rem; margin-top: 0.5rem; flex-wrap: wrap;">
                        <button type="submit" class="btn btn-primary btn-sm">Save</button>
                        <button type="button" class="btn btn-secondary btn-sm" onclick="testIntegrationConnection('${srv.name}')">Test</button>
                    </div>
        `);
    }).join('');
}

function integrationRow(srv, statusClass, status, fields) {
    return `
        <div class="section-card mb-3" id="integration-card-${srv.name}">
            <button type="button" class="integration-row" onclick="window.toggleIntegration('${srv.name}')">
                <span>${srv.title}</span>
                <span class="badge ${statusClass}" id="badge-${srv.name}">${status}</span>
            </button>
            <form id="integration-form-${srv.name}" class="integration-form" hidden onsubmit="saveIntegrationConfig(event, '${srv.name}')">
                ${fields}
            </form>
        </div>`;
}

window.toggleIntegration = function(name) {
    const target = document.getElementById('integration-form-' + name);
    const willOpen = target && target.hidden;
    document.querySelectorAll('.integration-form').forEach(el => { el.hidden = true; });
    if (target && willOpen) target.hidden = false;
};

async function testIntegrationConnection(serviceName) {
    let apiUrl = '';
    let apiKey = '';
    let enabled = true;

    if (serviceName === 'email') {
        const host = document.getElementById('cfg-email-host').value.trim();
        const port = document.getElementById('cfg-email-port').value.trim();
        const sender = document.getElementById('cfg-email-sender').value.trim();
        const tls = document.getElementById('cfg-email-tls').checked ? 'true' : 'false';
        apiUrl = `${host}:${port}:${sender}:${tls}`;
        apiKey = document.getElementById('cfg-email-password').value;
        enabled = document.getElementById('cfg-email-enabled').checked;
    } else {
        apiUrl = document.getElementById(`cfg-url-${serviceName}`).value.trim();
        apiKey = document.getElementById(`cfg-key-${serviceName}`).value.trim();
    }

    const badge = document.getElementById(`badge-${serviceName}`);
    const extraContainer = document.getElementById(`extra-options-${serviceName}`);

    if (!apiUrl) {
        showToast('Please enter SMTP/API configuration details before testing.');
        return;
    }

    if (badge) badge.textContent = 'Testing...';

    try {
        const res = await fetch(`/api/v1/integrations/test/${serviceName}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_url: apiUrl, api_key: apiKey, enabled: enabled })
        });

        if (!res.ok) throw new Error('Test failed');
        const data = await res.json();

        if (data.healthy) {
            if (badge) {
                badge.textContent = 'HEALTHY';
                badge.className = 'badge badge-4k';
            }
            let detailsHtml = '<div style="font-size: 0.85rem; color: var(--success); margin-top: 0.5rem; font-weight: 600;">✅ Connection Successful!</div>';
            
            if (data.profiles && data.profiles.length > 0) {
                detailsHtml += `
                    <div style="font-size: 0.8rem; margin-top: 0.5rem; color: var(--text-secondary);">
                        <b>Available Quality Profiles:</b> ${data.profiles.map(p => p.name).join(', ')}
                    </div>
                `;
            }
            if (data.root_folders && data.root_folders.length > 0) {
                detailsHtml += `
                    <div style="font-size: 0.8rem; margin-top: 0.25rem; color: var(--text-secondary);">
                        <b>Available Root Storage Folders:</b> ${data.root_folders.map(f => f.path).join(', ')}
                    </div>
                `;
            }

            if (extraContainer) extraContainer.innerHTML = detailsHtml;
        } else {
            if (badge) {
                badge.textContent = 'UNREACHABLE';
                badge.className = 'badge badge-sd';
            }
            const errMsg = data.error ? `: ${data.error}` : '.';
            if (extraContainer) extraContainer.innerHTML = `<div style="font-size: 0.85rem; color: var(--danger); margin-top: 0.5rem;">❌ Connection Failed${errMsg}</div>`;
        }
    } catch (err) {
        if (badge) badge.textContent = 'ERROR';
        showToast(`Connection test error: ${err.message}`);
    }
}

async function saveIntegrationConfig(event, serviceName) {
    event.preventDefault();
    let apiUrl = '';
    let apiKey = '';
    let enabled = true;

    if (serviceName === 'email') {
        const host = document.getElementById('cfg-email-host').value.trim();
        const port = document.getElementById('cfg-email-port').value.trim();
        const sender = document.getElementById('cfg-email-sender').value.trim();
        const tls = document.getElementById('cfg-email-tls').checked ? 'true' : 'false';
        apiUrl = `${host}:${port}:${sender}:${tls}`;
        apiKey = document.getElementById('cfg-email-password').value;
        enabled = document.getElementById('cfg-email-enabled').checked;
    } else {
        apiUrl = document.getElementById(`cfg-url-${serviceName}`).value.trim();
        apiKey = document.getElementById(`cfg-key-${serviceName}`).value.trim();
    }

    try {
        const res = await fetch(`/api/v1/integrations/config/${serviceName}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_url: apiUrl, api_key: apiKey, enabled: enabled })
        });
        if (!res.ok) throw new Error('Failed to save configuration');
        showToast(`${serviceName.toUpperCase()} integration settings saved successfully!`);
        window.loadAdminIntegrations();
    } catch (err) {
        showToast(err.message);
    }
}

window.loadAdminDirectories = async function() {
    const listContainer = document.getElementById('admin-directories-list');
    if (!listContainer) return;

    try {
        const res = await fetch('/api/v1/integrations/directories');
        if (!res.ok) throw new Error('Failed to load directories');
        const data = await res.json();

        renderDirectoriesList(data, listContainer);
    } catch (err) {
        listContainer.innerHTML = `<p class="error-msg">${escapeHtml(err.message)}</p>`;
    }
};

// formatBytes is defined in utils.js (loaded first)

function renderDirectoriesList(directories, container) {
    if (!directories || directories.length === 0) {
        container.innerHTML = `<p class="muted">No directories configured. System falls back to default config paths.</p>`;
        return;
    }

    container.innerHTML = `
        <div style="display: flex; flex-direction: column; gap: 0.6rem;">
            ${directories.map(d => {
                const ds = d.disk_space || {};
                let pillHtml = '';
                let progressHtml = '';
                let pillBg = 'rgba(255,255,255,0.1)';
                let pillFg = 'var(--text-secondary)';

                if (ds.error) {
                    pillHtml = `<span style="font-size: 0.75rem; color: #ef4444; font-weight: 500;">⚠️ ${escapeHtml(ds.error)}</span>`;
                } else if (ds.total) {
                    const freePct = ds.free_percent;
                    const usedPct = (100 - freePct).toFixed(1);
                    pillBg = '#22c55e'; // Green
                    pillFg = '#ffffff';
                    if (freePct <= 10.0) {
                        pillBg = '#ef4444'; // Red
                    } else if (freePct <= 15.0) {
                        pillBg = '#eab308'; // Yellow
                        pillFg = '#000000';
                    }
                    const freeStr = formatBytes(ds.free);
                    const usedStr = formatBytes(ds.used);
                    const totalStr = formatBytes(ds.total);
                    
                    pillHtml = `
                        <span class="badge" style="background: ${pillBg}; color: ${pillFg}; font-size: 0.75rem; padding: 4px 10px; border-radius: 4px; font-weight: 600; display: inline-flex; align-items: center;" title="Total: ${totalStr} | Free: ${freeStr}">
                            ${freePct}% free
                        </span>
                    `;

                    progressHtml = `
                        <div style="display: flex; flex-direction: column; gap: 0.2rem; flex: 1; max-width: 250px; min-width: 120px;">
                            <div style="background: rgba(255,255,255,0.06); height: 6px; border-radius: 999px; overflow: hidden;">
                                <div style="width: ${usedPct}%; height: 100%; background: ${pillBg}; border-radius: 999px;"></div>
                            </div>
                            <div style="display: flex; justify-content: space-between; font-size: 0.68rem; color: var(--text-secondary); font-weight: 500;">
                                <span>${usedStr} used</span>
                                <span>${freeStr} free</span>
                            </div>
                        </div>
                    `;
                } else {
                    pillHtml = `<span style="font-size: 0.75rem; color: var(--text-secondary);">No space data</span>`;
                }

                return `
                    <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); padding: 0.8rem 1.2rem; border-radius: var(--radius-md); gap: 1.5rem; flex-wrap: wrap;">
                        <div style="display: flex; align-items: center; gap: 0.5rem; flex: 1; min-width: 200px;">
                            <span style="font-size: 1.1rem;">📁</span>
                            <span style="font-family: monospace; font-size: 0.92rem; font-weight: 500; word-break: break-all; color: var(--text-primary);">${escapeHtml(d.path)}</span>
                        </div>
                        ${progressHtml}
                        <div style="display: flex; align-items: center; gap: 0.8rem; justify-content: flex-end; min-width: 180px;">
                            ${pillHtml}
                            ${d.id === 'default' ? 
                              `<span class="badge badge-sd" style="font-size: 0.65rem; padding: 4px 8px; border-radius: 4px;">Config Default</span>` : 
                              `<button class="btn btn-secondary btn-sm" onclick="removeMonitoredDirectory('${d.id}')" style="color: var(--danger); border-color: rgba(239, 68, 68, 0.2); background: rgba(239, 68, 68, 0.05); padding: 4px 10px; font-size: 0.72rem; font-weight: 600;">🗑️ Remove</button>`
                            }
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

window.removeMonitoredDirectory = async function(dirId) {
    if (!confirm('Are you sure you want to remove this directory from the monitoring list?')) return;

    try {
        const res = await fetch(`/api/v1/integrations/directories/${dirId}`, { method: 'DELETE' });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to remove directory');
        }
        showToast('Directory removed successfully.');
        window.loadAdminDirectories();
    } catch (err) {
        showToast(err.message);
    }
};

// Setup forms and hooks on load
document.addEventListener('DOMContentLoaded', () => {
    const addDirForm = document.getElementById('admin-add-directory-form');
    if (addDirForm) {
        addDirForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const pathInput = document.getElementById('admin-new-directory-path');
            const path = pathInput.value.trim();
            if (!path) return;

            try {
                const res = await fetch('/api/v1/integrations/directories', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path })
                });
                if (!res.ok) {
                    const err = await res.json();
                    throw new Error(err.detail || 'Failed to add directory');
                }
                showToast('Directory added successfully.');
                pathInput.value = '';
                window.loadAdminDirectories();
            } catch (err) {
                showToast(err.message);
            }
        });
    }
});
