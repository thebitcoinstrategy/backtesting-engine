#!/usr/bin/env python3
"""Daily price fetcher — run via cron to keep PostgreSQL prices up to date.

Usage:
    # Cron (00:15 UTC daily):
    15 0 * * * /path/to/venv/bin/python /path/to/fetch_prices.py

Requires environment variables:
    PRICE_DB_URL    — PostgreSQL connection string
    COINGECKO_API_KEY — CoinGecko Demo API key (free tier)

Fetches latest daily close prices from:
    - CoinGecko API for crypto assets
    - yfinance for stocks, indices, commodities
"""

import base64
import hashlib
import hmac as hmac_mod
import json as _json_top
import logging
import os
import sys
import time

import pandas as pd
import requests

import price_db
from helpers import compute_ratio_prices

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.environ.get("FETCH_LOG_DIR", os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(LOG_DIR, "fetch_prices.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CoinGecko
# ---------------------------------------------------------------------------
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def fetch_coingecko(coin_id, days=2):
    """Fetch daily close prices from CoinGecko.

    Returns DataFrame with DatetimeIndex(UTC) + 'close' column, or empty DataFrame on failure.
    """
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "daily"}
    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    for attempt in range(3):
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            log.warning("CoinGecko 429 for %s, retrying in %ds...", coin_id, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        resp.raise_for_status()  # raise after all retries exhausted

    prices = resp.json().get("prices", [])
    if not prices:
        return pd.DataFrame(columns=["close"])

    df = pd.DataFrame(prices, columns=["time_ms", "close"])
    # CoinGecko daily snapshots are at 00:00 UTC = opening price of that day = previous day's close
    df["date"] = pd.to_datetime(df["time_ms"], unit="ms", utc=True).dt.normalize() - pd.Timedelta(days=1)
    df = df.drop_duplicates(subset="date", keep="last")
    df = df.set_index("date")[["close"]].sort_index()
    # Exclude today — the day hasn't closed yet
    today = pd.Timestamp.now(tz="UTC").normalize()
    df = df[df.index < today]
    return df


# ---------------------------------------------------------------------------
# yfinance
# ---------------------------------------------------------------------------
def fetch_yfinance(ticker, period="5d"):
    """Fetch daily close prices from Yahoo Finance.

    Returns DataFrame with DatetimeIndex(UTC) + 'close' column, or empty DataFrame on failure.
    """
    import yfinance as yf

    data = yf.download(ticker, period=period, auto_adjust=True, progress=False)
    if data.empty:
        return pd.DataFrame(columns=["close"])

    # yfinance may return MultiIndex columns for single ticker — flatten
    if hasattr(data.columns, "levels"):
        data.columns = data.columns.get_level_values(0)

    df = data[["Close"]].rename(columns={"Close": "close"})
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index = df.index.normalize()
    df.index.name = "date"
    # Exclude today — the day hasn't closed yet
    today = pd.Timestamp.now(tz="UTC").normalize()
    df = df[df.index < today]
    return df


# ---------------------------------------------------------------------------
# FRED (Federal Reserve Economic Data)
# ---------------------------------------------------------------------------
def fetch_fred(series_id):
    """Fetch a FRED economic series and interpolate to daily frequency.

    FRED series are typically monthly or weekly. We fetch the full series
    (tiny — under 1000 points for M2SL) and linearly interpolate gaps so
    the backtesting engine sees a daily series like everything else.

    Returns DataFrame with DatetimeIndex(UTC) + 'close' column.
    """
    from io import StringIO

    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    r = requests.get(url, headers={"User-Agent": "BacktestingEngine/1.0"}, timeout=30)
    r.raise_for_status()

    raw = pd.read_csv(StringIO(r.text))
    date_col = raw.columns[0]
    val_col = raw.columns[1]
    raw[date_col] = pd.to_datetime(raw[date_col], utc=True)
    raw["close"] = pd.to_numeric(raw[val_col], errors="coerce")
    raw = raw[raw["close"].notna() & (raw["close"] > 0)]
    raw = raw.set_index(date_col).sort_index()

    if raw.empty:
        return pd.DataFrame(columns=["close"])

    daily_index = pd.date_range(raw.index[0], raw.index[-1], freq="D", tz="UTC")
    df = raw[["close"]].reindex(daily_index).interpolate(method="linear")
    df.index.name = "date"

    today = pd.Timestamp.now(tz="UTC").normalize()
    df = df[df.index < today]
    return df


# ---------------------------------------------------------------------------
# CoinGecko global market cap aggregates
# ---------------------------------------------------------------------------

# Maps source_id to how to compute market cap from /global data.
# Each entry is (description, compute_fn) where compute_fn takes (total, btc_cap, eth_cap, stablecoin_cap, top10_cap).
CRYPTO_AGG_FORMULAS = {
    "total":     lambda total, btc, eth, stable, top10: total,
    "total_es":  lambda total, btc, eth, stable, top10: total - stable,
    "total3":    lambda total, btc, eth, stable, top10: total - btc - eth,
    "total3_es": lambda total, btc, eth, stable, top10: total - btc - eth - stable,
    "others":    lambda total, btc, eth, stable, top10: total - top10,
}


def fetch_crypto_aggregates():
    """Fetch current crypto market cap aggregates from CoinGecko /global + /coins/categories.

    Returns dict mapping source_id -> DataFrame with single row (yesterday's date + close value),
    or empty dict on failure. Uses yesterday's date since market cap is a snapshot, not a daily close.
    """
    headers = {"User-Agent": "BacktestingEngine/1.0"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    # 1. Get total market cap and top coin percentages
    resp = requests.get(f"{COINGECKO_BASE}/global", headers=headers, timeout=15)
    resp.raise_for_status()
    gdata = resp.json().get("data", {})
    total = gdata.get("total_market_cap", {}).get("usd", 0)
    if not total:
        return {}

    pcts = gdata.get("market_cap_percentage", {})
    btc_cap = total * pcts.get("btc", 0) / 100
    eth_cap = total * pcts.get("eth", 0) / 100
    # Top 10 by market cap percentage
    sorted_pcts = sorted(pcts.values(), reverse=True)
    top10_pct = sum(sorted_pcts[:10])
    top10_cap = total * top10_pct / 100

    # 2. Get stablecoin market cap from categories
    time.sleep(5)
    resp2 = requests.get(f"{COINGECKO_BASE}/coins/categories", headers=headers, timeout=15)
    resp2.raise_for_status()
    stablecoin_cap = 0
    for cat in resp2.json():
        if cat.get("name") == "Stablecoins" and cat.get("market_cap"):
            stablecoin_cap = cat["market_cap"]
            break

    # 3. Compute each aggregate and return as single-row DataFrames
    yesterday = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1)
    result = {}
    for source_id, formula in CRYPTO_AGG_FORMULAS.items():
        value = formula(total, btc_cap, eth_cap, stablecoin_cap, top10_cap)
        df = pd.DataFrame({"close": [value]}, index=pd.DatetimeIndex([yesterday], name="date"))
        result[source_id] = df

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SIGNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_asset_signal")


def _send_test_signal_email():
    """Send a test signal email to admin for visual verification."""
    import uuid
    import database as db
    from helpers import send_email

    admin_email = db.ADMIN_EMAIL
    dummy_token = str(uuid.uuid4())
    html_body = _build_signal_email_html(
        signal='BUY',
        asset_display='Bitcoin',
        ind1_label='SMA(50)',
        ind2_label='SMA(200)',
        backtest_id='test-000',
        backtest_title='Golden Cross — BTC/USD',
        unsubscribe_token=dummy_token,
        user_id='admin',
        user_email=admin_email,
    )
    send_email(admin_email, 'TEST — BUY Signal: Bitcoin — SMA(50) / SMA(200)', html_body)
    log.info("Test signal email sent to %s", admin_email)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch latest daily prices")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Backfill N days of history (e.g., --backfill 30)")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a test signal email to admin and exit")
    args = parser.parse_args()

    if args.test_email:
        _send_test_signal_email()
        return

    cg_days = max(args.backfill, 2)
    yf_period = f"{max(args.backfill, 5)}d" if args.backfill > 5 else "5d"
    mode = f"backfill ({args.backfill} days)" if args.backfill else "daily"

    log.info("Starting price fetch (mode=%s)...", mode)

    price_db.init_db()
    assets = price_db.get_all_asset_metadata()
    log.info("Found %d assets in database", len(assets))

    updated = 0
    errors = 0

    # Group assets by source to maintain proper rate limiting
    coingecko_assets = [a for a in assets if a["source"] == "coingecko" and a["source_id"]]
    yfinance_assets = [a for a in assets if a["source"] == "yfinance" and a["source_id"]]

    # Fetch yfinance first (no strict rate limit)
    log.info("Fetching %d yfinance assets (period=%s)...", len(yfinance_assets), yf_period)
    for asset in yfinance_assets:
        try:
            df = fetch_yfinance(asset["source_id"], period=yf_period)
            if df.empty:
                log.warning("No data returned for %s (yfinance:%s)", asset["name"], asset["source_id"])
                continue
            price_db.upsert_prices(asset["id"], df)
            log.info("Updated %s: %d rows, latest=%s", asset["name"], len(df), df.index[-1].date())
            updated += 1
        except Exception:
            log.exception("Failed to fetch %s (yfinance:%s)", asset["name"], asset["source_id"])
            errors += 1

    # Fetch CoinGecko with proper rate limiting
    log.info("Fetching %d CoinGecko assets (days=%d)...", len(coingecko_assets), cg_days)
    for asset in coingecko_assets:
        try:
            df = fetch_coingecko(asset["source_id"], days=cg_days)
            if df.empty:
                log.warning("No data returned for %s (coingecko:%s)", asset["name"], asset["source_id"])
                continue
            price_db.upsert_prices(asset["id"], df)
            log.info("Updated %s: %d rows, latest=%s", asset["name"], len(df), df.index[-1].date())
            updated += 1
        except Exception:
            log.exception("Failed to fetch %s (coingecko:%s)", asset["name"], asset["source_id"])
            errors += 1
        time.sleep(5)  # conservative rate limit (safe without API key too)

    # Fetch FRED economic series (M2 money supply, etc.)
    fred_assets = [a for a in assets if a["source"] == "fred" and a["source_id"]]
    if fred_assets:
        log.info("Fetching %d FRED assets...", len(fred_assets))
        for asset in fred_assets:
            try:
                df = fetch_fred(asset["source_id"])
                if df.empty:
                    log.warning("No data returned for %s (fred:%s)", asset["name"], asset["source_id"])
                    continue
                price_db.upsert_prices(asset["id"], df)
                log.info("Updated %s: %d rows, latest=%s", asset["name"], len(df), df.index[-1].date())
                updated += 1
            except Exception:
                log.exception("Failed to fetch %s (fred:%s)", asset["name"], asset["source_id"])
                errors += 1

    # Fetch crypto market cap aggregates (Total, Total3, Others, etc.)
    agg_assets = [a for a in assets if a["source"] == "coingecko_global" and a.get("source_id")]
    if agg_assets:
        log.info("Fetching %d crypto aggregate assets...", len(agg_assets))
        try:
            agg_data = fetch_crypto_aggregates()
            for asset in agg_assets:
                sid = asset["source_id"]
                if sid in agg_data and not agg_data[sid].empty:
                    price_db.upsert_prices(asset["id"], agg_data[sid])
                    log.info("Updated %s: latest=%s, value=%.0f",
                             asset["name"], agg_data[sid].index[-1].date(),
                             agg_data[sid]["close"].iloc[-1])
                    updated += 1
                else:
                    log.warning("No aggregate data for %s (source_id=%s)", asset["name"], sid)
        except Exception:
            log.exception("Failed to fetch crypto aggregates")
            errors += 1

    # Signal Flask workers to reload ASSETS
    try:
        with open(SIGNAL_FILE, "w") as f:
            f.write(str(time.time()))
        log.info("Touched asset signal file")
    except OSError:
        log.warning("Could not touch signal file: %s", SIGNAL_FILE)

    log.info("Done. Updated %d assets, %d errors.", updated, errors)

    # --- Signal check: send Telegram notifications for position changes ---
    try:
        check_and_send_signals()
    except Exception:
        log.exception("Signal check failed")

    if errors:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Telegram Signal Notifications
# ---------------------------------------------------------------------------

def _generate_signal_chart(df, ind1_series, ind2_series, ind1_label, ind2_label,
                           buy_dates, sell_dates, asset_name, signal):
    """Generate a 3-month signal chart and return PNG bytes, or None on failure."""
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import backtest as bt

        # Trim to last 3 months for display
        three_months_ago = df.index[-1] - pd.Timedelta(days=90)
        df_plot = df[df.index >= three_months_ago]
        ind1_plot = ind1_series[ind1_series.index >= three_months_ago]
        ind2_plot = ind2_series[ind2_series.index >= three_months_ago]

        t = bt._get_theme("dark")
        fig, ax = plt.subplots(figsize=(12, 6), dpi=150)
        bt._apply_dark_theme(fig, [ax], "dark")

        ax.plot(df_plot.index, df_plot["close"], label=f"{asset_name} Price",
                color=t["price"], linewidth=1.2)
        ax.plot(ind1_plot.index, ind1_plot, label=ind1_label,
                color=t["accent"], linewidth=1.1, alpha=0.9)
        ax.plot(ind2_plot.index, ind2_plot, label=ind2_label,
                color=t["blue"], linewidth=1.1, alpha=0.9)

        for b in buy_dates:
            if b in df_plot.index:
                ax.annotate("BUY", xy=(b, df_plot.loc[b, "close"]),
                            fontsize=9, fontweight="bold", color="#00ff88",
                            ha="center", va="bottom",
                            xytext=(0, 18), textcoords="offset points",
                            arrowprops=dict(arrowstyle="->", color="#00ff88", lw=1.5))

        for s in sell_dates:
            if s in df_plot.index:
                ax.annotate("SELL", xy=(s, df_plot.loc[s, "close"]),
                            fontsize=9, fontweight="bold", color="#ff4444",
                            ha="center", va="top",
                            xytext=(0, -18), textcoords="offset points",
                            arrowprops=dict(arrowstyle="->", color="#ff4444", lw=1.5))

        ax.set_title(f"{asset_name} — {ind1_label} / {ind2_label} — {signal} Signal",
                     fontsize=13, color=t["text"])
        ax.set_ylabel("Price (USD)", color=t["text"])
        ax.yaxis.set_major_formatter(plt.FuncFormatter(
            lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
        ax.legend(loc="upper right", fontsize=9, facecolor=t["panel"],
                  edgecolor=t["grid"], labelcolor=t["text"])
        ax.grid(True, which="major", alpha=0.3, color=t["grid"])
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close()
        buf.seek(0)
        return buf.read()
    except Exception:
        log.exception("Failed to generate signal chart for %s", asset_name)
        return None


def _compute_signal(params, bt_id, bt_module, price_db_module):
    """Compute signal for a backtest. Returns dict with signal info or None if no signal.
    Shared by both Telegram and email alert processing."""
    import json as _json

    asset = params.get('asset', 'bitcoin')
    vs_asset = params.get('vs_asset', '')

    df = price_db_module.get_asset_df(asset)
    if df.empty or len(df) < 2:
        return None

    if vs_asset:
        df_vs = price_db_module.get_asset_df(vs_asset)
        if not df_vs.empty:
            try:
                df = compute_ratio_prices(df, df_vs)
            except ValueError:
                return None

    ind1_name = params.get('ind1_name', 'price')
    ind2_name = params.get('ind2_name', 'sma')
    ind1_period = int(params.get('period1', 0) or 0) or None
    ind2_period = int(params.get('period2', 0) or 0) or None
    exposure = params.get('exposure', 'long-cash')
    reverse = params.get('reverse', '') in ('true', 'True', '1', True)
    start_date = params.get('start_date', None)

    result = bt_module.run_strategy(
        df, ind1_name, ind1_period, ind2_name, ind2_period,
        initial_cash=10000, exposure=exposure, reverse=reverse,
        start_date=start_date
    )

    # Detect crossover (no shift — detect on the day it happens)
    ind1_s = result['ind1_series']
    ind2_s = result['ind2_series']
    above = ind1_s > ind2_s
    if reverse:
        above = ~above
    position = bt_module._apply_exposure(above, exposure).fillna(0)
    position[ind1_s.isna() | ind2_s.isna()] = 0

    if len(position) < 2:
        return None

    pos_today = position.iloc[-1]
    pos_yesterday = position.iloc[-2]

    if pos_today == pos_yesterday:
        return None

    signal = "BUY" if pos_today > pos_yesterday else "SELL"

    # Chart markers
    above_chart = ind1_s > ind2_s
    if reverse:
        above_chart = ~above_chart
    pos_chart = bt_module._apply_exposure(above_chart, exposure).fillna(0)
    pos_chart[ind1_s.isna() | ind2_s.isna()] = 0
    diff_chart = pos_chart.diff()
    chart_buys = diff_chart[diff_chart > 0].index
    chart_sells = diff_chart[diff_chart < 0].index

    asset_display = asset.replace('-', ' ').title()
    chart_png = _generate_signal_chart(
        df, ind1_s, ind2_s, result['ind1_label'], result['ind2_label'],
        chart_buys, chart_sells, asset_display, signal
    )

    return {
        'signal': signal,
        'asset': asset,
        'asset_display': asset_display,
        'result': result,
        'params': params,
        'chart_png': chart_png,
        'bt_id': bt_id,
    }


def _generate_email_login_token(user_id, email):
    """Generate an HMAC-signed login token for use in email links.
    30-day expiry, no nonce (replayable), purpose-scoped to 'email_login'."""
    secret = os.environ.get('ANALYTICS_SHARED_SECRET', '')
    if not secret:
        return ''
    payload = {
        'user_id': str(user_id),
        'email': email,
        'exp': int(time.time()) + 30 * 86400,
        'purpose': 'email_login',
    }
    payload_bytes = _json_top.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    sig = hmac_mod.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    payload['sig'] = sig
    token = base64.urlsafe_b64encode(_json_top.dumps(payload).encode()).decode().rstrip('=')
    return token


def _build_signal_email_html(signal, asset_display, ind1_label, ind2_label,
                              backtest_id, backtest_title, unsubscribe_token,
                              user_id=None, user_email=None):
    """Build the HTML body for a signal alert email."""
    SITE_URL = 'https://analytics.the-bitcoin-strategy.com'
    link = f"{SITE_URL}/backtest/{backtest_id}?view=livechart"
    unsub_link = f"{SITE_URL}/unsubscribe/{unsubscribe_token}"
    # Generate login token so "Manage all alerts" link auto-authenticates
    if user_id and user_email:
        login_token = _generate_email_login_token(user_id, user_email)
        account_link = f"{SITE_URL}/account?token={login_token}" if login_token else f"{SITE_URL}/account"
    else:
        account_link = f"{SITE_URL}/account"

    signal_color = '#34d399' if signal == 'BUY' else '#f87171'
    signal_bg = 'rgba(52,211,153,0.15)' if signal == 'BUY' else 'rgba(248,113,113,0.15)'
    signal_icon = '&#x25B2;' if signal == 'BUY' else '&#x25BC;'
    title = backtest_title or f"{asset_display} Strategy"
    from datetime import datetime
    date_str = datetime.utcnow().strftime('%B %d, %Y')

    return f"""\
<div style="max-width:600px;margin:0 auto;font-family:'Helvetica Neue',Arial,sans-serif;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden;border:1px solid #30363d">
    <div style="background:#161b22;padding:24px 32px;border-bottom:1px solid #30363d;text-align:center">
        <div style="font-size:20px;font-weight:700;letter-spacing:-0.02em;display:inline-block">
            <span style="background:linear-gradient(135deg,#6495ED,#4a7dd6);color:#fff;padding:4px 10px;display:inline-block">Bitcoin</span><span style="background:#1c2030;color:#e8eaf0;padding:4px 10px;display:inline-block;border:1px solid #30363d;border-left:none">Strategy Analytics</span>
        </div>
    </div>

    <div style="padding:32px">
        <div style="text-align:center;margin-bottom:24px">
            <div style="display:inline-block;background:{signal_bg};color:{signal_color};font-size:28px;font-weight:700;padding:12px 32px;border-radius:12px;letter-spacing:0.05em">
                {signal_icon} {signal}
            </div>
        </div>

        <h2 style="font-size:18px;margin:0 0 8px;text-align:center">{title}</h2>
        <p style="color:#8b949e;text-align:center;margin:0 0 24px;font-size:14px">{date_str}</p>

        <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:24px">
            <table style="width:100%;border-collapse:collapse;font-size:14px">
                <tr><td style="color:#8b949e;padding:6px 0">Asset</td><td style="text-align:right;font-weight:600">{asset_display}</td></tr>
                <tr><td style="color:#8b949e;padding:6px 0">Signal</td><td style="text-align:right;font-weight:600;color:{signal_color}">{signal}</td></tr>
                <tr><td style="color:#8b949e;padding:6px 0">Indicator 1</td><td style="text-align:right;font-weight:600">{ind1_label}</td></tr>
                <tr><td style="color:#8b949e;padding:6px 0">Indicator 2</td><td style="text-align:right;font-weight:600">{ind2_label}</td></tr>
            </table>
        </div>

        <div style="margin-bottom:24px;text-align:center">
            <img src="cid:signal_chart" alt="Signal Chart" style="max-width:100%;border-radius:8px;border:1px solid #30363d">
        </div>

        <div style="text-align:center;margin-bottom:24px">
            <a href="{link}" style="display:inline-block;background:#6495ED;color:#fff;text-decoration:none;padding:12px 32px;border-radius:8px;font-weight:600;font-size:15px">View Live Chart</a>
        </div>
    </div>

    <div style="background:#161b22;padding:24px 32px;border-top:1px solid #30363d;text-align:center">
        <p style="margin:0 0 16px;font-size:13px;color:#8b949e">You're receiving this because you enabled email alerts for this strategy.</p>
        <div style="margin-bottom:12px">
            <a href="{unsub_link}" style="display:inline-block;background:transparent;color:#f87171;text-decoration:none;padding:10px 28px;border-radius:6px;font-weight:600;font-size:14px;border:1px solid #f87171">Unsubscribe from this alert</a>
        </div>
        <p style="margin:0;font-size:13px"><a href="{account_link}" style="color:#58a6ff;text-decoration:none">Manage all alerts</a></p>
    </div>
</div>"""


def check_and_send_signals():
    """Check all telegram-enabled backtests and email alerts for position changes and send signals."""
    import json as _json
    import urllib.request

    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_SIGNAL_CHAT_ID = os.environ.get('TELEGRAM_SIGNAL_CHAT_ID', '')
    # Support comma-separated list so signals can be mirrored to multiple channels.
    _SIGNAL_CHAT_IDS = [c.strip() for c in TELEGRAM_SIGNAL_CHAT_ID.split(',') if c.strip()]
    SITE_URL = 'https://analytics.the-bitcoin-strategy.com'

    # Import backtest engine and database
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    import backtest as bt
    import database as db

    db.init_db()

    # --- Telegram signal alerts ---
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_SIGNAL_CHAT_ID:
        log.info("TELEGRAM_BOT_TOKEN or TELEGRAM_SIGNAL_CHAT_ID not set, skipping Telegram signal check")
    else:
        rows = db.list_telegram_enabled_backtests()
        if not rows:
            log.info("No telegram-enabled backtests found")
        else:
            log.info("Checking %d telegram-enabled backtests for signals...", len(rows))

            for row in rows:
                try:
                    params = _json.loads(row.get('params', '{}') or '{}')
                    sig_info = _compute_signal(params, row['id'], bt, price_db)
                    if not sig_info:
                        continue

                    signal = sig_info['signal']
                    asset = sig_info['asset']
                    asset_display = sig_info['asset_display']
                    result = sig_info['result']
                    chart_png = sig_info['chart_png']

                    # Build link to live chart
                    link = f"{SITE_URL}/backtest/{row['id']}?view=livechart"

                    # Format message from template
                    default_template = (
                        '\u26a0\ufe0f This is a <b>{signal}</b> Signal for {asset}.\n'
                        '\n'
                        'We are changing our position for {asset} since the moving averages have crossed: {ind1} / {ind2}\n'
                        '\n'
                        '{if_buy}For long signals, we use {long_lev}x leverage in {asset}.{/if_buy}'
                        '{if_sell}For short signals, we use {short_lev}x leverage in {asset}.{/if_sell}\n'
                        '\n'
                        '<a href="{link}">View Live Chart</a>'
                    )
                    template = row.get('telegram_message_template') or default_template
                    long_lev = params.get('long_leverage', '1')
                    short_lev = params.get('short_leverage', '1')

                    # Process conditionals
                    import re as _re
                    if signal == 'BUY':
                        template = _re.sub(r'\{if_buy\}(.*?)\{/if_buy\}', r'\1', template, flags=_re.DOTALL)
                        template = _re.sub(r'\{if_sell\}.*?\{/if_sell\}', '', template, flags=_re.DOTALL)
                    else:
                        template = _re.sub(r'\{if_sell\}(.*?)\{/if_sell\}', r'\1', template, flags=_re.DOTALL)
                        template = _re.sub(r'\{if_buy\}.*?\{/if_buy\}', '', template, flags=_re.DOTALL)

                    message = template.format(
                        asset=asset_display,
                        signal=signal,
                        ind1=result['ind1_label'],
                        ind2=result['ind2_label'],
                        long_lev=long_lev,
                        short_lev=short_lev,
                        link=link
                    )

                    # Truncate caption to Telegram's 1024 char limit
                    caption = message[:1024] if len(message) > 1024 else message

                    # Send to each configured channel — one failure doesn't block the others.
                    for _chat_id in _SIGNAL_CHAT_IDS:
                        sent = False
                        if chart_png:
                            try:
                                import requests as _requests
                                url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto'
                                resp = _requests.post(url, data={
                                    'chat_id': _chat_id,
                                    'caption': caption,
                                    'parse_mode': 'HTML',
                                }, files={
                                    'photo': ('signal_chart.png', chart_png, 'image/png')
                                }, timeout=30)
                                resp.raise_for_status()
                                sent = True
                                log.info("Sent %s signal with chart to %s for backtest %s (%s)", signal, _chat_id, row['id'], asset)
                            except Exception:
                                log.exception("sendPhoto failed for %s -> %s, falling back to text", asset, _chat_id)

                        if not sent:
                            try:
                                url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
                                payload = _json.dumps({
                                    'chat_id': _chat_id,
                                    'text': message,
                                    'parse_mode': 'HTML',
                                    'disable_web_page_preview': False
                                }).encode('utf-8')
                                req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
                                urllib.request.urlopen(req, timeout=15)
                                log.info("Sent %s signal (text-only) to %s for backtest %s (%s)", signal, _chat_id, row['id'], asset)
                            except Exception:
                                log.exception("sendMessage failed for %s -> %s", asset, _chat_id)

                except Exception:
                    log.exception("Failed to check signals for backtest %s", row.get('id', '?'))

    # --- Email signal alerts ---
    try:
        all_alerts = db.list_active_email_alerts_grouped()
        if not all_alerts:
            log.info("No active email alerts found")
        else:
            log.info("Checking %d email alert subscriptions...", len(all_alerts))
            from collections import defaultdict
            alerts_by_bt = defaultdict(list)
            for alert in all_alerts:
                alerts_by_bt[alert['backtest_id']].append(alert)

            email_batch = []

            for bt_id, user_alerts in alerts_by_bt.items():
                try:
                    row = user_alerts[0]
                    params = _json.loads(row.get('backtest_params', '{}') or '{}')
                    sig_info = _compute_signal(params, bt_id, bt, price_db)
                    if not sig_info:
                        continue

                    signal = sig_info['signal']
                    asset_display = sig_info['asset_display']
                    result = sig_info['result']
                    chart_png = sig_info['chart_png']

                    for alert in user_alerts:
                        try:
                            html_body = _build_signal_email_html(
                                signal=signal,
                                asset_display=asset_display,
                                ind1_label=result['ind1_label'],
                                ind2_label=result['ind2_label'],
                                backtest_id=bt_id,
                                backtest_title=alert.get('backtest_title', ''),
                                unsubscribe_token=alert['unsubscribe_token'],
                                user_id=alert['user_id'],
                                user_email=alert['user_email']
                            )
                            attachments = []
                            if chart_png:
                                attachments.append({
                                    'content': chart_png,
                                    'content_type': 'image/png',
                                    'filename': 'signal_chart.png',
                                    'content_id': 'signal_chart'
                                })
                            email_batch.append({
                                'to': alert['user_email'],
                                'subject': f"{signal} Signal: {asset_display} \u2014 {result['ind1_label']} / {result['ind2_label']}",
                                'html_body': html_body,
                                'attachments': attachments or None,
                            })
                        except Exception:
                            log.exception("Failed to build email for alert %s", alert.get('alert_id', '?'))
                except Exception:
                    log.exception("Failed to process email alerts for backtest %s", bt_id)

            if email_batch:
                from helpers import send_emails_batch
                sent, failed = send_emails_batch(email_batch)
                log.info("Email alerts: %d sent, %d failed", sent, failed)
    except Exception:
        log.exception("Email alert check failed")


if __name__ == "__main__":
    main()
