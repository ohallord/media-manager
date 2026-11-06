// Views (hash routes) only admins may open.
window.ADMIN_ONLY_VIEWS = ['admin', 'paths', 'users', 'playback', 'cleanup', 'transcode', 'downloads', 'duplicates', 'logs'];

function currentViewId() {
    return (location.hash || '').replace('#', '').split(/[/?]/)[0] || 'inventory';
}

window.isAdminUser = function() {
    return !!(window.currentUser && window.currentUser.role === 'admin');
};

// Auth & Session Management JS
window.currentUser = null;

async function checkSession() {
    try {
        const res = await fetch('/api/v1/auth/me');
        if (!res.ok) throw new Error('Not logged in');
        const data = await res.json();
        if (!data.user) throw new Error('Not logged in');
        window.currentUser = data.user;
        updateUserUI();
        closeAuthModal(); // Close modal if session is valid!
        if (data.user.must_change_password) {
            window.showForcedPasswordChangeModal();
        }
    } catch (err) {
        window.currentUser = null;
        updateUserUI();
        openAuthModal(true); // Force login (hides close button)
    }
}

function updateUserUI() {
    const appContainer = document.querySelector('.app-container');
    const displayName = document.getElementById('user-display-name');
    const authBtn = document.getElementById('auth-action-btn');

    if (window.currentUser) {
        if (appContainer) appContainer.style.display = 'flex';
        if (displayName) {
            displayName.innerHTML = `
                <span>${window.currentUser.username} (${window.currentUser.role})</span>
                <button class="btn btn-secondary btn-sm" onclick="window.openChangePasswordModal()" style="margin-left: 0.5rem; font-size: 0.75rem; padding: 2px 6px;">Password</button>
            `;
        }
        if (authBtn) {
            authBtn.textContent = 'Logout';
            authBtn.onclick = handleLogout;
        }
    } else {
        if (appContainer) appContainer.style.display = 'none';
        if (displayName) displayName.textContent = 'Guest';
        if (authBtn) {
            authBtn.textContent = 'Login';
            authBtn.onclick = () => openAuthModal(true);
        }
    }

    // Admin-only pages: hidden from the nav for everyone else, and bounced
    // to Inventory if reached by URL. The APIs behind them are admin-only too.
    const isAdmin = !!(window.currentUser && window.currentUser.role === 'admin');
    document.querySelectorAll('.admin-only').forEach(el => { el.style.display = isAdmin ? 'flex' : 'none'; });
    const view = currentViewId();
    if (window.ADMIN_ONLY_VIEWS.includes(view)) {
        if (!isAdmin) {
            location.hash = '#inventory';
        } else if (typeof navigateTo === 'function') {
            navigateTo(view);  // the first navigation ran before we knew the user was an admin
        }
    }
}

function openAuthModal(force = false) {
    const modal = document.getElementById('auth-modal');
    if (modal) {
        modal.style.display = 'flex';
        const closeBtn = document.getElementById('close-auth-modal');
        if (closeBtn) {
            closeBtn.style.display = force ? 'none' : 'block';
        }
    }
}

function closeAuthModal() {
    if (!window.currentUser) {
        showToast('You must log in to view the dashboard.');
        return;
    }
    const modal = document.getElementById('auth-modal');
    if (modal) modal.style.display = 'none';
}

async function handleLogout() {
    await fetch('/api/v1/auth/logout', { method: 'POST' });
    window.currentUser = null;
    updateUserUI();
    showToast('Logged out successfully.');
    checkSession(); // Force log in modal again
}

window.openChangePasswordModal = async function() {
    if (!window.currentUser) {
        showToast('Please log in first.');
        openAuthModal();
        return;
    }
    const currentPassword = prompt('Enter your current password:');
    if (!currentPassword) return;
    const newPassword = prompt('Enter your new password:');
    if (!newPassword || !newPassword.trim()) return;

    try {
        const res = await fetch('/api/v1/auth/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ current_password: currentPassword, new_password: newPassword.trim() })
        });
        if (!res.ok) {
            const data = await res.json();
            throw new Error(data.detail || 'Failed to change password');
        }
        showToast('Your password has been changed successfully!');
    } catch (err) {
        showToast(err.message);
    }
};

window.showForcedPasswordChangeModal = function() {
    if (document.getElementById('forced-pwd-change-modal')) return; // already showing

    const modal = document.createElement('div');
    modal.id = 'forced-pwd-change-modal';
    modal.className = 'shelf-dialog';

    modal.innerHTML = `
        <div class="section-card" style="width: 100%; max-width: 420px; padding: 2rem; border: 1px solid var(--border-color); text-align: left; box-shadow: none;">
            <h3 style="margin-top: 0; margin-bottom: 0.75rem; font-size: 1.3rem; display: flex; align-items: center; gap: 8px;">🔒 Password Reset Required</h3>
            <p style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 1.25rem;">
                An administrator set or reset this account's password. You must choose a new password before continuing.
            </p>
            <form id="forced-pwd-change-form">
                <div style="margin-bottom: 12px;">
                    <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Current (Temporary) Password</label>
                    <input type="password" id="fpc-current" required class="search-input" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                </div>
                <div style="margin-bottom: 1.25rem;">
                    <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">New Password</label>
                    <div style="position: relative; display: flex; align-items: center;">
                        <input type="password" id="fpc-new" required class="search-input" style="width: 100%; padding: 8px 12px; padding-right: 2.5rem; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                        <button type="button" onclick="window.togglePasswordVisibility('fpc-new', this)" style="position: absolute; right: 8px; background: none; border: none; color: var(--text-secondary); cursor: pointer; padding: 4px; font-size: 1rem; line-height: 1; display: flex; align-items: center; outline: none;">👁️</button>
                    </div>
                </div>
                <div style="display: flex; justify-content: flex-end;">
                    <button type="submit" class="btn btn-primary" style="padding: 8px 22px; font-size: 0.85rem;">Set New Password</button>
                </div>
            </form>
        </div>
    `;

    document.body.appendChild(modal);

    modal.querySelector('#forced-pwd-change-form').onsubmit = async (e) => {
        e.preventDefault();
        const currentPassword = modal.querySelector('#fpc-current').value;
        const newPassword = modal.querySelector('#fpc-new').value.trim();
        if (!newPassword) return;

        try {
            const res = await fetch('/api/v1/auth/change-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
            });
            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to change password');
            }
            if (window.currentUser) window.currentUser.must_change_password = false;
            modal.remove();
            showToast('Your password has been updated.');
        } catch (err) {
            showToast(err.message);
        }
    };
};

document.addEventListener('DOMContentLoaded', () => {
    checkSession();

    const loginForm = document.getElementById('login-form');
    const closeBtn = document.getElementById('close-auth-modal');

    if (closeBtn) closeBtn.addEventListener('click', closeAuthModal);

    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('login-username').value;
            const password = document.getElementById('login-password').value;

            try {
                const res = await fetch('/api/v1/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });

                if (!res.ok) {
                    const errData = await res.json();
                    throw new Error(errData.detail || 'Login failed');
                }

                const data = await res.json();
                console.log('Login Response:', data);
                if (data.status === 'requires_2fa') {
                    const code = prompt(`Two-factor verification is required. A code was sent via ${data.two_factor_method.toUpperCase()}.`);
                    if (!code || !code.trim()) {
                        throw new Error('Verification code is required');
                    }
                    const verifyRes = await fetch('/api/v1/auth/verify-2fa', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ temp_token: data.temp_token, code: code.trim() })
                    });
                    if (!verifyRes.ok) {
                        const errData = await verifyRes.json();
                        throw new Error(errData.detail || '2FA verification failed');
                    }
                    const verifyData = await verifyRes.json();

                    console.log('2FA Login Result:', verifyData);
                    if (verifyData && verifyData.user) {
                        window.currentUser = verifyData.user;
                        updateUserUI();
                        closeAuthModal();
                        if (verifyData.user.must_change_password) {
                            window.showForcedPasswordChangeModal();
                        } else {
                            showToast(`Welcome back, ${verifyData.user.username}!`);
                        }
                    } else {
                        throw new Error('Verification succeeded but user details are missing.');
                    }
                } else {
                    if (data && data.user) {
                        window.currentUser = data.user;
                        updateUserUI();
                        closeAuthModal();
                        if (data.user.must_change_password) {
                            window.showForcedPasswordChangeModal();
                        } else {
                            showToast(`Welcome back, ${data.user.username}!`);
                        }
                    } else {
                        throw new Error('Login succeeded but user details are missing.');
                    }
                }
                if (location.hash === '#admin') {
                    if (window.loadAdminView) window.loadAdminView();
                }
            } catch (err) {
                showToast(err.message);
            }
        });
    }

    const forgotLink = document.getElementById('forgot-password-link');
    if (forgotLink) {
        forgotLink.addEventListener('click', (e) => {
            e.preventDefault();
            openForgotPasswordModal();
        });
    }
});

function openForgotPasswordModal() {
    const modal = document.createElement('div');
    modal.id = 'forgot-password-modal';
    modal.className = 'shelf-dialog';
    
    modal.innerHTML = `
        <div class="section-card" style="width: 100%; max-width: 400px; padding: 2rem; border: 1px solid var(--border-color); text-align: left; box-shadow: none;">
            <h3 style="margin-top: 0; margin-bottom: 1.25rem; font-size: 1.3rem; display: flex; align-items: center; gap: 8px;">🔑 Reset User Password</h3>
            
            <form id="forgot-username-form">
                <div style="margin-bottom: 1.25rem;">
                    <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 6px;">Enter your Username</label>
                    <input type="text" id="fp-username" required class="search-input" placeholder="e.g. jdoe" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                </div>
                <div style="display: flex; gap: 12px; justify-content: flex-end;">
                    <button type="button" id="fp-close-btn" class="btn btn-secondary" style="padding: 8px 18px; font-size: 0.85rem;">Cancel</button>
                    <button type="submit" class="btn btn-primary" style="padding: 8px 22px; font-size: 0.85rem;">Send Verification Code</button>
                </div>
            </form>
        </div>
    `;

    document.body.appendChild(modal);

    modal.querySelector('#fp-close-btn').onclick = () => modal.remove();
    
    modal.querySelector('#forgot-username-form').onsubmit = async (e) => {
        e.preventDefault();
        const username = modal.querySelector('#fp-username').value.trim();
        if (!username) return;

        try {
            const res = await fetch('/api/v1/auth/forgot-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username })
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to request reset');
            }

            const data = await res.json();
            transitionToVerifyReset(modal, username, data.temp_token, data.method);
        } catch (err) {
            showToast(`Error: ${err.message}`);
        }
    };
}

function transitionToVerifyReset(modal, username, tempToken, method) {
    const card = modal.querySelector('.section-card');
    card.innerHTML = `
        <h3 style="margin-top: 0; margin-bottom: 1.25rem; font-size: 1.3rem; display: flex; align-items: center; gap: 8px;">🔑 Reset User Password</h3>
        <p style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 1.25rem;">
            A verification code was sent to your registered <b>${method.toUpperCase()}</b>. Please enter the code and your new password below.
        </p>
        
        <form id="forgot-verify-form">
            <div style="margin-bottom: 12px;">
                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">Verification Code</label>
                <input type="text" id="fv-code" class="search-input" placeholder="e.g. 123456" style="width: 100%; padding: 8px 12px; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
            </div>
            
            <div style="margin-bottom: 1.25rem;">
                <label style="font-size: 0.78rem; font-weight: 600; color: var(--text-secondary); display: block; margin-bottom: 4px;">New Password</label>
                <div style="position: relative; display: flex; align-items: center;">
                    <input type="password" id="fv-password" required class="search-input" placeholder="••••••••" style="width: 100%; padding: 8px 12px; padding-right: 2.5rem; font-size: 0.88rem; border: 1px solid var(--border-color); color: var(--text-primary);">
                    <button type="button" onclick="window.togglePasswordVisibility('fv-password', this)" style="position: absolute; right: 8px; background: none; border: none; color: var(--text-secondary); cursor: pointer; padding: 4px; font-size: 1rem; line-height: 1; display: flex; align-items: center; outline: none;">👁️</button>
                </div>
            </div>
            
            <div style="display: flex; gap: 12px; justify-content: flex-end;">
                <button type="button" id="fv-cancel-btn" class="btn btn-secondary" style="padding: 8px 18px; font-size: 0.85rem;">Cancel</button>
                <button type="submit" class="btn btn-primary" style="padding: 8px 22px; font-size: 0.85rem;">Reset Password</button>
            </div>
        </form>
    `;

    card.querySelector('#fv-cancel-btn').onclick = () => modal.remove();

    card.querySelector('#forgot-verify-form').onsubmit = async (e) => {
        e.preventDefault();
        const code = card.querySelector('#fv-code').value.trim();
        const newPassword = card.querySelector('#fv-password').value;

        try {
            const res = await fetch('/api/v1/auth/reset-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ temp_token: tempToken, code, new_password: newPassword })
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to reset password');
            }

            showToast('Your password has been successfully reset! You can now log in.');
            modal.remove();
        } catch (err) {
            showToast(`Error: ${err.message}`);
        }
    };
}

window.togglePasswordVisibility = function(inputId, buttonEl) {
    const input = document.getElementById(inputId);
    if (!input) return;
    if (input.type === 'password') {
        input.type = 'text';
        buttonEl.textContent = '🙈';
    } else {
        input.type = 'password';
        buttonEl.textContent = '👁️';
    }
};

window.openAuthModal = openAuthModal;
