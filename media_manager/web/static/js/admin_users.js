// Admin User Management Panel JS
window.loadAdminUsers = async function() {
    const container = document.getElementById('admin-users-list');
    if (!container) return;

    // Adjust card visibilities based on role
    const isMainAdmin = window.currentUser && window.currentUser.role === 'admin';
    
    const integrationsCard = document.getElementById('admin-integrations-list') ? document.getElementById('admin-integrations-list').closest('.section-card') : null;
    const directoriesCard = document.getElementById('admin-directories-list') ? document.getElementById('admin-directories-list').closest('.section-card') : null;
    const createUserBtn = document.getElementById('create-user-modal-btn');

    if (integrationsCard) integrationsCard.style.display = isMainAdmin ? 'block' : 'none';
    if (directoriesCard) directoriesCard.style.display = isMainAdmin ? 'block' : 'none';
    if (createUserBtn) createUserBtn.style.display = isMainAdmin ? 'inline-block' : 'none';

    try {
        const res = await fetch('/api/v1/admin/users');
        if (!res.ok) throw new Error('Failed to load user information.');
        const data = await res.json();

        window.loadedUsers = data.users;
        renderAdminUsers(data.users, container);
    } catch (err) {
        container.innerHTML = `<p class="error-msg">${err.message}</p>`;
    }
};

function formatLastLogin(u) {
    return u.last_login_at ? new Date(u.last_login_at).toLocaleString() : 'Never';
}

function mustResetBadge(u) {
    return u.must_change_password
        ? `<span class="badge" style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid #f59e0b;" title="User must set a new password on next login">⚠️ Reset Pending</span>`
        : '';
}

function renderAdminUsers(users, container) {
    if (!users || users.length === 0) {
        container.innerHTML = '<p class="muted">No user accounts found.</p>';
        return;
    }

    const isCallerAdmin = window.currentUser && window.currentUser.role === 'admin';

    container.innerHTML = `
        <div class="responsive-table-wrapper admin-users-table-desktop">
            <table style="width: 100%; border-collapse: collapse; margin-top: 1rem;">
                <thead>
                    <tr style="border-bottom: 1px solid var(--border-color); text-align: left; font-size: 0.85rem;" class="muted">
                        <th style="padding: 0.6rem 0.5rem;">Username</th>
                        <th style="padding: 0.6rem 0.5rem;">Full Name</th>
                        <th style="padding: 0.6rem 0.5rem;">Contact Info</th>
                        <th style="padding: 0.6rem 0.5rem;">Role</th>
                        <th style="padding: 0.6rem 0.5rem;">Two-Factor</th>
                        <th style="padding: 0.6rem 0.5rem;">Last Login</th>
                        <th style="padding: 0.6rem 0.5rem;">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${users.map(u => {
                        const fullName = [u.first_name || '', u.last_name || ''].join(' ').trim() || 'N/A';
                        const contact = [
                            u.email ? `✉️ ${u.email}` : '',
                            u.phone ? `📱 ${u.phone}` : ''
                        ].filter(Boolean).join('<br>') || 'N/A';

                        const tfaBadge = u.two_factor_enabled
                            ? `<span class="badge" style="background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid #10b981;">🔒 ${u.two_factor_method.toUpperCase()}</span>`
                            : `<span class="badge text-muted" style="border: 1px solid var(--border-color);">None</span>`;

                        return `
                            <tr style="border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.9rem;">
                                <td style="padding: 0.75rem 0.5rem;"><b>${escapeHtml(u.username)}</b></td>
                                <td style="padding: 0.75rem 0.5rem; color: var(--text-secondary);">${escapeHtml(fullName)}</td>
                                <td style="padding: 0.75rem 0.5rem; font-size: 0.8rem; line-height: 1.3;">${contact}</td>
                                <td style="padding: 0.75rem 0.5rem;"><span class="badge ${u.role === 'admin' ? 'badge-4k' : 'badge-sd'}">${u.role}</span></td>
                                <td style="padding: 0.75rem 0.5rem;">${tfaBadge}</td>
                                <td style="padding: 0.75rem 0.5rem; font-size: 0.78rem; color: var(--text-secondary);">
                                    ${formatLastLogin(u)}
                                    ${mustResetBadge(u) ? `<div style="margin-top: 4px;">${mustResetBadge(u)}</div>` : ''}
                                </td>
                                <td style="padding: 0.75rem 0.5rem; display: flex; gap: 0.5rem; flex-wrap: wrap;">
                                    ${isCallerAdmin ? (
                                        u.role === 'admin' ?
                                            `<button class="btn btn-secondary btn-sm" disabled style="opacity: 0.5; cursor: not-allowed; min-width: 140px; justify-content: center;" title="Admin accounts cannot be demoted">Protected</button>` :
                                            `<button class="btn btn-secondary btn-sm" onclick="toggleUserRole('${u.id}', 'admin')" style="min-width: 140px; justify-content: center;">Make Admin</button>`
                                    ) : ''}
                                    ${isCallerAdmin ? `<button class="btn btn-secondary btn-sm" onclick="adminResetPassword('${u.id}', '${u.username}')" style="min-width: 140px; justify-content: center;">🔑 Reset Password</button>` : ''}
                                    <button class="btn btn-secondary btn-sm" onclick="editUser('${u.id}')" style="min-width: 140px; justify-content: center;">✏️ Edit</button>
                                    <button class="btn btn-danger btn-sm" onclick="deleteUser('${u.id}', '${u.username}')" style="min-width: 140px; justify-content: center;">🗑️ Delete</button>
                                </td>
                            </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        </div>

        <div class="admin-user-cards-mobile">
            ${users.map(u => {
                const fullName = [u.first_name || '', u.last_name || ''].join(' ').trim() || 'N/A';
                const contact = [
                    u.email ? `✉️ ${u.email}` : '',
                    u.phone ? `📱 ${u.phone}` : ''
                ].filter(Boolean).join(' • ') || 'N/A';

                const tfaBadge = u.two_factor_enabled
                    ? `<span class="badge" style="background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid #10b981;">🔒 ${u.two_factor_method.toUpperCase()}</span>`
                    : `<span class="badge text-muted" style="border: 1px solid var(--border-color);">None</span>`;

                return `
                    <div class="admin-user-card">
                        <div class="admin-user-card-header">
                            <span style="font-weight: 700; font-size: 1rem; color: var(--text-primary);">👤 ${escapeHtml(u.username)}</span>
                            <span class="badge ${u.role === 'admin' ? 'badge-4k' : 'badge-sd'}">${u.role}</span>
                        </div>
                        <div class="admin-user-card-body">
                            <div><b>Name:</b> ${escapeHtml(fullName)}</div>
                            <div><b>Contact:</b> ${contact}</div>
                            <div><b>2FA:</b> ${tfaBadge}</div>
                            <div><b>Last Login:</b> ${formatLastLogin(u)}</div>
                            ${mustResetBadge(u) ? `<div style="margin-top: 4px;">${mustResetBadge(u)}</div>` : ''}
                        </div>
                        <div class="admin-user-card-actions" style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem;">
                            ${isCallerAdmin ? (
                                u.role === 'admin' ?
                                    `<button class="btn btn-secondary btn-sm" disabled style="opacity: 0.5; cursor: not-allowed; font-size: 0.78rem; min-width: 120px; justify-content: center;">Protected</button>` :
                                    `<button class="btn btn-secondary btn-sm" onclick="toggleUserRole('${u.id}', 'admin')" style="font-size: 0.78rem; min-width: 120px; justify-content: center;">Make Admin</button>`
                            ) : ''}
                            ${isCallerAdmin ? `<button class="btn btn-secondary btn-sm" onclick="adminResetPassword('${u.id}', '${u.username}')" style="font-size: 0.78rem; min-width: 120px; justify-content: center;">🔑 Reset Password</button>` : ''}
                            <button class="btn btn-secondary btn-sm" onclick="editUser('${u.id}')" style="font-size: 0.78rem; min-width: 120px; justify-content: center;">✏️ Edit</button>
                            <button class="btn btn-danger btn-sm" onclick="deleteUser('${u.id}', '${u.username}')" style="font-size: 0.78rem; min-width: 120px; justify-content: center;">🗑️ Delete</button>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

async function toggleUserRole(userId, newRole) {
    try {
        const res = await fetch(`/api/v1/admin/users/${userId}/role`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: newRole })
        });
        if (!res.ok) {
            const data = await res.json();
            throw new Error(data.detail || 'Failed to update role');
        }
        window.loadAdminUsers();
    } catch (err) {
        showToast(err.message);
    }
}

async function adminResetPassword(userId, username) {
    const modal = document.createElement('div');
    modal.id = 'admin-reset-pwd-modal';
    modal.className = 'shelf-dialog';
    
    modal.innerHTML = `
        <div class="section-card" style="width: 100%; max-width: 400px; padding: 2rem; border: 1px solid var(--border-color); text-align: left; box-shadow: none;">
            <h3 style="margin-top: 0; margin-bottom: 1.25rem; font-size: 1.3rem; display: flex; align-items: center; gap: 8px;">🔑 Reset User Password</h3>
            
            <form id="admin-reset-pwd-form">
                <div style="margin-bottom: 1.25rem;">
                    <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 6px;">New Password for <b>${username}</b></label>
                    <div style="position: relative; display: flex; align-items: center;">
                        <input type="password" id="arp-password" required class="search-input" placeholder="••••••••" style="width: 100%; padding: 8px 12px; padding-right: 2.5rem; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                        <button type="button" onclick="window.togglePasswordVisibility('arp-password', this)" style="position: absolute; right: 8px; background: none; border: none; color: var(--text-secondary); cursor: pointer; padding: 4px; font-size: 1rem; line-height: 1; display: flex; align-items: center; outline: none;">👁️</button>
                    </div>
                </div>
                <div style="display: flex; gap: 12px; justify-content: flex-end;">
                    <button type="button" id="arp-close-btn" class="btn btn-secondary" style="padding: 8px 18px; font-size: 0.85rem;">Cancel</button>
                    <button type="submit" class="btn btn-primary" style="padding: 8px 22px; font-size: 0.85rem;">Reset Password</button>
                </div>
            </form>
        </div>
    `;

    document.body.appendChild(modal);

    modal.querySelector('#arp-close-btn').onclick = () => modal.remove();
    
    modal.querySelector('#admin-reset-pwd-form').onsubmit = async (e) => {
        e.preventDefault();
        const newPassword = modal.querySelector('#arp-password').value;
        if (!newPassword || !newPassword.trim()) return;

        try {
            const res = await fetch(`/api/v1/admin/users/${userId}/password`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ new_password: newPassword.trim() })
            });
            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to reset password');
            }
            showToast(`Password for user '${username}' reset successfully! They'll be required to set a new password on their next login.`);
            modal.remove();
            window.loadAdminUsers();
        } catch (err) {
            showToast(err.message);
        }
    };
}

// Interactive Modern Create User Modal
document.addEventListener('DOMContentLoaded', () => {
    const createBtn = document.getElementById('create-user-modal-btn');
    if (createBtn) {
        createBtn.addEventListener('click', () => {
            // Build the modal form element
            const modal = document.createElement('div');
            modal.id = 'create-user-modal';
            modal.className = 'shelf-dialog';
            
            modal.innerHTML = `
                <div class="section-card" style="width: 100%; max-width: 500px; padding: 2rem; border: 1px solid var(--border-color); text-align: left; box-shadow: none;">
                    <h3 style="margin-top: 0; margin-bottom: 1.25rem; font-size: 1.3rem; display: flex; align-items: center; gap: 8px;">👤 Add New User Account</h3>
                    
                    <form id="create-user-form">
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                            <div>
                                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">First Name *</label>
                                <input type="text" id="cu-firstname" required class="form-control" placeholder="e.g. John" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                            </div>
                            <div>
                                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Last Name *</label>
                                <input type="text" id="cu-lastname" required class="form-control" placeholder="e.g. Doe" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                            </div>
                        </div>

                        <div style="margin-bottom: 12px;">
                            <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Username *</label>
                            <input type="text" id="cu-username" required class="form-control" placeholder="e.g. jdoe" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                        </div>

                        <div style="margin-bottom: 12px;">
                            <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Password *</label>
                            <div style="position: relative; display: flex; align-items: center;">
                                <input type="password" id="cu-password" required class="form-control" placeholder="••••••••" style="width: 100%; padding: 8px 12px; padding-right: 2.5rem; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                                <button type="button" onclick="window.togglePasswordVisibility('cu-password', this)" style="position: absolute; right: 8px; background: none; border: none; color: var(--text-secondary); cursor: pointer; padding: 4px; font-size: 1rem; line-height: 1; display: flex; align-items: center; outline: none;">👁️</button>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                            <div>
                                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Email</label>
                                <input type="email" id="cu-email" class="form-control" placeholder="jdoe@example.com" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                            </div>
                            <div>
                                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Phone Number</label>
                                <input type="tel" id="cu-phone" class="form-control" placeholder="+15550199" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                            </div>
                        </div>

                        <p style="font-size: 0.72rem; color: var(--text-secondary); margin-top: -4px; margin-bottom: 12px;"><i>* Note: Either Email or Phone is required to allow notification routing.</i></p>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px;">
                            <div>
                                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Role</label>
                                <select id="cu-role" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                                    <option value="user">User</option>
                                    <option value="admin">Admin</option>
                                </select>
                            </div>
                            <div>
                                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Two-Factor Verification</label>
                                <select id="cu-2fa" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                                    <option value="none">None</option>
                                    <option value="email">Email</option>
                                    <option value="phone">SMS / Phone</option>
                                </select>
                            </div>
                        </div>

                        <div style="display: flex; gap: 12px; justify-content: flex-end; margin-top: 1.5rem;">
                            <button type="button" id="cu-cancel-btn" class="btn btn-secondary" style="padding: 8px 18px; font-size: 0.85rem;">Cancel</button>
                            <button type="submit" class="btn btn-primary" style="padding: 8px 22px; font-size: 0.85rem;">Create Account</button>
                        </div>
                    </form>
                </div>
            `;

            document.body.appendChild(modal);

            modal.querySelector('#cu-cancel-btn').onclick = () => modal.remove();
            
            modal.querySelector('#create-user-form').onsubmit = async (e) => {
                e.preventDefault();
                
                const payload = {
                    username: modal.querySelector('#cu-username').value.trim(),
                    password: modal.querySelector('#cu-password').value,
                    first_name: modal.querySelector('#cu-firstname').value.trim(),
                    last_name: modal.querySelector('#cu-lastname').value.trim(),
                    email: modal.querySelector('#cu-email').value.trim() || null,
                    phone: modal.querySelector('#cu-phone').value.trim() || null,
                    role: modal.querySelector('#cu-role').value,
                    two_factor_method: modal.querySelector('#cu-2fa').value
                };

                try {
                    const res = await fetch('/api/v1/admin/users', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    
                    if (!res.ok) {
                        const data = await res.json();
                        throw new Error(data.detail || 'Failed to create user');
                    }
                    
                    showToast(`User '${payload.username}' created successfully! They'll be required to set a new password on their first login. Hardware Passkey credential setup can be finalized on their first login profile screen.`);
                    modal.remove();
                    window.loadAdminUsers();
                } catch (err) {
                    showToast(`Error creating user: ${err.message}`);
                }
            };
        });
    }
});

window.editUser = function(userId) {
    if (!window.loadedUsers) return;
    const u = window.loadedUsers.find(user => user.id === userId);
    if (!u) return;

    const modal = document.createElement('div');
    modal.id = 'edit-user-modal';
    modal.className = 'shelf-dialog';

    const isAdmin = window.currentUser && window.currentUser.role === 'admin';
    const roleSelectHTML = isAdmin ? `
        <div style="margin-bottom: 16px;">
            <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Role</label>
            <select id="eu-role" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary); outline: none;">
                <option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option>
                <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
            </select>
        </div>
    ` : '';

    modal.innerHTML = `
        <div class="section-card" style="width: 100%; max-width: 500px; padding: 2rem; border: 1px solid var(--border-color); text-align: left; box-shadow: none;">
            <h3 style="margin-top: 0; margin-bottom: 1.25rem; font-size: 1.3rem; display: flex; align-items: center; gap: 8px;">✏️ Edit User Information</h3>
            
            <form id="edit-user-form">
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                    <div>
                        <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">First Name *</label>
                        <input type="text" id="eu-firstname" required value="${u.first_name || ''}" class="form-control" placeholder="e.g. John" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary); outline: none;">
                    </div>
                    <div>
                        <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Last Name *</label>
                        <input type="text" id="eu-lastname" required value="${u.last_name || ''}" class="form-control" placeholder="e.g. Doe" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary); outline: none;">
                    </div>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                    <div>
                        <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Email</label>
                        <input type="email" id="eu-email" value="${u.email || ''}" class="form-control" placeholder="jdoe@example.com" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary); outline: none;">
                    </div>
                    <div>
                        <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Phone Number</label>
                        <input type="tel" id="eu-phone" value="${u.phone || ''}" class="form-control" placeholder="+15550199" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary); outline: none;">
                    </div>
                </div>

                <p style="font-size: 0.72rem; color: var(--text-secondary); margin-top: -4px; margin-bottom: 12px;"><i>* Note: Either Email or Phone is required to allow notification routing.</i></p>

                <div style="margin-bottom: 16px;">
                    <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Two-Factor Verification</label>
                    <select id="eu-2fa" class="form-control" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary); outline: none;">
                        <option value="none" ${u.two_factor_method === 'none' ? 'selected' : ''}>None</option>
                        <option value="email" ${u.two_factor_method === 'email' ? 'selected' : ''}>Email</option>
                        <option value="phone" ${u.two_factor_method === 'phone' ? 'selected' : ''}>SMS / Phone</option>
                    </select>
                </div>

                ${roleSelectHTML}

                <div style="display: flex; gap: 12px; justify-content: flex-end; margin-top: 1.5rem;">
                    <button type="button" id="eu-cancel-btn" class="btn btn-secondary" style="padding: 8px 18px; font-size: 0.85rem;">Cancel</button>
                    <button type="submit" class="btn btn-primary" style="padding: 8px 22px; font-size: 0.85rem;">Save Changes</button>
                </div>
            </form>
        </div>
    `;

    document.body.appendChild(modal);

    modal.querySelector('#eu-cancel-btn').onclick = () => modal.remove();

    modal.querySelector('#edit-user-form').onsubmit = async (e) => {
        e.preventDefault();

        const payload = {
            first_name: modal.querySelector('#eu-firstname').value.trim(),
            last_name: modal.querySelector('#eu-lastname').value.trim(),
            email: modal.querySelector('#eu-email').value.trim() || null,
            phone: modal.querySelector('#eu-phone').value.trim() || null,
            two_factor_method: modal.querySelector('#eu-2fa').value
        };

        if (isAdmin) {
            payload.role = modal.querySelector('#eu-role').value;
        }

        try {
            const res = await fetch(`/api/v1/admin/users/${userId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to update user information');
            }

            showToast('User information updated successfully.');
            modal.remove();
            window.loadAdminUsers();
        } catch (err) {
            showToast(`Error editing user: ${err.message}`);
        }
    };
};

window.deleteUser = function(userId, username) {
    const modal = document.createElement('div');
    modal.id = 'delete-user-modal';
    modal.className = 'shelf-dialog';

    modal.innerHTML = `
        <div class="section-card" style="width: 100%; max-width: 450px; padding: 2rem; border: 1px solid var(--border-color); text-align: left; box-shadow: none;">
            <h3 style="margin-top: 0; margin-bottom: 0.75rem; font-size: 1.3rem; display: flex; align-items: center; gap: 8px; color: #ef4444;">⚠️ Delete User Account</h3>
            <p style="font-size: 0.88rem; color: var(--text-secondary); margin-bottom: 1.25rem;">
                Are you absolutely sure you want to delete the account <b>${username}</b>?
                This action is permanent and cannot be undone. All settings and credentials for this user will be removed.
            </p>
            
            <div style="margin-bottom: 1.25rem;">
                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 6px;">
                    Please type <b>${username}</b> or the user's ID below to confirm:
                </label>
                <input type="text" id="du-confirm-input" class="search-input" placeholder="Type username or ID" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary); outline: none;">
            </div>

            <div style="display: flex; gap: 12px; justify-content: flex-end;">
                <button type="button" id="du-cancel-btn" class="btn btn-secondary" style="padding: 8px 18px; font-size: 0.85rem;">Cancel</button>
                <button type="button" id="du-confirm-btn" class="btn btn-danger" disabled style="padding: 8px 22px; font-size: 0.85rem; opacity: 0.5; cursor: not-allowed;">Confirm Delete</button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    const confirmInput = modal.querySelector('#du-confirm-input');
    const confirmBtn = modal.querySelector('#du-confirm-btn');
    const cancelBtn = modal.querySelector('#du-cancel-btn');

    confirmInput.oninput = () => {
        const val = confirmInput.value.trim();
        if (val === username || val === userId) {
            confirmBtn.disabled = false;
            confirmBtn.style.opacity = '1';
            confirmBtn.style.cursor = 'pointer';
        } else {
            confirmBtn.disabled = true;
            confirmBtn.style.opacity = '0.5';
            confirmBtn.style.cursor = 'not-allowed';
        }
    };

    cancelBtn.onclick = () => modal.remove();

    confirmBtn.onclick = async () => {
        try {
            const res = await fetch(`/api/v1/admin/users/${userId}`, {
                method: 'DELETE'
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to delete user');
            }

            showToast(`User '${username}' has been successfully deleted.`);
            modal.remove();

            // If the deleted user is the currently logged in user, logout and refresh
            if (window.currentUser && window.currentUser.id === userId) {
                await fetch('/api/v1/auth/logout', { method: 'POST' });
                window.location.reload();
            } else {
                window.loadAdminUsers();
            }
        } catch (err) {
            showToast(`Error deleting user: ${err.message}`);
        }
    };
};

