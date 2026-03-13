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
LARAVEL_LOGIN_URL = 'https://the-bitcoin-strategy.com/app'
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
        raw = base64.urlsafe_b64decode(token)
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
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               background: #0f1117; color: #e0e0e0; min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        h1 { text-align: center; margin-bottom: 24px; color: #f7931a; font-size: 1.8em; }
        .layout { display: flex; flex-direction: column; gap: 24px; }
        .panel { background: #1a1d27; border-radius: 12px; padding: 24px; border: 1px solid #2a2d3a; }
        .form-group { margin-bottom: 12px; }
        .form-row { display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-end; }
        .form-row .form-group { flex: 1; min-width: 140px; margin-bottom: 0; }
        label { display: block; font-size: 0.85em; color: #9ca3af; margin-bottom: 4px; font-weight: 500; }
        input, select { width: 100%; padding: 8px 12px; border-radius: 6px; border: 1px solid #2a2d3a;
                        background: #0f1117; color: #e0e0e0; font-size: 0.95em; }
        input:focus, select:focus { outline: none; border-color: #f7931a; }
        .row { display: flex; gap: 12px; }
        .row .form-group { flex: 1; }
        button { width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 1em;
                 font-weight: 600; cursor: pointer; background: #f7931a; color: #0f1117;
                 margin-top: 8px; transition: background 0.2s; }
        button:hover { background: #e8850f; }
        button:disabled { background: #555; cursor: wait; }
        .chart-img { width: 100%; border-radius: 8px; }
        .results-table { width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 0.85em; }
        .results-table th, .results-table td { padding: 6px 10px; text-align: right; border-bottom: 1px solid #2a2d3a; }
        .results-table th { color: #9ca3af; font-weight: 500; }
        .results-table tr:hover { background: #22253a; }
        .best { color: #22c55e; font-weight: 600; }
        .placeholder { text-align: center; color: #555; padding: 80px 20px; font-size: 1.1em; }
        .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
        .stat { flex: 1; min-width: 120px; background: #0f1117; border-radius: 8px; padding: 12px; text-align: center; }
        .stat-value { font-size: 1.3em; font-weight: 700; color: #f7931a; }
        .stat-label { font-size: 0.75em; color: #9ca3af; margin-top: 2px; }
        .form-section { border: 1px solid #2a2d3a; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; }
        .section-title { font-size: 0.75em; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; font-weight: 600; }
        .sep { width: 1px; background: #2a2d3a; align-self: stretch; margin: 0 4px; flex: 0 0 1px; }
        .hidden { display: none !important; }
        .spinner { display: none; }
        .loading .spinner { display: inline-block; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
<div class="container">
    <h1><span style="background:#6495ED;color:#fff;font-weight:700;padding:4px 10px">Bitcoin</span><span style="background:#000;color:#fff;font-weight:700;padding:4px 10px">Strategy Analytics</span></h1>
    <div class="layout">
        <div class="panel">
            <form method="POST" id="form" onsubmit="document.getElementById('btn').disabled=true; document.getElementById('btn').textContent='Running...';">
                <div class="form-section">
                    <div class="section-title">Asset & Strategy</div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Asset</label>
                            <select name="asset" id="asset" onchange="onAssetChange()">
                                {% for a in priority_assets %}
                                <option value="{{ a }}" {{ 'selected' if p.asset==a }}>{{ a|capitalize }}</option>
                                {% endfor %}
                                {% if other_assets %}
                                <option disabled>──────────</option>
                                {% for a in other_assets %}
                                <option value="{{ a }}" {{ 'selected' if p.asset==a }}>{{ a|capitalize }}</option>
                                {% endfor %}
                                {% endif %}
                            </select>
                        </div>
                        <div class="sep"></div>
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
                        <div class="sep"></div>
                        <div class="form-group" id="exposure-group">
                            <label>Exposure</label>
                            <select name="exposure" id="exposure">
                                <option value="long-cash" {{ 'selected' if p.exposure=='long-cash' }}>Long + Cash</option>
                                <option value="short-cash" {{ 'selected' if p.exposure=='short-cash' }}>Short + Cash</option>
                                <option value="long-short" {{ 'selected' if p.exposure=='long-short' }}>Long + Short</option>
                            </select>
                        </div>
                    </div>
                    <div id="signal-explainer" style="margin-top:8px;font-size:0.8em;color:#6b7280;line-height:1.4">
                        Buy when <span style="color:#e0e0e0" id="explainer-ind1">Price</span> crosses above <span style="color:#e0e0e0" id="explainer-ind2">SMA</span>. Sell when it crosses below.
                    </div>
                </div>
                <div class="form-section">
                    <div class="section-title">Mode & Parameters</div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Mode</label>
                            <select name="mode" id="mode" onchange="toggleFields()">
                                <option value="backtest" {{ 'selected' if p.mode=='backtest' }}>Backtest</option>
                                <option value="sweep" {{ 'selected' if p.mode=='sweep' }}>Find best period</option>
                                <option value="heatmap" {{ 'selected' if p.mode=='heatmap' }}>Find best combo</option>
                                <option value="sweep-lev" {{ 'selected' if p.mode=='sweep-lev' }}>Find best leverage</option>
                            </select>
                        </div>
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
                    <div class="section-title">Leverage</div>
                    <div class="form-row">
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
                            <label>Leverage Mode</label>
                            <select name="lev_mode">
                                <option value="rebalance" {{ 'selected' if p.lev_mode=='rebalance' }}>Daily Rebalance</option>
                                <option value="set-forget" {{ 'selected' if p.lev_mode=='set-forget' }}>Set & Forget</option>
                            </select>
                        </div>
                        <input type="hidden" name="lev_step" value="0.25">
                    </div>
                </div>
                <div class="form-section">
                    <div class="section-title">Date Range & Capital</div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Start Date <button type="button" onclick="setAllData()"
                                    style="background:#2a2d3a;color:#e0e0e0;font-size:0.65em;padding:2px 6px;border:1px solid #444;border-radius:3px;cursor:pointer;margin-left:4px;vertical-align:middle">All data</button></label>
                            <input type="date" name="start_date" id="start_date" value="{{ p.start_date }}">
                        </div>
                        <div class="form-group">
                            <label>End Date</label>
                            <input type="date" name="end_date" value="{{ p.end_date }}">
                        </div>
                        <div class="form-group">
                            <label>Initial Cash</label>
                            <div style="position:relative">
                                <span style="position:absolute;left:8px;top:50%;transform:translateY(-50%);color:#9ca3af;font-size:0.9em">$</span>
                                <input type="number" name="initial_cash" value="{{ p.initial_cash }}" min="1" style="padding-left:20px">
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
        <div class="panel">
            {% if chart %}
                {% if best %}
                <table class="results-table" style="margin-bottom:16px">
                    <tr>
                        <th style="text-align:left">Strategy</th>
                        <th>Ann. Return</th>
                        <th>Max Drawdown</th>
                        <th>Trades</th>
                        {% if lev_sweep|default(none) %}<th>Leverage</th>{% endif %}
                    </tr>
                    {% if not lev_sweep|default(none) %}
                    <tr class="best">
                        <td style="text-align:left">{{ best.label }}</td>
                        <td>{{ "%.2f"|format(best.annualized) }}%</td>
                        <td>{{ "%.2f"|format(best.max_drawdown) }}%</td>
                        <td>{{ best.trades }}</td>
                    </tr>
                    {% endif %}
                    {% if not hide_buyhold|default(false) %}
                    <tr>
                        <td style="text-align:left">Buy & Hold</td>
                        <td>{{ "%.2f"|format(best.buyhold_annualized) }}%</td>
                        <td>{{ "%.2f"|format(best.buyhold_max_drawdown) }}%</td>
                        <td>1</td>
                        {% if lev_sweep|default(none) %}<td></td>{% endif %}
                    </tr>
                    {% endif %}
                    {% if lev_sweep|default(none) %}
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
                    {% endif %}
                </table>
                {% endif %}
                {% if table_rows %}
                <details style="margin-bottom:16px">
                    <summary style="cursor:pointer;color:#9ca3af;font-size:0.9em">Show all results ({{ table_rows|length }})</summary>
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
                <img class="chart-img" src="data:image/png;base64,{{ chart }}" />
            {% else %}
                <div class="placeholder">Configure parameters and press Run Backtest</div>
            {% endif %}
        </div>
    </div>
</div>
<script>
var assetStarts = {{ asset_starts_json|tojson }};
function toggleFields() {
    var mode = document.getElementById('mode').value;
    var ind1 = document.getElementById('ind1_name').value;
    var isLevSweep = mode === 'sweep-lev';
    var rules = [
        ['period1-group', ind1 !== 'price' && mode !== 'heatmap'],
        ['period2-group', mode === 'backtest' || mode === 'sweep-lev'],
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
document.getElementById('ind2_name').addEventListener('change', updateExplainer);
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
toggleFields();
{% if not chart %}document.getElementById('form').submit();{% endif %}
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
            self.range_max = int(form.get("range_max", 365))
            self.step = int(form.get("step", 5))
            self.exposure = form.get("exposure", "long-cash")
            if self.mode == "sweep-lev":
                self.exposure = "long-short"
            self.fee = float(form.get("fee", 0.1))
            self.long_leverage = float(form.get("long_leverage", 1))
            self.short_leverage = float(form.get("short_leverage", 1))
            self.lev_mode = form.get("lev_mode", "set-forget")
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
            self.range_max = 365
            self.step = 5
            self.exposure = "long-cash"
            self.fee = 0.1
            self.long_leverage = 1
            self.short_leverage = 1
            self.lev_mode = "set-forget"
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
PRIORITY_ASSETS = [a for a in _PRIORITY_ORDER if a in ASSETS]
OTHER_ASSETS = [a for a in ASSET_NAMES if a not in _PRIORITY_ORDER]
DEFAULT_ASSET = "bitcoin" if "bitcoin" in ASSETS else ASSET_NAMES[0]


def _enrich_best(result, df):
    """Add annualized return and buy-and-hold metrics to a result dict."""
    import numpy as np
    n_days = len(df)
    result["annualized"] = bt._annualized_return(result["total_return"], n_days)
    result["buyhold_annualized"] = bt._annualized_return(result["buyhold_return"], n_days)
    result["buyhold_max_drawdown"] = bt._max_drawdown(result["buyhold"])
    daily_return = df["close"].pct_change().fillna(0)
    mean_d = daily_return.mean()
    std_d = daily_return.std()
    result["buyhold_sharpe"] = (mean_d / std_d * np.sqrt(365)) if std_d > 0 else 0.0
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
        return render_template_string(HTML, p=Params(), chart=None, best=None, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, asset_starts_json=ASSET_STARTS)

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
        show_long = p.exposure in ("long-cash", "long-short")
        show_short = p.exposure in ("short-cash", "long-short")
        all_levs = []
        if show_long:
            ax.plot(long_levs, long_sweep, color="steelblue", linewidth=1.5, label="Long Leverage")
            ax.scatter([best_long_lev], [best_long_ann], color="steelblue", s=60, zorder=5)
            all_levs.extend(long_levs)
        if show_short:
            ax.plot(short_levs, short_sweep, color="darkorange", linewidth=1.5, label="Short Leverage")
            ax.scatter([best_short_lev], [best_short_ann], color="darkorange", s=60, zorder=5)
            all_levs.extend(short_levs)
        x_min, x_max = min(all_levs), max(all_levs)
        if p.exposure != "short-cash":
            ax.plot([x_min, x_max], [bh_ann, bh_ann], color="gray", linestyle="--", linewidth=1,
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
        ax.legend(loc="best", fontsize=9)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png")
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
        return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, asset_starts_json=ASSET_STARTS,
                                      hide_buyhold=(p.exposure == "short-cash"), lev_sweep=lev_sweep_info)

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
        cbar.set_label("Annualized Return (%)")
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
        plt.savefig(buf, format="png")
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        best_result = bt.run_strategy(df, ind1_name, best_p1, ind2_name, best_p2,
                                       p.initial_cash, fee, p.exposure, p.long_leverage, p.short_leverage, p.lev_mode)
        best = _enrich_best(best_result, df)

        return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=None, col_header=col_header,
                                      asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, asset_starts_json=ASSET_STARTS,
                                      hide_buyhold=(p.exposure == "short-cash"))

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
        ax.plot(periods, annualized_returns, color="steelblue", linewidth=1)
        if p.exposure != "short-cash":
            ax.axhline(y=bh_annualized, color="gray", linestyle="--", linewidth=1,
                        label=f"Buy & Hold ({bh_annualized:.1f}%)")
        ax.scatter([best_period], [best_ann], color="red", s=60, zorder=5,
                    label=f"Best: {best_label} ({best_ann:.1f}%)")
        ax.set_xlabel(f"{ind2_upper} Period (days)")
        ax.set_ylabel("Annualized Return (%)")
        asset_title = p.asset.capitalize()
        title_prefix = f"{ind1_label_str} vs " if p.ind1_name != "price" else ""
        ax.set_title(f"{asset_title} \u2014 Annualized Return by {title_prefix}{ind2_upper} Period ({p.range_min}-{p.range_max}) | {p.exposure}")
        ax.legend(loc="best", fontsize=9)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png")
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
            else:
                fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), dpi=150,
                                                gridspec_kw={"height_ratios": [7, 3]}, sharex=True)

            ax1.plot(df.index, df["close"], label=f"{asset_name} Price", color="black", linewidth=0.8)

            # Plot ind2 (main/slow indicator)
            ax1.plot(best["ind2_series"].index, best["ind2_series"],
                     label=best["ind2_label"], color="blue", linewidth=0.8, alpha=0.8)
            # Plot ind1 if not price
            if best.get("ind1_name") != "price":
                ax1.plot(best["ind1_series"].index, best["ind1_series"],
                         label=best["ind1_label"], color="orange", linewidth=0.8, alpha=0.8)

            ax1.set_yscale("log")
            _fmt_usd = plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}")
            ax1.yaxis.set_major_formatter(_fmt_usd)
            ax1.yaxis.set_minor_formatter(_minor_usd_formatter())
            ax1.tick_params(axis='y', which='minor', labelsize=6)
            ax1.set_ylabel(f"{asset_name} Price (log scale)")
            ax1.set_title(f"{asset_name} Backtest \u2014 Best: {best['label']} "
                          f"({best['total_return']:.1f}% return) | {p.exposure}")
            ax1.legend(loc="upper left", fontsize=8)
            ax1.grid(True, which="major", alpha=0.3)
            ax1.grid(True, which="minor", alpha=0.15)

            ax2.plot(best["equity"].index, best["equity"], label="Strategy Equity", color="blue", linewidth=1)
            if show_ratio:
                ax2.plot(best["buyhold"].index, best["buyhold"], label="Buy & Hold", color="gray", linewidth=1, alpha=0.7)
            ax2.set_yscale("log")
            ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
            ax2.yaxis.set_minor_formatter(_minor_usd_formatter())
            ax2.tick_params(axis='y', which='minor', labelsize=6)
            ax2.set_ylabel("Portfolio Value (log)")
            ax2.legend(loc="upper left", fontsize=8)
            ax2.grid(True, which="major", alpha=0.3)
            ax2.grid(True, which="minor", alpha=0.15)

            last_ax = ax2
            if show_ratio:
                ratio = best["equity"] / best["buyhold"].replace(0, np.nan)
                ratio_normalized = ratio / ratio.dropna().iloc[0] * 100
                ax3.plot(ratio_normalized.index, ratio_normalized, color="purple", linewidth=1, label=f"Strategy in {asset_name}")
                ax3.axhline(y=100, color="gray", linestyle="--", linewidth=0.8, alpha=0.7)
                ax3.set_yscale("log")
                ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.2f}" if x < 1 else f"{x:,.0f}"))
                ax3.yaxis.set_minor_formatter(_minor_usd_formatter(dollar=False))
                ax3.tick_params(axis='y', which='minor', labelsize=6)
                ax3.set_ylabel(f"Value in {asset_name}")
                ax3.legend(loc="upper left", fontsize=8)
                ax3.grid(True, which="major", alpha=0.3)
                ax3.grid(True, which="minor", alpha=0.15)
                last_ax = ax3
            last_ax.set_xlabel("Date")
            last_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            last_ax.xaxis.set_major_locator(mdates.YearLocator(2))
            plt.tight_layout()

            buf = BytesIO()
            plt.savefig(buf, format="png")
            plt.close()
            buf.seek(0)
            chart_b64 = base64.b64encode(buf.read()).decode()

    return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=table_rows, col_header=col_header,
                                  asset_names=ASSET_NAMES, priority_assets=PRIORITY_ASSETS, other_assets=OTHER_ASSETS, asset_starts_json=ASSET_STARTS,
                                  hide_buyhold=(p.exposure == "short-cash"))


if __name__ == "__main__":
    print(f"Starting Strategy Analytics at http://localhost:5000 (assets: {', '.join(ASSET_NAMES)})")
    app.run(debug=False, port=5000)
