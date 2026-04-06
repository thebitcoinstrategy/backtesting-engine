#!/usr/bin/env python3
"""Web interface for the Backtesting Engine."""

import os
import re
import hmac
import hashlib
import json
import time
import base64
import functools
from io import BytesIO
from datetime import timedelta, datetime
from flask import Flask, render_template_string, request, session, redirect, jsonify, abort
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests as req_lib
import backtest as bt
import threading
import uuid
import database as db
import price_db
from helpers import compute_ratio_prices

app = Flask(__name__)
db.init_db()
# Backfill welcome notifications for all existing users
db.backfill_welcome_notifications()

# --- SMTP config (same as Laravel app) ---
SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 587
SMTP_USER = 'thebitcoinstrategy@gmail.com'
SMTP_PASS = 'gvcnyztughyyrlzp'
SMTP_FROM = 'Bitcoin Strategy <thebitcoinstrategy@gmail.com>'
ADMIN_FEEDBACK_EMAIL = 'kuschnik.gerhard@gmail.com'

# --- Telegram bot config (for moderator notifications) ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
TELEGRAM_SIGNAL_CHAT_ID = os.environ.get('TELEGRAM_SIGNAL_CHAT_ID', '')
SITE_URL = 'https://analytics.the-bitcoin-strategy.com'


def _send_telegram_async(message):
    """Send a Telegram message to the moderator group in a background thread."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    def _send():
        try:
            import urllib.request
            import json as _json
            url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
            payload = _json.dumps({
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }).encode('utf-8')
            req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[TELEGRAM ERROR] {e}")
    threading.Thread(target=_send, daemon=True).start()


# --- Avatar helpers ---
AVATAR_COLORS = [
    '#e74c3c', '#e67e22', '#f1c40f', '#2ecc71', '#1abc9c',
    '#3498db', '#9b59b6', '#e84393', '#00b894', '#6c5ce7',
]


def _avatar_color(user_id):
    """Deterministic color from user_id."""
    return AVATAR_COLORS[hash(str(user_id)) % len(AVATAR_COLORS)]


def _user_initial(display_name, email):
    """First letter of display_name or email."""
    name = display_name or email or '?'
    return name[0].upper()


def _send_email_async(to_email, subject, html_body):
    """Send email in a background thread so it doesn't block the request."""
    from helpers import send_email as _helpers_send_email
    def _send():
        try:
            _helpers_send_email(to_email, subject, html_body)
        except Exception as e:
            print(f"[EMAIL ERROR] Failed to send to {to_email}: {e}")
    threading.Thread(target=_send, daemon=True).start()

@app.template_filter('duration')
def duration_filter(days):
    """Format days as 'Xy Xm Xd', e.g. 1000 → '2y 8m 25d'."""
    days = int(days)
    years, days = divmod(days, 365)
    months, days = divmod(days, 30)
    parts = []
    if years:
        parts.append(f"{years}y")
    if months:
        parts.append(f"{months}m")
    if days or not parts:
        parts.append(f"{days}d")
    return " ".join(parts)

# --- Disk cache for backtest results ---
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_version():
    """Hash of backtest.py + app.py to auto-invalidate cache on code changes."""
    h = hashlib.md5()
    for fname in ("backtest.py", "app.py"):
        fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
        try:
            h.update(open(fpath, "rb").read())
        except FileNotFoundError:
            pass
    return h.hexdigest()[:12]

_CODE_VERSION = _cache_version()

def _normalize_param(value):
    """Normalize a form parameter value for consistent cache keys."""
    v = value.strip()
    if not v:
        return ""
    # Try to normalize numeric values (0.10 -> 0.1, 20.0 -> 20)
    try:
        n = float(v)
        # Use int representation if it's a whole number
        if n == int(n) and "e" not in v.lower():
            return str(int(n))
        return str(n)
    except (ValueError, OverflowError):
        return v

def _cache_key(form_params):
    """Build a deterministic cache key from form parameters.

    Only includes parameters actually used by the given mode to avoid
    cache fragmentation from irrelevant hidden fields.
    """
    # Core params always relevant
    core = {"mode", "asset", "vs_asset", "start_date", "end_date", "initial_cash", "fee", "financing_rate", "sizing", "reverse", "timeframe", "theme"}
    mode = form_params.get("mode", "backtest")
    signal_type = form_params.get("signal_type", "crossover")

    # Build set of relevant keys based on mode and signal type
    relevant = set(core)
    relevant.add("signal_type")

    if signal_type == "oscillator":
        relevant.update(["osc_name", "osc_period", "buy_threshold", "sell_threshold"])
        relevant.update(["ind1_name", "period1"])  # ind1 still used in oscillator mode
    else:
        relevant.update(["ind1_name", "period1", "ind2_name", "period2"])

    if mode == "sweep-lev":
        relevant.update(["lev_min", "lev_max", "lev_step", "lev_mode"])
        # exposure is forced to long-short in code, don't include
    elif mode == "sweep":
        relevant.update(["range_min", "range_max", "step", "exposure", "long_leverage", "short_leverage", "lev_mode"])
    elif mode == "heatmap":
        relevant.update(["range_min", "range_max", "step", "exposure", "long_leverage", "short_leverage", "lev_mode"])
    elif mode == "regression":
        relevant.update(["osc_name", "osc_period", "forward_days", "range_min", "range_max"])
    elif mode == "dca":
        relevant.update(["dca_frequency", "dca_amount", "dca_signal_type", "dca_signal_name",
                         "dca_signal_period", "dca_max_multiplier", "dca_show_lump_sum", "dca_reverse"])
    else:  # backtest
        relevant.update(["exposure", "long_leverage", "short_leverage", "lev_mode"])

    items = sorted(
        (k, _normalize_param(v))
        for k, v in form_params.items()
        if k in relevant and v.strip()
    )
    raw = _CODE_VERSION + "|" + "&".join(f"{k}={v}" for k, v in items)
    return hashlib.sha256(raw.encode()).hexdigest()

def _cache_get(key):
    """Return cached HTML response or None."""
    path = os.path.join(CACHE_DIR, key + ".html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            # Touch file to update access time (for LRU eviction)
            os.utime(path, None)
            return f.read()
    except FileNotFoundError:
        return None

def _cache_put(key, html):
    """Store HTML response to disk and enforce size limit."""
    path = os.path.join(CACHE_DIR, key + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    # Evict oldest files if cache exceeds size limit
    _cache_evict()

def _cache_evict():
    """Delete oldest cache files until total size is under CACHE_MAX_BYTES."""
    try:
        files = []
        total = 0
        for fname in os.listdir(CACHE_DIR):
            if not fname.endswith(".html"):
                continue
            fpath = os.path.join(CACHE_DIR, fname)
            stat = os.stat(fpath)
            files.append((stat.st_mtime, stat.st_size, fpath))
            total += stat.st_size
        if total <= CACHE_MAX_BYTES:
            return
        # Sort oldest first, delete until under limit
        files.sort()
        for mtime, size, fpath in files:
            if total <= CACHE_MAX_BYTES:
                break
            os.remove(fpath)
            total -= size
    except OSError:
        pass

# --- Request cancellation infrastructure ---
_cancel_flags = {}   # request_id -> threading.Event
_cancel_lock = threading.Lock()

class ClientDisconnected(Exception):
    pass

def check_cancelled(event):
    if event.is_set():
        raise ClientDisconnected()
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')
ANALYTICS_SECRET = os.environ.get('ANALYTICS_SHARED_SECRET', '')
LARAVEL_LOGIN_URL = 'https://the-bitcoin-strategy.com/app/analytics-redirect'
SESSION_DURATION = 86400  # 24 hours

app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') != 'development'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB for save/publish payloads

# In-memory nonce tracking for token replay protection
_used_nonces = {}
_NONCE_CLEANUP_INTERVAL = 300  # clean up expired nonces every 5 min
_last_nonce_cleanup = 0


def _cleanup_nonces():
    """Remove expired nonces to prevent memory growth."""
    global _last_nonce_cleanup
    now = time.time()
    if now - _last_nonce_cleanup < _NONCE_CLEANUP_INTERVAL:
        return
    _last_nonce_cleanup = now
    cutoff = now - 120  # nonces older than 2 min can't be valid (60s expiry + buffer)
    expired = [n for n, t in _used_nonces.items() if t < cutoff]
    for n in expired:
        del _used_nonces[n]


def _validate_token(token):
    """Validate an HMAC-signed token. Returns payload dict or None."""
    if not ANALYTICS_SECRET:
        return None
    try:
        # Add padding if stripped (PHP strips trailing '=')
        padded = token + '=' * (4 - len(token) % 4) if len(token) % 4 else token
        raw = base64.urlsafe_b64decode(padded)
        data = json.loads(raw)
    except Exception:
        return None

    signature = data.pop('sig', None)
    if not signature:
        return None

    # Recompute HMAC over the payload (without sig)
    payload_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    expected = hmac.new(ANALYTICS_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return None

    # Check expiry
    if time.time() > data.get('exp', 0):
        return None

    # Check nonce (replay protection)
    _cleanup_nonces()
    nonce = data.get('nonce', '')
    if nonce in _used_nonces:
        return None
    _used_nonces[nonce] = time.time()

    return data


def _validate_email_token(token):
    """Validate an email login token (no nonce/replay check, purpose-scoped).
    Returns payload dict or None."""
    if not ANALYTICS_SECRET:
        return None
    try:
        padded = token + '=' * (4 - len(token) % 4) if len(token) % 4 else token
        raw = base64.urlsafe_b64decode(padded)
        data = json.loads(raw)
    except Exception:
        return None

    signature = data.pop('sig', None)
    if not signature:
        return None

    payload_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    expected = hmac.new(ANALYTICS_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return None

    if time.time() > data.get('exp', 0):
        return None

    if data.get('purpose') != 'email_login':
        return None

    return data


def require_auth(f):
    """Decorator: require valid token or active session, else redirect to Laravel login."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Check for token in query string
        token = request.args.get('token')
        if token:
            payload = _validate_token(token)
            if not payload:
                payload = _validate_email_token(token)
            if payload:
                session.permanent = True
                session['user_id'] = str(payload.get('user_id', ''))
                session['email'] = payload.get('email')
                session['auth_time'] = time.time()
                # Redirect to clean URL (strip token from query string)
                return redirect(request.path, code=302)
            # Invalid token — fall through to session check

        # Check existing session
        auth_time = session.get('auth_time')
        if auth_time and (time.time() - auth_time) < SESSION_DURATION:
            return f(*args, **kwargs)

        # No valid auth — redirect to Laravel
        return redirect(LARAVEL_LOGIN_URL, code=302)

    return decorated


def _try_token_auth():
    """Process token from query string if present (for public pages). Does not redirect."""
    token = request.args.get('token')
    if token:
        payload = _validate_token(token)
        if not payload:
            payload = _validate_email_token(token)
        if payload:
            session.permanent = True
            session['user_id'] = str(payload.get('user_id', ''))
            session['email'] = payload.get('email')
            session['auth_time'] = time.time()


def _is_authenticated():
    """Check if current request has a valid session."""
    auth_time = session.get('auth_time')
    return bool(auth_time and (time.time() - auth_time) < SESSION_DURATION)


def _is_admin():
    """Check if current user is admin."""
    return _is_authenticated() and session.get('email') == db.ADMIN_EMAIL


@app.context_processor
def inject_auth():
    """Make is_authenticated, is_admin, and user avatar data available in all templates."""
    is_auth = _is_authenticated()
    is_adm = _is_admin()
    d = dict(is_authenticated=is_auth, is_admin=is_adm)
    if is_auth:
        uid = session.get('user_id')
        email = session.get('email', '')
        dn = db.get_display_name(uid)
        d['user_avatar'] = db.get_user_avatar(uid)
        d['user_initial'] = _user_initial(dn, email)
        d['user_avatar_color'] = _avatar_color(uid)
        # Ensure welcome notification exists
        db.ensure_welcome_notification(uid)
    return d


def _require_auth_api():
    """Check auth for API routes. Returns (user_id, email) or aborts with 401."""
    if not _is_authenticated():
        abort(401)
    return session.get('user_id'), session.get('email')


def _time_ago(dt_str):
    """Convert ISO datetime string to human-readable time ago."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        now = datetime.utcnow()
        diff = now - dt.replace(tzinfo=None) if dt.tzinfo else now - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 30:
            return f"{days}d ago"
        months = days // 30
        if months < 12:
            return f"{months}mo ago"
        return f"{days // 365}y ago"
    except Exception:
        return ""


NAV_HTML = """\
    <div class="header">
        {% if not is_authenticated %}
        <div class="auth-buttons">
            <a href="https://the-bitcoin-strategy.com/app/analytics-redirect" class="auth-btn auth-btn-login">Log In</a>
            <a href="https://the-bitcoin-strategy.com/subscribe" class="auth-btn auth-btn-signup">Sign Up</a>
        </div>
        {% endif %}
        <h1><a href="/" style="text-decoration:none;color:inherit;display:inline-flex;align-items:center;gap:0"><span class="brand-btc">Bitcoin</span><span class="brand-analytics">Strategy Analytics</span></a></h1>
        <div style="font-size:0.8em;color:var(--text-dim);margin-top:2px;font-family:'DM Sans',sans-serif">Exclusive to <a href="https://the-bitcoin-strategy.com" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:600">Premium Members</a> at the-bitcoin-strategy.com</div>
    </div>
    <nav class="nav-bar">
        <a href="/" class="nav-link {{ 'active' if nav_active|default('')=='featured' }}">Home</a>
        <a href="/community" class="nav-link {{ 'active' if nav_active|default('')=='community' }}">Community</a>
        <a href="/backtester" class="nav-link {{ 'active' if nav_active|default('')=='backtester' }}">Create Backtest</a>
        {% if is_authenticated %}
        <a href="/my-backtests" class="nav-link {{ 'active' if nav_active|default('')=='my-backtests' }}">My Backtests</a>
        {% endif %}
        {% if is_admin %}<a href="/admin/assets" class="nav-link {{ 'active' if nav_active|default('')=='admin-assets' }}">Assets</a>{% endif %}
        <div class="nav-right-group">
            <button class="theme-toggle" onclick="toggleTheme()" aria-label="Toggle theme">
                <svg class="icon-sun" viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 7a5 5 0 100 10 5 5 0 000-10zm0-5a1 1 0 011 1v2a1 1 0 01-2 0V3a1 1 0 011-1zm0 18a1 1 0 011 1v1a1 1 0 01-2 0v-1a1 1 0 011-1zm9-9a1 1 0 010 2h-2a1 1 0 010-2h2zM5 11a1 1 0 010 2H3a1 1 0 010-2h2zm13.36-5.64a1 1 0 010 1.41l-1.42 1.42a1 1 0 01-1.41-1.42l1.42-1.41a1 1 0 011.41 0zM8.46 15.54a1 1 0 010 1.41l-1.42 1.42a1 1 0 01-1.41-1.42l1.42-1.41a1 1 0 011.41 0zm9.18 2.83a1 1 0 01-1.41 0l-1.42-1.42a1 1 0 011.41-1.41l1.42 1.42a1 1 0 010 1.41zM8.46 8.46a1 1 0 01-1.41 0L5.63 7.05a1 1 0 011.41-1.42L8.46 7.05a1 1 0 010 1.41z"/></svg>
                <svg class="icon-moon" viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>
            </button>
            {% if is_authenticated %}
            <div class="notif-bell-wrap">
                <button class="notif-bell" onclick="toggleNotifDropdown(event)" aria-label="Notifications">
                    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
                    <span class="notif-badge hidden" id="notif-badge">0</span>
                </button>
                <div class="notif-dropdown hidden" id="notif-dropdown">
                    <div class="notif-dropdown-header"><span>Notifications</span></div>
                    <div class="notif-list" id="notif-list"><div class="notif-empty">No notifications yet</div></div>
                </div>
            </div>
            <div class="avatar-wrap">
                <button class="avatar-btn" onclick="toggleAvatarDropdown(event)">
                    {% if user_avatar %}
                    <img src="/static/avatars/{{ user_avatar }}" class="avatar-img" alt="Profile">
                    {% else %}
                    <span class="avatar-initials" style="background:{{ user_avatar_color }}">{{ user_initial }}</span>
                    {% endif %}
                </button>
                <div class="avatar-dropdown hidden" id="avatar-dropdown">
                    <a href="/account" class="avatar-dropdown-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg> Account Settings</a>
                    <a href="/feedback" class="avatar-dropdown-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg> Send Feedback</a>
                    <a href="https://the-bitcoin-strategy.com/app" target="_blank" class="avatar-dropdown-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg> Main Website</a>
                    <a href="https://the-bitcoin-strategy.com/subscription-and-invoices" target="_blank" class="avatar-dropdown-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg> Billing</a>
                    <div class="avatar-dropdown-divider"></div>
                    <a href="/logout" class="avatar-dropdown-item avatar-dropdown-logout"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg> Log Out</a>
                </div>
            </div>
        {% endif %}
        </div>
    </nav>"""

HTML = """\
<!DOCTYPE html>
<html>
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <title>Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --bg-deep: #080a10;
            --bg-base: #0f1117;
            --bg-surface: #161922;
            --bg-elevated: #1c2030;
            --border: #252a3a;
            --border-hover: #3a4060;
            --text: #e8eaf0;
            --text-muted: #8890a4;
            --text-dim: #555d74;
            --accent: #f7931a;
            --accent-hover: #ffa940;
            --accent-glow: rgba(247, 147, 26, 0.15);
            --green: #34d399;
            --green-dim: rgba(52, 211, 153, 0.12);
            --blue: #6495ED;
        }
        [data-theme="light"] {
            --bg-deep: #f5f6fa;
            --bg-base: #ebedf5;
            --bg-surface: #ffffff;
            --bg-elevated: #f0f2f5;
            --border: #d0d4e0;
            --border-hover: #a0a8c0;
            --text: #1a1a2e;
            --text-muted: #5a6078;
            --text-dim: #8890a4;
            --accent: #d97706;
            --accent-hover: #b45309;
            --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669;
            --green-dim: rgba(5, 150, 105, 0.12);
            --blue: #3a6fd8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'DM Sans', sans-serif;
            background: var(--bg-deep);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
        }
        body::before {
            content: '';
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background:
                radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247, 147, 26, 0.06), transparent),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none;
            z-index: 0;
        }
        [data-theme="light"] body::before {
            background:
                radial-gradient(ellipse 80% 50% at 50% -20%, rgba(217, 119, 6, 0.04), transparent),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(58, 111, 216, 0.03), transparent);
        }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; }

        /* Header */
        .header {
            text-align: center;
            margin-bottom: 32px;
            animation: fadeDown 0.6s ease-out;
            position: relative;
        }
        .header h1 {
            font-size: 1.6em;
            font-weight: 700;
            letter-spacing: -0.02em;
            display: inline-flex;
            align-items: center;
            gap: 0;
        }
        .header h1 .brand-btc {
            background: linear-gradient(135deg, var(--blue), #4a7dd6);
            color: #fff;
            padding: 6px 14px;
            border-radius: 0;
            font-weight: 700;
        }
        .header h1 .brand-analytics {
            background: var(--bg-elevated);
            color: var(--text);
            padding: 6px 14px;
            border-radius: 0;
            border: 1px solid var(--border);
            border-left: none;
        }

        /* Layout */
        .layout { display: flex; flex-direction: column; gap: 20px; }

        /* Panels */
        .panel {
            background: var(--bg-surface);
            border-radius: 16px;
            padding: 24px;
            border: 1px solid var(--border);
            animation: fadeUp 0.5s ease-out both;
        }
        .panel:nth-child(1) { animation-delay: 0.1s; }
        .panel:nth-child(2) { animation-delay: 0.2s; }

        /* Form sections */
        .form-section {
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px 18px;
            margin-bottom: 14px;
            background: var(--bg-base);
            transition: border-color 0.3s ease;
        }
        .form-section:hover { border-color: var(--border-hover); }
        .section-title {
            font-size: 0.7em;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.1em;
            margin-bottom: 12px;
            font-weight: 600;
        }

        /* Form elements */
        .form-group { margin-bottom: 12px; }
        .form-row { display: flex; gap: 14px; flex-wrap: wrap; align-items: flex-end; }
        .form-row .form-group { flex: 1; min-width: 140px; margin-bottom: 0; }
        label {
            display: block;
            font-size: 0.8em;
            color: var(--text-muted);
            margin-bottom: 6px;
            font-weight: 500;
            letter-spacing: 0.01em;
        }
        input:not([type="checkbox"]), select {
            width: 100%;
            padding: 10px 14px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--bg-deep);
            color: var(--text);
            font-size: 0.9em;
            font-family: 'DM Sans', sans-serif;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }
        input:not([type="checkbox"]):focus, select:focus {
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }
        select { cursor: pointer; }
        .row { display: flex; gap: 12px; }
        .row .form-group { flex: 1; }

        /* Mode selector cards */
        .mode-selector {
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 8px;
            margin-bottom: 14px;
        }
        .mode-card {
            display: flex; flex-direction: column; align-items: center; gap: 6px;
            padding: 12px 6px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--bg-deep);
            cursor: pointer;
            transition: all 0.2s ease;
            text-align: center;
            position: relative;
        }
        .mode-card:hover {
            border-color: var(--border-hover);
            background: var(--bg-surface);
        }
        .mode-card.active {
            border-color: var(--accent);
            background: rgba(247, 147, 26, 0.08);
            box-shadow: 0 0 0 1px var(--accent), 0 2px 12px rgba(247, 147, 26, 0.12);
        }
        .mode-card-icon {
            width: 28px; height: 28px;
            color: var(--text-dim);
            transition: color 0.2s ease;
        }
        .mode-card.active .mode-card-icon { color: var(--accent); }
        .mode-card:hover .mode-card-icon { color: var(--text-muted); }
        .mode-card.active:hover .mode-card-icon { color: var(--accent); }
        .mode-card-label {
            font-size: 0.7em;
            font-weight: 600;
            color: var(--text-dim);
            letter-spacing: 0.02em;
            line-height: 1.2;
            transition: color 0.2s ease;
        }
        .mode-card.active .mode-card-label { color: var(--text); }
        .mode-card:hover .mode-card-label { color: var(--text-muted); }
        .mode-card.active:hover .mode-card-label { color: var(--text); }

        /* Asset selected display */
        .asset-selected {
            display: flex; align-items: center; gap: 10px;
            padding: 10px 14px; border-radius: 10px;
            border: 1px solid var(--border); background: var(--bg-deep);
            cursor: pointer; transition: all 0.2s ease;
        }
        .asset-selected:hover { border-color: var(--border-hover); background: var(--bg-surface); }
        .asset-selected-logo { width: 36px; height: 36px; object-fit: contain; }
        .asset-selected-name {
            flex: 1; font-size: 0.95em; font-weight: 600; color: var(--text);
        }
        .asset-selected-chevron { color: var(--text-dim); transition: transform 0.2s ease; }
        .asset-selected:hover .asset-selected-chevron { color: var(--text-muted); }

        /* Asset modal */
        .asset-modal-overlay {
            display: none; position: fixed; inset: 0; z-index: 100000;
            background: rgba(0,0,0,0.6); backdrop-filter: blur(4px);
            align-items: center; justify-content: center;
        }
        .asset-modal-overlay.open { display: flex; animation: fadeIn 0.15s ease-out; }
        .asset-modal {
            background: var(--bg-base); border: 1px solid var(--border);
            border-radius: 16px; padding: 20px 24px; width: 94%; max-width: 820px;
            max-height: 80vh; overflow-y: auto;
            box-shadow: 0 24px 48px rgba(0,0,0,0.4);
            animation: fadeUp 0.2s ease-out;
        }
        .asset-modal-header {
            display: flex; align-items: center; gap: 12px;
            margin-bottom: 12px;
            font-size: 0.85em; font-weight: 600; color: var(--text);
        }
        .asset-modal-header span { white-space: nowrap; }
        .asset-modal-filter {
            flex: 1; padding: 6px 12px;
            background: var(--bg-deep); border: 1px solid var(--border);
            border-radius: 8px; color: var(--text); font-size: 0.82em;
            font-family: 'DM Sans', sans-serif; outline: none;
            transition: border-color 0.15s ease;
        }
        .asset-modal-filter:focus { border-color: var(--accent); }
        .asset-modal-filter::placeholder { color: var(--text-dim); }
        .asset-modal-close {
            background: none; border: none; color: var(--text-dim);
            font-size: 1.4em; cursor: pointer; padding: 0 4px;
            transition: color 0.15s ease; line-height: 1;
        }
        .asset-modal-close:hover { color: var(--text); }

        /* Asset grid */
        .asset-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
            gap: 6px;
        }
        .asset-card {
            display: flex; flex-direction: column; align-items: center; gap: 4px;
            padding: 8px 4px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--bg-deep);
            cursor: pointer;
            transition: all 0.2s ease;
            text-align: center;
        }
        .asset-card:hover {
            border-color: var(--border-hover);
            background: var(--bg-surface);
        }
        .asset-card.active {
            border-color: var(--accent);
            background: rgba(247, 147, 26, 0.08);
            box-shadow: 0 0 0 1px var(--accent), 0 2px 12px rgba(247, 147, 26, 0.12);
        }
        .asset-card-logo {
            width: 28px; height: 28px; object-fit: contain;
        }
        .asset-card-placeholder {
            width: 28px; height: 28px; border-radius: 50%;
            background: var(--bg-elevated); display: flex; align-items: center;
            justify-content: center; font-size: 0.6em; font-weight: 700;
            color: var(--text-dim);
        }
        .asset-card-label {
            font-size: 0.65em; font-weight: 600; color: var(--text-dim);
            letter-spacing: 0.02em; line-height: 1.2; text-align: center;
            word-break: break-word; max-width: 100%;
            transition: color 0.2s ease;
        }
        .asset-card.active .asset-card-label { color: var(--text); }
        .asset-card:hover .asset-card-label { color: var(--text-muted); }
        .asset-card.active:hover .asset-card-label { color: var(--text); }
        .asset-section-label {
            font-size: 0.6em; color: var(--text-dim); text-transform: uppercase;
            letter-spacing: 0.08em; margin: 8px 0 4px; font-weight: 600;
        }

        /* Separator */
        .sep { width: 1px; background: var(--border); align-self: stretch; margin: 0 2px; flex: 0 0 1px; opacity: 0.6; }

        /* Button */
        button[type="submit"], #btn {
            width: 100%;
            padding: 12px 24px;
            border: none;
            border-radius: 12px;
            font-size: 0.95em;
            font-weight: 600;
            font-family: 'DM Sans', sans-serif;
            cursor: pointer;
            background: linear-gradient(135deg, var(--accent), #e8850f);
            color: var(--bg-deep);
            margin-top: 8px;
            transition: all 0.25s ease;
            box-shadow: 0 4px 16px rgba(247, 147, 26, 0.2);
            letter-spacing: 0.02em;
        }
        button[type="submit"]:hover, #btn:hover {
            background: linear-gradient(135deg, var(--accent-hover), var(--accent));
            box-shadow: 0 6px 24px rgba(247, 147, 26, 0.3);
            transform: translateY(-1px);
        }
        button[type="submit"]:active, #btn:active { transform: translateY(0); }
        button:disabled, #btn:disabled {
            background: var(--bg-elevated) !important;
            color: var(--text-dim) !important;
            cursor: wait;
            box-shadow: none !important;
            transform: none !important;
        }
        #btn.btn-stop {
            background: linear-gradient(135deg, #e74c3c, #c0392b) !important;
            color: #fff !important;
            cursor: pointer !important;
            box-shadow: 0 4px 16px rgba(231, 76, 60, 0.3) !important;
        }
        #btn.btn-stop:hover {
            background: linear-gradient(135deg, #c0392b, #e74c3c) !important;
            box-shadow: 0 6px 24px rgba(231, 76, 60, 0.4) !important;
        }
        #btn.btn-done {
            background: linear-gradient(135deg, #1a6b3c, #22874b) !important;
            color: #fff !important;
            cursor: default !important;
            box-shadow: 0 4px 16px rgba(34, 135, 75, 0.2) !important;
            opacity: 0.85;
        }

        /* Results table */
        .results-table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 16px;
            font-size: 0.85em;
            font-family: 'JetBrains Mono', monospace;
        }
        .results-table th, .results-table td {
            padding: 10px 12px;
            text-align: right;
            border-bottom: 1px solid var(--border);
        }
        .results-table th {
            color: var(--text-muted);
            font-weight: 500;
            font-size: 0.85em;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .results-table tr { transition: background 0.15s ease; }
        .results-table tr:hover { background: var(--bg-elevated); }
        .best { color: var(--green); font-weight: 600; }
        .best td:first-child::before {
            content: '';
            display: inline-block;
            width: 6px; height: 6px;
            background: var(--green);
            border-radius: 50%;
            margin-right: 8px;
            vertical-align: middle;
            box-shadow: 0 0 8px var(--green);
        }

        /* Metrics table */
        .metrics-panel { margin-bottom: 16px; }
        .metrics-table {
            width: 100%; border-collapse: collapse;
            font-size: 0.9em; font-family: 'JetBrains Mono', monospace;
        }
        .metrics-table th {
            padding: 6px 10px; font-size: 0.8em; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.06em;
            border-bottom: 1px solid var(--border);
        }
        .metrics-table th.col-metric { text-align: left; color: var(--text-muted); }
        .metrics-table th.col-strategy { text-align: right; color: var(--green); }
        .metrics-table th.col-buyhold { text-align: right; color: var(--blue); }
        .metrics-table td {
            padding: 5px 10px; border-bottom: 1px solid rgba(37,42,58,0.4);
        }
        .metrics-table td.m-label {
            font-size: 0.95em; color: var(--text-muted); font-family: 'DM Sans', sans-serif;
            font-weight: 500;
        }
        .metrics-table td.m-val { text-align: right; font-weight: 600; color: var(--text); }
        .metrics-table td.m-val.positive { color: var(--green); }
        .metrics-table td.m-val.negative { color: #ef4444; }
        .metrics-table td.m-val.muted { color: var(--text-dim); }
        .metrics-table tr.section-row td {
            padding: 8px 8px 3px; font-size: 0.65em; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.1em;
            color: var(--text-dim); border-bottom: 1px solid var(--border);
        }

        /* Long/short breakdown sub-values */
        .m-ls {
            display: block; font-size: 0.75em; font-weight: 400;
            color: var(--text-dim); margin-top: 1px; line-height: 1.3;
        }
        .m-ls .ls-l { color: var(--green); }
        .m-ls .ls-s { color: #f7931a; }

        /* Metric info tooltips */
        .m-info {
            font-size: 0.7em; color: var(--text-dim); cursor: help;
            margin-left: 4px; opacity: 0.5; position: relative;
            transition: opacity 0.2s;
        }
        .m-info:hover { opacity: 1; }
        .m-info:hover::after {
            content: attr(data-tip);
            position: absolute; bottom: 120%; left: 50%;
            transform: translateX(-50%);
            background: var(--bg-elevated); color: var(--text);
            border: 1px solid var(--border-hover);
            border-radius: 8px; padding: 8px 12px;
            font-size: 11px; font-family: 'DM Sans', sans-serif;
            font-weight: 400; line-height: 1.5;
            white-space: pre-line; width: max-content; max-width: 260px;
            z-index: 100; pointer-events: none;
            box-shadow: 0 4px 16px rgba(0,0,0,0.4);
        }

        /* Chart tabs */
        .chart-tabs {
            display: flex;
            gap: 4px;
            margin-bottom: 12px;
        }
        .chart-tab {
            padding: 6px 16px;
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 8px 8px 0 0;
            color: var(--text-muted);
            cursor: pointer;
            font-family: 'DM Sans', sans-serif;
            font-size: 0.85em;
            font-weight: 500;
            transition: all 0.2s;
        }
        .chart-tab:hover {
            color: var(--text);
            border-color: var(--border-hover);
        }
        .chart-tab.active {
            background: var(--bg-elevated);
            color: var(--text);
            border-color: var(--accent);
            border-bottom-color: var(--bg-elevated);
        }
        /* Chart tools */
        .lw-measure-label {
            position: absolute; z-index: 5; pointer-events: none;
            background: rgba(22,25,34,0.92); border: 1px solid var(--border-hover);
            border-radius: 6px; padding: 6px 10px; font-family: 'JetBrains Mono', monospace;
            font-size: 12px; line-height: 1.5; color: var(--text); white-space: nowrap;
            backdrop-filter: blur(4px); box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        /* Chart */
        .chart-img {
            width: 100%;
            border-radius: 12px;
            border: 1px solid var(--border);
            animation: fadeUp 0.6s ease-out 0.3s both;
        }

        /* Rolling Window Mode */
        .rolling-results { animation: fadeUp 0.5s ease-out both; }
        .rolling-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:8px; }
        .consistency-badge { display:inline-block; padding:8px 16px; border-radius:8px; font-weight:700; font-size:0.95em; font-family:'JetBrains Mono',monospace; }
        .consistency-badge.excellent { background:rgba(52,211,153,0.15); color:#34d399; }
        .consistency-badge.good { background:rgba(100,149,237,0.15); color:#6495ED; }
        .consistency-badge.fair { background:rgba(247,147,26,0.15); color:#f7931a; }
        .consistency-badge.poor { background:rgba(239,68,68,0.15); color:#ef4444; }
        .rolling-tabs { display:flex; gap:4px; margin-bottom:12px; border-bottom:1px solid var(--border); padding-bottom:8px; overflow-x:auto; }
        .rolling-tab-btn { padding:8px 16px; background:transparent; border:1px solid var(--border); border-radius:8px 8px 0 0; color:var(--text-dim); cursor:pointer; font-size:0.85em; font-weight:500; transition:all 0.2s; white-space:nowrap; }
        .rolling-tab-btn:hover { color:var(--text); background:var(--bg-surface); }
        .rolling-tab-btn.active { background:var(--bg-surface); color:var(--accent); border-bottom-color:var(--bg-surface); }
        .rolling-tab-content.hidden { display:none; }

        .chart-download-btn {
            position: absolute; top: 12px; right: 12px;
            background: var(--bg-surface); color: var(--text-secondary);
            border: 1px solid var(--border); border-radius: 8px;
            padding: 6px 8px; cursor: pointer; opacity: 0;
            transition: opacity 0.2s ease, background 0.15s ease, color 0.15s ease;
            display: flex; align-items: center; justify-content: center;
        }
        .chart-download-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
        div:hover > .chart-download-btn { opacity: 1; }

        /* Placeholder */
        .placeholder {
            text-align: center;
            color: var(--text-dim);
            padding: 80px 20px;
            font-size: 1em;
            letter-spacing: 0.01em;
        }

        /* Stats */
        .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
        .stat {
            flex: 1; min-width: 120px;
            background: var(--bg-base);
            border-radius: 12px;
            padding: 14px;
            text-align: center;
            border: 1px solid var(--border);
        }
        .stat-value {
            font-size: 1.3em;
            font-weight: 700;
            color: var(--accent);
            font-family: 'JetBrains Mono', monospace;
        }
        .stat-label { font-size: 0.72em; color: var(--text-muted); margin-top: 4px; }

        /* Signal explainer */
        .signal-explainer {
            margin-top: 10px;
            font-size: 0.8em;
            color: var(--text-dim);
            line-height: 1.5;
            padding: 8px 12px;
            background: var(--bg-deep);
            border-radius: 8px;
            border-left: 2px solid var(--accent);
        }
        .signal-explainer span { color: var(--text); font-weight: 500; }

        /* Details */
        details summary {
            cursor: pointer;
            color: var(--text-muted);
            font-size: 0.88em;
            padding: 8px 0;
            transition: color 0.2s;
        }
        details summary:hover { color: var(--text); }
        details[open] summary { margin-bottom: 8px; }

        /* All data button */
        .btn-all-data {
            background: var(--bg-elevated);
            color: var(--text-muted);
            font-size: 0.65em;
            padding: 2px 8px;
            border: 1px solid var(--border);
            border-radius: 4px;
            cursor: pointer;
            margin-left: 6px;
            vertical-align: middle;
            font-family: 'DM Sans', sans-serif;
            transition: all 0.2s ease;
        }
        .btn-all-data:hover { background: var(--border-hover); color: var(--text); }

        /* Info icon */
        .info-icon {
            cursor: pointer;
            color: var(--text-dim);
            font-size: 1.1em;
            vertical-align: middle;
            margin-left: 4px;
            transition: color 0.2s ease;
        }
        .info-icon:hover { color: var(--accent); }

        /* Scrollbar */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: var(--bg-base); }
        ::-webkit-scrollbar-thumb { background: var(--border-hover); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

        /* Nav bar */
        .nav-bar {
            display: flex; align-items: center; justify-content: center; gap: 4px;
            margin-bottom: 20px;
            animation: fadeDown 0.6s ease-out;
            position: relative;
        }
        .nav-link {
            padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500;
            color: var(--text-muted); text-decoration: none; transition: all 0.2s ease;
            border: 1px solid transparent;
        }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        /* Theme toggle */
        .theme-toggle { background: none; border: 1px solid var(--border); cursor: pointer; color: var(--text-muted); padding: 7px; border-radius: 8px; transition: all 0.2s ease; display: flex; align-items: center; justify-content: center; }
        .theme-toggle:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border-hover); }
        .theme-toggle svg { width: 16px; height: 16px; }
        .theme-toggle .icon-sun { display: none; }
        .theme-toggle .icon-moon { display: block; }
        [data-theme="light"] .theme-toggle .icon-sun { display: block; }
        [data-theme="light"] .theme-toggle .icon-moon { display: none; }
        /* Notification bell */
        .nav-right-group { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; z-index: 9999; }
        .notif-bell-wrap { position: relative; }
        .notif-bell { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 8px; border-radius: 8px; position: relative; transition: all 0.2s ease; }
        .notif-bell:hover { color: var(--text); background: var(--bg-elevated); }
        .notif-badge { position: absolute; top: 2px; right: 2px; background: #e74c3c; color: #fff; font-size: 0.65em; font-weight: 700; min-width: 16px; height: 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; padding: 0 4px; font-family: 'JetBrains Mono', monospace; }
        .notif-badge.hidden { display: none; }
        .notif-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 340px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; }
        .notif-dropdown.hidden { display: none; }
        .notif-dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.85em; color: var(--text); }
        .notif-mark-read { background: none; border: none; color: var(--accent); cursor: pointer; font-size: 0.8em; font-weight: 500; padding: 0; font-family: 'DM Sans', sans-serif; }
        .notif-mark-read:hover { text-decoration: underline; }
        .notif-list { max-height: 320px; overflow-y: auto; }
        .notif-item { display: block; padding: 12px 16px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); font-size: 0.82em; transition: background 0.15s ease; cursor: pointer; }
        .notif-item.notif-unread { background: rgba(100,149,237,0.08); border-left: 3px solid var(--accent); }
        .notif-item.notif-read { opacity: 0.55; }
        .notif-item:hover { background: var(--bg-elevated); opacity: 1; }
        .notif-item:last-child { border-bottom: none; }
        .notif-item-text { line-height: 1.4; }
        .notif-item-text strong { color: var(--accent); font-weight: 600; }
        .notif-item-time { color: var(--text-dim); font-size: 0.78em; margin-top: 4px; }
        .notif-empty { padding: 24px 16px; text-align: center; color: var(--text-dim); font-size: 0.82em; }
        /* Avatar */
        .avatar-wrap { position: relative; }
        .avatar-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 50%; transition: all 0.2s ease; }
        .avatar-btn:hover { background: var(--bg-elevated); }
        .avatar-img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); display: block; }
        .avatar-initials { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 700; color: #fff; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 220px; background: var(--bg-surface, var(--bg-base)); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; padding: 6px 0; }
        .avatar-dropdown.hidden { display: none; }
        .avatar-dropdown-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; color: var(--text-muted); text-decoration: none; font-size: 0.82em; font-weight: 500; transition: all 0.15s ease; font-family: 'DM Sans', sans-serif; }
        .avatar-dropdown-item:hover { background: var(--bg-elevated); color: var(--text); }
        .avatar-dropdown-item svg { width: 16px; height: 16px; flex-shrink: 0; }
        .avatar-dropdown-divider { height: 1px; background: var(--border); margin: 4px 0; }
        .avatar-dropdown-logout { color: #e74c3c; }
        .avatar-dropdown-logout:hover { background: rgba(231,76,60,0.1); color: #e74c3c; }
        /* Small avatars for cards and comments */
        .card-avatar-img { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; margin-right: 4px; }
        .card-avatar-initials { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 0.6em; font-weight: 700; color: #fff; vertical-align: middle; margin-right: 4px; }
        .comment-avatar-img { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; }
        .comment-avatar-initials { width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.65em; font-weight: 700; color: #fff; flex-shrink: 0; }

        /* Save/Publish buttons */
        .action-buttons { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
        .action-btn {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 16px; border-radius: 8px; border: 1px solid var(--border);
            background: var(--bg-elevated); color: var(--text-muted); cursor: pointer;
            font-size: 0.82em; font-weight: 500; font-family: 'DM Sans', sans-serif;
            transition: all 0.2s ease;
        }
        .action-btn:hover { border-color: var(--border-hover); color: var(--text); background: var(--bg-surface); }
        .action-btn.primary { border-color: var(--accent); color: var(--accent); }
        .action-btn.primary:hover { background: rgba(247,147,26,0.1); }
        .action-btn.liked { color: #ef4444; border-color: #ef4444; }
        .action-btn svg { width: 16px; height: 16px; }

        /* Publish modal */
        .publish-modal-overlay {
            display: none; position: fixed; inset: 0; z-index: 1000;
            background: rgba(0,0,0,0.6); backdrop-filter: blur(4px);
            align-items: center; justify-content: center;
        }
        .publish-modal-overlay.open { display: flex; }
        .publish-modal {
            background: var(--bg-surface); border: 1px solid var(--border); border-radius: 16px;
            padding: 28px; width: 90%; max-width: 500px; position: relative;
        }
        .publish-modal h3 { font-size: 1.1em; font-weight: 600; margin-bottom: 16px; }
        .publish-modal label { display: block; font-size: 0.8em; color: var(--text-muted); margin-bottom: 6px; font-weight: 500; }
        .publish-modal input, .publish-modal textarea {
            width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border);
            background: var(--bg-deep); color: var(--text); font-size: 0.9em; font-family: 'DM Sans', sans-serif;
            transition: border-color 0.2s ease; margin-bottom: 14px;
        }
        .publish-modal input:focus, .publish-modal textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .publish-modal textarea { resize: vertical; min-height: 80px; }
        .publish-modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 4px; }
        .publish-modal .close-btn {
            position: absolute; top: 12px; right: 16px; background: none; border: none;
            color: var(--text-dim); cursor: pointer; font-size: 1.2em;
        }

        /* Community page styles */
        .page-title { font-size: 1.4em; font-weight: 700; margin-bottom: 6px; }
        .page-subtitle { font-size: 0.85em; color: var(--text-muted); margin-bottom: 20px; }
        .sort-tabs { display: flex; gap: 4px; margin-bottom: 20px; }
        .sort-tab {
            padding: 6px 16px; border-radius: 8px; font-size: 0.8em; font-weight: 500;
            color: var(--text-muted); text-decoration: none; border: 1px solid transparent;
            transition: all 0.2s ease;
        }
        .sort-tab:hover { color: var(--text); background: var(--bg-elevated); }
        .sort-tab.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }

        .backtest-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
        .backtest-card {
            background: var(--bg-surface); border: 1px solid var(--border); border-radius: 14px;
            padding: 18px; transition: all 0.2s ease; cursor: pointer; text-decoration: none; color: inherit;
        }
        .backtest-card:hover { border-color: var(--border-hover); transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
        .backtest-card-title { font-size: 1em; font-weight: 600; margin-bottom: 6px; color: var(--text); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.3; }
        .backtest-card-desc { font-size: 0.8em; color: var(--text-muted); margin-bottom: 10px; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
        .backtest-card-metrics { display: flex; gap: 14px; margin-bottom: 10px; }
        .backtest-card-metric { font-family: 'JetBrains Mono', monospace; font-size: 0.75em; }
        .backtest-card-metric .label { color: var(--text-dim); font-size: 0.85em; }
        .backtest-card-metric .value { color: var(--text); font-weight: 500; }
        .backtest-card-footer { display: flex; align-items: center; justify-content: space-between; font-size: 0.75em; color: var(--text-dim); }
        .backtest-card-footer .engagement { display: flex; gap: 12px; }
        .backtest-card-footer .engagement span { display: flex; align-items: center; gap: 3px; }
        .card-author { display: flex; align-items: center; gap: 6px; }
        .card-author-avatar { width: 22px; height: 22px; border-radius: 50%; object-fit: cover; }
        .card-author-initials { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 0.7em; font-weight: 700; flex-shrink: 0; }
        .card-author-name { font-weight: 600; color: var(--text-muted); }
        .card-author-sep { color: var(--text-dim); }
        .card-author-time { color: var(--text-dim); }
        .backtest-card-badge {
            display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.05em;
        }
        .badge-featured { background: rgba(247,147,26,0.15); color: var(--accent); }
        .badge-community { background: rgba(100,149,237,0.15); color: var(--blue); }
        .badge-private { background: rgba(136,144,164,0.15); color: var(--text-muted); }

        /* Detail page */
        .detail-header { margin-bottom: 20px; }
        .detail-title { font-size: 1.3em; font-weight: 700; margin-bottom: 4px; }
        .detail-meta { font-size: 0.8em; color: var(--text-muted); display: flex; gap: 16px; align-items: center; }
        .detail-description { font-size: 0.9em; color: var(--text-muted); line-height: 1.6; margin-bottom: 20px; white-space: pre-wrap; }

        /* Comments */
        .comments-section { margin-top: 24px; }
        .comments-title { font-size: 1em; font-weight: 600; margin-bottom: 14px; }
        .comment-form { margin-bottom: 20px; }
        .comment-form textarea {
            width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border);
            background: var(--bg-deep); color: var(--text); font-size: 0.85em; font-family: 'DM Sans', sans-serif;
            resize: vertical; min-height: 60px; margin-bottom: 8px;
        }
        .comment-form textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .comment { padding: 12px 0; border-bottom: 1px solid var(--border); }
        .comment:last-child { border-bottom: none; }
        .comment-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 0.8em; }
        .comment-author { font-weight: 600; color: var(--text); }
        .comment-time { color: var(--text-dim); }
        .comment-body { font-size: 0.85em; color: var(--text-muted); line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
        .comment-body a { color: #fff; text-decoration: underline; }
        .comment-body a:hover { opacity: 0.8; }
        .comment-actions { margin-top: 6px; display: flex; gap: 12px; }
        .comment-action-btn { background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 0.75em; font-family: 'DM Sans', sans-serif; }
        .comment-action-btn:hover { color: var(--text); }
        .comment-replies { margin-left: 24px; border-left: 2px solid var(--border); padding-left: 14px; margin-top: 4px; }
        .reply-to-tag { font-size: 0.75em; color: var(--accent); margin-bottom: 4px; }
        .reply-to-tag .reply-arrow { opacity: 0.7; margin-right: 3px; }
        .reply-form { margin-top: 8px; }
        .reply-form textarea { min-height: 40px; font-size: 0.8em; }

        /* Pagination */
        .pagination { display: flex; justify-content: center; gap: 8px; margin-top: 24px; }
        .pagination a {
            padding: 8px 14px; border-radius: 8px; border: 1px solid var(--border);
            color: var(--text-muted); text-decoration: none; font-size: 0.82em; transition: all 0.2s ease;
        }
        .pagination a:hover { border-color: var(--border-hover); color: var(--text); }
        .pagination a.active { border-color: var(--accent); color: var(--accent); }

        /* CTA banner for non-auth */
        .cta-banner {
            background: linear-gradient(135deg, rgba(247,147,26,0.1), rgba(100,149,237,0.1));
            border: 1px solid var(--accent); border-radius: 12px; padding: 20px; text-align: center; margin-top: 20px;
        }
        .cta-banner h3 { font-size: 1em; margin-bottom: 6px; }
        .cta-banner p { font-size: 0.85em; color: var(--text-muted); margin-bottom: 12px; }
        .cta-banner a {
            display: inline-block; padding: 10px 24px; background: var(--accent); color: #fff;
            border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 0.85em;
        }

        /* Auth buttons (top-right when logged out) */
        .auth-buttons {
            position: absolute; top: 0; right: 0;
            display: flex; gap: 10px; align-items: center;
        }
        .auth-btn {
            display: inline-block; padding: 10px 24px; border-radius: 8px;
            font-weight: 700; font-size: 0.9em; text-decoration: none;
            font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; cursor: pointer;
        }
        .auth-btn-login {
            background: var(--accent); color: #fff; border: 2px solid var(--accent);
        }
        .auth-btn-login:hover { background: #e08a1a; border-color: #e08a1a; }
        .auth-btn-signup {
            background: var(--accent); color: #fff; border: 2px solid var(--accent);
        }
        .auth-btn-signup:hover { background: #e08a1a; border-color: #e08a1a; }

        /* Animations */
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(16px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes fadeDown {
            from { opacity: 0; transform: translateY(-12px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .hidden { display: none !important; }

        /* ── Mobile responsive ── */
        @media (max-width: 600px) {
            .mode-selector { grid-template-columns: repeat(3, 1fr); }
            .auth-buttons { position: static; display: flex; justify-content: center; margin-bottom: 12px; }
            .auth-btn { padding: 8px 16px; font-size: 0.8em; }
        }
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
            .nav-bar { flex-wrap: wrap; }
            .nav-link { padding: 6px 10px; font-size: 0.75em; }
            .form-row { flex-direction: column; }
            .form-row .form-group { min-width: unset; }
            .asset-grid { grid-template-columns: repeat(auto-fill, minmax(60px, 1fr)); }
        }
        @media (max-width: 400px) {
            .mode-selector { grid-template-columns: repeat(2, 1fr); }
            .backtest-grid { grid-template-columns: 1fr; }
            .notif-dropdown { width: calc(100vw - 32px); right: -8px; }
        }
    </style>
</head>
<body>
<div class="container">
""" + NAV_HTML + """
    <div class="layout">
        <div class="panel">
            <form method="POST" id="form">
                <div class="form-section">
                    <div class="section-title">Mode</div>
                    <input type="hidden" name="mode" id="mode" value="{{ p.mode }}">
                    <input type="hidden" name="theme" id="theme-input" value="dark">
                    <div class="mode-selector">
                        <div class="mode-card {{ 'active' if p.mode=='backtest' }}" data-mode="backtest" onclick="selectMode('backtest', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="4 20 10 12 16 16 24 6"/>
                                <line x1="4" y1="24" x2="24" y2="24" opacity="0.4"/>
                                <circle cx="24" cy="6" r="2" fill="currentColor" stroke="none"/>
                            </svg>
                            <span class="mode-card-label">Backtest</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='sweep' }}" data-mode="sweep" onclick="selectMode('sweep', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="12" cy="14" r="8" opacity="0.4"/>
                                <line x1="18" y1="20" x2="24" y2="26"/>
                                <path d="M9 14h6M12 11v6"/>
                            </svg>
                            <span class="mode-card-label">Single Indicator Optimization</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='heatmap' }}" data-mode="heatmap" onclick="selectMode('heatmap', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <rect x="3" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.6"/>
                                <rect x="11" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.3"/>
                                <rect x="19" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.15"/>
                                <rect x="3" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.3"/>
                                <rect x="11" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.8"/>
                                <rect x="19" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.4"/>
                                <rect x="3" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.15"/>
                                <rect x="11" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.4"/>
                                <rect x="19" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.6"/>
                            </svg>
                            <span class="mode-card-label">Indicator Combination</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='sweep-lev' }}" data-mode="sweep-lev" onclick="selectMode('sweep-lev', this)">
                            <svg class="mode-card-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
                            </svg>
                            <span class="mode-card-label">Leverage Optimization</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='regression' }}" data-mode="regression" onclick="selectMode('regression', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="7" cy="20" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/>
                                <circle cx="10" cy="16" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/>
                                <circle cx="13" cy="18" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/>
                                <circle cx="16" cy="12" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/>
                                <circle cx="19" cy="10" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/>
                                <circle cx="22" cy="7" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/>
                                <line x1="5" y1="22" x2="24" y2="6" opacity="0.8"/>
                            </svg>
                            <span class="mode-card-label">Regression Analysis</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='dca' }}" data-mode="dca" onclick="selectMode('dca', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="6" y1="22" x2="6" y2="16" stroke-width="2.5" opacity="0.5"/>
                                <line x1="10" y1="22" x2="10" y2="12" stroke-width="2.5" opacity="0.6"/>
                                <line x1="14" y1="22" x2="14" y2="14" stroke-width="2.5" opacity="0.7"/>
                                <line x1="18" y1="22" x2="18" y2="9" stroke-width="2.5" opacity="0.8"/>
                                <line x1="22" y1="22" x2="22" y2="5" stroke-width="2.5" opacity="0.9"/>
                                <path d="M4 8 Q13 3 24 6" stroke-width="1.5" opacity="0.6"/>
                            </svg>
                            <span class="mode-card-label">DCA Optimization</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='rolling' }}" data-mode="rolling" onclick="selectMode('rolling', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <rect x="3" y="8" width="10" height="6" rx="1" opacity="0.4"/>
                                <rect x="7" y="11" width="10" height="6" rx="1" opacity="0.6"/>
                                <rect x="11" y="14" width="10" height="6" rx="1" opacity="0.8"/>
                                <rect x="15" y="17" width="10" height="6" rx="1" opacity="1"/>
                                <path d="M5 7 L23 7" stroke-width="1" opacity="0.3" stroke-dasharray="2 2"/>
                            </svg>
                            <span class="mode-card-label">Rolling Window</span>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group" id="range-min-group">
                            <label>Range Min</label>
                            <input type="number" name="range_min" value="{{ p.range_min }}" min="2">
                        </div>
                        <div class="form-group" id="range-max-group">
                            <label>Range Max</label>
                            <input type="number" name="range_max" value="{{ p.range_max }}" min="2">
                        </div>
                        <div class="form-group" id="step-group">
                            <label>Step</label>
                            <input type="number" name="step" value="{{ p.step }}" min="1">
                        </div>
                    </div>
                    <div class="form-row hidden" id="rolling-params-row">
                        <div class="form-group">
                            <label>Window Size (years)</label>
                            <select name="window_size">
                                {% for y in [1,2,3,4,5] %}
                                <option value="{{ y }}" {{ 'selected' if p.window_size == y }}>{{ y }} year{{ 's' if y > 1 }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <input type="hidden" name="step_size" value="0.5">
                        <div class="form-group">
                            <label>Metric</label>
                            <select name="rolling_metric">
                                <option value="total_return" {{ 'selected' if p.rolling_metric == 'total_return' }}>Total Return</option>
                                <option value="alpha" {{ 'selected' if p.rolling_metric == 'alpha' }}>Alpha vs B&H</option>
                                <option value="sharpe" {{ 'selected' if p.rolling_metric == 'sharpe' }}>Sharpe Ratio</option>
                            </select>
                        </div>
                    </div>
                    <input type="hidden" name="rolling_metric" id="rolling-metric-hidden" value="{{ p.rolling_metric }}">
                </div>
                <div class="form-section">
                    <div class="section-title">Asset</div>
                    <input type="hidden" name="asset" id="asset" value="{{ p.asset }}">
                    <input type="hidden" name="vs_asset" id="vs_asset" value="{{ p.vs_asset or '' }}">
                    <div class="asset-selected" id="asset-selected" onclick="openAssetModal()">
                        {% if asset_logos.get(p.asset) %}
                        <img class="asset-selected-logo" src="/static/logos/{{ asset_logos[p.asset] }}" alt="{{ p.asset }}">
                        {% else %}
                        <div class="asset-card-placeholder" style="width:36px;height:36px;font-size:0.75em">{{ p.asset[:3]|upper }}</div>
                        {% endif %}
                        <span class="asset-selected-name">{{ p.asset|capitalize if p.asset == p.asset|lower else p.asset }}</span>
                        <svg class="asset-selected-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
                        <label style="font-size:0.8em;color:var(--text-dim);display:flex;align-items:center;gap:6px;cursor:pointer">
                            <input type="checkbox" id="vs-toggle" onchange="toggleVsAsset()" {{ 'checked' if p.vs_asset }} style="accent-color:var(--accent)">
                            Relative (ratio)
                        </label>
                    </div>
                    <div id="vs-asset-row" class="{{ '' if p.vs_asset else 'hidden' }}" style="margin-top:8px">
                        <div style="font-size:0.75em;color:var(--text-dim);margin-bottom:4px">÷ Denominator Asset</div>
                        <div class="asset-selected" id="vs-asset-selected" onclick="openVsAssetModal()">
                            {% if p.vs_asset and asset_logos.get(p.vs_asset) %}
                            <img class="asset-selected-logo" src="/static/logos/{{ asset_logos[p.vs_asset] }}" alt="{{ p.vs_asset }}">
                            {% elif p.vs_asset %}
                            <div class="asset-card-placeholder" style="width:36px;height:36px;font-size:0.75em">{{ p.vs_asset[:3]|upper }}</div>
                            {% else %}
                            <div class="asset-card-placeholder" style="width:36px;height:36px;font-size:0.75em">---</div>
                            {% endif %}
                            <span class="asset-selected-name">{{ (p.vs_asset|capitalize if p.vs_asset == p.vs_asset|lower else p.vs_asset) if p.vs_asset else 'Select asset' }}</span>
                            <svg class="asset-selected-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                    </div>
                </div>
                <div class="form-section" id="indicators-section">
                    <div class="section-title">Indicators</div>
                    <input type="hidden" name="signal_type" id="signal_type" value="{{ p.signal_type }}">
                    <input type="hidden" name="osc_name" id="osc_name" value="{{ p.osc_name }}">
                    <div class="form-row">
                        <div class="form-group" id="ind1-group">
                            <label>Indicator 1</label>
                            <select name="ind1_name" id="ind1_name" onchange="toggleFields()">
                                <option value="price" {{ 'selected' if p.ind1_name=='price' }}>Price</option>
                                <option value="sma" {{ 'selected' if p.ind1_name=='sma' }}>SMA (Simple Moving Average)</option>
                                <option value="ema" {{ 'selected' if p.ind1_name=='ema' }}>EMA (Exponential Moving Average)</option>
                                <option value="wma" {{ 'selected' if p.ind1_name=='wma' }}>WMA (Weighted Moving Average)</option>
                                <option value="hma" {{ 'selected' if p.ind1_name=='hma' }}>HMA (Hull Moving Average)</option>
                                <option value="dema" {{ 'selected' if p.ind1_name=='dema' }}>DEMA (Double Exponential MA)</option>
                                <option value="tema" {{ 'selected' if p.ind1_name=='tema' }}>TEMA (Triple Exponential MA)</option>
                                <option value="kama" {{ 'selected' if p.ind1_name=='kama' }}>KAMA (Kaufman Adaptive MA)</option>
                                <option value="zlema" {{ 'selected' if p.ind1_name=='zlema' }}>ZLEMA (Zero-Lag EMA)</option>
                                <option value="smma" {{ 'selected' if p.ind1_name=='smma' }}>SMMA (Smoothed Moving Average)</option>
                                <option value="lsma" {{ 'selected' if p.ind1_name=='lsma' }}>LSMA (Least Squares MA)</option>
                                <option value="alma" {{ 'selected' if p.ind1_name=='alma' }}>ALMA (Arnaud Legoux MA)</option>
                                <option value="frama" {{ 'selected' if p.ind1_name=='frama' }}>FRAMA (Fractal Adaptive MA)</option>
                                <option value="t3" {{ 'selected' if p.ind1_name=='t3' }}>T3 (Tillson T3)</option>
                                <option value="mcginley" {{ 'selected' if p.ind1_name=='mcginley' }}>McGinley Dynamic</option>
                            </select>
                        </div>
                        <div class="form-group" id="period1-group">
                            <label>Period 1</label>
                            <input type="number" name="period1" value="{{ p.ind1_period or '' }}" placeholder="e.g. 20" min="2">
                        </div>
                        <div class="sep" id="ind-sep"></div>
                        <div class="form-group">
                            <label>Indicator 2</label>
                            <select name="ind2_name" id="ind2_name" onchange="toggleFields()">
                                <optgroup label="Moving Averages">
                                <option value="sma" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='sma' }}>SMA (Simple Moving Average)</option>
                                <option value="ema" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='ema' }}>EMA (Exponential Moving Average)</option>
                                <option value="wma" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='wma' }}>WMA (Weighted Moving Average)</option>
                                <option value="hma" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='hma' }}>HMA (Hull Moving Average)</option>
                                <option value="dema" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='dema' }}>DEMA (Double Exponential MA)</option>
                                <option value="tema" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='tema' }}>TEMA (Triple Exponential MA)</option>
                                <option value="kama" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='kama' }}>KAMA (Kaufman Adaptive MA)</option>
                                <option value="zlema" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='zlema' }}>ZLEMA (Zero-Lag EMA)</option>
                                <option value="smma" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='smma' }}>SMMA (Smoothed Moving Average)</option>
                                <option value="lsma" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='lsma' }}>LSMA (Least Squares MA)</option>
                                <option value="alma" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='alma' }}>ALMA (Arnaud Legoux MA)</option>
                                <option value="frama" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='frama' }}>FRAMA (Fractal Adaptive MA)</option>
                                <option value="t3" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='t3' }}>T3 (Tillson T3)</option>
                                <option value="mcginley" {{ 'selected' if p.signal_type=='crossover' and p.ind2_name=='mcginley' }}>McGinley Dynamic</option>
                                </optgroup>
                                <optgroup label="Oscillators">
                                <option value="osc_rsi" {{ 'selected' if p.signal_type=='oscillator' and p.osc_name=='rsi' }}>RSI (Relative Strength Index)</option>
                                <option value="osc_macd" {{ 'selected' if p.signal_type=='oscillator' and p.osc_name=='macd' }}>MACD (Moving Avg Convergence Divergence)</option>
                                <option value="osc_stochastic" {{ 'selected' if p.signal_type=='oscillator' and p.osc_name=='stochastic' }}>Stochastic Oscillator (%K/%D)</option>
                                <option value="osc_cci" {{ 'selected' if p.signal_type=='oscillator' and p.osc_name=='cci' }}>CCI (Commodity Channel Index)</option>
                                <option value="osc_roc" {{ 'selected' if p.signal_type=='oscillator' and p.osc_name=='roc' }}>ROC (Rate of Change)</option>
                                <option value="osc_momentum" {{ 'selected' if p.signal_type=='oscillator' and p.osc_name=='momentum' }}>Momentum</option>
                                <option value="osc_williams_r" {{ 'selected' if p.signal_type=='oscillator' and p.osc_name=='williams_r' }}>Williams %R</option>
                                </optgroup>
                            </select>
                        </div>
                        <div class="form-group" id="period2-group">
                            <label>Period 2</label>
                            <input type="number" name="period2" value="{{ p.ind2_period or '' }}" placeholder="e.g. 40" min="2">
                        </div>
                    </div>
                    <div class="form-row" id="osc-params-row" class="hidden">
                        <div class="form-group" id="osc-period-group">
                            <label>Period</label>
                            <input type="number" name="osc_period" id="osc_period" value="{{ p.osc_period or '' }}" placeholder="14" min="2">
                        </div>
                        <div class="form-group" id="buy-threshold-group">
                            <label>Buy Threshold</label>
                            <input type="number" name="buy_threshold" id="buy_threshold" value="{{ p.buy_threshold }}" step="any">
                        </div>
                        <div class="form-group" id="sell-threshold-group">
                            <label>Sell Threshold</label>
                            <input type="number" name="sell_threshold" id="sell_threshold" value="{{ p.sell_threshold }}" step="any">
                        </div>
                    </div>
                    <div class="form-row hidden" id="forward-days-row">
                        <div class="form-group" id="forward-days-group">
                            <label>Forward Days</label>
                            <input type="number" name="forward_days" id="forward_days" value="{{ p.forward_days }}" placeholder="365" min="1">
                        </div>
                    </div>
                    <div id="osc-description" class="hidden" style="margin-top:6px;font-size:0.78em;color:var(--text-muted);line-height:1.5;padding:8px 12px;background:var(--bg-deep);border-radius:8px;border-left:2px solid var(--accent)"></div>
                    <div class="signal-explainer" id="signal-explainer">
                        <span id="explainer-text">Buy when <span id="explainer-ind1">Price</span> crosses above <span id="explainer-ind2">SMA</span>. Sell when it crosses below.</span>
                    </div>
                    <div style="display:flex;gap:18px;margin-top:6px;flex-wrap:wrap">
                    <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:0.82em;color:var(--text-muted)">
                        <input type="checkbox" name="reverse" id="reverse" value="1" {{ 'checked' if p.reverse }} onchange="updateExplainer(); enableBtn();" style="accent-color:var(--accent)"> Reverse signal logic
                    </label>
                    <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:0.82em;color:var(--text-muted)">
                        <input type="checkbox" id="weekly-toggle" onchange="toggleTimeframe(); enableBtn();" {{ 'checked' if p.timeframe=='weekly' }} style="accent-color:var(--accent)"> Weekly data
                    </label>
                    <input type="hidden" name="timeframe" id="timeframe" value="{{ p.timeframe }}">
                    </div>
                </div>
                <div class="form-section hidden" id="dca-section">
                    <div class="section-title">DCA Settings</div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Frequency</label>
                            <select name="dca_frequency" id="dca_frequency">
                                <option value="daily" {{ 'selected' if p.dca_frequency=='daily' }}>Daily</option>
                                <option value="weekly" {{ 'selected' if p.dca_frequency=='weekly' }}>Weekly</option>
                                <option value="monthly" {{ 'selected' if p.dca_frequency=='monthly' }}>Monthly</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label>Amount per interval</label>
                            <div style="position:relative">
                                <span style="position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:0.9em">$</span>
                                <input type="number" name="dca_amount" id="dca_amount" value="{{ p.dca_amount }}" min="1" step="any" style="padding-left:22px">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>Signal Type <span class="m-info" id="ath-info" style="display:none" data-tip="Buys more when price is far below ATH, less when near ATH.&#10;Uses a rolling window (default 5 years) to determine the worst drawdown as reference.&#10;At ATH → minimum buy. At worst drawdown in the lookback window → maximum buy.">ⓘ</span></label>
                            <select name="dca_signal_type" id="dca_signal_type" onchange="toggleFields()">
                                <option value="oscillator" {{ 'selected' if p.dca_signal_type=='oscillator' }}>Oscillator (RSI, etc.)</option>
                                <option value="ma_distance" {{ 'selected' if p.dca_signal_type=='ma_distance' }}>Distance from MA</option>
                                <option value="ath_drawdown" {{ 'selected' if p.dca_signal_type=='ath_drawdown' }}>ATH Drawdown</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-row" id="dca-signal-row">
                        <div class="form-group" id="dca-signal-name-group">
                            <label id="dca-signal-name-label">Signal Indicator</label>
                            <select name="dca_signal_name" id="dca_signal_name">
                                <optgroup label="Oscillators" id="dca-osc-optgroup">
                                <option value="rsi" {{ 'selected' if p.dca_signal_name=='rsi' }}>RSI</option>
                                <option value="stochastic" {{ 'selected' if p.dca_signal_name=='stochastic' }}>Stochastic</option>
                                <option value="cci" {{ 'selected' if p.dca_signal_name=='cci' }}>CCI</option>
                                <option value="roc" {{ 'selected' if p.dca_signal_name=='roc' }}>ROC</option>
                                <option value="williams_r" {{ 'selected' if p.dca_signal_name=='williams_r' }}>Williams %R</option>
                                </optgroup>
                                <optgroup label="Moving Averages" id="dca-ma-optgroup">
                                <option value="sma" {{ 'selected' if p.dca_signal_name=='sma' }}>SMA</option>
                                <option value="ema" {{ 'selected' if p.dca_signal_name=='ema' }}>EMA</option>
                                <option value="hma" {{ 'selected' if p.dca_signal_name=='hma' }}>HMA</option>
                                </optgroup>
                            </select>
                        </div>
                        <div class="form-group" id="dca-signal-period-group">
                            <label id="dca-signal-period-label">Signal Period</label>
                            <input type="number" name="dca_signal_period" id="dca_signal_period" value="{{ p.dca_signal_period if p.dca_signal_period is not none else '' }}" placeholder="14" min="1" step="any">
                        </div>
                        <div class="form-group">
                            <label>Max Multiplier</label>
                            <input type="number" name="dca_max_multiplier" value="{{ p.dca_max_multiplier }}" step="0.5" min="1">
                        </div>
                    </div>
                    <div class="form-row hidden" id="dca-sweep-row">
                        <div class="form-group">
                            <label>Sweep Parameter</label>
                            <select name="dca_sweep_param" id="dca_sweep_param">
                                <option value="multiplier" {{ 'selected' if p.dca_sweep_param=='multiplier' }}>Max Multiplier</option>
                                <option value="period" {{ 'selected' if p.dca_sweep_param=='period' }}>Signal Period</option>
                            </select>
                        </div>
                    </div>
                    <div style="margin-top:6px;display:flex;gap:18px;flex-wrap:wrap">
                        <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:0.82em;color:var(--text-muted)">
                            <input type="checkbox" name="dca_reverse" id="dca_reverse" value="1" {{ 'checked' if p.dca_reverse|default(false) }} style="accent-color:var(--accent)"> Reverse signal (buy more when high)
                        </label>
                    </div>
                    <div id="dca-explainer" style="margin-top:6px;font-size:0.78em;color:var(--text-muted);line-height:1.5;padding:8px 12px;background:var(--bg-deep);border-radius:8px;border-left:2px solid var(--accent)">
                        Compares constant DCA (fixed $ per interval) vs dynamic DCA (signal-adjusted amounts). Budget is always equal &mdash; dynamic DCA just redistributes spend to buy more when the signal says "cheap" and less when "expensive".
                    </div>
                </div>
                <div class="form-section" id="exposure-section">
                    <div class="section-title">Exposure & Leverage</div>
                    <div class="form-row">
                        <div class="form-group" id="exposure-group">
                            <label>Exposure</label>
                            <select name="exposure" id="exposure">
                                <option value="long-cash" {{ 'selected' if p.exposure=='long-cash' }}>Long + Cash</option>
                                <option value="short-cash" {{ 'selected' if p.exposure=='short-cash' }}>Short + Cash</option>
                                <option value="long-short" {{ 'selected' if p.exposure=='long-short' }}>Long + Short</option>
                            </select>
                        </div>
                        <div class="sep"></div>
                        <div class="form-group" id="long-lev-group">
                            <label>Long Leverage</label>
                            <input type="number" name="long_leverage" value="{{ p.long_leverage }}" step="any" min="0.1">
                        </div>
                        <div class="form-group" id="short-lev-group">
                            <label>Short Leverage</label>
                            <input type="number" name="short_leverage" value="{{ p.short_leverage }}" step="any" min="0.1">
                        </div>
                        <div class="form-group" id="lev-min-group">
                            <label>Lev Min</label>
                            <input type="number" name="lev_min" value="{{ p.lev_min }}" step="any" min="0.1">
                        </div>
                        <div class="form-group" id="lev-max-group">
                            <label>Lev Max</label>
                            <input type="number" name="lev_max" value="{{ p.lev_max }}" step="any" min="0.1">
                        </div>
                        <div class="form-group" id="lev-mode-group">
                            <label>Leverage Mode <span class="info-icon" onclick="document.getElementById('lev-mode-info').classList.toggle('hidden')" title="Click for details">&#9432;</span></label>
                            <select name="lev_mode">
                                <option value="optimal" {{ 'selected' if p.lev_mode=='optimal' }}>Optimal</option>
                                <option value="rebalance" {{ 'selected' if p.lev_mode=='rebalance' }}>Daily Rebalance</option>
                                <option value="set-forget" {{ 'selected' if p.lev_mode=='set-forget' }}>Set & Forget</option>
                            </select>
                        </div>
                        <div class="form-group" id="sizing-group">
                            <label>Position Sizing <span class="info-icon" onclick="document.getElementById('sizing-info').classList.toggle('hidden')" title="Click for details">&#9432;</span></label>
                            <select name="sizing">
                                <option value="compound" {{ 'selected' if p.sizing=='compound' }}>Compounding</option>
                                <option value="fixed" {{ 'selected' if p.sizing=='fixed' }}>Fixed</option>
                            </select>
                        </div>
                        <input type="hidden" name="lev_step" value="0.25">
                    </div>
                    <div id="lev-mode-info" class="hidden" style="margin-top:10px;font-size:0.78em;color:var(--text-muted);line-height:1.6;padding:10px 14px;background:var(--bg-deep);border-radius:8px;border-left:2px solid var(--accent)">
                        <strong style="color:var(--text)">Optimal</strong> — Daily rebalance for long positions, set & forget for short positions. Best of both worlds.<br>
                        <strong style="color:var(--text)">Daily Rebalance</strong> — Leverage is reset to target every day. Consistent exposure but higher fees in volatile markets.<br>
                        <strong style="color:var(--text)">Set & Forget</strong> — Leverage is applied at entry and drifts naturally. Lower fees but exposure changes over time.
                    </div>
                    <div id="sizing-info" class="hidden" style="margin-top:10px;font-size:0.78em;color:var(--text-muted);line-height:1.6;padding:10px 14px;background:var(--bg-deep);border-radius:8px;border-left:2px solid var(--accent)">
                        <strong style="color:var(--text)">Compounding</strong> — Position size scales with equity. Gains compound but so do losses (volatility drag).<br>
                        <strong style="color:var(--text)">Fixed</strong> — Always trade the initial capital amount. No volatility drag — reversing the signal gives exactly the opposite P&L. Useful for measuring pure signal quality.
                    </div>
                </div>
                <div class="form-section">
                    <div class="section-title">Date Range & Capital</div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Start Date <button type="button" onclick="setAllData()" class="btn-all-data">All data</button></label>
                            <input type="date" name="start_date" id="start_date" value="{{ p.start_date }}">
                        </div>
                        <div class="form-group">
                            <label>End Date</label>
                            <input type="date" name="end_date" value="{{ p.end_date }}">
                        </div>
                        <div class="form-group">
                            <label>Initial Cash</label>
                            <div style="position:relative">
                                <span style="position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:0.9em">$</span>
                                <input type="number" name="initial_cash" value="{{ p.initial_cash }}" min="1" style="padding-left:22px">
                            </div>
                        </div>
                        <div class="form-group">
                            <label>Fee per Trade (%)</label>
                            <input type="number" name="fee" value="{{ p.fee }}" step="0.01" min="0">
                        </div>
                        <div class="form-group" id="financing-group">
                            <label>Financing (% p.a.) <span class="m-info" data-tip="Annual financing rate for leveraged/margin positions.&#10;Long pays, short earns. Applied daily.&#10;Crypto: full notional. Stocks: borrowed portion.">ⓘ</span></label>
                            <input type="number" name="financing_rate" value="{{ p.financing_rate }}" step="0.1" min="0">
                        </div>
                        <div class="form-group" style="min-width:auto">
                            <label>&nbsp;</label>
                            <button type="submit" id="btn">Run Backtest</button>
                        </div>
                    </div>
                </div>
            </form>
        </div>
        <div class="panel" id="results-panel">
            {% if error|default(none) %}
                <div class="placeholder" style="color:var(--accent)">{{ error }}</div>
            {% elif rolling_charts|default(none) %}
                <div class="rolling-results">
                    <div class="rolling-header">
                        <div class="consistency-badge {{ rolling_score_label|lower }}">
                            Consistency: {{ "%.0f"|format(rolling_score) }}/100 ({{ rolling_score_label }})
                        </div>
                        <div style="font-size:0.85em;color:var(--text-dim)">
                            {{ rolling_windows }} windows  &middot;  {{ rolling_strategy }}
                        </div>
                    </div>
                    {% if rolling_is_dual|default(false) %}
                    <div class="rolling-tabs">
                        <button class="rolling-tab-btn active" onclick="switchRollingTab('animated', this)">Heatmap Over Time</button>
                        <button class="rolling-tab-btn" onclick="switchRollingTab('timeline', this)">Timeline</button>
                        <button class="rolling-tab-btn" onclick="switchRollingTab('equity', this)">Equity Overlay</button>
                    </div>
                    <div class="rolling-tab-content" id="rtab-animated">
                        <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
                            <button id="anim-play-btn" onclick="toggleHeatmapPlay()" style="background:var(--bg-elevated);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 14px;cursor:pointer;font-size:0.85em;display:flex;align-items:center;gap:6px">
                                <span id="anim-play-icon">&#9654;</span> <span id="anim-play-label">Play</span>
                            </button>
                            <span id="anim-window-label" style="font-size:0.85em;color:var(--text-dim)"></span>
                        </div>
                        <div id="anim-stack" style="position:relative;width:100%;height:600px;border-radius:12px;border:1px solid var(--border);background:var(--bg-deep);overflow:hidden">
                            <div id="plotly-anim-a" style="position:absolute;inset:0;transition:opacity 1.2s ease;opacity:1"></div>
                            <div id="plotly-anim-b" style="position:absolute;inset:0;transition:opacity 1.2s ease;opacity:0;pointer-events:none"></div>
                        </div>
                        <div style="margin-top:8px">
                            <input type="range" id="anim-slider" min="0" max="0" value="0" style="width:100%;accent-color:var(--accent)" oninput="goToHeatmapFrame(parseInt(this.value), true)">
                        </div>
                        <script>
                        var _animData, _animPlaying = false, _animTimer = null, _animFrame = 0, _animFront = 'a', _animTransitioning = false;
                        (function() {
                            _animData = {{ rolling_plotly_data|safe }};
                            function makeTrace(d, frameIdx) {
                                var f = d.frames[frameIdx], fm = f.zmax || 100;
                                var rdYlGn = [[0,'#a50026'],[0.1,'#d73027'],[0.2,'#f46d43'],[0.3,'#fdae61'],[0.4,'#fee08b'],[0.5,'#ffffbf'],[0.6,'#d9ef8b'],[0.7,'#a6d96a'],[0.8,'#66bd63'],[0.9,'#1a9850'],[1,'#006837']];
                                var trace = {
                                    x: d.periods, y: d.periods, z: f.z,
                                    type: 'heatmap', colorscale: rdYlGn, showscale: true,
                                    zmin: -fm, zmax: fm,
                                    colorbar: { title: {text: d.metric_label, font:{color:'#8890a4'}}, tickfont: {color:'#8890a4'} },
                                    hovertemplate: d.ind1_name + '(%{y}) / ' + d.ind2_name + '(%{x})<br>' + d.metric_label + ': %{z:.1f}<extra></extra>'
                                };
                                var markers = [];
                                if (d.selected_p1 && d.selected_p2) {
                                    markers.push({x:[d.selected_p2], y:[d.selected_p1], mode:'markers',
                                        type:'scatter', marker:{size:14, color:'#f7931a', symbol:'diamond', line:{color:'white',width:2}},
                                        hovertemplate:'Selected: '+d.strategy_label+'<extra></extra>', showlegend:false});
                                }
                                // Per-frame best combo (moves with each window)
                                if (f.best_p1 && f.best_p2) {
                                    markers.push({x:[f.best_p2], y:[f.best_p1], mode:'markers',
                                        type:'scatter', marker:{size:14, color:'#34d399', symbol:'star', line:{color:'white',width:2}},
                                        hovertemplate:'Best in window: '+d.ind1_name+'('+f.best_p1+')/'+d.ind2_name+'('+f.best_p2+')<extra></extra>', showlegend:false});
                                }
                                return {traces: [trace].concat(markers), label: f.label, nMarkers: markers.length};
                            }
                            function makeLayout(d, label) {
                                return {
                                    xaxis: {title: d.ind2_name + ' Period', color:'#8890a4', gridcolor:'#252a3a', dtick: d.dtick},
                                    yaxis: {title: d.ind1_name + ' Period', color:'#8890a4', gridcolor:'#252a3a', dtick: d.dtick},
                                    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: {color:'#e8eaf0'},
                                    title: {text: d.strategy_label + ' \u2014 ' + d.metric_label + ' (' + label + ')', font:{size:14, color:'#e8eaf0'}},
                                    margin: {l:60, r:20, t:50, b:20}
                                };
                            }
                            window._animMakeTrace = makeTrace;
                            window._animMakeLayout = makeLayout;
                            function renderAnimated() {
                                if (typeof Plotly === 'undefined') { setTimeout(renderAnimated, 200); return; }
                                var d = _animData;
                                var t = makeTrace(d, 0);
                                Plotly.newPlot('plotly-anim-a', t.traces, makeLayout(d, t.label), {responsive:true});
                                // Pre-render layer b with same frame (ready for cross-fade)
                                Plotly.newPlot('plotly-anim-b', makeTrace(d, 0).traces, makeLayout(d, t.label), {responsive:true});
                                document.getElementById('anim-slider').max = d.frames.length - 1;
                                document.getElementById('anim-window-label').textContent = 'Window: ' + t.label;
                            }
                            if (typeof Plotly === 'undefined') {
                                var s = document.createElement('script');
                                s.src = 'https://cdn.plot.ly/plotly-2.35.2.min.js';
                                s.onload = renderAnimated;
                                document.head.appendChild(s);
                            } else { renderAnimated(); }
                        })();
                        function _updatePlotlyFrame(elId, d, f) {
                            var fm = f.zmax || 100;
                            Plotly.restyle(elId, {z:[f.z], zmin:[-fm], zmax:[fm]}, [0]);
                            // Update best star marker position (last trace = best marker if it exists)
                            var el = document.getElementById(elId);
                            if (el && el.data && f.best_p1 && f.best_p2) {
                                var bestIdx = el.data.length - 1;
                                Plotly.restyle(elId, {x:[[f.best_p2]], y:[[f.best_p1]],
                                    'hovertemplate': 'Best in window: '+d.ind1_name+'('+f.best_p1+')/'+d.ind2_name+'('+f.best_p2+')<extra></extra>'}, [bestIdx]);
                            }
                            Plotly.relayout(elId, {'title.text': d.strategy_label + ' \u2014 ' + d.metric_label + ' (' + f.label + ')'});
                        }
                        function goToHeatmapFrame(idx, instant) {
                            if (_animTransitioning && !instant) return;
                            _animFrame = idx;
                            var d = _animData, f = d.frames[idx];
                            document.getElementById('anim-slider').value = idx;
                            document.getElementById('anim-window-label').textContent = 'Window: ' + f.label;
                            if (instant) {
                                _updatePlotlyFrame('plotly-anim-' + _animFront, d, f);
                                return;
                            }
                            // Cross-fade: render new frame on back layer, then swap opacity
                            _animTransitioning = true;
                            var backId = _animFront === 'a' ? 'b' : 'a';
                            var frontEl = document.getElementById('plotly-anim-' + _animFront);
                            var backEl = document.getElementById('plotly-anim-' + backId);
                            _updatePlotlyFrame('plotly-anim-' + backId, d, f);
                            // Cross-fade: back fades in, front fades out
                            backEl.style.opacity = '1';
                            backEl.style.pointerEvents = 'auto';
                            frontEl.style.opacity = '0';
                            frontEl.style.pointerEvents = 'none';
                            _animFront = backId;
                            setTimeout(function() { _animTransitioning = false; }, 1300);
                        }
                        function toggleHeatmapPlay() {
                            if (_animPlaying) {
                                clearInterval(_animTimer); _animTimer = null; _animPlaying = false;
                                document.getElementById('anim-play-icon').innerHTML = '&#9654;';
                                document.getElementById('anim-play-label').textContent = 'Play';
                            } else {
                                _animPlaying = true;
                                document.getElementById('anim-play-icon').innerHTML = '&#9646;&#9646;';
                                document.getElementById('anim-play-label').textContent = 'Pause';
                                _animTimer = setInterval(function() {
                                    _animFrame = (_animFrame + 1) % _animData.frames.length;
                                    goToHeatmapFrame(_animFrame, false);
                                }, 2500);
                            }
                        }
                        </script>
                    </div>
                    {% else %}
                    <div class="rolling-tabs">
                        <button class="rolling-tab-btn active" onclick="switchRollingTab('heatmap', this)">Heatmap</button>
                        <button class="rolling-tab-btn" onclick="switchRollingTab('timeline', this)">Timeline</button>
                        <button class="rolling-tab-btn" onclick="switchRollingTab('equity', this)">Equity Overlay</button>
                    </div>
                    <div class="rolling-tab-content" id="rtab-heatmap"><img class="chart-img" src="data:image/png;base64,{{ rolling_charts.heatmap }}"/></div>
                    {% endif %}
                    <div class="rolling-tab-content hidden" id="rtab-timeline"><img class="chart-img" src="data:image/png;base64,{{ rolling_charts.timeline }}"/></div>
                    <div class="rolling-tab-content hidden" id="rtab-equity"><img class="chart-img" src="data:image/png;base64,{{ rolling_charts.equity }}"/></div>
                </div>
            {% elif chart %}
                {% if regression|default(none) %}
                {# Regression analysis results #}
                <div style="position:relative">
                    <img class="chart-img" id="backtest-chart-img" src="data:image/png;base64,{{ chart }}" data-equity-top="{{ equity_top|default(0.7) }}" data-equity-bottom="{{ equity_bottom|default(1.0) }}" />
                    <button onclick="downloadChart()" class="chart-download-btn" title="Download chart as PNG">
                        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M8 2v8m0 0l-3-3m3 3l3-3M3 12h10"/>
                        </svg>
                    </button>
                </div>
                <div class="metrics-panel">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
                    <div>
                        <table class="metrics-table">
                            <thead><tr><th class="col-metric">Statistic</th><th class="col-strategy">Value</th></tr></thead>
                            <tbody>
                            <tr class="section-row"><td colspan="2">Correlation</td></tr>
                            <tr><td class="m-label">R² <span class="m-info" data-tip="Coefficient of determination — measures how much of the variance in forward returns is explained by the oscillator value.&#10;&#10;Range: 0 to 1. A value of 0.05 means the oscillator explains 5% of return variance. Higher = stronger predictive relationship.&#10;In practice, R² above 0.02 for financial data is notable.">ⓘ</span></td><td class="m-val">{{ "%.6f"|format(regression.r_squared) }}</td></tr>
                            <tr><td class="m-label">Pearson r <span class="m-info" data-tip="Linear correlation between the oscillator and forward log returns.&#10;&#10;Range: -1 to +1.&#10;Positive r → higher oscillator values tend to predict higher returns.&#10;Negative r → higher oscillator values tend to predict lower returns.&#10;&#10;Example: r = -0.15 means a weak negative linear relationship.">ⓘ</span></td><td class="m-val">{{ "%.4f"|format(regression.pearson_r) }}</td></tr>
                            <tr><td class="m-label">Spearman ρ <span class="m-info" data-tip="Rank correlation — measures if the oscillator and returns move in the same direction, even if the relationship isn't perfectly linear.&#10;&#10;More robust to outliers than Pearson r.&#10;If Spearman is much higher than Pearson, the relationship may be monotonic but non-linear.">ⓘ</span></td><td class="m-val">{{ "%.4f"|format(regression.spearman_r) }}</td></tr>
                            <tr><td class="m-label">p-value <span class="m-info" data-tip="Statistical significance — the probability of seeing this correlation by pure chance.&#10;&#10;p &lt; 0.05: statistically significant (95% confidence).&#10;p &lt; 0.01: highly significant.&#10;p &lt; 0.001: very highly significant.&#10;&#10;Low p-value confirms the relationship is real, but doesn't tell you how strong it is — check R² for that.">ⓘ</span></td><td class="m-val">{{ "%.2e"|format(regression.p_value) }}</td></tr>
                            <tr class="section-row"><td colspan="2">Regression (log returns)</td></tr>
                            <tr><td class="m-label">Slope <span class="m-info" data-tip="The regression slope in log-return space.&#10;&#10;For each 1-unit increase in the oscillator, the predicted forward log return changes by this amount (in %).&#10;&#10;Example: slope = -0.50 means each +1 on the oscillator predicts 0.50% lower log return over the forward period.&#10;&#10;Negative slope (common for RSI) → higher oscillator = lower expected returns → overbought conditions tend to underperform.">ⓘ</span></td><td class="m-val">{{ "%.4f"|format(regression.slope) }}</td></tr>
                            <tr><td class="m-label">Intercept <span class="m-info" data-tip="The predicted return when the oscillator equals zero.&#10;&#10;Shown as a simple percentage return (converted from log space via exp).&#10;&#10;For RSI (range 0–100), this is the predicted return at RSI = 0. Combined with the slope, you can estimate the predicted return at any oscillator value.&#10;&#10;Example: intercept = 150%, slope = -1.5 → at RSI 50: predicted return ≈ exp((−1.5×50 + log-intercept)/100) − 1.">ⓘ</span></td><td class="m-val">{{ "%.2f"|format(regression.intercept_simple) }}%</td></tr>
                            <tr><td class="m-label">Std Error <span class="m-info" data-tip="Standard error of the slope estimate.&#10;&#10;Measures uncertainty in the slope. A slope of -0.50 ± 0.05 is much more reliable than -0.50 ± 0.40.&#10;&#10;Rule of thumb: if |slope| > 2× std error, the slope is statistically significant.">ⓘ</span></td><td class="m-val">{{ "%.4f"|format(regression.std_err) }}</td></tr>
                            <tr><td class="m-label">Data Points</td><td class="m-val">{{ "{:,}".format(regression.n_points) }}</td></tr>
                            </tbody>
                        </table>
                    </div>
                    <div>
                        <table class="metrics-table">
                            <thead><tr><th class="col-metric">Zone</th><th>Mean Return <span class="m-info" data-tip="Average forward return for all data points in this zone.&#10;Positive = profitable on average.">ⓘ</span></th><th>Median <span class="m-info" data-tip="Middle value of forward returns in this zone.&#10;Less affected by outliers than the mean.&#10;If median &lt; mean, a few large gains are pulling the average up.">ⓘ</span></th><th>Count</th><th>Win Rate <span class="m-info" data-tip="Percentage of data points with positive forward returns.&#10;Above 50% = more winners than losers in this zone.">ⓘ</span></th></tr></thead>
                            <tbody>
                            {% for zone_name, zone in [('Oversold', regression.zone_stats.oversold), ('Neutral', regression.zone_stats.neutral), ('Overbought', regression.zone_stats.overbought)] %}
                            <tr>
                                <td class="m-label" style="color:{{ '#34d399' if zone_name == 'Oversold' else '#ef4444' if zone_name == 'Overbought' else '#8890a4' }}">{{ zone_name }}</td>
                                <td class="m-val {{ 'positive' if zone.mean > 0 else 'negative' }}">{{ "%.1f"|format(zone.mean) }}%</td>
                                <td class="m-val {{ 'positive' if zone.median > 0 else 'negative' }}">{{ "%.1f"|format(zone.median) }}%</td>
                                <td class="m-val">{{ zone.count }}</td>
                                <td class="m-val">{{ "%.1f"|format(zone.win_rate) }}%</td>
                            </tr>
                            {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
                </div>
                {% if regression_sweep_chart|default(none) %}
                <div style="position:relative;margin-top:16px">
                    <img class="chart-img" src="data:image/png;base64,{{ regression_sweep_chart }}" />
                </div>
                {% endif %}
                <input type="hidden" id="equity-thumbnail" value="{{ thumb_b64|default('') }}">
                {% if is_authenticated %}
                <div class="action-buttons" id="backtest-actions">
                    <button class="action-btn" onclick="saveBacktest()" id="save-btn">
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v10h10V6l-3-3H3z"/><path d="M5 3v3h4V3"/><path d="M5 9h6v4H5z"/></svg>
                        Save
                    </button>
                    <button class="action-btn primary" onclick="openPublishModal()">
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12l4-4 4 4"/><path d="M8 8v6"/><path d="M13.5 10.5A3.5 3.5 0 0010 5a4 4 0 00-7.5 2"/></svg>
                        Publish
                    </button>
                    <button class="action-btn hidden" onclick="copyShortLink()" id="copy-link-btn">
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a3 3 0 004.24 0l2-2a3 3 0 00-4.24-4.24L6.5 3.26"/><path d="M10 8a3 3 0 00-4.24 0l-2 2a3 3 0 004.24 4.24L9.5 12.74"/></svg>
                        Copy Link
                    </button>
                </div>
                {% endif %}
                {% elif best %}
                {% if lev_sweep|default(none) %}
                {# Leverage sweep mode — keep compact table #}
                <table class="results-table" style="margin-bottom:16px">
                    <tr>
                        <th style="text-align:left">Strategy</th>
                        <th>Ann. Return</th>
                        <th>Max Drawdown</th>
                        <th>Trades</th>
                        <th>Leverage</th>
                    </tr>
                    {% if not hide_buyhold|default(false) %}
                    <tr>
                        <td style="text-align:left">Buy & Hold</td>
                        <td>{{ "%.2f"|format(best.buyhold_annualized) }}%</td>
                        <td>{{ "%.2f"|format(best.buyhold_max_drawdown) }}%</td>
                        <td>1</td>
                        <td></td>
                    </tr>
                    {% endif %}
                    <tr>
                        <td style="text-align:left">Best Long Leverage</td>
                        <td>{{ "%.1f"|format(lev_sweep.best_long_ann) }}%</td>
                        <td></td>
                        <td></td>
                        <td>{{ "%.2f"|format(lev_sweep.best_long_lev) }}x</td>
                    </tr>
                    <tr>
                        <td style="text-align:left">Best Short Leverage</td>
                        <td>{{ "%.1f"|format(lev_sweep.best_short_ann) }}%</td>
                        <td></td>
                        <td></td>
                        <td>{{ "%.2f"|format(lev_sweep.best_short_lev) }}x</td>
                    </tr>
                    <tr class="best">
                        <td style="text-align:left">{{ lev_sweep.combined_label }}</td>
                        <td>{{ "%.1f"|format(lev_sweep.combined_ann) }}%</td>
                        <td>{{ "%.2f"|format(best.max_drawdown) }}%</td>
                        <td>{{ best.trades }}</td>
                        <td>{{ "%.2f"|format(lev_sweep.best_long_lev) }}x / {{ "%.2f"|format(lev_sweep.best_short_lev) }}x</td>
                    </tr>
                </table>
                {% endif %}
                {% endif %}
                {% if not regression|default(none) %}
                {% if table_rows %}
                <details style="margin-bottom:16px">
                    <summary>Show all results ({{ table_rows|length }})</summary>
                    <div style="max-height:300px;overflow-y:auto;margin-top:8px">
                    <table class="results-table">
                        <tr><th>Strategy</th><th>Return %</th><th>B&H %</th><th>Max DD %</th><th>Trades</th></tr>
                        {% for r in table_rows %}
                        <tr{% if loop.first %} class="best"{% endif %}>
                            <td>{{ r.label }}</td><td>{{ "%.2f"|format(r.total_return) }}</td>
                            <td>{{ "%.2f"|format(r.buyhold_return) }}</td><td>{{ "%.2f"|format(r.max_drawdown) }}</td>
                            <td>{{ r.trades }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                    </div>
                </details>
                {% endif %}
                {% if price_json %}
                <div class="chart-tabs">
                    <button class="chart-tab active" onclick="switchChartTab('backtest', this)">Backtest Chart</button>
                    <button class="chart-tab" onclick="switchChartTab('livechart', this)">Live Chart</button>
                </div>
                {% endif %}
                <div id="backtest-chart-tab">
                    <div style="position:relative">
                        <img class="chart-img" id="backtest-chart-img" src="data:image/png;base64,{{ chart }}" data-equity-top="{{ equity_top|default(0.7) }}" data-equity-bottom="{{ equity_bottom|default(1.0) }}" />
                        <button onclick="downloadChart()" class="chart-download-btn" title="Download chart as PNG">
                            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M8 2v8m0 0l-3-3m3 3l3-3M3 12h10"/>
                            </svg>
                        </button>
                    </div>
                </div>
                <input type="hidden" id="equity-thumbnail" value="{{ thumb_b64|default('') }}">
                {% if price_json %}
                <div id="livechart-tab" style="display:none">
                    <div id="lw-chart-container"
                         style="position:relative;height:600px;border-radius:12px;overflow:hidden;border:1px solid var(--border)">
                    </div>
                    <div style="display:flex;gap:16px;justify-content:center;margin-top:8px;font-size:0.72em;color:var(--text-muted);font-family:'JetBrains Mono',monospace;letter-spacing:0.02em">
                        <span><kbd style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:0.95em">M</kbd> Measure</span>
                        <span><kbd style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:0.95em">D</kbd> Draw line</span>
                        <span><kbd style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:0.95em">L</kbd> Log/Linear</span>
                        <span><kbd style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:0.95em">C</kbd> Clear</span>
                        <span><kbd style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:0.95em">Esc</kbd> Cancel</span>
                    </div>
                </div>
                <script>
                var __lwAsset = {{ p.asset|tojson }};
                var __lwVsAsset = {{ (p.vs_asset or '')|tojson }};
                var __lwData = {
                    price: {{ price_json|safe }},
                    ind1: {{ ind1_json|safe }},
                    ind2: {{ ind2_json|safe }},
                    ind1Label: {{ ind1_label|tojson }},
                    ind2Label: {{ ind2_label|tojson }}
                };
                </script>
                {% endif %}
                {% if best %}
                <div id="best-params" data-ind1-period="{{ best.get('ind1_period', '') }}" data-ind2-period="{{ best.get('ind2_period', '') }}"
                     data-best-long-lev="{{ lev_sweep.best_long_lev if lev_sweep|default(none) else '' }}"
                     data-best-short-lev="{{ lev_sweep.best_short_lev if lev_sweep|default(none) else '' }}"
                     style="display:none"></div>
                {% endif %}
                {% if is_authenticated %}
                <div class="action-buttons" id="backtest-actions">
                    {% if best and p.mode in ('sweep', 'heatmap', 'sweep-lev') %}
                    <button class="action-btn primary" onclick="viewBestInBacktest()">
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 12 8 4 12 12"/><line x1="2" y1="14" x2="14" y2="14" opacity="0.4"/></svg>
                        View Best
                    </button>
                    {% endif %}
                    <button class="action-btn" onclick="saveBacktest()" id="save-btn">
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v10h10V6l-3-3H3z"/><path d="M5 3v3h4V3"/><path d="M5 9h6v4H5z"/></svg>
                        Save
                    </button>
                    <button class="action-btn primary" onclick="openPublishModal()">
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12l4-4 4 4"/><path d="M8 8v6"/><path d="M13.5 10.5A3.5 3.5 0 0010 5a4 4 0 00-7.5 2"/></svg>
                        Publish
                    </button>
                    <button class="action-btn hidden" onclick="copyShortLink()" id="copy-link-btn">
                        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a3 3 0 004.24 0l2-2a3 3 0 00-4.24-4.24L6.5 3.26"/><path d="M10 8a3 3 0 00-4.24 0l-2 2a3 3 0 004.24 4.24L9.5 12.74"/></svg>
                        Copy Link
                    </button>
                </div>
                {% endif %}
                {% if best and best.dca_mode|default(false) %}
                {# DCA-specific metrics table #}
                <div class="metrics-panel">
                <table class="metrics-table">
                    <thead><tr>
                        <th class="col-metric">Metric</th>
                        <th class="col-strategy">Dynamic DCA</th>
                        <th class="col-buyhold">Constant DCA</th>
                    </tr></thead>
                    <tbody>
                    <tr class="section-row"><td colspan="3">Portfolio</td></tr>
                    <tr><td class="m-label">Total Invested</td><td class="m-val">${{ "{:,.0f}".format(best.total_invested) }}</td><td class="m-val">${{ "{:,.0f}".format(best.total_invested) }}</td></tr>
                    <tr><td class="m-label">Final Value</td><td class="m-val {{ 'positive' if best.final_value > best.total_invested else 'negative' }}">${{ "{:,.2f}".format(best.final_value) }}</td><td class="m-val {{ 'positive' if best.const_final_value > best.total_invested else 'negative' }}">${{ "{:,.2f}".format(best.const_final_value) }}</td></tr>
                    <tr><td class="m-label">Dynamic Advantage</td><td class="m-val {{ 'positive' if best.advantage > 0 else 'negative' }}">${{ "{:,.2f}".format(best.advantage) }} ({{ "%+.2f"|format(best.advantage_pct) }}%)</td><td class="m-val"></td></tr>
                    <tr class="section-row"><td colspan="3">Performance</td></tr>
                    <tr><td class="m-label">Total Return</td><td class="m-val {{ 'positive' if best.total_return > 0 else 'negative' }}">{{ "%.2f"|format(best.total_return) }}%</td><td class="m-val {{ 'positive' if best.buyhold_return > 0 else 'negative' }}">{{ "%.2f"|format(best.buyhold_return) }}%</td></tr>
                    <tr><td class="m-label">Ann. Return</td><td class="m-val {{ 'positive' if best.annualized > 0 else 'negative' }}">{{ "%.2f"|format(best.annualized) }}%</td><td class="m-val {{ 'positive' if best.buyhold_annualized > 0 else 'negative' }}">{{ "%.2f"|format(best.buyhold_annualized) }}%</td></tr>
                    <tr class="section-row"><td colspan="3">Risk</td></tr>
                    <tr><td class="m-label">Max Drawdown</td><td class="m-val negative">{{ "%.2f"|format(best.max_drawdown) }}%</td><td class="m-val negative">{{ "%.2f"|format(best.buyhold_max_drawdown) }}%</td></tr>
                    <tr><td class="m-label">Drawdown Duration</td><td class="m-val">{{ best.max_dd_duration|duration }}</td><td class="m-val">{{ best.buyhold_max_dd_duration|duration }}</td></tr>
                    <tr class="section-row"><td colspan="3">Purchase Stats</td></tr>
                    <tr><td class="m-label">Purchases</td><td class="m-val">{{ best.n_purchases }}</td><td class="m-val">{{ best.const_n_purchases }}</td></tr>
                    <tr><td class="m-label">Avg Buy Amount</td><td class="m-val">${{ "{:,.2f}".format(best.avg_buy_amount) }}</td><td class="m-val">${{ "{:,.2f}".format(best.const_avg_buy_amount) }}</td></tr>
                    <tr><td class="m-label">Median Buy Amount</td><td class="m-val">${{ "{:,.2f}".format(best.median_buy_amount) }}</td><td class="m-val">${{ "{:,.2f}".format(best.const_median_buy_amount) }}</td></tr>
                    <tr><td class="m-label">Min Buy</td><td class="m-val">${{ "{:,.2f}".format(best.min_buy_amount) }}</td><td class="m-val">${{ "{:,.2f}".format(best.const_min_buy_amount) }}</td></tr>
                    <tr><td class="m-label">Max Buy</td><td class="m-val">${{ "{:,.2f}".format(best.max_buy_amount) }}</td><td class="m-val">${{ "{:,.2f}".format(best.const_max_buy_amount) }}</td></tr>
                    <tr class="section-row"><td colspan="3">Accumulation</td></tr>
                    <tr><td class="m-label">Total Units</td><td class="m-val">{{ "%.6f"|format(best.total_units) }}</td><td class="m-val">{{ "%.6f"|format(best.const_total_units) }}</td></tr>
                    <tr><td class="m-label">Avg Cost per Unit</td><td class="m-val">${{ "{:,.2f}".format(best.avg_cost_per_unit) }}</td><td class="m-val">${{ "{:,.2f}".format(best.const_avg_cost_per_unit) }}</td></tr>
                    <tr><td class="m-label">Unit Advantage</td><td class="m-val {{ 'positive' if best.total_units > best.const_total_units else 'negative' }}" colspan="2">{{ "%+.2f"|format((best.total_units / best.const_total_units - 1) * 100 if best.const_total_units > 0 else 0) }}% more units</td></tr>
                    </tbody>
                </table>
                </div>
                {% elif best and not lev_sweep|default(none) %}
                {# Compact 3-column metrics table: Metric | Strategy | Buy & Hold #}
                <div class="metrics-panel">
                <table class="metrics-table">
                    <thead><tr>
                        <th class="col-metric">Metric</th>
                        <th class="col-strategy">{{ best.label }}{% if p.long_leverage != 1 or p.short_leverage != 1 %} ({{ "%.3g"|format(p.long_leverage) }}x Long, {{ "%.3g"|format(p.short_leverage) }}x Short){% endif %}</th>
                        <th class="col-buyhold">Buy & Hold</th>
                    </tr></thead>
                    <tbody>
                    {% set ls = ls_breakdown|default(none) %}
                    <tr class="section-row"><td colspan="3">Performance</td></tr>
                    <tr><td class="m-label">$100 would be <span class="m-info" data-tip="What $100 invested at the start would be worth today.&#10;Formula: $100 × (1 + total return)">ⓘ</span></td><td class="m-val {{ 'positive' if best.total_return > 0 else 'negative' }}">${{ "{:,.2f}".format(100 * (1 + best.total_return / 100)) }}</td><td class="m-val {{ 'positive' if best.buyhold_return > 0 else 'negative' }}">${{ "{:,.2f}".format(100 * (1 + best.buyhold_return / 100)) }}</td></tr>
                    <tr><td class="m-label">Ann. Return <span class="m-info" data-tip="Yearly average return, compounded.&#10;Formula: (total growth)^(365/days) − 1">ⓘ</span></td><td class="m-val {{ 'positive' if best.annualized > 0 else 'negative' }}">{{ "%.2f"|format(best.annualized) }}%</td><td class="m-val {{ 'positive' if best.buyhold_annualized > 0 else 'negative' }}">{{ "%.2f"|format(best.buyhold_annualized) }}%</td></tr>
                    <tr><td class="m-label">Sharpe Ratio <span class="m-info" data-tip="Return per unit of risk — higher is better.&#10;Formula: mean daily return ÷ std dev × √365">ⓘ</span></td><td class="m-val">{{ "%.2f"|format(best.sharpe) }}</td><td class="m-val">{{ "%.2f"|format(best.buyhold_sharpe) }}</td></tr>
                    <tr><td class="m-label">Sortino Ratio <span class="m-info" data-tip="Like Sharpe but only penalizes downside risk.&#10;Formula: mean daily return ÷ downside std × √365">ⓘ</span></td><td class="m-val">{{ "%.2f"|format(best.sortino) }}</td><td class="m-val">{{ "%.2f"|format(best.buyhold_sortino) }}</td></tr>
                    <tr class="section-row"><td colspan="3">Risk</td></tr>
                    <tr><td class="m-label">Max Drawdown <span class="m-info" data-tip="Largest peak-to-trough drop.&#10;Formula: (trough − peak) ÷ peak">ⓘ</span></td><td class="m-val negative">{{ "%.2f"|format(best.max_drawdown) }}%</td><td class="m-val negative">{{ "%.2f"|format(best.buyhold_max_drawdown) }}%</td></tr>
                    <tr><td class="m-label">Drawdown Duration <span class="m-info" data-tip="Longest time spent below a previous high.&#10;Days from peak until recovery">ⓘ</span></td><td class="m-val">{{ best.max_dd_duration|duration }}</td><td class="m-val">{{ best.buyhold_max_dd_duration|duration }}</td></tr>
                    <tr><td class="m-label">Volatility <span class="m-info" data-tip="How much returns fluctuate day to day.&#10;Formula: annualized std dev of daily returns">ⓘ</span></td><td class="m-val">{{ "%.1f"|format(best.volatility) }}%</td><td class="m-val">{{ "%.1f"|format(best.buyhold_volatility) }}%</td></tr>
                    <tr><td class="m-label">Beta <span class="m-info" data-tip="How much the strategy moves with the market.&#10;Formula: Cov(strategy, market) ÷ Var(market)">ⓘ</span></td><td class="m-val">{{ "%.2f"|format(best.beta) }}</td><td class="m-val muted">1.00</td></tr>
                    <tr><td class="m-label">Calmar Ratio <span class="m-info" data-tip="Return relative to worst drawdown.&#10;Formula: Ann. return ÷ |max drawdown|">ⓘ</span></td><td class="m-val">{{ "%.2f"|format(best.calmar) }}</td><td class="m-val">{{ "%.2f"|format(best.buyhold_calmar) }}</td></tr>
                    <tr class="section-row"><td colspan="3">Trades</td></tr>
                    <tr><td class="m-label">Trades <span class="m-info" data-tip="Number of position changes.&#10;Count of buy/sell signals">ⓘ</span></td><td class="m-val">{{ best.trades }}{% if ls %}<span class="m-ls"><span class="ls-l">Long {{ ls.long.trades }}</span><br><span class="ls-s">Short {{ ls.short.trades }}</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Win Rate <span class="m-info" data-tip="Percentage of trades that made money.&#10;Formula: winning trades ÷ total trades">ⓘ</span></td><td class="m-val">{{ "%.1f"|format(best.win_rate) }}%{% if ls %}<span class="m-ls"><span class="ls-l">Long {{ "%.1f"|format(ls.long.win_rate) }}%</span><br><span class="ls-s">Short {{ "%.1f"|format(ls.short.win_rate) }}%</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Avg Win / Loss <span class="m-info" data-tip="Average return of winning vs losing trades.&#10;Formula: mean return of wins / losses">ⓘ</span></td><td class="m-val"><span class="positive">+{{ "%.1f"|format(best.avg_win) }}%</span> / <span class="negative">{{ "%.1f"|format(best.avg_loss) }}%</span>{% if ls %}<span class="m-ls"><span class="ls-l">Long +{{ "%.1f"|format(ls.long.avg_win) }}% / {{ "%.1f"|format(ls.long.avg_loss) }}%</span><br><span class="ls-s">Short +{{ "%.1f"|format(ls.short.avg_win) }}% / {{ "%.1f"|format(ls.short.avg_loss) }}%</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Profit Factor <span class="m-info" data-tip="Gross profits divided by gross losses.&#10;Formula: sum of wins ÷ sum of losses">ⓘ</span></td><td class="m-val">{% if best.profit_factor > 9999 %}&infin;{% else %}{{ "%.2f"|format(best.profit_factor) }}{% endif %}{% if ls %}<span class="m-ls"><span class="ls-l">Long {% if ls.long.profit_factor > 9999 %}&infin;{% else %}{{ "%.2f"|format(ls.long.profit_factor) }}{% endif %}</span><br><span class="ls-s">Short {% if ls.short.profit_factor > 9999 %}&infin;{% else %}{{ "%.2f"|format(ls.short.profit_factor) }}{% endif %}</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Avg Duration <span class="m-info" data-tip="Average holding period per trade.&#10;Formula: total days in trades ÷ trade count">ⓘ</span></td><td class="m-val">{{ "%.0f"|format(best.avg_trade_duration) }}d{% if ls %}<span class="m-ls"><span class="ls-l">Long {{ "%.0f"|format(ls.long.avg_trade_duration) }}d</span><br><span class="ls-s">Short {{ "%.0f"|format(ls.short.avg_trade_duration) }}d</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    {% if best.get('total_financing_cost', 0) != 0 %}
                    <tr class="section-row"><td colspan="3">Financing</td></tr>
                    {% if best.get('financing_cost_long', 0) > 0 %}
                    <tr><td class="m-label">Long Cost <span class="m-info" data-tip="Financing fees paid while holding long positions.&#10;Crypto: leverage &times; rate / 365 daily&#10;Tradfi: (leverage-1) &times; rate / trading days">ⓘ</span></td><td class="m-val negative">-${{ "{:,.0f}".format(best.financing_cost_long) }}</td><td class="m-val muted">&mdash;</td></tr>
                    {% endif %}
                    {% if best.get('financing_cost_short', 0) > 0 %}
                    <tr><td class="m-label">Short Revenue <span class="m-info" data-tip="Financing fees earned while holding short positions.&#10;Short positions earn the financing rate.">ⓘ</span></td><td class="m-val positive">+${{ "{:,.0f}".format(best.financing_cost_short) }}</td><td class="m-val muted">&mdash;</td></tr>
                    {% endif %}
                    <tr><td class="m-label">Net Financing <span class="m-info" data-tip="Net financing cost (long cost minus short revenue).&#10;Positive = net cost, negative = net income.">ⓘ</span></td><td class="m-val {% if best.total_financing_cost > 0 %}negative{% else %}positive{% endif %}">${{ "{:,.0f}".format(best.total_financing_cost) }}</td><td class="m-val muted">&mdash;</td></tr>
                    {% endif %}
                    <tr class="section-row"><td colspan="3">Annual</td></tr>
                    <tr><td class="m-label">Best Year <span class="m-info" data-tip="Highest calendar year return.&#10;Max of yearly returns">ⓘ</span></td><td class="m-val positive">{% if best.best_year[0] %}+{{ "%.1f"|format(best.best_year[1]) }}% ({{ best.best_year[0] }}){% else %}&mdash;{% endif %}</td><td class="m-val positive">{% if best.buyhold_best_year[0] %}+{{ "%.1f"|format(best.buyhold_best_year[1]) }}% ({{ best.buyhold_best_year[0] }}){% else %}&mdash;{% endif %}</td></tr>
                    <tr><td class="m-label">Worst Year <span class="m-info" data-tip="Lowest calendar year return.&#10;Min of yearly returns">ⓘ</span></td><td class="m-val negative">{% if best.worst_year[0] %}{{ "%.1f"|format(best.worst_year[1]) }}% ({{ best.worst_year[0] }}){% else %}&mdash;{% endif %}</td><td class="m-val negative">{% if best.buyhold_worst_year[0] %}{{ "%.1f"|format(best.buyhold_worst_year[1]) }}% ({{ best.buyhold_worst_year[0] }}){% else %}&mdash;{% endif %}</td></tr>
                    </tbody>
                </table>
                </div>
                {% endif %}
                {% endif %}
            {% else %}
                <div class="placeholder">Configure parameters and press Run Backtest</div>
            {% endif %}
        </div>
    </div>
</div>
<script src="/static/js/nav.js"></script>
<script src="/static/js/chart.js"></script>
<script>
var assetStarts = {{ asset_starts_json|tojson }};
function selectMode(mode, el) {
    document.getElementById('mode').value = mode;
    var cards = document.querySelectorAll('.mode-card');
    for (var i = 0; i < cards.length; i++) cards[i].classList.remove('active');
    el.classList.add('active');
    toggleFields();
}
function switchRollingTab(name, btn) {
    document.querySelectorAll('.rolling-tab-content').forEach(function(el) { el.classList.add('hidden'); });
    document.getElementById('rtab-' + name).classList.remove('hidden');
    document.querySelectorAll('.rolling-tab-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    if (name === 'animated' && typeof Plotly !== 'undefined') {
        setTimeout(function(){
            var a = document.getElementById('plotly-anim-a');
            var b = document.getElementById('plotly-anim-b');
            if (a && a.data) Plotly.Plots.resize(a);
            if (b && b.data) Plotly.Plots.resize(b);
        }, 50);
    }
}
var oscDefaults = {
    rsi:        { period: 14, buy: 30, sell: 70, desc: 'Relative Strength Index \u2014 buy when dropping below 30 (oversold/cheap), sell when rising above 70 (overbought/expensive). Hold between thresholds.' },
    macd:       { period: 9, buy: 0, sell: 0, desc: 'MACD \u2014 buy when MACD crosses above signal line, sell when below. Period controls signal line smoothing.' },
    stochastic: { period: 14, buy: 20, sell: 80, desc: 'Stochastic Oscillator \u2014 buy when dropping below 20 (oversold/cheap), sell when rising above 80 (overbought/expensive). Hold between thresholds.' },
    cci:        { period: 20, buy: -100, sell: 100, desc: 'Commodity Channel Index \u2014 buy when dropping below \u2212100 (oversold), sell when rising above +100 (overbought). Hold between thresholds.' },
    roc:        { period: 12, buy: 0, sell: 0, desc: 'Rate of Change \u2014 buy when dropping below 0 (negative momentum = cheap), sell when rising above 0 (positive = expensive). Hold between thresholds.' },
    momentum:   { period: 10, buy: 0, sell: 0, desc: 'Price Momentum \u2014 buy when dropping below 0 (downward momentum = cheap), sell when rising above 0 (upward = expensive). Hold between thresholds.' },
    williams_r: { period: 14, buy: -80, sell: -20, desc: 'Williams %R \u2014 buy when dropping below \u221280 (oversold/cheap), sell when rising above \u221220 (overbought/expensive). Hold between thresholds.' }
};
function _isOscValue(val) { return val && val.indexOf('osc_') === 0; }
function _oscKey(val) { return val.substring(4); }
function _syncOscHidden() {
    // Auto-detect signal type from ind2 dropdown and sync hidden fields
    var ind2Val = document.getElementById('ind2_name').value;
    var isOsc = _isOscValue(ind2Val);
    document.getElementById('signal_type').value = isOsc ? 'oscillator' : 'crossover';
    if (isOsc) {
        var oscName = _oscKey(ind2Val);
        document.getElementById('osc_name').value = oscName;
        var d = oscDefaults[oscName];
        if (d) {
            var oscPeriodEl = document.getElementById('osc_period');
            var buyEl = document.getElementById('buy_threshold');
            var sellEl = document.getElementById('sell_threshold');
            // Set defaults when switching to a new oscillator
            if (buyEl.dataset.lastOsc !== oscName) {
                buyEl.value = d.buy;
                sellEl.value = d.sell;
                oscPeriodEl.value = d.period;
            }
            if (!oscPeriodEl.value) oscPeriodEl.value = d.period;
            buyEl.dataset.lastOsc = oscName;
            document.getElementById('osc-description').textContent = d.desc;
        }
    }
    return isOsc;
}
function toggleFields() {
    var mode = document.getElementById('mode').value;
    var isRegression = mode === 'regression';
    var isDCA = mode === 'dca';
    var ind2El = document.getElementById('ind2_name');

    // Auto-select RSI for regression mode if not already an oscillator
    if (isRegression && !_isOscValue(ind2El.value)) {
        ind2El.value = 'osc_rsi';
    }

    var isOsc = _syncOscHidden();
    var ind1El = document.getElementById('ind1_name');
    var ind1 = ind1El.value;
    // Auto-promote ind1 from price to SMA in heatmap mode
    if (mode === 'heatmap' && ind1 === 'price') {
        ind1El.value = 'sma';
        ind1 = 'sma';
    }

    var isLevSweep = mode === 'sweep-lev';

    // Show/hide Indicators section (hidden in DCA mode)
    var indSection = document.getElementById('indicators-section');
    if (isDCA) { indSection.classList.add('hidden'); } else { indSection.classList.remove('hidden'); }

    // Show/hide DCA section
    var dcaSection = document.getElementById('dca-section');
    if (isDCA) {
        dcaSection.classList.remove('hidden');
        var dcaInputs = dcaSection.querySelectorAll('input,select');
        for (var di = 0; di < dcaInputs.length; di++) dcaInputs[di].disabled = false;
        // Show/hide DCA signal name based on signal type
        var dcaSigType = document.getElementById('dca_signal_type').value;
        var dcaSigNameGroup = document.getElementById('dca-signal-name-group');
        var dcaSigPeriodGroup = document.getElementById('dca-signal-period-group');
        var athInfo = document.getElementById('ath-info');
        if (athInfo) athInfo.style.display = (dcaSigType === 'ath_drawdown') ? 'inline' : 'none';
        var dcaSigPeriodLabel = document.getElementById('dca-signal-period-label');
        var dcaSigPeriodInput = dcaSigPeriodGroup.querySelectorAll('input')[0];
        if (dcaSigType === 'ath_drawdown') {
            dcaSigNameGroup.classList.add('hidden');
            dcaSigNameGroup.querySelectorAll('select')[0].disabled = true;
            dcaSigPeriodGroup.classList.remove('hidden');
            dcaSigPeriodInput.disabled = false;
            dcaSigPeriodLabel.textContent = 'Lookback (years)';
            dcaSigPeriodInput.placeholder = '5';
            if (!dcaSigPeriodInput.value) dcaSigPeriodInput.value = '5';
        } else {
            dcaSigNameGroup.classList.remove('hidden');
            dcaSigNameGroup.querySelectorAll('select')[0].disabled = false;
            dcaSigPeriodGroup.classList.remove('hidden');
            dcaSigPeriodInput.disabled = false;
            dcaSigPeriodLabel.textContent = 'Signal Period';
            dcaSigPeriodInput.placeholder = '14';
            // Update label and options
            var dcaSigNameLabel = document.getElementById('dca-signal-name-label');
            var dcaOscOpt = document.getElementById('dca-osc-optgroup');
            var dcaMaOpt = document.getElementById('dca-ma-optgroup');
            if (dcaSigType === 'oscillator') {
                dcaSigNameLabel.textContent = 'Oscillator';
                dcaOscOpt.style.display = '';
                dcaMaOpt.style.display = 'none';
            } else {
                dcaSigNameLabel.textContent = 'Moving Average';
                dcaOscOpt.style.display = 'none';
                dcaMaOpt.style.display = '';
            }
        }
        // Show sweep row for sweep/heatmap sub-modes
        var dcaSweepRow = document.getElementById('dca-sweep-row');
        if (document.getElementById('dca-sub-mode')) {
            var dcaSubMode = document.getElementById('dca-sub-mode').value;
            if (dcaSubMode === 'sweep') {
                dcaSweepRow.classList.remove('hidden');
            } else {
                dcaSweepRow.classList.add('hidden');
            }
        }
    } else {
        dcaSection.classList.add('hidden');
        var dcaInputs2 = dcaSection.querySelectorAll('input,select');
        for (var di2 = 0; di2 < dcaInputs2.length; di2++) dcaInputs2[di2].disabled = true;
    }

    // Show/hide oscillator param row and description
    var oscParamsRow = document.getElementById('osc-params-row');
    var oscDesc = document.getElementById('osc-description');
    if ((isOsc || isRegression) && !isDCA) {
        oscParamsRow.classList.remove('hidden');
        oscDesc.classList.remove('hidden');
        var oInputs = oscParamsRow.querySelectorAll('input');
        for (var oi = 0; oi < oInputs.length; oi++) oInputs[oi].disabled = false;
    } else {
        oscParamsRow.classList.add('hidden');
        oscDesc.classList.add('hidden');
        var oInputs2 = oscParamsRow.querySelectorAll('input');
        for (var oi2 = 0; oi2 < oInputs2.length; oi2++) oInputs2[oi2].disabled = true;
    }

    // Show/hide forward days row
    var fwdRow = document.getElementById('forward-days-row');
    if (isRegression) {
        fwdRow.classList.remove('hidden');
        fwdRow.querySelector('input').disabled = false;
    } else {
        fwdRow.classList.add('hidden');
        fwdRow.querySelector('input').disabled = true;
    }

    var isRolling = mode === 'rolling';
    var rollingRow = document.getElementById('rolling-params-row');
    if (isRolling) { rollingRow.classList.remove('hidden'); } else { rollingRow.classList.add('hidden'); }
    var rInputs = rollingRow.querySelectorAll('input,select');
    for (var ri = 0; ri < rInputs.length; ri++) rInputs[ri].disabled = !isRolling;

    var sizingVal = document.querySelector('select[name="sizing"]').value;
    var rules = [
        ['ind1-group', !isOsc && !isRegression && !isDCA],
        ['period1-group', !isOsc && !isRegression && !isDCA && ind1 !== 'price' && mode !== 'heatmap'],
        ['ind-sep', !isOsc && !isRegression && !isDCA],
        ['period2-group', !isOsc && !isRegression && !isDCA && (mode === 'backtest' || mode === 'sweep-lev' || isRolling)],
        ['range-min-group', !isOsc && !isRegression && !isDCA && (mode === 'sweep' || mode === 'heatmap' || isRolling)],
        ['range-max-group', !isOsc && !isRegression && !isDCA && (mode === 'sweep' || mode === 'heatmap' || isRolling)],
        ['step-group', !isOsc && !isRegression && !isDCA && (mode === 'heatmap' || isRolling)],
        ['long-lev-group', !isLevSweep && !isRegression && !isDCA],
        ['short-lev-group', !isLevSweep && !isRegression && !isDCA],
        ['exposure-group', !isLevSweep && !isRegression && !isDCA],
        ['lev-mode-group', !isRegression && !isDCA],
        ['sizing-group', !isRegression && !isDCA],
        ['financing-group', !isRegression && !isDCA && sizingVal !== 'fixed'],
        ['lev-min-group', isLevSweep && !isRegression],
        ['lev-max-group', isLevSweep && !isRegression],
        ['exposure-section', !isDCA],
    ];
    for (var i = 0; i < rules.length; i++) {
        var el = document.getElementById(rules[i][0]);
        if (!el) continue;
        var show = rules[i][1];
        if (show) { el.classList.remove('hidden'); } else { el.classList.add('hidden'); }
        var inputs = el.querySelectorAll('input,select');
        for (var j = 0; j < inputs.length; j++) inputs[j].disabled = !show;
    }
    updateExplainer();
}
function updateExplainer() {
    var rev = document.getElementById('reverse').checked;
    var el = document.getElementById('explainer-text');
    var ind2Val = document.getElementById('ind2_name').value;
    var mode = document.getElementById('mode').value;

    if (mode === 'dca') {
        var dcaSigType = document.getElementById('dca_signal_type').value;
        var dcaFreq = document.getElementById('dca_frequency').value;
        var dcaAmount = document.getElementById('dca_amount') ? document.getElementById('dca_amount').value : '100';
        var sigDesc = dcaSigType === 'oscillator' ? 'oscillator signal' : (dcaSigType === 'ma_distance' ? 'MA distance' : 'ATH drawdown');
        el.innerHTML = 'Compare constant $' + dcaAmount + '/' + dcaFreq + ' DCA vs dynamic DCA adjusted by ' + sigDesc + '. Budget is always equal.';
        return;
    }

    if (mode === 'regression' && _isOscValue(ind2Val)) {
        var osc = _oscKey(ind2Val);
        var oscPer = document.getElementById('osc_period').value || oscDefaults[osc].period;
        var fwdDays = document.getElementById('forward_days').value || 365;
        var oscLabel = osc.toUpperCase().replace('_', ' ') + '(' + oscPer + ')';
        el.innerHTML = 'Scatter plot: ' + oscLabel + ' value vs forward ' + fwdDays + '-day return. Regression line shows linear relationship.';
        return;
    }

    if (_isOscValue(ind2Val)) {
        var osc = _oscKey(ind2Val);
        var buyThr = document.getElementById('buy_threshold').value;
        var sellThr = document.getElementById('sell_threshold').value;
        var oscPer = document.getElementById('osc_period').value || oscDefaults[osc].period;
        var oscLabel = osc.toUpperCase().replace('_', ' ') + '(' + oscPer + ')';
        if (osc === 'macd') {
            if (rev) {
                el.innerHTML = '<b>Sell</b> when MACD(' + oscPer + ') crosses above signal line. <b>Buy</b> when it crosses below.';
            } else {
                el.innerHTML = 'Buy when MACD(' + oscPer + ') crosses above signal line. Sell when it crosses below.';
            }
        } else {
            if (rev) {
                el.innerHTML = '<b>Sell</b> when ' + oscLabel + ' drops below ' + buyThr + '. <b>Buy</b> when it rises above ' + sellThr + '. Hold between thresholds.';
            } else {
                el.innerHTML = 'Buy when ' + oscLabel + ' drops below ' + buyThr + ' (cheap). Sell when it rises above ' + sellThr + ' (expensive). Hold between thresholds.';
            }
        }
        return;
    }

    var ind1 = document.getElementById('ind1_name');
    var p1 = document.querySelector('#period1-group input');
    var p2 = document.querySelector('#period2-group input');
    var isSweepOrHeatmap = (mode === 'sweep' || mode === 'heatmap');
    var label1 = ind1.value === 'price' ? 'Price' : ind1.value.toUpperCase() + (!isSweepOrHeatmap && p1.value ? '(' + p1.value + ')' : '');
    var label2 = ind2Val.toUpperCase() + (!isSweepOrHeatmap && p2.value ? '(' + p2.value + ')' : '');
    if (rev) {
        el.innerHTML = '<b>Sell</b> when <span id="explainer-ind1">' + label1 + '</span> crosses above <span id="explainer-ind2">' + label2 + '</span>. <b>Buy</b> when it crosses below.';
    } else {
        el.innerHTML = 'Buy when <span id="explainer-ind1">' + label1 + '</span> crosses above <span id="explainer-ind2">' + label2 + '</span>. Sell when it crosses below.';
    }
}
document.querySelector('#period1-group input').addEventListener('input', updateExplainer);
document.querySelector('#period2-group input').addEventListener('input', updateExplainer);
document.getElementById('ind2_name').addEventListener('change', function() { toggleFields(); enableBtn(); });
document.getElementById('buy_threshold').addEventListener('input', function() { updateExplainer(); enableBtn(); });
document.getElementById('sell_threshold').addEventListener('input', function() { updateExplainer(); enableBtn(); });
document.getElementById('osc_period').addEventListener('input', function() { updateExplainer(); enableBtn(); });
document.querySelector('select[name="sizing"]').addEventListener('change', function() { toggleFields(); enableBtn(); });
function setAllData() {
    var asset = document.getElementById('asset').value;
    document.getElementById('start_date').value = assetStarts[asset] || '';
}
function onAssetChange() {
    var asset = document.getElementById('asset').value;
    var vsAsset = document.getElementById('vs_asset').value;
    var startInput = document.getElementById('start_date');
    var start1 = assetStarts[asset] || '';
    if (vsAsset && assetStarts[vsAsset]) {
        var start2 = assetStarts[vsAsset];
        startInput.value = start1 > start2 ? start1 : start2;
    } else if (start1) {
        startInput.value = start1;
    }
}
var assetLogos = {{ asset_logos|tojson }};
function selectAsset(name, el) {
    document.getElementById('asset').value = name;
    var cards = document.querySelectorAll('.asset-card');
    for (var i = 0; i < cards.length; i++) cards[i].classList.remove('active');
    el.classList.add('active');
    // Update selected display
    var sel = document.getElementById('asset-selected');
    var logo = assetLogos[name];
    var displayName = name === name.toLowerCase() ? name.charAt(0).toUpperCase() + name.slice(1) : name;
    var logoHtml = logo
        ? '<img class="asset-selected-logo" src="/static/logos/' + logo + '" alt="' + name + '">'
        : '<div class="asset-card-placeholder" style="width:36px;height:36px;font-size:0.75em">' + name.slice(0,3).toUpperCase() + '</div>';
    sel.innerHTML = logoHtml +
        '<span class="asset-selected-name">' + displayName + '</span>' +
        '<svg class="asset-selected-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
    closeAssetModal();
    onAssetChange();
}
function openAssetModal() {
    document.getElementById('asset-modal-overlay').classList.add('open');
    var f = document.getElementById('asset-filter');
    f.value = '';
    filterAssetCards('', 'asset-modal-overlay');
    setTimeout(function() { f.focus(); }, 50);
}
function closeAssetModal() {
    document.getElementById('asset-modal-overlay').classList.remove('open');
}
function toggleVsAsset() {
    var checked = document.getElementById('vs-toggle').checked;
    var row = document.getElementById('vs-asset-row');
    if (checked) {
        row.classList.remove('hidden');
        if (!document.getElementById('vs_asset').value) {
            var defaultVs = 'TOTALES';
            var card = document.querySelector('.vs-asset-card[data-asset="' + defaultVs + '"]');
            if (card) {
                selectVsAsset(defaultVs, card);
            }
        }
    } else {
        row.classList.add('hidden');
        document.getElementById('vs_asset').value = '';
        onAssetChange();
    }
}
function selectVsAsset(name, el) {
    document.getElementById('vs_asset').value = name;
    var cards = document.querySelectorAll('.vs-asset-card');
    for (var i = 0; i < cards.length; i++) cards[i].classList.remove('active');
    el.classList.add('active');
    var sel = document.getElementById('vs-asset-selected');
    var logo = assetLogos[name];
    var displayName = name === name.toLowerCase() ? name.charAt(0).toUpperCase() + name.slice(1) : name;
    var logoHtml = logo
        ? '<img class="asset-selected-logo" src="/static/logos/' + logo + '" alt="' + name + '">'
        : '<div class="asset-card-placeholder" style="width:36px;height:36px;font-size:0.75em">' + name.slice(0,3).toUpperCase() + '</div>';
    sel.innerHTML = logoHtml +
        '<span class="asset-selected-name">' + displayName + '</span>' +
        '<svg class="asset-selected-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
    closeVsAssetModal();
    onAssetChange();
}
function openVsAssetModal() {
    document.getElementById('vs-asset-modal-overlay').classList.add('open');
    var f = document.getElementById('vs-asset-filter');
    f.value = '';
    filterAssetCards('', 'vs-asset-modal-overlay');
    setTimeout(function() { f.focus(); }, 50);
}
function closeVsAssetModal() {
    document.getElementById('vs-asset-modal-overlay').classList.remove('open');
}
function filterAssetCards(query, overlayId) {
    var overlay = document.getElementById(overlayId);
    var q = query.toLowerCase();
    var cards = overlay.querySelectorAll('.asset-card');
    for (var i = 0; i < cards.length; i++) {
        var name = cards[i].getAttribute('data-asset').toLowerCase();
        var ticker = (cards[i].getAttribute('data-ticker') || '').toLowerCase();
        cards[i].style.display = (!q || name.indexOf(q) !== -1 || ticker.indexOf(q) !== -1) ? '' : 'none';
    }
    var labels = overlay.querySelectorAll('.asset-section-label');
    for (var j = 0; j < labels.length; j++) {
        var grid = labels[j].nextElementSibling;
        if (grid && grid.classList.contains('asset-grid')) {
            var visible = grid.querySelectorAll('.asset-card:not([style*="display: none"])');
            labels[j].style.display = visible.length ? '' : 'none';
        }
    }
}

toggleFields();

// Re-enable Run button when any form parameter changes
(function() {
    var form = document.getElementById('form');
    var inputs = form.querySelectorAll('input, select');
    for (var i = 0; i < inputs.length; i++) {
        inputs[i].addEventListener('change', enableBtn);
        inputs[i].addEventListener('input', enableBtn);
    }
    // Also hook into mode cards and asset selection
    var origSelectMode = window.selectMode;
    window.selectMode = function(m, el) { origSelectMode(m, el); enableBtn(); };
    var origSelectAsset = window.selectAsset;
    window.selectAsset = function(n, el) { origSelectAsset(n, el); enableBtn(); };
})();

// Lightweight Charts tab switching



// Validation before submit
function validateForm() {
    var mode = document.getElementById('mode').value;
    var sigType = document.getElementById('signal_type').value;
    var errors = [];

    if (sigType === 'oscillator') {
        // Oscillator mode: no period/ind validation needed (defaults apply)
        return errors;
    }

    var ind1 = document.getElementById('ind1_name').value;
    var p2 = document.querySelector('#period2-group input').value.trim();
    var p1 = document.querySelector('#period1-group input').value.trim();

    // Period 2 required in backtest and sweep-lev modes
    if ((mode === 'backtest' || mode === 'sweep-lev') && !p2) {
        errors.push('Period 2 is required');
    }
    // Period 1 required when ind1 is not price (and not heatmap which sweeps it)
    if (ind1 !== 'price' && mode !== 'heatmap' && !p1) {
        errors.push('Period 1 is required when Indicator 1 is not Price');
    }
    return errors;
}

// AJAX form submission — only replace the results panel
var currentAbort = null;
var currentRequestId = null;

function resetBtn() {
    var btn = document.getElementById('btn');
    btn.classList.remove('btn-stop');
    btn.classList.remove('btn-done');
    btn.disabled = false;
    btn.textContent = 'Run Backtest';
    currentAbort = null;
    currentRequestId = null;
}
function enableBtn() {}

function toggleTimeframe() {
    var cb = document.getElementById('weekly-toggle');
    document.getElementById('timeframe').value = cb.checked ? 'weekly' : 'daily';
}

document.getElementById('btn').addEventListener('click', function(e) {
    if (currentAbort) {
        e.preventDefault();
        currentAbort.abort();
        if (currentRequestId) {
            fetch('/cancel', { method: 'POST', body: new URLSearchParams({ id: currentRequestId }) });
        }
        return;
    }
});

var _isPopstate = false;
document.getElementById('form').addEventListener('submit', function(e) {
    e.preventDefault();
    var btn = document.getElementById('btn');
    var panel = document.getElementById('results-panel');

    if (currentAbort) {
        currentAbort.abort();
        currentAbort = null;
        if (!_isPopstate) return;
    }

    var errors = validateForm();
    if (errors.length > 0) {
        panel.innerHTML = '<div class="placeholder" style="color:var(--accent)">' + errors.join('<br>') + '</div>';
        return;
    }

    currentAbort = new AbortController();
    currentRequestId = crypto.randomUUID();
    btn.textContent = 'Stop';
    btn.classList.add('btn-stop');
    panel.style.opacity = '0.5';
    panel.style.transition = 'opacity 0.2s ease';

    var formData = new FormData(this);
    formData.append('_request_id', currentRequestId);

    fetch('/backtester', { method: 'POST', body: formData, signal: currentAbort.signal })
        .then(function(resp) {
            if (resp.redirected) { window.location.href = resp.url; throw new Error('redirect'); }
            return resp.text();
        })
        .then(function(html) {
            var doc = new DOMParser().parseFromString(html, 'text/html');
            var newPanel = doc.getElementById('results-panel');
            if (newPanel) {
                // Lock panel height to prevent scroll jump during swap
                var oldHeight = panel.offsetHeight;
                panel.style.minHeight = oldHeight + 'px';
                var scrollY = window.scrollY;
                panel.innerHTML = newPanel.innerHTML;
                // Execute inline scripts (DOMParser doesn't run them)
                var scripts = panel.querySelectorAll('script');
                for (var si = 0; si < scripts.length; si++) {
                    var ns = document.createElement('script');
                    ns.textContent = scripts[si].textContent;
                    scripts[si].replaceWith(ns);
                }
                window.scrollTo(0, scrollY);
                panel.style.opacity = '1';
                lwChartLoaded = false;
                // Re-trigger fadeUp animation on chart image
                var img = panel.querySelector('.chart-img');
                if (img) {
                    img.style.animation = 'none';
                    img.offsetHeight;
                    img.style.animation = 'fadeUp 0.5s ease-out both';
                }
                // Release height lock after content settles
                requestAnimationFrame(function() { panel.style.minHeight = ''; });
            } else {
                panel.style.opacity = '1';
                panel.innerHTML = '<div class="placeholder">Error loading results. Try refreshing the page.</div>';
            }
            // Update URL with form params for browser back/forward navigation
            var qs = new URLSearchParams(formData);
            var viewParam = new URLSearchParams(window.location.search).get('view');
            if (viewParam) qs.set('view', viewParam);
            if (_isPopstate) {
                history.replaceState({ formParams: qs.toString() }, '', '?' + qs.toString());
            } else {
                history.pushState({ formParams: qs.toString() }, '', '?' + qs.toString());
            }
            _isPopstate = false;
            activateViewFromURL();
            resetBtn();
        })
        .catch(function(err) {
            panel.style.opacity = '1';
            if (err.name === 'AbortError') {
                // Don't show "Stopped" if there's already a loading spinner (auto-load in progress)
                if (!panel.querySelector('.spinner')) {
                    panel.innerHTML = '<div class="placeholder">Stopped</div>';
                }
            } else if (err.message !== 'redirect') {
                panel.innerHTML = '<div class="placeholder">Error: ' + err.message + '</div>';
            }
            resetBtn();
        });
});

// Auto-submit on page load if URL has query params (e.g. from "View Best in Backtest" button)
// This replaces the old initial-load block — the form submit handler above handles everything.
{% if not chart %}
(function() {
    var params = new URLSearchParams(window.location.search);
    if (params.has('mode') && params.has('asset')) {
        var panel = document.getElementById('results-panel');
        panel.innerHTML = '<div class="placeholder" style="display:flex;align-items:center;gap:10px"><span class="spinner" style="width:20px;height:20px;border:2px solid var(--text-dim);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;display:inline-block"></span> Running backtest...</div>';
        _isPopstate = true;
        document.getElementById('form').dispatchEvent(new Event('submit', { cancelable: true }));
    }
})();
{% endif %}

// Browser back/forward: restore form fields from URL and re-submit
window.addEventListener('popstate', function(e) {
    var params = new URLSearchParams(window.location.search);
    if (params.toString() === '') return;
    var form = document.getElementById('form');

    // Restore all form fields from URL params
    params.forEach(function(value, key) {
        var el = form.querySelector('[name="' + key + '"]');
        if (!el) return;
        if (el.type === 'checkbox') {
            el.checked = (value === 'on' || value === 'true' || value === '1');
        } else {
            el.value = value;
        }
    });

    // Restore ind2 dropdown: if oscillator, set to osc_<name> prefix
    var sigType = params.get('signal_type');
    var oscName = params.get('osc_name');
    if (sigType === 'oscillator' && oscName) {
        document.getElementById('ind2_name').value = 'osc_' + oscName;
    }

    // Restore mode card active state
    var mode = params.get('mode');
    if (mode) {
        var cards = document.querySelectorAll('.mode-card');
        for (var i = 0; i < cards.length; i++) {
            cards[i].classList.remove('active');
            if (cards[i].getAttribute('data-mode') === mode) cards[i].classList.add('active');
        }
    }

    // Update asset button display
    var assetName = params.get('asset');
    if (assetName) {
        var selectedBtn = document.getElementById('asset-selected');
        var assetInput = document.getElementById('asset');
        if (assetInput) assetInput.value = assetName;
        if (selectedBtn) {
            var logo = assetLogos[assetName];
            selectedBtn.innerHTML = (logo ? '<img class="asset-selected-logo" src="/static/logos/' + logo + '" alt="' + assetName + '">' : '') +
                '<span>' + assetName.charAt(0).toUpperCase() + assetName.slice(1) + '</span>' +
                '<svg width="10" height="6" viewBox="0 0 10 6" style="margin-left:6px;opacity:0.5"><path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>';
        }
    }

    toggleFields();
    // Re-submit form to load the backtest (flag to use replaceState, not pushState)
    _isPopstate = true;
    form.dispatchEvent(new Event('submit', { cancelable: true }));
});

// --- Save / Publish / Like / Comment functionality ---
var _currentShortCode = null;

function _getChartThumbnail() {
    var el = document.getElementById('equity-thumbnail');
    return el ? el.value : '';
}

function _mergeBestParams(params) {
    var bp = document.getElementById('best-params');
    if (!bp) return;
    if (!params.period1 && bp.dataset.ind1Period) params.period1 = bp.dataset.ind1Period;
    if (!params.period2 && bp.dataset.ind2Period) params.period2 = bp.dataset.ind2Period;
}
function viewBestInBacktest() {
    var form = document.getElementById('form');
    var fd = new FormData(form);
    var bp = document.getElementById('best-params');
    var params = new URLSearchParams();
    params.set('mode', 'backtest');
    params.set('asset', fd.get('asset'));
    if (fd.get('vs_asset')) params.set('vs_asset', fd.get('vs_asset'));
    params.set('ind1_name', fd.get('ind1_name'));
    params.set('ind2_name', fd.get('ind2_name'));
    // Use best periods from optimization result (skip empty/None values)
    var p1 = (bp && bp.dataset.ind1Period && bp.dataset.ind1Period !== 'None') ? bp.dataset.ind1Period : fd.get('period1');
    if (p1 && p1 !== 'None' && p1 !== '') params.set('period1', p1);
    var p2 = (bp && bp.dataset.ind2Period && bp.dataset.ind2Period !== 'None') ? bp.dataset.ind2Period : fd.get('period2');
    if (p2 && p2 !== 'None' && p2 !== '') params.set('period2', p2);
    params.set('exposure', fd.get('exposure'));
    params.set('fee', fd.get('fee'));
    params.set('start_date', fd.get('start_date'));
    params.set('end_date', fd.get('end_date'));
    params.set('sizing', fd.get('sizing'));
    params.set('financing_rate', fd.get('financing_rate'));
    params.set('lev_mode', fd.get('lev_mode'));
    if (fd.get('reverse')) params.set('reverse', '1');
    if (fd.get('timeframe')) params.set('timeframe', fd.get('timeframe'));
    // For lev sweep, use best leverage values
    if (bp && bp.dataset.bestLongLev) params.set('long_leverage', bp.dataset.bestLongLev);
    else params.set('long_leverage', fd.get('long_leverage'));
    if (bp && bp.dataset.bestShortLev) params.set('short_leverage', bp.dataset.bestShortLev);
    else params.set('short_leverage', fd.get('short_leverage'));
    window.location.href = '/backtester?' + params.toString();
}
function saveBacktest() {
    var btn = document.getElementById('save-btn');
    btn.textContent = 'Saving...';
    btn.disabled = true;
    var formData = new FormData(document.getElementById('form'));
    var params = {};
    formData.forEach(function(v, k) { params[k] = v; });
    _mergeBestParams(params);
    var qs = new URLSearchParams(params).toString();
    var resultsHtml = document.getElementById('results-panel').innerHTML;
    var thumb = _getChartThumbnail();
    fetch('/api/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({params: JSON.stringify(params), query_string: qs, cached_html: resultsHtml, thumbnail: thumb})
    }).then(function(r) {
        if (!r.ok) throw new Error('Server error: ' + r.status);
        return r.json();
    }).then(function(data) {
        btn.innerHTML = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8l4 4 8-8"/></svg> Saved!';
        _currentShortCode = data.short_code;
        setTimeout(function() {
            btn.innerHTML = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v10h10V6l-3-3H3z"/><path d="M5 3v3h4V3"/><path d="M5 9h6v4H5z"/></svg> Save';
            btn.disabled = false;
        }, 2000);
    }).catch(function(e) { btn.textContent = 'Save'; btn.disabled = false; _swal.fire({icon:'error', title:'Save failed', text:e.message}); });
}

function openPublishModal() {
    var nameInput = document.getElementById('publish-display-name');
    // Fetch saved display name (or email prefix fallback)
    fetch('/api/display-name').then(function(r) { return r.json(); }).then(function(data) {
        nameInput.value = data.display_name || '';
        if (data.is_custom) {
            nameInput.readOnly = true;
            nameInput.style.opacity = '0.8';
        } else {
            // First time — make editable
            nameInput.readOnly = false;
            nameInput.style.opacity = '1';
        }
    });
    document.getElementById('publish-modal-overlay').classList.add('open');
    document.getElementById('publish-title').focus();
}
function toggleUsernameEdit() {
    var nameInput = document.getElementById('publish-display-name');
    nameInput.readOnly = false;
    nameInput.style.opacity = '1';
    nameInput.focus();
    nameInput.select();
}
function closePublishModal() {
    document.getElementById('publish-modal-overlay').classList.remove('open');
}

function publishBacktest(visibility) {
    var displayName = document.getElementById('publish-display-name').value.trim();
    var title = document.getElementById('publish-title').value.trim();
    var desc = document.getElementById('publish-desc').value.trim();
    if (!displayName) { _swal.fire({icon:'warning', title:'Username required', text:'Please enter a public username.'}); document.getElementById('publish-display-name').focus(); return; }
    if (!title) { _swal.fire({icon:'warning', title:'Title required', text:'Please enter a title for your backtest.'}); return; }
    if (!desc) { _swal.fire({icon:'warning', title:'Description required', text:'Please add a description.'}); return; }
    var formData = new FormData(document.getElementById('form'));
    var params = {};
    formData.forEach(function(v, k) { params[k] = v; });
    _mergeBestParams(params);
    var qs = new URLSearchParams(params).toString();
    var resultsHtml = document.getElementById('results-panel').innerHTML;
    var thumb = _getChartThumbnail();
    fetch('/api/publish', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            params: JSON.stringify(params), query_string: qs, cached_html: resultsHtml,
            title: title, description: desc, visibility: visibility || 'community',
            display_name: displayName, thumbnail: thumb
        })
    }).then(function(r) {
        if (!r.ok) throw new Error('Server error: ' + r.status + ' ' + r.statusText);
        return r.json();
    }).then(function(data) {
        if (data.error) { _swal.fire({icon:'error', title:'Error', text:data.error}); return; }
        closePublishModal();
        var dest = (visibility === 'featured') ? '/featured' : '/community';
        _swal.fire({icon:'success', title:'Published!', text:'Your backtest is now live.', timer:2000, showConfirmButton:false}).then(function() {
            window.location.href = dest;
        });
    }).catch(function(e) { _swal.fire({icon:'error', title:'Publish failed', text:e.message}); });
}

function copyShortLink() {
    if (!_currentShortCode) return;
    var url = location.origin + '/s/' + _currentShortCode;
    navigator.clipboard.writeText(url).then(function() {
        var btn = document.getElementById('copy-link-btn');
        var orig = btn.innerHTML;
        btn.innerHTML = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8l4 4 8-8"/></svg> Copied!';
        setTimeout(function() { btn.innerHTML = orig; }, 2000);
    });
}

function toggleLike(backtestId, btn) {
    fetch('/api/backtest/' + backtestId + '/like', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.querySelector('.like-count').textContent = data.likes_count;
        if (data.liked) {
            btn.classList.add('liked');
            btn.classList.add('just-liked');
            setTimeout(function() { btn.classList.remove('just-liked'); }, 500);
        } else {
            btn.classList.remove('liked');
        }
    });
}

function submitComment(backtestId, parentId, textareaSrcId) {
    var textareaId = textareaSrcId ? 'reply-' + textareaSrcId : (parentId ? 'reply-' + parentId : 'comment-body');
    var body = document.getElementById(textareaId).value.trim();
    if (!body) return;
    fetch('/api/backtest/' + backtestId + '/comment', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({body: body, parent_id: parentId || null})
    }).then(function(r) { return r.json(); })
    .then(function() { location.reload(); });
}

function deleteComment(commentId) {
    _swal.fire({
        title: 'Delete this comment?', icon: 'warning',
        showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/comment/' + commentId, { method: 'DELETE' })
        .then(function() { location.reload(); });
    });
}

function showReplyForm(commentId) {
    var el = document.getElementById('reply-form-' + commentId);
    if (el) el.classList.toggle('hidden');
}

function deleteBacktest(backtestId) {
    _swal.fire({
        title: 'Delete this backtest?', icon: 'warning',
        showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/backtest/' + backtestId, { method: 'DELETE' })
        .then(function() { location.reload(); });
    });
}

function featureBacktest(backtestId) {
    fetch('/api/backtest/' + backtestId + '/feature', { method: 'POST' })
    .then(function() { location.reload(); });
}

function openEditModal(backtestId, title, desc) {
    document.getElementById('edit-bt-id').value = backtestId;
    document.getElementById('edit-title').value = title || '';
    document.getElementById('edit-desc').value = desc || '';
    document.getElementById('edit-modal-overlay').classList.add('open');
    document.getElementById('edit-title').focus();
}
function closeEditModal() {
    document.getElementById('edit-modal-overlay').classList.remove('open');
}
function saveEdit() {
    var btId = document.getElementById('edit-bt-id').value;
    var title = document.getElementById('edit-title').value.trim();
    var desc = document.getElementById('edit-desc').value.trim();
    fetch('/api/backtest/' + btId, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, description: desc})
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.error) { _swal.fire({icon:'error', title:'Error', text:data.error}); return; }
        closeEditModal();
        location.reload();
    }).catch(function() { _swal.fire({icon:'error', title:'Failed to save'}); });
}
</script>

<!-- Publish Modal -->
<div class="publish-modal-overlay" id="publish-modal-overlay">
    <div class="publish-modal">
        <button class="close-btn" onclick="closePublishModal()">&times;</button>
        <h3>Publish to Community</h3>
        <label for="publish-display-name">Public Username</label>
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:14px">
            <input type="text" id="publish-display-name" placeholder="How should your name appear?" maxlength="40" style="margin-bottom:0;flex:1" readonly>
            <button type="button" class="action-btn" id="edit-username-btn" onclick="toggleUsernameEdit()" title="Edit username" style="padding:8px 10px;flex-shrink:0">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M11.5 1.5l3 3L5 14H2v-3L11.5 1.5z"/></svg>
            </button>
        </div>
        <label for="publish-title">Title</label>
        <input type="text" id="publish-title" placeholder="e.g. Bitcoin EMA(20)/SMA(100) Crossover" maxlength="120">
        <label for="publish-desc">Description</label>
        <textarea id="publish-desc" placeholder="Describe your findings, strategy rationale, or key takeaways..."></textarea>
        <div class="publish-modal-actions">
            <button class="action-btn" onclick="closePublishModal()">Cancel</button>
            <button class="action-btn primary" onclick="publishBacktest('community')">Publish</button>
            {% if session.get('email') == '""" + db.ADMIN_EMAIL + """' %}
            <button class="action-btn" style="border-color:var(--green);color:var(--green)" onclick="publishBacktest('featured')">Publish as Featured</button>
            {% endif %}
        </div>
    </div>
</div>

<!-- Edit Backtest Modal -->
<div class="publish-modal-overlay" id="edit-modal-overlay">
    <div class="publish-modal">
        <button class="close-btn" onclick="closeEditModal()">&times;</button>
        <h3>Edit Backtest</h3>
        <input type="hidden" id="edit-bt-id">
        <label for="edit-title">Title</label>
        <input type="text" id="edit-title" maxlength="120">
        <label for="edit-desc">Description</label>
        <textarea id="edit-desc"></textarea>
        <div class="publish-modal-actions">
            <button class="action-btn" onclick="closeEditModal()">Cancel</button>
            <button class="action-btn primary" onclick="saveEdit()">Save Changes</button>
        </div>
    </div>
</div>
<!-- Denominator asset picker modal -->
<div class="asset-modal-overlay" id="vs-asset-modal-overlay" onclick="closeVsAssetModal()">
    <div class="asset-modal" onclick="event.stopPropagation()">
        <div class="asset-modal-header">
            <span>Select Denominator Asset</span>
            <input type="text" class="asset-modal-filter" id="vs-asset-filter" placeholder="Filter assets..." oninput="filterAssetCards(this.value, 'vs-asset-modal-overlay')">
            <button type="button" class="asset-modal-close" onclick="closeVsAssetModal()">&times;</button>
        </div>
        <div class="asset-grid">
            {% for a in priority_assets %}
            <div class="vs-asset-card asset-card {{ 'active' if p.vs_asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectVsAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
            {% for a in other_assets %}
            <div class="vs-asset-card asset-card {{ 'active' if p.vs_asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectVsAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% if crypto_agg_assets %}
        <div class="asset-section-label">Crypto Aggregates</div>
        <div class="asset-grid">
            {% for a in crypto_agg_assets %}
            <div class="vs-asset-card asset-card {{ 'active' if p.vs_asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectVsAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if stock_assets %}
        <div class="asset-section-label">Stocks</div>
        <div class="asset-grid">
            {% for a in stock_assets %}
            <div class="vs-asset-card asset-card {{ 'active' if p.vs_asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectVsAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if metal_assets %}
        <div class="asset-section-label">Precious Metals</div>
        <div class="asset-grid">
            {% for a in metal_assets %}
            <div class="vs-asset-card asset-card {{ 'active' if p.vs_asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectVsAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if index_assets %}
        <div class="asset-section-label">Indices</div>
        <div class="asset-grid">
            {% for a in index_assets %}
            <div class="vs-asset-card asset-card {{ 'active' if p.vs_asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectVsAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if commodity_assets %}
        <div class="asset-section-label">Commodities</div>
        <div class="asset-grid">
            {% for a in commodity_assets %}
            <div class="vs-asset-card asset-card {{ 'active' if p.vs_asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectVsAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
    </div>
</div>
<!-- Asset picker modal -->
<div class="asset-modal-overlay" id="asset-modal-overlay" onclick="closeAssetModal()">
    <div class="asset-modal" onclick="event.stopPropagation()">
        <div class="asset-modal-header">
            <span>Select Asset</span>
            <input type="text" class="asset-modal-filter" id="asset-filter" placeholder="Filter assets..." oninput="filterAssetCards(this.value, 'asset-modal-overlay')">
            <button type="button" class="asset-modal-close" onclick="closeAssetModal()">&times;</button>
        </div>
        <div class="asset-grid">
            {% for a in priority_assets %}
            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
            {% for a in other_assets %}
            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% if crypto_agg_assets %}
        <div class="asset-section-label">Crypto Aggregates</div>
        <div class="asset-grid">
            {% for a in crypto_agg_assets %}
            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if stock_assets %}
        <div class="asset-section-label">Stocks</div>
        <div class="asset-grid">
            {% for a in stock_assets %}
            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if metal_assets %}
        <div class="asset-section-label">Precious Metals</div>
        <div class="asset-grid">
            {% for a in metal_assets %}
            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if index_assets %}
        <div class="asset-section-label">Indices</div>
        <div class="asset-grid">
            {% for a in index_assets %}
            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        {% if commodity_assets %}
        <div class="asset-section-label">Commodities</div>
        <div class="asset-grid">
            {% for a in commodity_assets %}
            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" data-ticker="{{ asset_tickers.get(a, '') }}" onclick="selectAsset('{{ a }}', this)">
                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                <span class="asset-card-label">{{ a|capitalize if a == a|lower else a }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
    </div>
</div>
</body>
</html>
"""


class Params:
    """Hold form parameters with defaults."""
    def __init__(self, form=None):
        if form:
            _raw_asset = form.get("asset", "").strip()
            self.asset = _resolve_asset(_raw_asset) or _raw_asset or DEFAULT_ASSET
            self.vs_asset = _resolve_asset(form.get("vs_asset", "").strip())
            self.mode = form.get("mode", "sweep")
            self.signal_type = form.get("signal_type", "crossover")
            self.ind1_name = form.get("ind1_name", "price")
            p1_val = form.get("period1", "").strip()
            self.ind1_period = int(p1_val) if p1_val and p1_val not in ("None", "null") else None
            self.ind2_name = form.get("ind2_name", "sma")
            p2_val = form.get("period2", "").strip()
            self.ind2_period = int(p2_val) if p2_val else None
            self.range_min = int(form.get("range_min", 2))
            self.range_max = int(form.get("range_max", 200))
            self.step = int(form.get("step", 5))
            self.exposure = form.get("exposure", "long-cash")
            if self.mode == "sweep-lev":
                self.exposure = "long-short"
            self.fee = float(form.get("fee", 0.05))
            self.long_leverage = float(form.get("long_leverage", 1))
            self.short_leverage = float(form.get("short_leverage", 1))
            self.lev_mode = form.get("lev_mode", "optimal")
            self.lev_min = float(form.get("lev_min", 0.25))
            self.lev_max = float(form.get("lev_max", 10))
            self.lev_step = float(form.get("lev_step", 0.25))
            self.initial_cash = float(form.get("initial_cash", 10000))
            self.start_date = form.get("start_date", "").strip()
            self.end_date = form.get("end_date", "").strip()
            self.reverse = bool(form.get("reverse"))
            self.sizing = form.get("sizing", "compound")
            self.financing_rate = float(form.get("financing_rate", 0))
            self.timeframe = form.get("timeframe", "daily")
            # Oscillator params
            self.osc_name = form.get("osc_name", "rsi")
            osc_p = form.get("osc_period", "").strip()
            self.osc_period = int(osc_p) if osc_p else None
            self.buy_threshold = float(form.get("buy_threshold", bt.OSCILLATORS.get(self.osc_name, {}).get("buy_threshold", 30)))
            self.sell_threshold = float(form.get("sell_threshold", bt.OSCILLATORS.get(self.osc_name, {}).get("sell_threshold", 70)))
            self.forward_days = int(form.get("forward_days", 365))
            # DCA params
            self.dca_frequency = form.get("dca_frequency", "daily")
            self.dca_amount = float(form.get("dca_amount", 100))
            self.dca_signal_type = form.get("dca_signal_type", "oscillator")
            self.dca_signal_name = form.get("dca_signal_name", "rsi")
            dca_sp = form.get("dca_signal_period", "").strip()
            if dca_sp:
                self.dca_signal_period = float(dca_sp) if self.dca_signal_type == "ath_drawdown" else int(dca_sp)
            else:
                self.dca_signal_period = 5.0 if self.dca_signal_type == "ath_drawdown" else None
            self.dca_max_multiplier = float(form.get("dca_max_multiplier", 3.0))
            self.dca_show_lump_sum = False
            self.dca_reverse = bool(form.get("dca_reverse"))
            self.dca_sweep_param = form.get("dca_sweep_param", "multiplier")
            self.theme = form.get("theme", "dark")
            if self.theme not in ("dark", "light"):
                self.theme = "dark"
            # Rolling window params
            self.window_size = int(form.get("window_size", 4))
            self.step_size = float(form.get("step_size", 0.5))
            self.rolling_metric = form.get("rolling_metric", "total_return")
        else:
            self.asset = DEFAULT_ASSET
            self.vs_asset = None
            self.mode = "backtest"
            self.signal_type = "crossover"
            self.ind1_name = "price"
            self.ind1_period = None
            self.ind2_name = "sma"
            self.ind2_period = 44
            self.range_min = 2
            self.range_max = 200
            self.step = 5
            self.exposure = "long-cash"
            self.fee = 0.05
            self.long_leverage = 1
            self.short_leverage = 1
            self.lev_mode = "optimal"
            self.lev_min = 0.25
            self.lev_max = 10
            self.lev_step = 0.25
            self.initial_cash = 10000
            self.start_date = "2018-01-01"
            self.end_date = str(ASSETS[DEFAULT_ASSET].index[-1].date())
            self.reverse = False
            self.sizing = "compound"
            self.financing_rate = 11
            self.timeframe = "daily"
            # Oscillator defaults
            self.osc_name = "rsi"
            self.osc_period = None
            self.buy_threshold = 30
            self.sell_threshold = 70
            self.forward_days = 365
            # DCA defaults
            self.dca_frequency = "daily"
            self.dca_amount = 100
            self.dca_signal_type = "oscillator"
            self.dca_signal_name = "rsi"
            self.dca_signal_period = None
            self.dca_max_multiplier = 3.0
            self.dca_show_lump_sum = False
            self.dca_reverse = False
            self.dca_sweep_param = "multiplier"
            # Rolling window defaults
            self.window_size = 4
            self.step_size = 0.5
            self.rolling_metric = "total_return"


# Load data once at startup
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Use PostgreSQL if available, fall back to CSV files
ASSETS = {}
ASSET_STARTS = {}
_USE_PRICE_DB = bool(os.environ.get("PRICE_DB_URL"))
if _USE_PRICE_DB:
    price_db.init_db()
    for _name, _df in price_db.get_all_assets().items():
        ASSETS[_name] = _df
        ASSET_STARTS[_name] = str(_df.index[0].date())
else:
    for _fname in sorted(os.listdir(DATA_DIR)):
        if _fname.endswith(".csv"):
            _name = _fname.replace(".csv", "")
            _df = bt.load_data(os.path.join(DATA_DIR, _fname))
            ASSETS[_name] = _df
            ASSET_STARTS[_name] = str(_df.index[0].date())
ASSET_NAMES = sorted(ASSETS.keys())
# Case-insensitive lookup: maps lowercased name -> actual ASSETS key
_ASSET_KEY_MAP = {k.lower(): k for k in ASSETS}


def _resolve_asset(name):
    """Resolve an asset name to its canonical ASSETS key (case-insensitive)."""
    if not name:
        return None
    if name in ASSETS:
        return name
    return _ASSET_KEY_MAP.get(name.lower())


_PRIORITY_ORDER = ["bitcoin", "ethereum", "solana"]
_CRYPTO_AGG_ASSETS = set()
_STOCK_ASSETS = {"Apple", "Microsoft", "Amazon", "Alphabet", "Tesla", "Nvidia", "Meta", "Netflix", "Coinbase", "Strategy"}
_INDEX_ASSETS = {"Dax", "Dow Jones", "Hang Seng", "Nasdaq100", "SP500"}
_METAL_ASSETS = {"Gold", "Silver", "Palladium"}
_COMMODITY_ASSETS = {"Oil (Brent)", "Oil (Wti)"}

# Load custom category assignments (from uploaded assets)
_CATEGORIES_FILE = os.path.join(DATA_DIR, "_categories.json")
if os.path.exists(_CATEGORIES_FILE):
    with open(_CATEGORIES_FILE) as _f:
        for _asset, _cat in json.load(_f).items():
            if _cat == 'crypto_agg': _CRYPTO_AGG_ASSETS.add(_asset)
            elif _cat == 'stock': _STOCK_ASSETS.add(_asset)
            elif _cat == 'index': _INDEX_ASSETS.add(_asset)
            elif _cat == 'metal': _METAL_ASSETS.add(_asset)
            elif _cat == 'commodity': _COMMODITY_ASSETS.add(_asset)

PRIORITY_ASSETS = [a for a in _PRIORITY_ORDER if a in ASSETS]
OTHER_ASSETS = [a for a in ASSET_NAMES if a not in _PRIORITY_ORDER and a not in _CRYPTO_AGG_ASSETS and a not in _STOCK_ASSETS and a not in _INDEX_ASSETS and a not in _METAL_ASSETS and a not in _COMMODITY_ASSETS]
CRYPTO_AGG_ASSETS = [a for a in ASSET_NAMES if a in _CRYPTO_AGG_ASSETS]
STOCK_ASSETS = [a for a in ASSET_NAMES if a in _STOCK_ASSETS]
INDEX_ASSETS = [a for a in ASSET_NAMES if a in _INDEX_ASSETS]
METAL_ASSETS = [a for a in ASSET_NAMES if a in _METAL_ASSETS]
COMMODITY_ASSETS = [a for a in ASSET_NAMES if a in _COMMODITY_ASSETS]
DEFAULT_ASSET = "bitcoin" if "bitcoin" in ASSETS else ASSET_NAMES[0]
ASSET_LOGOS = {
    "bitcoin": "bitcoin-btc-logo.png", "ethereum": "ethereum-eth-logo.png",
    "solana": "solana-sol-logo.png", "XRP": "xrp-xrp-logo.png",
    "BNB": "bnb-bnb-logo.png", "Cardano": "cardano-ada-logo.png",
    "Chainlink": "chainlink-link-logo.png", "Dogecoin": "dogecoin-doge-logo.png",
    "Monero": "monero-xmr-logo.png", "Bitcoin Cash": "bitcoin-cash-bch-logo.png",
    "Hyperliquid": "hyperliquid-logo.png",
    "Bittensor": "bittensor-tao-logo.png",
    "Dax": "dax-logo.svg", "Dow Jones": "dowjones-logo.svg",
    "Hang Seng": "hangseng-logo.svg", "Nasdaq100": "nasdaq-logo.svg",
    "SP500": "sp500-logo.svg",
    "Gold": "gold-logo.svg", "Silver": "silver-logo.svg", "Palladium": "palladium-logo.svg",
    "Oil (Brent)": "oil-brent-logo.svg", "Oil (Wti)": "oil-wti-logo.svg",
    "Apple": "apple-logo.png", "Microsoft": "microsoft-logo.png", "Amazon": "amazon-logo.png",
    "Alphabet": "alphabet-logo.png", "Tesla": "tesla-logo.png", "Nvidia": "nvidia-logo.png",
    "Meta": "meta-logo.png", "Netflix": "netflix-logo.png", "Coinbase": "coinbase-logo.png",
    "Strategy": "strategy-logo.png",
}

# Load custom logo assignments (from uploaded assets)
_LOGOS_FILE = os.path.join(DATA_DIR, "_logos.json")
if os.path.exists(_LOGOS_FILE):
    with open(_LOGOS_FILE) as _f:
        ASSET_LOGOS.update(json.load(_f))

# ---------------------------------------------------------------------------
# Live price: API usage tracking + in-memory price cache
# ---------------------------------------------------------------------------
_API_USAGE_FILE = os.path.join(DATA_DIR, "_api_usage.json")
_API_USAGE_LIMIT = 2500  # 25% of CoinGecko free tier (10,000/month)
_api_usage = {"month": "", "calls": 0}
_api_usage_lock = threading.Lock()
_api_usage_dirty = 0  # flush to disk every 10 increments

if os.path.exists(_API_USAGE_FILE):
    try:
        with open(_API_USAGE_FILE) as _f:
            _api_usage.update(json.load(_f))
    except Exception:
        pass


def _api_usage_flush():
    """Write usage to disk."""
    try:
        with open(_API_USAGE_FILE, "w") as f:
            json.dump(_api_usage, f)
    except Exception:
        pass


def _api_usage_increment():
    """Bump the API call counter, reset if month changed."""
    global _api_usage_dirty
    cur_month = time.strftime("%Y-%m")
    with _api_usage_lock:
        if _api_usage["month"] != cur_month:
            _api_usage["month"] = cur_month
            _api_usage["calls"] = 0
        _api_usage["calls"] += 1
        _api_usage_dirty += 1
        if _api_usage_dirty >= 10:
            _api_usage_dirty = 0
            _api_usage_flush()


def _api_usage_get():
    """Return usage stats dict."""
    cur_month = time.strftime("%Y-%m")
    with _api_usage_lock:
        if _api_usage["month"] != cur_month:
            _api_usage["month"] = cur_month
            _api_usage["calls"] = 0
        calls = _api_usage["calls"]
    pct = round(calls / _API_USAGE_LIMIT * 100, 1) if _API_USAGE_LIMIT else 0
    return {"month": cur_month, "calls": calls, "limit": _API_USAGE_LIMIT, "pct": pct}


def _api_usage_ok():
    """Return True if we haven't exceeded the usage limit."""
    return _api_usage_get()["calls"] < _API_USAGE_LIMIT


# In-memory live price cache: {asset_name: {"price": float, "time": str, "ts": float}}
_price_now_cache = {}
_PRICE_CACHE_TTL = 60  # seconds

# Asset metadata lookup (source + source_id) — built from DB or hardcoded
_ASSET_META = {}
ASSET_TICKERS = {}  # {asset_name: ticker_symbol}
if _USE_PRICE_DB:
    try:
        for _m in price_db.get_all_asset_metadata():
            _ASSET_META[_m["name"]] = {"source": _m["source"], "source_id": _m["source_id"]}
            if _m.get("ticker"):
                ASSET_TICKERS[_m["name"]] = _m["ticker"]
    except Exception:
        pass

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")


def _fetch_live_price(asset_name):
    """Fetch current price for an asset. Returns {"price": float, "time": str} or None."""
    meta = _ASSET_META.get(asset_name)
    if not meta or not meta.get("source") or not meta.get("source_id"):
        return None

    source = meta["source"]
    source_id = meta["source_id"]

    try:
        if source == "coingecko":
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {"ids": source_id, "vs_currencies": "usd"}
            headers = {"User-Agent": "BacktestingEngine/1.0"}
            if COINGECKO_API_KEY:
                headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
            resp = req_lib.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                price = data.get(source_id, {}).get("usd")
                if price is not None:
                    _api_usage_increment()
                    return {"price": float(price), "time": time.strftime("%Y-%m-%d")}
        elif source == "yfinance":
            import yfinance as yf
            ticker = yf.Ticker(source_id)
            price = ticker.fast_info.get("lastPrice")
            if price is not None:
                return {"price": float(price), "time": time.strftime("%Y-%m-%d")}
    except Exception:
        pass
    return None


def _rebuild_asset_lists():
    """Rebuild all asset name/category lists from current ASSETS dict."""
    global ASSET_NAMES, PRIORITY_ASSETS, OTHER_ASSETS, CRYPTO_AGG_ASSETS, STOCK_ASSETS, INDEX_ASSETS, METAL_ASSETS, COMMODITY_ASSETS, _ASSET_KEY_MAP
    ASSET_NAMES = sorted(ASSETS.keys())
    _ASSET_KEY_MAP = {k.lower(): k for k in ASSETS}
    PRIORITY_ASSETS = [a for a in _PRIORITY_ORDER if a in ASSETS]
    OTHER_ASSETS = [a for a in ASSET_NAMES if a not in _PRIORITY_ORDER and a not in _CRYPTO_AGG_ASSETS and a not in _STOCK_ASSETS and a not in _INDEX_ASSETS and a not in _METAL_ASSETS and a not in _COMMODITY_ASSETS]
    CRYPTO_AGG_ASSETS = [a for a in ASSET_NAMES if a in _CRYPTO_AGG_ASSETS]
    STOCK_ASSETS = [a for a in ASSET_NAMES if a in _STOCK_ASSETS]
    INDEX_ASSETS = [a for a in ASSET_NAMES if a in _INDEX_ASSETS]
    METAL_ASSETS = [a for a in ASSET_NAMES if a in _METAL_ASSETS]
    COMMODITY_ASSETS = [a for a in ASSET_NAMES if a in _COMMODITY_ASSETS]


def _download_logo(asset_name, asset_type):
    """Try to download a logo for the asset. Returns filename or None."""
    import urllib.request
    import urllib.parse
    logos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logos")
    safe_name = re.sub(r'[^a-zA-Z0-9]', '-', asset_name.lower()).strip('-')
    filename = f"{safe_name}-logo.png"
    filepath = os.path.join(logos_dir, filename)

    try:
        if asset_type == 'crypto':
            # Try CoinGecko search API
            search_url = f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(asset_name)}"
            req = urllib.request.Request(search_url, headers={'User-Agent': 'BacktestingEngine/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                coins = data.get('coins', [])
                if coins:
                    img_url = coins[0].get('large') or coins[0].get('thumb')
                    if img_url:
                        img_req = urllib.request.Request(img_url, headers={'User-Agent': 'BacktestingEngine/1.0'})
                        with urllib.request.urlopen(img_req, timeout=10) as img_resp:
                            with open(filepath, 'wb') as f:
                                f.write(img_resp.read())
                        return filename
        else:
            # Try Clearbit logo API with company name as domain
            domain_guess = re.sub(r'[^a-zA-Z0-9]', '', asset_name.lower()) + '.com'
            logo_url = f"https://logo.clearbit.com/{domain_guess}"
            req = urllib.request.Request(logo_url, headers={'User-Agent': 'BacktestingEngine/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                with open(filepath, 'wb') as f:
                    f.write(resp.read())
                return filename
    except Exception:
        pass
    return None


def _save_categories_file():
    """Persist custom category assignments to JSON file."""
    # Only save assets not in the original hardcoded sets
    _ORIG_STOCK = {"Apple", "Microsoft", "Amazon", "Alphabet", "Tesla", "Nvidia", "Meta", "Netflix", "Coinbase", "Strategy"}
    _ORIG_INDEX = {"Dax", "Dow Jones", "Hang Seng", "Nasdaq100", "SP500"}
    _ORIG_METAL = {"Gold", "Silver", "Palladium"}
    _ORIG_COMMODITY = {"Oil (Brent)", "Oil (Wti)"}
    custom = {}
    for a in _CRYPTO_AGG_ASSETS:
        custom[a] = 'crypto_agg'
    for a in _STOCK_ASSETS - _ORIG_STOCK:
        custom[a] = 'stock'
    for a in _INDEX_ASSETS - _ORIG_INDEX:
        custom[a] = 'index'
    for a in _METAL_ASSETS - _ORIG_METAL:
        custom[a] = 'metal'
    for a in _COMMODITY_ASSETS - _ORIG_COMMODITY:
        custom[a] = 'commodity'
    with open(_CATEGORIES_FILE, 'w') as f:
        json.dump(custom, f, indent=2)


def _save_logos_file():
    """Persist custom logo assignments to JSON file."""
    _ORIG_LOGOS = {
        "bitcoin", "ethereum", "solana", "XRP", "BNB", "Cardano", "Chainlink", "Dogecoin",
        "Monero", "Bitcoin Cash", "Hyperliquid", "Bittensor", "Dax", "Dow Jones", "Hang Seng",
        "Nasdaq100", "SP500", "Gold", "Silver", "Palladium", "Oil (Brent)", "Oil (Wti)",
        "Apple", "Microsoft", "Amazon", "Alphabet", "Tesla", "Nvidia", "Meta", "Netflix",
        "Coinbase", "Strategy",
    }
    custom = {k: v for k, v in ASSET_LOGOS.items() if k not in _ORIG_LOGOS}
    with open(_LOGOS_FILE, 'w') as f:
        json.dump(custom, f, indent=2)


# --- Multi-worker asset sync via signal file ---
_ASSET_SIGNAL_FILE = os.path.join(DATA_DIR, "_asset_signal")
_last_asset_signal_mtime = [0.0]  # mutable container for before_request closure


def _touch_asset_signal():
    """Write current timestamp to signal file so other workers know to reload."""
    with open(_ASSET_SIGNAL_FILE, 'w') as f:
        f.write(str(time.time()))
    _last_asset_signal_mtime[0] = os.path.getmtime(_ASSET_SIGNAL_FILE)


def _reload_assets_from_disk():
    """Full reload of ASSETS, categories, logos. Called by workers that detect a signal."""
    global ASSETS, ASSET_STARTS, ASSET_LOGOS, _CRYPTO_AGG_ASSETS, _STOCK_ASSETS, _INDEX_ASSETS, _METAL_ASSETS, _COMMODITY_ASSETS

    # Build new dicts first, then swap atomically to avoid race conditions
    # where a request thread sees an empty ASSETS dict mid-reload
    new_assets = {}
    new_starts = {}
    if _USE_PRICE_DB:
        for name, df in price_db.get_all_assets().items():
            new_assets[name] = df
            new_starts[name] = str(df.index[0].date())
    else:
        for fname in sorted(os.listdir(DATA_DIR)):
            if fname.endswith(".csv"):
                name = fname.replace(".csv", "")
                try:
                    df = bt.load_data(os.path.join(DATA_DIR, fname))
                    new_assets[name] = df
                    new_starts[name] = str(df.index[0].date())
                except Exception:
                    pass
    # Remove stale keys, then update — avoids the empty-dict window that
    # clear()+update() would create (race condition with request threads)
    for k in list(ASSETS.keys()):
        if k not in new_assets:
            del ASSETS[k]
    ASSETS.update(new_assets)
    for k in list(ASSET_STARTS.keys()):
        if k not in new_starts:
            del ASSET_STARTS[k]
    ASSET_STARTS.update(new_starts)

    # Reload categories
    _CRYPTO_AGG_ASSETS.clear()
    _STOCK_ASSETS.clear()
    _STOCK_ASSETS.update({"Apple", "Microsoft", "Amazon", "Alphabet", "Tesla", "Nvidia", "Meta", "Netflix", "Coinbase", "Strategy"})
    _INDEX_ASSETS.clear()
    _INDEX_ASSETS.update({"Dax", "Dow Jones", "Hang Seng", "Nasdaq100", "SP500"})
    _METAL_ASSETS.clear()
    _METAL_ASSETS.update({"Gold", "Silver", "Palladium"})
    _COMMODITY_ASSETS.clear()
    _COMMODITY_ASSETS.update({"Oil (Brent)", "Oil (Wti)"})
    if os.path.exists(_CATEGORIES_FILE):
        with open(_CATEGORIES_FILE) as f:
            for asset, cat in json.load(f).items():
                if cat == 'crypto_agg': _CRYPTO_AGG_ASSETS.add(asset)
                elif cat == 'stock': _STOCK_ASSETS.add(asset)
                elif cat == 'index': _INDEX_ASSETS.add(asset)
                elif cat == 'metal': _METAL_ASSETS.add(asset)
                elif cat == 'commodity': _COMMODITY_ASSETS.add(asset)

    # Reload logos
    ASSET_LOGOS.update({
        "bitcoin": "bitcoin-btc-logo.png", "ethereum": "ethereum-eth-logo.png",
        "solana": "solana-sol-logo.png", "XRP": "xrp-xrp-logo.png",
        "BNB": "bnb-bnb-logo.png", "Cardano": "cardano-ada-logo.png",
        "Chainlink": "chainlink-link-logo.png", "Dogecoin": "dogecoin-doge-logo.png",
        "Monero": "monero-xmr-logo.png", "Bitcoin Cash": "bitcoin-cash-bch-logo.png",
        "Hyperliquid": "hyperliquid-logo.png", "Bittensor": "bittensor-tao-logo.png",
        "Dax": "dax-logo.svg", "Dow Jones": "dowjones-logo.svg",
        "Hang Seng": "hangseng-logo.svg", "Nasdaq100": "nasdaq-logo.svg",
        "SP500": "sp500-logo.svg",
        "Gold": "gold-logo.svg", "Silver": "silver-logo.svg", "Palladium": "palladium-logo.svg",
        "Oil (Brent)": "oil-brent-logo.svg", "Oil (Wti)": "oil-wti-logo.svg",
        "Apple": "apple-logo.png", "Microsoft": "microsoft-logo.png", "Amazon": "amazon-logo.png",
        "Alphabet": "alphabet-logo.png", "Tesla": "tesla-logo.png", "Nvidia": "nvidia-logo.png",
        "Meta": "meta-logo.png", "Netflix": "netflix-logo.png", "Coinbase": "coinbase-logo.png",
        "Strategy": "strategy-logo.png",
    })
    if os.path.exists(_LOGOS_FILE):
        with open(_LOGOS_FILE) as f:
            ASSET_LOGOS.update(json.load(f))

    _rebuild_asset_lists()


@app.before_request
def _check_asset_signal():
    """Check if another worker has modified assets and reload if needed."""
    if not os.path.exists(_ASSET_SIGNAL_FILE):
        return
    try:
        mtime = os.path.getmtime(_ASSET_SIGNAL_FILE)
        if mtime > _last_asset_signal_mtime[0]:
            _last_asset_signal_mtime[0] = mtime
            _reload_assets_from_disk()
    except OSError:
        pass


def _series_to_lw_json(series):
    """Convert pandas Series (datetime index + float values) to Lightweight Charts format."""
    def _smart_round(val):
        """Round to 6 significant figures — preserves precision for small ratio values."""
        if val == 0:
            return 0
        from math import log10, floor
        magnitude = floor(log10(abs(val)))
        digits = max(2, 6 - magnitude - 1)  # at least 2 decimal places
        return round(val, digits)

    return json.dumps([
        {"time": str(idx.date()), "value": _smart_round(float(val))}
        for idx, val in series.dropna().items()
    ])


def _enrich_best(result, df, periods_per_year=365):
    """Add annualized return and buy-and-hold metrics to a result dict."""
    import numpy as np
    import pandas as pd_mod
    _ppy = periods_per_year
    n_periods = len(df)
    result["annualized"] = bt._annualized_return(result["total_return"], n_periods, _ppy)
    result["buyhold_annualized"] = bt._annualized_return(result["buyhold_return"], n_periods, _ppy)
    result["buyhold_max_drawdown"] = bt._max_drawdown(result["buyhold"])
    period_return = df["close"].pct_change().fillna(0)
    mean_d = period_return.mean()
    std_d = period_return.std()
    result["buyhold_sharpe"] = (mean_d / std_d * np.sqrt(_ppy)) if std_d > 0 else 0.0
    # Buy-and-hold additional metrics
    bh_returns = pd_mod.Series(result["buyhold"].values).pct_change().fillna(0)
    result["buyhold_volatility"] = std_d * np.sqrt(_ppy) * 100
    result["buyhold_sortino"] = bt._sortino_ratio(bh_returns, _ppy)
    result["buyhold_calmar"] = abs(result["buyhold_annualized"] / result["buyhold_max_drawdown"]) if result["buyhold_max_drawdown"] != 0 else 0.0
    result["buyhold_max_dd_duration"] = bt._max_drawdown_duration(result["buyhold"])
    bh_yearly = bt._yearly_returns(result["buyhold"])
    result["buyhold_best_year"] = max(bh_yearly.items(), key=lambda x: x[1]) if bh_yearly else (None, 0)
    result["buyhold_worst_year"] = min(bh_yearly.items(), key=lambda x: x[1]) if bh_yearly else (None, 0)
    # Convert period-based durations to days for weekly data
    if _ppy == 52:
        result["max_dd_duration"] = result["max_dd_duration"] * 7
        result["avg_trade_duration"] = result["avg_trade_duration"] * 7
        result["buyhold_max_dd_duration"] = result["buyhold_max_dd_duration"] * 7
    return result


def _minor_usd_formatter(dollar=True):
    """Return a formatter that shows every 2nd minor tick label."""
    from matplotlib.ticker import FuncFormatter
    state = {"count": 0}
    def _fmt(x, pos):
        state["count"] += 1
        if state["count"] % 2 == 0:
            return ""
        if dollar:
            return f"${x:,.2f}" if x < 1 else f"${x:,.0f}"
        return f"{x:,.2f}" if x < 1 else f"{x:,.0f}"
    return FuncFormatter(_fmt)


def _build_strategy_label(p):
    """Build a human-readable strategy label from params."""
    if p.ind1_name == "price":
        return f"Price/{p.ind2_name.upper()}"
    return f"{p.ind1_name.upper()}/{p.ind2_name.upper()}"


@app.route('/cancel', methods=['POST'])
def cancel():
    rid = request.form.get('id', '')
    with _cancel_lock:
        if rid in _cancel_flags:
            _cancel_flags[rid].set()
    return '', 204


def _render_main(p, **kwargs):
    """Render the main backtester HTML template with shared asset metadata."""
    defaults = dict(
        nav_active='backtester',
        asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS,
        other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS,
        index_assets=INDEX_ASSETS, metal_assets=METAL_ASSETS,
        commodity_assets=COMMODITY_ASSETS, crypto_agg_assets=CRYPTO_AGG_ASSETS,
        asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
        asset_tickers=ASSET_TICKERS,
    )
    defaults.update(kwargs)
    return render_template_string(HTML, p=p, **defaults)


@app.route("/backtester", methods=["GET", "POST"])
@require_auth
def index():
    chart_b64 = None
    best = None
    table_rows = None
    col_header = "Strategy"
    long_short_breakdown = None

    if request.method == "GET":
        # If query params present, pre-fill form from them (shareable URL support)
        if any(k in request.args for k in ('asset', 'mode', 'ind1_name', 'ind2_name', 'period1', 'period2', 'exposure', 'reverse', 'timeframe', 'dca_frequency')):
            p = Params(request.args)
        else:
            p = Params()
        return _render_main(p, chart=None, best=None, table_rows=None, col_header=col_header,
                            price_json=None, ind1_json='[]', ind2_json='[]', ind1_label='', ind2_label='')

    # Check disk cache first
    cache_key = _cache_key(request.form)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    rid = request.form.get('_request_id', str(uuid.uuid4()))
    cancel_event = threading.Event()
    with _cancel_lock:
        _cancel_flags[rid] = cancel_event

    try:
        result = _run_post_handler(cancel_event)
        # Cache the rendered HTML response
        if isinstance(result, str):
            _cache_put(cache_key, result)
        return result
    except ClientDisconnected:
        return '', 204
    finally:
        with _cancel_lock:
            _cancel_flags.pop(rid, None)


def _run_post_handler(cancel_event):
    chart_b64 = None
    thumb_b64 = ''
    best = None
    table_rows = None
    col_header = "Strategy"
    long_short_breakdown = None
    is_oscillator = False
    equity_top = 0.7
    equity_bottom = 1.0

    p = Params(request.form)
    t = bt._get_theme(p.theme)
    is_oscillator = p.signal_type == "oscillator"
    is_ratio = bool(p.vs_asset and p.vs_asset in ASSETS)
    import pandas as pd_mod
    if p.asset not in ASSETS:
        return _render_main(p, chart=None, best=None, table_rows=None, col_header=col_header,
                            error=f'Asset "{p.asset}" not found. It may have been renamed or deleted.',
                            price_json=None, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")
    df_full = ASSETS[p.asset].copy()

    # Relative price mode: divide by denominator asset
    if is_ratio:
        df_vs = ASSETS[p.vs_asset].copy()
        try:
            df_full = compute_ratio_prices(df_full, df_vs)
        except ValueError:
            return _render_main(p, chart=None, best=None, table_rows=None, col_header=col_header,
                                error=f"No overlapping dates between {p.asset} and {p.vs_asset}.",
                                price_json=None, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")
        _cap = lambda s: s.capitalize() if s == s.lower() else s
        asset_display = f"{_cap(p.asset)} / {_cap(p.vs_asset)}"
    else:
        _cap = lambda s: s.capitalize() if s == s.lower() else s
        asset_display = _cap(p.asset)

    # Resample to weekly if requested
    is_weekly = p.timeframe == "weekly"
    periods_per_year = 52 if is_weekly else 365
    if is_weekly:
        df_full = bt.resample_to_weekly(df_full)

    # df_price_all = full price data from start_date to newest available (for chart price line)
    if not p.start_date:
        p.start_date = str(df_full.index[0].date())
    if not p.end_date:
        p.end_date = str(df_full.index[-1].date())
    df_price_all = df_full[df_full.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
    if p.end_date:
        df_full = df_full[df_full.index <= pd_mod.Timestamp(p.end_date, tz="UTC")]
    warmup_start_date = p.start_date
    # df_full = data up to end_date for indicator warmup (passed to strategy functions)
    # df = trimmed to start_date..end_date for display (equity, metrics, buy-and-hold)
    # df_price_all = start_date to newest date (for price line on charts)
    df = df_full[df_full.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]

    fee = p.fee / 100
    fin_rate = p.financing_rate / 100

    # Oscillator mode forces backtest (no sweep/heatmap/lev-sweep support), except regression
    if is_oscillator and p.mode not in ("backtest", "regression", "dca"):
        p.mode = "backtest"

    # --- Regression Analysis Mode ---
    if p.mode == "regression":
        if not is_oscillator:
            return _render_main(p, chart=None, best=None, table_rows=None, col_header=col_header,
                                error="Regression analysis requires an oscillator indicator. Please select one from Indicator 2.",
                                price_json=None, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")

        reg_result = bt.run_regression_analysis(df, p.osc_name, p.osc_period, p.forward_days,
                                                 p.buy_threshold, p.sell_threshold)
        chart_b64 = bt.generate_regression_chart(reg_result, theme=p.theme)

        sweep_result = bt.sweep_regression_r_squared(df, p.osc_name, p.osc_period,
                                                      p.buy_threshold, p.sell_threshold)
        sweep_chart_b64 = bt.generate_regression_sweep_chart(sweep_result, theme=p.theme)

        # Generate small regression thumbnail (scatter plot)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax], p.theme)
        osc_vals = reg_result["osc_values"]
        fwd_rets = reg_result["forward_returns"]
        thumb_ax.scatter(osc_vals, fwd_rets, c=t["muted"], alpha=0.15, s=3, rasterized=True)
        x_range = np.linspace(osc_vals.min(), osc_vals.max(), 100)
        y_pred = reg_result["slope"] * x_range + reg_result["intercept"]
        thumb_ax.plot(x_range, y_pred, color=t["accent"], linewidth=1.5)
        thumb_ax.axhline(y=0, color=t["muted"], linestyle="--", linewidth=0.5, alpha=0.5)
        thumb_ax.grid(True, which="major", alpha=0.3, color=t["grid"])
        thumb_ax.tick_params(labelsize=7)
        thumb_ax.set_xlabel("")
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        return _render_main(p, chart=chart_b64, best=None, table_rows=None, col_header=col_header,
                            regression=reg_result, regression_sweep_chart=sweep_chart_b64, regression_sweep=sweep_result, thumb_b64=thumb_b64,
                            price_json=None, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")

    # --- DCA Optimization Mode ---
    if p.mode == "dca":
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import numpy as np
        import math

        dca_result = bt.run_dca_compare(
            df_full, frequency=p.dca_frequency, amount=p.dca_amount,
            signal_type=p.dca_signal_type, signal_name=p.dca_signal_name,
            signal_period=p.dca_signal_period, max_multiplier=p.dca_max_multiplier,
            fee=fee, start_date=warmup_start_date,
            show_lump_sum=p.dca_show_lump_sum, reverse=p.dca_reverse, periods_per_year=periods_per_year
        )

        if dca_result is None:
            return _render_main(p, chart=None, best=None, table_rows=None, col_header=col_header,
                                error="Not enough data for DCA analysis.",
                                price_json=None, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")

        const = dca_result["constant"]
        dyn = dca_result["dynamic"]
        has_lump = "lump_sum" in dca_result

        tf_label = " (Weekly)" if is_weekly else ""
        n_panels = 4  # price + signal + equity + units
        fig, axes = plt.subplots(n_panels, 1, figsize=(14, 16), dpi=150,
                                  gridspec_kw={"height_ratios": [4, 2, 4, 3]}, sharex=True)
        ax_price, ax_signal, ax_equity, ax_units = axes
        bt._apply_dark_theme(fig, list(axes), p.theme)

        # Panel 1: Price
        prices = dca_result["prices"]
        ax_price.plot(prices.index, prices, color=t["price"], linewidth=0.8, label=f"{asset_display} Price")
        ax_price.set_yscale("log")
        if is_ratio:
            _fmt_ratio = plt.FuncFormatter(lambda x, _: f"{x:.4f}" if x < 0.01 else (f"{x:.3f}" if x < 1 else f"{x:,.2f}"))
            ax_price.yaxis.set_major_formatter(_fmt_ratio)
        else:
            _fmt_usd = plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}")
            ax_price.yaxis.set_major_formatter(_fmt_usd)
        ax_price.set_ylabel(f"{asset_display} Price (log)")
        ax_price.set_title(f"{asset_display}{tf_label} DCA Optimization — {p.dca_frequency.capitalize()} ${p.dca_amount:.0f}\n"
                           f"Signal: {dca_result['signal_label']} | Max Multiplier: {p.dca_max_multiplier:.1f}x | "
                           f"{dca_result['n_buys']} purchases, ${dca_result['total_budget']:,.0f} total")
        ax_price.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
        ax_price.grid(True, which="major", alpha=0.3, color=t["grid"])

        # Panel 2: Signal (0-1)
        signal = dca_result["signal_series"]
        ax_signal.plot(signal.index, signal, color=t["blue"], linewidth=0.7, alpha=0.8)
        ax_signal.fill_between(signal.index, 0, signal, alpha=0.1, color=t["blue"])
        ax_signal.axhline(y=0.5, color=t["muted"], linestyle="--", linewidth=0.6, alpha=0.5)
        ax_signal.set_ylim(-0.05, 1.05)
        ax_signal.set_ylabel(dca_result["signal_label"])
        ax_signal.text(0.01, 0.95, "Buy Less", transform=ax_signal.transAxes, fontsize=7, color=t["red"], va="top")
        ax_signal.text(0.01, 0.05, "Buy More", transform=ax_signal.transAxes, fontsize=7, color=t["green"], va="bottom")
        ax_signal.grid(True, which="major", alpha=0.3, color=t["grid"])

        # Panel 3: Portfolio value
        ax_equity.plot(const["equity"].index, const["equity"], color=t["muted"], linewidth=1.2,
                       label=f"Constant DCA (${const['final_value']:,.0f})")
        ax_equity.plot(dyn["equity"].index, dyn["equity"], color=t["accent"], linewidth=1.2,
                       label=f"Dynamic DCA (${dyn['final_value']:,.0f})")
        ax_equity.plot(const["cum_invested"].index, const["cum_invested"], color="#4ade80", linewidth=1,
                       alpha=0.6, linestyle="--",
                       label=f"Const. Invested (${const['total_invested']:,.0f})")
        ax_equity.plot(dyn["cum_invested"].index, dyn["cum_invested"], color="#facc15", linewidth=1,
                       alpha=0.6, linestyle="--",
                       label=f"Dyn. Invested (${dyn['total_invested']:,.0f})")
        if has_lump:
            lump = dca_result["lump_sum"]
            ax_equity.plot(lump["equity"].index, lump["equity"], color="#a78bfa", linewidth=1,
                           alpha=0.7, linestyle="--",
                           label=f"Lump Sum (${lump['final_value']:,.0f})")
        ax_equity.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
        ax_equity.set_ylabel("Portfolio Value")
        ax_equity.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
        ax_equity.grid(True, which="major", alpha=0.3, color=t["grid"])

        # Panel 4: Units accumulated (base currency)
        const_final_units = const["cum_units"].iloc[-1]
        dyn_final_units = dyn["cum_units"].iloc[-1]
        ax_units.plot(const["cum_units"].index, const["cum_units"], color=t["muted"], linewidth=1.2,
                      label=f"Constant DCA ({const_final_units:,.4f} {asset_display})")
        ax_units.plot(dyn["cum_units"].index, dyn["cum_units"], color=t["accent"], linewidth=1.2,
                      label=f"Dynamic DCA ({dyn_final_units:,.4f} {asset_display})")
        ax_units.set_ylabel(f"{asset_display} Accumulated")
        ax_units.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
        ax_units.grid(True, which="major", alpha=0.3, color=t["grid"])

        ax_units.set_xlabel("Date")
        ax_units.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        date_range_years = (prices.index[-1] - prices.index[0]).days / 365.25
        year_step = max(1, math.ceil(date_range_years / 18))
        ax_units.xaxis.set_major_locator(mdates.YearLocator(year_step))
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        # Thumbnail
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax], p.theme)
        thumb_ax.plot(const["equity"].index, const["equity"], color=t["muted"], linewidth=1.2)
        thumb_ax.plot(dyn["equity"].index, dyn["equity"], color=t["accent"], linewidth=1.2)
        if has_lump:
            thumb_ax.plot(lump["equity"].index, lump["equity"], color="#a78bfa", linewidth=1, alpha=0.7, linestyle="--")
        thumb_ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        thumb_ax.grid(True, which="major", alpha=0.3, color=t["grid"])
        thumb_ax.tick_params(labelsize=7)
        thumb_ax.set_xlabel("")
        thumb_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        thumb_ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        # Build best dict for metrics panel
        advantage = dyn["final_value"] - const["final_value"]
        advantage_pct = (dyn["final_value"] / const["final_value"] - 1) * 100 if const["final_value"] > 0 else 0
        best = {
            "label": dyn["label"],
            "total_return": dyn["total_return"],
            "annualized": dyn["annualized"],
            "max_drawdown": dyn["max_drawdown"],
            "sharpe": dyn["sharpe"],
            "sortino": dyn["sortino"],
            "total_invested": dyn["total_invested"],
            "final_value": dyn["final_value"],
            "total_units": dyn["total_units"],
            "volatility": dyn["volatility"],
            "max_dd_duration": dyn["max_dd_duration"],
            "avg_cost_per_unit": dyn["avg_cost_per_unit"],
            "n_purchases": dyn["n_purchases"],
            "avg_buy_amount": dyn["avg_buy_amount"],
            "min_buy_amount": dyn["min_buy_amount"],
            "max_buy_amount": dyn["max_buy_amount"],
            "median_buy_amount": dyn["median_buy_amount"],
            # Constant DCA as "buyhold" comparison
            "buyhold_return": const["total_return"],
            "buyhold_annualized": const["annualized"],
            "buyhold_max_drawdown": const["max_drawdown"],
            "buyhold_sharpe": const["sharpe"],
            "buyhold_sortino": const["sortino"],
            "buyhold_volatility": const["volatility"],
            "buyhold_max_dd_duration": const["max_dd_duration"],
            "const_final_value": const["final_value"],
            "const_total_units": const["total_units"],
            "const_avg_cost_per_unit": const["avg_cost_per_unit"],
            "const_n_purchases": const["n_purchases"],
            "const_avg_buy_amount": const["avg_buy_amount"],
            "const_min_buy_amount": const["min_buy_amount"],
            "const_max_buy_amount": const["max_buy_amount"],
            "const_median_buy_amount": const["median_buy_amount"],
            "advantage": advantage,
            "advantage_pct": advantage_pct,
            "dca_mode": True,
            "ind1_label": "Dynamic DCA",
            "ind2_label": dca_result["signal_label"],
            "ind1_name": "price",
            "ind2_name": p.dca_signal_name,
        }

        price_json = _series_to_lw_json(df_price_all["close"])
        return _render_main(p, chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                            thumb_b64=thumb_b64,
                            price_json=price_json, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")

    # --- Leverage Sweep Mode ---
    if p.mode == "sweep-lev":
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        n_days = len(df)
        lev_values = [round(p.lev_min + i * p.lev_step, 4)
                      for i in range(int((p.lev_max - p.lev_min) / p.lev_step) + 1)]

        # Compute base position from ind1/ind2 (on full data for warmup)
        ind1_series, _ = bt.compute_indicator_from_spec(df_full, p.ind1_name, p.ind1_period)
        ind2_period_val = p.ind2_period if p.ind2_period else 44
        ind2_series, _ = bt.compute_indicator_from_spec(df_full, p.ind2_name, ind2_period_val)
        above = ind1_series > ind2_series
        if p.reverse:
            above = ~above
        position_base = bt._apply_exposure(above, p.exposure).shift(1).fillna(0)
        # Force cash during NaN warmup period
        _nan_mask = ind1_series.isna() | ind2_series.isna()
        position_base[_nan_mask] = 0
        daily_return = df_full["close"].pct_change().fillna(0)
        # Trim to start_date after indicator warmup
        _ws_ts = pd_mod.Timestamp(warmup_start_date, tz="UTC")
        _ws_mask = df_full.index >= _ws_ts
        position_base = position_base[_ws_mask]
        daily_return = daily_return[_ws_mask]

        if p.ind1_name == "price":
            title_label = f"Price/{p.ind2_name.upper()}({ind2_period_val})"
        else:
            p1_str = p.ind1_period if p.ind1_period else "?"
            title_label = f"{p.ind1_name.upper()}({p1_str})/{p.ind2_name.upper()}({ind2_period_val})"

        def _sweep_ann(ll, sl):
            _apply_fin = bt._should_apply_financing(fin_rate, p.exposure, ll, sl, p.sizing)
            _fdl = bt._financing_daily_rate(ll, fin_rate, periods_per_year) if _apply_fin else 0.0
            _fds = bt._financing_daily_rate(sl, fin_rate, periods_per_year) if _apply_fin else 0.0
            if p.sizing == "fixed":
                leverage = np.where(position_base.values > 0, ll,
                           np.where(position_base.values < 0, sl, 1))
                daily_pnl = p.initial_cash * position_base.values * daily_return.values * leverage
                daily_pnl = daily_pnl.copy()
                trade_changes = np.diff(position_base.values, prepend=0)
                daily_pnl[np.abs(trade_changes) > 0] -= p.initial_cash * fee
                equity_arr = p.initial_cash + np.cumsum(daily_pnl)
            elif p.lev_mode == "set-forget":
                equity_arr, _, _, _ = bt._compute_equity_set_and_forget(
                    position_base.values, daily_return.values, p.initial_cash, ll, sl, fee, _fdl, _fds)
            elif p.lev_mode == "optimal":
                equity_arr, _, _, _ = bt._compute_equity_optimal(
                    position_base.values, daily_return.values, p.initial_cash, ll, sl, fee, _fdl, _fds)
            else:
                leverage = np.where(position_base.values > 0, ll,
                           np.where(position_base.values < 0, sl, 1))
                strat_ret = position_base.values * daily_return.values * leverage
                strat_ret = strat_ret.copy()
                trade_changes = np.diff(position_base.values, prepend=0)
                strat_ret[np.abs(trade_changes) > 0] -= fee
                if _apply_fin:
                    _fr = bt._financing_daily_rate(leverage, fin_rate, periods_per_year)
                    strat_ret -= position_base.values * _fr
                equity_arr, _ = bt._compute_equity_with_liquidation(strat_ret, p.initial_cash)
            equity_final = equity_arr[-1] if len(equity_arr) > 0 else p.initial_cash
            total_ret = (equity_final / p.initial_cash - 1) * 100
            return bt._annualized_return(total_ret, n_days, periods_per_year)

        long_sweep_full = []
        for lv in lev_values:
            check_cancelled(cancel_event)
            long_sweep_full.append(_sweep_ann(lv, 0))
        short_sweep_full = []
        for lv in lev_values:
            check_cancelled(cancel_event)
            short_sweep_full.append(_sweep_ann(0, lv))

        def _trim_flatline(values, levs):
            if len(values) < 3:
                return values, levs
            for i in range(len(values) - 1, 1, -1):
                if abs(values[i] - values[i - 1]) > 0.01:
                    return values[:i + 2], levs[:i + 2]
            return values, levs

        long_sweep, long_levs = _trim_flatline(long_sweep_full, list(lev_values))
        short_sweep, short_levs = _trim_flatline(short_sweep_full, list(lev_values))

        best_long_idx = np.argmax(long_sweep)
        best_short_idx = np.argmax(short_sweep)
        best_long_lev = long_levs[best_long_idx]
        best_long_ann = long_sweep[best_long_idx]
        best_short_lev = short_levs[best_short_idx]
        best_short_ann = short_sweep[best_short_idx]

        bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_ann = bt._annualized_return(bh_total, n_days, periods_per_year)

        asset_title = asset_display
        tf_label = " (Weekly)" if is_weekly else ""
        fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
        bt._apply_dark_theme(fig, ax, p.theme)
        show_long = p.exposure in ("long-cash", "long-short")
        show_short = p.exposure in ("short-cash", "long-short")
        all_levs = []
        if show_long:
            ax.plot(long_levs, long_sweep, color=t["blue"], linewidth=1.5, label="Long Leverage")
            ax.scatter([best_long_lev], [best_long_ann], color=t["blue"], s=60, zorder=5)
            all_levs.extend(long_levs)
        if show_short:
            ax.plot(short_levs, short_sweep, color=t["accent"], linewidth=1.5, label="Short Leverage")
            ax.scatter([best_short_lev], [best_short_ann], color=t["accent"], s=60, zorder=5)
            all_levs.extend(short_levs)
        x_min, x_max = min(all_levs), max(all_levs)
        if p.exposure != "short-cash":
            ax.plot([x_min, x_max], [bh_ann, bh_ann], color=t["muted"], linestyle="--", linewidth=1,
                    label=f"Buy & Hold ({bh_ann:.1f}%)")
        ax.set_xlim(x_min, x_max)
        from matplotlib.ticker import MultipleLocator
        ax.xaxis.set_major_locator(MultipleLocator(0.25))
        ax.set_xlabel("Leverage")
        ax.set_ylabel("Annualized Return (%)")
        title_parts = []
        if show_long:
            title_parts.append(f"Best Long: {best_long_lev:.2f}x ({best_long_ann:.1f}%)")
        if show_short:
            title_parts.append(f"Best Short: {best_short_lev:.2f}x ({best_short_ann:.1f}%)")
        ax.set_title(f"{asset_title}{tf_label} {title_label} \u2014 Leverage Sweep | {p.exposure}\n"
                     f"{' | '.join(title_parts)}")
        ax.legend(loc="best", fontsize=9, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
        ax.grid(True, alpha=0.3, color=t["grid"])
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        # Generate small leverage-sweep thumbnail
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax], p.theme)
        if show_long:
            thumb_ax.plot(long_levs, long_sweep, color=t["blue"], linewidth=1.5)
            thumb_ax.scatter([best_long_lev], [best_long_ann], color=t["blue"], s=40, zorder=5)
        if show_short:
            thumb_ax.plot(short_levs, short_sweep, color=t["accent"], linewidth=1.5)
            thumb_ax.scatter([best_short_lev], [best_short_ann], color=t["accent"], s=40, zorder=5)
        if p.exposure != "short-cash":
            thumb_ax.axhline(y=bh_ann, color=t["muted"], linestyle="--", linewidth=1, alpha=0.7)
        thumb_ax.set_xlabel("")
        thumb_ax.grid(True, which="major", alpha=0.3, color=t["grid"])
        thumb_ax.tick_params(labelsize=7)
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        best_result = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, ind2_period_val,
                                       p.initial_cash, fee, p.exposure, best_long_lev, best_short_lev, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
        best = _enrich_best(best_result, df, periods_per_year)

        combined_ann = _sweep_ann(best_long_lev, best_short_lev)
        lev_sweep_info = {
            "best_long_lev": best_long_lev,
            "best_long_ann": best_long_ann,
            "best_short_lev": best_short_lev,
            "best_short_ann": best_short_ann,
            "combined_ann": combined_ann,
            "combined_label": f"{title_label} with long {best_long_lev:.2f}x / short {best_short_lev:.2f}x",
        }
        price_json = _series_to_lw_json(df_price_all["close"])
        _lc2, _ = bt.compute_indicator_from_spec(df_price_all, best["ind2_name"], best.get("ind2_period"))
        _lc2 = _lc2[_lc2.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
        ind2_json = _series_to_lw_json(_lc2)
        if best.get("ind1_name") != "price":
            _lc1, _ = bt.compute_indicator_from_spec(df_price_all, best["ind1_name"], best.get("ind1_period"))
            _lc1 = _lc1[_lc1.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
            ind1_json = _series_to_lw_json(_lc1)
        else:
            ind1_json = "[]"
        return _render_main(p, chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                            hide_buyhold=(p.exposure == "short-cash"), lev_sweep=lev_sweep_info, thumb_b64=thumb_b64,
                            price_json=price_json, ind1_json=ind1_json, ind2_json=ind2_json,
                            ind1_label=best.get("ind1_label", ""), ind2_label=best.get("ind2_label", ""))

    # --- Rolling Window Mode ---
    if p.mode == "rolling":
        import matplotlib
        matplotlib.use("Agg")
        import numpy as np

        # Validate: need a fixed strategy period
        if p.ind2_period is None:
            return _render_main(p, chart=None, best=None, table_rows=None, col_header=col_header,
                                error="Rolling window requires a fixed strategy. Please set Period 2.",
                                price_json=None, ind1_json="[]", ind2_json="[]",
                                ind1_label="", ind2_label="")

        # Generate windows
        try:
            windows = bt.generate_rolling_windows(df_full, p.window_size, p.step_size, periods_per_year,
                                                     start_date=p.start_date, end_date=p.end_date)
        except ValueError as e:
            return _render_main(p, chart=None, best=None, table_rows=None, col_header=col_header,
                                error=str(e),
                                price_json=None, ind1_json="[]", ind2_json="[]",
                                ind1_label="", ind2_label="")

        # Fixed strategy evaluation (for timeline + equity overlay)
        fixed_results = bt.rolling_window_evaluate(
            df_full, windows, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
            p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage,
            p.lev_mode, p.reverse, p.sizing, periods_per_year, fin_rate)

        # Consistency score
        score, score_label = bt.compute_consistency_score(fixed_results, p.rolling_metric)

        # Strategy label
        if p.ind1_name != "price":
            strategy_label = f"{p.ind1_name.upper()}({p.ind1_period})/{p.ind2_name.upper()}({p.ind2_period})"
        else:
            strategy_label = f"Price/{p.ind2_name.upper()}({p.ind2_period})"

        # Common charts
        chart_timeline = bt.generate_rolling_timeline_chart(
            fixed_results, p.rolling_metric, strategy_label, score, score_label, p.theme)
        chart_equity = bt.generate_rolling_equity_overlay(fixed_results, strategy_label, p.theme, mode="usd")

        is_dual = p.ind1_name != "price"
        metric_names = {"total_return": "Return %", "alpha": "Alpha %", "sharpe": "Sharpe"}
        metric_display = metric_names.get(p.rolling_metric, p.rolling_metric)

        if is_dual:
            # Dual indicator: animated heatmap over time
            import json as json_mod
            dual_sweep = bt.rolling_window_sweep_dual(
                df_full, windows, p.ind1_name, p.ind2_name,
                p.range_min, p.range_max, p.step,
                p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage,
                p.lev_mode, p.reverse, p.sizing, periods_per_year, fin_rate,
                metric=p.rolling_metric)

            # Plotly JSON for animated heatmap
            periods_anim = dual_sweep["periods"]
            per_window = dual_sweep["per_window_matrices"]
            window_labels = dual_sweep["window_labels"]
            # Build frames: one heatmap per window with per-frame scale and best combo
            frames_json = []
            for wi, (mat, label) in enumerate(zip(per_window, window_labels)):
                z_frame = [[None if np.isnan(v) else round(float(v), 2) for v in row] for row in mat]
                frame_vals = [v for row in mat for v in row if not np.isnan(v)]
                fmax = round(float(max(abs(v) for v in frame_vals)), 2) if frame_vals else 100
                # Find best combo for this specific window
                best_v, best_fp1, best_fp2 = -np.inf, periods_anim[0], periods_anim[0]
                for pi in range(len(periods_anim)):
                    for pj in range(len(periods_anim)):
                        v = mat[pi, pj]
                        if not np.isnan(v) and v > best_v:
                            best_v = v
                            best_fp1 = periods_anim[pi]
                            best_fp2 = periods_anim[pj]
                frames_json.append({"label": label, "z": z_frame, "zmax": fmax,
                                    "best_p1": int(best_fp1), "best_p2": int(best_fp2)})
            # dtick for axes: auto based on period count
            dtick = max(1, len(periods_anim) // 15) * (periods_anim[1] - periods_anim[0]) if len(periods_anim) > 1 else 1
            plotly_data = json_mod.dumps({
                "periods": periods_anim, "frames": frames_json,
                "dtick": dtick,
                "selected_p1": p.ind1_period, "selected_p2": p.ind2_period,
                "best_p1": int(dual_sweep["best_p1"]), "best_p2": int(dual_sweep["best_p2"]),
                "best_val": round(float(dual_sweep["best_val"]), 2),
                "ind1_name": p.ind1_name.upper(), "ind2_name": p.ind2_name.upper(),
                "metric_label": metric_display, "strategy_label": strategy_label,
                "same_type": dual_sweep["same_type"],
            })

            return _render_main(p, chart=chart_timeline,
                                rolling_charts={"timeline": chart_timeline,
                                                "equity": chart_equity},
                                rolling_is_dual=True, rolling_plotly_data=plotly_data,
                                rolling_score=score, rolling_score_label=score_label,
                                rolling_metric=p.rolling_metric, rolling_windows=len(windows),
                                rolling_strategy=strategy_label,
                                best=None, table_rows=None, col_header=col_header,
                                price_json=None, ind1_json="[]", ind2_json="[]",
                                ind1_label="", ind2_label="")
        else:
            # Single indicator (price vs MA): heatmap sweep of ind2 period
            sweep_data = bt.rolling_window_sweep(
                df_full, windows, p.ind1_name, p.ind1_period, p.ind2_name,
                "ind2", p.range_min, p.range_max, p.step,
                p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage,
                p.lev_mode, p.reverse, p.sizing, periods_per_year, fin_rate,
                metric=p.rolling_metric)

            sweep_ind_label = f"{p.ind2_name.upper()} Period"
            chart_heatmap = bt.generate_rolling_heatmap(sweep_data, p.rolling_metric, strategy_label, p.theme,
                                                         selected_period=p.ind2_period, sweep_ind_label=sweep_ind_label)

            return _render_main(p, chart=chart_heatmap,
                                rolling_charts={"heatmap": chart_heatmap,
                                                "timeline": chart_timeline,
                                                "equity": chart_equity},
                                rolling_score=score, rolling_score_label=score_label,
                                rolling_metric=p.rolling_metric, rolling_windows=len(windows),
                                rolling_strategy=strategy_label,
                                best=None, table_rows=None, col_header=col_header,
                                price_json=None, ind1_json="[]", ind2_json="[]",
                                ind1_label="", ind2_label="")

    # --- Heatmap Mode ---
    if p.mode == "heatmap":
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        ind1_name = p.ind1_name
        ind2_name = p.ind2_name

        # Price has no period to sweep — fall back to sweep chart (1D)
        if ind1_name == "price":
            p.mode = "sweep"
            # Fall through to sweep handler below

    if p.mode == "heatmap":
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        ind1_name = p.ind1_name
        ind2_name = p.ind2_name

        n_days = len(df)
        periods = list(range(p.range_min, p.range_max + 1, p.step))
        n = len(periods)
        same_type = (ind1_name == ind2_name)

        ind1_upper = ind1_name.upper()
        ind2_upper = ind2_name.upper()

        # Precompute indicators on full data (warmup), then trim
        ind1_cache = {}
        ind2_cache = {}
        for per in periods:
            ind1_cache[per], _ = bt.compute_indicator_from_spec(df_full, ind1_name, per)
            if same_type:
                ind2_cache[per] = ind1_cache[per]
            else:
                ind2_cache[per], _ = bt.compute_indicator_from_spec(df_full, ind2_name, per)

        daily_return_full = df_full["close"].pct_change().fillna(0)

        matrix = np.full((n, n), np.nan)
        best_ann = -np.inf
        best_p1 = best_p2 = None
        # Trim mask for start_date (on df_full index)
        _hm_ts = pd_mod.Timestamp(warmup_start_date, tz="UTC")
        _hm_mask = df_full.index >= _hm_ts
        daily_return_trimmed = daily_return_full[_hm_mask]
        n_days = int(_hm_mask.sum())
        for i, p1 in enumerate(periods):
            check_cancelled(cancel_event)
            for j, p2 in enumerate(periods):
                if same_type and p1 >= p2:
                    continue
                above = ind1_cache[p1] > ind2_cache[p2]
                if p.reverse:
                    above = ~above
                position = bt._apply_exposure(above, p.exposure).shift(1).fillna(0)
                # Force cash during NaN warmup period
                nan_mask = ind1_cache[p1].isna() | ind2_cache[p2].isna()
                position[nan_mask] = 0
                # Trim to start_date after position is computed with warmup
                position = position[_hm_mask]
                leverage = np.where(position > 0, p.long_leverage,
                           np.where(position < 0, p.short_leverage, 1))
                if p.sizing == "fixed":
                    daily_pnl = p.initial_cash * position * daily_return_trimmed * leverage
                    trade_mask = position.diff().fillna(0).abs() > 0
                    daily_pnl = daily_pnl.copy()
                    daily_pnl[trade_mask] -= p.initial_cash * fee
                    equity_arr = p.initial_cash + daily_pnl.cumsum().values
                else:
                    strat_return = position * daily_return_trimmed * leverage
                    trade_mask = position.diff().fillna(0).abs() > 0
                    strat_return = strat_return.copy()
                    strat_return[trade_mask] -= fee
                    equity_arr, _ = bt._compute_equity_with_liquidation(strat_return.values, p.initial_cash)
                equity_final = equity_arr[-1] if len(equity_arr) > 0 else p.initial_cash
                total_ret = (equity_final / p.initial_cash - 1) * 100
                ann = bt._annualized_return(total_ret, n_days, periods_per_year)
                matrix[i, j] = ann
                if ann > best_ann:
                    best_ann = ann
                    best_p1 = p1
                    best_p2 = p2

        # df is already trimmed to start_date; use it for buy-and-hold and chart display
        bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_ann = bt._annualized_return(bh_total, n_days, periods_per_year)

        tf_label = " (Weekly)" if is_weekly else ""
        fig, ax = plt.subplots(figsize=(14, 12), dpi=150)
        bt._apply_dark_theme(fig, ax, p.theme)
        im = ax.imshow(matrix, cmap="RdYlGn", aspect="auto", origin="lower",
                       interpolation="nearest")
        ax.set_xticks(range(n))
        ax.set_xticklabels(periods, rotation=90, fontsize=max(4, min(8, 200 // n)))
        ax.set_yticks(range(n))
        ax.set_yticklabels(periods, fontsize=max(4, min(8, 200 // n)))

        asset_title = asset_display
        period_unit = "weeks" if is_weekly else "days"

        if same_type:
            ax.set_xlabel(f"Slow {ind1_upper} Period ({period_unit})")
            ax.set_ylabel(f"Fast {ind1_upper} Period ({period_unit})")
        else:
            ax.set_xlabel(f"{ind2_upper} Period ({period_unit})")
            ax.set_ylabel(f"{ind1_upper} Period ({period_unit})")
        ax.set_title(f"{asset_title}{tf_label} {ind1_upper}/{ind2_upper} Crossover \u2014 Annualized Return % (step={p.step})\n"
                     f"Best: {ind1_upper}({best_p1})/{ind2_upper}({best_p2}) = {best_ann:.1f}% | "
                     f"B&H: {bh_ann:.1f}% | {p.exposure}")
        cbar = fig.colorbar(im, ax=ax, shrink=0.8)
        cbar.set_label("Annualized Return (%)", color=t["muted"])
        cbar.ax.yaxis.set_tick_params(color=t["muted"])
        cbar.outline.set_edgecolor(t["grid"])
        for label in cbar.ax.get_yticklabels():
            label.set_color("#9ca3af")
        if n <= 30:
            for i in range(n):
                for j in range(n):
                    val = matrix[i, j]
                    if not np.isnan(val):
                        color = "black" if abs(val - np.nanmean(matrix)) < np.nanstd(matrix) else "white"
                        ax.text(j, i, f"{val:.0f}", ha="center", va="center",
                                fontsize=max(4, min(7, 150 // n)), color=color)
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        # Generate small heatmap thumbnail
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax], p.theme)
        thumb_im = thumb_ax.imshow(matrix, cmap="RdYlGn", aspect="auto", origin="lower", interpolation="nearest")
        thumb_ax.set_xticks([])
        thumb_ax.set_yticks([])
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        best_result = bt.run_strategy(df_full, ind1_name, best_p1, ind2_name, best_p2,
                                       p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
        best = _enrich_best(best_result, df, periods_per_year)

        price_json = _series_to_lw_json(df_price_all["close"])
        _lc2, _ = bt.compute_indicator_from_spec(df_price_all, best["ind2_name"], best.get("ind2_period"))
        _lc2 = _lc2[_lc2.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
        ind2_json = _series_to_lw_json(_lc2)
        if best.get("ind1_name") != "price":
            _lc1, _ = bt.compute_indicator_from_spec(df_price_all, best["ind1_name"], best.get("ind1_period"))
            _lc1 = _lc1[_lc1.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
            ind1_json = _series_to_lw_json(_lc1)
        else:
            ind1_json = "[]"
        return _render_main(p, chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                            hide_buyhold=(p.exposure == "short-cash"), thumb_b64=thumb_b64,
                            price_json=price_json, ind1_json=ind1_json, ind2_json=ind2_json,
                            ind1_label=best.get("ind1_label", ""), ind2_label=best.get("ind2_label", ""))

    # --- Sweep Mode (Find Best Period) ---
    if p.mode == "sweep":
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        n_days = len(df)
        periods = list(range(p.range_min, p.range_max + 1))
        annualized_returns = []

        for period in periods:
            check_cancelled(cancel_event)
            result = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, period,
                                      p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
            ann = bt._annualized_return(result["total_return"], n_days, periods_per_year)
            annualized_returns.append(ann)

        bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_annualized = bt._annualized_return(bh_total, n_days, periods_per_year)
        best_idx = np.argmax(annualized_returns)
        best_period = periods[best_idx]
        best_ann = annualized_returns[best_idx]

        ind2_upper = p.ind2_name.upper()
        if p.ind1_name != "price":
            ind1_label_str = f"{p.ind1_name.upper()}({p.ind1_period})"
            best_label = f"{ind1_label_str}/{ind2_upper}({best_period})"
        else:
            best_label = f"{ind2_upper}({best_period})"

        fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
        bt._apply_dark_theme(fig, ax, p.theme)
        ax.plot(periods, annualized_returns, color=t["blue"], linewidth=1)
        if p.exposure != "short-cash":
            ax.axhline(y=bh_annualized, color=t["muted"], linestyle="--", linewidth=1,
                        label=f"Buy & Hold ({bh_annualized:.1f}%)")
        ax.scatter([best_period], [best_ann], color=t["accent"], s=60, zorder=5,
                    label=f"Best: {best_label} ({best_ann:.1f}%)")
        period_unit = "weeks" if is_weekly else "days"
        ax.set_xlabel(f"{ind2_upper} Period ({period_unit})")
        ax.set_ylabel("Annualized Return (%)")
        asset_title = asset_display
        tf_label = " (Weekly)" if is_weekly else ""
        title_prefix = f"{ind1_label_str} vs " if p.ind1_name != "price" else ""
        ax.set_title(f"{asset_title}{tf_label} \u2014 Annualized Return by {title_prefix}{ind2_upper} Period ({p.range_min}-{p.range_max}) | {p.exposure}")
        ax.legend(loc="best", fontsize=9, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
        ax.grid(True, alpha=0.3, color=t["grid"])
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        # Generate small sweep thumbnail
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax], p.theme)
        thumb_ax.plot(periods, annualized_returns, color=t["blue"], linewidth=1.5)
        thumb_ax.scatter([best_period], [best_ann], color=t["accent"], s=40, zorder=5)
        if p.exposure != "short-cash":
            thumb_ax.axhline(y=bh_annualized, color=t["muted"], linestyle="--", linewidth=1, alpha=0.7)
        thumb_ax.grid(True, which="major", alpha=0.3, color=t["grid"])
        thumb_ax.tick_params(labelsize=7)
        thumb_ax.set_xlabel("")
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        best_result = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, best_period,
                                       p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
        best = _enrich_best(best_result, df, periods_per_year)

    # --- Backtest Mode ---
    else:

        if is_oscillator:
            # Oscillator strategy
            result = bt.run_oscillator_strategy(df_full, p.osc_name, p.osc_period, p.buy_threshold, p.sell_threshold,
                                                 p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
            results = [result]
        elif p.ind2_period is not None:
            # Single run with fixed period
            result = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
                                      p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
            results = [result]
        else:
            # Sweep ind2 period and show table
            results = bt.sweep_periods(df_full, p.ind1_name, p.ind1_period, p.ind2_name, None,
                                        "ind2", p.range_min, p.range_max,
                                        p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode,
                                        sizing=p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year,
                                        financing_rate=fin_rate)
            # For same-type crossover, filter invalid combos
            if p.ind1_name != "price" and p.ind1_name == p.ind2_name and p.ind1_period is not None:
                results = [r for r in results if r["ind2_period"] > p.ind1_period]
                results.sort(key=lambda r: r["total_return"], reverse=True)

        if results:
            best = _enrich_best(results[0], df, periods_per_year)
            if len(results) > 1:
                table_rows = [{"label": r["label"], **r} for r in results]

            # Compute long/short breakdown for long-short exposure
            long_short_breakdown = None
            if not is_oscillator and p.exposure == "long-short" and p.ind2_period is not None:
                long_only = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
                                             p.initial_cash, fee, "long-cash", p.long_leverage, 1, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
                short_only = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
                                              p.initial_cash, fee, "short-cash", 1, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date, periods_per_year=periods_per_year, financing_rate=fin_rate)
                long_only = _enrich_best(long_only, df, periods_per_year)
                short_only = _enrich_best(short_only, df, periods_per_year)
                long_short_breakdown = {"long": long_only, "short": short_only}

            # Generate chart
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            import numpy as np

            asset_name = asset_display
            show_ratio = p.exposure != "short-cash" and p.sizing != "fixed"

            if is_oscillator:
                # Oscillator chart: price panel + oscillator panel + equity panel
                if show_ratio:
                    fig, (ax1, ax_osc, ax2, ax3) = plt.subplots(4, 1, figsize=(14, 16), dpi=150,
                                                                  gridspec_kw={"height_ratios": [4, 2, 2.5, 2.5]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax_osc, ax2, ax3], p.theme)
                    equity_top = (4 + 2) / (4 + 2 + 2.5 + 2.5)
                    equity_bottom = (4 + 2 + 2.5) / (4 + 2 + 2.5 + 2.5)
                else:
                    fig, (ax1, ax_osc, ax2) = plt.subplots(3, 1, figsize=(14, 13), dpi=150,
                                                             gridspec_kw={"height_ratios": [5, 2, 3]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax_osc, ax2], p.theme)
                    equity_top = (5 + 2) / (5 + 2 + 3)
                    equity_bottom = 1.0
            else:
                if show_ratio:
                    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 13), dpi=150,
                                                         gridspec_kw={"height_ratios": [5, 2.5, 2.5]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax2, ax3], p.theme)
                    equity_top = 5 / (5 + 2.5 + 2.5)
                    equity_bottom = (5 + 2.5) / (5 + 2.5 + 2.5)
                else:
                    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), dpi=150,
                                                    gridspec_kw={"height_ratios": [7, 3]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax2], p.theme)
                    equity_top = 7 / (7 + 3)
                    equity_bottom = 1.0

            ax1.plot(df_price_all.index, df_price_all["close"], label=f"{asset_name} {'Ratio' if is_ratio else 'Price'}", color=t["price"], linewidth=0.8)

            if not is_oscillator:
                # Compute indicators on full price data (extends beyond backtest end_date)
                _ext_ind2, _ = bt.compute_indicator_from_spec(df_price_all, best["ind2_name"], best.get("ind2_period"))
                _ext_ind2 = _ext_ind2[_ext_ind2.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
                ax1.plot(_ext_ind2.index, _ext_ind2,
                         label=best["ind2_label"], color=t["blue"], linewidth=0.8, alpha=0.8)
                if best.get("ind1_name") != "price":
                    _ext_ind1, _ = bt.compute_indicator_from_spec(df_price_all, best["ind1_name"], best.get("ind1_period"))
                    _ext_ind1 = _ext_ind1[_ext_ind1.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
                    ax1.plot(_ext_ind1.index, _ext_ind1,
                             label=best["ind1_label"], color=t["accent"], linewidth=0.8, alpha=0.8)

            ax1.set_yscale("log")
            if is_ratio:
                _fmt_ratio = plt.FuncFormatter(lambda x, _: f"{x:.4f}" if x < 0.01 else (f"{x:.3f}" if x < 1 else f"{x:,.2f}"))
                ax1.yaxis.set_major_formatter(_fmt_ratio)
                ax1.yaxis.set_minor_formatter(_minor_usd_formatter(dollar=False))
            else:
                _fmt_usd = plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}")
                ax1.yaxis.set_major_formatter(_fmt_usd)
                ax1.yaxis.set_minor_formatter(_minor_usd_formatter())
            ax1.tick_params(axis='y', which='minor', labelsize=6)
            ax1.set_ylabel(f"{asset_name} Ratio (log scale)" if is_ratio else f"{asset_name} Price (log scale)")
            tf_label = " (Weekly)" if is_weekly else ""
            ax1.set_title(f"{asset_name}{tf_label} Backtest \u2014 {best['label']} "
                          f"({best['total_return']:.1f}% return) | {p.exposure}")
            ax1.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
            ax1.grid(True, which="major", alpha=0.3, color=t["grid"])
            ax1.grid(True, which="minor", alpha=0.15, color=t["grid"])

            # --- Oscillator panel ---
            if is_oscillator:
                osc_data = best.get("osc_data")
                osc_spec = osc_data["spec"]
                osc_colors = [t["blue"], t["accent"], t["green"]]

                if p.osc_name == "macd":
                    # MACD: line + signal + histogram bars
                    macd_s = osc_data["series"]["MACD"]
                    sig_s = osc_data["series"]["Signal"]
                    hist_s = osc_data["series"]["Histogram"]
                    ax_osc.plot(macd_s.index, macd_s, color=t["blue"], linewidth=0.9, label="MACD")
                    ax_osc.plot(sig_s.index, sig_s, color=t["accent"], linewidth=0.9, label="Signal")
                    # Histogram as bars
                    pos_hist = hist_s.where(hist_s >= 0, 0)
                    neg_hist = hist_s.where(hist_s < 0, 0)
                    ax_osc.fill_between(hist_s.index, 0, pos_hist, alpha=0.3, color=t["green"], step="mid")
                    ax_osc.fill_between(hist_s.index, 0, neg_hist, alpha=0.3, color=t["red"], step="mid")
                    ax_osc.axhline(y=0, color=t["muted"], linestyle="--", linewidth=0.6, alpha=0.5)
                else:
                    # Single or dual line oscillators
                    for idx, (line_name, line_series) in enumerate(osc_data["series"].items()):
                        ax_osc.plot(line_series.index, line_series, color=osc_colors[idx % len(osc_colors)],
                                    linewidth=0.9, label=line_name)

                    # Draw threshold lines
                    ax_osc.axhline(y=p.buy_threshold, color=t["green"], linestyle="--", linewidth=0.7, alpha=0.7,
                                   label=f"Buy ({p.buy_threshold})")
                    ax_osc.axhline(y=p.sell_threshold, color=t["red"], linestyle="--", linewidth=0.7, alpha=0.7,
                                   label=f"Sell ({p.sell_threshold})")

                    # Shade overbought/oversold zones
                    osc_range = osc_spec.get("range")
                    if osc_range:
                        ax_osc.fill_between(df.index, osc_range[0], p.buy_threshold, alpha=0.04, color=t["green"])
                        ax_osc.fill_between(df.index, p.sell_threshold, osc_range[1], alpha=0.04, color=t["red"])
                    else:
                        # For unbounded oscillators, just shade between threshold lines
                        ax_osc.axhline(y=0, color=t["muted"], linestyle="--", linewidth=0.5, alpha=0.3)

                # Set y-limits for bounded oscillators
                osc_range = osc_spec.get("range")
                if osc_range:
                    ax_osc.set_ylim(osc_range[0] - 2, osc_range[1] + 2)

                ax_osc.set_ylabel(osc_data["label"])
                ax_osc.legend(loc="upper left", fontsize=7, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"], ncol=3)
                ax_osc.grid(True, which="major", alpha=0.3, color=t["grid"])

            ax2.plot(best["equity"].index, best["equity"], label="Strategy Equity", color=t["blue"], linewidth=1)
            if show_ratio:
                ax2.plot(best["buyhold"].index, best["buyhold"], label="Buy & Hold", color=t["muted"], linewidth=1, alpha=0.7)
            if p.sizing == "fixed":
                ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if abs(x) < 1 else f"${x:,.0f}"))
                ax2.axhline(y=p.initial_cash, color=t["muted"], linestyle="--", linewidth=0.8, alpha=0.5)
                ax2.set_ylabel("Portfolio Value (linear, fixed sizing)")
            else:
                ax2.set_yscale("log")
                ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
                ax2.yaxis.set_minor_formatter(_minor_usd_formatter())
                ax2.tick_params(axis='y', which='minor', labelsize=6)
                ax2.set_ylabel("Portfolio Value (log)")
            ax2.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
            ax2.grid(True, which="major", alpha=0.3, color=t["grid"])
            ax2.grid(True, which="minor", alpha=0.15, color=t["grid"])

            last_ax = ax2
            if show_ratio:
                ratio = best["equity"] / best["buyhold"].replace(0, np.nan)
                ratio_normalized = ratio / ratio.dropna().iloc[0] * 100
                ax3.plot(ratio_normalized.index, ratio_normalized, color="#a78bfa", linewidth=1, label=f"Strategy in {asset_name}")
                ax3.axhline(y=100, color=t["muted"], linestyle="--", linewidth=0.8, alpha=0.7)
                if p.sizing != "fixed":
                    ax3.set_yscale("log")
                    ax3.yaxis.set_minor_formatter(_minor_usd_formatter(dollar=False))
                    ax3.tick_params(axis='y', which='minor', labelsize=6)
                ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.2f}" if abs(x) < 1 else f"{x:,.0f}"))
                ax3.set_ylabel(f"Value in {asset_name}")
                ax3.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["price"])
                ax3.grid(True, which="major", alpha=0.3, color=t["grid"])
                ax3.grid(True, which="minor", alpha=0.15, color=t["grid"])
                last_ax = ax3
            last_ax.set_xlabel("Date")
            last_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            import math
            chart_last_date = max(df.index[-1], df_price_all.index[-1])
            date_range_years = (chart_last_date - df.index[0]).days / 365.25
            year_step = max(1, math.ceil(date_range_years / 18))
            last_ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
            plt.tight_layout()

            buf = BytesIO()
            plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
            plt.close()
            buf.seek(0)
            chart_b64 = base64.b64encode(buf.read()).decode()

            # Generate small equity-only thumbnail
            thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
            bt._apply_dark_theme(thumb_fig, [thumb_ax], p.theme)
            thumb_ax.plot(best["equity"].index, best["equity"], color=t["blue"], linewidth=1.5)
            if show_ratio:
                thumb_ax.plot(best["buyhold"].index, best["buyhold"], color=t["muted"], linewidth=1, alpha=0.7)
            if p.sizing == "fixed":
                thumb_ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            else:
                thumb_ax.set_yscale("log")
                thumb_ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            thumb_ax.grid(True, which="major", alpha=0.3, color=t["grid"])
            thumb_ax.tick_params(labelsize=7)
            thumb_ax.set_xlabel("")
            thumb_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            thumb_ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
            plt.tight_layout()
            thumb_buf = BytesIO()
            plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
            plt.close()
            thumb_buf.seek(0)
            thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

    price_json = _series_to_lw_json(df_price_all["close"]) if best else None
    if is_oscillator:
        ind1_json = "[]"
        ind2_json = "[]"
    elif best:
        # Compute indicators on full price data for live chart (extends beyond backtest end_date)
        _lc_ind2, _ = bt.compute_indicator_from_spec(df_price_all, best["ind2_name"], best.get("ind2_period"))
        _lc_ind2 = _lc_ind2[_lc_ind2.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
        ind2_json = _series_to_lw_json(_lc_ind2)
        if best.get("ind1_name") != "price":
            _lc_ind1, _ = bt.compute_indicator_from_spec(df_price_all, best["ind1_name"], best.get("ind1_period"))
            _lc_ind1 = _lc_ind1[_lc_ind1.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
            ind1_json = _series_to_lw_json(_lc_ind1)
        else:
            ind1_json = "[]"
    else:
        ind1_json = "[]"
        ind2_json = "[]"
    return _render_main(p, chart=chart_b64, best=best, table_rows=table_rows, col_header=col_header,
                        hide_buyhold=(p.exposure == "short-cash"),
                        ls_breakdown=long_short_breakdown,
                        equity_top=equity_top if best else 0.7, equity_bottom=equity_bottom if best else 1.0,
                        thumb_b64=thumb_b64 if best else '',
                        price_json=price_json, ind1_json=ind1_json, ind2_json=ind2_json,
                        ind1_label=best.get("ind1_label", "") if best else "", ind2_label=best.get("ind2_label", "") if best else "")


# --- Community / Save / Publish Templates ---

COMMUNITY_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <title>{{ page_title }} — Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --bg-deep: #080a10; --bg-base: #0f1117; --bg-surface: #161922; --bg-elevated: #1c2030;
            --border: #252a3a; --border-hover: #3a4060; --text: #e8eaf0; --text-muted: #8890a4; --text-dim: #555d74;
            --accent: #f7931a; --accent-hover: #ffa940; --accent-glow: rgba(247, 147, 26, 0.15);
            --green: #34d399; --blue: #6495ED;
        }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247, 147, 26, 0.06), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none; z-index: 0;
        }
        [data-theme="light"] body::before {
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(217, 119, 6, 0.04), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(58, 111, 216, 0.03), transparent);
        }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }
        .community-layout { display: grid; grid-template-columns: 1fr 320px; gap: 16px; align-items: start; }
        .community-layout .panel-main { min-width: 0; }
        .community-layout .panel-sidebar { position: sticky; top: 20px; max-height: calc(100vh - 40px); overflow-y: auto; }
        .community-layout .panel-sidebar::-webkit-scrollbar { width: 4px; }
        .community-layout .panel-sidebar::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
        @media (max-width: 960px) { .community-layout { grid-template-columns: 1fr; } .community-layout .panel-sidebar { position: static; max-height: none; } }
        .header { text-align: center; margin-bottom: 32px; position: relative; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; border-radius: 0; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border-radius: 0; border: 1px solid var(--border); border-left: none; }
        .auth-buttons { position: absolute; top: 0; right: 0; display: flex; gap: 10px; align-items: center; }
        .auth-btn { display: inline-block; padding: 10px 24px; border-radius: 8px; font-weight: 700; font-size: 0.9em; text-decoration: none; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; cursor: pointer; }
        .auth-btn-login { background: var(--accent); color: #fff; border: 2px solid var(--accent); }
        .auth-btn-login:hover { background: #e08a1a; border-color: #e08a1a; }
        .auth-btn-signup { background: var(--accent); color: #fff; border: 2px solid var(--accent); }
        .auth-btn-signup:hover { background: #e08a1a; border-color: #e08a1a; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; position: relative; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        .nav-right-group { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; z-index: 9999; }
        /* Theme toggle */
        .theme-toggle { background: none; border: 1px solid var(--border); cursor: pointer; color: var(--text-muted); padding: 7px; border-radius: 8px; transition: all 0.2s ease; display: flex; align-items: center; justify-content: center; }
        .theme-toggle:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border-hover); }
        .theme-toggle svg { width: 16px; height: 16px; }
        .theme-toggle .icon-sun { display: none; }
        .theme-toggle .icon-moon { display: block; }
        [data-theme="light"] .theme-toggle .icon-sun { display: block; }
        [data-theme="light"] .theme-toggle .icon-moon { display: none; }
        .notif-bell-wrap { position: relative; }
        .notif-bell { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 8px; border-radius: 8px; position: relative; transition: all 0.2s ease; }
        .notif-bell:hover { color: var(--text); background: var(--bg-elevated); }
        .notif-badge { position: absolute; top: 2px; right: 2px; background: #e74c3c; color: #fff; font-size: 0.65em; font-weight: 700; min-width: 16px; height: 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; padding: 0 4px; font-family: 'JetBrains Mono', monospace; }
        .notif-badge.hidden { display: none; }
        .notif-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 340px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; }
        .notif-dropdown.hidden { display: none; }
        .notif-dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.85em; color: var(--text); }
        .notif-list { max-height: 320px; overflow-y: auto; }
        .notif-item { display: block; padding: 12px 16px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); font-size: 0.82em; transition: background 0.15s ease; cursor: pointer; }
        .notif-item.notif-unread { background: rgba(100,149,237,0.08); border-left: 3px solid var(--accent); }
        .notif-item.notif-read { opacity: 0.55; }
        .notif-item:hover { background: var(--bg-elevated); opacity: 1; }
        .notif-item:last-child { border-bottom: none; }
        .notif-item-text { line-height: 1.4; }
        .notif-item-text strong { color: var(--accent); font-weight: 600; }
        .notif-item-time { color: var(--text-dim); font-size: 0.78em; margin-top: 4px; }
        .notif-empty { padding: 24px 16px; text-align: center; color: var(--text-dim); font-size: 0.82em; }
        /* Avatar */
        .avatar-wrap { position: relative; }
        .avatar-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 50%; transition: all 0.2s ease; }
        .avatar-btn:hover { background: var(--bg-elevated); }
        .avatar-img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); display: block; }
        .avatar-initials { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 700; color: #fff; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 220px; background: var(--bg-surface, var(--bg-base)); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; padding: 6px 0; }
        .avatar-dropdown.hidden { display: none; }
        .avatar-dropdown-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; color: var(--text-muted); text-decoration: none; font-size: 0.82em; font-weight: 500; transition: all 0.15s ease; font-family: 'DM Sans', sans-serif; }
        .avatar-dropdown-item:hover { background: var(--bg-elevated); color: var(--text); }
        .avatar-dropdown-item svg { width: 16px; height: 16px; flex-shrink: 0; }
        .avatar-dropdown-divider { height: 1px; background: var(--border); margin: 4px 0; }
        .avatar-dropdown-logout { color: #e74c3c; }
        .avatar-dropdown-logout:hover { background: rgba(231,76,60,0.1); color: #e74c3c; }
        /* Small avatars for cards and comments */
        .card-avatar-img { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; margin-right: 4px; }
        .card-avatar-initials { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 0.6em; font-weight: 700; color: #fff; vertical-align: middle; margin-right: 4px; }
        .comment-avatar-img { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; }
        .comment-avatar-initials { width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.65em; font-weight: 700; color: #fff; flex-shrink: 0; }
        .panel { background: var(--bg-surface); border-radius: 16px; padding: 24px; border: 1px solid var(--border); }
        .page-title { font-size: 1.4em; font-weight: 700; margin-bottom: 6px; }
        .page-subtitle { font-size: 0.85em; color: var(--text-muted); margin-bottom: 20px; }
        .sort-tabs { display: flex; gap: 4px; margin-bottom: 20px; }
        .sort-tab { padding: 6px 16px; border-radius: 8px; font-size: 0.8em; font-weight: 500; color: var(--text-muted); text-decoration: none; border: 1px solid transparent; transition: all 0.2s ease; }
        .sort-tab:hover { color: var(--text); background: var(--bg-elevated); }
        .sort-tab.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        .asset-section { margin-bottom: 28px; }
        .asset-section-header { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--border); }
        .asset-section-logo { width: 36px; height: 36px; object-fit: contain; border-radius: 50%; background: var(--bg-deep); }
        .asset-section-title { font-size: 1.15em; font-weight: 700; color: var(--text); flex: 1; }
        .section-reorder-controls { display: flex; gap: 4px; margin-left: auto; }
        .backtest-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
        .backtest-card-wrapper { position: relative; }
        .backtest-card-wrapper.locked .backtest-card { pointer-events: none; }
        .backtest-card-wrapper.locked .backtest-card-thumb { filter: blur(8px); transition: filter 0.3s ease; }
        .backtest-card-wrapper.locked .backtest-card-metrics { filter: blur(6px); }
        .backtest-card-wrapper.locked .backtest-card-desc { filter: blur(4px); }
        .locked-overlay { display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 5; cursor: pointer; border-radius: 14px; background: rgba(22,25,34,0.3); align-items: center; justify-content: center; flex-direction: column; gap: 8px; transition: background 0.2s ease; }
        .backtest-card-wrapper.locked .locked-overlay, .collection-card-wrapper.locked .locked-overlay { display: flex; }
        .locked-overlay:hover { background: rgba(22,25,34,0.5); }
        .locked-overlay svg { width: 32px; height: 32px; color: var(--text-muted); transition: transform 0.3s ease; }
        .locked-overlay.shake svg { animation: lockShake 0.5s ease; }
        .locked-overlay span { font-size: 0.8em; font-weight: 600; color: var(--text-muted); letter-spacing: 0.05em; text-transform: uppercase; }
        @keyframes lockShake { 0%,100% { transform: translateX(0) rotate(0); } 15% { transform: translateX(-4px) rotate(-5deg); } 30% { transform: translateX(4px) rotate(5deg); } 45% { transform: translateX(-3px) rotate(-3deg); } 60% { transform: translateX(3px) rotate(3deg); } 75% { transform: translateX(-1px) rotate(-1deg); } }
        .reorder-controls { position: absolute; top: 8px; right: 8px; z-index: 10; display: flex; flex-direction: column; gap: 2px; opacity: 0; transition: opacity 0.2s ease; }
        .backtest-card-wrapper:hover .reorder-controls, .collection-card-wrapper:hover .reorder-controls { opacity: 1; }
        .reorder-btn { width: 28px; height: 28px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg-surface); color: var(--text-muted); cursor: pointer; font-size: 0.7em; display: flex; align-items: center; justify-content: center; transition: all 0.15s ease; }
        .reorder-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
        .backtest-card { display: block; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px; transition: all 0.2s ease; cursor: pointer; text-decoration: none; color: inherit; }
        .backtest-card:hover { border-color: var(--border-hover); transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
        .backtest-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
        .backtest-card-asset-logo { width: 22px; height: 22px; object-fit: contain; border-radius: 50%; background: var(--bg-deep); flex-shrink: 0; }
        .backtest-card-asset-fallback { width: 22px; height: 22px; border-radius: 50%; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.65em; color: var(--text-muted); flex-shrink: 0; }
        .backtest-card-head-text { flex: 1; min-width: 0; }
        .backtest-card-title { font-size: 1em; font-weight: 600; color: var(--text); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.3; }
        .backtest-card-asset-name { font-size: 0.75em; color: var(--text-muted); }
        .backtest-card-mode-icon { color: var(--text-dim); flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 8px; background: var(--bg-deep); }
        .backtest-card-desc { font-size: 0.8em; color: var(--text-muted); margin-bottom: 10px; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
        .backtest-card-params { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
        .backtest-card-tag { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 6px; background: var(--bg-deep); border: 1px solid var(--border); font-size: 0.7em; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; white-space: nowrap; }
        .backtest-card-tag svg { width: 12px; height: 12px; opacity: 0.6; }
        .backtest-card-thumb { width: 100%; height: 140px; object-fit: cover; border-radius: 8px; margin-bottom: 10px; border: 1px solid var(--border); }
        .backtest-card-metrics { display: flex; gap: 12px; margin-bottom: 10px; }
        .card-metric { flex: 1; padding: 8px 10px; background: var(--bg-deep); border: 1px solid var(--border); border-radius: 8px; text-align: center; }
        .card-metric-label { display: block; font-size: 0.65em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); margin-bottom: 2px; }
        .card-metric-val { display: block; font-size: 0.95em; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .card-metric-val.positive { color: var(--green); }
        .card-metric-val.negative { color: #ef4444; }
        .card-metric-vs { display: block; font-size: 0.6em; color: #ffffff; font-family: 'JetBrains Mono', monospace; margin-top: 1px; }
        .backtest-card-footer { display: flex; align-items: center; justify-content: space-between; font-size: 0.75em; color: var(--text-dim); }
        .backtest-card-footer .engagement { display: flex; gap: 12px; }
        .backtest-card-footer .engagement span { display: flex; align-items: center; gap: 3px; }
        .card-author { display: flex; align-items: center; gap: 6px; }
        .card-author-avatar { width: 22px; height: 22px; border-radius: 50%; object-fit: cover; }
        .card-author-initials { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 0.7em; font-weight: 700; flex-shrink: 0; }
        .card-author-name { font-weight: 600; color: var(--text-muted); }
        .card-author-sep { color: var(--text-dim); }
        .card-author-time { color: var(--text-dim); }
        .backtest-card-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
        .badge-featured { background: rgba(247,147,26,0.15); color: var(--accent); }
        .badge-community { background: rgba(100,149,237,0.15); color: var(--blue); }
        .badge-private { background: rgba(136,144,164,0.15); color: var(--text-muted); }
        .badge-collection { background: rgba(139,92,246,0.15); color: #8b5cf6; }
        .collection-card-wrapper { position: relative; }
        .collection-card-wrapper::before { content: ''; position: absolute; top: 4px; left: 4px; right: -4px; bottom: -4px; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 14px; z-index: -1; }
        .collection-card-wrapper::after { content: ''; position: absolute; top: 8px; left: 8px; right: -8px; bottom: -8px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 14px; z-index: -2; opacity: 0.5; }
        .collection-card { display: block; background: var(--bg-surface); border: 1px solid var(--border); border-left: 3px solid #8b5cf6; border-radius: 14px; padding: 18px; transition: all 0.2s ease; cursor: pointer; text-decoration: none; color: inherit; position: relative; z-index: 1; }
        .collection-card:hover { border-color: var(--border-hover); border-left-color: #8b5cf6; transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
        .collection-card-count { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 6px; background: rgba(139,92,246,0.1); border: 1px solid rgba(139,92,246,0.2); font-size: 0.7em; color: #8b5cf6; font-family: 'JetBrains Mono', monospace; white-space: nowrap; }
        .collection-card-count svg { width: 12px; height: 12px; }
        .collection-yt-indicator { position: absolute; top: 12px; right: 12px; width: 24px; height: 24px; background: rgba(255,0,0,0.9); border-radius: 50%; display: flex; align-items: center; justify-content: center; z-index: 2; box-shadow: 0 1px 4px rgba(0,0,0,0.3); }
        .collection-yt-indicator svg { width: 16px; height: 16px; color: #fff; margin-left: 1px; }
        .collection-ct-indicator { position: absolute; top: 12px; right: 12px; height: 24px; padding: 0 8px; background: rgba(16,185,129,0.9); border-radius: 4px; display: flex; align-items: center; gap: 4px; z-index: 2; font-size: 0.65em; font-weight: 600; color: #fff; white-space: nowrap; }
        .collection-ct-indicator svg { width: 12px; height: 12px; color: #fff; }
        .collection-yt-indicator + .collection-ct-indicator, .collection-ct-indicator.has-yt { right: 42px; }
        .coll-thumb-grid { display: grid; gap: 4px; border-radius: 8px; overflow: hidden; margin-bottom: 10px; border: 1px solid var(--border); }
        .coll-thumb-grid.thumbs-1 { grid-template-columns: 1fr; }
        .coll-thumb-grid.thumbs-2 { grid-template-columns: 1fr 1fr; }
        .coll-thumb-grid.thumbs-3 { grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }
        .coll-thumb-grid.thumbs-4 { grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }
        .coll-thumb-grid.thumbs-3 img:first-child { grid-row: 1 / 3; }
        .coll-thumb-grid img { width: 100%; height: 100%; object-fit: cover; display: block; }
        .coll-thumb-grid.thumbs-1 img { height: 140px; }
        .coll-thumb-grid.thumbs-2 img { height: 100px; }
        .coll-thumb-grid.thumbs-3 img { height: 68px; }
        .coll-thumb-grid.thumbs-3 img:first-child { height: 100%; }
        .coll-thumb-grid.thumbs-4 img { height: 68px; }
        .coll-top-row { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
        .coll-top-row .backtest-card-badge { position: static; margin: 0; }
        .coll-asset-logos { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
        .coll-asset-logo { width: 22px; height: 22px; border-radius: 50%; object-fit: contain; background: var(--bg-deep); border: 1px solid var(--border); }
        .coll-asset-fallback { width: 22px; height: 22px; border-radius: 50%; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.55em; color: var(--text-muted); border: 1px solid var(--border); }
        .pagination { display: flex; justify-content: center; gap: 8px; margin-top: 24px; }
        .pagination a { padding: 8px 14px; border-radius: 8px; border: 1px solid var(--border); color: var(--text-muted); text-decoration: none; font-size: 0.82em; transition: all 0.2s ease; }
        .pagination a:hover { border-color: var(--border-hover); color: var(--text); }
        .pagination a.active { border-color: var(--accent); color: var(--accent); }
        .cta-banner { background: linear-gradient(135deg, rgba(247,147,26,0.1), rgba(100,149,237,0.1)); border: 1px solid var(--accent); border-radius: 12px; padding: 20px; text-align: center; margin-top: 20px; }
        .cta-banner h3 { font-size: 1em; margin-bottom: 6px; }
        .cta-banner p { font-size: 0.85em; color: var(--text-muted); margin-bottom: 12px; }
        .cta-banner a { display: inline-block; padding: 10px 24px; background: var(--accent); color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 0.85em; }
        .empty-state { text-align: center; padding: 60px 20px; color: var(--text-muted); }
        .empty-state h3 { font-size: 1.1em; margin-bottom: 8px; color: var(--text); }
        /* Recent comments */
        .recent-comments { margin-top: 24px; }
        .recent-comments-title { font-size: 1em; font-weight: 600; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
        .rc-item { display: flex; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); transition: background 0.15s ease; }
        .rc-item:last-child { border-bottom: none; }
        .rc-item:hover { background: var(--bg-elevated); margin: 0 -12px; padding: 12px; border-radius: 8px; border-bottom-color: transparent; }
        .rc-avatar { flex-shrink: 0; }
        .rc-avatar img { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; }
        .rc-avatar-initials { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.65em; font-weight: 700; color: #fff; }
        .rc-content { flex: 1; min-width: 0; }
        .rc-header { font-size: 0.78em; color: var(--text-dim); margin-bottom: 4px; }
        .rc-header strong { color: var(--text); font-weight: 600; }
        .rc-body { font-size: 0.82em; color: var(--text-muted); line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .rc-backtest { font-size: 0.75em; color: var(--text-dim); margin-top: 3px; }
        .rc-backtest em { color: var(--blue); }
        .action-btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-muted); cursor: pointer; font-size: 0.82em; font-weight: 500; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; text-decoration: none; }
        .action-btn:hover { border-color: var(--border-hover); color: var(--text); }
        .action-btn.primary { border-color: var(--accent); color: var(--accent); }
        .action-btn.liked { color: #ef4444; border-color: #ef4444; }
        .action-btn.danger { border-color: #ef4444; color: #ef4444; }
        .action-btn.danger:hover { background: rgba(239,68,68,0.1); }
        .hidden { display: none !important; }

        /* ── Mobile responsive ── */
        @media (max-width: 600px) {
            .auth-buttons { position: static; display: flex; justify-content: center; margin-bottom: 12px; }
            .auth-btn { padding: 8px 16px; font-size: 0.8em; }
        }
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
            .nav-bar { flex-wrap: wrap; }
            .nav-link { padding: 6px 10px; font-size: 0.75em; }
        }
        @media (max-width: 400px) {
            .backtest-grid { grid-template-columns: 1fr; }
            .notif-dropdown { width: calc(100vw - 32px); right: -8px; }
        }
    </style>
</head>
<body>
<div class="container">
""" + NAV_HTML + """
    {% if nav_active == 'community' and recent_comments|default(none) and recent_comments|length > 0 %}
    <div class="community-layout">
    {% endif %}
    <div class="panel{% if nav_active == 'community' and recent_comments|default(none) and recent_comments|length > 0 %} panel-main{% endif %}">
        {% if nav_active != 'featured' %}
        <h2 class="page-title">{{ page_title }}</h2>
        <p class="page-subtitle">{{ page_subtitle }}</p>
        {% else %}
        <div style="display:flex;justify-content:flex-end;margin-bottom:8px">
            <a href="/backtester" class="action-btn" style="background:linear-gradient(135deg,var(--accent),var(--accent-hover));color:#fff;border-color:transparent;padding:10px 20px;font-size:0.85em;white-space:nowrap">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                Create Backtest
            </a>
        </div>
        {% endif %}

        {% if show_sort|default(true) and not asset_sections|default(none) %}
        <div class="sort-tabs">
            <a href="?sort=newest&page=1" class="sort-tab {{ 'active' if sort=='newest' }}">Newest</a>
            <a href="?sort=popular&page=1" class="sort-tab {{ 'active' if sort=='popular' }}">Most Liked</a>
        </div>
        {% endif %}

        {% if asset_sections|default(none) %}
        {# Grouped by asset view (featured page) #}
        {% for section in asset_sections %}
        <div class="asset-section" data-asset="{{ section.asset }}">
            <div class="asset-section-header">
                {% if section.logo %}<img class="asset-section-logo" src="/static/logos/{{ section.logo }}" alt="{{ section.display }}">{% endif %}
                <h3 class="asset-section-title">{{ section.display }}</h3>
                {% if is_admin|default(false) %}
                <div class="section-reorder-controls">
                    <button class="reorder-btn" onclick="moveSection('{{ section.asset }}', -1)" title="Move section up">&#9650;</button>
                    <button class="reorder-btn" onclick="moveSection('{{ section.asset }}', 1)" title="Move section down">&#9660;</button>
                </div>
                {% endif %}
            </div>
            <div class="backtest-grid" id="backtest-grid-{{ section.asset }}">
                {% for bt in section.backtests %}
                {% if bt._is_collection|default(false) %}
                {# Collection card inline #}
                <div class="collection-card-wrapper{{ ' locked' if not is_authenticated and not loop.first else '' }}" data-coll-id="{{ bt.id }}">
                    {% if not is_authenticated and not loop.first %}
                    <div class="locked-overlay" onclick="shakeLock(this)">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                        <span>Locked</span>
                    </div>
                    {% endif %}
                    {% if is_admin|default(false) %}
                    <div class="reorder-controls">
                        <button class="reorder-btn" onclick="event.preventDefault();moveCollection('{{ bt.id }}', -1)" title="Move up">&#9650;</button>
                        <button class="reorder-btn" onclick="event.preventDefault();moveCollection('{{ bt.id }}', 1)" title="Move down">&#9660;</button>
                    </div>
                    {% endif %}
                    {% if bt.youtube_url %}
                    <div class="collection-yt-indicator" title="Includes video"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="8 5 19 12 8 19"/></svg></div>
                    {% endif %}
                    {% if bt.copy_trading_url %}
                    <div class="collection-ct-indicator{{ ' has-yt' if bt.youtube_url else '' }}" title="Copy trading available"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>Copy Trading</div>
                    {% endif %}
                    <a class="collection-card" href="{{ '/collection/' ~ bt.id if is_authenticated or loop.first else '#' }}">
                        <div class="coll-top-row">
                            <span class="backtest-card-badge badge-collection">Strategy</span>
                            {% if bt._assets %}
                            {% for asset_name, asset_logo in bt._assets %}
                            {% if asset_logo %}<img class="coll-asset-logo" src="/static/logos/{{ asset_logo }}" alt="{{ asset_name }}" title="{{ asset_name|capitalize }}">{% else %}<span class="coll-asset-fallback" title="{{ asset_name|capitalize }}">{{ asset_name[:1]|upper }}</span>{% endif %}
                            {% endfor %}
                            {% endif %}
                        </div>
                        <div class="backtest-card-head">
                            <div class="backtest-card-head-text">
                                <div class="backtest-card-title">{{ bt.title }}</div>
                            </div>
                        </div>
                        {% if bt._thumbnails %}
                        <div class="coll-thumb-grid thumbs-{{ bt._thumbnails|length }}">
                            {% for thumb in bt._thumbnails %}
                            <img src="{{ thumb }}" alt="Preview">
                            {% endfor %}
                        </div>
                        {% endif %}
                        {% if bt.description %}<div class="backtest-card-desc">{{ bt.description[:250] }}</div>{% endif %}
                        <div class="backtest-card-params">
                            <span class="collection-card-count"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> {{ bt._backtest_count }} backtest{{ 's' if bt._backtest_count != 1 }}</span>
                        </div>
                        <div class="backtest-card-footer">
                            <div class="card-author">
                                {% if bt._avatar %}
                                <img src="/static/avatars/{{ bt._avatar }}" class="card-author-avatar" alt="">
                                {% else %}
                                <span class="card-author-initials" style="background:{{ bt._avatar_color }}">{{ bt._initial }}</span>
                                {% endif %}
                                <span class="card-author-name">{{ bt._display_name }}</span>
                                <span class="card-author-sep">·</span>
                                <span class="card-author-time">{{ time_ago(bt.created_at) }}</span>
                            </div>
                            <div class="engagement">
                                <span>{{ bt.views_count or 0 }} views</span>
                            </div>
                        </div>
                    </a>
                </div>
                {% else %}
                {# Regular backtest card #}
                <div class="backtest-card-wrapper{{ ' locked' if not is_authenticated and not loop.first else '' }}" data-id="{{ bt.id }}">
                {% if not is_authenticated and not loop.first %}
                <div class="locked-overlay" onclick="shakeLock(this)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                    <span>Locked</span>
                </div>
                {% endif %}
                {% if is_admin|default(false) %}
                <div class="reorder-controls">
                    <button class="reorder-btn" onclick="event.preventDefault();moveCard('{{ bt.id }}', -1)" title="Move up">&#9650;</button>
                    <button class="reorder-btn" onclick="event.preventDefault();moveCard('{{ bt.id }}', 1)" title="Move down">&#9660;</button>
                </div>
                {% endif %}
                <a class="backtest-card" href="{{ '/backtest/' ~ bt.id if is_authenticated or loop.first else '#' }}">
                    <div class="backtest-card-head">
                        {% if bt._asset_logo %}<img class="backtest-card-asset-logo" src="/static/logos/{{ bt._asset_logo }}" alt="{{ bt._asset_display }}">{% endif %}
                        <div class="backtest-card-head-text">
                            <div class="backtest-card-title">{{ bt.title|title if bt.title else 'Untitled Backtest' }}</div>
                        </div>
                        <div class="backtest-card-mode-icon" title="{{ bt._mode_label }}">{{ bt._mode_svg|safe }}</div>
                    </div>
                    {% if bt.thumbnail %}<img class="backtest-card-thumb" src="{{ bt.thumbnail }}" alt="Chart">{% endif %}
                    {% if bt.description %}<div class="backtest-card-desc">{{ bt.description[:250] }}</div>{% endif %}
                    <div class="backtest-card-params">
                        <span class="backtest-card-tag" title="Mode">{{ bt._mode_label }}</span>
                        <span class="backtest-card-tag" title="Strategy">{{ bt._strategy }}</span>
                        {% if bt._leverage %}<span class="backtest-card-tag" title="Leverage (Long/Short)">{{ bt._leverage }}</span>{% endif %}
                        {% if bt._start_date %}<span class="backtest-card-tag" title="Start date">{{ bt._start_date }}</span>{% endif %}
                        {% if bt._exposure != 'long-cash' %}<span class="backtest-card-tag" title="Exposure">{{ bt._exposure }}</span>{% endif %}
                    </div>
                    {% if bt._apr %}
                    <div class="backtest-card-metrics">
                        <div class="card-metric">
                            <span class="card-metric-label">APR</span>
                            <span class="card-metric-val {{ 'positive' if bt._apr|float > 0 else 'negative' }}">{{ bt._apr }}%</span>
                            <span class="card-metric-vs">vs {{ bt._apr_bh }}% B&H</span>
                        </div>
                        <div class="card-metric">
                            <span class="card-metric-label">Max DD</span>
                            <span class="card-metric-val negative">{{ bt._max_dd }}%</span>
                            <span class="card-metric-vs">vs {{ bt._max_dd_bh }}% B&H</span>
                        </div>
                    </div>
                    {% endif %}
                    <div class="backtest-card-footer">
                        <div class="card-author">
                            {% if bt._avatar %}
                            <img src="/static/avatars/{{ bt._avatar }}" class="card-author-avatar" alt="">
                            {% else %}
                            <span class="card-author-initials" style="background:{{ bt._avatar_color }}">{{ bt._initial }}</span>
                            {% endif %}
                            <span class="card-author-name">{{ bt._display_name }}</span>
                            <span class="card-author-sep">·</span>
                            <span class="card-author-time">{{ time_ago(bt.created_at) }}</span>
                        </div>
                        <div class="engagement">
                            <span>♥ {{ bt.likes_count }}</span>
                            <span>💬 {{ bt.comments_count }}</span>
                            <span>👁 {{ bt.views_count or 0 }}</span>
                        </div>
                    </div>
                </a>
                </div>
                {% endif %}
                {% endfor %}
            </div>
        </div>
        {% endfor %}

        {% elif backtests %}
        <div class="backtest-grid" id="backtest-grid">
            {% for bt in backtests %}
            {% if bt._is_collection|default(false) %}
            {# Collection card inline #}
            <div class="collection-card-wrapper{{ ' locked' if not is_authenticated and not loop.first else '' }}" data-coll-id="{{ bt.id }}">
                {% if not is_authenticated and not loop.first %}
                <div class="locked-overlay" onclick="shakeLock(this)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                    <span>Locked</span>
                </div>
                {% endif %}
                {% if bt.youtube_url %}
                <div class="collection-yt-indicator" title="Includes video"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="8 5 19 12 8 19"/></svg></div>
                {% endif %}
                {% if bt.copy_trading_url %}
                <div class="collection-ct-indicator{{ ' has-yt' if bt.youtube_url else '' }}" title="Copy trading available"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>Copy Trading</div>
                {% endif %}
                <a class="collection-card" href="{{ '/collection/' ~ bt.id if is_authenticated or loop.first else '#' }}">
                    <div class="coll-top-row">
                        <span class="backtest-card-badge badge-collection">Strategy</span>
                        {% if bt._assets %}
                        {% for asset_name, asset_logo in bt._assets %}
                        {% if asset_logo %}<img class="coll-asset-logo" src="/static/logos/{{ asset_logo }}" alt="{{ asset_name }}" title="{{ asset_name|capitalize }}">{% else %}<span class="coll-asset-fallback" title="{{ asset_name|capitalize }}">{{ asset_name[:1]|upper }}</span>{% endif %}
                        {% endfor %}
                        {% endif %}
                    </div>
                    <div class="backtest-card-head">
                        <div class="backtest-card-head-text">
                            <div class="backtest-card-title">{{ bt.title }}</div>
                        </div>
                    </div>
                    {% if bt._thumbnails %}
                    <div class="coll-thumb-grid thumbs-{{ bt._thumbnails|length }}">
                        {% for thumb in bt._thumbnails %}
                        <img src="{{ thumb }}" alt="Preview">
                        {% endfor %}
                    </div>
                    {% endif %}
                    {% if bt.description %}<div class="backtest-card-desc">{{ bt.description[:250] }}</div>{% endif %}
                    <div class="backtest-card-params">
                        <span class="collection-card-count"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> {{ bt._backtest_count }} backtest{{ 's' if bt._backtest_count != 1 }}</span>
                    </div>
                    <div class="backtest-card-footer">
                        <div class="card-author">
                            {% if bt._avatar %}
                            <img src="/static/avatars/{{ bt._avatar }}" class="card-author-avatar" alt="">
                            {% else %}
                            <span class="card-author-initials" style="background:{{ bt._avatar_color }}">{{ bt._initial }}</span>
                            {% endif %}
                            <span class="card-author-name">{{ bt._display_name }}</span>
                            <span class="card-author-sep">·</span>
                            <span class="card-author-time">{{ time_ago(bt.created_at) }}</span>
                        </div>
                        <div class="engagement">
                            <span>{{ bt.views_count or 0 }} views</span>
                        </div>
                    </div>
                </a>
            </div>
            {% else %}
            {# Regular backtest card #}
            <div class="backtest-card-wrapper{{ ' locked' if not is_authenticated and not loop.first else '' }}" data-id="{{ bt.id }}">
            {% if not is_authenticated and not loop.first %}
            <div class="locked-overlay" onclick="shakeLock(this)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                <span>Locked</span>
            </div>
            {% endif %}
            {% if is_admin|default(false) and nav_active == 'featured' %}
            <div class="reorder-controls">
                <button class="reorder-btn" onclick="event.preventDefault();moveCard('{{ bt.id }}', -1)" title="Move up">&#9650;</button>
                <button class="reorder-btn" onclick="event.preventDefault();moveCard('{{ bt.id }}', 1)" title="Move down">&#9660;</button>
            </div>
            {% endif %}
            <a class="backtest-card" href="{{ '/backtest/' ~ bt.id if is_authenticated or loop.first else '#' }}">
                {% if bt.visibility == 'community' %}<span class="backtest-card-badge badge-community">Community</span>{% endif %}
                {% if bt.visibility == 'private' %}<span class="backtest-card-badge badge-private">Private</span>{% endif %}
                <div class="backtest-card-head">
                    {% if bt._asset_logo %}<img class="backtest-card-asset-logo" src="/static/logos/{{ bt._asset_logo }}" alt="{{ bt._asset_display }}">{% else %}<div class="backtest-card-asset-fallback">{{ bt._asset_display[:1] }}</div>{% endif %}
                    <div class="backtest-card-head-text">
                        <div class="backtest-card-title">{{ bt.title|title if bt.title else 'Untitled Backtest' }}</div>
                        <div class="backtest-card-asset-name">{{ bt._asset_display }}</div>
                    </div>
                    <div class="backtest-card-mode-icon" title="{{ bt._mode_label }}">{{ bt._mode_svg|safe }}</div>
                </div>
                {% if bt.thumbnail %}<img class="backtest-card-thumb" src="{{ bt.thumbnail }}" alt="Chart">{% endif %}
                {% if bt.description %}<div class="backtest-card-desc">{{ bt.description[:250] }}</div>{% endif %}
                <div class="backtest-card-params">
                    <span class="backtest-card-tag" title="Mode">{{ bt._mode_label }}</span>
                    <span class="backtest-card-tag" title="Strategy">{{ bt._strategy }}</span>
                    {% if bt._leverage %}<span class="backtest-card-tag" title="Leverage (Long/Short)">{{ bt._leverage }}</span>{% endif %}
                    {% if bt._start_date %}<span class="backtest-card-tag" title="Start date">{{ bt._start_date }}</span>{% endif %}
                    {% if bt._exposure != 'long-cash' %}<span class="backtest-card-tag" title="Exposure">{{ bt._exposure }}</span>{% endif %}
                </div>
                {% if bt._apr %}
                <div class="backtest-card-metrics">
                    <div class="card-metric">
                        <span class="card-metric-label">APR</span>
                        <span class="card-metric-val {{ 'positive' if bt._apr|float > 0 else 'negative' }}">{{ bt._apr }}%</span>
                        <span class="card-metric-vs">vs {{ bt._apr_bh }}% B&H</span>
                    </div>
                    <div class="card-metric">
                        <span class="card-metric-label">Max DD</span>
                        <span class="card-metric-val negative">{{ bt._max_dd }}%</span>
                        <span class="card-metric-vs">vs {{ bt._max_dd_bh }}% B&H</span>
                    </div>
                </div>
                {% endif %}
                <div class="backtest-card-footer">
                    <div class="card-author">
                        {% if bt._avatar %}
                        <img src="/static/avatars/{{ bt._avatar }}" class="card-author-avatar" alt="">
                        {% else %}
                        <span class="card-author-initials" style="background:{{ bt._avatar_color }}">{{ bt._initial }}</span>
                        {% endif %}
                        <span class="card-author-name">{{ bt._display_name }}</span>
                        <span class="card-author-sep">·</span>
                        <span class="card-author-time">{{ time_ago(bt.created_at) }}</span>
                    </div>
                    <div class="engagement">
                        <span>♥ {{ bt.likes_count }}</span>
                        <span>💬 {{ bt.comments_count }}</span>
                        <span>👁 {{ bt.views_count or 0 }}</span>
                    </div>
                </div>
            </a>
            </div>
            {% endif %}
            {% endfor %}
        </div>

        {% if total_pages > 1 %}
        <div class="pagination">
            {% if page > 1 %}<a href="?sort={{ sort }}&page={{ page - 1 }}">← Prev</a>{% endif %}
            {% for pg in range(1, total_pages + 1) %}
                {% if pg == page %}<a class="active" href="?sort={{ sort }}&page={{ pg }}">{{ pg }}</a>
                {% elif (pg - page)|abs <= 2 or pg == 1 or pg == total_pages %}<a href="?sort={{ sort }}&page={{ pg }}">{{ pg }}</a>{% endif %}
            {% endfor %}
            {% if page < total_pages %}<a href="?sort={{ sort }}&page={{ page + 1 }}">Next →</a>{% endif %}
        </div>
        {% endif %}

        {% else %}
        <div class="empty-state">
            <h3>No backtests yet</h3>
            <p>{{ empty_message|default('Be the first to publish a backtest!') }}</p>
        </div>
        {% endif %}

        {% if not is_authenticated %}
        <div class="cta-banner">
            <h3>Want to run your own backtests?</h3>
            <p>Sign up for a premium membership to access the full backtesting engine.</p>
            <a href="https://the-bitcoin-strategy.com">Get Started</a>
        </div>
        {% endif %}
    </div>

    {% if nav_active == 'community' and recent_comments|default(none) and recent_comments|length > 0 %}
    <div class="panel panel-sidebar">
        <div class="recent-comments">
            <h3 class="recent-comments-title">
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                Recent Comments
            </h3>
            {% for rc in recent_comments %}
            <a class="rc-item" href="/backtest/{{ rc.backtest_id }}#comment-{{ rc.id }}">
                <div class="rc-avatar">
                    {% if rc._avatar %}
                    <img src="/static/avatars/{{ rc._avatar }}" alt="">
                    {% else %}
                    <div class="rc-avatar-initials" style="background:{{ rc._avatar_color }}">{{ rc._initial }}</div>
                    {% endif %}
                </div>
                <div class="rc-content">
                    <div class="rc-header"><strong>{{ rc._display_name }}</strong> · {{ rc._time_ago }}</div>
                    <div class="rc-body">{{ rc.body }}</div>
                    <div class="rc-backtest">on <em>{{ rc.backtest_title or 'Untitled' }}</em></div>
                </div>
            </a>
            {% endfor %}
        </div>
    </div>
    </div>{# close community-layout #}
    {% endif %}
</div>
<script src="/static/js/nav.js"></script>
<script>
function deleteBacktest(backtestId) {
    _swal.fire({
        title: 'Delete this backtest?', icon: 'warning',
        showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/backtest/' + backtestId, { method: 'DELETE' })
        .then(function() { location.reload(); });
    });
}
function featureBacktest(backtestId) {
    fetch('/api/backtest/' + backtestId + '/feature', { method: 'POST' })
    .then(function() { location.reload(); });
}
function saveMixedOrder() {
    // Collect all cards (backtests + collections) across all grids in DOM order
    var items = [];
    document.querySelectorAll('.backtest-card-wrapper[data-id], .collection-card-wrapper[data-coll-id]').forEach(function(el) {
        if (el.dataset.id) items.push({type: 'bt', id: el.dataset.id});
        else if (el.dataset.collId) items.push({type: 'coll', id: el.dataset.collId});
    });
    fetch('/api/reorder-mixed', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ordered_items: items})
    });
}
function moveCard(id, direction) {
    var el = document.querySelector('.backtest-card-wrapper[data-id="' + id + '"]');
    if (!el) return;
    var grid = el.parentElement;
    var siblings = Array.from(grid.children);
    var idx = siblings.indexOf(el);
    if (idx < 0) return;
    var newIdx = idx + direction;
    if (newIdx < 0 || newIdx >= siblings.length) return;
    if (direction < 0) { grid.insertBefore(el, siblings[newIdx]); }
    else { grid.insertBefore(siblings[newIdx], el); }
    saveMixedOrder();
}
function shakeLock(el) {
    el.classList.remove('shake');
    void el.offsetWidth;
    el.classList.add('shake');
    setTimeout(function() { el.classList.remove('shake'); }, 600);
}
function moveSection(asset, direction) {
    var sections = Array.from(document.querySelectorAll('.asset-section'));
    var el = document.querySelector('.asset-section[data-asset="' + asset + '"]');
    if (!el) return;
    var idx = sections.indexOf(el);
    var newIdx = idx + direction;
    if (newIdx < 0 || newIdx >= sections.length) return;
    var parent = el.parentElement;
    if (direction < 0) { parent.insertBefore(el, sections[newIdx]); }
    else { parent.insertBefore(sections[newIdx], el); }
    saveMixedOrder();
}
function moveCollection(id, direction) {
    var el = document.querySelector('.collection-card-wrapper[data-coll-id="' + id + '"]');
    if (!el) return;
    var grid = el.parentElement;
    var siblings = Array.from(grid.children);
    var idx = siblings.indexOf(el);
    if (idx < 0) return;
    var newIdx = idx + direction;
    if (newIdx < 0 || newIdx >= siblings.length) return;
    if (direction < 0) { grid.insertBefore(el, siblings[newIdx]); }
    else { grid.insertBefore(siblings[newIdx], el); }
    saveMixedOrder();
}
</script>
</body>
</html>
"""


DETAIL_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <title>{{ backtest.title|title if backtest.title else 'Backtest' }} — Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --bg-deep: #080a10; --bg-base: #0f1117; --bg-surface: #161922; --bg-elevated: #1c2030;
            --border: #252a3a; --border-hover: #3a4060; --text: #e8eaf0; --text-muted: #8890a4; --text-dim: #555d74;
            --accent: #f7931a; --accent-hover: #ffa940; --accent-glow: rgba(247, 147, 26, 0.15);
            --green: #34d399; --blue: #6495ED;
        }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247, 147, 26, 0.06), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none; z-index: 0;
        }
        [data-theme="light"] body::before {
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(217, 119, 6, 0.04), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(58, 111, 216, 0.03), transparent);
        }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }
        .header { text-align: center; margin-bottom: 32px; position: relative; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .auth-buttons { position: absolute; top: 0; right: 0; display: flex; gap: 10px; align-items: center; }
        .auth-btn { display: inline-block; padding: 10px 24px; border-radius: 8px; font-weight: 700; font-size: 0.9em; text-decoration: none; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; cursor: pointer; }
        .auth-btn-login { background: var(--accent); color: #fff; border: 2px solid var(--accent); }
        .auth-btn-login:hover { background: #e08a1a; border-color: #e08a1a; }
        .auth-btn-signup { background: var(--accent); color: #fff; border: 2px solid var(--accent); }
        .auth-btn-signup:hover { background: #e08a1a; border-color: #e08a1a; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; position: relative; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        /* Theme toggle */
        .theme-toggle { background: none; border: 1px solid var(--border); cursor: pointer; color: var(--text-muted); padding: 7px; border-radius: 8px; transition: all 0.2s ease; display: flex; align-items: center; justify-content: center; }
        .theme-toggle:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border-hover); }
        .theme-toggle svg { width: 16px; height: 16px; }
        .theme-toggle .icon-sun { display: none; }
        .theme-toggle .icon-moon { display: block; }
        [data-theme="light"] .theme-toggle .icon-sun { display: block; }
        [data-theme="light"] .theme-toggle .icon-moon { display: none; }
        .nav-right-group { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; z-index: 9999; }
        .notif-bell-wrap { position: relative; }
        .notif-bell { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 8px; border-radius: 8px; position: relative; transition: all 0.2s ease; }
        .notif-bell:hover { color: var(--text); background: var(--bg-elevated); }
        .notif-badge { position: absolute; top: 2px; right: 2px; background: #e74c3c; color: #fff; font-size: 0.65em; font-weight: 700; min-width: 16px; height: 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; padding: 0 4px; font-family: 'JetBrains Mono', monospace; }
        .notif-badge.hidden { display: none; }
        .notif-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 340px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; }
        .notif-dropdown.hidden { display: none; }
        .notif-dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.85em; color: var(--text); }
        .notif-list { max-height: 320px; overflow-y: auto; }
        .notif-item { display: block; padding: 12px 16px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); font-size: 0.82em; transition: background 0.15s ease; cursor: pointer; }
        .notif-item.notif-unread { background: rgba(100,149,237,0.08); border-left: 3px solid var(--accent); }
        .notif-item.notif-read { opacity: 0.55; }
        .notif-item:hover { background: var(--bg-elevated); opacity: 1; }
        .notif-item:last-child { border-bottom: none; }
        .notif-item-text { line-height: 1.4; }
        .notif-item-text strong { color: var(--accent); font-weight: 600; }
        .notif-item-time { color: var(--text-dim); font-size: 0.78em; margin-top: 4px; }
        .notif-empty { padding: 24px 16px; text-align: center; color: var(--text-dim); font-size: 0.82em; }
        /* Avatar */
        .avatar-wrap { position: relative; }
        .avatar-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 50%; transition: all 0.2s ease; }
        .avatar-btn:hover { background: var(--bg-elevated); }
        .avatar-img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); display: block; }
        .avatar-initials { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 700; color: #fff; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 220px; background: var(--bg-surface, var(--bg-base)); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; padding: 6px 0; }
        .avatar-dropdown.hidden { display: none; }
        .avatar-dropdown-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; color: var(--text-muted); text-decoration: none; font-size: 0.82em; font-weight: 500; transition: all 0.15s ease; font-family: 'DM Sans', sans-serif; }
        .avatar-dropdown-item:hover { background: var(--bg-elevated); color: var(--text); }
        .avatar-dropdown-item svg { width: 16px; height: 16px; flex-shrink: 0; }
        .avatar-dropdown-divider { height: 1px; background: var(--border); margin: 4px 0; }
        .avatar-dropdown-logout { color: #e74c3c; }
        .avatar-dropdown-logout:hover { background: rgba(231,76,60,0.1); color: #e74c3c; }
        /* Small avatars for cards and comments */
        .card-avatar-img { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; margin-right: 4px; }
        .card-avatar-initials { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 0.6em; font-weight: 700; color: #fff; vertical-align: middle; margin-right: 4px; }
        .comment-avatar-img { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; }
        .comment-avatar-initials { width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.65em; font-weight: 700; color: #fff; flex-shrink: 0; }
        .panel { background: var(--bg-surface); border-radius: 16px; padding: 24px; border: 1px solid var(--border); margin-bottom: 16px; }
        .detail-header { margin-bottom: 20px; }
        .detail-title { font-size: 1.3em; font-weight: 700; margin-bottom: 4px; display: inline; }
        .detail-title-row { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
        .copy-link-icon { width: 18px; height: 18px; color: var(--text-dim); cursor: pointer; transition: color 0.2s ease, transform 0.15s ease; flex-shrink: 0; position: relative; top: 1px; }
        .copy-link-icon:hover { color: var(--accent); transform: scale(1.1); }
        .copy-link-icon.copied { color: #22c55e; }
        .detail-meta { font-size: 0.8em; color: var(--text-muted); display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
        .detail-author-row { display: flex; align-items: center; gap: 12px; margin-top: 4px; }
        .detail-author-avatar { width: 36px; height: 36px; border-radius: 50%; object-fit: cover; }
        .detail-author-initials { width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 0.9em; font-weight: 700; flex-shrink: 0; }
        .detail-author-info { display: flex; flex-direction: column; gap: 2px; }
        .detail-author-name { font-weight: 600; color: var(--text); font-size: 0.9em; }
        .detail-author-meta { font-size: 0.78em; color: var(--text-dim); }
        .detail-description { font-size: 0.9em; color: var(--text-muted); line-height: 1.6; margin-bottom: 20px; white-space: pre-wrap; }
        .detail-params { margin-bottom: 20px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        @media (max-width: 600px) { .detail-params { grid-template-columns: 1fr; } }
        .params-group { padding: 14px 16px; background: var(--bg-elevated); border-radius: 10px; border: 1px solid var(--border); }
        .params-group-header { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
        .params-group-icon { width: 16px; height: 16px; color: var(--accent); flex-shrink: 0; opacity: 0.8; }
        .params-group-title { font-size: 0.7em; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.08em; }
        .params-table { width: 100%; border-collapse: collapse; }
        .params-table tr { border-bottom: 1px solid rgba(255,255,255,0.04); }
        .params-table tr:last-child { border-bottom: none; }
        .params-td-label { font-size: 0.72em; font-weight: 600; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.04em; padding: 5px 8px 5px 0; white-space: nowrap; width: 1%; }
        .params-td-value { font-size: 0.82em; font-weight: 500; color: var(--text); font-family: 'JetBrains Mono', monospace; padding: 5px 0; }
        .action-btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-muted); cursor: pointer; font-size: 0.82em; font-weight: 500; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; text-decoration: none; }
        .action-btn:hover { border-color: var(--border-hover); color: var(--text); }
        .action-btn.primary { border-color: var(--accent); color: var(--accent); }
        .action-btn.liked { color: #ef4444; border-color: #ef4444; }
        .action-btn.danger { border-color: #ef4444; color: #ef4444; }
        .action-btn.danger:hover { background: rgba(239,68,68,0.1); }
        .action-buttons { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; justify-content: center; }
        .open-backtester-btn { display: inline-flex; align-items: center; gap: 8px; padding: 10px 28px; border-radius: 10px; border: 1.5px solid #f7931a; background: rgba(247,147,26,0.08); color: #f7931a; font-size: 0.88em; font-weight: 600; font-family: 'DM Sans', sans-serif; text-decoration: none; transition: all 0.2s ease; }
        .open-backtester-btn:hover { background: rgba(247,147,26,0.18); transform: translateY(-1px); }
        .like-btn { display: inline-flex; align-items: center; gap: 8px; padding: 12px 28px; border-radius: 12px; border: 2px solid #ef4444; background: transparent; color: #ef4444; cursor: pointer; font-size: 0.95em; font-weight: 600; font-family: 'DM Sans', sans-serif; transition: all 0.25s ease; }
        .like-btn:hover { background: rgba(239,68,68,0.1); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(239,68,68,0.15); }
        .like-btn.liked { background: #ef4444; color: #fff; box-shadow: 0 2px 8px rgba(239,68,68,0.2); }
        .like-btn.disabled { cursor: default; opacity: 0.5; }
        .like-heart { font-size: 1.2em; display: inline-block; }
        .like-btn.liked .like-heart { animation: heartBounce 0.5s ease; }
        @keyframes heartBounce { 0% { transform: scale(1); } 25% { transform: scale(1.4); } 50% { transform: scale(0.9); } 75% { transform: scale(1.15); } 100% { transform: scale(1); } }
        .like-btn.just-liked { animation: btnPulse 0.4s ease; }
        @keyframes btnPulse { 0% { box-shadow: 0 0 0 0 rgba(239,68,68,0.4); } 70% { box-shadow: 0 0 0 12px rgba(239,68,68,0); } 100% { box-shadow: 0 2px 8px rgba(239,68,68,0.1); } }
        .edit-modal-overlay { display: none; position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); align-items: center; justify-content: center; }
        .edit-modal-overlay.open { display: flex; }
        .edit-modal { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 16px; padding: 28px; width: 90%; max-width: 500px; position: relative; }
        .edit-modal h3 { font-size: 1.1em; font-weight: 600; margin-bottom: 16px; }
        .edit-modal label { display: block; font-size: 0.8em; color: var(--text-muted); margin-bottom: 6px; font-weight: 500; }
        .edit-modal input, .edit-modal textarea { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.9em; font-family: 'DM Sans', sans-serif; margin-bottom: 14px; }
        .edit-modal input:focus, .edit-modal textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .edit-modal textarea { resize: vertical; min-height: 80px; }
        .edit-modal .close-btn { position: absolute; top: 12px; right: 16px; background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 1.2em; }
        .comments-section { margin-top: 24px; }
        .comments-title { font-size: 1em; font-weight: 600; margin-bottom: 14px; }
        .comment-form { background: var(--bg-elevated); padding: 16px; border-radius: 12px; margin-bottom: 20px; border: 1px solid var(--border); }
        .comment-form textarea { width: 100%; padding: 12px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.88em; font-family: 'DM Sans', sans-serif; resize: vertical; min-height: 80px; margin-bottom: 10px; transition: border-color 0.2s ease; }
        .comment-form textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .comment-form .action-btn.primary { padding: 10px 24px; font-size: 0.88em; font-weight: 600; }
        .comment { padding: 12px 0; border-bottom: 1px solid var(--border); }
        .comment:last-child { border-bottom: none; }
        .comment-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 0.8em; }
        .comment-author { font-weight: 600; color: var(--text); }
        .comment-time { color: var(--text-dim); }
        .comment-body { font-size: 0.85em; color: var(--text-muted); line-height: 1.5; }
        .comment-body.comment-deleted { color: var(--text-dim); font-style: italic; }
        .comment-edited { color: var(--text-dim); font-size: 0.85em; font-style: italic; }
        .comment-edit-form textarea { width: 100%; min-height: 60px; background: var(--bg-deep); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 8px; font-family: 'DM Sans', sans-serif; font-size: 0.85em; resize: vertical; }
        .comment-actions { margin-top: 6px; display: flex; gap: 12px; }
        .comment-action-btn { background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 0.75em; font-family: 'DM Sans', sans-serif; }
        .comment-action-btn:hover { color: var(--text); }
        .comment-replies { margin-left: 24px; border-left: 2px solid var(--border); padding-left: 14px; margin-top: 4px; }
        .reply-to-tag { font-size: 0.75em; color: var(--accent); margin-bottom: 4px; }
        .reply-to-tag .reply-arrow { opacity: 0.7; margin-right: 3px; }
        .reactions-row { display: flex; align-items: center; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
        .reaction-pill { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 12px; border: 1px solid var(--border); background: var(--bg-elevated); cursor: pointer; font-size: 0.78em; transition: all 0.15s ease; user-select: none; }
        .reaction-pill:hover { border-color: var(--border-hover, #3a4060); background: var(--bg-surface); }
        .reaction-pill.reacted { border-color: var(--blue); background: rgba(100,149,237,0.12); }
        .reaction-pill .r-count { color: var(--text-muted); font-family: 'JetBrains Mono', monospace; font-size: 0.85em; }
        .reaction-pill.reacted .r-count { color: var(--blue); }
        .reaction-add { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 12px; border: 1px dashed var(--border); background: none; cursor: pointer; font-size: 0.85em; color: var(--text-dim); transition: all 0.15s ease; position: relative; }
        .reaction-add:hover { border-color: var(--text-muted); color: var(--text-muted); }
        .emoji-picker { display: none; position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%); background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 10px; padding: 6px; gap: 2px; z-index: 50; white-space: nowrap; box-shadow: 0 4px 16px rgba(0,0,0,0.4); }
        .emoji-picker.open { display: flex; }
        .emoji-pick { padding: 4px 6px; border-radius: 6px; cursor: pointer; font-size: 1.1em; transition: background 0.1s; border: none; background: none; }
        .emoji-pick:hover { background: var(--bg-surface); }
        @keyframes reactionPop { 0% { transform: scale(1); } 50% { transform: scale(1.3); } 100% { transform: scale(1); } }
        .reaction-pill.just-reacted { animation: reactionPop 0.3s ease; }
        .reply-form { margin-top: 8px; }
        .reply-form textarea { min-height: 40px; font-size: 0.8em; width: 100%; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-family: 'DM Sans', sans-serif; resize: vertical; margin-bottom: 6px; }
        .reply-form textarea:focus { outline: none; border-color: var(--accent); }
        .cta-banner { background: linear-gradient(135deg, rgba(247,147,26,0.1), rgba(100,149,237,0.1)); border: 1px solid var(--accent); border-radius: 12px; padding: 20px; text-align: center; margin-top: 20px; }
        .cta-banner h3 { font-size: 1em; margin-bottom: 6px; }
        .cta-banner p { font-size: 0.85em; color: var(--text-muted); margin-bottom: 12px; }
        .cta-banner a { display: inline-block; padding: 10px 24px; background: var(--accent); color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 0.85em; }
        .hidden { display: none !important; }
        .chart-img { width: 100%; border-radius: 12px; border: 1px solid var(--border); }
        .chart-download-btn {
            position: absolute; top: 12px; right: 12px;
            background: var(--bg-surface); color: var(--text-muted);
            border: 1px solid var(--border); border-radius: 8px;
            padding: 6px 8px; cursor: pointer; opacity: 0;
            transition: opacity 0.2s ease, background 0.15s ease, color 0.15s ease;
            display: flex; align-items: center; justify-content: center;
        }
        .chart-download-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
        div:hover > .chart-download-btn { opacity: 1; }
        .results-table {
            width: 100%; border-collapse: collapse; margin-bottom: 16px;
            font-size: 0.85em; font-family: 'JetBrains Mono', monospace;
        }
        .results-table th, .results-table td { padding: 10px 12px; text-align: right; border-bottom: 1px solid var(--border); }
        .results-table th { color: var(--text-muted); font-weight: 500; font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.05em; }
        .results-table tr { transition: background 0.15s ease; }
        .results-table tr:hover { background: var(--bg-elevated); }
        .best { color: var(--green); font-weight: 600; }
        .best td:first-child::before {
            content: ''; display: inline-block; width: 6px; height: 6px;
            background: var(--green); border-radius: 50%; margin-right: 8px;
            vertical-align: middle; box-shadow: 0 0 8px var(--green);
        }
        .metrics-panel { margin-bottom: 16px; }
        .metrics-table {
            width: 100%; border-collapse: collapse;
            font-size: 0.9em; font-family: 'JetBrains Mono', monospace;
        }
        .metrics-table th {
            padding: 6px 10px; font-size: 0.8em; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.06em;
            border-bottom: 1px solid var(--border);
        }
        .metrics-table th.col-metric { text-align: left; color: var(--text-muted); }
        .metrics-table th.col-strategy { text-align: right; color: var(--green); }
        .metrics-table th.col-buyhold { text-align: right; color: var(--blue); }
        .metrics-table td { padding: 5px 10px; border-bottom: 1px solid rgba(37,42,58,0.4); }
        .metrics-table td.m-label {
            font-size: 0.95em; color: var(--text-muted); font-family: 'DM Sans', sans-serif; font-weight: 500;
        }
        .metrics-table td.m-val { text-align: right; font-weight: 600; color: var(--text); }
        .metrics-table td.m-val.positive { color: var(--green); }
        .metrics-table td.m-val.negative { color: #ef4444; }
        .metrics-table td.m-val.muted { color: var(--text-dim); }
        .metrics-table tr.section-row td {
            padding: 8px 8px 3px; font-size: 0.65em; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.1em;
            color: var(--text-dim); border-bottom: 1px solid var(--border);
        }
        .m-ls {
            display: block; font-size: 0.75em; font-weight: 400;
            color: var(--text-dim); margin-top: 1px; line-height: 1.3;
        }
        .m-ls .ls-l { color: var(--green); }
        .m-ls .ls-s { color: #f7931a; }
        .m-info {
            font-size: 0.7em; color: var(--text-dim); cursor: help;
            margin-left: 4px; opacity: 0.5; position: relative; transition: opacity 0.2s;
        }
        .m-info:hover { opacity: 1; }
        .m-info:hover::after {
            content: attr(data-tip);
            position: absolute; bottom: 120%; left: 50%; transform: translateX(-50%);
            background: var(--bg-elevated); color: var(--text);
            border: 1px solid var(--border-hover); border-radius: 8px; padding: 8px 12px;
            font-size: 11px; font-family: 'DM Sans', sans-serif;
            font-weight: 400; line-height: 1.5;
            white-space: pre-line; width: max-content; max-width: 260px;
            z-index: 100; pointer-events: none;
            box-shadow: 0 4px 16px rgba(0,0,0,0.4);
        }
        .chart-tabs { display: flex; gap: 4px; margin-bottom: 12px; }
        .chart-tab {
            padding: 6px 16px; background: var(--bg-surface); border: 1px solid var(--border);
            border-radius: 8px 8px 0 0; color: var(--text-muted);
            cursor: pointer; font-size: 0.8em; font-family: 'DM Sans', sans-serif; font-weight: 500;
            transition: all 0.2s ease;
        }
        .chart-tab:hover { color: var(--text); background: var(--bg-elevated); }
        .chart-tab.active { background: var(--bg-elevated); color: var(--text); border-bottom-color: var(--bg-elevated); }
        .lw-measure-label {
            position: absolute; z-index: 5; pointer-events: none;
            background: rgba(22,25,34,0.92); border: 1px solid var(--border-hover);
            border-radius: 6px; padding: 6px 10px; font-family: 'JetBrains Mono', monospace;
            font-size: 12px; line-height: 1.5; color: var(--text); white-space: nowrap;
            backdrop-filter: blur(4px); box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        .backtest-card-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
        .badge-featured { background: rgba(247,147,26,0.15); color: var(--accent); }
        .badge-community { background: rgba(100,149,237,0.15); color: var(--blue); }

        /* ── Mobile responsive ── */
        @media (max-width: 600px) {
            .auth-buttons { position: static; display: flex; justify-content: center; margin-bottom: 12px; }
            .auth-btn { padding: 8px 16px; font-size: 0.8em; }
        }
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
            .nav-bar { flex-wrap: wrap; }
            .nav-link { padding: 6px 10px; font-size: 0.75em; }
        }
        @media (max-width: 400px) {
            .notif-dropdown { width: calc(100vw - 32px); right: -8px; }
        }
    </style>
</head>
<body>
<div class="container">
""" + NAV_HTML + """

    <div class="panel">
        <div class="detail-header">
            {% if backtest.visibility == 'community' %}<span class="backtest-card-badge badge-community">Community</span>{% endif %}
            <div class="detail-title-row">
                <h2 class="detail-title">{{ backtest.title|title if backtest.title else 'Backtest' }}</h2>
                <svg class="copy-link-icon" onclick="copyLink(this)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" title="Copy link"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
            </div>
            <div class="detail-author-row">
                {% if author_avatar %}
                <img src="/static/avatars/{{ author_avatar }}" class="detail-author-avatar" alt="">
                {% else %}
                <span class="detail-author-initials" style="background:{{ author_avatar_color }}">{{ author_initial }}</span>
                {% endif %}
                <div class="detail-author-info">
                    <span class="detail-author-name">{{ display_name or backtest.user_email.split('@')[0] }}</span>
                    <span class="detail-author-meta">{{ time_ago(backtest.created_at) }} · ♥ {{ backtest.likes_count }} · 💬 {{ backtest.comments_count }} · 👁 {{ backtest.views_count or 0 }}</span>
                </div>
            </div>
        </div>
        {% if backtest.description %}
        <div class="detail-description">{{ backtest.description }}</div>
        {% endif %}

        {% if bt_params %}
        <div class="detail-params">
            {# ── Asset ── #}
            <div class="params-group">
                <div class="params-group-header">
                    <svg class="params-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
                    <span class="params-group-title">Asset</span>
                </div>
                <table class="params-table">
                    <tr><td class="params-td-label">Asset</td><td class="params-td-value">{{ bt_params.asset|capitalize }}</td></tr>
                    {% if bt_params.get('mode') %}<tr><td class="params-td-label">Mode</td><td class="params-td-value">{{ bt_params.mode|replace('-', ' ')|title }}</td></tr>{% endif %}
                </table>
            </div>

            {# ── Indicators ── #}
            <div class="params-group">
                <div class="params-group-header">
                    <svg class="params-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                    <span class="params-group-title">Indicators</span>
                </div>
                <table class="params-table">
                    {% if bt_params.ind1_name %}<tr><td class="params-td-label">Indicator 1</td><td class="params-td-value">{{ bt_params.ind1_name|upper }}{% if bt_params.get('period1') %} ({{ bt_params.period1 }}){% endif %}</td></tr>{% endif %}
                    {% if bt_params.ind2_name %}<tr><td class="params-td-label">Indicator 2</td><td class="params-td-value">{{ bt_params.ind2_name|upper }}{% if bt_params.get('period2') %} ({{ bt_params.period2 }}){% endif %}</td></tr>{% endif %}
                    {% if bt_params.get('osc_name') and bt_params.get('signal_type', '') == 'oscillator' %}
                    <tr><td class="params-td-label">Oscillator</td><td class="params-td-value">{{ bt_params.osc_name|upper }}{% if bt_params.get('osc_period') %} ({{ bt_params.osc_period }}){% endif %}</td></tr>
                    {% if bt_params.get('buy_threshold') %}<tr><td class="params-td-label">Buy / Sell</td><td class="params-td-value">{{ bt_params.buy_threshold }} / {{ bt_params.sell_threshold }}</td></tr>{% endif %}
                    {% endif %}
                    {% if bt_params.get('reverse') == '1' %}<tr><td class="params-td-label">Signal</td><td class="params-td-value">Reversed</td></tr>{% endif %}
                    {% if bt_params.get('range_min') %}<tr><td class="params-td-label">Period Range</td><td class="params-td-value">{{ bt_params.range_min }} – {{ bt_params.range_max }} (step {{ bt_params.get('step', '1') }})</td></tr>{% endif %}
                    {% if bt_params.get('forward_days') %}<tr><td class="params-td-label">Forward Days</td><td class="params-td-value">{{ bt_params.forward_days }}</td></tr>{% endif %}
                </table>
            </div>

            {# ── Exposure & Leverage ── #}
            <div class="params-group">
                <div class="params-group-header">
                    <svg class="params-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
                    <span class="params-group-title">Exposure & Leverage</span>
                </div>
                <table class="params-table">
                    {% if bt_params.exposure %}<tr><td class="params-td-label">Exposure</td><td class="params-td-value">{{ bt_params.exposure|replace('-', ' ')|title }}</td></tr>{% endif %}
                    {% set ll = bt_params.get('long_leverage', '1') %}{% set sl = bt_params.get('short_leverage', '1') %}
                    {% if ll not in ('1', '1.0') or sl not in ('1', '1.0') %}
                    <tr><td class="params-td-label">Long Lev</td><td class="params-td-value">{{ ll }}x</td></tr>
                    <tr><td class="params-td-label">Short Lev</td><td class="params-td-value">{{ sl }}x</td></tr>
                    {% if bt_params.get('lev_mode') %}<tr><td class="params-td-label">Lev Mode</td><td class="params-td-value">{{ bt_params.lev_mode|capitalize }}</td></tr>{% endif %}
                    {% endif %}
                    <tr><td class="params-td-label">Sizing</td><td class="params-td-value">{{ bt_params.get('sizing', 'compound')|capitalize }}</td></tr>
                </table>
            </div>

            {# ── Date Range & Capital ── #}
            <div class="params-group">
                <div class="params-group-header">
                    <svg class="params-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                    <span class="params-group-title">Date Range & Capital</span>
                </div>
                <table class="params-table">
                    <tr><td class="params-td-label">Start</td><td class="params-td-value">{{ bt_params.get('start_date', '–') }}</td></tr>
                    <tr><td class="params-td-label">End</td><td class="params-td-value">{{ bt_params.get('end_date', '–') }}</td></tr>
                    <tr><td class="params-td-label">Capital</td><td class="params-td-value">${{ bt_params.get('initial_cash', '10,000') }}</td></tr>
                    <tr><td class="params-td-label">Fee</td><td class="params-td-value">{{ bt_params.get('fee', '0.1') }}%</td></tr>
                    {% if bt_params.get('financing_rate', 0)|float > 0 %}
                    <tr><td class="params-td-label">Financing</td><td class="params-td-value">{{ bt_params.get('financing_rate') }}% p.a.</td></tr>
                    {% endif %}
                </table>
            </div>
        </div>
        {% endif %}

        {% if is_authenticated %}
        <div style="text-align:center;margin-bottom:16px">
            <a class="open-backtester-btn" href="/backtester?{{ backtest.query_string }}">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                Open in Backtester
            </a>
        </div>
        <div class="action-buttons">
            {% if is_admin or backtest.user_id == session.get('user_id') %}
            <button class="action-btn" onclick="openDetailEditModal()">Edit</button>
            <button class="action-btn danger" onclick="deleteThisBacktest()">Delete</button>
            {% endif %}
            {% if is_admin %}
            <button class="action-btn {{ 'primary' if backtest.telegram_enabled else '' }}" onclick="toggleTelegram('{{ backtest.id }}', {{ 'true' if backtest.telegram_enabled else 'false' }})">Telegram {{ 'ON' if backtest.telegram_enabled else 'OFF' }}</button>
            {% endif %}
            {% if is_authenticated %}
            <button class="action-btn {{ 'primary' if has_email_alert else '' }}" onclick="toggleEmailAlert('{{ backtest.id }}', {{ 'true' if has_email_alert else 'false' }})" title="Get email alerts when this strategy generates a BUY or SELL signal">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><rect width="20" x="2" y="4" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>{{ 'Email Alerts ON' if has_email_alert else 'Email Signal Alerts' }}
            </button>
            {% endif %}
        </div>
        {% endif %}
    </div>

    {% if not is_authenticated %}
    <div class="cta-banner" style="margin-bottom:16px">
        <h3>Want to run your own backtests?</h3>
        <p>Sign up for a premium membership to access the full backtesting engine.</p>
        <a href="https://the-bitcoin-strategy.com">Get Started</a>
    </div>
    {% endif %}

    {% if backtest.cached_html %}
    <div class="panel" id="results-panel">
        {{ backtest.cached_html|safe }}
    </div>
    {% endif %}

    <div id="like-container" style="display:flex;justify-content:center;margin-bottom:16px">
        {% if is_authenticated %}
        <button class="like-btn {{ 'liked' if has_liked }}" onclick="toggleLike('{{ backtest.id }}', this)">
            <span class="like-heart">♥</span> <span class="like-count">{{ backtest.likes_count }}</span> <span class="like-text">{{ 'Liked' if has_liked else 'Like' }}</span>
        </button>
        {% else %}
        <div class="like-btn disabled">
            <span class="like-heart">♥</span> <span class="like-count">{{ backtest.likes_count }}</span> <span class="like-text">Like</span>
        </div>
        {% endif %}
    </div>

    <div class="panel">
        <div class="comments-section">
            <h3 class="comments-title">Comments ({{ comments|length }})</h3>

            {% if is_authenticated %}
            <div class="comment-form">
                <textarea id="comment-body" placeholder="Share your thoughts..."></textarea>
                <button class="action-btn primary" onclick="submitComment('{{ backtest.id }}', null)">Post Comment</button>
            </div>
            {% endif %}

            {% for comment in comments %}
            <div class="comment" id="comment-{{ comment.id }}">
                <div class="comment-header">
                    <span class="comment-author">{{ comment._display_name }}</span>
                    <span class="comment-time">{{ time_ago(comment.created_at) }}{% if comment.edited_at %} <span class="comment-edited">(edited)</span>{% endif %}</span>
                </div>
                {% if comment.is_deleted %}
                <div class="comment-body comment-deleted">[deleted]</div>
                {% else %}
                <div class="comment-body" id="comment-body-{{ comment.id }}">{{ comment.body }}</div>
                <div class="comment-edit-form hidden" id="edit-form-{{ comment.id }}">
                    <textarea id="edit-{{ comment.id }}">{{ comment.body }}</textarea>
                    <div style="display:flex;gap:8px;margin-top:6px;">
                        <button class="action-btn primary" onclick="saveEdit('{{ comment.id }}')">Save</button>
                        <button class="action-btn" onclick="cancelEdit('{{ comment.id }}')">Cancel</button>
                    </div>
                </div>
                <div class="comment-actions" id="comment-actions-{{ comment.id }}">
                    {% if is_authenticated %}
                    <button class="comment-action-btn" onclick="showReplyForm('{{ comment.id }}')">Reply</button>
                    {% endif %}
                    {% if is_authenticated and (comment.user_id == session.get('user_id') or is_admin) %}
                    <button class="comment-action-btn" onclick="startEdit('{{ comment.id }}')">Edit</button>
                    <button class="comment-action-btn" onclick="deleteComment('{{ comment.id }}')">Delete</button>
                    {% endif %}
                </div>
                <div class="reactions-row" id="reactions-{{ comment.id }}">
                    {% for emoji, info in comment._reactions.items() %}
                    <button class="reaction-pill{{ ' reacted' if info.reacted }}" onclick="toggleReaction('{{ comment.id }}', '{{ emoji }}', this)">
                        <span class="r-emoji">{{ emoji }}</span><span class="r-count">{{ info.count }}</span>
                    </button>
                    {% endfor %}
                    {% if is_authenticated %}
                    <span class="reaction-add" onclick="togglePicker(this)">+<div class="emoji-picker" onclick="event.stopPropagation()">
                        {% for e in ['👍','❤️','😂','🎯','🚀','👎'] %}<span class="emoji-pick" onclick="toggleReaction('{{ comment.id }}', '{{ e }}', null, this)">{{ e }}</span>{% endfor %}
                    </div></span>
                    {% endif %}
                </div>
                {% endif %}
                <div class="reply-form hidden" id="reply-form-{{ comment.id }}">
                    <textarea id="reply-{{ comment.id }}" placeholder="Write a reply..."></textarea>
                    <button class="action-btn" onclick="submitComment('{{ backtest.id }}', '{{ comment.id }}')">Reply</button>
                </div>
                {% if comment.replies %}
                <div class="comment-replies">
                    {% for reply in comment.replies %}
                    <div class="comment" id="comment-{{ reply.id }}">
                        <div class="reply-to-tag"><span class="reply-arrow">↩</span> replying to <strong>{{ comment._display_name }}</strong></div>
                        <div class="comment-header">
                            <span class="comment-author">{{ reply._display_name }}</span>
                            <span class="comment-time">{{ time_ago(reply.created_at) }}{% if reply.edited_at %} <span class="comment-edited">(edited)</span>{% endif %}</span>
                        </div>
                        {% if reply.is_deleted %}
                        <div class="comment-body comment-deleted">[deleted]</div>
                        {% else %}
                        <div class="comment-body" id="comment-body-{{ reply.id }}">{{ reply.body }}</div>
                        <div class="comment-edit-form hidden" id="edit-form-{{ reply.id }}">
                            <textarea id="edit-{{ reply.id }}">{{ reply.body }}</textarea>
                            <div style="display:flex;gap:8px;margin-top:6px;">
                                <button class="action-btn primary" onclick="saveEdit('{{ reply.id }}')">Save</button>
                                <button class="action-btn" onclick="cancelEdit('{{ reply.id }}')">Cancel</button>
                            </div>
                        </div>
                        <div class="comment-actions" id="comment-actions-{{ reply.id }}">
                            {% if is_authenticated %}
                            <button class="comment-action-btn" onclick="showReplyForm('{{ reply.id }}')">Reply</button>
                            {% endif %}
                            {% if is_authenticated and (reply.user_id == session.get('user_id') or is_admin) %}
                            <button class="comment-action-btn" onclick="startEdit('{{ reply.id }}')">Edit</button>
                            <button class="comment-action-btn" onclick="deleteComment('{{ reply.id }}')">Delete</button>
                            {% endif %}
                        </div>
                        <div class="reactions-row" id="reactions-{{ reply.id }}">
                            {% for emoji, info in reply._reactions.items() %}
                            <button class="reaction-pill{{ ' reacted' if info.reacted }}" onclick="toggleReaction('{{ reply.id }}', '{{ emoji }}', this)">
                                <span class="r-emoji">{{ emoji }}</span><span class="r-count">{{ info.count }}</span>
                            </button>
                            {% endfor %}
                            {% if is_authenticated %}
                            <span class="reaction-add" onclick="togglePicker(this)">+<div class="emoji-picker" onclick="event.stopPropagation()">
                                {% for e in ['👍','❤️','😂','🎯','🚀','👎'] %}<span class="emoji-pick" onclick="toggleReaction('{{ reply.id }}', '{{ e }}', null, this)">{{ e }}</span>{% endfor %}
                            </div></span>
                            {% endif %}
                        </div>
                        {% endif %}
                        {% if is_authenticated %}
                        <div class="reply-form hidden" id="reply-form-{{ reply.id }}">
                            <textarea id="reply-{{ reply.id }}" placeholder="Write a reply..."></textarea>
                            <button class="action-btn" onclick="submitComment('{{ backtest.id }}', '{{ comment.id }}', '{{ reply.id }}')">Reply</button>
                        </div>
                        {% endif %}
                    </div>
                    {% endfor %}
                </div>
                {% endif %}
            </div>
            {% endfor %}
        </div>

        </div>
</div>
<script src="/static/js/nav.js"></script>
<script src="/static/js/chart.js"></script>
<script>
document.addEventListener("DOMContentLoaded", function() {
    activateViewFromURL();
});
function toggleLike(backtestId, btn) {
    fetch('/api/backtest/' + backtestId + '/like', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.querySelector('.like-count').textContent = data.likes_count;
        var textEl = btn.querySelector('.like-text');
        if (data.liked) {
            btn.classList.add('liked');
            btn.classList.add('just-liked');
            if (textEl) textEl.textContent = 'Liked';
            setTimeout(function() { btn.classList.remove('just-liked'); }, 500);
        } else {
            btn.classList.remove('liked');
            if (textEl) textEl.textContent = 'Like';
        }
    });
}
function deleteThisBacktest() {
    _swal.fire({
        title: 'Delete this backtest?', text: 'This cannot be undone.',
        icon: 'warning', showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/backtest/{{ backtest.id }}', { method: 'DELETE' })
        .then(function(r) {
            if (r.ok) { window.location.href = '/'; } else { _swal.fire({icon:'error', title:'Failed to delete'}); }
        });
    });
}
function openDetailEditModal() {
    document.getElementById('detail-edit-overlay').classList.add('open');
    document.getElementById('detail-edit-title').focus();
}
function closeDetailEditModal() {
    document.getElementById('detail-edit-overlay').classList.remove('open');
}
function saveDetailEdit() {
    var title = document.getElementById('detail-edit-title').value.trim();
    var desc = document.getElementById('detail-edit-desc').value.trim();
    fetch('/api/backtest/{{ backtest.id }}', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, description: desc})
    }).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    }).then(function() {
        location.reload();
    }).catch(function(e) { _swal.fire({icon:'error', title:'Failed to save', text:e.message}); });
}
function submitComment(backtestId, parentId, textareaSrcId) {
    var textareaId = textareaSrcId ? 'reply-' + textareaSrcId : (parentId ? 'reply-' + parentId : 'comment-body');
    var textarea = document.getElementById(textareaId);
    var body = textarea.value.trim();
    if (!body) return;
    var form = textarea.closest('.comment-form') || textarea.closest('.reply-form');
    var submitBtn = form ? form.querySelector('button[onclick*="submitComment"]') : null;
    if (submitBtn) { submitBtn.disabled = true; submitBtn.innerHTML = 'Posting&hellip;'; }
    fetch('/api/backtest/' + backtestId + '/comment', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({body: body, parent_id: parentId || null})
    }).then(function(r) { return r.json(); })
    .then(function() {
        if (submitBtn) { submitBtn.innerHTML = '&#10003; Posted!'; submitBtn.style.background = '#22c55e'; submitBtn.style.borderColor = '#22c55e'; }
        setTimeout(function() { location.reload(); }, 500);
    });
}
function deleteComment(commentId) {
    _swal.fire({
        title: 'Delete this comment?', icon: 'warning',
        showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/comment/' + commentId, { method: 'DELETE' }).then(function() { location.reload(); });
    });
}
function showReplyForm(commentId) {
    var el = document.getElementById('reply-form-' + commentId);
    if (el) {
        el.classList.toggle('hidden');
        if (!el.classList.contains('hidden')) {
            var ta = el.querySelector('textarea');
            if (ta) ta.focus();
        }
    }
}
function startEdit(commentId) {
    document.getElementById('comment-body-' + commentId).classList.add('hidden');
    document.getElementById('comment-actions-' + commentId).classList.add('hidden');
    document.getElementById('edit-form-' + commentId).classList.remove('hidden');
}
function cancelEdit(commentId) {
    document.getElementById('edit-form-' + commentId).classList.add('hidden');
    document.getElementById('comment-body-' + commentId).classList.remove('hidden');
    document.getElementById('comment-actions-' + commentId).classList.remove('hidden');
}
function saveEdit(commentId) {
    var body = document.getElementById('edit-' + commentId).value.trim();
    if (!body) return;
    fetch('/api/comment/' + commentId, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({body: body})
    }).then(function(r) {
        if (r.ok) { location.reload(); }
        else { _swal.fire({icon:'error', title:'Failed to edit'}); }
    });
}
function toggleReaction(commentId, emoji, pillBtn, pickBtn) {
    fetch('/api/comment/' + commentId + '/reaction', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({emoji: emoji})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        var row = document.getElementById('reactions-' + commentId);
        if (!row) return;
        // Close any open picker
        row.querySelectorAll('.emoji-picker').forEach(function(p) { p.classList.remove('open'); });
        // Rebuild pills
        var addBtn = row.querySelector('.reaction-add');
        row.querySelectorAll('.reaction-pill').forEach(function(p) { p.remove(); });
        var emojis = data.reactions || {};
        var orderedEmojis = ['👍','❤️','😂','🎯','🚀','👎'];
        orderedEmojis.forEach(function(e) {
            if (!emojis[e]) return;
            var pill = document.createElement('button');
            pill.className = 'reaction-pill' + (emojis[e].reacted ? ' reacted' : '');
            pill.innerHTML = '<span class="r-emoji">' + e + '</span><span class="r-count">' + emojis[e].count + '</span>';
            pill.onclick = function() { toggleReaction(commentId, e, pill, null); };
            if (e === emoji) { pill.classList.add('just-reacted'); setTimeout(function() { pill.classList.remove('just-reacted'); }, 300); }
            row.insertBefore(pill, addBtn);
        });
    });
}
function togglePicker(btn) {
    var picker = btn.querySelector('.emoji-picker');
    var wasOpen = picker.classList.contains('open');
    document.querySelectorAll('.emoji-picker.open').forEach(function(p) { p.classList.remove('open'); });
    if (!wasOpen) picker.classList.add('open');
}
document.addEventListener('click', function(e) {
    if (!e.target.closest('.reaction-add')) {
        document.querySelectorAll('.emoji-picker.open').forEach(function(p) { p.classList.remove('open'); });
    }
});
// Linkify URLs in comment bodies
(function() {
    var urlPattern = /(https?:\/\/[^\s<]+)/g;
    function escapeHtml(s) {
        var d = document.createElement('div'); d.textContent = s; return d.innerHTML;
    }
    document.querySelectorAll('.comment-body').forEach(function(el) {
        var text = el.textContent;
        if (urlPattern.test(text)) {
            var parts = text.split(urlPattern);
            var html = '';
            for (var i = 0; i < parts.length; i++) {
                if (urlPattern.test(parts[i])) {
                    html += '<a href="' + escapeHtml(parts[i]) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(parts[i]) + '</a>';
                } else {
                    html += escapeHtml(parts[i]);
                }
            }
            el.innerHTML = html;
        }
    });
})();
function featureBacktest(backtestId) {
    fetch('/api/backtest/' + backtestId + '/feature', { method: 'POST' }).then(function() { location.reload(); });
}
var _defaultTgTemplate = '\\u26a0\\ufe0f This is a <b>{signal}</b> Signal for {asset}.\\n\\nWe are changing our position for {asset} since the moving averages have crossed: {ind1} / {ind2}\\n\\n{if_buy}For long signals, we use {long_lev}x leverage in {asset}.{/if_buy}{if_sell}For short signals, we use {short_lev}x leverage in {asset}.{/if_sell}\\n\\n<a href=\\"{link}\\">View Live Chart</a>';
var _tgExample = {
    asset: '{{ bt_params.get("asset", "bitcoin")|replace("'", "")|capitalize }}',
    signal: 'BUY',
    ind1: '{{ bt_params.get("ind1_name", "price")|upper }}{% if bt_params.get("period1") %}({{ bt_params.period1 }}){% endif %}',
    ind2: '{{ bt_params.get("ind2_name", "sma")|upper }}{% if bt_params.get("period2") %}({{ bt_params.period2 }}){% endif %}',
    long_lev: '{{ bt_params.get("long_leverage", "1") }}',
    short_lev: '{{ bt_params.get("short_leverage", "1") }}',
    link: '{{ "https://analytics.the-bitcoin-strategy.com/backtest/" ~ backtest.id ~ "?view=livechart" }}'
};
var _tgPreviewSignal = 'BUY';
function _renderTgPreview() {
    var tpl = (document.getElementById('tg-template') || {}).value || '';
    var isBuy = _tgPreviewSignal === 'BUY';
    var rendered = isBuy
        ? tpl.replace(/\{if_buy\}([\s\S]*?)\{\/if_buy\}/g, '$1').replace(/\{if_sell\}([\s\S]*?)\{\/if_sell\}/g, '')
        : tpl.replace(/\{if_sell\}([\s\S]*?)\{\/if_sell\}/g, '$1').replace(/\{if_buy\}([\s\S]*?)\{\/if_buy\}/g, '');
    rendered = rendered.replace(/\\n/g, '<br>').replace(/\{asset\}/g, _tgExample.asset).replace(/\{signal\}/g, _tgPreviewSignal).replace(/\{ind1\}/g, _tgExample.ind1).replace(/\{ind2\}/g, _tgExample.ind2).replace(/\{long_lev\}/g, _tgExample.long_lev).replace(/\{short_lev\}/g, _tgExample.short_lev).replace(/\{link\}/g, _tgExample.link);
    var el = document.getElementById('tg-preview');
    if (el) el.innerHTML = rendered;
    // Character counter — strip HTML tags to get plain text length
    var plain = rendered.replace(/<br>/g, '\\n').replace(/<[^>]*>/g, '');
    var charCount = plain.length;
    var counterEl = document.getElementById('tg-char-counter');
    if (counterEl) {
        counterEl.textContent = charCount + ' / 1024 characters';
        counterEl.style.color = charCount > 1024 ? '#ff4444' : charCount > 900 ? '#ffaa00' : 'var(--text-muted)';
    }
    // Update toggle button states
    var buyBtn = document.getElementById('tg-preview-buy');
    var sellBtn = document.getElementById('tg-preview-sell');
    if (buyBtn) buyBtn.style.opacity = isBuy ? '1' : '0.5';
    if (sellBtn) sellBtn.style.opacity = isBuy ? '0.5' : '1';
}
function _toggleTgPreviewSignal(sig) {
    _tgPreviewSignal = sig;
    _renderTgPreview();
}
function toggleTelegram(btId, currentlyEnabled) {
    if (currentlyEnabled) {
        _swal.fire({
            title: 'Disable Telegram signals?',
            icon: 'warning', showCancelButton: true, confirmButtonText: 'Disable'
        }).then(function(result) {
            if (!result.isConfirmed) return;
            fetch('/api/backtest/' + btId + '/telegram', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: false, template: ''})
            }).then(function() { location.reload(); });
        });
    } else {
        var currentTemplate = '{{ backtest.telegram_message_template|default("", true)|replace("\n", "\\\\n")|replace("\r", "")|e }}'.replace(/&#39;/g,"'").replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"');
        _swal.fire({
            title: 'Enable Telegram Signals',
            html: '<textarea id="tg-template" rows="6" style="width:100%;font-family:monospace;font-size:13px;background:var(--bg-deep);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px;resize:vertical" oninput="_renderTgPreview()"></textarea>' +
                  '<p style="font-size:11px;color:var(--text-muted);margin-top:8px;text-align:left">Placeholders: <code>{asset}</code> <code>{signal}</code> <code>{ind1}</code> <code>{ind2}</code> <code>{long_lev}</code> <code>{short_lev}</code> <code>{link}</code><br>Conditionals: <code>{if_buy}...{/if_buy}</code> <code>{if_sell}...{/if_sell}</code></p>' +
                  '<div style="margin-top:12px;text-align:left"><div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><span style="font-size:11px;color:var(--text-muted);font-weight:600">Preview:</span><button type="button" id="tg-preview-buy" onclick="_toggleTgPreviewSignal(&#39;BUY&#39;)" style="font-size:11px;padding:2px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg-elevated);color:var(--text);cursor:pointer">BUY</button><button type="button" id="tg-preview-sell" onclick="_toggleTgPreviewSignal(&#39;SELL&#39;)" style="font-size:11px;padding:2px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg-elevated);color:var(--text);cursor:pointer;opacity:0.5">SELL</button></div><div id="tg-preview" style="background:var(--bg-deep);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:13px;line-height:1.5"></div><div id="tg-char-counter" style="font-size:11px;color:var(--text-muted);margin-top:6px;text-align:right">0 / 1024 characters</div></div>',
            showCancelButton: true, confirmButtonText: 'Enable',
            didOpen: function() {
                var ta = document.getElementById('tg-template');
                ta.value = (currentTemplate || _defaultTgTemplate).replace(/\\\\n/g, '\\n');
                _renderTgPreview();
            },
            preConfirm: function() { return document.getElementById('tg-template').value; }
        }).then(function(result) {
            if (!result.isConfirmed) return;
            fetch('/api/backtest/' + btId + '/telegram', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: true, template: result.value})
            }).then(function() { location.reload(); });
        });
    }
}


function toggleEmailAlert(btId, currentlyEnabled) {
    if (currentlyEnabled) {
        _swal.fire({
            title: 'Disable email alerts?',
            text: 'You will no longer receive email notifications when this strategy generates a signal.',
            icon: 'warning', showCancelButton: true, confirmButtonText: 'Disable'
        }).then(function(result) {
            if (!result.isConfirmed) return;
            fetch('/api/backtest/' + btId + '/email-alert', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: false})
            }).then(function(r) { return r.json(); }).then(function() { location.reload(); });
        });
    } else {
        _swal.fire({
            title: 'Enable email alerts?',
            html: 'You will receive an email whenever this strategy generates a <b>BUY</b> or <b>SELL</b> signal.<br><br><span style="font-size:13px;color:var(--text-muted)">Alerts are checked daily after price data is updated.</span>',
            icon: 'info', showCancelButton: true, confirmButtonText: 'Enable'
        }).then(function(result) {
            if (!result.isConfirmed) return;
            fetch('/api/backtest/' + btId + '/email-alert', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: true})
            }).then(function(r) { return r.json(); }).then(function(data) {
                if (data.ok) { location.reload(); }
                else { _swal.fire({icon:'error', title:'Error', text: data.error || 'Failed to enable alert'}); }
            });
        });
    }
}

function copyLink(el) {
    navigator.clipboard.writeText(location.origin + '/s/{{ backtest.short_code }}');
    if (el) { el.classList.add('copied'); setTimeout(function(){ el.classList.remove('copied'); }, 1200); }
    _swal.fire({icon:'success', title:'Link copied!', timer:1200, showConfirmButton:false});
}
// Move like button between chart and metrics table
(function() {
    var panel = document.getElementById('results-panel');
    var likeContainer = document.getElementById('like-container');
    if (panel && likeContainer) {
        var metrics = panel.querySelector('.metrics-panel');
        if (metrics) {
            likeContainer.style.margin = '12px 0';
            metrics.parentNode.insertBefore(likeContainer, metrics);
        }
    }
})();
</script>
{% if is_authenticated and (is_admin or backtest.user_id == session.get('user_id')) %}
<div class="edit-modal-overlay" id="detail-edit-overlay">
    <div class="edit-modal">
        <button class="close-btn" onclick="closeDetailEditModal()">&times;</button>
        <h3>Edit Backtest</h3>
        <label>Title</label>
        <input type="text" id="detail-edit-title" value="{{ backtest.title|e }}">
        <label>Description</label>
        <textarea id="detail-edit-desc">{{ backtest.description or '' }}</textarea>
        <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:4px">
            <button class="action-btn" onclick="closeDetailEditModal()">Cancel</button>
            <button class="action-btn primary" onclick="saveDetailEdit()">Save Changes</button>
        </div>
    </div>
</div>
{% endif %}
</body>
</html>
"""


MY_BACKTESTS_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <title>My Backtests — Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --bg-deep: #080a10; --bg-base: #0f1117; --bg-surface: #161922; --bg-elevated: #1c2030;
            --border: #252a3a; --border-hover: #3a4060; --text: #e8eaf0; --text-muted: #8890a4; --text-dim: #555d74;
            --accent: #f7931a; --accent-hover: #ffa940; --accent-glow: rgba(247, 147, 26, 0.15);
            --green: #34d399; --blue: #6495ED;
        }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247, 147, 26, 0.06), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none; z-index: 0;
        }
        [data-theme="light"] body::before {
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(217, 119, 6, 0.04), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(58, 111, 216, 0.03), transparent);
        }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }
        .header { text-align: center; margin-bottom: 32px; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; position: relative; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        /* Theme toggle */
        .theme-toggle { background: none; border: 1px solid var(--border); cursor: pointer; color: var(--text-muted); padding: 7px; border-radius: 8px; transition: all 0.2s ease; display: flex; align-items: center; justify-content: center; }
        .theme-toggle:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border-hover); }
        .theme-toggle svg { width: 16px; height: 16px; }
        .theme-toggle .icon-sun { display: none; }
        .theme-toggle .icon-moon { display: block; }
        [data-theme="light"] .theme-toggle .icon-sun { display: block; }
        [data-theme="light"] .theme-toggle .icon-moon { display: none; }
        .nav-right-group { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; z-index: 9999; }
        .notif-bell-wrap { position: relative; }
        .notif-bell { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 8px; border-radius: 8px; position: relative; transition: all 0.2s ease; }
        .notif-bell:hover { color: var(--text); background: var(--bg-elevated); }
        .notif-badge { position: absolute; top: 2px; right: 2px; background: #e74c3c; color: #fff; font-size: 0.65em; font-weight: 700; min-width: 16px; height: 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; padding: 0 4px; font-family: 'JetBrains Mono', monospace; }
        .notif-badge.hidden { display: none; }
        .notif-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 340px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; }
        .notif-dropdown.hidden { display: none; }
        .notif-dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.85em; color: var(--text); }
        .notif-list { max-height: 320px; overflow-y: auto; }
        .notif-item { display: block; padding: 12px 16px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); font-size: 0.82em; transition: background 0.15s ease; cursor: pointer; }
        .notif-item.notif-unread { background: rgba(100,149,237,0.08); border-left: 3px solid var(--accent); }
        .notif-item.notif-read { opacity: 0.55; }
        .notif-item:hover { background: var(--bg-elevated); opacity: 1; }
        .notif-item:last-child { border-bottom: none; }
        .notif-item-text { line-height: 1.4; }
        .notif-item-text strong { color: var(--accent); font-weight: 600; }
        .notif-item-time { color: var(--text-dim); font-size: 0.78em; margin-top: 4px; }
        .notif-empty { padding: 24px 16px; text-align: center; color: var(--text-dim); font-size: 0.82em; }
        /* Avatar */
        .avatar-wrap { position: relative; }
        .avatar-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 50%; transition: all 0.2s ease; }
        .avatar-btn:hover { background: var(--bg-elevated); }
        .avatar-img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); display: block; }
        .avatar-initials { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 700; color: #fff; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 220px; background: var(--bg-surface, var(--bg-base)); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; padding: 6px 0; }
        .avatar-dropdown.hidden { display: none; }
        .avatar-dropdown-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; color: var(--text-muted); text-decoration: none; font-size: 0.82em; font-weight: 500; transition: all 0.15s ease; font-family: 'DM Sans', sans-serif; }
        .avatar-dropdown-item:hover { background: var(--bg-elevated); color: var(--text); }
        .avatar-dropdown-item svg { width: 16px; height: 16px; flex-shrink: 0; }
        .avatar-dropdown-divider { height: 1px; background: var(--border); margin: 4px 0; }
        .avatar-dropdown-logout { color: #e74c3c; }
        .avatar-dropdown-logout:hover { background: rgba(231,76,60,0.1); color: #e74c3c; }
        /* Small avatars for cards and comments */
        .card-avatar-img { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; margin-right: 4px; }
        .card-avatar-initials { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 0.6em; font-weight: 700; color: #fff; vertical-align: middle; margin-right: 4px; }
        .comment-avatar-img { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; }
        .comment-avatar-initials { width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.65em; font-weight: 700; color: #fff; flex-shrink: 0; }
        .panel { background: var(--bg-surface); border-radius: 16px; padding: 24px; border: 1px solid var(--border); margin-bottom: 16px; }
        .page-title { font-size: 1.4em; font-weight: 700; margin-bottom: 6px; }
        .section-header { font-size: 1em; font-weight: 600; margin-bottom: 14px; color: var(--text-muted); }
        .backtest-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .backtest-card { display: block; background: var(--bg-base); border: 1px solid var(--border); border-radius: 14px; padding: 18px; transition: all 0.2s ease; color: inherit; text-decoration: none; }
        .backtest-card:hover { border-color: var(--border-hover); transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
        .backtest-card-title { font-size: 1em; font-weight: 600; margin-bottom: 6px; color: var(--text); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.3; }
        .backtest-card-desc { font-size: 0.8em; color: var(--text-muted); margin-bottom: 10px; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
        .backtest-card-footer { display: flex; align-items: center; justify-content: space-between; font-size: 0.75em; color: var(--text-dim); }
        .backtest-card-footer .engagement { display: flex; gap: 12px; }
        .backtest-card-footer .engagement span { display: flex; align-items: center; gap: 3px; }
        .card-author { display: flex; align-items: center; gap: 6px; }
        .card-author-avatar { width: 22px; height: 22px; border-radius: 50%; object-fit: cover; }
        .card-author-initials { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 0.7em; font-weight: 700; flex-shrink: 0; }
        .card-author-name { font-weight: 600; color: var(--text-muted); }
        .card-author-sep { color: var(--text-dim); }
        .card-author-time { color: var(--text-dim); }
        .backtest-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
        .backtest-card-asset-logo { width: 22px; height: 22px; object-fit: contain; border-radius: 50%; background: var(--bg-deep); flex-shrink: 0; }
        .backtest-card-asset-fallback { width: 22px; height: 22px; border-radius: 50%; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.65em; color: var(--text-muted); flex-shrink: 0; }
        .backtest-card-head-text { flex: 1; min-width: 0; }
        .backtest-card-asset-name { font-size: 0.75em; color: var(--text-muted); }
        .backtest-card-mode-icon { color: var(--text-dim); flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 8px; background: var(--bg-deep); }
        .backtest-card-params { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
        .backtest-card-tag { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 6px; background: var(--bg-deep); border: 1px solid var(--border); font-size: 0.7em; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; white-space: nowrap; }
        .backtest-card-thumb { width: 100%; height: 140px; object-fit: cover; border-radius: 8px; margin-bottom: 10px; border: 1px solid var(--border); }
        .backtest-card-metrics { display: flex; gap: 12px; margin-bottom: 10px; }
        .card-metric { flex: 1; padding: 8px 10px; background: var(--bg-deep); border: 1px solid var(--border); border-radius: 8px; text-align: center; }
        .card-metric-label { display: block; font-size: 0.65em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); margin-bottom: 2px; }
        .card-metric-val { display: block; font-size: 0.95em; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .card-metric-val.positive { color: var(--green); }
        .card-metric-val.negative { color: #ef4444; }
        .card-metric-vs { display: block; font-size: 0.6em; color: #ffffff; font-family: 'JetBrains Mono', monospace; margin-top: 1px; }
        .backtest-card-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
        .badge-featured { background: rgba(247,147,26,0.15); color: var(--accent); }
        .badge-community { background: rgba(100,149,237,0.15); color: var(--blue); }
        .badge-private { background: rgba(136,144,164,0.15); color: var(--text-muted); }
        .card-actions { display: flex; gap: 8px; margin-top: 10px; }
        .publish-modal-overlay { display: none; position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); align-items: center; justify-content: center; }
        .publish-modal-overlay.open { display: flex; }
        .publish-modal { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 16px; padding: 28px; width: 90%; max-width: 500px; position: relative; }
        .publish-modal h3 { font-size: 1.1em; font-weight: 600; margin-bottom: 16px; }
        .publish-modal label { display: block; font-size: 0.8em; color: var(--text-muted); margin-bottom: 6px; font-weight: 500; }
        .publish-modal input, .publish-modal textarea { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.9em; font-family: 'DM Sans', sans-serif; margin-bottom: 14px; }
        .publish-modal input:focus, .publish-modal textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .publish-modal textarea { resize: vertical; min-height: 80px; }
        .publish-modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 4px; }
        .publish-modal .close-btn { position: absolute; top: 12px; right: 16px; background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 1.2em; }
        .action-btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-muted); cursor: pointer; font-size: 0.75em; font-weight: 500; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; text-decoration: none; }
        .action-btn:hover { border-color: var(--border-hover); color: var(--text); }
        .action-btn.danger { border-color: #ef4444; color: #ef4444; }
        .action-btn.danger:hover { background: rgba(239,68,68,0.1); }
        .empty-state { text-align: center; padding: 40px 20px; color: var(--text-muted); }
        .empty-state h3 { font-size: 1.1em; margin-bottom: 8px; color: var(--text); }
        .hidden { display: none !important; }

        /* ── Mobile responsive ── */
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
            .nav-bar { flex-wrap: wrap; }
            .nav-link { padding: 6px 10px; font-size: 0.75em; }
        }
        @media (max-width: 400px) {
            .backtest-grid { grid-template-columns: 1fr; }
            .notif-dropdown { width: calc(100vw - 32px); right: -8px; }
        }
    </style>
</head>
<body>
<div class="container">
""" + NAV_HTML + """

    <div class="panel">
        <h2 class="page-title">My Backtests</h2>

        {# Collections section #}
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:16px;margin-bottom:14px">
            <h3 class="section-header" style="margin:0">My Collections ({{ collections|default([])|length }})</h3>
            <button class="action-btn" onclick="openCollectionModal()" style="background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#fff;border-color:transparent;padding:8px 16px">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                New Collection
            </button>
        </div>
        {% if collections|default(none) %}
        <div class="backtest-grid">
            {% for coll in collections %}
            <div class="backtest-card" style="border-left:3px solid #8b5cf6;position:relative">
                <a href="/collection/{{ coll.id }}" style="text-decoration:none;color:inherit">
                    <span class="backtest-card-badge" style="background:rgba(139,92,246,0.15);color:#8b5cf6">Collection</span>
                    <div class="backtest-card-title">{{ coll.title }}</div>
                    {% if coll.description %}<div class="backtest-card-desc">{{ coll.description[:200] }}</div>{% endif %}
                    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
                        <span class="backtest-card-tag" style="background:rgba(139,92,246,0.1);border-color:rgba(139,92,246,0.2);color:#8b5cf6">{{ coll._backtest_count }} backtest{{ 's' if coll._backtest_count != 1 }}</span>
                        {% if coll.youtube_url %}<span class="backtest-card-tag" style="background:rgba(255,0,0,0.1);border-color:rgba(255,0,0,0.2);color:#ef4444">Video</span>{% endif %}
                        {% if coll.copy_trading_url %}<span class="backtest-card-tag" style="background:rgba(16,185,129,0.1);border-color:rgba(16,185,129,0.2);color:#10b981">Copy Trading</span>{% endif %}
                        {% if coll.visibility == 'community' %}<span class="backtest-card-badge badge-community" style="margin:0">Community</span>{% elif coll.visibility == 'featured' %}<span class="backtest-card-badge badge-featured" style="margin:0">Featured</span>{% else %}<span class="backtest-card-badge badge-private" style="margin:0">Private</span>{% endif %}
                    </div>
                    <div class="backtest-card-footer">
                        <span>{{ time_ago(coll.created_at) }}</span>
                        <div class="engagement"><span>👁 {{ coll.views_count or 0 }}</span></div>
                    </div>
                </a>
                <div class="card-actions">
                    <a class="action-btn" href="/collection/{{ coll.id }}">View</a>
                    <button class="action-btn" onclick="event.stopPropagation();openEditCollectionModal('{{ coll.id }}', '{{ coll.title|e }}', '{{ (coll.description or '')|e }}', '{{ (coll.youtube_url or '')|e }}', '{{ (coll.copy_trading_url or '')|e }}')">Edit</button>
                    <button class="action-btn danger" onclick="event.stopPropagation();deleteCollection('{{ coll.id }}')">Delete</button>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        {% if published %}
        <h3 class="section-header" style="margin-top:16px">Published ({{ published|length }})</h3>
        <div class="backtest-grid">
            {% for bt in published %}
            <div class="backtest-card">
                <a href="/backtest/{{ bt.id }}" style="text-decoration:none;color:inherit">
                    {% if bt.visibility == 'community' %}<span class="backtest-card-badge badge-community">Community</span>{% endif %}
                    <div class="backtest-card-head">
                        {% if bt._asset_logo %}<img class="backtest-card-asset-logo" src="/static/logos/{{ bt._asset_logo }}" alt="{{ bt._asset_display }}">{% else %}<div class="backtest-card-asset-fallback">{{ bt._asset_display[:1] }}</div>{% endif %}
                        <div class="backtest-card-head-text">
                            <div class="backtest-card-title">{{ bt.title|title if bt.title else 'Untitled' }}</div>
                            <div class="backtest-card-asset-name">{{ bt._asset_display }}</div>
                        </div>
                        {% if bt.id in alerted_ids %}<div style="color:var(--green);flex-shrink:0;display:flex;align-items:center;margin-right:4px" title="Email alert active"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="20" x="2" y="4" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg></div>{% endif %}
                        <div class="backtest-card-mode-icon" title="{{ bt._mode_label }}">{{ bt._mode_svg|safe }}</div>
                    </div>
                    {% if bt.thumbnail %}<img class="backtest-card-thumb" src="{{ bt.thumbnail }}" alt="Chart">{% endif %}
                    {% if bt.description %}<div class="backtest-card-desc">{{ bt.description[:250] }}</div>{% endif %}
                    <div class="backtest-card-params">
                        <span class="backtest-card-tag">{{ bt._mode_label }}</span>
                        <span class="backtest-card-tag">{{ bt._strategy }}</span>
                        {% if bt._leverage %}<span class="backtest-card-tag">{{ bt._leverage }}</span>{% endif %}
                        {% if bt._start_date %}<span class="backtest-card-tag">{{ bt._start_date }}</span>{% endif %}
                    </div>
                    {% if bt._apr %}
                    <div class="backtest-card-metrics">
                        <div class="card-metric">
                            <span class="card-metric-label">APR</span>
                            <span class="card-metric-val {{ 'positive' if bt._apr|float > 0 else 'negative' }}">{{ bt._apr }}%</span>
                            <span class="card-metric-vs">vs {{ bt._apr_bh }}% B&H</span>
                        </div>
                        <div class="card-metric">
                            <span class="card-metric-label">Max DD</span>
                            <span class="card-metric-val negative">{{ bt._max_dd }}%</span>
                            <span class="card-metric-vs">vs {{ bt._max_dd_bh }}% B&H</span>
                        </div>
                    </div>
                    {% endif %}
                    <div class="backtest-card-footer">
                        <span>{{ time_ago(bt.created_at) }}</span>
                        <div class="engagement">
                            <span>♥ {{ bt.likes_count }}</span>
                            <span>💬 {{ bt.comments_count }}</span>
                            <span>👁 {{ bt.views_count or 0 }}</span>
                        </div>
                    </div>
                </a>
                <div class="card-actions">
                    <a class="action-btn" href="/backtester?{{ bt.query_string }}">Open</a>
                    <button class="action-btn" onclick="event.stopPropagation();openEditModal('{{ bt.id }}', '{{ bt.title|e }}', '{{ bt.description|e }}')">Edit</button>
                    <button class="action-btn danger" onclick="event.stopPropagation();deleteBacktest('{{ bt.id }}')">Delete</button>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        {% if saved %}
        <h3 class="section-header" style="margin-top:16px">Saved / Private ({{ saved|length }})</h3>
        <div class="backtest-grid">
            {% for bt in saved %}
            <div class="backtest-card">
                <a href="/backtest/{{ bt.id }}" style="text-decoration:none;color:inherit">
                    <span class="backtest-card-badge badge-private">Private</span>
                    <div class="backtest-card-head">
                        {% if bt._asset_logo %}<img class="backtest-card-asset-logo" src="/static/logos/{{ bt._asset_logo }}" alt="{{ bt._asset_display }}">{% else %}<div class="backtest-card-asset-fallback">{{ bt._asset_display[:1] }}</div>{% endif %}
                        <div class="backtest-card-head-text">
                            <div class="backtest-card-title">{{ bt.title|title if bt.title else 'Saved Backtest' }}</div>
                            <div class="backtest-card-asset-name">{{ bt._asset_display }}</div>
                        </div>
                        {% if bt.id in alerted_ids %}<div style="color:var(--green);flex-shrink:0;display:flex;align-items:center;margin-right:4px" title="Email alert active"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="20" x="2" y="4" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg></div>{% endif %}
                        <div class="backtest-card-mode-icon" title="{{ bt._mode_label }}">{{ bt._mode_svg|safe }}</div>
                    </div>
                    {% if bt.thumbnail %}<img class="backtest-card-thumb" src="{{ bt.thumbnail }}" alt="Chart">{% endif %}
                    <div class="backtest-card-params">
                        <span class="backtest-card-tag">{{ bt._mode_label }}</span>
                        <span class="backtest-card-tag">{{ bt._strategy }}</span>
                        {% if bt._leverage %}<span class="backtest-card-tag">{{ bt._leverage }}</span>{% endif %}
                        {% if bt._start_date %}<span class="backtest-card-tag">{{ bt._start_date }}</span>{% endif %}
                    </div>
                    {% if bt._apr %}
                    <div class="backtest-card-metrics">
                        <div class="card-metric">
                            <span class="card-metric-label">APR</span>
                            <span class="card-metric-val {{ 'positive' if bt._apr|float > 0 else 'negative' }}">{{ bt._apr }}%</span>
                            <span class="card-metric-vs">vs {{ bt._apr_bh }}% B&H</span>
                        </div>
                        <div class="card-metric">
                            <span class="card-metric-label">Max DD</span>
                            <span class="card-metric-val negative">{{ bt._max_dd }}%</span>
                            <span class="card-metric-vs">vs {{ bt._max_dd_bh }}% B&H</span>
                        </div>
                    </div>
                    {% endif %}
                    <div class="backtest-card-footer">
                        <span>{{ time_ago(bt.created_at) }}</span>
                    </div>
                </a>
                <div class="card-actions">
                    <a class="action-btn" href="/backtester?{{ bt.query_string }}">Open</a>
                    <button class="action-btn" onclick="event.stopPropagation();openEditModal('{{ bt.id }}', '{{ bt.title|e }}', '{{ (bt.description or '')|e }}')">Edit</button>
                    <button class="action-btn danger" onclick="event.stopPropagation();deleteBacktest('{{ bt.id }}')">Delete</button>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        {% if not published and not saved and not collections|default(none) %}
        <div class="empty-state">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#f7931a" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:16px;opacity:0.7"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            <h3>No backtests yet</h3>
            <p style="margin-bottom:20px">Create your first backtest, then save or publish it to build your collection.</p>
            <a href="/backtester" style="display:inline-flex;align-items:center;gap:8px;padding:12px 28px;border-radius:10px;background:#f7931a;color:#fff;font-weight:600;font-size:0.9em;text-decoration:none;font-family:'DM Sans',sans-serif;transition:all 0.2s ease;box-shadow:0 2px 12px rgba(247,147,26,0.3);" onmouseover="this.style.background='#e8850f';this.style.transform='translateY(-1px)'" onmouseout="this.style.background='#f7931a';this.style.transform=''">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                Create Your First Backtest
            </a>
        </div>
        {% endif %}
    </div>
</div>
<script src="/static/js/nav.js"></script>
<script>
function deleteBacktest(backtestId) {
    _swal.fire({
        title: 'Delete this backtest?', icon: 'warning',
        showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/backtest/' + backtestId, { method: 'DELETE' })
        .then(function() { location.reload(); });
    });
}
function openEditModal(backtestId, title, desc) {
    document.getElementById('edit-bt-id').value = backtestId;
    document.getElementById('edit-title').value = title || '';
    document.getElementById('edit-desc').value = desc || '';
    document.getElementById('edit-modal-overlay').classList.add('open');
    document.getElementById('edit-title').focus();
}
function closeEditModal() {
    document.getElementById('edit-modal-overlay').classList.remove('open');
}
function saveEdit() {
    var btId = document.getElementById('edit-bt-id').value;
    var title = document.getElementById('edit-title').value.trim();
    var desc = document.getElementById('edit-desc').value.trim();
    fetch('/api/backtest/' + btId, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, description: desc})
    }).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    }).then(function(data) {
        closeEditModal();
        location.reload();
    }).catch(function(e) { _swal.fire({icon:'error', title:'Failed to save', text:e.message}); });
}
// Collection functions
function openCollectionModal() {
    document.getElementById('coll-modal-overlay').classList.add('open');
    document.getElementById('coll-title').focus();
}
function closeCollectionModal() {
    document.getElementById('coll-modal-overlay').classList.remove('open');
    document.getElementById('coll-title').value = '';
    document.getElementById('coll-desc').value = '';
    document.getElementById('coll-youtube').value = '';
    document.getElementById('coll-copytrading').value = '';
}
function saveCollection() {
    var title = document.getElementById('coll-title').value.trim();
    if (!title) { _swal.fire({icon:'warning', title:'Title required'}); return; }
    var desc = document.getElementById('coll-desc').value.trim();
    var yt = document.getElementById('coll-youtube').value.trim();
    var ct = document.getElementById('coll-copytrading').value.trim();
    fetch('/api/collection/create', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, description: desc, youtube_url: yt, copy_trading_url: ct})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) throw new Error(data.error);
        closeCollectionModal();
        location.reload();
    }).catch(function(e) { _swal.fire({icon:'error', title:'Failed', text:e.message}); });
}
function deleteCollection(collId) {
    _swal.fire({
        title: 'Delete this collection?', text: 'Backtests inside will not be deleted.',
        icon: 'warning', showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/collection/' + collId + '/delete', { method: 'POST' })
        .then(function() { location.reload(); });
    });
}
function openEditCollectionModal(id, title, desc, yt, ct) {
    document.getElementById('edit-coll-id').value = id;
    document.getElementById('edit-coll-title').value = title || '';
    document.getElementById('edit-coll-desc').value = desc || '';
    document.getElementById('edit-coll-youtube').value = yt || '';
    document.getElementById('edit-coll-copytrading').value = ct || '';
    document.getElementById('edit-coll-modal-overlay').classList.add('open');
    document.getElementById('edit-coll-title').focus();
}
function closeEditCollectionModal() {
    document.getElementById('edit-coll-modal-overlay').classList.remove('open');
}
function saveEditCollection() {
    var id = document.getElementById('edit-coll-id').value;
    var title = document.getElementById('edit-coll-title').value.trim();
    var desc = document.getElementById('edit-coll-desc').value.trim();
    var yt = document.getElementById('edit-coll-youtube').value.trim();
    var ct = document.getElementById('edit-coll-copytrading').value.trim();
    fetch('/api/collection/' + id + '/update', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, description: desc, youtube_url: yt, copy_trading_url: ct})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) throw new Error(data.error);
        closeEditCollectionModal();
        location.reload();
    }).catch(function(e) { _swal.fire({icon:'error', title:'Failed', text:e.message}); });
}
</script>

<!-- Edit Backtest Modal -->
<div class="publish-modal-overlay" id="edit-modal-overlay">
    <div class="publish-modal">
        <button class="close-btn" onclick="closeEditModal()">&times;</button>
        <h3>Edit Backtest</h3>
        <input type="hidden" id="edit-bt-id">
        <label for="edit-title">Title</label>
        <input type="text" id="edit-title" maxlength="120">
        <label for="edit-desc">Description</label>
        <textarea id="edit-desc"></textarea>
        <div class="publish-modal-actions">
            <button class="action-btn" onclick="closeEditModal()">Cancel</button>
            <button class="action-btn primary" onclick="saveEdit()">Save Changes</button>
        </div>
    </div>
</div>

<!-- New Collection Modal -->
<div class="publish-modal-overlay" id="coll-modal-overlay">
    <div class="publish-modal">
        <button class="close-btn" onclick="closeCollectionModal()">&times;</button>
        <h3>New Collection</h3>
        <label for="coll-title">Title</label>
        <input type="text" id="coll-title" maxlength="120" placeholder="e.g. Bitcoin Moving Average Strategies">
        <label for="coll-desc">Description (optional)</label>
        <textarea id="coll-desc" placeholder="Describe what this collection is about..."></textarea>
        <label for="coll-youtube">Video URL (optional)</label>
        <input type="text" id="coll-youtube" placeholder="YouTube or Vimeo URL">
        <label for="coll-copytrading">Copy Trading URL (optional)</label>
        <input type="text" id="coll-copytrading" placeholder="https://the-bitcoin-strategy.com/automatic-copy-trading">
        <div class="publish-modal-actions">
            <button class="action-btn" onclick="closeCollectionModal()">Cancel</button>
            <button class="action-btn" onclick="saveCollection()" style="background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#fff;border-color:transparent">Create Collection</button>
        </div>
    </div>
</div>

<!-- Edit Collection Modal -->
<div class="publish-modal-overlay" id="edit-coll-modal-overlay">
    <div class="publish-modal">
        <button class="close-btn" onclick="closeEditCollectionModal()">&times;</button>
        <h3>Edit Collection</h3>
        <input type="hidden" id="edit-coll-id">
        <label for="edit-coll-title">Title</label>
        <input type="text" id="edit-coll-title" maxlength="120">
        <label for="edit-coll-desc">Description</label>
        <textarea id="edit-coll-desc"></textarea>
        <label for="edit-coll-youtube">Video URL</label>
        <input type="text" id="edit-coll-youtube" placeholder="YouTube or Vimeo URL">
        <label for="edit-coll-copytrading">Copy Trading URL</label>
        <input type="text" id="edit-coll-copytrading" placeholder="https://the-bitcoin-strategy.com/automatic-copy-trading">
        <div class="publish-modal-actions">
            <button class="action-btn" onclick="closeEditCollectionModal()">Cancel</button>
            <button class="action-btn" onclick="saveEditCollection()" style="background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#fff;border-color:transparent">Save Changes</button>
        </div>
    </div>
</div>
</body>
</html>
"""


ADMIN_ASSETS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Asset Management — Bitcoin Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --bg-deep: #0f1118; --bg-base: #161922; --bg-surface: #1b1f2e;
            --bg-elevated: #252a3a; --border: #2a2f40;
            --text: #e8e9ed; --text-muted: #8890a4; --text-dim: #5a6178;
            --accent: #f7931a; --accent-hover: #ffa940;
            --green: #34d399; --red: #ef4444;
            --blue: #6495ED;
        }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        .container { max-width: 1000px; margin: 0 auto; padding: 24px 20px; }
        .header { text-align: center; margin-bottom: 32px; position: relative; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .nav-bar { display: flex; justify-content: center; gap: 4px; margin-bottom: 24px; flex-wrap: wrap; position: relative; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; border: 1px solid transparent; transition: all 0.2s ease; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: rgba(247,147,26,0.2); }
        /* Theme toggle */
        .theme-toggle { background: none; border: 1px solid var(--border); cursor: pointer; color: var(--text-muted); padding: 7px; border-radius: 8px; transition: all 0.2s ease; display: flex; align-items: center; justify-content: center; }
        .theme-toggle:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border-hover); }
        .theme-toggle svg { width: 16px; height: 16px; }
        .theme-toggle .icon-sun { display: none; }
        .theme-toggle .icon-moon { display: block; }
        [data-theme="light"] .theme-toggle .icon-sun { display: block; }
        [data-theme="light"] .theme-toggle .icon-moon { display: none; }
        .nav-right-group { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; z-index: 9999; }
        .notif-bell-wrap { position: relative; }
        .notif-bell { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 8px; border-radius: 8px; position: relative; transition: all 0.2s ease; }
        .notif-bell:hover { color: var(--text); background: var(--bg-elevated); }
        .notif-badge { position: absolute; top: 2px; right: 2px; background: #e74c3c; color: #fff; font-size: 0.65em; font-weight: 700; min-width: 16px; height: 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; padding: 0 4px; font-family: 'JetBrains Mono', monospace; }
        .notif-badge.hidden { display: none; }
        .notif-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 340px; background: var(--bg-surface, var(--bg-base)); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; }
        .notif-dropdown.hidden { display: none; }
        .notif-dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.85em; color: var(--text); }
        .notif-list { max-height: 320px; overflow-y: auto; }
        .notif-item { display: block; padding: 12px 16px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); font-size: 0.82em; transition: background 0.15s ease; cursor: pointer; }
        .notif-item.notif-unread { background: rgba(100,149,237,0.08); border-left: 3px solid var(--accent); }
        .notif-item.notif-read { opacity: 0.55; }
        .notif-item:hover { background: var(--bg-elevated); opacity: 1; }
        .notif-item:last-child { border-bottom: none; }
        .notif-item-text { line-height: 1.4; }
        .notif-item-text strong { color: var(--accent); font-weight: 600; }
        .notif-item-time { color: var(--text-dim); font-size: 0.78em; margin-top: 4px; }
        .notif-empty { padding: 24px 16px; text-align: center; color: var(--text-dim); font-size: 0.82em; }
        /* Avatar */
        .avatar-wrap { position: relative; }
        .avatar-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 50%; transition: all 0.2s ease; }
        .avatar-btn:hover { background: var(--bg-elevated); }
        .avatar-img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); display: block; }
        .avatar-initials { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 700; color: #fff; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 220px; background: var(--bg-surface, var(--bg-base)); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; padding: 6px 0; }
        .avatar-dropdown.hidden { display: none; }
        .avatar-dropdown-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; color: var(--text-muted); text-decoration: none; font-size: 0.82em; font-weight: 500; transition: all 0.15s ease; font-family: 'DM Sans', sans-serif; }
        .avatar-dropdown-item:hover { background: var(--bg-elevated); color: var(--text); }
        .avatar-dropdown-item svg { width: 16px; height: 16px; flex-shrink: 0; }
        .avatar-dropdown-divider { height: 1px; background: var(--border); margin: 4px 0; }
        .avatar-dropdown-logout { color: #e74c3c; }
        .avatar-dropdown-logout:hover { background: rgba(231,76,60,0.1); color: #e74c3c; }
        /* Small avatars for cards and comments */
        .card-avatar-img { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; margin-right: 4px; }
        .card-avatar-initials { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 0.6em; font-weight: 700; color: #fff; vertical-align: middle; margin-right: 4px; }
        .comment-avatar-img { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; }
        .comment-avatar-initials { width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.65em; font-weight: 700; color: #fff; flex-shrink: 0; }
        .panel { background: var(--bg-base); border: 1px solid var(--border); border-radius: 16px; padding: 28px; }
        .page-title { font-size: 1.1em; font-weight: 700; margin-bottom: 20px; }

        /* Upload section */
        .upload-section { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 24px; }
        .upload-section h3 { font-size: 0.9em; font-weight: 600; margin-bottom: 16px; }
        .upload-dropzone {
            border: 2px dashed var(--border); border-radius: 12px;
            padding: 24px 16px; text-align: center; cursor: pointer;
            transition: all 0.2s ease; margin-bottom: 12px; background: var(--bg-deep);
        }
        .upload-dropzone:hover, .upload-dropzone.dragover { border-color: var(--accent); background: rgba(247,147,26,0.04); }
        .upload-dropzone-text { font-size: 0.8em; color: var(--text-dim); }
        .upload-dropzone-text strong { color: var(--accent); }
        .upload-dropzone-file { font-size: 0.75em; color: var(--green); margin-top: 8px; font-family: 'JetBrains Mono', monospace; }
        .upload-row { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
        .upload-field { flex: 1; min-width: 140px; }
        .upload-field label { display: block; font-size: 0.7em; font-weight: 600; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
        .upload-field input, .upload-field select {
            width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px;
            background: var(--bg-deep); color: var(--text); font-size: 0.85em;
            font-family: 'DM Sans', sans-serif; outline: none;
        }
        .upload-field input:focus, .upload-field select:focus { border-color: var(--accent); }
        .btn { padding: 8px 16px; border: none; border-radius: 8px; font-size: 0.85em; font-weight: 600; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.15s ease; }
        .btn-primary { background: linear-gradient(135deg, var(--accent), #e8850f); color: var(--bg-deep); }
        .btn-primary:hover { opacity: 0.9; }
        .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
        .upload-msg { font-size: 0.75em; margin-top: 8px; font-family: 'JetBrains Mono', monospace; }
        .upload-msg.error { color: var(--red); }

        /* Bulk upload */
        .bulk-file-list { margin: 12px 0; max-height: 320px; overflow-y: auto; }
        .bulk-file-item {
            display: flex; align-items: center; gap: 10px; padding: 8px 12px;
            border: 1px solid var(--border); border-radius: 8px; margin-bottom: 6px;
            background: var(--bg-deep); font-size: 0.8em;
        }
        .bulk-file-item .file-ticker { font-family: 'JetBrains Mono', monospace; color: var(--text-dim); min-width: 80px; }
        .bulk-file-item .file-name { flex: 1; color: var(--text); font-weight: 600; }
        .bulk-file-item .file-name input {
            width: 100%; padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px;
            background: var(--bg-surface); color: var(--text); font-size: 0.95em;
            font-family: 'DM Sans', sans-serif; outline: none;
        }
        .bulk-file-item .file-name input:focus { border-color: var(--accent); }
        .bulk-file-item .file-status { min-width: 24px; text-align: center; }
        .bulk-file-item .file-remove { cursor: pointer; color: var(--text-dim); font-size: 1.1em; padding: 0 4px; }
        .bulk-file-item .file-remove:hover { color: var(--red); }
        .bulk-file-item.resolving { opacity: 0.7; }
        .bulk-file-item.resolved .file-name input { border-color: var(--green); }
        .bulk-file-item.failed .file-name input { border-color: var(--red); }
        .bulk-file-item.uploaded { opacity: 0.5; }
        .bulk-file-item.uploaded .file-status { color: var(--green); }
        .bulk-file-item.upload-error .file-status { color: var(--red); }
        .bulk-progress { font-size: 0.75em; color: var(--text-dim); margin-top: 8px; font-family: 'JetBrains Mono', monospace; }
        .upload-msg.success { color: var(--green); }

        /* Asset table */
        .asset-table { width: 100%; border-collapse: collapse; }
        .asset-table th {
            text-align: left; font-size: 0.7em; font-weight: 600; color: var(--text-dim);
            text-transform: uppercase; letter-spacing: 0.05em; padding: 8px 10px;
            border-bottom: 1px solid var(--border);
        }
        .asset-table td { padding: 10px; border-bottom: 1px solid var(--border); font-size: 0.85em; vertical-align: middle; }
        .asset-table tr:hover { background: var(--bg-surface); }
        .asset-table tr:last-child td { border-bottom: none; }
        .asset-logo { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; }
        .asset-placeholder { width: 28px; height: 28px; border-radius: 50%; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; font-size: 0.6em; font-weight: 700; color: var(--text-dim); }
        .asset-name-cell { display: flex; align-items: center; gap: 10px; }
        .asset-name-text { font-weight: 600; }
        .cat-select {
            padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px;
            background: var(--bg-deep); color: var(--text); font-size: 0.85em;
            font-family: 'DM Sans', sans-serif; outline: none; cursor: pointer;
        }
        .cat-select:focus { border-color: var(--accent); }
        .ticker-input {
            width: 70px; padding: 4px 8px; border: 1px solid transparent; border-radius: 6px;
            background: transparent; color: var(--text-muted); font-size: 0.8em;
            font-family: 'JetBrains Mono', monospace; outline: none; text-transform: uppercase;
        }
        .ticker-input:hover { border-color: var(--border); background: var(--bg-deep); }
        .ticker-input:focus { border-color: var(--accent); background: var(--bg-deep); color: var(--text); }
        .action-btn-sm {
            padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px;
            background: none; color: var(--text-muted); font-size: 0.75em; cursor: pointer;
            font-family: 'DM Sans', sans-serif; transition: all 0.15s ease;
        }
        .action-btn-sm:hover { border-color: var(--text-muted); color: var(--text); }
        .action-btn-sm.danger { color: var(--red); border-color: rgba(239,68,68,0.3); }
        .action-btn-sm.danger:hover { border-color: var(--red); background: rgba(239,68,68,0.08); }
        .actions-cell { display: flex; gap: 6px; }

        /* Rename modal */
        .modal-overlay { display: none; position: fixed; inset: 0; z-index: 1100; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); align-items: center; justify-content: center; }
        .modal-overlay.open { display: flex; }
        .modal-box { background: var(--bg-base); border: 1px solid var(--border); border-radius: 16px; padding: 24px; width: 90%; max-width: 400px; }
        .modal-box h3 { font-size: 0.9em; margin-bottom: 16px; }
        .modal-box input { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-deep); color: var(--text); font-size: 0.85em; font-family: 'DM Sans', sans-serif; outline: none; margin-bottom: 16px; }
        .modal-box input:focus { border-color: var(--accent); }
        .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

        /* ── Mobile responsive ── */
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
            .nav-bar { flex-wrap: wrap; }
            .nav-link { padding: 6px 10px; font-size: 0.75em; }
        }
        @media (max-width: 400px) {
            .notif-dropdown { width: calc(100vw - 32px); right: -8px; }
        }
    </style>
</head>
<body>
<div class="container">
""" + NAV_HTML + """

    <div class="panel">
        <h2 class="page-title">Asset Management</h2>

        <!-- API Usage -->
        <div id="api-usage-panel" style="margin-bottom:24px;padding:16px 20px;background:var(--bg-surface);border:1px solid var(--border);border-radius:12px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
                <span style="font-weight:600;font-size:0.9em">CoinGecko API Usage</span>
                <span id="api-usage-text" style="font-family:'JetBrains Mono',monospace;font-size:0.85em;color:var(--text-muted)">Loading...</span>
            </div>
            <div style="height:8px;background:var(--bg-elevated);border-radius:4px;overflow:hidden">
                <div id="api-usage-bar" style="height:100%;width:0%;border-radius:4px;transition:width 0.5s ease,background 0.3s ease;background:var(--green)"></div>
            </div>
            <div id="api-usage-status" style="margin-top:6px;font-size:0.78em;color:var(--text-dim)"></div>
        </div>
        <script>
        (function() {
            fetch('/api/admin/api-usage').then(function(r){return r.json()}).then(function(d){
                var pct = d.pct || 0;
                document.getElementById('api-usage-text').textContent = d.calls + ' / ' + d.limit + ' calls (' + pct + '%)';
                var bar = document.getElementById('api-usage-bar');
                bar.style.width = Math.min(pct, 100) + '%';
                if (pct >= 80) bar.style.background = 'var(--red)';
                else if (pct >= 50) bar.style.background = 'var(--accent)';
                var status = document.getElementById('api-usage-status');
                if (pct >= 100) status.textContent = 'Live price updates DISABLED — quota exceeded';
                else status.textContent = 'Live price updates active — resets monthly';
                status.style.color = pct >= 100 ? 'var(--red)' : 'var(--text-dim)';
            }).catch(function(){
                document.getElementById('api-usage-text').textContent = 'Unavailable';
            });
        })();
        </script>

        <!-- Upload -->
        <div class="upload-section">
            <h3>Add New Asset</h3>
            <div class="upload-dropzone" id="upload-dropzone" onclick="document.getElementById('upload-file-input').click()">
                <div class="upload-dropzone-text"><strong>Click to browse</strong> or drag & drop<br>CSV file with <code>time</code> and <code>close</code> columns</div>
                <div class="upload-dropzone-file" id="upload-file-name"></div>
                <input type="file" id="upload-file-input" accept=".csv" style="display:none" onchange="handleUploadFile(this.files[0])">
            </div>
            <div class="upload-row">
                <div class="upload-field" style="flex:2"><label>Asset Name</label><input type="text" id="upload-asset-name" placeholder="e.g. Litecoin, AMD, Natural Gas"></div>
                <div class="upload-field"><label>Category</label>
                    <select id="upload-asset-type">
                        <option value="crypto">Crypto</option>
                        <option value="crypto_agg">Crypto Aggregate</option>
                        <option value="stock">Stock</option>
                        <option value="index">Stock Index</option>
                        <option value="metal">Precious Metal</option>
                        <option value="commodity">Commodity</option>
                    </select>
                </div>
                <div><button class="btn btn-primary" id="upload-confirm-btn" onclick="submitUpload()" disabled>Upload</button></div>
            </div>
            <div class="upload-msg" id="upload-message"></div>
        </div>

        <!-- Bulk Upload -->
        <div class="upload-section">
            <h3>Bulk Upload Assets</h3>
            <div class="upload-dropzone" id="bulk-dropzone" onclick="document.getElementById('bulk-file-input').click()">
                <div class="upload-dropzone-text"><strong>Click to browse</strong> or drag & drop<br>Multiple CSV files &mdash; ticker symbol in filename is auto-resolved to full name</div>
                <input type="file" id="bulk-file-input" accept=".csv" multiple style="display:none" onchange="handleBulkFiles(this.files)">
            </div>
            <div class="upload-row" style="margin-bottom:8px">
                <div class="upload-field"><label>Category for all</label>
                    <select id="bulk-asset-type">
                        <option value="crypto">Crypto</option>
                        <option value="crypto_agg">Crypto Aggregate</option>
                        <option value="stock">Stock</option>
                        <option value="index">Stock Index</option>
                        <option value="metal">Precious Metal</option>
                        <option value="commodity">Commodity</option>
                    </select>
                </div>
                <div><button class="btn btn-primary" id="bulk-upload-btn" onclick="submitBulkUpload()" disabled>Upload All</button></div>
            </div>
            <div class="bulk-file-list" id="bulk-file-list"></div>
            <div class="bulk-progress" id="bulk-progress"></div>
        </div>

        <!-- Asset List -->
        <table class="asset-table">
            <thead><tr><th>Asset</th><th>Ticker</th><th>Category</th><th>Start Date</th><th>Actions</th></tr></thead>
            <tbody>
            {% for name in asset_names %}
            <tr id="row-{{ name|replace(' ', '_') }}">
                <td>
                    <div class="asset-name-cell">
                        {% if asset_logos.get(name) %}<img class="asset-logo" src="/static/logos/{{ asset_logos[name] }}" alt="{{ name }}">{% else %}<div class="asset-placeholder">{{ name[:2]|upper }}</div>{% endif %}
                        <span class="asset-name-text">{{ name }}</span>
                    </div>
                </td>
                <td><input class="ticker-input" value="{{ asset_tickers.get(name, '') }}" data-asset="{{ name }}" data-orig="{{ asset_tickers.get(name, '') }}" onblur="saveTicker(this)" onkeydown="if(event.key==='Enter'){this.blur();}"></td>
                <td>
                    <select class="cat-select" data-asset="{{ name }}" onchange='changeCategory({{ name|tojson }}, this.value)'>
                        <option value="crypto" {{ 'selected' if name in crypto_names }}>Crypto</option>
                        <option value="crypto_agg" {{ 'selected' if name in crypto_agg_names }}>Crypto Aggregate</option>
                        <option value="stock" {{ 'selected' if name in stock_names }}>Stock</option>
                        <option value="index" {{ 'selected' if name in index_names }}>Index</option>
                        <option value="metal" {{ 'selected' if name in metal_names }}>Precious Metal</option>
                        <option value="commodity" {{ 'selected' if name in commodity_names }}>Commodity</option>
                    </select>
                </td>
                <td style="font-family:'JetBrains Mono',monospace;font-size:0.8em;color:var(--text-muted)">{{ asset_starts.get(name, '') }}</td>
                <td>
                    <div class="actions-cell">
                        <button class="action-btn-sm" onclick='openRenameModal({{ name|tojson }})'>Rename</button>
                        <button class="action-btn-sm danger" onclick='deleteAsset({{ name|tojson }})'>Delete</button>
                    </div>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
</div>

<!-- Rename Modal -->
<div class="modal-overlay" id="rename-modal">
    <div class="modal-box">
        <h3>Rename Asset</h3>
        <input type="hidden" id="rename-old-name">
        <input type="text" id="rename-new-name" placeholder="New name">
        <div class="modal-actions">
            <button class="action-btn-sm" onclick="closeRenameModal()">Cancel</button>
            <button class="btn btn-primary" onclick="submitRename()">Rename</button>
        </div>
    </div>
</div>

<script src="/static/js/nav.js"></script>
<script>

// Upload
var _uploadFile = null;
function handleUploadFile(file) {
    if (!file) return;
    if (!file.name.endsWith('.csv')) { document.getElementById('upload-message').innerHTML = '<span class="upload-msg error">Please select a CSV file</span>'; return; }
    _uploadFile = file;
    document.getElementById('upload-file-name').textContent = file.name + ' (' + (file.size / 1024).toFixed(1) + ' KB)';
    document.getElementById('upload-message').innerHTML = '';
    _checkReady();
}
function _checkReady() {
    document.getElementById('upload-confirm-btn').disabled = !(_uploadFile && document.getElementById('upload-asset-name').value.trim());
}
document.getElementById('upload-asset-name').addEventListener('input', _checkReady);
(function() {
    var dz = document.getElementById('upload-dropzone');
    ['dragenter','dragover'].forEach(function(ev) { dz.addEventListener(ev, function(e) { e.preventDefault(); dz.classList.add('dragover'); }); });
    ['dragleave','drop'].forEach(function(ev) { dz.addEventListener(ev, function(e) { e.preventDefault(); dz.classList.remove('dragover'); }); });
    dz.addEventListener('drop', function(e) { if (e.dataTransfer.files.length) handleUploadFile(e.dataTransfer.files[0]); });
})();
function submitUpload() {
    var btn = document.getElementById('upload-confirm-btn');
    var msg = document.getElementById('upload-message');
    var name = document.getElementById('upload-asset-name').value.trim();
    var type = document.getElementById('upload-asset-type').value;
    if (!_uploadFile || !name) return;
    btn.disabled = true; btn.textContent = 'Uploading...'; msg.innerHTML = '';
    var fd = new FormData(); fd.append('file', _uploadFile); fd.append('asset_name', name); fd.append('asset_type', type);
    fetch('/api/upload-asset', { method: 'POST', body: fd })
        .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
        .then(function(res) {
            if (res.ok && res.data.ok) { msg.innerHTML = '<span class="upload-msg success">Added! Reloading...</span>'; setTimeout(function() { location.reload(); }, 1000); }
            else { msg.innerHTML = '<span class="upload-msg error">' + (res.data.error || 'Failed') + '</span>'; btn.disabled = false; btn.textContent = 'Upload'; }
        }).catch(function(e) { msg.innerHTML = '<span class="upload-msg error">' + e.message + '</span>'; btn.disabled = false; btn.textContent = 'Upload'; });
}

// Bulk upload
var _bulkFiles = [];
function extractTicker(filename) {
    var name = filename.replace(/\.csv$/i, '').trim();
    // TradingView style: "COINBASE ARBUSD, 1D" or "CRYPTO ETCUSD, 1D"
    name = name.replace(/,\s*\d+[DWMH]$/i, '').trim();
    name = name.replace(/^(COINBASE|BINANCE|BITSTAMP|CRYPTO(?:COM)?)\s+/i, '').trim();
    name = name.replace(/USD[T]?$/i, '').trim();
    // If nothing left or still has spaces, fall back to original minus .csv
    if (!name) name = filename.replace(/\.csv$/i, '').trim();
    return name.toUpperCase();
}
function handleBulkFiles(fileList) {
    var list = document.getElementById('bulk-file-list');
    for (var i = 0; i < fileList.length; i++) {
        var file = fileList[i];
        if (!file.name.endsWith('.csv')) continue;
        var ticker = extractTicker(file.name);
        var idx = _bulkFiles.length;
        _bulkFiles.push({ file: file, ticker: ticker, resolvedName: '', status: 'pending' });
        var item = document.createElement('div');
        item.className = 'bulk-file-item resolving';
        item.id = 'bulk-item-' + idx;
        item.innerHTML = '<span class="file-ticker">' + ticker + '</span>' +
            '<span class="file-name"><input type="text" id="bulk-name-' + idx + '" value="Resolving..." readonly></span>' +
            '<span class="file-status" id="bulk-status-' + idx + '"></span>' +
            '<span class="file-remove" onclick="removeBulkItem(' + idx + ')">&times;</span>';
        list.appendChild(item);
        resolveTicker(idx, ticker);
    }
    document.getElementById('bulk-file-input').value = '';
    _checkBulkReady();
}
function resolveTicker(idx, ticker) {
    var cat = document.getElementById('bulk-asset-type').value;
    fetch('/api/resolve-ticker', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ticker: ticker, category: cat})
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        var item = document.getElementById('bulk-item-' + idx);
        if (!item) return;
        var nameInput = document.getElementById('bulk-name-' + idx);
        nameInput.value = d.name || ticker;
        nameInput.readOnly = false;
        _bulkFiles[idx].resolvedName = d.name || ticker;
        item.classList.remove('resolving');
        item.classList.add(d.ok ? 'resolved' : 'failed');
        _checkBulkReady();
    }).catch(function() {
        var item = document.getElementById('bulk-item-' + idx);
        if (!item) return;
        var nameInput = document.getElementById('bulk-name-' + idx);
        nameInput.value = ticker;
        nameInput.readOnly = false;
        _bulkFiles[idx].resolvedName = ticker;
        item.classList.remove('resolving');
        item.classList.add('failed');
        _checkBulkReady();
    });
}
function removeBulkItem(idx) {
    _bulkFiles[idx] = null;
    var item = document.getElementById('bulk-item-' + idx);
    if (item) item.remove();
    _checkBulkReady();
}
function _checkBulkReady() {
    var hasFiles = _bulkFiles.some(function(f) { return f && f.status !== 'done'; });
    var allResolved = !_bulkFiles.some(function(f) { return f && f.status === 'pending' && document.getElementById('bulk-item-' + _bulkFiles.indexOf(f)) && document.getElementById('bulk-item-' + _bulkFiles.indexOf(f)).classList.contains('resolving'); });
    document.getElementById('bulk-upload-btn').disabled = !hasFiles || !allResolved;
}
function submitBulkUpload() {
    var btn = document.getElementById('bulk-upload-btn');
    btn.disabled = true; btn.textContent = 'Uploading...';
    var cat = document.getElementById('bulk-asset-type').value;
    var pending = [];
    for (var i = 0; i < _bulkFiles.length; i++) {
        if (_bulkFiles[i] && _bulkFiles[i].status !== 'done') {
            var nameInput = document.getElementById('bulk-name-' + i);
            _bulkFiles[i].resolvedName = nameInput ? nameInput.value.trim() : _bulkFiles[i].resolvedName;
            pending.push(i);
        }
    }
    var total = pending.length, done = 0, errors = 0;
    var prog = document.getElementById('bulk-progress');
    function uploadNext() {
        if (pending.length === 0) {
            prog.textContent = done + '/' + total + ' uploaded' + (errors ? ', ' + errors + ' failed' : '') + '. Reloading...';
            btn.textContent = 'Upload All';
            setTimeout(function() { location.reload(); }, 1200);
            return;
        }
        var idx = pending.shift();
        var entry = _bulkFiles[idx];
        var fd = new FormData();
        fd.append('file', entry.file);
        fd.append('asset_name', entry.resolvedName);
        fd.append('asset_type', cat);
        fd.append('ticker', entry.ticker);
        var statusEl = document.getElementById('bulk-status-' + idx);
        var itemEl = document.getElementById('bulk-item-' + idx);
        fetch('/api/upload-asset', { method: 'POST', body: fd })
            .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
            .then(function(res) {
                done++;
                if (res.ok && res.data.ok) {
                    entry.status = 'done';
                    if (itemEl) itemEl.className = 'bulk-file-item uploaded';
                    if (statusEl) statusEl.textContent = '\u2713';
                } else {
                    errors++;
                    if (itemEl) itemEl.className = 'bulk-file-item upload-error';
                    if (statusEl) statusEl.textContent = res.data.error || '\u2717';
                }
                prog.textContent = done + '/' + total + ' uploaded' + (errors ? ', ' + errors + ' failed' : '');
                uploadNext();
            }).catch(function(e) {
                done++; errors++;
                if (itemEl) itemEl.className = 'bulk-file-item upload-error';
                if (statusEl) statusEl.textContent = e.message;
                prog.textContent = done + '/' + total + ' uploaded, ' + errors + ' failed';
                uploadNext();
            });
    }
    uploadNext();
}
(function() {
    var dz = document.getElementById('bulk-dropzone');
    ['dragenter','dragover'].forEach(function(ev) { dz.addEventListener(ev, function(e) { e.preventDefault(); dz.classList.add('dragover'); }); });
    ['dragleave','drop'].forEach(function(ev) { dz.addEventListener(ev, function(e) { e.preventDefault(); dz.classList.remove('dragover'); }); });
    dz.addEventListener('drop', function(e) { if (e.dataTransfer.files.length) handleBulkFiles(e.dataTransfer.files); });
})();
document.getElementById('bulk-asset-type').addEventListener('change', function() {
    for (var i = 0; i < _bulkFiles.length; i++) {
        if (_bulkFiles[i] && _bulkFiles[i].status !== 'done') {
            var item = document.getElementById('bulk-item-' + i);
            if (item) { item.className = 'bulk-file-item resolving'; }
            var nameInput = document.getElementById('bulk-name-' + i);
            if (nameInput) { nameInput.value = 'Resolving...'; nameInput.readOnly = true; }
            resolveTicker(i, _bulkFiles[i].ticker);
        }
    }
});

// Category change
function saveTicker(el) {
    var asset = el.getAttribute('data-asset');
    var orig = el.getAttribute('data-orig');
    var val = el.value.trim().toUpperCase();
    el.value = val;
    if (val === orig) return;
    fetch('/api/update-ticker', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({asset: asset, ticker: val})
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.ok) { el.setAttribute('data-orig', val); el.style.borderColor = 'var(--green)'; setTimeout(function(){ el.style.borderColor = ''; }, 1000); }
        else { _swal.fire({icon:'error', title:'Error', text: d.error}); el.value = orig; }
    }).catch(function(e) { _swal.fire({icon:'error', title:'Error', text: e.message}); el.value = orig; });
}
function changeCategory(asset, cat) {
    fetch('/api/change-asset-category', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({asset: asset, category: cat})
    }).then(function(r) {
        if (!r.ok) throw new Error('Server returned ' + r.status);
        return r.json();
    }).then(function(d) {
        if (d.error) _swal.fire({icon:'error', title:'Error', text: d.error});
        else _swal.fire({icon:'success', title:'Updated', text: asset + ' category changed', timer: 1500, showConfirmButton: false});
    }).catch(function(e) { _swal.fire({icon:'error', title:'Error', text: e.message}); });
}

// Rename
function openRenameModal(name) {
    document.getElementById('rename-old-name').value = name;
    document.getElementById('rename-new-name').value = name;
    document.getElementById('rename-modal').classList.add('open');
    document.getElementById('rename-new-name').focus();
    document.getElementById('rename-new-name').select();
}
function closeRenameModal() { document.getElementById('rename-modal').classList.remove('open'); }
function submitRename() {
    var oldName = document.getElementById('rename-old-name').value;
    var newName = document.getElementById('rename-new-name').value.trim();
    if (!newName || newName === oldName) { closeRenameModal(); return; }
    fetch('/api/rename-asset', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({old_name: oldName, new_name: newName})
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.ok) { closeRenameModal(); location.reload(); }
        else _swal.fire({icon:'error', title:'Error', text: d.error});
    }).catch(function(e) { _swal.fire({icon:'error', title:'Error', text: e.message}); });
}

// Delete
function deleteAsset(name) {
    _swal.fire({
        title: 'Delete "' + name + '"?',
        text: 'This will permanently remove the asset and its CSV file.',
        icon: 'warning', showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/delete-asset', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({asset: name})
        }).then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok) location.reload();
            else _swal.fire({icon:'error', title:'Error', text: d.error});
        }).catch(function(e) { _swal.fire({icon:'error', title:'Error', text: e.message}); });
    });
}
</script>
</body>
</html>
"""


# --- API Routes ---

@app.route('/s/<code>')
def short_link(code):
    """Public short link redirect."""
    bt_entry = db.get_backtest_by_short_code(code)
    if not bt_entry:
        abort(404)
    return redirect('/backtester?' + bt_entry['query_string'], code=302)


@app.route('/api/save', methods=['POST'])
def api_save():
    """Save a backtest privately."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    if not data:
        abort(400)
    result = db.save_backtest(
        user_id=user_id, email=email,
        params=data.get('params', '{}'),
        query_string=data.get('query_string', ''),
        cached_html=data.get('cached_html', ''),
        visibility='private',
        thumbnail=data.get('thumbnail', '')
    )
    return jsonify(result)


@app.route('/api/publish', methods=['POST'])
def api_publish():
    """Publish a backtest to community."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    if not data:
        abort(400)
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    display_name = data.get('display_name', '').strip()
    if not title:
        return jsonify({'error': 'Title is required'}), 400
    if not description:
        return jsonify({'error': 'Description is required'}), 400
    if not display_name:
        return jsonify({'error': 'Display name is required'}), 400
    # Save display name
    db.set_display_name(user_id, email, display_name)
    visibility = data.get('visibility', 'community')
    # Only admin can publish as featured
    if visibility == 'featured' and email != db.ADMIN_EMAIL:
        visibility = 'community'
    result = db.save_backtest(
        user_id=user_id, email=email,
        params=data.get('params', '{}'),
        query_string=data.get('query_string', ''),
        cached_html=data.get('cached_html', ''),
        visibility=visibility,
        title=title, description=description,
        thumbnail=data.get('thumbnail', '')
    )
    # Telegram notification for moderators
    _send_telegram_async(
        f'📊 <b>New backtest published</b>\n'
        f'By: {display_name}\n'
        f'Title: {title}\n'
        f'{SITE_URL}/backtest/{result.get("id", "")}'
    )
    return jsonify(result)


@app.route('/api/backtest/<bt_id>', methods=['PATCH'])
def api_update_backtest(bt_id):
    """Update title/description of a backtest."""
    user_id, email = _require_auth_api()
    data = request.get_json(force=True)
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    # Admin can edit any backtest
    if email == db.ADMIN_EMAIL:
        conn = db._get_conn()
        row = conn.execute("SELECT user_id FROM backtests WHERE id=?", (bt_id,)).fetchone()
        conn.close()
        if row:
            user_id = row['user_id']
    result = db.update_backtest(bt_id, user_id,
        title=data.get('title'),
        description=data.get('description'))
    if not result:
        return jsonify({'error': 'Not found or not authorized'}), 403
    return jsonify(result)


@app.route('/api/display-name', methods=['GET'])
def api_get_display_name():
    """Get current user's display name (or email prefix as fallback)."""
    if not _is_authenticated():
        return jsonify({'display_name': None, 'is_custom': False})
    user_id = session.get('user_id')
    email = session.get('email', '')
    name = db.get_display_name(user_id)
    if name:
        return jsonify({'display_name': name, 'is_custom': True})
    # Fallback: email prefix
    fallback = email.split('@')[0] if email else ''
    return jsonify({'display_name': fallback, 'is_custom': False})


@app.route('/api/display-name', methods=['POST'])
def api_set_display_name():
    """Set current user's display name."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    if not data or not data.get('display_name', '').strip():
        abort(400)
    db.set_display_name(user_id, email, data['display_name'].strip())
    return jsonify({'ok': True, 'display_name': data['display_name'].strip()})


@app.route('/api/backtest/<bt_id>', methods=['DELETE'])
def api_delete_backtest(bt_id):
    """Delete a backtest."""
    user_id, email = _require_auth_api()
    if email == db.ADMIN_EMAIL:
        db.delete_backtest_admin(bt_id)
    else:
        if not db.delete_backtest(bt_id, user_id):
            abort(403)
    return jsonify({'ok': True})


@app.route('/api/backtest/<bt_id>/feature', methods=['POST'])
def api_feature_backtest(bt_id):
    """Admin: promote to featured."""
    user_id, email = _require_auth_api()
    if email != db.ADMIN_EMAIL:
        abort(403)
    db.update_visibility(bt_id, 'featured')
    return jsonify({'ok': True})


@app.route('/api/backtest/<bt_id>/telegram', methods=['POST'])
def api_toggle_telegram(bt_id):
    """Admin: toggle telegram signal notifications for a backtest."""
    user_id, email = _require_auth_api()
    if email != db.ADMIN_EMAIL:
        abort(403)
    data = request.get_json(force=True)
    enabled = bool(data.get('enabled', False))
    template = data.get('template', '').strip() or None
    db.set_telegram_config(bt_id, enabled, template)
    return jsonify({'ok': True, 'telegram_enabled': enabled})


@app.route('/api/backtest/<bt_id>/email-alert', methods=['POST'])
def api_toggle_email_alert(bt_id):
    """Toggle email signal alert for the current user on a backtest."""
    user_id, email = _require_auth_api()
    data = request.get_json(force=True)
    enabled = bool(data.get('enabled', False))
    # Verify backtest exists and is accessible
    bt_entry = db.get_backtest(bt_id)
    if not bt_entry:
        abort(404)
    is_own = str(bt_entry['user_id']) == str(user_id)
    is_public = bt_entry['visibility'] in ('community', 'featured')
    if not is_own and not is_public:
        abort(403)
    if enabled:
        try:
            db.create_email_alert(user_id, bt_id)
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
        return jsonify({'ok': True, 'enabled': True})
    else:
        db.delete_email_alert(user_id, bt_id)
        return jsonify({'ok': True, 'enabled': False})


@app.route('/api/my-email-alerts')
def api_my_email_alerts():
    """List all active email alerts for the current user."""
    user_id, email = _require_auth_api()
    alerts = db.list_user_email_alerts(user_id)
    count = len(alerts)
    limit = db.MAX_EMAIL_ALERTS_PER_USER
    return jsonify({'ok': True, 'alerts': alerts, 'count': count, 'limit': limit})


@app.route('/unsubscribe/<token>')
def unsubscribe_email_alert(token):
    """One-click unsubscribe from an email alert (no login required)."""
    alert = db.get_email_alert_by_token(token)
    if not alert or not alert.get('is_active'):
        return render_template_string(UNSUBSCRIBE_HTML, success=False)
    db.deactivate_email_alert_by_token(token)
    return render_template_string(UNSUBSCRIBE_HTML, success=True,
        backtest_title=alert.get('backtest_title', 'this backtest'))


@app.route('/api/reorder-mixed', methods=['POST'])
def api_reorder_mixed():
    """Admin: reorder mixed backtests and collections."""
    user_id, email = _require_auth_api()
    if email != db.ADMIN_EMAIL:
        abort(403)
    data = request.get_json(force=True)
    ordered_items = data.get('ordered_items', [])
    db.reorder_mixed(ordered_items)
    return jsonify({'ok': True})


@app.route('/api/reorder-featured', methods=['POST'])
def api_reorder_featured():
    """Admin: reorder featured backtests."""
    user_id, email = _require_auth_api()
    if email != db.ADMIN_EMAIL:
        abort(403)
    data = request.get_json(force=True)
    ordered_ids = data.get('ordered_ids', [])
    db.reorder_backtests(ordered_ids)
    return jsonify({'ok': True})


@app.route('/api/reorder-collections', methods=['POST'])
def api_reorder_collections():
    """Admin: reorder collections."""
    user_id, email = _require_auth_api()
    if email != db.ADMIN_EMAIL:
        abort(403)
    data = request.get_json(force=True)
    ordered_ids = data.get('ordered_ids', [])
    db.reorder_collections(ordered_ids)
    return jsonify({'ok': True})


@app.route('/api/backtest/<bt_id>/like', methods=['POST'])
def api_like(bt_id):
    """Toggle like."""
    user_id, email = _require_auth_api()
    likes_count, liked = db.toggle_like(user_id, bt_id)
    return jsonify({'likes_count': likes_count, 'liked': liked})


@app.route('/api/backtest/<bt_id>/comment', methods=['POST'])
def api_comment(bt_id):
    """Add a comment. Sends email notifications if enabled."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    if not data or not data.get('body', '').strip():
        abort(400)
    comment = db.add_comment(bt_id, user_id, email, data['body'].strip(), data.get('parent_id'))
    # Send email notifications
    commenter_name = db.get_display_name(user_id) or email.split('@')[0]
    bt_entry = db.get_backtest(bt_id)
    bt_title = bt_entry['title'] or 'Untitled' if bt_entry else 'Untitled'
    comment_url = f"https://analytics.the-bitcoin-strategy.com/backtest/{bt_id}#comment-{comment['id']}"
    for target_uid, target_email, notif_type in comment.get('_email_targets', []):
        if notif_type == 'reply':
            subject = f'{commenter_name} replied to your comment'
            action = 'replied to your comment on'
        else:
            subject = f'{commenter_name} commented on your backtest'
            action = 'commented on your backtest'
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto">
            <h2 style="color:#333">{subject}</h2>
            <p><strong>{commenter_name}</strong> {action} <em>{bt_title}</em>:</p>
            <div style="background:#f5f5f5;padding:12px 16px;border-radius:8px;border-left:3px solid #6495ED;margin:16px 0;white-space:pre-wrap">{data['body'].strip()}</div>
            <a href="{comment_url}" style="display:inline-block;padding:10px 24px;background:#6495ED;color:#fff;text-decoration:none;border-radius:8px;font-weight:600">View Comment</a>
            <p style="color:#999;font-size:0.85em;margin-top:24px">You can manage email notifications in your <a href="https://analytics.the-bitcoin-strategy.com/account">account settings</a>.</p>
        </div>
        """
        _send_email_async(target_email, subject, html)
    # Telegram notification for moderators
    parent_label = ' (reply)' if data.get('parent_id') else ''
    _send_telegram_async(
        f'💬 <b>New comment{parent_label}</b>\n'
        f'By: {commenter_name}\n'
        f'On: {bt_title}\n'
        f'"{data["body"].strip()[:200]}"\n'
        f'{comment_url}'
    )
    # Remove internal field before returning
    comment.pop('_email_targets', None)
    return jsonify(comment)


@app.route('/api/comment/<comment_id>', methods=['DELETE'])
def api_delete_comment(comment_id):
    """Delete a comment."""
    user_id, email = _require_auth_api()
    if email == db.ADMIN_EMAIL:
        if not db.delete_comment_admin(comment_id):
            abort(404)
    else:
        if not db.delete_comment(comment_id, user_id):
            abort(403)
    return jsonify({'ok': True})


@app.route('/api/comment/<comment_id>', methods=['PUT'])
def api_edit_comment(comment_id):
    """Edit a comment."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    new_body = (data.get('body') or '').strip()
    if not new_body:
        return jsonify({'error': 'Comment body is required'}), 400
    if email == db.ADMIN_EMAIL:
        if not db.edit_comment_admin(comment_id, new_body):
            abort(404)
    else:
        if not db.edit_comment(comment_id, user_id, new_body):
            abort(403)
    return jsonify({'ok': True})


@app.route('/api/comment/<comment_id>/reaction', methods=['POST'])
def api_toggle_reaction(comment_id):
    """Toggle an emoji reaction on a comment."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    emoji = data.get('emoji', '') if data else ''
    if not emoji:
        abort(400)
    summary, reacted = db.toggle_reaction(comment_id, user_id, emoji)
    if summary is None:
        abort(400)
    return jsonify({'reactions': summary, 'reacted': reacted})


@app.route('/api/notifications')
def api_notifications():
    """Get all notifications for the current user (unread count in badge)."""
    if not _is_authenticated():
        return jsonify({'count': 0, 'notifications': []})
    user_id = session.get('user_id')
    count = db.get_unread_count(user_id)
    notifications = db.get_all_notifications(user_id)
    for n in notifications:
        name = db.get_display_name(n['actor_id'])
        n['actor_name'] = name or (n['actor_email'] or 'system').split('@')[0]
        n['time_ago'] = _time_ago(n['created_at'])
    return jsonify({'count': count, 'notifications': notifications})


@app.route('/api/notifications/read', methods=['POST'])
def api_notifications_read():
    """Mark all notifications as read for the current user."""
    user_id, email = _require_auth_api()
    db.mark_notifications_read(user_id)
    return jsonify({'ok': True})


@app.route('/api/avatar', methods=['POST'])
def api_upload_avatar():
    """Upload user avatar image."""
    user_id, email = _require_auth_api()
    if 'avatar' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['avatar']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in ('jpg', 'jpeg', 'png', 'webp'):
        return jsonify({'error': 'Invalid file type'}), 400
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 2 * 1024 * 1024:
        return jsonify({'error': 'File too large (2MB max)'}), 400
    filename = f'{user_id}.{ext}'
    avatars_dir = os.path.join(os.path.dirname(__file__), 'static', 'avatars')
    os.makedirs(avatars_dir, exist_ok=True)
    old = db.get_user_avatar(user_id)
    if old and old != filename:
        old_path = os.path.join(avatars_dir, old)
        if os.path.exists(old_path):
            os.remove(old_path)
    f.save(os.path.join(avatars_dir, filename))
    db.set_user_avatar(user_id, filename)
    return jsonify({'ok': True, 'avatar': filename})


@app.route('/api/avatar', methods=['DELETE'])
def api_delete_avatar():
    """Remove user avatar."""
    user_id, email = _require_auth_api()
    old = db.get_user_avatar(user_id)
    if old:
        path = os.path.join(os.path.dirname(__file__), 'static', 'avatars', old)
        if os.path.exists(path):
            os.remove(path)
    db.remove_user_avatar(user_id)
    return jsonify({'ok': True})


@app.route('/api/notification-pref', methods=['POST'])
def api_notification_pref():
    """Update notification preferences."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    nc = 1 if data.get('notify_comments', True) else 0
    nr = 1 if data.get('notify_replies', True) else 0
    db.set_notification_prefs(user_id, nc, nr)
    return jsonify({'ok': True})


@app.route('/api/feedback', methods=['POST'])
def api_feedback():
    """Submit feedback — sends email to admin."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    body = (data or {}).get('body', '').strip()
    if not body:
        abort(400)
    dn = db.get_display_name(user_id) or email.split('@')[0]
    html = f"""
    <h2>New Feedback from {dn}</h2>
    <p><strong>User:</strong> {dn} ({email})</p>
    <p><strong>Message:</strong></p>
    <p style="white-space:pre-wrap;background:#f5f5f5;padding:12px;border-radius:8px">{body}</p>
    """
    _send_email_async(ADMIN_FEEDBACK_EMAIL, f'Feedback from {dn}', html)
    return jsonify({'ok': True})


@app.route('/logout')
def logout():
    """Clear session and redirect to Laravel logout."""
    session.clear()
    return redirect('https://the-bitcoin-strategy.com/logout')


@app.route('/api/upload-asset', methods=['POST'])
def api_upload_asset():
    """Admin-only: upload a new asset CSV file."""
    if not _is_admin():
        abort(403)

    if 'file' not in request.files:
        return jsonify(error='No file provided'), 400

    file = request.files['file']
    asset_name = request.form.get('asset_name', '').strip()
    asset_type = request.form.get('asset_type', 'crypto')
    asset_ticker = request.form.get('ticker', '').strip().upper() or None

    if not asset_name:
        return jsonify(error='Asset name is required'), 400
    if asset_name in ASSETS:
        return jsonify(error=f'Asset "{asset_name}" already exists'), 400

    # Save CSV to temp file for validation
    import tempfile
    import shutil
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.csv')
    tmp_path = tmp.name
    tmp.close()
    try:
        file.save(tmp_path)
        df = bt.load_data(tmp_path)
        if df.empty or 'close' not in df.columns:
            os.unlink(tmp_path)
            return jsonify(error='CSV must have "time" and "close" columns'), 400
    except Exception as e:
        os.unlink(tmp_path)
        return jsonify(error=f'Failed to parse CSV: {str(e)}'), 400

    # Persist price data
    if _USE_PRICE_DB:
        asset_id = price_db.get_or_create_asset(
            asset_name, category=asset_type, source='csv', source_id=None,
            ticker=asset_ticker)
        price_db.upsert_prices(asset_id, df)
        os.unlink(tmp_path)
    else:
        csv_path = os.path.join(DATA_DIR, f"{asset_name}.csv")
        shutil.move(tmp_path, csv_path)

    # Update in-memory state
    ASSETS[asset_name] = df
    ASSET_STARTS[asset_name] = str(df.index[0].date())

    # Add to category set
    category_map = {
        'crypto_agg': _CRYPTO_AGG_ASSETS,
        'stock': _STOCK_ASSETS,
        'index': _INDEX_ASSETS,
        'metal': _METAL_ASSETS,
        'commodity': _COMMODITY_ASSETS,
    }
    cat_set = category_map.get(asset_type)
    if cat_set is not None:
        cat_set.add(asset_name)

    _save_categories_file()

    # Try to download logo
    logo_file = _download_logo(asset_name, asset_type)
    if logo_file:
        ASSET_LOGOS[asset_name] = logo_file
        _save_logos_file()

    if asset_ticker:
        ASSET_TICKERS[asset_name] = asset_ticker

    _rebuild_asset_lists()
    _touch_asset_signal()

    return jsonify(ok=True, asset=asset_name, logo=ASSET_LOGOS.get(asset_name, ''))


@app.route('/api/resolve-ticker', methods=['POST'])
def api_resolve_ticker():
    """Resolve a ticker symbol to a full asset name."""
    if not _is_admin():
        abort(403)
    data = request.get_json()
    ticker = data.get('ticker', '').strip()
    category = data.get('category', 'crypto')
    if not ticker:
        return jsonify(error='No ticker provided'), 400

    import urllib.request
    import urllib.parse

    name = None
    try:
        if category in ('crypto', 'crypto_agg'):
            # CoinGecko search API
            search_url = f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(ticker)}"
            headers = {'User-Agent': 'BacktestingEngine/1.0'}
            if COINGECKO_API_KEY:
                headers['x-cg-demo-api-key'] = COINGECKO_API_KEY
            req = urllib.request.Request(search_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                coins = json.loads(resp.read()).get('coins', [])
                # Match by symbol (case-insensitive)
                for coin in coins:
                    if coin.get('symbol', '').upper() == ticker.upper():
                        name = coin.get('name')
                        break
                # Fallback: first result
                if not name and coins:
                    name = coins[0].get('name')
        else:
            # yfinance for stocks/indices/metals/commodities
            import yfinance as yf
            t = yf.Ticker(ticker.upper())
            info = t.info
            name = info.get('longName') or info.get('shortName')
    except Exception:
        pass

    if name:
        return jsonify(ok=True, name=name)
    else:
        return jsonify(ok=False, name=ticker.upper())


MODE_SVGS = {
    'backtest': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 20 10 12 16 16 24 6"/><line x1="4" y1="24" x2="24" y2="24" opacity="0.4"/><circle cx="24" cy="6" r="2" fill="currentColor" stroke="none"/></svg>',
    'sweep': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="14" r="8" opacity="0.4"/><line x1="18" y1="20" x2="24" y2="26"/><path d="M9 14h6M12 11v6"/></svg>',
    'heatmap': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.6"/><rect x="11" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.3"/><rect x="19" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.15"/><rect x="3" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.3"/><rect x="11" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.8"/><rect x="19" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.4"/><rect x="3" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.15"/><rect x="11" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.4"/><rect x="19" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.6"/></svg>',
    'sweep-lev': '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    'regression': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="20" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="10" cy="16" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="16" cy="12" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="22" cy="8" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><line x1="4" y1="23" x2="25" y2="5" opacity="0.6"/></svg>',
    'dca': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4v20" opacity="0.4"/><path d="M8 10l6-6 6 6"/><circle cx="7" cy="18" r="2" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="14" cy="18" r="2" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="21" cy="18" r="2" fill="currentColor" stroke="none" opacity="0.5"/></svg>',
}

MODE_LABELS = {
    'backtest': 'Backtest',
    'sweep': 'Sweep',
    'heatmap': 'Heatmap',
    'sweep-lev': 'Leverage',
    'regression': 'Regression',
    'dca': 'DCA',
}


def _enrich_collection_cards(collections):
    """Add display-ready fields to collection dicts."""
    user_ids = {c.get('user_id') for c in collections if c.get('user_id')}
    profiles = db.get_user_profiles(user_ids)
    for coll in collections:
        uid = coll.get('user_id', '')
        profile = profiles.get(uid, {})
        coll['_display_name'] = profile.get('display_name') or coll.get('user_email', '').split('@')[0]
        coll['_avatar'] = profile.get('avatar')
        coll['_avatar_color'] = _avatar_color(uid)
        coll['_initial'] = _user_initial(coll['_display_name'], coll.get('user_email', ''))
        coll['_first_thumbnail'] = db.get_collection_first_thumbnail(coll['id'])
        coll['_thumbnails'] = db.get_collection_thumbnails(coll['id'], limit=4)
        coll['_assets'] = [(a, ASSET_LOGOS.get(a, '')) for a in db.get_collection_assets(coll['id'])]
        coll['_primary_asset'] = db.get_collection_primary_asset(coll['id'])
    return collections


def _enrich_backtest_cards(backtests):
    """Parse params JSON and add display-ready fields to each backtest dict."""
    # Batch-resolve display names and avatars
    user_ids = {bt.get('user_id') for bt in backtests if bt.get('user_id')}
    profiles = db.get_user_profiles(user_ids)
    for bt in backtests:
        uid = bt.get('user_id', '')
        profile = profiles.get(uid, {})
        bt['_display_name'] = profile.get('display_name') or bt.get('user_email', '').split('@')[0]
        bt['_avatar'] = profile.get('avatar')
        bt['_avatar_color'] = _avatar_color(uid)
        bt['_initial'] = _user_initial(bt['_display_name'], bt.get('user_email', ''))
        try:
            p = json.loads(bt.get('params', '{}'))
        except (json.JSONDecodeError, TypeError):
            p = {}
        # Asset
        asset = p.get('asset', '')
        bt['_asset'] = asset
        bt['_asset_display'] = asset.capitalize() if asset else ''
        bt['_asset_logo'] = ASSET_LOGOS.get(asset, '')
        # Mode
        mode = p.get('mode', 'backtest')
        bt['_mode'] = mode
        bt['_mode_label'] = MODE_LABELS.get(mode, mode.capitalize())
        bt['_mode_svg'] = MODE_SVGS.get(mode, '')
        # Indicators
        ind1 = p.get('ind1_name', 'price')
        ind2 = p.get('ind2_name', 'sma')
        p1 = p.get('period1', '')
        p2 = p.get('period2', '')
        if ind1 == 'price':
            bt['_strategy'] = f"Price / {ind2.upper()}({p2})" if p2 else f"Price / {ind2.upper()}"
        else:
            ind1_str = f"{ind1.upper()}({p1})" if p1 else ind1.upper()
            ind2_str = f"{ind2.upper()}({p2})" if p2 else ind2.upper()
            bt['_strategy'] = f"{ind1_str} / {ind2_str}"
        # Leverage
        ll = p.get('long_leverage', '1')
        sl = p.get('short_leverage', '1')
        try:
            ll_f = float(ll)
            sl_f = float(sl)
        except (ValueError, TypeError):
            ll_f = 1.0
            sl_f = 1.0
        if ll_f != 1.0 or sl_f != 1.0:
            bt['_leverage'] = f"{ll_f:.2g}x / {sl_f:.2g}x"
        else:
            bt['_leverage'] = None
        # Start date
        bt['_start_date'] = p.get('start_date', '')
        # Exposure
        bt['_exposure'] = p.get('exposure', 'long-cash')
        # Extract key metrics from cached HTML
        html = bt.get('cached_html', '') or ''
        # Ann. Return (strategy vs buy & hold) — metrics-panel format
        m = re.search(r'Ann\. Return.*?m-val[^>]*>([\-\d.]+)%.*?m-val[^>]*>([\-\d.]+)%', html, re.DOTALL)
        bt['_apr'] = m.group(1) if m else None
        bt['_apr_bh'] = m.group(2) if m else None
        # Max Drawdown (strategy vs buy & hold) — metrics-panel format
        m = re.search(r'Max Drawdown.*?m-val[^>]*>([\-\d.]+)%.*?m-val[^>]*>([\-\d.]+)%', html, re.DOTALL)
        bt['_max_dd'] = m.group(1) if m else None
        bt['_max_dd_bh'] = m.group(2) if m else None
        # Fallback: leverage sweep results-table format (plain <td> tags)
        if not bt['_apr'] and 'results-table' in html:
            # Buy & Hold row: <td>Ann%</td><td>DD%</td>
            m_bh = re.search(r'Buy &amp; Hold.*?<td>([\-\d.]+)%</td>\s*<td>([\-\d.]+)%</td>', html, re.DOTALL)
            if m_bh:
                bt['_apr_bh'] = m_bh.group(1)
                bt['_max_dd_bh'] = m_bh.group(2)
            # Best combined row (class="best"): <td>Ann%</td><td>DD%</td>
            m_best = re.search(r'class="best".*?<td>([\-\d.]+)%</td>\s*<td>([\-\d.]+)%</td>', html, re.DOTALL)
            if m_best:
                bt['_apr'] = m_best.group(1)
                bt['_max_dd'] = m_best.group(2)
    return backtests


# --- Live Price API ---

@app.route('/api/price-now/<asset_name>')
def api_price_now(asset_name):
    """Return current price for an asset (cached 60s, quota-aware)."""
    if asset_name not in ASSETS:
        return jsonify(error="not_found"), 404
    if not _api_usage_ok():
        return jsonify(error="quota", price=None)

    now = time.time()
    cached = _price_now_cache.get(asset_name)
    if cached and (now - cached["ts"]) < _PRICE_CACHE_TTL:
        return jsonify(price=cached["price"], time=cached["time"])

    result = _fetch_live_price(asset_name)
    if result:
        _price_now_cache[asset_name] = {"price": result["price"], "time": result["time"], "ts": now}
        return jsonify(price=result["price"], time=result["time"])

    # Return stale cache if fresh fetch failed
    if cached:
        return jsonify(price=cached["price"], time=cached["time"])
    return jsonify(error="unavailable", price=None)


@app.route('/api/admin/api-usage')
@require_auth
def api_admin_usage():
    """Admin-only: return CoinGecko API usage stats."""
    if not _is_admin():
        abort(403)
    return jsonify(_api_usage_get())


# --- Admin ---

@app.route('/admin/assets')
@require_auth
def admin_assets():
    """Admin-only asset management page."""
    if not _is_admin():
        abort(403)
    # Build sets for category detection in template
    crypto_default = set(ASSET_NAMES) - _CRYPTO_AGG_ASSETS - _STOCK_ASSETS - _INDEX_ASSETS - _METAL_ASSETS - _COMMODITY_ASSETS
    return render_template_string(ADMIN_ASSETS_HTML,
        nav_active='admin-assets',
        asset_names=ASSET_NAMES, asset_logos=ASSET_LOGOS, asset_tickers=ASSET_TICKERS, asset_starts=ASSET_STARTS,
        crypto_names=crypto_default, crypto_agg_names=_CRYPTO_AGG_ASSETS,
        stock_names=_STOCK_ASSETS, index_names=_INDEX_ASSETS,
        metal_names=_METAL_ASSETS, commodity_names=_COMMODITY_ASSETS)


@app.route('/api/delete-asset', methods=['POST'])
def api_delete_asset():
    """Admin-only: delete an asset."""
    if not _is_admin():
        abort(403)
    data = request.get_json()
    name = data.get('asset', '').strip()
    if not name or name not in ASSETS:
        return jsonify(error='Asset not found'), 404
    if name == 'bitcoin':
        return jsonify(error='Cannot delete the default asset'), 400

    # Remove from storage
    if _USE_PRICE_DB:
        price_db.delete_asset(name)
    else:
        csv_path = os.path.join(DATA_DIR, f"{name}.csv")
        if os.path.exists(csv_path):
            os.unlink(csv_path)

    # Remove from in-memory state
    ASSETS.pop(name, None)
    ASSET_STARTS.pop(name, None)
    ASSET_LOGOS.pop(name, None)

    # Remove from all category sets
    for cat_set in [_CRYPTO_AGG_ASSETS, _STOCK_ASSETS, _INDEX_ASSETS, _METAL_ASSETS, _COMMODITY_ASSETS]:
        cat_set.discard(name)

    _save_categories_file()
    _save_logos_file()
    _rebuild_asset_lists()
    _touch_asset_signal()
    return jsonify(ok=True)


@app.route('/api/rename-asset', methods=['POST'])
def api_rename_asset():
    """Admin-only: rename an asset."""
    if not _is_admin():
        abort(403)
    data = request.get_json()
    old_name = data.get('old_name', '').strip()
    new_name = data.get('new_name', '').strip()
    if not old_name or not new_name:
        return jsonify(error='Both old and new name required'), 400
    if old_name not in ASSETS:
        return jsonify(error=f'Asset "{old_name}" not found'), 404
    if new_name in ASSETS:
        return jsonify(error=f'Asset "{new_name}" already exists'), 400

    # Rename in storage
    if _USE_PRICE_DB:
        price_db.rename_asset(old_name, new_name)
    else:
        import shutil
        old_path = os.path.join(DATA_DIR, f"{old_name}.csv")
        new_path = os.path.join(DATA_DIR, f"{new_name}.csv")
        if os.path.exists(old_path):
            shutil.move(old_path, new_path)

    # Propagate rename to all backtests referencing this asset
    bt_updated = db.rename_asset_in_backtests(old_name, new_name)

    # Update in-memory state
    ASSETS[new_name] = ASSETS.pop(old_name)
    ASSET_STARTS[new_name] = ASSET_STARTS.pop(old_name, '')
    if old_name in ASSET_LOGOS:
        ASSET_LOGOS[new_name] = ASSET_LOGOS.pop(old_name)
    if old_name in ASSET_TICKERS:
        ASSET_TICKERS[new_name] = ASSET_TICKERS.pop(old_name)
    if old_name in _ASSET_META:
        _ASSET_META[new_name] = _ASSET_META.pop(old_name)

    # Update category sets
    for cat_set in [_CRYPTO_AGG_ASSETS, _STOCK_ASSETS, _INDEX_ASSETS, _METAL_ASSETS, _COMMODITY_ASSETS]:
        if old_name in cat_set:
            cat_set.discard(old_name)
            cat_set.add(new_name)

    _save_categories_file()
    _save_logos_file()
    _rebuild_asset_lists()
    _touch_asset_signal()
    return jsonify(ok=True, backtests_updated=bt_updated)


@app.route('/api/change-asset-category', methods=['POST'])
def api_change_asset_category():
    """Admin-only: change an asset's category."""
    if not _is_admin():
        abort(403)
    data = request.get_json()
    name = data.get('asset', '').strip()
    category = data.get('category', '').strip()
    if not name or name not in ASSETS:
        return jsonify(error='Asset not found'), 404

    # Remove from all category sets
    for cat_set in [_CRYPTO_AGG_ASSETS, _STOCK_ASSETS, _INDEX_ASSETS, _METAL_ASSETS, _COMMODITY_ASSETS]:
        cat_set.discard(name)

    # Add to new category (if not plain crypto)
    category_map = {
        'crypto_agg': _CRYPTO_AGG_ASSETS,
        'stock': _STOCK_ASSETS,
        'index': _INDEX_ASSETS,
        'metal': _METAL_ASSETS,
        'commodity': _COMMODITY_ASSETS,
    }
    cat_set = category_map.get(category)
    if cat_set is not None:
        cat_set.add(name)

    _save_categories_file()
    _rebuild_asset_lists()
    _touch_asset_signal()
    return jsonify(ok=True)


@app.route('/api/update-ticker', methods=['POST'])
def api_update_ticker():
    """Admin-only: update an asset's ticker symbol."""
    if not _is_admin():
        abort(403)
    data = request.get_json()
    name = data.get('asset', '').strip()
    ticker = data.get('ticker', '').strip().upper()
    if not name or name not in ASSETS:
        return jsonify(error='Asset not found'), 404
    if _USE_PRICE_DB:
        conn = price_db._get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE assets SET ticker = %s WHERE name = %s", (ticker or None, name))
        conn.commit()
        conn.close()
    if ticker:
        ASSET_TICKERS[name] = ticker
    elif name in ASSET_TICKERS:
        del ASSET_TICKERS[name]
    return jsonify(ok=True)


# --- Page Routes ---

@app.route('/community')
def community():
    """Community backtests page."""
    _try_token_auth()
    sort = request.args.get('sort', 'newest')
    page = int(request.args.get('page', 1))
    backtests, total = db.list_backtests(visibility='community', sort=sort, page=page, per_page=20)
    # Filter out backtests that belong to a published collection
    in_coll = db.get_backtests_in_published_collections('community')
    backtests = [bt for bt in backtests if bt['id'] not in in_coll]
    _enrich_backtest_cards(backtests)
    total_pages = max(1, (total + 19) // 20)
    # Recent comments
    recent_comments = db.get_recent_comments(limit=8)
    rc_user_ids = {c['user_id'] for c in recent_comments}
    rc_profiles = db.get_user_profiles(rc_user_ids)
    for c in recent_comments:
        p = rc_profiles.get(c['user_id'], {})
        c['_display_name'] = p.get('display_name') or c['user_email'].split('@')[0]
        c['_avatar'] = p.get('avatar')
        c['_avatar_color'] = _avatar_color(c['user_id'])
        c['_initial'] = _user_initial(c['_display_name'], c['user_email'])
        c['_time_ago'] = _time_ago(c['created_at'])
    # Collections — merge into backtests list
    community_collections, _ = db.list_collections(visibility='community', sort='newest')
    _enrich_collection_cards(community_collections)
    # Mark collections so template can distinguish them
    for coll in community_collections:
        coll['_is_collection'] = True
    # Prepend collections to backtests list
    mixed_items = community_collections + backtests
    return render_template_string(COMMUNITY_HTML,
        nav_active='community', page_title='Community Backtests',
        page_subtitle='Strategies shared by the community',
        backtests=mixed_items,
        sort=sort, page=page, total_pages=total_pages,
        recent_comments=recent_comments,
        is_authenticated=_is_authenticated(), time_ago=_time_ago)


@app.route('/')
@app.route('/featured')
def featured():
    """Featured backtests page."""
    _try_token_auth()
    sort = request.args.get('sort', 'manual')
    backtests, total = db.list_backtests(visibility='featured', sort=sort, page=1, per_page=200)
    # Filter out backtests that belong to a featured collection
    in_coll = db.get_backtests_in_published_collections('featured')
    backtests = [bt for bt in backtests if bt['id'] not in in_coll]
    _enrich_backtest_cards(backtests)
    # Collections
    featured_collections, _ = db.list_collections(visibility='featured', sort='manual')
    _enrich_collection_cards(featured_collections)
    for coll in featured_collections:
        coll['_is_collection'] = True
        coll['_asset'] = coll.get('_primary_asset', 'other') or 'other'
    # Merge backtests + collections into one list sorted by sort_order
    all_items = backtests + featured_collections
    all_items.sort(key=lambda x: x.get('sort_order', 0) or 0)
    # Group by asset, preserving sort order
    from collections import OrderedDict
    grouped = OrderedDict()
    for item in all_items:
        asset = item.get('_asset', '') or 'other'
        if asset not in grouped:
            grouped[asset] = []
        grouped[asset].append(item)
    # Build sections with display info
    asset_sections = []
    for asset, items in grouped.items():
        asset_sections.append({
            'asset': asset,
            'display': asset.capitalize() if asset != 'other' else 'Other',
            'logo': ASSET_LOGOS.get(asset, ''),
            'backtests': items,
        })
    return render_template_string(COMMUNITY_HTML,
        nav_active='featured', page_title='Home',
        page_subtitle='Curated strategies hand-picked by our team',
        backtests=backtests, asset_sections=asset_sections,
        sort=sort, page=1, total_pages=1,
        is_authenticated=_is_authenticated(), is_admin=_is_admin(), time_ago=_time_ago)


@app.route('/my-backtests')
@require_auth
def my_backtests():
    """User's personal backtest dashboard."""
    user_id = session.get('user_id')
    all_bt = db.list_user_backtests(user_id)
    published = [b for b in all_bt if b['visibility'] in ('community', 'featured')]
    saved = [b for b in all_bt if b['visibility'] == 'private']
    _enrich_backtest_cards(published)
    _enrich_backtest_cards(saved)
    # Email alert indicators
    all_bt_ids = [b['id'] for b in all_bt]
    alerted_ids = db.get_user_alerted_backtest_ids(user_id, all_bt_ids)
    # Collections
    user_collections = db.list_user_collections(user_id)
    _enrich_collection_cards(user_collections)
    display_name = db.get_display_name(user_id)
    email = session.get('email', '')
    email_prefix = email.split('@')[0] if email else ''
    return render_template_string(MY_BACKTESTS_HTML,
        nav_active='my-backtests',
        published=published, saved=saved, collections=user_collections,
        time_ago=_time_ago, alerted_ids=alerted_ids,
        display_name=display_name, email_prefix=email_prefix)


@app.route('/backtest/<bt_id>')
def backtest_detail(bt_id):
    """Single backtest detail page with comments."""
    _try_token_auth()
    bt_entry = db.get_backtest(bt_id)
    if not bt_entry:
        abort(404)
    # Private backtests only visible to owner
    if bt_entry['visibility'] == 'private':
        if not _is_authenticated() or str(session.get('user_id', '')) != str(bt_entry['user_id']):
            abort(404)
    # Increment view count
    db.increment_views(bt_id)
    bt_entry['views_count'] = (bt_entry.get('views_count') or 0) + 1
    comments = db.get_comments(bt_id)
    is_auth = _is_authenticated()
    liked = db.has_liked(session.get('user_id', ''), bt_id) if is_auth else False
    author_display_name = db.get_display_name(bt_entry['user_id'])
    author_avatar = db.get_user_avatar(bt_entry['user_id'])
    author_uid = bt_entry['user_id']
    author_initial = _user_initial(author_display_name, bt_entry.get('user_email', ''))
    author_avatar_color = _avatar_color(author_uid)
    # Resolve display names for comment authors
    commenter_ids = set()
    for c in comments:
        commenter_ids.add(c['user_id'])
        for r in c.get('replies', []):
            commenter_ids.add(r['user_id'])
    commenter_names = {}
    for uid in commenter_ids:
        name = db.get_display_name(uid)
        if name:
            commenter_names[uid] = name
    # Collect all comment IDs for reactions
    all_comment_ids = []
    for c in comments:
        all_comment_ids.append(c['id'])
        for r in c.get('replies', []):
            all_comment_ids.append(r['id'])
    reactions_map = db.get_reactions_for_comments(all_comment_ids, session.get('user_id') if is_auth else None)
    for c in comments:
        c['_display_name'] = commenter_names.get(c['user_id'], c['user_email'].split('@')[0])
        c['_reactions'] = reactions_map.get(c['id'], {})
        for r in c.get('replies', []):
            r['_display_name'] = commenter_names.get(r['user_id'], r['user_email'].split('@')[0])
            r['_reactions'] = reactions_map.get(r['id'], {})
    # Strip save/publish action buttons from cached HTML
    cached = bt_entry.get('cached_html', '') or ''
    cached = re.sub(r'<div class="action-buttons"[^>]*id="backtest-actions"[^>]*>.*?</div>', '', cached, flags=re.DOTALL)
    # Convert raw "XXXd" drawdown durations to human-readable "Xy Xm Xd"
    cached = re.sub(r'(Drawdown Duration.*?m-val[^>]*>)(\d+)d(.*?m-val[^>]*>)(\d+)d',
                    lambda m: m.group(1) + duration_filter(int(m.group(2))) + m.group(3) + duration_filter(int(m.group(4))),
                    cached, flags=re.DOTALL)

    # Inject fresh live chart data (price + indicators extended to newest date)
    import json as json_mod
    bt_params = json_mod.loads(bt_entry.get('params', '{}') or '{}')
    # Backfill missing period2 from cached HTML (older saves from sweep mode)
    if not bt_params.get('period2') and cached:
        _m = re.search(r'data-ind2-period="(\d+)"', cached)
        if not _m:
            _ind2_upper = bt_params.get('ind2_name', '').upper()
            if _ind2_upper:
                _m = re.search(r'ind2Label:\s*["\']' + re.escape(_ind2_upper) + r'\((\d+)\)', cached)
        if _m:
            bt_params['period2'] = _m.group(1)
    try:
        import pandas as pd_mod
        _asset = _resolve_asset(bt_params.get('asset', ''))
        _vs = _resolve_asset(bt_params.get('vs_asset', ''))
        if not _asset or _asset not in ASSETS:
            raise KeyError(f'Asset "{bt_params.get("asset")}" not found')
        _df_all = ASSETS[_asset].copy()
        if _vs and _vs in ASSETS:
            _df_vs = ASSETS[_vs].copy()
            try:
                _df_all = compute_ratio_prices(_df_all, _df_vs)
            except ValueError:
                pass  # No overlapping dates — show raw prices
        _sd = bt_params.get('start_date', '')
        if _sd:
            _df_all = _df_all[_df_all.index >= pd_mod.Timestamp(_sd, tz="UTC")]
        if not _df_all.empty:
            _fresh_price = _series_to_lw_json(_df_all["close"])
            _ind1_name = bt_params.get('ind1_name', 'price')
            _ind2_name = bt_params.get('ind2_name', 'sma')
            _p1 = int(bt_params.get('period1', 0) or 0) or None
            _p2 = int(bt_params.get('period2', 0) or 0) or None
            _ind2_s, _ = bt.compute_indicator_from_spec(_df_all, _ind2_name, _p2)
            _fresh_ind2 = _series_to_lw_json(_ind2_s)
            if _ind1_name != "price":
                _ind1_s, _ = bt.compute_indicator_from_spec(_df_all, _ind1_name, _p1)
                _fresh_ind1 = _series_to_lw_json(_ind1_s)
            else:
                _fresh_ind1 = "[]"
            # Build labels
            _ind1_lbl = f"{_ind1_name.upper()}({_p1})" if _ind1_name != "price" and _p1 else ("Price" if _ind1_name == "price" else _ind1_name.upper())
            _ind2_lbl = f"{_ind2_name.upper()}({_p2})" if _p2 else _ind2_name.upper()
            # Replace the __lwData block in cached HTML (includes __lwAsset for live polling)
            _new_lw = (f'<script>\nvar __lwAsset = {json_mod.dumps(_asset)};\n'
                       f'var __lwVsAsset = {json_mod.dumps(_vs or "")};\n'
                       f'var __lwData = {{\n'
                       f'    price: {_fresh_price},\n'
                       f'    ind1: {_fresh_ind1},\n'
                       f'    ind2: {_fresh_ind2},\n'
                       f'    ind1Label: {json_mod.dumps(_ind1_lbl)},\n'
                       f'    ind2Label: {json_mod.dumps(_ind2_lbl)}\n'
                       f'}};\n</script>')
            cached = re.sub(r'<script>\s*(?:var __lwAsset\s*=.*?)?(?:var __lwVsAsset\s*=.*?)?var __lwData\s*=\s*\{.*?\};\s*</script>', _new_lw, cached, flags=re.DOTALL)
    except Exception:
        pass  # If fresh data injection fails, keep original cached HTML

    bt_entry = dict(bt_entry)
    bt_entry['cached_html'] = cached
    # Check if current user has an email alert on this backtest
    has_email_alert = False
    if is_auth:
        has_email_alert = bool(db.get_email_alert(session.get('user_id', ''), bt_id))
    nav = bt_entry.get('visibility', '')
    if nav not in ('featured', 'community'):
        nav = ''
    return render_template_string(DETAIL_HTML,
        nav_active=nav,
        backtest=bt_entry, comments=comments, bt_params=bt_params,
        is_authenticated=is_auth, is_admin=_is_admin(),
        has_liked=liked, has_email_alert=has_email_alert, time_ago=_time_ago,
        display_name=author_display_name,
        author_avatar=author_avatar, author_initial=author_initial,
        author_avatar_color=author_avatar_color)


# --- Collection Routes ---

def _extract_video_embed_url(url):
    """Build embed URL from YouTube or Vimeo URL/ID."""
    if not url:
        return None
    url = url.strip()
    import re as re_mod
    # Vimeo: vimeo.com/123456 or player.vimeo.com/video/123456 or just numeric ID
    m = re_mod.search(r'vimeo\.com/(?:video/)?(\d+)', url)
    if m:
        return f'https://player.vimeo.com/video/{m.group(1)}'
    if re_mod.fullmatch(r'\d+', url):
        # Bare numeric ID — treat as Vimeo
        return f'https://player.vimeo.com/video/{url}'
    # YouTube: youtube.com/watch?v=..., youtu.be/..., embed/...
    m = re_mod.search(r'(?:v=|youtu\.be/|embed/)([\w-]+)', url)
    if m:
        return f'https://www.youtube.com/embed/{m.group(1)}'
    return None


@app.route('/collection/<collection_id>')
def collection_detail(collection_id):
    """Collection detail page."""
    _try_token_auth()
    coll = db.get_collection(collection_id)
    if not coll:
        abort(404)
    if coll['visibility'] == 'private':
        if not _is_authenticated() or str(session.get('user_id', '')) != str(coll['user_id']):
            abort(404)
    db.increment_collection_views(collection_id)
    coll['views_count'] = (coll.get('views_count') or 0) + 1
    backtests = db.get_collection_backtests(collection_id)
    _enrich_backtest_cards(backtests)
    # Author info
    author_display_name = db.get_display_name(coll['user_id'])
    author_avatar = db.get_user_avatar(coll['user_id'])
    author_uid = coll['user_id']
    author_initial = _user_initial(author_display_name, coll.get('user_email', ''))
    author_avatar_color = _avatar_color(author_uid)
    is_auth = _is_authenticated()
    is_owner = is_auth and str(session.get('user_id', '')) == str(coll['user_id'])
    # Video embed (YouTube or Vimeo)
    youtube_embed_url = _extract_video_embed_url(coll.get('youtube_url'))
    # User's backtests for "Add Backtest" dropdown
    user_backtests = []
    collection_bt_ids = set()
    if is_owner:
        uid = session.get('user_id')
        user_backtests = db.list_user_backtests(uid)
        # Filter out backtests already in any collection
        in_any_coll = db.get_backtests_in_any_collection(uid)
        collection_bt_ids = {bt['id'] for bt in backtests}
        user_backtests = [ubt for ubt in user_backtests if ubt['id'] not in in_any_coll or ubt['id'] in collection_bt_ids]
        # Parse asset from params for dropdown display
        for ubt in user_backtests:
            try:
                p = json.loads(ubt.get('params', '{}'))
            except (json.JSONDecodeError, TypeError):
                p = {}
            ubt['_asset'] = (p.get('asset', '') or '').capitalize()
    return render_template_string(COLLECTION_DETAIL_HTML,
        collection=coll, backtests=backtests,
        youtube_embed_url=youtube_embed_url,
        is_authenticated=is_auth, is_admin=_is_admin(), is_owner=is_owner,
        display_name=author_display_name or coll.get('user_email', '').split('@')[0],
        author_avatar=author_avatar, author_initial=author_initial,
        author_avatar_color=author_avatar_color,
        user_backtests=user_backtests, collection_bt_ids=collection_bt_ids,
        time_ago=_time_ago)


@app.route('/cs/<code>')
def collection_short_link(code):
    """Short link redirect for collections."""
    coll = db.get_collection_by_short_code(code)
    if not coll:
        abort(404)
    return redirect(f'/collection/{coll["id"]}')


@app.route('/api/collection/create', methods=['POST'])
@require_auth
def api_create_collection():
    """Create a new collection."""
    data = request.get_json()
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify(error='Title is required'), 400
    description = (data.get('description') or '').strip() or None
    youtube_url = (data.get('youtube_url') or '').strip() or None
    copy_trading_url = (data.get('copy_trading_url') or '').strip() or None
    user_id = session.get('user_id')
    email = session.get('email', '')
    coll = db.save_collection(user_id, email, title, description, youtube_url, copy_trading_url)
    return jsonify(ok=True, id=coll['id'], short_code=coll['short_code'])


@app.route('/api/collection/<collection_id>/update', methods=['POST'])
@require_auth
def api_update_collection(collection_id):
    """Update collection metadata."""
    data = request.get_json()
    user_id = session.get('user_id')
    title = data.get('title')
    description = data.get('description')
    youtube_url = data.get('youtube_url')
    copy_trading_url = data.get('copy_trading_url')
    updated = db.update_collection(collection_id, user_id, title=title, description=description, youtube_url=youtube_url, copy_trading_url=copy_trading_url)
    if not updated:
        return jsonify(error='Not found or not authorized'), 404
    return jsonify(ok=True)


@app.route('/api/collection/<collection_id>/delete', methods=['POST'])
@require_auth
def api_delete_collection(collection_id):
    """Delete a collection."""
    user_id = session.get('user_id')
    if _is_admin():
        db.delete_collection_admin(collection_id)
    else:
        if not db.delete_collection(collection_id, user_id):
            return jsonify(error='Not found or not authorized'), 404
    return jsonify(ok=True)


@app.route('/api/collection/<collection_id>/add-backtest', methods=['POST'])
@require_auth
def api_add_backtest_to_collection(collection_id):
    """Add a backtest to a collection."""
    data = request.get_json()
    backtest_id = data.get('backtest_id')
    if not backtest_id:
        return jsonify(error='backtest_id required'), 400
    # Verify ownership of collection
    coll = db.get_collection(collection_id)
    if not coll or str(coll['user_id']) != str(session.get('user_id')):
        return jsonify(error='Not authorized'), 403
    added = db.add_backtest_to_collection(collection_id, backtest_id)
    if not added:
        return jsonify(error='Already in collection'), 409
    return jsonify(ok=True)


@app.route('/api/collection/<collection_id>/remove-backtest', methods=['POST'])
@require_auth
def api_remove_backtest_from_collection(collection_id):
    """Remove a backtest from a collection."""
    data = request.get_json()
    backtest_id = data.get('backtest_id')
    if not backtest_id:
        return jsonify(error='backtest_id required'), 400
    coll = db.get_collection(collection_id)
    if not coll or str(coll['user_id']) != str(session.get('user_id')):
        return jsonify(error='Not authorized'), 403
    db.remove_backtest_from_collection(collection_id, backtest_id)
    return jsonify(ok=True)


@app.route('/api/collection/<collection_id>/reorder', methods=['POST'])
@require_auth
def api_reorder_collection(collection_id):
    """Reorder backtests within a collection."""
    data = request.get_json()
    ordered_ids = data.get('ordered_ids', [])
    coll = db.get_collection(collection_id)
    if not coll or str(coll['user_id']) != str(session.get('user_id')):
        return jsonify(error='Not authorized'), 403
    db.reorder_collection_backtests(collection_id, ordered_ids)
    return jsonify(ok=True)


@app.route('/api/collection/<collection_id>/visibility', methods=['POST'])
def api_collection_visibility(collection_id):
    """Admin: change collection visibility."""
    if not _is_admin():
        abort(403)
    data = request.get_json()
    new_vis = data.get('visibility')
    if new_vis not in ('private', 'community', 'featured'):
        return jsonify(error='Invalid visibility'), 400
    db.update_collection_visibility(collection_id, new_vis)
    return jsonify(ok=True)


COLLECTION_DETAIL_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <title>{{ collection.title }} — Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --bg-deep: #080a10; --bg-base: #0f1117; --bg-surface: #161922; --bg-elevated: #1c2030;
            --border: #252a3a; --border-hover: #3a4060; --text: #e8eaf0; --text-muted: #8890a4; --text-dim: #555d74;
            --accent: #f7931a; --accent-hover: #ffa940; --accent-glow: rgba(247, 147, 26, 0.15);
            --green: #34d399; --blue: #6495ED;
        }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(139, 92, 246, 0.06), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none; z-index: 0;
        }
        [data-theme="light"] body::before {
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(217, 119, 6, 0.04), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(58, 111, 216, 0.03), transparent);
        }
        .container { max-width: 960px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }
        .back-link { display: inline-flex; align-items: center; gap: 6px; color: var(--text-muted); text-decoration: none; font-size: 0.85em; margin-bottom: 20px; transition: color 0.2s; }
        .back-link:hover { color: var(--text); }
        .coll-header { margin-bottom: 24px; }
        .coll-badge { display: inline-block; padding: 3px 10px; border-radius: 5px; font-size: 0.72em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; background: rgba(139,92,246,0.15); color: #8b5cf6; }
        .coll-title { font-size: 1.6em; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.02em; }
        .coll-meta { display: flex; align-items: center; gap: 8px; font-size: 0.85em; color: var(--text-muted); margin-bottom: 16px; flex-wrap: wrap; }
        .coll-meta-avatar { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; }
        .coll-meta-initials { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 0.7em; font-weight: 700; }
        .coll-actions { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; align-items: center; }
        .vis-select { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-muted); cursor: pointer; font-size: 0.8em; font-weight: 500; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; appearance: none; -webkit-appearance: none; padding-right: 28px; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238890a4' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 8px center; }
        .vis-select:hover { border-color: var(--border-hover); color: var(--text); }
        .vis-select:focus { outline: none; border-color: #8b5cf6; box-shadow: 0 0 0 3px rgba(139,92,246,0.15); }
        .action-btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-muted); cursor: pointer; font-size: 0.8em; font-weight: 500; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; text-decoration: none; }
        .action-btn:hover { border-color: var(--border-hover); color: var(--text); }
        .action-btn.danger { border-color: #ef4444; color: #ef4444; }
        .action-btn.danger:hover { background: rgba(239,68,68,0.1); }
        .yt-embed { position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden; border-radius: 12px; margin-bottom: 24px; border: 1px solid var(--border); }
        .yt-embed iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none; }
        .copy-trading-link { display: flex; align-items: center; gap: 10px; padding: 14px 20px; background: linear-gradient(135deg, rgba(16,185,129,0.1), rgba(16,185,129,0.05)); border: 1px solid rgba(16,185,129,0.3); border-radius: 12px; margin-bottom: 24px; color: #10b981; font-weight: 600; font-size: 0.95em; text-decoration: none; transition: all 0.2s ease; }
        .copy-trading-link:hover { background: linear-gradient(135deg, rgba(16,185,129,0.15), rgba(16,185,129,0.08)); border-color: rgba(16,185,129,0.5); transform: translateY(-1px); }
        .copy-trading-link svg:last-child { margin-left: auto; opacity: 0.6; }
        .coll-desc { font-size: 0.95em; color: var(--text-muted); line-height: 1.6; margin-bottom: 24px; white-space: pre-wrap; }
        .section-divider { display: flex; align-items: center; gap: 12px; margin: 24px 0 16px; }
        .section-divider span { font-size: 0.85em; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; white-space: nowrap; }
        .section-divider::after { content: ''; flex: 1; height: 1px; background: var(--border); }
        .backtest-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
        .backtest-card { display: block; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px; transition: all 0.2s ease; cursor: pointer; text-decoration: none; color: inherit; }
        .backtest-card:hover { border-color: var(--border-hover); transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
        .backtest-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
        .backtest-card-asset-logo { width: 22px; height: 22px; object-fit: contain; border-radius: 50%; background: var(--bg-deep); flex-shrink: 0; }
        .backtest-card-asset-fallback { width: 22px; height: 22px; border-radius: 50%; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.65em; color: var(--text-muted); flex-shrink: 0; }
        .backtest-card-head-text { flex: 1; min-width: 0; }
        .backtest-card-title { font-size: 1em; font-weight: 600; color: var(--text); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.3; }
        .backtest-card-asset-name { font-size: 0.75em; color: var(--text-muted); }
        .backtest-card-mode-icon { color: var(--text-dim); flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 8px; background: var(--bg-deep); }
        .backtest-card-thumb { width: 100%; height: 140px; object-fit: cover; border-radius: 8px; margin-bottom: 10px; border: 1px solid var(--border); }
        .backtest-card-desc { font-size: 0.8em; color: var(--text-muted); margin-bottom: 10px; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
        .backtest-card-params { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
        .backtest-card-tag { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 6px; background: var(--bg-deep); border: 1px solid var(--border); font-size: 0.7em; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; white-space: nowrap; }
        .backtest-card-metrics { display: flex; gap: 12px; margin-bottom: 10px; }
        .card-metric { flex: 1; padding: 8px 10px; background: var(--bg-deep); border: 1px solid var(--border); border-radius: 8px; text-align: center; }
        .card-metric-label { display: block; font-size: 0.65em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); margin-bottom: 2px; }
        .card-metric-val { display: block; font-size: 0.95em; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .card-metric-val.positive { color: var(--green); }
        .card-metric-val.negative { color: #ef4444; }
        .card-metric-vs { display: block; font-size: 0.6em; color: #ffffff; font-family: 'JetBrains Mono', monospace; margin-top: 1px; }
        .backtest-card-footer { display: flex; align-items: center; justify-content: space-between; font-size: 0.75em; color: var(--text-dim); }
        .backtest-card-footer .engagement { display: flex; gap: 12px; }
        .backtest-card-footer .engagement span { display: flex; align-items: center; gap: 3px; }
        .card-author { display: flex; align-items: center; gap: 6px; }
        .card-author-avatar { width: 22px; height: 22px; border-radius: 50%; object-fit: cover; }
        .card-author-initials { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 0.7em; font-weight: 700; flex-shrink: 0; }
        .card-author-name { font-weight: 600; color: var(--text-muted); }
        .card-author-sep { color: var(--text-dim); }
        .card-author-time { color: var(--text-dim); }
        .backtest-card-wrapper { position: relative; }
        .remove-bt-btn { position: absolute; top: 8px; right: 8px; z-index: 10; width: 28px; height: 28px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg-surface); color: var(--text-dim); cursor: pointer; font-size: 0.9em; display: flex; align-items: center; justify-content: center; opacity: 0; transition: all 0.15s ease; }
        .backtest-card-wrapper:hover .remove-bt-btn { opacity: 1; }
        .remove-bt-btn:hover { background: rgba(239,68,68,0.15); color: #ef4444; border-color: #ef4444; }
        /* Drag-and-drop reorder */
        .drag-handle { position: absolute; top: 8px; left: 8px; z-index: 10; width: 28px; height: 28px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg-surface); color: var(--text-dim); cursor: grab; font-size: 0.8em; display: flex; align-items: center; justify-content: center; opacity: 0; transition: all 0.15s ease; }
        .backtest-card-wrapper:hover .drag-handle { opacity: 1; }
        .drag-handle:hover { background: var(--bg-elevated); color: var(--text); border-color: var(--border-hover); }
        .drag-handle:active { cursor: grabbing; }
        .backtest-card-wrapper.dragging { opacity: 0.4; }
        .backtest-card-wrapper.drag-over { border-top: 2px solid #8b5cf6; }
        .publish-modal-overlay { display: none; position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); align-items: center; justify-content: center; }
        .publish-modal-overlay.open { display: flex; }
        .publish-modal { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 16px; padding: 28px; width: 90%; max-width: 500px; position: relative; }
        .publish-modal h3 { font-size: 1.1em; font-weight: 600; margin-bottom: 16px; }
        .publish-modal label { display: block; font-size: 0.8em; color: var(--text-muted); margin-bottom: 6px; font-weight: 500; }
        .publish-modal input, .publish-modal textarea { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.9em; font-family: 'DM Sans', sans-serif; margin-bottom: 14px; }
        .publish-modal input:focus, .publish-modal textarea:focus { outline: none; border-color: #8b5cf6; box-shadow: 0 0 0 3px rgba(139,92,246,0.15); }
        .publish-modal textarea { resize: vertical; min-height: 80px; }
        .publish-modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 4px; }
        .publish-modal .close-btn { position: absolute; top: 12px; right: 16px; background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 1.2em; }
        .empty-state { text-align: center; padding: 40px 20px; color: var(--text-muted); }
        .empty-state h3 { font-size: 1.1em; margin-bottom: 8px; color: var(--text); }
        /* Add backtest dropdown */
        .add-bt-section { margin-top: 24px; }
        .add-bt-dropdown { position: relative; display: inline-block; }
        .add-bt-list { position: absolute; top: calc(100% + 4px); left: 0; min-width: 380px; max-height: 360px; overflow-y: auto; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 100; padding: 6px 0; }
        .add-bt-list.hidden { display: none; }
        .add-bt-item { display: flex; align-items: center; gap: 10px; padding: 10px 14px; cursor: pointer; font-size: 0.82em; color: var(--text-muted); transition: background 0.15s; border-bottom: 1px solid var(--border); }
        .add-bt-item:last-child { border-bottom: none; }
        .add-bt-item:hover { background: var(--bg-elevated); color: var(--text); }
        .add-bt-item.in-collection { opacity: 0.5; pointer-events: none; }
        .add-bt-item .abt-info { flex: 1; min-width: 0; }
        .add-bt-item .abt-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; color: var(--text); }
        .add-bt-item .abt-meta { font-size: 0.85em; color: var(--text-dim); margin-top: 2px; display: flex; gap: 8px; }
        .add-bt-item .abt-check { color: #8b5cf6; font-weight: 700; flex-shrink: 0; }
        /* Auth buttons */
        .header { text-align: center; margin-bottom: 24px; position: relative; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .auth-buttons { position: absolute; top: 0; right: 0; display: flex; gap: 10px; align-items: center; }
        .auth-btn { display: inline-block; padding: 10px 24px; border-radius: 8px; font-weight: 700; font-size: 0.9em; text-decoration: none; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; cursor: pointer; }
        .auth-btn-login { background: var(--accent); color: #fff; border: 2px solid var(--accent); }
        .auth-btn-login:hover { background: #e08a1a; border-color: #e08a1a; }
        .auth-btn-signup { background: var(--accent); color: #fff; border: 2px solid var(--accent); }
        .auth-btn-signup:hover { background: #e08a1a; border-color: #e08a1a; }
        /* Locked overlay */
        .locked-overlay { display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 5; cursor: pointer; border-radius: 14px; background: rgba(22,25,34,0.3); align-items: center; justify-content: center; flex-direction: column; gap: 8px; transition: background 0.2s ease; }
        .backtest-card-wrapper.locked .locked-overlay { display: flex; }
        .locked-overlay:hover { background: rgba(22,25,34,0.5); }
        .locked-overlay svg { width: 32px; height: 32px; color: var(--text-muted); transition: transform 0.3s ease; }
        .locked-overlay.shake svg { animation: lockShake 0.5s ease; }
        .locked-overlay span { font-size: 0.8em; font-weight: 600; color: var(--text-muted); letter-spacing: 0.05em; text-transform: uppercase; }
        @keyframes lockShake { 0%,100% { transform: translateX(0) rotate(0); } 15% { transform: translateX(-4px) rotate(-5deg); } 30% { transform: translateX(4px) rotate(5deg); } 45% { transform: translateX(-3px) rotate(-3deg); } 60% { transform: translateX(3px) rotate(3deg); } 75% { transform: translateX(-1px) rotate(-1deg); } }

        /* ── Mobile responsive ── */
        @media (max-width: 600px) {
            .auth-buttons { position: static; display: flex; justify-content: center; margin-bottom: 12px; }
            .auth-btn { padding: 8px 16px; font-size: 0.8em; }
        }
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
        }
        @media (max-width: 400px) {
            .backtest-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        {% if not is_authenticated %}
        <div class="auth-buttons">
            <a href="https://the-bitcoin-strategy.com/app/analytics-redirect" class="auth-btn auth-btn-login">Log In</a>
            <a href="https://the-bitcoin-strategy.com/subscribe" class="auth-btn auth-btn-signup">Sign Up</a>
        </div>
        {% endif %}
        <h1><a href="/" style="text-decoration:none;color:inherit;display:inline-flex;align-items:center;gap:0"><span class="brand-btc">Bitcoin</span><span class="brand-analytics">Strategy Analytics</span></a></h1>
        <div style="font-size:0.8em;color:var(--text-dim);margin-top:2px">Exclusive to <a href="https://the-bitcoin-strategy.com" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:600">Premium Members</a></div>
    </div>
    <a href="javascript:history.back()" class="back-link">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
        Back
    </a>
    <div class="coll-header">
        <span class="coll-badge">Collection</span>
        <h1 class="coll-title">{{ collection.title }}</h1>
        <div class="coll-meta">
            {% if author_avatar %}
            <img src="/static/avatars/{{ author_avatar }}" class="coll-meta-avatar" alt="">
            {% else %}
            <span class="coll-meta-initials" style="background:{{ author_avatar_color }}">{{ author_initial }}</span>
            {% endif %}
            <span>{{ display_name }}</span>
            <span style="color:var(--text-dim)">·</span>
            <span>{{ time_ago(collection.created_at) }}</span>
            <span style="color:var(--text-dim)">·</span>
            <span>{{ backtests|length }} backtest{{ 's' if backtests|length != 1 }}</span>
            <span style="color:var(--text-dim)">·</span>
            <span>{{ collection.views_count or 0 }} views</span>
        </div>
        {% if is_owner or is_admin %}
        <div class="coll-actions">
            <button class="action-btn" onclick="openEditModal()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                Edit
            </button>
            <button class="action-btn" onclick="copyLink()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                Share
            </button>
            <button class="action-btn danger" onclick="deleteCollection()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                Delete
            </button>
            {% if is_admin %}
            <select class="vis-select" id="coll-visibility" onchange="changeVisibility(this.value)">
                <option value="private"{{ ' selected' if collection.visibility == 'private' }}>Private</option>
                <option value="community"{{ ' selected' if collection.visibility == 'community' }}>Community</option>
                <option value="featured"{{ ' selected' if collection.visibility == 'featured' }}>Featured</option>
            </select>
            {% endif %}
        </div>
        {% endif %}
    </div>

    {% if youtube_embed_url %}
    <div class="yt-embed">
        <iframe src="{{ youtube_embed_url }}" allowfullscreen allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"></iframe>
    </div>
    {% endif %}

    {% if collection.copy_trading_url %}
    <a href="{{ collection.copy_trading_url }}" target="_blank" rel="noopener" class="copy-trading-link">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
        Automatic Copy Trading — Trade this strategy automatically
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
    </a>
    {% endif %}

    {% if collection.description %}
    <div class="coll-desc">{{ collection.description }}</div>
    {% endif %}

    <div class="section-divider"><span>Backtests in this Collection</span></div>

    {% if backtests %}
    <div class="backtest-grid">
        {% for bt in backtests %}
        <div class="backtest-card-wrapper{{ ' locked' if not is_authenticated and not loop.first else '' }}" data-bt-id="{{ bt.id }}" {% if is_owner %}draggable="true"{% endif %}>
            {% if not is_authenticated and not loop.first %}
            <div class="locked-overlay" onclick="shakeLock(this)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                <span>Locked</span>
            </div>
            {% endif %}
            {% if is_owner %}
            <div class="drag-handle" title="Drag to reorder">⠿</div>
            <button class="remove-bt-btn" onclick="event.preventDefault();removeBacktest('{{ bt.id }}')" title="Remove from collection">&times;</button>
            {% endif %}
            <a class="backtest-card" href="{{ '/backtest/' ~ bt.id if is_authenticated or loop.first else '#' }}">
                <div class="backtest-card-head">
                    {% if bt._asset_logo %}<img class="backtest-card-asset-logo" src="/static/logos/{{ bt._asset_logo }}" alt="{{ bt._asset_display }}">{% else %}<div class="backtest-card-asset-fallback">{{ bt._asset_display[:1] }}</div>{% endif %}
                    <div class="backtest-card-head-text">
                        <div class="backtest-card-title">{{ bt.title|title if bt.title else 'Untitled Backtest' }}</div>
                        <div class="backtest-card-asset-name">{{ bt._asset_display }}</div>
                    </div>
                    <div class="backtest-card-mode-icon" title="{{ bt._mode_label }}">{{ bt._mode_svg|safe }}</div>
                </div>
                {% if bt.thumbnail %}<img class="backtest-card-thumb" src="{{ bt.thumbnail }}" alt="Chart">{% endif %}
                {% if bt.description %}<div class="backtest-card-desc">{{ bt.description[:250] }}</div>{% endif %}
                <div class="backtest-card-params">
                    <span class="backtest-card-tag">{{ bt._mode_label }}</span>
                    <span class="backtest-card-tag">{{ bt._strategy }}</span>
                    {% if bt._leverage %}<span class="backtest-card-tag">{{ bt._leverage }}</span>{% endif %}
                    {% if bt._start_date %}<span class="backtest-card-tag">{{ bt._start_date }}</span>{% endif %}
                </div>
                {% if bt._apr %}
                <div class="backtest-card-metrics">
                    <div class="card-metric">
                        <span class="card-metric-label">APR</span>
                        <span class="card-metric-val {{ 'positive' if bt._apr|float > 0 else 'negative' }}">{{ bt._apr }}%</span>
                        <span class="card-metric-vs">vs {{ bt._apr_bh }}% B&H</span>
                    </div>
                    <div class="card-metric">
                        <span class="card-metric-label">Max DD</span>
                        <span class="card-metric-val negative">{{ bt._max_dd }}%</span>
                        <span class="card-metric-vs">vs {{ bt._max_dd_bh }}% B&H</span>
                    </div>
                </div>
                {% endif %}
                <div class="backtest-card-footer">
                    <div class="card-author">
                        {% if bt._avatar %}
                        <img src="/static/avatars/{{ bt._avatar }}" class="card-author-avatar" alt="">
                        {% else %}
                        <span class="card-author-initials" style="background:{{ bt._avatar_color }}">{{ bt._initial }}</span>
                        {% endif %}
                        <span class="card-author-name">{{ bt._display_name }}</span>
                        <span class="card-author-sep">·</span>
                        <span class="card-author-time">{{ time_ago(bt.created_at) }}</span>
                    </div>
                    <div class="engagement">
                        <span>{{ bt.likes_count }} likes</span>
                        <span>{{ bt.comments_count }} comments</span>
                    </div>
                </div>
            </a>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="empty-state">
        <h3>No backtests yet</h3>
        <p>Add backtests to this collection from your My Backtests page.</p>
    </div>
    {% endif %}

    {% if is_owner %}
    <div class="add-bt-section">
        <div class="add-bt-dropdown">
            <button class="action-btn" onclick="toggleAddBtList()" style="background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#fff;border-color:transparent">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                Add Backtest
            </button>
            <div class="add-bt-list hidden" id="add-bt-list">
                {% for ubt in user_backtests|sort(attribute='created_at', reverse=true) %}
                <div class="add-bt-item{{ ' in-collection' if ubt.id in collection_bt_ids else '' }}" onclick="addBacktest('{{ ubt.id }}')">
                    <div class="abt-info">
                        <div class="abt-title">{{ ubt.title or 'Untitled' }}</div>
                        <div class="abt-meta">
                            {% if ubt._asset %}<span>{{ ubt._asset }}</span>{% endif %}
                            <span>{{ ubt.created_at[:10] if ubt.created_at else '' }}</span>
                        </div>
                    </div>
                    {% if ubt.id in collection_bt_ids %}<span class="abt-check">Added</span>{% endif %}
                </div>
                {% endfor %}
                {% if not user_backtests %}
                <div style="padding:12px 14px;color:var(--text-dim);font-size:0.82em">No backtests to add</div>
                {% endif %}
            </div>
        </div>
    </div>
    {% endif %}
</div>

<script>
function shakeLock(el) {
    el.classList.remove('shake');
    void el.offsetWidth;
    el.classList.add('shake');
    setTimeout(function() { el.classList.remove('shake'); }, 600);
}
var _swal = Swal.mixin({
    background: '#1e2130', color: '#e8e9ed', confirmButtonColor: '#8b5cf6',
    customClass: { popup: 'swal-dark' }
});
var collId = '{{ collection.id }}';
function copyLink() {
    navigator.clipboard.writeText(window.location.href);
    _swal.fire({icon:'success', title:'Link copied!', timer:1500, showConfirmButton:false});
}
function deleteCollection() {
    _swal.fire({
        title: 'Delete this collection?', text: 'Backtests inside will not be deleted.',
        icon: 'warning', showCancelButton: true, confirmButtonText: 'Delete', confirmButtonColor: '#e74c3c'
    }).then(function(result) {
        if (!result.isConfirmed) return;
        fetch('/api/collection/' + collId + '/delete', { method: 'POST' })
        .then(function() { window.location.href = '/my-backtests'; });
    });
}
function changeVisibility(vis) {
    fetch('/api/collection/' + collId + '/visibility', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({visibility: vis})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) { _swal.fire({icon:'error', title:data.error}); return; }
        _swal.fire({icon:'success', title:'Visibility updated to ' + vis, timer:1500, showConfirmButton:false});
    });
}
function removeBacktest(btId) {
    fetch('/api/collection/' + collId + '/remove-backtest', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({backtest_id: btId})
    }).then(function() { location.reload(); });
}
function toggleAddBtList() {
    document.getElementById('add-bt-list').classList.toggle('hidden');
}
function addBacktest(btId) {
    fetch('/api/collection/' + collId + '/add-backtest', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({backtest_id: btId})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) { _swal.fire({icon:'warning', title:data.error}); return; }
        location.reload();
    });
}
function openEditModal() {
    document.getElementById('edit-modal-overlay').classList.add('open');
    document.getElementById('edit-coll-title').focus();
}
function closeEditModal() {
    document.getElementById('edit-modal-overlay').classList.remove('open');
}
function saveEditColl() {
    var title = document.getElementById('edit-coll-title').value.trim();
    var desc = document.getElementById('edit-coll-desc').value.trim();
    var yt = document.getElementById('edit-coll-youtube').value.trim();
    var ct = document.getElementById('edit-coll-copytrading').value.trim();
    fetch('/api/collection/' + collId + '/update', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, description: desc, youtube_url: yt, copy_trading_url: ct})
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) throw new Error(data.error);
        closeEditModal(); location.reload();
    }).catch(function(e) { _swal.fire({icon:'error', title:'Failed', text:e.message}); });
}
document.addEventListener('click', function(e) {
    var list = document.getElementById('add-bt-list');
    if (list && !list.classList.contains('hidden')) {
        var wrap = e.target.closest('.add-bt-dropdown');
        if (!wrap) list.classList.add('hidden');
    }
});
// Drag-and-drop reorder
(function() {
    var dragSrc = null;
    var grid = document.querySelector('.backtest-grid');
    if (!grid) return;
    var cards = grid.querySelectorAll('.backtest-card-wrapper[draggable]');
    if (!cards.length) return;
    cards.forEach(function(card) {
        card.addEventListener('dragstart', function(e) {
            dragSrc = card;
            card.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', card.dataset.btId);
        });
        card.addEventListener('dragend', function() {
            card.classList.remove('dragging');
            grid.querySelectorAll('.drag-over').forEach(function(el) { el.classList.remove('drag-over'); });
        });
        card.addEventListener('dragover', function(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            if (card !== dragSrc) card.classList.add('drag-over');
        });
        card.addEventListener('dragleave', function() {
            card.classList.remove('drag-over');
        });
        card.addEventListener('drop', function(e) {
            e.preventDefault();
            card.classList.remove('drag-over');
            if (dragSrc === card) return;
            // Reorder DOM
            var allCards = Array.from(grid.querySelectorAll('.backtest-card-wrapper[data-bt-id]'));
            var fromIdx = allCards.indexOf(dragSrc);
            var toIdx = allCards.indexOf(card);
            if (fromIdx < toIdx) {
                card.parentNode.insertBefore(dragSrc, card.nextSibling);
            } else {
                card.parentNode.insertBefore(dragSrc, card);
            }
            // Save new order
            var newOrder = Array.from(grid.querySelectorAll('.backtest-card-wrapper[data-bt-id]')).map(function(el) { return el.dataset.btId; });
            fetch('/api/collection/' + collId + '/reorder', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ordered_ids: newOrder})
            });
        });
    });
})();
</script>

{% if is_owner or is_admin %}
<div class="publish-modal-overlay" id="edit-modal-overlay">
    <div class="publish-modal">
        <button class="close-btn" onclick="closeEditModal()">&times;</button>
        <h3>Edit Collection</h3>
        <label for="edit-coll-title">Title</label>
        <input type="text" id="edit-coll-title" maxlength="120" value="{{ collection.title|e }}">
        <label for="edit-coll-desc">Description</label>
        <textarea id="edit-coll-desc">{{ collection.description or '' }}</textarea>
        <label for="edit-coll-youtube">Video URL</label>
        <input type="text" id="edit-coll-youtube" value="{{ collection.youtube_url or '' }}" placeholder="YouTube or Vimeo URL">
        <label for="edit-coll-copytrading">Copy Trading URL</label>
        <input type="text" id="edit-coll-copytrading" value="{{ collection.copy_trading_url or '' }}" placeholder="https://the-bitcoin-strategy.com/automatic-copy-trading">
        <div class="publish-modal-actions">
            <button class="action-btn" onclick="closeEditModal()">Cancel</button>
            <button class="action-btn" onclick="saveEditColl()" style="background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#fff;border-color:transparent">Save Changes</button>
        </div>
    </div>
</div>
{% endif %}
</body>
</html>
"""


UNSUBSCRIBE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Unsubscribe — Bitcoin Strategy Analytics</title>
<style>
body { margin:0; padding:0; background:#0d1117; color:#e6edf3; font-family:'DM Sans',-apple-system,sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; }
.card { background:#161b22; border:1px solid #30363d; border-radius:16px; padding:48px; max-width:480px; text-align:center; }
.card h1 { font-size:24px; margin:0 0 16px; }
.card p { color:#8b949e; line-height:1.6; margin:0 0 24px; }
.card a { color:#58a6ff; text-decoration:none; }
.card a:hover { text-decoration:underline; }
.icon { font-size:48px; margin-bottom:16px; }
</style>
</head>
<body>
<div class="card">
{% if success %}
<div class="icon">&#x2705;</div>
<h1>Unsubscribed</h1>
<p>You will no longer receive email alerts for <b>{{ backtest_title }}</b>.</p>
<p>You can re-enable alerts from the backtest page, or manage all alerts in your <a href="https://analytics.the-bitcoin-strategy.com/account">account settings</a>.</p>
{% else %}
<div class="icon">&#x26A0;&#xFE0F;</div>
<h1>Alert Not Found</h1>
<p>This alert has already been unsubscribed or does not exist.</p>
{% endif %}
<p><a href="https://analytics.the-bitcoin-strategy.com/">Back to Bitcoin Strategy Analytics</a></p>
</div>
</body>
</html>"""


ACCOUNT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Account Settings - Bitcoin Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root { --bg-deep: #0d0f1a; --bg-base: #131525; --bg-surface: #1a1d2e; --bg-elevated: #242842; --border: #2a2e45; --border-hover: #3d4266; --text: #e8e9ed; --text-muted: #b0b3c5; --text-dim: #6b7094; --accent: #F7931A; --accent-glow: rgba(247,147,26,0.15); --blue: #6495ED; }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: var(--bg-deep); color: var(--text); font-family: 'DM Sans', sans-serif; padding: 20px; }
        .container { max-width: 600px; margin: 0 auto; }
        .header { text-align: center; margin-bottom: 32px; position: relative; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; position: relative; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        /* Theme toggle */
        .theme-toggle { background: none; border: 1px solid var(--border); cursor: pointer; color: var(--text-muted); padding: 7px; border-radius: 8px; transition: all 0.2s ease; display: flex; align-items: center; justify-content: center; }
        .theme-toggle:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border-hover); }
        .theme-toggle svg { width: 16px; height: 16px; }
        .theme-toggle .icon-sun { display: none; }
        .theme-toggle .icon-moon { display: block; }
        [data-theme="light"] .theme-toggle .icon-sun { display: block; }
        [data-theme="light"] .theme-toggle .icon-moon { display: none; }
        .nav-right-group { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; z-index: 9999; }
        .notif-bell-wrap { position: relative; }
        .notif-bell { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 8px; border-radius: 8px; position: relative; transition: all 0.2s ease; }
        .notif-bell:hover { color: var(--text); background: var(--bg-elevated); }
        .notif-badge { position: absolute; top: 2px; right: 2px; background: #e74c3c; color: #fff; font-size: 0.65em; font-weight: 700; min-width: 16px; height: 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; padding: 0 4px; font-family: 'JetBrains Mono', monospace; }
        .notif-badge.hidden { display: none; }
        .notif-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 340px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; }
        .notif-dropdown.hidden { display: none; }
        .notif-dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.85em; color: var(--text); }
        .notif-list { max-height: 320px; overflow-y: auto; }
        .notif-item { display: block; padding: 12px 16px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); font-size: 0.82em; transition: background 0.15s ease; cursor: pointer; }
        .notif-item.notif-unread { background: rgba(100,149,237,0.08); border-left: 3px solid var(--accent); }
        .notif-item.notif-read { opacity: 0.55; }
        .notif-item:hover { background: var(--bg-elevated); opacity: 1; }
        .notif-item:last-child { border-bottom: none; }
        .notif-item-text { line-height: 1.4; }
        .notif-item-text strong { color: var(--accent); font-weight: 600; }
        .notif-item-time { color: var(--text-dim); font-size: 0.78em; margin-top: 4px; }
        .notif-empty { padding: 24px 16px; text-align: center; color: var(--text-dim); font-size: 0.82em; }
        .avatar-wrap { position: relative; }
        .avatar-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 50%; transition: all 0.2s ease; }
        .avatar-btn:hover { background: var(--bg-elevated); }
        .avatar-img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); display: block; }
        .avatar-initials { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 700; color: #fff; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 220px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; padding: 6px 0; }
        .avatar-dropdown.hidden { display: none; }
        .avatar-dropdown-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; color: var(--text-muted); text-decoration: none; font-size: 0.82em; font-weight: 500; transition: all 0.15s ease; font-family: 'DM Sans', sans-serif; }
        .avatar-dropdown-item:hover { background: var(--bg-elevated); color: var(--text); }
        .avatar-dropdown-item svg { width: 16px; height: 16px; flex-shrink: 0; }
        .avatar-dropdown-divider { height: 1px; background: var(--border); margin: 4px 0; }
        .avatar-dropdown-logout { color: #e74c3c; }
        .avatar-dropdown-logout:hover { background: rgba(231,76,60,0.1); color: #e74c3c; }
        .panel { background: var(--bg-surface); border-radius: 16px; padding: 24px; border: 1px solid var(--border); margin-bottom: 16px; }
        .page-title { font-size: 1.4em; font-weight: 700; margin-bottom: 20px; }
        .setting-group { margin-bottom: 24px; }
        .setting-label { font-size: 0.8em; color: var(--text-muted); font-weight: 500; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
        .setting-input { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.85em; font-family: 'DM Sans', sans-serif; }
        .setting-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .setting-input[readonly] { opacity: 0.6; cursor: not-allowed; }
        .setting-btn { padding: 8px 20px; border-radius: 8px; border: none; font-weight: 600; font-size: 0.82em; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; }
        .setting-btn-primary { background: var(--accent); color: #fff; }
        .setting-btn-primary:hover { background: #e08a1a; }
        .setting-btn-danger { background: transparent; color: #e74c3c; border: 1px solid #e74c3c; }
        .setting-btn-danger:hover { background: rgba(231,76,60,0.1); }
        .avatar-preview { width: 96px; height: 96px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 2em; font-weight: 700; color: #fff; margin-bottom: 12px; overflow: hidden; }
        .avatar-preview img { width: 100%; height: 100%; object-fit: cover; }
        .avatar-actions { display: flex; gap: 10px; align-items: center; }
        .toggle-switch { position: relative; display: inline-block; width: 44px; height: 24px; }
        .toggle-switch input { opacity: 0; width: 0; height: 0; }
        .toggle-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background: var(--bg-elevated); border: 1px solid var(--border); transition: 0.3s; border-radius: 24px; }
        .toggle-slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 2px; bottom: 2px; background: var(--text-dim); transition: 0.3s; border-radius: 50%; }
        input:checked + .toggle-slider { background: var(--accent); border-color: var(--accent); }
        input:checked + .toggle-slider:before { transform: translateX(20px); background: #fff; }
        .toggle-row { display: flex; align-items: center; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid var(--border); }
        .toggle-row:last-child { border-bottom: none; }
        .toggle-info { }
        .toggle-title { font-size: 0.85em; font-weight: 600; }
        .toggle-desc { font-size: 0.75em; color: var(--text-dim); margin-top: 2px; }

        /* ── Mobile responsive ── */
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
            .nav-bar { flex-wrap: wrap; }
            .nav-link { padding: 6px 10px; font-size: 0.75em; }
        }
        @media (max-width: 400px) {
            .notif-dropdown { width: calc(100vw - 32px); right: -8px; }
        }
    </style>
</head>
<body>
<div class="container">
""" + NAV_HTML + """

    <div class="panel">
        <h2 class="page-title">Account Settings</h2>

        <div class="setting-group">
            <div class="setting-label">Profile Picture</div>
            <div class="avatar-preview" id="avatar-preview" style="background:{{ avatar_color }}">
                {% if avatar %}
                <img src="/static/avatars/{{ avatar }}" alt="Avatar">
                {% else %}
                {{ initial }}
                {% endif %}
            </div>
            <div class="avatar-actions">
                <button class="setting-btn setting-btn-primary" onclick="document.getElementById('avatar-file').click()">Upload Photo</button>
                <input type="file" id="avatar-file" accept="image/jpeg,image/png,image/webp" style="display:none" onchange="uploadAvatar(this.files[0])">
                {% if avatar %}
                <button class="setting-btn setting-btn-danger" onclick="removeAvatar()">Remove</button>
                {% endif %}
            </div>
        </div>

        <div class="setting-group">
            <div class="setting-label">Username</div>
            <div style="display:flex;gap:10px;align-items:center">
                <input type="text" class="setting-input" id="username-input" value="{{ display_name or email_prefix }}" maxlength="40" style="flex:1">
                <button class="setting-btn setting-btn-primary" onclick="saveUsername()">Save</button>
            </div>
        </div>

        <div class="setting-group">
            <div class="setting-label">Email</div>
            <input type="text" class="setting-input" value="{{ email }}" readonly>
        </div>

        <div class="setting-group">
            <div class="setting-label">Email Notifications</div>
            <div class="toggle-row">
                <div class="toggle-info">
                    <div class="toggle-title">New comments on my backtests</div>
                    <div class="toggle-desc">Get emailed when someone comments on a backtest you published</div>
                </div>
                <label class="toggle-switch">
                    <input type="checkbox" id="notify-comments" {{ 'checked' if notify_comments }} onchange="saveNotifPref()">
                    <span class="toggle-slider"></span>
                </label>
            </div>
            <div class="toggle-row">
                <div class="toggle-info">
                    <div class="toggle-title">Replies to my comments</div>
                    <div class="toggle-desc">Get emailed when someone replies to a comment you wrote</div>
                </div>
                <label class="toggle-switch">
                    <input type="checkbox" id="notify-replies" {{ 'checked' if notify_replies }} onchange="saveNotifPref()">
                    <span class="toggle-slider"></span>
                </label>
            </div>
        </div>

        <div class="setting-group">
            <div class="setting-label">Signal Email Alerts <span style="color:var(--text-muted);font-weight:400;font-size:14px">({{ email_alert_count }} / {{ email_alert_limit }})</span></div>
            <div id="email-alerts-list" style="color:var(--text-muted);font-size:14px">
                {% if email_alerts %}
                {% for a in email_alerts %}
                <div class="toggle-row" style="padding:10px 0;border-bottom:1px solid var(--border)" data-bt-id="{{ a.backtest_id }}">
                    <div class="toggle-info">
                        <div class="toggle-title"><a href="/backtest/{{ a.backtest_id }}" style="color:var(--text);text-decoration:none">{{ a.title or 'Untitled' }}</a></div>
                    </div>
                    <button class="action-btn danger" style="font-size:12px;padding:4px 12px" onclick="removeEmailAlert('{{ a.backtest_id }}', this)">Remove</button>
                </div>
                {% endfor %}
                {% else %}
                <p style="color:var(--text-muted);margin:8px 0">No active signal alerts. You can enable alerts from any backtest detail page.</p>
                {% endif %}
            </div>
        </div>
    </div>
</div>
<script src="/static/js/nav.js"></script>
<script>
function removeEmailAlert(btId, btn) {
    btn.disabled = true;
    btn.textContent = '...';
    fetch('/api/backtest/' + btId + '/email-alert', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: false})
    }).then(function(r) { return r.json(); }).then(function() {
        btn.closest('.toggle-row').remove();
        var remaining = document.querySelectorAll('#email-alerts-list .toggle-row').length;
        document.querySelector('.setting-label span').textContent = '(' + remaining + ' / {{ email_alert_limit }})';
        if (remaining === 0) {
            document.getElementById('email-alerts-list').innerHTML = '<p style="color:var(--text-muted);margin:8px 0">No active signal alerts. You can enable alerts from any backtest detail page.</p>';
        }
    }).catch(function() { btn.disabled = false; btn.textContent = 'Remove'; });
}
// Account settings functions
function uploadAvatar(file) {
    if (!file) return;
    var fd = new FormData();
    fd.append('avatar', file);
    fetch('/api/avatar', { method: 'POST', body: fd })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.ok) {
            _swal.fire({icon:'success', title:'Avatar updated!', timer:1500, showConfirmButton:false});
            setTimeout(function() { location.reload(); }, 1600);
        } else {
            _swal.fire({icon:'error', title:'Upload failed', text: data.error});
        }
    }).catch(function(e) { _swal.fire({icon:'error', title:'Upload failed', text:e.message}); });
}
function removeAvatar() {
    fetch('/api/avatar', { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.ok) location.reload();
    });
}
function saveUsername() {
    var name = document.getElementById('username-input').value.trim();
    if (!name) { _swal.fire({icon:'warning', title:'Please enter a username'}); return; }
    fetch('/api/display-name', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({display_name: name})
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) _swal.fire({icon:'success', title:'Username saved!', timer:1500, showConfirmButton:false});
    }).catch(function(e) { _swal.fire({icon:'error', title:'Save failed', text:e.message}); });
}
function saveNotifPref() {
    fetch('/api/notification-pref', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            notify_comments: document.getElementById('notify-comments').checked,
            notify_replies: document.getElementById('notify-replies').checked
        })
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) _swal.fire({icon:'success', title:'Preferences saved!', timer:1200, showConfirmButton:false});
    });
}
</script>
</body></html>
"""

FEEDBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Send Feedback - Bitcoin Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root { --bg-deep: #0d0f1a; --bg-base: #131525; --bg-surface: #1a1d2e; --bg-elevated: #242842; --border: #2a2e45; --border-hover: #3d4266; --text: #e8e9ed; --text-muted: #b0b3c5; --text-dim: #6b7094; --accent: #F7931A; --accent-glow: rgba(247,147,26,0.15); --blue: #6495ED; }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: var(--bg-deep); color: var(--text); font-family: 'DM Sans', sans-serif; padding: 20px; }
        .container { max-width: 600px; margin: 0 auto; }
        .header { text-align: center; margin-bottom: 32px; position: relative; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; position: relative; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        /* Theme toggle */
        .theme-toggle { background: none; border: 1px solid var(--border); cursor: pointer; color: var(--text-muted); padding: 7px; border-radius: 8px; transition: all 0.2s ease; display: flex; align-items: center; justify-content: center; }
        .theme-toggle:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border-hover); }
        .theme-toggle svg { width: 16px; height: 16px; }
        .theme-toggle .icon-sun { display: none; }
        .theme-toggle .icon-moon { display: block; }
        [data-theme="light"] .theme-toggle .icon-sun { display: block; }
        [data-theme="light"] .theme-toggle .icon-moon { display: none; }
        .nav-right-group { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; z-index: 9999; }
        .notif-bell-wrap { position: relative; }
        .notif-bell { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 8px; border-radius: 8px; position: relative; transition: all 0.2s ease; }
        .notif-bell:hover { color: var(--text); background: var(--bg-elevated); }
        .notif-badge { position: absolute; top: 2px; right: 2px; background: #e74c3c; color: #fff; font-size: 0.65em; font-weight: 700; min-width: 16px; height: 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; padding: 0 4px; font-family: 'JetBrains Mono', monospace; }
        .notif-badge.hidden { display: none; }
        .notif-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 340px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; }
        .notif-dropdown.hidden { display: none; }
        .notif-dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.85em; color: var(--text); }
        .notif-list { max-height: 320px; overflow-y: auto; }
        .notif-item { display: block; padding: 12px 16px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); font-size: 0.82em; transition: background 0.15s ease; cursor: pointer; }
        .notif-item.notif-unread { background: rgba(100,149,237,0.08); border-left: 3px solid var(--accent); }
        .notif-item.notif-read { opacity: 0.55; }
        .notif-item:hover { background: var(--bg-elevated); opacity: 1; }
        .notif-item:last-child { border-bottom: none; }
        .notif-item-text { line-height: 1.4; }
        .notif-item-text strong { color: var(--accent); font-weight: 600; }
        .notif-item-time { color: var(--text-dim); font-size: 0.78em; margin-top: 4px; }
        .notif-empty { padding: 24px 16px; text-align: center; color: var(--text-dim); font-size: 0.82em; }
        .avatar-wrap { position: relative; }
        .avatar-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 50%; transition: all 0.2s ease; }
        .avatar-btn:hover { background: var(--bg-elevated); }
        .avatar-img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); display: block; }
        .avatar-initials { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 700; color: #fff; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px); width: 220px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); z-index: 9999; overflow: hidden; padding: 6px 0; }
        .avatar-dropdown.hidden { display: none; }
        .avatar-dropdown-item { display: flex; align-items: center; gap: 10px; padding: 10px 16px; color: var(--text-muted); text-decoration: none; font-size: 0.82em; font-weight: 500; transition: all 0.15s ease; font-family: 'DM Sans', sans-serif; }
        .avatar-dropdown-item:hover { background: var(--bg-elevated); color: var(--text); }
        .avatar-dropdown-item svg { width: 16px; height: 16px; flex-shrink: 0; }
        .avatar-dropdown-divider { height: 1px; background: var(--border); margin: 4px 0; }
        .avatar-dropdown-logout { color: #e74c3c; }
        .avatar-dropdown-logout:hover { background: rgba(231,76,60,0.1); color: #e74c3c; }
        .panel { background: var(--bg-surface); border-radius: 16px; padding: 24px; border: 1px solid var(--border); }
        .page-title { font-size: 1.4em; font-weight: 700; margin-bottom: 6px; }
        .page-subtitle { font-size: 0.85em; color: var(--text-muted); margin-bottom: 20px; }
        .feedback-textarea { width: 100%; min-height: 160px; padding: 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.9em; font-family: 'DM Sans', sans-serif; resize: vertical; margin-bottom: 12px; }
        .feedback-textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .feedback-btn { padding: 10px 28px; border-radius: 8px; border: none; background: var(--accent); color: #fff; font-weight: 600; font-size: 0.9em; cursor: pointer; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; }
        .feedback-btn:hover { background: #e08a1a; }

        /* ── Mobile responsive ── */
        @media (max-width: 480px) {
            .container { padding: 16px 12px; }
            .header h1 { font-size: 1.2em; }
            .nav-bar { flex-wrap: wrap; }
            .nav-link { padding: 6px 10px; font-size: 0.75em; }
        }
        @media (max-width: 400px) {
            .notif-dropdown { width: calc(100vw - 32px); right: -8px; }
        }
    </style>
</head>
<body>
<div class="container">
""" + NAV_HTML + """

    <div class="panel">
        <h2 class="page-title">Send Feedback</h2>
        <p class="page-subtitle">We'd love to hear your thoughts, suggestions, or bug reports. Your feedback helps us improve!</p>
        <textarea class="feedback-textarea" id="feedback-body" placeholder="What's on your mind?"></textarea>
        <button class="feedback-btn" onclick="submitFeedback()">Send Feedback</button>
    </div>
</div>
<script src="/static/js/nav.js"></script>
<script>
// Feedback
function submitFeedback() {
    var body = document.getElementById('feedback-body').value.trim();
    if (!body) { _swal.fire({icon:'warning', title:'Please write some feedback'}); return; }
    fetch('/api/feedback', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({body: body})
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
            document.getElementById('feedback-body').value = '';
            _swal.fire({icon:'success', title:'Thank you!', text:'Your feedback has been sent.', timer:2000, showConfirmButton:false});
        }
    }).catch(function(e) { _swal.fire({icon:'error', title:'Failed to send', text:e.message}); });
}
</script>
</body></html>
"""


@app.route('/account')
@require_auth
def account_page():
    """Account settings page."""
    user_id = session.get('user_id')
    email = session.get('email', '')
    display_name = db.get_display_name(user_id)
    avatar = db.get_user_avatar(user_id)
    prefs = db.get_notification_prefs(user_id)
    email_alerts = db.list_user_email_alerts(user_id)
    email_alert_count = len(email_alerts)
    email_alert_limit = db.MAX_EMAIL_ALERTS_PER_USER
    return render_template_string(ACCOUNT_HTML,
        nav_active='account',
        display_name=display_name,
        email=email,
        email_prefix=email.split('@')[0] if email else '',
        avatar=avatar,
        avatar_color=_avatar_color(user_id),
        initial=_user_initial(display_name, email),
        notify_comments=prefs['notify_comments'],
        notify_replies=prefs['notify_replies'],
        email_alerts=email_alerts,
        email_alert_count=email_alert_count,
        email_alert_limit=email_alert_limit)


@app.route('/feedback')
@require_auth
def feedback_page():
    """Feedback page."""
    return render_template_string(FEEDBACK_HTML, nav_active='feedback')


# ---------------------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------------------

_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <script>document.documentElement.setAttribute("data-theme",localStorage.getItem("theme")||"dark")</script>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{{ code }} — Strategy Analytics</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/x-icon" href="/static/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-deep: #080a10; --bg-base: #0f1117; --bg-surface: #161922;
            --bg-elevated: #1c2030; --border: #252a3a; --text: #e8eaf0;
            --text-muted: #8890a4; --accent: #f7931a; --accent-hover: #ffa940;
            --blue: #6495ED; --green: #34d399;
        }
        [data-theme="light"] {
            --bg-deep: #f5f6fa; --bg-base: #ebedf5; --bg-surface: #ffffff; --bg-elevated: #f0f2f5;
            --border: #d0d4e0; --border-hover: #a0a8c0; --text: #1a1a2e; --text-muted: #5a6078; --text-dim: #8890a4;
            --accent: #d97706; --accent-hover: #b45309; --accent-glow: rgba(217, 119, 6, 0.15);
            --green: #059669; --green-dim: rgba(5, 150, 105, 0.12); --blue: #3a6fd8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'DM Sans', sans-serif; background: var(--bg-deep);
            color: var(--text); min-height: 100vh; display: flex;
            align-items: center; justify-content: center; overflow: hidden;
        }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background:
                radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247,147,26,0.06), transparent),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100,149,237,0.04), transparent);
            pointer-events: none;
        }
        [data-theme="light"] body::before {
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(217, 119, 6, 0.04), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(58, 111, 216, 0.03), transparent);
        }
        .error-container {
            text-align: center; position: relative; z-index: 1;
            animation: fadeUp 0.6s ease-out;
        }
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .error-code {
            font-size: 8rem; font-weight: 700; letter-spacing: -0.04em;
            line-height: 1; margin-bottom: 8px;
            background: linear-gradient(135deg, var(--blue), var(--accent));
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .error-title {
            font-size: 1.5rem; font-weight: 600; margin-bottom: 12px;
            color: var(--text);
        }
        .error-message {
            font-size: 0.95rem; color: var(--text-muted); max-width: 420px;
            margin: 0 auto 32px; line-height: 1.6;
        }
        .error-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
        .error-btn {
            display: inline-flex; align-items: center; gap: 8px;
            padding: 10px 24px; border-radius: 10px; font-size: 0.85rem;
            font-weight: 500; font-family: 'DM Sans', sans-serif;
            text-decoration: none; transition: all 0.2s ease; cursor: pointer;
            border: 1px solid var(--border);
        }
        .error-btn-primary {
            background: linear-gradient(135deg, var(--blue), #4a7dd6);
            color: #fff; border-color: transparent;
        }
        .error-btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 20px rgba(100,149,237,0.3); }
        .error-btn-ghost { background: var(--bg-elevated); color: var(--text-muted); }
        .error-btn-ghost:hover { border-color: var(--border-hover, #3a4060); color: var(--text); }
        .logo {
            margin-bottom: 32px; display: inline-flex; font-size: 1rem;
            font-weight: 700; letter-spacing: -0.02em;
        }
        .logo .brand-btc {
            background: linear-gradient(135deg, var(--blue), #4a7dd6);
            color: #fff; padding: 6px 14px;
        }
        .logo .brand-analytics {
            background: var(--bg-elevated); color: var(--text);
            padding: 6px 14px; border: 1px solid var(--border); border-left: none;
        }
        .particle {
            position: fixed; border-radius: 50%; pointer-events: none;
            opacity: 0; animation: drift linear infinite;
        }
        @keyframes drift {
            0%   { opacity: 0; transform: translateY(0) scale(0); }
            15%  { opacity: 0.6; }
            100% { opacity: 0; transform: translateY(-100vh) scale(1); }
        }
    </style>
</head>
<body>
    {% for i in range(12) %}
    <div class="particle" style="
        left: {{ range(5,95)|random }}%;
        bottom: -10px;
        width: {{ range(2,6)|random }}px;
        height: {{ range(2,6)|random }}px;
        background: {{ ['var(--accent)','var(--blue)','var(--green)'][range(0,2)|random] }};
        animation-duration: {{ range(6,14)|random }}s;
        animation-delay: {{ range(0,8)|random }}s;
    "></div>
    {% endfor %}

    <div class="error-container">
        <div class="logo">
            <span class="brand-btc">Strategy</span><span class="brand-analytics">Analytics</span>
        </div>
        <div class="error-code">{{ code }}</div>
        <div class="error-title">{{ title }}</div>
        <div class="error-message">{{ message }}</div>
        <div class="error-actions">
            <a href="/" class="error-btn error-btn-primary">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
                Go Home
            </a>
            <a href="javascript:history.back()" class="error-btn error-btn-ghost">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
                Go Back
            </a>
        </div>
    </div>
</body>
</html>"""

_ERROR_MAP = {
    400: ("Bad Request", "The request didn't quite make sense. Double-check the URL or form data and try again."),
    401: ("Not Authenticated", "You need to be logged in to view this page. Sign in and try again."),
    403: ("Access Denied", "You don't have permission to view this page. If you think this is a mistake, reach out to us."),
    404: ("Page Not Found", "This backtest may have been deleted, or the link might be broken. It happens to the best of us."),
    500: ("Server Error", "Something went wrong on our end. We're on it — please try again in a moment."),
}

@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def handle_error(e):
    code = e.code if hasattr(e, 'code') else 500
    title, message = _ERROR_MAP.get(code, ("Error", "Something unexpected happened."))
    return render_template_string(_ERROR_HTML, code=code, title=title, message=message), code


if __name__ == "__main__":
    print(f"Starting Strategy Analytics at http://localhost:5000 (assets: {', '.join(ASSET_NAMES)})")
    app.run(debug=False, port=5000)
