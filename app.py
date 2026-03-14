#!/usr/bin/env python3
"""Web interface for the Backtesting Engine."""

import os
import hmac
import hashlib
import json
import time
import base64
import functools
from io import BytesIO
from datetime import timedelta
from flask import Flask, render_template_string, request, session, redirect
import backtest as bt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')
ANALYTICS_SECRET = os.environ.get('ANALYTICS_SHARED_SECRET', '')
LARAVEL_LOGIN_URL = 'https://the-bitcoin-strategy.com/app/analytics-redirect'
SESSION_DURATION = 86400  # 24 hours

app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') != 'development'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

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
                return redirect('/', code=302)
            # Invalid token — fall through to session check

        # Check existing session
        auth_time = session.get('auth_time')
        if auth_time and (time.time() - auth_time) < SESSION_DURATION:
            return f(*args, **kwargs)

        # No valid auth — redirect to Laravel
        return redirect(LARAVEL_LOGIN_URL, code=302)

    return decorated

HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>Strategy Analytics</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
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
        input, select {
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
        input:focus, select:focus {
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
            grid-template-columns: repeat(4, 1fr);
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
    </div>
    <div class="layout">
        <div class="panel">
            <form method="POST" id="form">
                <div class="form-section">
                    <div class="section-title">Mode</div>
                    <input type="hidden" name="mode" id="mode" value="{{ p.mode }}">
                    <div class="mode-selector">
                        <div class="mode-card {{ 'active' if p.mode=='backtest' }}" onclick="selectMode('backtest', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="4 20 10 12 16 16 24 6"/>
                                <line x1="4" y1="24" x2="24" y2="24" opacity="0.4"/>
                                <circle cx="24" cy="6" r="2" fill="currentColor" stroke="none"/>
                            </svg>
                            <span class="mode-card-label">Backtest</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='sweep' }}" onclick="selectMode('sweep', this)">
                            <svg class="mode-card-icon" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="12" cy="14" r="8" opacity="0.4"/>
                                <line x1="18" y1="20" x2="24" y2="26"/>
                                <path d="M9 14h6M12 11v6"/>
                            </svg>
                            <span class="mode-card-label">Single Indicator Optimization</span>
                        </div>
                        <div class="mode-card {{ 'active' if p.mode=='heatmap' }}" onclick="selectMode('heatmap', this)">
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
                        <div class="mode-card {{ 'active' if p.mode=='sweep-lev' }}" onclick="selectMode('sweep-lev', this)">
                            <svg class="mode-card-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
                            </svg>
                            <span class="mode-card-label">Leverage Optimization</span>
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
                        <div class="asset-section-label">Stock Indices</div>
                        <div class="asset-grid">
                            {% for a in stock_assets %}
                            <div class="asset-card {{ 'active' if p.asset==a }}" data-asset="{{ a }}" onclick="selectAsset('{{ a }}', this)">
                                <div class="asset-card-placeholder">{{ a[:3]|upper }}</div>
                                <span class="asset-card-label">{{ a }}</span>
                            </div>
                            {% endfor %}
                        </div>
                        {% endif %}
                    </div>
                </div>
                <div class="form-section">
                    <div class="section-title">Indicators</div>
                    <div class="form-row">
                        <div class="form-group">
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
                        <div class="sep"></div>
                        <div class="form-group">
                            <label>Indicator 2</label>
                            <select name="ind2_name" id="ind2_name">
                                <option value="price" {{ 'selected' if p.ind2_name=='price' }}>Price</option>
                                <option value="sma" {{ 'selected' if p.ind2_name=='sma' }}>SMA (Simple Moving Average)</option>
                                <option value="ema" {{ 'selected' if p.ind2_name=='ema' }}>EMA (Exponential Moving Average)</option>
                                <option value="wma" {{ 'selected' if p.ind2_name=='wma' }}>WMA (Weighted Moving Average)</option>
                                <option value="hma" {{ 'selected' if p.ind2_name=='hma' }}>HMA (Hull Moving Average)</option>
                                <option value="dema" {{ 'selected' if p.ind2_name=='dema' }}>DEMA (Double Exponential MA)</option>
                                <option value="tema" {{ 'selected' if p.ind2_name=='tema' }}>TEMA (Triple Exponential MA)</option>
                                <option value="kama" {{ 'selected' if p.ind2_name=='kama' }}>KAMA (Kaufman Adaptive MA)</option>
                                <option value="zlema" {{ 'selected' if p.ind2_name=='zlema' }}>ZLEMA (Zero-Lag EMA)</option>
                                <option value="smma" {{ 'selected' if p.ind2_name=='smma' }}>SMMA (Smoothed Moving Average)</option>
                                <option value="lsma" {{ 'selected' if p.ind2_name=='lsma' }}>LSMA (Least Squares MA)</option>
                                <option value="alma" {{ 'selected' if p.ind2_name=='alma' }}>ALMA (Arnaud Legoux MA)</option>
                                <option value="frama" {{ 'selected' if p.ind2_name=='frama' }}>FRAMA (Fractal Adaptive MA)</option>
                                <option value="t3" {{ 'selected' if p.ind2_name=='t3' }}>T3 (Tillson T3)</option>
                                <option value="mcginley" {{ 'selected' if p.ind2_name=='mcginley' }}>McGinley Dynamic</option>
                            </select>
                        </div>
                        <div class="form-group" id="period2-group">
                            <label>Period 2</label>
                            <input type="number" name="period2" value="{{ p.ind2_period or '' }}" placeholder="e.g. 40" min="2">
                        </div>
                    </div>
                    <div class="signal-explainer" id="signal-explainer">
                        Buy when <span id="explainer-ind1">Price</span> crosses above <span id="explainer-ind2">SMA</span>. Sell when it crosses below.
                    </div>
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
                        <input type="hidden" name="lev_step" value="0.25">
                    </div>
                    <div id="lev-mode-info" class="hidden" style="margin-top:10px;font-size:0.78em;color:var(--text-muted);line-height:1.6;padding:10px 14px;background:var(--bg-deep);border-radius:8px;border-left:2px solid var(--accent)">
                        <strong style="color:var(--text)">Optimal</strong> — Daily rebalance for long positions, set & forget for short positions. Best of both worlds.<br>
                        <strong style="color:var(--text)">Daily Rebalance</strong> — Leverage is reset to target every day. Consistent exposure but higher fees in volatile markets.<br>
                        <strong style="color:var(--text)">Set & Forget</strong> — Leverage is applied at entry and drifts naturally. Lower fees but exposure changes over time.
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
            {% if chart %}
                {% if best %}
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
                        <img class="chart-img" id="backtest-chart-img" src="data:image/png;base64,{{ chart }}" />
                        <button onclick="downloadChart()" class="chart-download-btn" title="Download chart as PNG">
                            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M8 2v8m0 0l-3-3m3 3l3-3M3 12h10"/>
                            </svg>
                        </button>
                    </div>
                </div>
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
                {% if best and not lev_sweep|default(none) %}
                {# Compact 3-column metrics table: Metric | Strategy | Buy & Hold #}
                <div class="metrics-panel">
                <table class="metrics-table">
                    <thead><tr>
                        <th class="col-metric">Metric</th>
                        <th class="col-strategy">{{ best.label }}</th>
                        <th class="col-buyhold">Buy & Hold</th>
                    </tr></thead>
                    <tbody>
                    <tr class="section-row"><td colspan="3">Performance</td></tr>
                    <tr><td class="m-label">$100 would be</td><td class="m-val {{ 'positive' if best.total_return > 0 else 'negative' }}">${{ "{:,.2f}"|format(100 * (1 + best.total_return / 100)) }}</td><td class="m-val {{ 'positive' if best.buyhold_return > 0 else 'negative' }}">${{ "{:,.2f}"|format(100 * (1 + best.buyhold_return / 100)) }}</td></tr>
                    <tr><td class="m-label">Ann. Return</td><td class="m-val {{ 'positive' if best.annualized > 0 else 'negative' }}">{{ "%.2f"|format(best.annualized) }}%</td><td class="m-val {{ 'positive' if best.buyhold_annualized > 0 else 'negative' }}">{{ "%.2f"|format(best.buyhold_annualized) }}%</td></tr>
                    <tr><td class="m-label">Sharpe Ratio</td><td class="m-val">{{ "%.2f"|format(best.sharpe) }}</td><td class="m-val">{{ "%.2f"|format(best.buyhold_sharpe) }}</td></tr>
                    <tr><td class="m-label">Sortino Ratio</td><td class="m-val">{{ "%.2f"|format(best.sortino) }}</td><td class="m-val">{{ "%.2f"|format(best.buyhold_sortino) }}</td></tr>
                    <tr class="section-row"><td colspan="3">Risk</td></tr>
                    <tr><td class="m-label">Max Drawdown</td><td class="m-val negative">{{ "%.2f"|format(best.max_drawdown) }}%</td><td class="m-val negative">{{ "%.2f"|format(best.buyhold_max_drawdown) }}%</td></tr>
                    <tr><td class="m-label">Drawdown Duration</td><td class="m-val">{{ best.max_dd_duration }}d</td><td class="m-val">{{ best.buyhold_max_dd_duration }}d</td></tr>
                    <tr><td class="m-label">Volatility</td><td class="m-val">{{ "%.1f"|format(best.volatility) }}%</td><td class="m-val">{{ "%.1f"|format(best.buyhold_volatility) }}%</td></tr>
                    <tr><td class="m-label">Beta</td><td class="m-val">{{ "%.2f"|format(best.beta) }}</td><td class="m-val muted">1.00</td></tr>
                    <tr><td class="m-label">Calmar Ratio</td><td class="m-val">{{ "%.2f"|format(best.calmar) }}</td><td class="m-val">{{ "%.2f"|format(best.buyhold_calmar) }}</td></tr>
                    <tr class="section-row"><td colspan="3">Trades</td></tr>
                    <tr><td class="m-label">Trades</td><td class="m-val">{{ best.trades }}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Win Rate</td><td class="m-val">{{ "%.1f"|format(best.win_rate) }}%</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Avg Win / Loss</td><td class="m-val"><span class="positive">+{{ "%.1f"|format(best.avg_win) }}%</span> / <span class="negative">{{ "%.1f"|format(best.avg_loss) }}%</span></td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Profit Factor</td><td class="m-val">{% if best.profit_factor > 9999 %}&infin;{% else %}{{ "%.2f"|format(best.profit_factor) }}{% endif %}</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Avg Duration</td><td class="m-val">{{ "%.0f"|format(best.avg_trade_duration) }}d</td><td class="m-val muted">&mdash;</td></tr>
                    <tr><td class="m-label">Time in Market</td><td class="m-val">{{ "%.1f"|format(best.time_in_market) }}%</td><td class="m-val muted">&mdash;</td></tr>
                    <tr class="section-row"><td colspan="3">Annual</td></tr>
                    <tr><td class="m-label">Best Year</td><td class="m-val positive">{% if best.best_year[0] %}+{{ "%.1f"|format(best.best_year[1]) }}% ({{ best.best_year[0] }}){% else %}&mdash;{% endif %}</td><td class="m-val positive">{% if best.buyhold_best_year[0] %}+{{ "%.1f"|format(best.buyhold_best_year[1]) }}% ({{ best.buyhold_best_year[0] }}){% else %}&mdash;{% endif %}</td></tr>
                    <tr><td class="m-label">Worst Year</td><td class="m-val negative">{% if best.worst_year[0] %}{{ "%.1f"|format(best.worst_year[1]) }}% ({{ best.worst_year[0] }}){% else %}&mdash;{% endif %}</td><td class="m-val negative">{% if best.buyhold_worst_year[0] %}{{ "%.1f"|format(best.buyhold_worst_year[1]) }}% ({{ best.buyhold_worst_year[0] }}){% else %}&mdash;{% endif %}</td></tr>
                    </tbody>
                </table>
                </div>
                {% endif %}
            {% else %}
                <div class="placeholder">Configure parameters and press Run Backtest</div>
            {% endif %}
        </div>
    </div>
</div>
<script>
var assetStarts = {{ asset_starts_json|tojson }};
function selectMode(mode, el) {
    document.getElementById('mode').value = mode;
    var cards = document.querySelectorAll('.mode-card');
    for (var i = 0; i < cards.length; i++) cards[i].classList.remove('active');
    el.classList.add('active');
    toggleFields();
}
function toggleFields() {
    var mode = document.getElementById('mode').value;
    var ind1El = document.getElementById('ind1_name');
    var ind1 = ind1El.value;
    // Auto-promote ind1 from price to SMA in heatmap mode
    if (mode === 'heatmap' && ind1 === 'price') {
        ind1El.value = 'sma';
        ind1 = 'sma';
    }
    var isLevSweep = mode === 'sweep-lev';
    var rules = [
        ['period1-group', ind1 !== 'price' && mode !== 'heatmap'],
        ['period2-group', (mode === 'backtest' || mode === 'sweep-lev') && document.getElementById('ind2_name').value !== 'price'],
        ['range-min-group', mode === 'sweep' || mode === 'heatmap'],
        ['range-max-group', mode === 'sweep' || mode === 'heatmap'],
        ['step-group', mode === 'heatmap'],
        ['long-lev-group', !isLevSweep],
        ['short-lev-group', !isLevSweep],
        ['exposure-group', !isLevSweep],
        ['lev-mode-group', true],
        ['lev-min-group', isLevSweep],
        ['lev-max-group', isLevSweep],
    ];
    for (var i = 0; i < rules.length; i++) {
        var el = document.getElementById(rules[i][0]);
        var show = rules[i][1];
        if (show) { el.classList.remove('hidden'); } else { el.classList.add('hidden'); }
        var inputs = el.querySelectorAll('input,select');
        for (var j = 0; j < inputs.length; j++) inputs[j].disabled = !show;
    }
    updateExplainer();
}
function updateExplainer() {
    var ind1 = document.getElementById('ind1_name');
    var ind2 = document.getElementById('ind2_name');
    var p1 = document.querySelector('#period1-group input');
    var p2 = document.querySelector('#period2-group input');
    var label1 = ind1.value === 'price' ? 'Price' : ind1.value.toUpperCase() + (p1.value ? '(' + p1.value + ')' : '');
    var label2 = ind2.value.toUpperCase() + (p2.value ? '(' + p2.value + ')' : '');
    document.getElementById('explainer-ind1').textContent = label1;
    document.getElementById('explainer-ind2').textContent = label2;
}
document.querySelector('#period1-group input').addEventListener('input', updateExplainer);
document.querySelector('#period2-group input').addEventListener('input', updateExplainer);
document.getElementById('ind2_name').addEventListener('change', function() { updateExplainer(); toggleFields(); });
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
    var ind1 = document.getElementById('ind1_name').value;
    var p2 = document.querySelector('#period2-group input').value.trim();
    var p1 = document.querySelector('#period1-group input').value.trim();
    var errors = [];

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

function resetBtn(done) {
    var btn = document.getElementById('btn');
    btn.classList.remove('btn-stop');
    currentAbort = null;
    if (done) {
        btn.disabled = true;
        btn.textContent = '\u2713  Results Ready';
        btn.classList.add('btn-done');
    } else {
        btn.disabled = false;
        btn.textContent = 'Run Backtest';
        btn.classList.remove('btn-done');
    }
}
function enableBtn() {
    var btn = document.getElementById('btn');
    if (btn.classList.contains('btn-done')) {
        btn.disabled = false;
        btn.textContent = 'Run Backtest';
        btn.classList.remove('btn-done');
    }
}

document.getElementById('btn').addEventListener('click', function(e) {
    if (currentAbort) {
        e.preventDefault();
        currentAbort.abort();
        return;
    }
});

document.getElementById('form').addEventListener('submit', function(e) {
    e.preventDefault();
    var btn = document.getElementById('btn');
    var panel = document.getElementById('results-panel');

    if (currentAbort) {
        currentAbort.abort();
        return;
    }

    var errors = validateForm();
    if (errors.length > 0) {
        panel.innerHTML = '<div class="placeholder" style="color:var(--accent)">' + errors.join('<br>') + '</div>';
        return;
    }

    currentAbort = new AbortController();
    btn.textContent = 'Stop';
    btn.classList.add('btn-stop');
    panel.style.opacity = '0.5';
    panel.style.transition = 'opacity 0.2s ease';

    var formData = new FormData(this);

    fetch('/', { method: 'POST', body: formData, signal: currentAbort.signal })
        .then(function(resp) { return resp.text(); })
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
            }
            // Update URL with form params for shareable links
            var qs = new URLSearchParams(formData);
            var viewParam = new URLSearchParams(window.location.search).get('view');
            if (viewParam) qs.set('view', viewParam);
            history.replaceState(null, '', '?' + qs.toString());
            activateViewFromURL();
            resetBtn(true);
        })
        .catch(function(err) {
            panel.style.opacity = '1';
            if (err.name === 'AbortError') {
                panel.innerHTML = '<div class="placeholder">Stopped</div>';
            } else {
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
    fetch('/', { method: 'POST', body: formData, signal: currentAbort.signal })
        .then(function(resp) { return resp.text(); })
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
            }
            var qs = new URLSearchParams(formData);
            var viewParam = new URLSearchParams(window.location.search).get('view');
            if (viewParam) qs.set('view', viewParam);
            history.replaceState(null, '', '?' + qs.toString());
            activateViewFromURL();
            resetBtn(true);
        })
        .catch(function(err) {
            if (err.name === 'AbortError') {
                panel.innerHTML = '<div class="placeholder">Stopped</div>';
            }
            resetBtn();
        });
})();
{% endif %}
</script>
</body>
</html>
"""


class Params:
    """Hold form parameters with defaults."""
    def __init__(self, form=None):
        if form:
            self.asset = form.get("asset", DEFAULT_ASSET)
            self.mode = form.get("mode", "sweep")
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
        else:
            self.asset = DEFAULT_ASSET
            self.mode = "backtest"
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
_STOCK_ASSETS = {"Dax", "Dow Jones", "Hang Seng", "Nasdaq100", "SP500"}
PRIORITY_ASSETS = [a for a in _PRIORITY_ORDER if a in ASSETS]
OTHER_ASSETS = [a for a in ASSET_NAMES if a not in _PRIORITY_ORDER and a not in _STOCK_ASSETS]
STOCK_ASSETS = [a for a in ASSET_NAMES if a in _STOCK_ASSETS]
DEFAULT_ASSET = "bitcoin" if "bitcoin" in ASSETS else ASSET_NAMES[0]
ASSET_LOGOS = {
    "bitcoin": "bitcoin-btc-logo.png", "ethereum": "ethereum-eth-logo.png",
    "solana": "solana-sol-logo.png", "XRP": "xrp-xrp-logo.png",
    "BNB": "bnb-bnb-logo.png", "Cardano": "cardano-ada-logo.png",
    "Chainlink": "chainlink-link-logo.png", "Dogecoin": "dogecoin-doge-logo.png",
    "Monero": "monero-xmr-logo.png", "Bitcoin Cash": "bitcoin-cash-bch-logo.png",
    "Hyperliquid": None,
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


@app.route("/", methods=["GET", "POST"])
@require_auth
def index():
    chart_b64 = None
    best = None
    table_rows = None
    col_header = "Strategy"

    if request.method == "GET":
        # If query params present, pre-fill form from them (shareable URL support)
        if any(k in request.args for k in ('asset', 'mode', 'ind1_name', 'ind2_name', 'period1', 'period2', 'exposure')):
            p = Params(request.args)
        else:
            p = Params()
        return render_template_string(HTML, p=p, chart=None, best=None, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
                                      price_json=None, ind1_json='[]', ind2_json='[]', ind1_label='', ind2_label='')

    p = Params(request.form)
    import pandas as pd_mod
    df = ASSETS.get(p.asset, ASSETS[DEFAULT_ASSET]).copy()
    if p.start_date:
        df = df[df.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
    if p.end_date:
        df = df[df.index <= pd_mod.Timestamp(p.end_date, tz="UTC")]
    if not p.start_date:
        p.start_date = str(df.index[0].date())
    if not p.end_date:
        p.end_date = str(df.index[-1].date())

    fee = p.fee / 100

    # --- Leverage Sweep Mode ---
    if p.mode == "sweep-lev":
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        n_days = len(df)
        lev_values = [round(p.lev_min + i * p.lev_step, 4)
                      for i in range(int((p.lev_max - p.lev_min) / p.lev_step) + 1)]

        # Compute base position from ind1/ind2
        ind1_series, _ = bt.compute_indicator_from_spec(df, p.ind1_name, p.ind1_period)
        ind2_period_val = p.ind2_period if p.ind2_period else 44
        ind2_series, _ = bt.compute_indicator_from_spec(df, p.ind2_name, ind2_period_val)
        above = ind1_series > ind2_series
        position_base = bt._apply_exposure(above, p.exposure).shift(1).fillna(0)
        daily_return = df["close"].pct_change().fillna(0)

        if p.ind1_name == "price":
            title_label = f"Price/{p.ind2_name.upper()}({ind2_period_val})"
        else:
            p1_str = p.ind1_period if p.ind1_period else "?"
            title_label = f"{p.ind1_name.upper()}({p1_str})/{p.ind2_name.upper()}({ind2_period_val})"

        def _sweep_ann(ll, sl):
            if p.lev_mode == "set-forget":
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

        long_sweep_full = [_sweep_ann(lv, 0) for lv in lev_values]
        short_sweep_full = [_sweep_ann(0, lv) for lv in lev_values]

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

        best_result = bt.run_strategy(df, p.ind1_name, p.ind1_period, p.ind2_name, ind2_period_val,
                                       p.initial_cash, fee, p.exposure, best_long_lev, best_short_lev, p.lev_mode)
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
        return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
                                      hide_buyhold=(p.exposure == "short-cash"), lev_sweep=lev_sweep_info,
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

        # Precompute indicators
        ind1_cache = {}
        ind2_cache = {}
        for per in periods:
            ind1_cache[per], _ = bt.compute_indicator_from_spec(df, ind1_name, per)
            if same_type:
                ind2_cache[per] = ind1_cache[per]
            else:
                ind2_cache[per], _ = bt.compute_indicator_from_spec(df, ind2_name, per)

        daily_return = df["close"].pct_change().fillna(0)

        matrix = np.full((n, n), np.nan)
        best_ann = -np.inf
        best_p1 = best_p2 = None
        for i, p1 in enumerate(periods):
            for j, p2 in enumerate(periods):
                if same_type and p1 >= p2:
                    continue
                above = ind1_cache[p1] > ind2_cache[p2]
                position = bt._apply_exposure(above, p.exposure).shift(1).fillna(0)
                leverage = np.where(position > 0, p.long_leverage,
                           np.where(position < 0, p.short_leverage, 1))
                strat_return = position * daily_return * leverage
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

        best_result = bt.run_strategy(df, ind1_name, best_p1, ind2_name, best_p2,
                                       p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode)
        best = _enrich_best(best_result, df)

        price_json = _series_to_lw_json(df["close"])
        ind1_json = _series_to_lw_json(best["ind1_series"]) if best.get("ind1_name") != "price" else "[]"
        ind2_json = _series_to_lw_json(best["ind2_series"])
        return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
                                      hide_buyhold=(p.exposure == "short-cash"),
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
            result = bt.run_strategy(df, p.ind1_name, p.ind1_period, p.ind2_name, period,
                                      p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode)
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

        best_result = bt.run_strategy(df, p.ind1_name, p.ind1_period, p.ind2_name, best_period,
                                       p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode)
        best = _enrich_best(best_result, df)

    # --- Backtest Mode ---
    else:
        if p.ind2_period is not None:
            # Single run with fixed period
            result = bt.run_strategy(df, p.ind1_name, p.ind1_period, p.ind2_name, p.ind2_period,
                                      p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode)
            results = [result]
        else:
            # Sweep ind2 period and show table
            results = bt.sweep_periods(df, p.ind1_name, p.ind1_period, p.ind2_name, None,
                                        "ind2", p.range_min, p.range_max,
                                        p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode)
            # For same-type crossover, filter invalid combos
            if p.ind1_name != "price" and p.ind1_name == p.ind2_name and p.ind1_period is not None:
                results = [r for r in results if r["ind2_period"] > p.ind1_period]
                results.sort(key=lambda r: r["total_return"], reverse=True)

        if results:
            best = _enrich_best(results[0], df)
            if len(results) > 1:
                table_rows = [{"label": r["label"], **r} for r in results]

            # Generate chart
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            import numpy as np

            asset_name = p.asset.capitalize()
            show_ratio = p.exposure != "short-cash"
            if show_ratio:
                fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 13), dpi=150,
                                                     gridspec_kw={"height_ratios": [5, 2.5, 2.5]}, sharex=True)
                bt._apply_dark_theme(fig, [ax1, ax2, ax3])
            else:
                fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), dpi=150,
                                                gridspec_kw={"height_ratios": [7, 3]}, sharex=True)
                bt._apply_dark_theme(fig, [ax1, ax2])

            ax1.plot(df.index, df["close"], label=f"{asset_name} Price", color="#e8eaf0", linewidth=0.8)

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
            ax1.set_title(f"{asset_name} Backtest \u2014 Best: {best['label']} "
                          f"({best['total_return']:.1f}% return) | {p.exposure}")
            ax1.legend(loc="upper left", fontsize=8, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
            ax1.grid(True, which="major", alpha=0.3, color="#252a3a")
            ax1.grid(True, which="minor", alpha=0.15, color="#252a3a")

            ax2.plot(best["equity"].index, best["equity"], label="Strategy Equity", color="#6495ED", linewidth=1)
            if show_ratio:
                ax2.plot(best["buyhold"].index, best["buyhold"], label="Buy & Hold", color="#8890a4", linewidth=1, alpha=0.7)
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
                ax3.set_yscale("log")
                ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.2f}" if x < 1 else f"{x:,.0f}"))
                ax3.yaxis.set_minor_formatter(_minor_usd_formatter(dollar=False))
                ax3.tick_params(axis='y', which='minor', labelsize=6)
                ax3.set_ylabel(f"Value in {asset_name}")
                ax3.legend(loc="upper left", fontsize=8, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
                ax3.grid(True, which="major", alpha=0.3, color="#252a3a")
                ax3.grid(True, which="minor", alpha=0.15, color="#252a3a")
                last_ax = ax3
            last_ax.set_xlabel("Date")
            last_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            last_ax.xaxis.set_major_locator(mdates.YearLocator(2))
            plt.tight_layout()

            buf = BytesIO()
            plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
            plt.close()
            buf.seek(0)
            chart_b64 = base64.b64encode(buf.read()).decode()

    price_json = _series_to_lw_json(df["close"]) if best else None
    ind1_json = _series_to_lw_json(best["ind1_series"]) if best and best.get("ind1_name") != "price" else "[]"
    ind2_json = _series_to_lw_json(best["ind2_series"]) if best else "[]"
    return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=table_rows, col_header=col_header,
                                  asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, stock_assets=STOCK_ASSETS, asset_starts_json=ASSET_STARTS, asset_logos=ASSET_LOGOS,
                                  hide_buyhold=(p.exposure == "short-cash"),
                                  price_json=price_json, ind1_json=ind1_json, ind2_json=ind2_json,
                                  ind1_label=best.get("ind1_label", "") if best else "", ind2_label=best.get("ind2_label", "") if best else "")


if __name__ == "__main__":
    print(f"Starting Strategy Analytics at http://localhost:5000 (assets: {', '.join(ASSET_NAMES)})")
    app.run(debug=False, port=5000)
