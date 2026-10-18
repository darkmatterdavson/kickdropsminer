/* Kick sign in via session cookie. Replaces TwitchDropsMiner's browser-login.js panel. */
class KickLoginPanel {
    constructor() {
        this.data = null;
        this.busy = false;
        this.error = '';
        this.el('kick-login-form').addEventListener('submit', e => { e.preventDefault(); this.submit(); });
        this.el('twitch-logout').addEventListener('click', () => this.logout());
        this.render();
    }

    el(id) { return document.getElementById(id); }
    t() { return (typeof state !== 'undefined' && state.translations.gui?.browser_login) || {}; }

    async load() {
        try {
            const response = await fetch('/api/session', { cache: 'no-store' });
            if (response.ok) this.updateStatus(await response.json());
        } catch (_) { /* socket reconnect retries */ }
    }

    updateStatus(data) {
        if (!data || typeof data.logged_in !== 'boolean') return;
        this.data = data;
        this.render();
    }

    updateLogin(login) {
        if (!this.data) return;
        const loggedIn = Boolean(login?.user_id);
        if (loggedIn !== this.data.logged_in) { this.data.logged_in = loggedIn; this.render(); }
    }

    render() {
        const t = this.t();
        const set = (id, text) => { if (text) this.el(id).textContent = text; };
        set('twitch-login-title', t.title);
        set('twitch-login-instructions', t.instructions);
        set('kick-cookie-label', t.cookie_label);
        set('kick-login-submit', t.sign_in);
        set('twitch-logout', t.logout);
        set('twitch-logout-help', t.logout_help);
        this.el('kick-token-hint').textContent = t.token_hint || '';
        if (!this.data) return;
        const normal = this.data.logged_in === true;
        this.el('dashboard').hidden = !normal;
        this.el('twitch-login-screen').hidden = normal;
        const access = this.el('dashboard-access-controls');
        const slot = this.el(normal ? 'dashboard-access-slot' : 'twitch-login-access');
        if (access && slot && access.parentElement !== slot) slot.appendChild(access);
        const status = this.busy ? (t.verifying || 'Verifying…')
            : (!normal && this.data.has_token && this.data.state === 'error') ? (t.error || 'Sign in failed') : '';
        this.el('twitch-login-status').textContent = status;
        this.el('twitch-login-error').textContent = this.error
            || (!normal && this.data.state === 'error' && this.data.error ? this.data.error : '');
        this.el('kick-login-submit').disabled = this.busy;
        this.el('twitch-logout').disabled = this.busy || !normal;
        const renewal = this.el('twitch-renewal-status');
        renewal.textContent = normal ? (t.renewal_ready || '') : '';
        renewal.title = renewal.textContent;
    }

    async submit() {
        const input = this.el('kick-cookie-input');
        if (!input.value.trim() || this.busy) return;
        this.busy = true; this.error = ''; this.render();
        try {
            const response = await fetch('/api/session/login', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cookies: input.value })
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
            input.value = '';
            await this.load();
        } catch (e) {
            const t = this.t();
            const known = { no_session_token: 'session_token ✗', invalid_session: t.error || 'Sign in failed' };
            this.error = known[e.message] || e.message;
        } finally {
            this.busy = false;
            this.render();
        }
    }

    logout() {
        const t = this.t();
        const run = async () => {
            this.busy = true; this.render();
            try {
                await fetch('/api/session/logout', { method: 'POST' });
                await this.load();
            } finally { this.busy = false; this.render(); }
        };
        if (typeof showConfirmModal === 'function') showConfirmModal(t.logout_confirm || 'Log out of Kick?', run);
        else if (confirm(t.logout_confirm || 'Log out of Kick?')) run();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.browserLoginPanel = new KickLoginPanel();
    window.browserLoginPanel.load();
    setInterval(() => window.browserLoginPanel.load(), 15000);
});
