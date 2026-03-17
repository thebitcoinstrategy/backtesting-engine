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
import backtest as bt
import threading
import uuid
import database as db

app = Flask(__name__)
db.init_db()

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
    core = {"mode", "asset", "start_date", "end_date", "initial_cash", "fee", "sizing", "reverse"}
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


def require_auth(f):
    """Decorator: require valid token or active session, else redirect to Laravel login."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Check for token in query string
        token = request.args.get('token')
        if token:
            payload = _validate_token(token)
            if payload:
                session.permanent = True
                session['user_id'] = payload.get('user_id')
                session['email'] = payload.get('email')
                session['auth_time'] = time.time()
                # Redirect to clean URL (strip token from query string)
                return redirect('/backtester', code=302)
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
        if payload:
            session.permanent = True
            session['user_id'] = payload.get('user_id')
            session['email'] = payload.get('email')
            session['auth_time'] = time.time()


def _is_authenticated():
    """Check if current request has a valid session."""
    auth_time = session.get('auth_time')
    return bool(auth_time and (time.time() - auth_time) < SESSION_DURATION)


def _is_admin():
    """Check if current user is admin."""
    return _is_authenticated() and session.get('email') == db.ADMIN_EMAIL


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


HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>Strategy Analytics</title>
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
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }

        /* Header */
        .header {
            text-align: center;
            margin-bottom: 32px;
            animation: fadeDown 0.6s ease-out;
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
            grid-template-columns: repeat(5, 1fr);
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
            display: none; position: fixed; inset: 0; z-index: 1000;
            background: rgba(0,0,0,0.6); backdrop-filter: blur(4px);
            align-items: center; justify-content: center;
        }
        .asset-modal-overlay.open { display: flex; animation: fadeIn 0.15s ease-out; }
        .asset-modal {
            background: var(--bg-base); border: 1px solid var(--border);
            border-radius: 16px; padding: 20px 24px; width: 90%; max-width: 480px;
            box-shadow: 0 24px 48px rgba(0,0,0,0.4);
            animation: fadeUp 0.2s ease-out;
        }
        .asset-modal-header {
            display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 16px;
            font-size: 0.85em; font-weight: 600; color: var(--text);
        }
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
            letter-spacing: 0.02em; line-height: 1.2; white-space: nowrap;
            overflow: hidden; text-overflow: ellipsis; max-width: 100%;
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
        /* Chart */
        .chart-img {
            width: 100%;
            border-radius: 12px;
            border: 1px solid var(--border);
            animation: fadeUp 0.6s ease-out 0.3s both;
        }

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
        }
        .nav-link {
            padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500;
            color: var(--text-muted); text-decoration: none; transition: all 0.2s ease;
            border: 1px solid transparent;
        }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }

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
        .comment-body { font-size: 0.85em; color: var(--text-muted); line-height: 1.5; }
        .comment-actions { margin-top: 6px; display: flex; gap: 12px; }
        .comment-action-btn { background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 0.75em; font-family: 'DM Sans', sans-serif; }
        .comment-action-btn:hover { color: var(--text); }
        .comment-replies { margin-left: 24px; border-left: 2px solid var(--border); padding-left: 14px; }
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

        /* Animations */
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(16px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes fadeDown {
            from { opacity: 0; transform: translateY(-12px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .hidden { display: none !important; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="text-decoration:none;color:inherit;display:inline-flex;align-items:center;gap:0"><span class="brand-btc">Bitcoin</span><span class="brand-analytics">Strategy Analytics</span></a></h1>
        <div style="font-size:0.8em;color:var(--text-dim);margin-top:2px;font-family:'DM Sans',sans-serif">Exclusive to <a href="https://the-bitcoin-strategy.com" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:600">Premium Members</a> at the-bitcoin-strategy.com</div>
    </div>
    <nav class="nav-bar">
        <a href="/" class="nav-link {{ 'active' if nav_active|default('')=='featured' }}">Featured</a>
        <a href="/community" class="nav-link {{ 'active' if nav_active|default('')=='community' }}">Community</a>
        {% if session.get('user_id') %}
        <a href="/my-backtests" class="nav-link {{ 'active' if nav_active|default('')=='my-backtests' }}">My Backtests</a>
        {% endif %}
        <a href="/backtester" class="nav-link {{ 'active' if nav_active|default('')=='backtester' }}">Backtester</a>
    </nav>
    <div class="layout">
        <div class="panel">
            <form method="POST" id="form">
                <div class="form-section">
                    <div class="section-title">Mode</div>
                    <input type="hidden" name="mode" id="mode" value="{{ p.mode }}">
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
                </div>
                <div class="form-section">
                    <div class="section-title">Asset</div>
                    <input type="hidden" name="asset" id="asset" value="{{ p.asset }}">
                    <div class="asset-selected" id="asset-selected" onclick="openAssetModal()">
                        {% if asset_logos.get(p.asset) %}
                        <img class="asset-selected-logo" src="/static/logos/{{ asset_logos[p.asset] }}" alt="{{ p.asset }}">
                        {% else %}
                        <div class="asset-card-placeholder" style="width:36px;height:36px;font-size:0.75em">{{ p.asset[:3]|upper }}</div>
                        {% endif %}
                        <span class="asset-selected-name">{{ p.asset|capitalize if p.asset == p.asset|lower else p.asset }}</span>
                        <svg class="asset-selected-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </div>
                </div>
                <!-- Asset picker modal -->
                <div class="asset-modal-overlay" id="asset-modal-overlay" onclick="closeAssetModal()">
                    <div class="asset-modal" onclick="event.stopPropagation()">
                        <div class="asset-modal-header">
                            <span>Select Asset</span>
                            <button type="button" class="asset-modal-close" onclick="closeAssetModal()">&times;</button>
                        </div>
                        <div class="asset-grid">
                            {% for a in priority_assets %}
                            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" onclick="selectAsset('{{ a }}', this)">
                                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                                <span class="asset-card-label">{{ a|capitalize }}</span>
                            </div>
                            {% endfor %}
                            {% for a in other_assets %}
                            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" onclick="selectAsset('{{ a }}', this)">
                                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                                <span class="asset-card-label">{{ a|capitalize }}</span>
                            </div>
                            {% endfor %}
                        </div>
                        {% if stock_assets %}
                        <div class="asset-section-label">Stocks</div>
                        <div class="asset-grid">
                            {% for a in stock_assets %}
                            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" onclick="selectAsset('{{ a }}', this)">
                                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                                <span class="asset-card-label">{{ a }}</span>
                            </div>
                            {% endfor %}
                        </div>
                        {% endif %}
                        {% if metal_assets %}
                        <div class="asset-section-label">Precious Metals</div>
                        <div class="asset-grid">
                            {% for a in metal_assets %}
                            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" onclick="selectAsset('{{ a }}', this)">
                                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                                <span class="asset-card-label">{{ a }}</span>
                            </div>
                            {% endfor %}
                        </div>
                        {% endif %}
                        {% if index_assets %}
                        <div class="asset-section-label">Indices</div>
                        <div class="asset-grid">
                            {% for a in index_assets %}
                            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" onclick="selectAsset('{{ a }}', this)">
                                {% if asset_logos.get(a) %}<img class="asset-card-logo" src="/static/logos/{{ asset_logos[a] }}" alt="{{ a }}">{% else %}<div class="asset-card-placeholder">{{ a[:3]|upper }}</div>{% endif %}
                                <span class="asset-card-label">{{ a }}</span>
                            </div>
                            {% endfor %}
                        </div>
                        {% endif %}
                    </div>
                </div>
                <div class="form-section">
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
                    <label style="display:inline-flex;align-items:center;gap:6px;margin-top:6px;cursor:pointer;font-size:0.82em;color:var(--text-muted)">
                        <input type="checkbox" name="reverse" id="reverse" value="1" {{ 'checked' if p.reverse }} onchange="updateExplainer(); enableBtn();" style="accent-color:var(--accent)"> Reverse signal logic
                    </label>
                </div>
                <div class="form-section">
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
                            <tr><td class="m-label">R² <span class="m-info" data-tip="Coefficient of determination.&#10;How much variance in returns is explained by the oscillator.">ⓘ</span></td><td class="m-val">{{ "%.6f"|format(regression.r_squared) }}</td></tr>
                            <tr><td class="m-label">Pearson r <span class="m-info" data-tip="Linear correlation coefficient.&#10;Range: -1 to +1">ⓘ</span></td><td class="m-val">{{ "%.4f"|format(regression.pearson_r) }}</td></tr>
                            <tr><td class="m-label">Spearman ρ <span class="m-info" data-tip="Rank correlation coefficient.&#10;Captures monotonic (non-linear) relationships.">ⓘ</span></td><td class="m-val">{{ "%.4f"|format(regression.spearman_r) }}</td></tr>
                            <tr><td class="m-label">p-value <span class="m-info" data-tip="Statistical significance of the correlation.&#10;Lower = more significant. &lt; 0.05 is typical threshold.">ⓘ</span></td><td class="m-val">{{ "%.2e"|format(regression.p_value) }}</td></tr>
                            <tr class="section-row"><td colspan="2">Regression</td></tr>
                            <tr><td class="m-label">Slope <span class="m-info" data-tip="Change in forward return (%) per unit change in oscillator.">ⓘ</span></td><td class="m-val">{{ "%.4f"|format(regression.slope) }}</td></tr>
                            <tr><td class="m-label">Intercept</td><td class="m-val">{{ "%.2f"|format(regression.intercept) }}%</td></tr>
                            <tr><td class="m-label">Std Error</td><td class="m-val">{{ "%.4f"|format(regression.std_err) }}</td></tr>
                            <tr><td class="m-label">Data Points</td><td class="m-val">{{ "{:,}".format(regression.n_points) }}</td></tr>
                            </tbody>
                        </table>
                    </div>
                    <div>
                        <table class="metrics-table">
                            <thead><tr><th class="col-metric">Zone</th><th>Mean Return</th><th>Median</th><th>Count</th><th>Win Rate</th></tr></thead>
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
                {% if session.get('user_id') %}
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
                         style="height:600px;border-radius:12px;overflow:hidden;border:1px solid var(--border)">
                    </div>
                </div>
                <script>
                var __lwData = {
                    price: {{ price_json|safe }},
                    ind1: {{ ind1_json|safe }},
                    ind2: {{ ind2_json|safe }},
                    ind1Label: {{ ind1_label|tojson }},
                    ind2Label: {{ ind2_label|tojson }}
                };
                </script>
                {% endif %}
                {% if session.get('user_id') %}
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
                {% if best and not lev_sweep|default(none) %}
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
                    <tr><td class="m-label">Drawdown Duration <span class="m-info" data-tip="Longest time spent below a previous high.&#10;Days from peak until recovery">ⓘ</span></td><td class="m-val">{{ best.max_dd_duration }}d</td><td class="m-val">{{ best.buyhold_max_dd_duration }}d</td></tr>
                    <tr><td class="m-label">Volatility <span class="m-info" data-tip="How much returns fluctuate day to day.&#10;Formula: annualized std dev of daily returns">ⓘ</span></td><td class="m-val">{{ "%.1f"|format(best.volatility) }}%</td><td class="m-val">{{ "%.1f"|format(best.buyhold_volatility) }}%</td></tr>
                    <tr><td class="m-label">Beta <span class="m-info" data-tip="How much the strategy moves with the market.&#10;Formula: Cov(strategy, market) ÷ Var(market)">ⓘ</span></td><td class="m-val">{{ "%.2f"|format(best.beta) }}</td><td class="m-val muted">1.00</td></tr>
                    <tr><td class="m-label">Calmar Ratio <span class="m-info" data-tip="Return relative to worst drawdown.&#10;Formula: Ann. return ÷ |max drawdown|">ⓘ</span></td><td class="m-val">{{ "%.2f"|format(best.calmar) }}</td><td class="m-val">{{ "%.2f"|format(best.buyhold_calmar) }}</td></tr>
                    <tr class="section-row"><td colspan="3">Trades</td></tr>
                    <tr><td class="m-label">Trades <span class="m-info" data-tip="Number of position changes.&#10;Count of buy/sell signals">ⓘ</span></td><td class="m-val">{{ best.trades }}{% if ls %}<span class="m-ls"><span class="ls-l">Long {{ ls.long.trades }}</span><br><span class="ls-s">Short {{ ls.short.trades }}</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Win Rate <span class="m-info" data-tip="Percentage of trades that made money.&#10;Formula: winning trades ÷ total trades">ⓘ</span></td><td class="m-val">{{ "%.1f"|format(best.win_rate) }}%{% if ls %}<span class="m-ls"><span class="ls-l">Long {{ "%.1f"|format(ls.long.win_rate) }}%</span><br><span class="ls-s">Short {{ "%.1f"|format(ls.short.win_rate) }}%</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Avg Win / Loss <span class="m-info" data-tip="Average return of winning vs losing trades.&#10;Formula: mean return of wins / losses">ⓘ</span></td><td class="m-val"><span class="positive">+{{ "%.1f"|format(best.avg_win) }}%</span> / <span class="negative">{{ "%.1f"|format(best.avg_loss) }}%</span>{% if ls %}<span class="m-ls"><span class="ls-l">Long +{{ "%.1f"|format(ls.long.avg_win) }}% / {{ "%.1f"|format(ls.long.avg_loss) }}%</span><br><span class="ls-s">Short +{{ "%.1f"|format(ls.short.avg_win) }}% / {{ "%.1f"|format(ls.short.avg_loss) }}%</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Profit Factor <span class="m-info" data-tip="Gross profits divided by gross losses.&#10;Formula: sum of wins ÷ sum of losses">ⓘ</span></td><td class="m-val">{% if best.profit_factor > 9999 %}&infin;{% else %}{{ "%.2f"|format(best.profit_factor) }}{% endif %}{% if ls %}<span class="m-ls"><span class="ls-l">Long {% if ls.long.profit_factor > 9999 %}&infin;{% else %}{{ "%.2f"|format(ls.long.profit_factor) }}{% endif %}</span><br><span class="ls-s">Short {% if ls.short.profit_factor > 9999 %}&infin;{% else %}{{ "%.2f"|format(ls.short.profit_factor) }}{% endif %}</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Avg Duration <span class="m-info" data-tip="Average holding period per trade.&#10;Formula: total days in trades ÷ trade count">ⓘ</span></td><td class="m-val">{{ "%.0f"|format(best.avg_trade_duration) }}d{% if ls %}<span class="m-ls"><span class="ls-l">Long {{ "%.0f"|format(ls.long.avg_trade_duration) }}d</span><br><span class="ls-s">Short {{ "%.0f"|format(ls.short.avg_trade_duration) }}d</span></span>{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
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
<script>
var _swal = Swal.mixin({
    background: '#1e2130', color: '#e8e9ed', confirmButtonColor: '#6495ED',
    customClass: { popup: 'swal-dark' }
});
var assetStarts = {{ asset_starts_json|tojson }};
function selectMode(mode, el) {
    document.getElementById('mode').value = mode;
    var cards = document.querySelectorAll('.mode-card');
    for (var i = 0; i < cards.length; i++) cards[i].classList.remove('active');
    el.classList.add('active');
    toggleFields();
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

    // Show/hide oscillator param row and description
    var oscParamsRow = document.getElementById('osc-params-row');
    var oscDesc = document.getElementById('osc-description');
    if (isOsc || isRegression) {
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

    var rules = [
        ['ind1-group', !isOsc && !isRegression],
        ['period1-group', !isOsc && !isRegression && ind1 !== 'price' && mode !== 'heatmap'],
        ['ind-sep', !isOsc && !isRegression],
        ['period2-group', !isOsc && !isRegression && (mode === 'backtest' || mode === 'sweep-lev')],
        ['range-min-group', !isOsc && !isRegression && (mode === 'sweep' || mode === 'heatmap')],
        ['range-max-group', !isOsc && !isRegression && (mode === 'sweep' || mode === 'heatmap')],
        ['step-group', !isOsc && !isRegression && mode === 'heatmap'],
        ['long-lev-group', !isLevSweep && !isRegression],
        ['short-lev-group', !isLevSweep && !isRegression],
        ['exposure-group', !isLevSweep && !isRegression],
        ['lev-mode-group', !isRegression],
        ['sizing-group', !isRegression],
        ['lev-min-group', isLevSweep && !isRegression],
        ['lev-max-group', isLevSweep && !isRegression],
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
    var label1 = ind1.value === 'price' ? 'Price' : ind1.value.toUpperCase() + (p1.value ? '(' + p1.value + ')' : '');
    var label2 = ind2Val.toUpperCase() + (p2.value ? '(' + p2.value + ')' : '');
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
function setAllData() {
    var asset = document.getElementById('asset').value;
    document.getElementById('start_date').value = assetStarts[asset] || '';
}
function onAssetChange() {
    var asset = document.getElementById('asset').value;
    var startInput = document.getElementById('start_date');
    var assetStart = assetStarts[asset];
    if (assetStart) {
        startInput.value = assetStart;
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
}
function closeAssetModal() {
    document.getElementById('asset-modal-overlay').classList.remove('open');
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
var lwChartLoaded = false;
function switchChartTab(tab, btn) {
    var bt = document.getElementById('backtest-chart-tab');
    var lw = document.getElementById('livechart-tab');
    if (!bt || !lw) return;
    bt.style.display = tab === 'backtest' ? '' : 'none';
    lw.style.display = tab === 'livechart' ? '' : 'none';
    var tabs = btn.parentElement.querySelectorAll('.chart-tab');
    for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
    btn.classList.add('active');
    if (tab === 'livechart' && !lwChartLoaded) {
        loadLWChart();
    }
    // Update URL with view parameter
    var url = new URL(window.location);
    if (tab === 'livechart') {
        url.searchParams.set('view', 'livechart');
    } else {
        url.searchParams.delete('view');
    }
    history.replaceState(null, '', url.toString());
}
function activateViewFromURL() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('view') === 'livechart') {
        var tabs = document.querySelectorAll('.chart-tab');
        if (tabs.length >= 2) switchChartTab('livechart', tabs[1]);
    }
}
function downloadChart() {
    var img = document.getElementById('backtest-chart-img');
    if (!img) return;
    var asset = document.getElementById('asset');
    var assetName = asset ? asset.value : 'chart';
    var a = document.createElement('a');
    a.href = img.src;
    a.download = assetName + '_backtest.png';
    a.click();
}
function loadLWChart() {
    var container = document.getElementById('lw-chart-container');
    if (!container || typeof __lwData === 'undefined') return;
    lwChartLoaded = true;
    container.innerHTML = '';

    var priceData = __lwData.price || [];
    var ind1Data = __lwData.ind1 || [];
    var ind2Data = __lwData.ind2 || [];
    var ind1Label = __lwData.ind1Label || '';
    var ind2Label = __lwData.ind2Label || '';

    if (priceData.length === 0) return;

    var chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 600,
        layout: {
            background: { color: '#161922' },
            textColor: '#8890a4',
            fontFamily: "'DM Sans', sans-serif"
        },
        grid: {
            vertLines: { color: '#252a3a' },
            horzLines: { color: '#252a3a' }
        },
        rightPriceScale: {
            mode: LightweightCharts.PriceScaleMode.Logarithmic,
            borderColor: '#252a3a'
        },
        timeScale: {
            borderColor: '#252a3a',
            timeVisible: false
        },
        crosshair: {
            horzLine: { color: '#555d74', labelBackgroundColor: '#252a3a' },
            vertLine: { color: '#555d74', labelBackgroundColor: '#252a3a' }
        }
    });

    var priceSeries = chart.addSeries(LightweightCharts.LineSeries, {
        color: '#e8eaf0',
        lineWidth: 2,
        title: 'Price',
        priceLineVisible: false
    });
    priceSeries.setData(priceData);

    if (ind2Data.length > 0) {
        var ind2Series = chart.addSeries(LightweightCharts.LineSeries, {
            color: '#6495ED',
            lineWidth: 2,
            title: ind2Label,
            priceLineVisible: false
        });
        ind2Series.setData(ind2Data);
    }

    if (ind1Data.length > 0) {
        var ind1Series = chart.addSeries(LightweightCharts.LineSeries, {
            color: '#f7931a',
            lineWidth: 2,
            title: ind1Label,
            priceLineVisible: false
        });
        ind1Series.setData(ind1Data);
    }

    // Default zoom: show last 12 months
    if (priceData.length > 0) {
        var lastPoint = priceData[priceData.length - 1];
        var lastDate = new Date(lastPoint.time);
        var fromDate = new Date(lastDate);
        fromDate.setFullYear(fromDate.getFullYear() - 1);
        var fromStr = fromDate.toISOString().split('T')[0];
        chart.timeScale().setVisibleRange({
            from: fromStr,
            to: lastPoint.time
        });
    } else {
        chart.timeScale().fitContent();
    }

    window.addEventListener('resize', function() {
        chart.applyOptions({ width: container.clientWidth });
    });
}

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
                panel.innerHTML = '<div class="placeholder">Stopped</div>';
            } else if (err.message !== 'redirect') {
                panel.innerHTML = '<div class="placeholder">Error: ' + err.message + '</div>';
            }
            resetBtn();
        });
});

// Initial load on first visit or when opened via shareable URL
{% if not chart %}
(function() {
    var btn = document.getElementById('btn');
    var panel = document.getElementById('results-panel');
    currentAbort = new AbortController();
    btn.textContent = 'Stop';
    btn.classList.add('btn-stop');
    var formData = new FormData(document.getElementById('form'));
    fetch('/backtester', { method: 'POST', body: formData, signal: currentAbort.signal })
        .then(function(resp) {
            if (resp.redirected) { window.location.href = resp.url; throw new Error('redirect'); }
            return resp.text();
        })
        .then(function(html) {
            var doc = new DOMParser().parseFromString(html, 'text/html');
            var newPanel = doc.getElementById('results-panel');
            if (newPanel) {
                panel.innerHTML = newPanel.innerHTML;
                var scripts = panel.querySelectorAll('script');
                for (var si = 0; si < scripts.length; si++) {
                    var ns = document.createElement('script');
                    ns.textContent = scripts[si].textContent;
                    scripts[si].replaceWith(ns);
                }
                lwChartLoaded = false;
                var img = panel.querySelector('.chart-img');
                if (img) { img.style.animation = 'fadeUp 0.5s ease-out both'; }
            } else {
                panel.innerHTML = '<div class="placeholder">Error loading results. Try refreshing the page.</div>';
            }
            var qs = new URLSearchParams(formData);
            var viewParam = new URLSearchParams(window.location.search).get('view');
            if (viewParam) qs.set('view', viewParam);
            history.replaceState(null, '', '?' + qs.toString());
            activateViewFromURL();
            resetBtn();
        })
        .catch(function(err) {
            if (err.name === 'AbortError') {
                panel.innerHTML = '<div class="placeholder">Stopped</div>';
            } else if (err.message !== 'redirect') {
                panel.innerHTML = '<div class="placeholder">Error loading results. Try refreshing the page.</div>';
            }
            resetBtn();
        });
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

function saveBacktest() {
    var btn = document.getElementById('save-btn');
    btn.textContent = 'Saving...';
    btn.disabled = true;
    var formData = new FormData(document.getElementById('form'));
    var params = {};
    formData.forEach(function(v, k) { params[k] = v; });
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
        if (data.liked) { btn.classList.add('liked'); } else { btn.classList.remove('liked'); }
    });
}

function submitComment(backtestId, parentId) {
    var textareaId = parentId ? 'reply-' + parentId : 'comment-body';
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
</body>
</html>
"""


class Params:
    """Hold form parameters with defaults."""
    def __init__(self, form=None):
        if form:
            self.asset = form.get("asset", DEFAULT_ASSET)
            self.mode = form.get("mode", "sweep")
            self.signal_type = form.get("signal_type", "crossover")
            self.ind1_name = form.get("ind1_name", "price")
            p1_val = form.get("period1", "").strip()
            self.ind1_period = int(p1_val) if p1_val else None
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
            # Oscillator params
            self.osc_name = form.get("osc_name", "rsi")
            osc_p = form.get("osc_period", "").strip()
            self.osc_period = int(osc_p) if osc_p else None
            self.buy_threshold = float(form.get("buy_threshold", bt.OSCILLATORS.get(self.osc_name, {}).get("buy_threshold", 30)))
            self.sell_threshold = float(form.get("sell_threshold", bt.OSCILLATORS.get(self.osc_name, {}).get("sell_threshold", 70)))
            self.forward_days = int(form.get("forward_days", 365))
        else:
            self.asset = DEFAULT_ASSET
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
            # Oscillator defaults
            self.osc_name = "rsi"
            self.osc_period = None
            self.buy_threshold = 30
            self.sell_threshold = 70
            self.forward_days = 365


# Load data once at startup
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

ASSETS = {}
ASSET_STARTS = {}
for _fname in sorted(os.listdir(DATA_DIR)):
    if _fname.endswith(".csv"):
        _name = _fname.replace(".csv", "")
        _df = bt.load_data(os.path.join(DATA_DIR, _fname))
        ASSETS[_name] = _df
        ASSET_STARTS[_name] = str(_df.index[0].date())
ASSET_NAMES = sorted(ASSETS.keys())
_PRIORITY_ORDER = ["bitcoin", "ethereum", "solana"]
_STOCK_ASSETS = {"Apple", "Microsoft", "Amazon", "Alphabet", "Tesla", "Nvidia", "Meta", "Netflix", "Coinbase", "Strategy"}
_INDEX_ASSETS = {"Dax", "Dow Jones", "Hang Seng", "Nasdaq100", "SP500"}
_METAL_ASSETS = {"Gold", "Silver", "Palladium"}
PRIORITY_ASSETS = [a for a in _PRIORITY_ORDER if a in ASSETS]
OTHER_ASSETS = [a for a in ASSET_NAMES if a not in _PRIORITY_ORDER and a not in _STOCK_ASSETS and a not in _INDEX_ASSETS and a not in _METAL_ASSETS]
STOCK_ASSETS = [a for a in ASSET_NAMES if a in _STOCK_ASSETS]
INDEX_ASSETS = [a for a in ASSET_NAMES if a in _INDEX_ASSETS]
METAL_ASSETS = [a for a in ASSET_NAMES if a in _METAL_ASSETS]
DEFAULT_ASSET = "bitcoin" if "bitcoin" in ASSETS else ASSET_NAMES[0]
ASSET_LOGOS = {
    "bitcoin": "bitcoin-btc-logo.png", "ethereum": "ethereum-eth-logo.png",
    "solana": "solana-sol-logo.png", "XRP": "xrp-xrp-logo.png",
    "BNB": "bnb-bnb-logo.png", "Cardano": "cardano-ada-logo.png",
    "Chainlink": "chainlink-link-logo.png", "Dogecoin": "dogecoin-doge-logo.png",
    "Monero": "monero-xmr-logo.png", "Bitcoin Cash": "bitcoin-cash-bch-logo.png",
    "Hyperliquid": "hyperliquid-logo.png",
    "Dax": "dax-logo.svg", "Dow Jones": "dowjones-logo.svg",
    "Hang Seng": "hangseng-logo.svg", "Nasdaq100": "nasdaq-logo.svg",
    "SP500": "sp500-logo.svg",
    "Gold": "gold-logo.svg", "Silver": "silver-logo.svg", "Palladium": "palladium-logo.svg",
    "Apple": "apple-logo.png", "Microsoft": "microsoft-logo.png", "Amazon": "amazon-logo.png",
    "Alphabet": "alphabet-logo.png", "Tesla": "tesla-logo.png", "Nvidia": "nvidia-logo.png",
    "Meta": "meta-logo.png", "Netflix": "netflix-logo.png", "Coinbase": "coinbase-logo.png",
    "Strategy": "strategy-logo.png",
}

def _series_to_lw_json(series):
    """Convert pandas Series (datetime index + float values) to Lightweight Charts format."""
    return json.dumps([
        {"time": str(idx.date()), "value": round(float(val), 2)}
        for idx, val in series.dropna().items()
    ])


def _enrich_best(result, df):
    """Add annualized return and buy-and-hold metrics to a result dict."""
    import numpy as np
    import pandas as pd_mod
    n_days = len(df)
    result["annualized"] = bt._annualized_return(result["total_return"], n_days)
    result["buyhold_annualized"] = bt._annualized_return(result["buyhold_return"], n_days)
    result["buyhold_max_drawdown"] = bt._max_drawdown(result["buyhold"])
    daily_return = df["close"].pct_change().fillna(0)
    mean_d = daily_return.mean()
    std_d = daily_return.std()
    result["buyhold_sharpe"] = (mean_d / std_d * np.sqrt(365)) if std_d > 0 else 0.0
    # Buy-and-hold additional metrics
    bh_returns = pd_mod.Series(result["buyhold"].values).pct_change().fillna(0)
    result["buyhold_volatility"] = std_d * np.sqrt(365) * 100
    result["buyhold_sortino"] = bt._sortino_ratio(bh_returns)
    result["buyhold_calmar"] = abs(result["buyhold_annualized"] / result["buyhold_max_drawdown"]) if result["buyhold_max_drawdown"] != 0 else 0.0
    result["buyhold_max_dd_duration"] = bt._max_drawdown_duration(result["buyhold"])
    bh_yearly = bt._yearly_returns(result["buyhold"])
    result["buyhold_best_year"] = max(bh_yearly.items(), key=lambda x: x[1]) if bh_yearly else (None, 0)
    result["buyhold_worst_year"] = min(bh_yearly.items(), key=lambda x: x[1]) if bh_yearly else (None, 0)
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
        if any(k in request.args for k in ('asset', 'mode', 'ind1_name', 'ind2_name', 'period1', 'period2', 'exposure', 'reverse')):
            p = Params(request.args)
        else:
            p = Params()
        return render_template_string(HTML, p=p, nav_active='backtester', chart=None, best=None, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, index_assets=INDEX_ASSETS, metal_assets=METAL_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
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
    is_oscillator = p.signal_type == "oscillator"
    import pandas as pd_mod
    df_full = ASSETS.get(p.asset, ASSETS[DEFAULT_ASSET]).copy()
    if p.end_date:
        df_full = df_full[df_full.index <= pd_mod.Timestamp(p.end_date, tz="UTC")]
    if not p.start_date:
        p.start_date = str(df_full.index[0].date())
    if not p.end_date:
        p.end_date = str(df_full.index[-1].date())
    warmup_start_date = p.start_date
    # df_full = full data for indicator warmup (passed to strategy functions)
    # df = trimmed to start_date for display (charts, n_days, buy-and-hold)
    df = df_full[df_full.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]

    fee = p.fee / 100

    # Oscillator mode forces backtest (no sweep/heatmap/lev-sweep support), except regression
    if is_oscillator and p.mode not in ("backtest", "regression"):
        p.mode = "backtest"

    # --- Regression Analysis Mode ---
    if p.mode == "regression":
        if not is_oscillator:
            return render_template_string(HTML, p=p, nav_active='backtester', chart=None, best=None, table_rows=None, col_header=col_header,
                                          asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, index_assets=INDEX_ASSETS, metal_assets=METAL_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
                                          error="Regression analysis requires an oscillator indicator. Please select one from Indicator 2.",
                                          price_json=None, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")

        reg_result = bt.run_regression_analysis(df, p.osc_name, p.osc_period, p.forward_days,
                                                 p.buy_threshold, p.sell_threshold)
        chart_b64 = bt.generate_regression_chart(reg_result)

        sweep_result = bt.sweep_regression_r_squared(df, p.osc_name, p.osc_period,
                                                      p.buy_threshold, p.sell_threshold)
        sweep_chart_b64 = bt.generate_regression_sweep_chart(sweep_result)

        # Generate small regression thumbnail (scatter plot)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax])
        osc_vals = reg_result["osc_values"]
        fwd_rets = reg_result["forward_returns"]
        thumb_ax.scatter(osc_vals, fwd_rets, c="#8890a4", alpha=0.15, s=3, rasterized=True)
        x_range = np.linspace(osc_vals.min(), osc_vals.max(), 100)
        y_pred = reg_result["slope"] * x_range + reg_result["intercept"]
        thumb_ax.plot(x_range, y_pred, color="#f7931a", linewidth=1.5)
        thumb_ax.axhline(y=0, color="#8890a4", linestyle="--", linewidth=0.5, alpha=0.5)
        thumb_ax.grid(True, which="major", alpha=0.3, color="#252a3a")
        thumb_ax.tick_params(labelsize=7)
        thumb_ax.set_xlabel("")
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        return render_template_string(HTML, p=p, nav_active='backtester', chart=chart_b64, best=None, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, index_assets=INDEX_ASSETS, metal_assets=METAL_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
                                      regression=reg_result, regression_sweep_chart=sweep_chart_b64, regression_sweep=sweep_result, thumb_b64=thumb_b64,
                                      price_json=None, ind1_json="[]", ind2_json="[]", ind1_label="", ind2_label="")

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
            if p.sizing == "fixed":
                leverage = np.where(position_base.values > 0, ll,
                           np.where(position_base.values < 0, sl, 1))
                daily_pnl = p.initial_cash * position_base.values * daily_return.values * leverage
                daily_pnl = daily_pnl.copy()
                trade_changes = np.diff(position_base.values, prepend=0)
                daily_pnl[np.abs(trade_changes) > 0] -= p.initial_cash * fee
                equity_arr = p.initial_cash + np.cumsum(daily_pnl)
            elif p.lev_mode == "set-forget":
                equity_arr, _ = bt._compute_equity_set_and_forget(
                    position_base.values, daily_return.values, p.initial_cash, ll, sl, fee)
            elif p.lev_mode == "optimal":
                equity_arr, _ = bt._compute_equity_optimal(
                    position_base.values, daily_return.values, p.initial_cash, ll, sl, fee)
            else:
                leverage = np.where(position_base.values > 0, ll,
                           np.where(position_base.values < 0, sl, 1))
                strat_ret = position_base.values * daily_return.values * leverage
                strat_ret = strat_ret.copy()
                trade_changes = np.diff(position_base.values, prepend=0)
                strat_ret[np.abs(trade_changes) > 0] -= fee
                equity_arr, _ = bt._compute_equity_with_liquidation(strat_ret, p.initial_cash)
            equity_final = equity_arr[-1] if len(equity_arr) > 0 else p.initial_cash
            total_ret = (equity_final / p.initial_cash - 1) * 100
            return bt._annualized_return(total_ret, n_days)

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
        bh_ann = bt._annualized_return(bh_total, n_days)

        asset_title = p.asset.capitalize()
        fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
        bt._apply_dark_theme(fig, ax)
        show_long = p.exposure in ("long-cash", "long-short")
        show_short = p.exposure in ("short-cash", "long-short")
        all_levs = []
        if show_long:
            ax.plot(long_levs, long_sweep, color="#6495ED", linewidth=1.5, label="Long Leverage")
            ax.scatter([best_long_lev], [best_long_ann], color="#6495ED", s=60, zorder=5)
            all_levs.extend(long_levs)
        if show_short:
            ax.plot(short_levs, short_sweep, color="#f7931a", linewidth=1.5, label="Short Leverage")
            ax.scatter([best_short_lev], [best_short_ann], color="#f7931a", s=60, zorder=5)
            all_levs.extend(short_levs)
        x_min, x_max = min(all_levs), max(all_levs)
        if p.exposure != "short-cash":
            ax.plot([x_min, x_max], [bh_ann, bh_ann], color="#8890a4", linestyle="--", linewidth=1,
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
        ax.set_title(f"{asset_title} {title_label} \u2014 Leverage Sweep | {p.exposure}\n"
                     f"{' | '.join(title_parts)}")
        ax.legend(loc="best", fontsize=9, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
        ax.grid(True, alpha=0.3, color="#252a3a")
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        # Generate small leverage-sweep thumbnail
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax])
        if show_long:
            thumb_ax.plot(long_levs, long_sweep, color="#6495ED", linewidth=1.5)
            thumb_ax.scatter([best_long_lev], [best_long_ann], color="#6495ED", s=40, zorder=5)
        if show_short:
            thumb_ax.plot(short_levs, short_sweep, color="#f7931a", linewidth=1.5)
            thumb_ax.scatter([best_short_lev], [best_short_ann], color="#f7931a", s=40, zorder=5)
        if p.exposure != "short-cash":
            thumb_ax.axhline(y=bh_ann, color="#8890a4", linestyle="--", linewidth=1, alpha=0.7)
        thumb_ax.set_xlabel("")
        thumb_ax.grid(True, which="major", alpha=0.3, color="#252a3a")
        thumb_ax.tick_params(labelsize=7)
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        best_result = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, ind2_period_val,
                                       p.initial_cash, fee, p.exposure, best_long_lev, best_short_lev, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
        best = _enrich_best(best_result, df)

        combined_ann = _sweep_ann(best_long_lev, best_short_lev)
        lev_sweep_info = {
            "best_long_lev": best_long_lev,
            "best_long_ann": best_long_ann,
            "best_short_lev": best_short_lev,
            "best_short_ann": best_short_ann,
            "combined_ann": combined_ann,
            "combined_label": f"{title_label} with long {best_long_lev:.2f}x / short {best_short_lev:.2f}x",
        }
        price_json = _series_to_lw_json(df["close"])
        ind1_json = _series_to_lw_json(best["ind1_series"]) if best.get("ind1_name") != "price" else "[]"
        ind2_json = _series_to_lw_json(best["ind2_series"])
        return render_template_string(HTML, p=p, nav_active='backtester', chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, index_assets=INDEX_ASSETS, metal_assets=METAL_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
                                      hide_buyhold=(p.exposure == "short-cash"), lev_sweep=lev_sweep_info, thumb_b64=thumb_b64,
                                      price_json=price_json, ind1_json=ind1_json, ind2_json=ind2_json,
                                      ind1_label=best.get("ind1_label", ""), ind2_label=best.get("ind2_label", ""))

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
                ann = bt._annualized_return(total_ret, n_days)
                matrix[i, j] = ann
                if ann > best_ann:
                    best_ann = ann
                    best_p1 = p1
                    best_p2 = p2

        # df is already trimmed to start_date; use it for buy-and-hold and chart display
        bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_ann = bt._annualized_return(bh_total, n_days)

        fig, ax = plt.subplots(figsize=(14, 12), dpi=150)
        bt._apply_dark_theme(fig, ax)
        im = ax.imshow(matrix, cmap="RdYlGn", aspect="auto", origin="lower",
                       interpolation="nearest")
        ax.set_xticks(range(n))
        ax.set_xticklabels(periods, rotation=90, fontsize=max(4, min(8, 200 // n)))
        ax.set_yticks(range(n))
        ax.set_yticklabels(periods, fontsize=max(4, min(8, 200 // n)))

        if same_type:
            ax.set_xlabel(f"Slow {ind1_upper} Period")
            ax.set_ylabel(f"Fast {ind1_upper} Period")
        else:
            ax.set_xlabel(f"{ind2_upper} Period")
            ax.set_ylabel(f"{ind1_upper} Period")

        asset_title = p.asset.capitalize()
        ax.set_title(f"{asset_title} {ind1_upper}/{ind2_upper} Crossover \u2014 Annualized Return % (step={p.step})\n"
                     f"Best: {ind1_upper}({best_p1})/{ind2_upper}({best_p2}) = {best_ann:.1f}% | "
                     f"B&H: {bh_ann:.1f}% | {p.exposure}")
        cbar = fig.colorbar(im, ax=ax, shrink=0.8)
        cbar.set_label("Annualized Return (%)", color="#8890a4")
        cbar.ax.yaxis.set_tick_params(color="#8890a4")
        cbar.outline.set_edgecolor("#2a2d3a")
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
        bt._apply_dark_theme(thumb_fig, [thumb_ax])
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
                                       p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
        best = _enrich_best(best_result, df)

        price_json = _series_to_lw_json(df["close"])
        ind1_json = _series_to_lw_json(best["ind1_series"]) if best.get("ind1_name") != "price" else "[]"
        ind2_json = _series_to_lw_json(best["ind2_series"])
        return render_template_string(HTML, p=p, nav_active='backtester', chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, index_assets=INDEX_ASSETS, metal_assets=METAL_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
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
                                      p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
            ann = bt._annualized_return(result["total_return"], n_days)
            annualized_returns.append(ann)

        bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_annualized = bt._annualized_return(bh_total, n_days)
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
        bt._apply_dark_theme(fig, ax)
        ax.plot(periods, annualized_returns, color="#6495ED", linewidth=1)
        if p.exposure != "short-cash":
            ax.axhline(y=bh_annualized, color="#8890a4", linestyle="--", linewidth=1,
                        label=f"Buy & Hold ({bh_annualized:.1f}%)")
        ax.scatter([best_period], [best_ann], color="#f7931a", s=60, zorder=5,
                    label=f"Best: {best_label} ({best_ann:.1f}%)")
        ax.set_xlabel(f"{ind2_upper} Period (days)")
        ax.set_ylabel("Annualized Return (%)")
        asset_title = p.asset.capitalize()
        title_prefix = f"{ind1_label_str} vs " if p.ind1_name != "price" else ""
        ax.set_title(f"{asset_title} \u2014 Annualized Return by {title_prefix}{ind2_upper} Period ({p.range_min}-{p.range_max}) | {p.exposure}")
        ax.legend(loc="best", fontsize=9, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
        ax.grid(True, alpha=0.3, color="#252a3a")
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        # Generate small sweep thumbnail
        thumb_fig, thumb_ax = plt.subplots(1, 1, figsize=(6, 2.5), dpi=100)
        bt._apply_dark_theme(thumb_fig, [thumb_ax])
        thumb_ax.plot(periods, annualized_returns, color="#6495ED", linewidth=1.5)
        thumb_ax.scatter([best_period], [best_ann], color="#f7931a", s=40, zorder=5)
        if p.exposure != "short-cash":
            thumb_ax.axhline(y=bh_annualized, color="#8890a4", linestyle="--", linewidth=1, alpha=0.7)
        thumb_ax.grid(True, which="major", alpha=0.3, color="#252a3a")
        thumb_ax.tick_params(labelsize=7)
        thumb_ax.set_xlabel("")
        plt.tight_layout()
        thumb_buf = BytesIO()
        plt.savefig(thumb_buf, format="png", facecolor=thumb_fig.get_facecolor())
        plt.close()
        thumb_buf.seek(0)
        thumb_b64 = "data:image/png;base64," + base64.b64encode(thumb_buf.read()).decode()

        best_result = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, best_period,
                                       p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
        best = _enrich_best(best_result, df)

    # --- Backtest Mode ---
    else:

        if is_oscillator:
            # Oscillator strategy
            result = bt.run_oscillator_strategy(df_full, p.osc_name, p.osc_period, p.buy_threshold, p.sell_threshold,
                                                 p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
            results = [result]
        elif p.ind2_period is not None:
            # Single run with fixed period
            result = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
                                      p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
            results = [result]
        else:
            # Sweep ind2 period and show table
            results = bt.sweep_periods(df_full, p.ind1_name, p.ind1_period, p.ind2_name, None,
                                        "ind2", p.range_min, p.range_max,
                                        p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode,
                                        sizing=p.sizing, start_date=warmup_start_date)
            # For same-type crossover, filter invalid combos
            if p.ind1_name != "price" and p.ind1_name == p.ind2_name and p.ind1_period is not None:
                results = [r for r in results if r["ind2_period"] > p.ind1_period]
                results.sort(key=lambda r: r["total_return"], reverse=True)

        if results:
            best = _enrich_best(results[0], df)
            if len(results) > 1:
                table_rows = [{"label": r["label"], **r} for r in results]

            # Compute long/short breakdown for long-short exposure
            long_short_breakdown = None
            if not is_oscillator and p.exposure == "long-short" and p.ind2_period is not None:
                long_only = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
                                             p.initial_cash, fee, "long-cash", p.long_leverage, 1, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
                short_only = bt.run_strategy(df_full, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
                                              p.initial_cash, fee, "short-cash", 1, p.short_leverage, p.lev_mode, p.reverse, p.sizing, start_date=warmup_start_date)
                long_only = _enrich_best(long_only, df)
                short_only = _enrich_best(short_only, df)
                long_short_breakdown = {"long": long_only, "short": short_only}

            # Generate chart
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            import numpy as np

            asset_name = p.asset.capitalize()
            show_ratio = p.exposure != "short-cash" and p.sizing != "fixed"

            if is_oscillator:
                # Oscillator chart: price panel + oscillator panel + equity panel
                if show_ratio:
                    fig, (ax1, ax_osc, ax2, ax3) = plt.subplots(4, 1, figsize=(14, 16), dpi=150,
                                                                  gridspec_kw={"height_ratios": [4, 2, 2.5, 2.5]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax_osc, ax2, ax3])
                    equity_top = (4 + 2) / (4 + 2 + 2.5 + 2.5)
                    equity_bottom = (4 + 2 + 2.5) / (4 + 2 + 2.5 + 2.5)
                else:
                    fig, (ax1, ax_osc, ax2) = plt.subplots(3, 1, figsize=(14, 13), dpi=150,
                                                             gridspec_kw={"height_ratios": [5, 2, 3]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax_osc, ax2])
                    equity_top = (5 + 2) / (5 + 2 + 3)
                    equity_bottom = 1.0
            else:
                if show_ratio:
                    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 13), dpi=150,
                                                         gridspec_kw={"height_ratios": [5, 2.5, 2.5]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax2, ax3])
                    equity_top = 5 / (5 + 2.5 + 2.5)
                    equity_bottom = (5 + 2.5) / (5 + 2.5 + 2.5)
                else:
                    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), dpi=150,
                                                    gridspec_kw={"height_ratios": [7, 3]}, sharex=True)
                    bt._apply_dark_theme(fig, [ax1, ax2])
                    equity_top = 7 / (7 + 3)
                    equity_bottom = 1.0

            ax1.plot(df.index, df["close"], label=f"{asset_name} Price", color="#e8eaf0", linewidth=0.8)

            if not is_oscillator:
                # Plot ind2 (main/slow indicator)
                ax1.plot(best["ind2_series"].index, best["ind2_series"],
                         label=best["ind2_label"], color="#6495ED", linewidth=0.8, alpha=0.8)
                # Plot ind1 if not price
                if best.get("ind1_name") != "price":
                    ax1.plot(best["ind1_series"].index, best["ind1_series"],
                             label=best["ind1_label"], color="#f7931a", linewidth=0.8, alpha=0.8)

            ax1.set_yscale("log")
            _fmt_usd = plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}")
            ax1.yaxis.set_major_formatter(_fmt_usd)
            ax1.yaxis.set_minor_formatter(_minor_usd_formatter())
            ax1.tick_params(axis='y', which='minor', labelsize=6)
            ax1.set_ylabel(f"{asset_name} Price (log scale)")
            ax1.set_title(f"{asset_name} Backtest \u2014 {best['label']} "
                          f"({best['total_return']:.1f}% return) | {p.exposure}")
            ax1.legend(loc="upper left", fontsize=8, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
            ax1.grid(True, which="major", alpha=0.3, color="#252a3a")
            ax1.grid(True, which="minor", alpha=0.15, color="#252a3a")

            # --- Oscillator panel ---
            if is_oscillator:
                osc_data = best.get("osc_data")
                osc_spec = osc_data["spec"]
                osc_colors = ["#6495ED", "#f7931a", "#34d399"]

                if p.osc_name == "macd":
                    # MACD: line + signal + histogram bars
                    macd_s = osc_data["series"]["MACD"]
                    sig_s = osc_data["series"]["Signal"]
                    hist_s = osc_data["series"]["Histogram"]
                    ax_osc.plot(macd_s.index, macd_s, color="#6495ED", linewidth=0.9, label="MACD")
                    ax_osc.plot(sig_s.index, sig_s, color="#f7931a", linewidth=0.9, label="Signal")
                    # Histogram as bars
                    pos_hist = hist_s.where(hist_s >= 0, 0)
                    neg_hist = hist_s.where(hist_s < 0, 0)
                    ax_osc.fill_between(hist_s.index, 0, pos_hist, alpha=0.3, color="#34d399", step="mid")
                    ax_osc.fill_between(hist_s.index, 0, neg_hist, alpha=0.3, color="#ef4444", step="mid")
                    ax_osc.axhline(y=0, color="#8890a4", linestyle="--", linewidth=0.6, alpha=0.5)
                else:
                    # Single or dual line oscillators
                    for idx, (line_name, line_series) in enumerate(osc_data["series"].items()):
                        ax_osc.plot(line_series.index, line_series, color=osc_colors[idx % len(osc_colors)],
                                    linewidth=0.9, label=line_name)

                    # Draw threshold lines
                    ax_osc.axhline(y=p.buy_threshold, color="#34d399", linestyle="--", linewidth=0.7, alpha=0.7,
                                   label=f"Buy ({p.buy_threshold})")
                    ax_osc.axhline(y=p.sell_threshold, color="#ef4444", linestyle="--", linewidth=0.7, alpha=0.7,
                                   label=f"Sell ({p.sell_threshold})")

                    # Shade overbought/oversold zones
                    osc_range = osc_spec.get("range")
                    if osc_range:
                        ax_osc.fill_between(df.index, osc_range[0], p.buy_threshold, alpha=0.04, color="#34d399")
                        ax_osc.fill_between(df.index, p.sell_threshold, osc_range[1], alpha=0.04, color="#ef4444")
                    else:
                        # For unbounded oscillators, just shade between threshold lines
                        ax_osc.axhline(y=0, color="#8890a4", linestyle="--", linewidth=0.5, alpha=0.3)

                # Set y-limits for bounded oscillators
                osc_range = osc_spec.get("range")
                if osc_range:
                    ax_osc.set_ylim(osc_range[0] - 2, osc_range[1] + 2)

                ax_osc.set_ylabel(osc_data["label"])
                ax_osc.legend(loc="upper left", fontsize=7, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0", ncol=3)
                ax_osc.grid(True, which="major", alpha=0.3, color="#252a3a")

            ax2.plot(best["equity"].index, best["equity"], label="Strategy Equity", color="#6495ED", linewidth=1)
            if show_ratio:
                ax2.plot(best["buyhold"].index, best["buyhold"], label="Buy & Hold", color="#8890a4", linewidth=1, alpha=0.7)
            if p.sizing == "fixed":
                ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if abs(x) < 1 else f"${x:,.0f}"))
                ax2.axhline(y=p.initial_cash, color="#8890a4", linestyle="--", linewidth=0.8, alpha=0.5)
                ax2.set_ylabel("Portfolio Value (linear, fixed sizing)")
            else:
                ax2.set_yscale("log")
                ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
                ax2.yaxis.set_minor_formatter(_minor_usd_formatter())
                ax2.tick_params(axis='y', which='minor', labelsize=6)
                ax2.set_ylabel("Portfolio Value (log)")
            ax2.legend(loc="upper left", fontsize=8, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
            ax2.grid(True, which="major", alpha=0.3, color="#252a3a")
            ax2.grid(True, which="minor", alpha=0.15, color="#252a3a")

            last_ax = ax2
            if show_ratio:
                ratio = best["equity"] / best["buyhold"].replace(0, np.nan)
                ratio_normalized = ratio / ratio.dropna().iloc[0] * 100
                ax3.plot(ratio_normalized.index, ratio_normalized, color="#a78bfa", linewidth=1, label=f"Strategy in {asset_name}")
                ax3.axhline(y=100, color="#8890a4", linestyle="--", linewidth=0.8, alpha=0.7)
                if p.sizing != "fixed":
                    ax3.set_yscale("log")
                    ax3.yaxis.set_minor_formatter(_minor_usd_formatter(dollar=False))
                    ax3.tick_params(axis='y', which='minor', labelsize=6)
                ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.2f}" if abs(x) < 1 else f"{x:,.0f}"))
                ax3.set_ylabel(f"Value in {asset_name}")
                ax3.legend(loc="upper left", fontsize=8, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
                ax3.grid(True, which="major", alpha=0.3, color="#252a3a")
                ax3.grid(True, which="minor", alpha=0.15, color="#252a3a")
                last_ax = ax3
            last_ax.set_xlabel("Date")
            last_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            import math
            date_range_years = (df.index[-1] - df.index[0]).days / 365.25
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
            bt._apply_dark_theme(thumb_fig, [thumb_ax])
            thumb_ax.plot(best["equity"].index, best["equity"], color="#6495ED", linewidth=1.5)
            if show_ratio:
                thumb_ax.plot(best["buyhold"].index, best["buyhold"], color="#8890a4", linewidth=1, alpha=0.7)
            if p.sizing == "fixed":
                thumb_ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            else:
                thumb_ax.set_yscale("log")
                thumb_ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            thumb_ax.grid(True, which="major", alpha=0.3, color="#252a3a")
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

    price_json = _series_to_lw_json(df["close"]) if best else None
    if is_oscillator:
        ind1_json = "[]"
        ind2_json = "[]"
    else:
        ind1_json = _series_to_lw_json(best["ind1_series"]) if best and best.get("ind1_name") != "price" else "[]"
        ind2_json = _series_to_lw_json(best["ind2_series"]) if best else "[]"
    return render_template_string(HTML, p=p, nav_active='backtester', chart=chart_b64, best=best, table_rows=table_rows, col_header=col_header,
                                  asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, index_assets=INDEX_ASSETS, metal_assets=METAL_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
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
    <title>{{ page_title }} — Strategy Analytics</title>
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
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247, 147, 26, 0.06), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none; z-index: 0;
        }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }
        .header { text-align: center; margin-bottom: 32px; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; border-radius: 0; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border-radius: 0; border: 1px solid var(--border); border-left: none; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
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
        .backtest-card-wrapper.locked .locked-overlay { display: flex; }
        .locked-overlay:hover { background: rgba(22,25,34,0.5); }
        .locked-overlay svg { width: 32px; height: 32px; color: var(--text-muted); transition: transform 0.3s ease; }
        .locked-overlay.shake svg { animation: lockShake 0.5s ease; }
        .locked-overlay span { font-size: 0.8em; font-weight: 600; color: var(--text-muted); letter-spacing: 0.05em; text-transform: uppercase; }
        @keyframes lockShake { 0%,100% { transform: translateX(0) rotate(0); } 15% { transform: translateX(-4px) rotate(-5deg); } 30% { transform: translateX(4px) rotate(5deg); } 45% { transform: translateX(-3px) rotate(-3deg); } 60% { transform: translateX(3px) rotate(3deg); } 75% { transform: translateX(-1px) rotate(-1deg); } }
        .reorder-controls { position: absolute; top: 8px; right: 8px; z-index: 10; display: flex; flex-direction: column; gap: 2px; opacity: 0; transition: opacity 0.2s ease; }
        .backtest-card-wrapper:hover .reorder-controls { opacity: 1; }
        .reorder-btn { width: 28px; height: 28px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg-surface); color: var(--text-muted); cursor: pointer; font-size: 0.7em; display: flex; align-items: center; justify-content: center; transition: all 0.15s ease; }
        .reorder-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
        .backtest-card { display: block; background: var(--bg-surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px; transition: all 0.2s ease; cursor: pointer; text-decoration: none; color: inherit; }
        .backtest-card:hover { border-color: var(--border-hover); transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
        .backtest-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
        .backtest-card-asset-logo { width: 32px; height: 32px; object-fit: contain; border-radius: 50%; background: var(--bg-deep); flex-shrink: 0; }
        .backtest-card-asset-fallback { width: 32px; height: 32px; border-radius: 50%; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.8em; color: var(--text-muted); flex-shrink: 0; }
        .backtest-card-head-text { flex: 1; min-width: 0; }
        .backtest-card-title { font-size: 1em; font-weight: 600; color: var(--text); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.3; }
        .backtest-card-asset-name { font-size: 0.75em; color: var(--text-muted); }
        .backtest-card-mode-icon { color: var(--text-dim); flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 8px; background: var(--bg-deep); }
        .backtest-card-desc { font-size: 0.8em; color: var(--text-muted); margin-bottom: 10px; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
        .backtest-card-params { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
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
        .backtest-card-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
        .badge-featured { background: rgba(247,147,26,0.15); color: var(--accent); }
        .badge-community { background: rgba(100,149,237,0.15); color: var(--blue); }
        .badge-private { background: rgba(136,144,164,0.15); color: var(--text-muted); }
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
        .action-btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-muted); cursor: pointer; font-size: 0.82em; font-weight: 500; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; text-decoration: none; }
        .action-btn:hover { border-color: var(--border-hover); color: var(--text); }
        .action-btn.primary { border-color: var(--accent); color: var(--accent); }
        .action-btn.liked { color: #ef4444; border-color: #ef4444; }
        .action-btn.danger { border-color: #ef4444; color: #ef4444; }
        .action-btn.danger:hover { background: rgba(239,68,68,0.1); }
        .hidden { display: none !important; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="text-decoration:none;color:inherit;display:inline-flex;align-items:center;gap:0"><span class="brand-btc">Bitcoin</span><span class="brand-analytics">Strategy Analytics</span></a></h1>
        <div style="font-size:0.8em;color:var(--text-dim);margin-top:2px">Exclusive to <a href="https://the-bitcoin-strategy.com" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:600">Premium Members</a></div>
    </div>
    <nav class="nav-bar">
        <a href="/" class="nav-link {{ 'active' if nav_active=='featured' }}">Featured</a>
        <a href="/community" class="nav-link {{ 'active' if nav_active=='community' }}">Community</a>
        {% if session.get('user_id') %}
        <a href="/my-backtests" class="nav-link {{ 'active' if nav_active=='my-backtests' }}">My Backtests</a>
        {% endif %}
        <a href="/backtester" class="nav-link {{ 'active' if nav_active=='backtester' }}">Backtester</a>
    </nav>
    <div class="panel">
        <h2 class="page-title">{{ page_title }}</h2>
        <p class="page-subtitle">{{ page_subtitle }}</p>

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
                        <span>{{ bt._display_name }} · {{ time_ago(bt.created_at) }}</span>
                        <div class="engagement">
                            <span>♥ {{ bt.likes_count }}</span>
                            <span>💬 {{ bt.comments_count }}</span>
                        </div>
                    </div>
                </a>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endfor %}

        {% elif backtests %}
        <div class="backtest-grid" id="backtest-grid">
            {% for bt in backtests %}
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
                    <span>{{ bt._display_name }} · {{ time_ago(bt.created_at) }}</span>
                    <div class="engagement">
                        <span>♥ {{ bt.likes_count }}</span>
                        <span>💬 {{ bt.comments_count }}</span>
                    </div>
                </div>
            </a>
            </div>
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
</div>
<script>
var _swal = Swal.mixin({
    background: '#1e2130', color: '#e8e9ed', confirmButtonColor: '#6495ED',
    customClass: { popup: 'swal-dark' }
});
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
function moveCard(id, direction) {
    var el = document.querySelector('.backtest-card-wrapper[data-id="' + id + '"]');
    if (!el) return;
    var grid = el.parentElement;
    var wrappers = Array.from(grid.querySelectorAll('.backtest-card-wrapper'));
    var idx = wrappers.indexOf(el);
    if (idx < 0) return;
    var newIdx = idx + direction;
    if (newIdx < 0 || newIdx >= wrappers.length) return;
    if (direction < 0) {
        grid.insertBefore(el, wrappers[newIdx]);
    } else {
        grid.insertBefore(wrappers[newIdx], el);
    }
    // Collect all IDs across all grids in order
    var orderedIds = Array.from(document.querySelectorAll('.backtest-card-wrapper')).map(function(w) { return w.getAttribute('data-id'); });
    fetch('/api/reorder-featured', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ordered_ids: orderedIds})
    });
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
    if (direction < 0) {
        parent.insertBefore(el, sections[newIdx]);
    } else {
        parent.insertBefore(sections[newIdx], el);
    }
    // Collect all backtest IDs in new section order
    var orderedIds = Array.from(document.querySelectorAll('.backtest-card-wrapper')).map(function(w) { return w.getAttribute('data-id'); });
    fetch('/api/reorder-featured', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ordered_ids: orderedIds})
    });
}
</script>
</body>
</html>
"""


DETAIL_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>{{ backtest.title|title if backtest.title else 'Backtest' }} — Strategy Analytics</title>
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
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247, 147, 26, 0.06), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none; z-index: 0;
        }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }
        .header { text-align: center; margin-bottom: 32px; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
        .panel { background: var(--bg-surface); border-radius: 16px; padding: 24px; border: 1px solid var(--border); margin-bottom: 16px; }
        .detail-header { margin-bottom: 20px; }
        .detail-title { font-size: 1.3em; font-weight: 700; margin-bottom: 4px; display: inline; }
        .detail-title-row { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
        .copy-link-icon { width: 18px; height: 18px; color: var(--text-dim); cursor: pointer; transition: color 0.2s ease, transform 0.15s ease; flex-shrink: 0; position: relative; top: 1px; }
        .copy-link-icon:hover { color: var(--accent); transform: scale(1.1); }
        .copy-link-icon.copied { color: #22c55e; }
        .detail-meta { font-size: 0.8em; color: var(--text-muted); display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
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
        .like-btn { display: inline-flex; align-items: center; gap: 8px; padding: 10px 24px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-surface); color: var(--text-muted); cursor: pointer; font-size: 0.9em; font-weight: 600; font-family: 'DM Sans', sans-serif; transition: all 0.2s ease; }
        .like-btn:hover { border-color: #ef4444; color: #ef4444; }
        .like-btn.liked { border-color: #ef4444; color: #ef4444; background: rgba(239,68,68,0.08); }
        .like-btn.disabled { cursor: default; opacity: 0.5; }
        .like-heart { font-size: 1.1em; }
        .like-btn.liked .like-heart { animation: heartPop 0.3s ease; }
        @keyframes heartPop { 0% { transform: scale(1); } 50% { transform: scale(1.3); } 100% { transform: scale(1); } }
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
        .comment-form textarea { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.85em; font-family: 'DM Sans', sans-serif; resize: vertical; min-height: 60px; margin-bottom: 8px; }
        .comment-form textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
        .comment { padding: 12px 0; border-bottom: 1px solid var(--border); }
        .comment:last-child { border-bottom: none; }
        .comment-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 0.8em; }
        .comment-author { font-weight: 600; color: var(--text); }
        .comment-time { color: var(--text-dim); }
        .comment-body { font-size: 0.85em; color: var(--text-muted); line-height: 1.5; }
        .comment-actions { margin-top: 6px; display: flex; gap: 12px; }
        .comment-action-btn { background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 0.75em; font-family: 'DM Sans', sans-serif; }
        .comment-action-btn:hover { color: var(--text); }
        .comment-replies { margin-left: 24px; border-left: 2px solid var(--border); padding-left: 14px; }
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
        .backtest-card-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
        .badge-featured { background: rgba(247,147,26,0.15); color: var(--accent); }
        .badge-community { background: rgba(100,149,237,0.15); color: var(--blue); }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="text-decoration:none;color:inherit;display:inline-flex;align-items:center;gap:0"><span class="brand-btc">Bitcoin</span><span class="brand-analytics">Strategy Analytics</span></a></h1>
        <div style="font-size:0.8em;color:var(--text-dim);margin-top:2px">Exclusive to <a href="https://the-bitcoin-strategy.com" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:600">Premium Members</a></div>
    </div>
    <nav class="nav-bar">
        <a href="/" class="nav-link {{ 'active' if backtest.visibility=='featured' }}">Featured</a>
        <a href="/community" class="nav-link {{ 'active' if backtest.visibility=='community' }}">Community</a>
        {% if session.get('user_id') %}
        <a href="/my-backtests" class="nav-link">My Backtests</a>
        {% endif %}
        <a href="/backtester" class="nav-link">Backtester</a>
    </nav>

    <div class="panel">
        <div class="detail-header">
            {% if backtest.visibility == 'community' %}<span class="backtest-card-badge badge-community">Community</span>{% endif %}
            <div class="detail-title-row">
                <h2 class="detail-title">{{ backtest.title|title if backtest.title else 'Backtest' }}</h2>
                <svg class="copy-link-icon" onclick="copyLink(this)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" title="Copy link"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
            </div>
            <div class="detail-meta">
                <span>by {{ display_name or backtest.user_email.split('@')[0] }}</span>
                <span>{{ time_ago(backtest.created_at) }}</span>
                <span>♥ {{ backtest.likes_count }} · 💬 {{ backtest.comments_count }}</span>
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
            {% if backtest.visibility != 'featured' %}
            <button class="action-btn primary" onclick="featureBacktest('{{ backtest.id }}')">Feature</button>
            {% endif %}
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

    <div id="like-container" style="display:flex;justify-content:flex-end;margin-bottom:16px">
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
            <div class="comment">
                <div class="comment-header">
                    <span class="comment-author">{{ comment._display_name }}</span>
                    <span class="comment-time">{{ time_ago(comment.created_at) }}</span>
                </div>
                <div class="comment-body">{{ comment.body }}</div>
                <div class="comment-actions">
                    {% if is_authenticated %}
                    <button class="comment-action-btn" onclick="showReplyForm('{{ comment.id }}')">Reply</button>
                    {% endif %}
                    {% if is_authenticated and (comment.user_id == session.get('user_id') or is_admin) %}
                    <button class="comment-action-btn" onclick="deleteComment('{{ comment.id }}')">Delete</button>
                    {% endif %}
                </div>
                <div class="reply-form hidden" id="reply-form-{{ comment.id }}">
                    <textarea id="reply-{{ comment.id }}" placeholder="Write a reply..."></textarea>
                    <button class="action-btn" onclick="submitComment('{{ backtest.id }}', '{{ comment.id }}')">Reply</button>
                </div>
                {% if comment.replies %}
                <div class="comment-replies">
                    {% for reply in comment.replies %}
                    <div class="comment">
                        <div class="comment-header">
                            <span class="comment-author">{{ reply._display_name }}</span>
                            <span class="comment-time">{{ time_ago(reply.created_at) }}</span>
                        </div>
                        <div class="comment-body">{{ reply.body }}</div>
                        {% if is_authenticated and (reply.user_id == session.get('user_id') or is_admin) %}
                        <div class="comment-actions">
                            <button class="comment-action-btn" onclick="deleteComment('{{ reply.id }}')">Delete</button>
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
<script>
var _swal = Swal.mixin({
    background: '#1e2130', color: '#e8e9ed', confirmButtonColor: '#6495ED',
    customClass: { popup: 'swal-dark' }
});
function toggleLike(backtestId, btn) {
    fetch('/api/backtest/' + backtestId + '/like', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.querySelector('.like-count').textContent = data.likes_count;
        var textEl = btn.querySelector('.like-text');
        if (data.liked) { btn.classList.add('liked'); if (textEl) textEl.textContent = 'Liked'; }
        else { btn.classList.remove('liked'); if (textEl) textEl.textContent = 'Like'; }
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
function submitComment(backtestId, parentId) {
    var textareaId = parentId ? 'reply-' + parentId : 'comment-body';
    var body = document.getElementById(textareaId).value.trim();
    if (!body) return;
    fetch('/api/backtest/' + backtestId + '/comment', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({body: body, parent_id: parentId || null})
    }).then(function() { location.reload(); });
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
    if (el) el.classList.toggle('hidden');
}
function featureBacktest(backtestId) {
    fetch('/api/backtest/' + backtestId + '/feature', { method: 'POST' }).then(function() { location.reload(); });
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
    <title>My Backtests — Strategy Analytics</title>
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
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background: var(--bg-deep); color: var(--text); min-height: 100vh; }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(247, 147, 26, 0.06), transparent),
                        radial-gradient(ellipse 60% 40% at 80% 100%, rgba(100, 149, 237, 0.04), transparent);
            pointer-events: none; z-index: 0;
        }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px 20px; position: relative; z-index: 1; }
        .header { text-align: center; margin-bottom: 32px; }
        .header h1 { font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; display: inline-flex; align-items: center; gap: 0; }
        .header h1 .brand-btc { background: linear-gradient(135deg, var(--blue), #4a7dd6); color: #fff; padding: 6px 14px; font-weight: 700; }
        .header h1 .brand-analytics { background: var(--bg-elevated); color: var(--text); padding: 6px 14px; border: 1px solid var(--border); border-left: none; }
        .nav-bar { display: flex; align-items: center; justify-content: center; gap: 4px; margin-bottom: 20px; }
        .nav-link { padding: 8px 18px; border-radius: 8px; font-size: 0.82em; font-weight: 500; color: var(--text-muted); text-decoration: none; transition: all 0.2s ease; border: 1px solid transparent; }
        .nav-link:hover { color: var(--text); background: var(--bg-elevated); border-color: var(--border); }
        .nav-link.active { color: var(--accent); background: rgba(247,147,26,0.08); border-color: var(--accent); }
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
        .backtest-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
        .backtest-card-asset-logo { width: 32px; height: 32px; object-fit: contain; border-radius: 50%; background: var(--bg-deep); flex-shrink: 0; }
        .backtest-card-asset-fallback { width: 32px; height: 32px; border-radius: 50%; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.8em; color: var(--text-muted); flex-shrink: 0; }
        .backtest-card-head-text { flex: 1; min-width: 0; }
        .backtest-card-asset-name { font-size: 0.75em; color: var(--text-muted); }
        .backtest-card-mode-icon { color: var(--text-dim); flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 8px; background: var(--bg-deep); }
        .backtest-card-params { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
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
        .username-section { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; padding: 14px 18px; background: var(--bg-base); border: 1px solid var(--border); border-radius: 12px; }
        .username-section label { font-size: 0.8em; color: var(--text-muted); font-weight: 500; white-space: nowrap; margin: 0; }
        .username-section input { flex: 1; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-deep); color: var(--text); font-size: 0.85em; font-family: 'DM Sans', sans-serif; }
        .username-section input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
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
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="text-decoration:none;color:inherit;display:inline-flex;align-items:center;gap:0"><span class="brand-btc">Bitcoin</span><span class="brand-analytics">Strategy Analytics</span></a></h1>
        <div style="font-size:0.8em;color:var(--text-dim);margin-top:2px">Exclusive to <a href="https://the-bitcoin-strategy.com" target="_blank" style="color:var(--accent);text-decoration:none;font-weight:600">Premium Members</a></div>
    </div>
    <nav class="nav-bar">
        <a href="/" class="nav-link">Featured</a>
        <a href="/community" class="nav-link">Community</a>
        <a href="/my-backtests" class="nav-link active">My Backtests</a>
        <a href="/backtester" class="nav-link">Backtester</a>
    </nav>

    <div class="panel">
        <h2 class="page-title">My Backtests</h2>

        <div class="username-section">
            <label>Public Username:</label>
            <input type="text" id="display-name-input" value="{{ display_name or email_prefix }}" placeholder="Set your public username" maxlength="40" {% if display_name %}readonly style="opacity:0.8"{% endif %}>
            <button class="action-btn" id="edit-name-btn" onclick="toggleMyUsername()" title="Edit username" style="padding:8px 10px">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M11.5 1.5l3 3L5 14H2v-3L11.5 1.5z"/></svg>
            </button>
            <button class="action-btn hidden" id="save-name-btn" onclick="saveDisplayName()">Save</button>
        </div>

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
                        </div>
                    </div>
                </a>
                <div class="card-actions">
                    <a class="action-btn" href="/?{{ bt.query_string }}">Open</a>
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
                    <a class="action-btn" href="/?{{ bt.query_string }}">Open</a>
                    <button class="action-btn" onclick="event.stopPropagation();openEditModal('{{ bt.id }}', '{{ bt.title|e }}', '{{ (bt.description or '')|e }}')">Edit</button>
                    <button class="action-btn danger" onclick="event.stopPropagation();deleteBacktest('{{ bt.id }}')">Delete</button>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        {% if not published and not saved %}
        <div class="empty-state">
            <h3>No backtests yet</h3>
            <p>Run a backtest and click Save or Publish to add it here.</p>
        </div>
        {% endif %}
    </div>
</div>
<script>
var _swal = Swal.mixin({
    background: '#1e2130', color: '#e8e9ed', confirmButtonColor: '#6495ED',
    customClass: { popup: 'swal-dark' }
});
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
function toggleMyUsername() {
    var input = document.getElementById('display-name-input');
    input.readOnly = false;
    input.style.opacity = '1';
    input.focus();
    input.select();
    document.getElementById('edit-name-btn').classList.add('hidden');
    document.getElementById('save-name-btn').classList.remove('hidden');
}
function saveDisplayName() {
    var input = document.getElementById('display-name-input');
    var name = input.value.trim();
    if (!name) { _swal.fire({icon:'warning', title:'Please enter a username'}); return; }
    fetch('/api/display-name', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({display_name: name})
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
            input.readOnly = true;
            input.style.opacity = '0.8';
            document.getElementById('edit-name-btn').classList.remove('hidden');
            document.getElementById('save-name-btn').classList.add('hidden');
            _swal.fire({icon:'success', title:'Username saved!', text:'This applies to all your backtests and comments.', timer:2000, showConfirmButton:false});
        }
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
</script>

<!-- Edit Modal -->
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


@app.route('/api/backtest/<bt_id>/like', methods=['POST'])
def api_like(bt_id):
    """Toggle like."""
    user_id, email = _require_auth_api()
    likes_count, liked = db.toggle_like(user_id, bt_id)
    return jsonify({'likes_count': likes_count, 'liked': liked})


@app.route('/api/backtest/<bt_id>/comment', methods=['POST'])
def api_comment(bt_id):
    """Add a comment."""
    user_id, email = _require_auth_api()
    data = request.get_json()
    if not data or not data.get('body', '').strip():
        abort(400)
    comment = db.add_comment(bt_id, user_id, email, data['body'].strip(), data.get('parent_id'))
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


MODE_SVGS = {
    'backtest': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 20 10 12 16 16 24 6"/><line x1="4" y1="24" x2="24" y2="24" opacity="0.4"/><circle cx="24" cy="6" r="2" fill="currentColor" stroke="none"/></svg>',
    'sweep': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="14" r="8" opacity="0.4"/><line x1="18" y1="20" x2="24" y2="26"/><path d="M9 14h6M12 11v6"/></svg>',
    'heatmap': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.6"/><rect x="11" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.3"/><rect x="19" y="3" width="6" height="6" rx="1" fill="currentColor" opacity="0.15"/><rect x="3" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.3"/><rect x="11" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.8"/><rect x="19" y="11" width="6" height="6" rx="1" fill="currentColor" opacity="0.4"/><rect x="3" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.15"/><rect x="11" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.4"/><rect x="19" y="19" width="6" height="6" rx="1" fill="currentColor" opacity="0.6"/></svg>',
    'sweep-lev': '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    'regression': '<svg width="18" height="18" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="20" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="10" cy="16" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="16" cy="12" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><circle cx="22" cy="8" r="1.5" fill="currentColor" stroke="none" opacity="0.5"/><line x1="4" y1="23" x2="25" y2="5" opacity="0.6"/></svg>',
}

MODE_LABELS = {
    'backtest': 'Backtest',
    'sweep': 'Sweep',
    'heatmap': 'Heatmap',
    'sweep-lev': 'Leverage',
    'regression': 'Regression',
}


def _enrich_backtest_cards(backtests):
    """Parse params JSON and add display-ready fields to each backtest dict."""
    # Batch-resolve display names
    user_ids = {bt.get('user_id') for bt in backtests if bt.get('user_id')}
    display_names = {}
    for uid in user_ids:
        name = db.get_display_name(uid)
        if name:
            display_names[uid] = name
    for bt in backtests:
        uid = bt.get('user_id', '')
        bt['_display_name'] = display_names.get(uid, bt.get('user_email', '').split('@')[0])
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


# --- Page Routes ---

@app.route('/community')
def community():
    """Community backtests page."""
    _try_token_auth()
    sort = request.args.get('sort', 'newest')
    page = int(request.args.get('page', 1))
    backtests, total = db.list_backtests(visibility='community', sort=sort, page=page, per_page=20)
    _enrich_backtest_cards(backtests)
    total_pages = max(1, (total + 19) // 20)
    return render_template_string(COMMUNITY_HTML,
        nav_active='community', page_title='Community Backtests',
        page_subtitle='Strategies shared by the community',
        backtests=backtests, sort=sort, page=page, total_pages=total_pages,
        is_authenticated=_is_authenticated(), time_ago=_time_ago)


@app.route('/')
@app.route('/featured')
def featured():
    """Featured backtests page."""
    _try_token_auth()
    sort = request.args.get('sort', 'manual')
    backtests, total = db.list_backtests(visibility='featured', sort=sort, page=1, per_page=200)
    _enrich_backtest_cards(backtests)
    # Group by asset, preserving sort order
    from collections import OrderedDict
    grouped = OrderedDict()
    for bt in backtests:
        asset = bt.get('_asset', '') or 'other'
        if asset not in grouped:
            grouped[asset] = []
        grouped[asset].append(bt)
    # Build sections with display info
    asset_sections = []
    for asset, bts in grouped.items():
        asset_sections.append({
            'asset': asset,
            'display': asset.capitalize() if asset != 'other' else 'Other',
            'logo': ASSET_LOGOS.get(asset, ''),
            'backtests': bts,
        })
    return render_template_string(COMMUNITY_HTML,
        nav_active='featured', page_title='Featured Backtests',
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
    display_name = db.get_display_name(user_id)
    email = session.get('email', '')
    email_prefix = email.split('@')[0] if email else ''
    return render_template_string(MY_BACKTESTS_HTML,
        published=published, saved=saved, time_ago=_time_ago,
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
        if not _is_authenticated() or session.get('user_id') != bt_entry['user_id']:
            abort(404)
    comments = db.get_comments(bt_id)
    is_auth = _is_authenticated()
    liked = db.has_liked(session.get('user_id', ''), bt_id) if is_auth else False
    author_display_name = db.get_display_name(bt_entry['user_id'])
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
    for c in comments:
        c['_display_name'] = commenter_names.get(c['user_id'], c['user_email'].split('@')[0])
        for r in c.get('replies', []):
            r['_display_name'] = commenter_names.get(r['user_id'], r['user_email'].split('@')[0])
    # Strip save/publish action buttons from cached HTML
    cached = bt_entry.get('cached_html', '') or ''
    cached = re.sub(r'<div class="action-buttons"[^>]*id="backtest-actions"[^>]*>.*?</div>', '', cached, flags=re.DOTALL)
    bt_entry = dict(bt_entry)
    bt_entry['cached_html'] = cached
    # Parse params for display
    import json as json_mod
    bt_params = json_mod.loads(bt_entry.get('params', '{}') or '{}')
    return render_template_string(DETAIL_HTML,
        backtest=bt_entry, comments=comments, bt_params=bt_params,
        is_authenticated=is_auth, is_admin=_is_admin(),
        has_liked=liked, time_ago=_time_ago,
        display_name=author_display_name)


if __name__ == "__main__":
    print(f"Starting Strategy Analytics at http://localhost:5000 (assets: {', '.join(ASSET_NAMES)})")
    app.run(debug=False, port=5000)
