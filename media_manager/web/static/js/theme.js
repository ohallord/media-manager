// Theme Switcher JS (Light / Dark / System)
//
// `mode` is what the user picked (light/dark/system, persisted verbatim so the right
// button stays highlighted). `data-theme` on <html> - the attribute main.css actually
// reads - is always resolved to a concrete 'light' or 'dark', since "system" isn't a
// real palette: it means "whatever the OS prefers right now".
function resolveTheme(mode) {
    if (mode === 'system') {
        const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
        return prefersLight ? 'light' : 'dark';
    }
    return mode;
}

function setTheme(mode) {
    document.documentElement.setAttribute('data-theme', resolveTheme(mode));
    localStorage.setItem('portal-theme', mode);

    document.querySelectorAll('.theme-btn').forEach(btn => btn.classList.remove('active'));
    const activeBtn = document.getElementById('theme-btn-' + mode);
    if (activeBtn) activeBtn.classList.add('active');
}

document.addEventListener('DOMContentLoaded', () => {
    const savedTheme = localStorage.getItem('portal-theme') || 'system';
    setTheme(savedTheme);

    const btnLight = document.getElementById('theme-btn-light');
    const btnDark = document.getElementById('theme-btn-dark');
    const btnSystem = document.getElementById('theme-btn-system');

    if (btnLight) btnLight.addEventListener('click', () => setTheme('light'));
    if (btnDark) btnDark.addEventListener('click', () => setTheme('dark'));
    if (btnSystem) btnSystem.addEventListener('click', () => setTheme('system'));

    // Live-update if the OS theme changes while "system" is selected.
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
            if ((localStorage.getItem('portal-theme') || 'system') === 'system') {
                setTheme('system');
            }
        });
    }

    const versionEl = document.getElementById('app-version');
    if (versionEl) {
        fetch('/api/v1/version')
            .then(res => res.json())
            .then(data => { versionEl.textContent = `v${data.version}`; })
            .catch(() => { versionEl.textContent = ''; });
    }
});
