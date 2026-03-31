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
# Main
# ---------------------------------------------------------------------------
SIGNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_asset_signal")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch latest daily prices")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Backfill N days of history (e.g., --backfill 30)")
    args = parser.parse_args()

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

def check_and_send_signals():
    """Check all telegram-enabled backtests for position changes and send signals."""
    import json as _json
    import urllib.request

    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_SIGNAL_CHAT_ID = os.environ.get('TELEGRAM_SIGNAL_CHAT_ID', '')
    SITE_URL = 'https://analytics.the-bitcoin-strategy.com'

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_SIGNAL_CHAT_ID:
        log.info("TELEGRAM_BOT_TOKEN or TELEGRAM_SIGNAL_CHAT_ID not set, skipping signal check")
        return

    # Import backtest engine and database
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    import backtest as bt
    import database as db

    db.init_db()
    rows = db.list_telegram_enabled_backtests()

    if not rows:
        log.info("No telegram-enabled backtests found")
        return

    log.info("Checking %d telegram-enabled backtests for signals...", len(rows))

    for row in rows:
        try:
            params = _json.loads(row.get('params', '{}') or '{}')
            asset = params.get('asset', 'bitcoin')
            vs_asset = params.get('vs_asset', '')

            # Load price data from PostgreSQL
            df = price_db.get_asset_df(asset)
            if df.empty or len(df) < 2:
                log.warning("Insufficient data for %s, skipping", asset)
                continue

            # Handle vs_asset (ratio mode)
            if vs_asset:
                df_vs = price_db.get_asset_df(vs_asset)
                if not df_vs.empty:
                    try:
                        df = compute_ratio_prices(df, df_vs)
                    except ValueError:
                        log.warning("No overlapping dates for %s / %s", asset, vs_asset)
                        continue

            # Extract strategy params
            ind1_name = params.get('ind1_name', 'price')
            ind2_name = params.get('ind2_name', 'sma')
            ind1_period = int(params.get('period1', 0) or 0) or None
            ind2_period = int(params.get('period2', 0) or 0) or None
            exposure = params.get('exposure', 'long-cash')
            reverse = params.get('reverse', '') in ('true', 'True', '1', True)
            start_date = params.get('start_date', None)

            # Run strategy
            result = bt.run_strategy(
                df, ind1_name, ind1_period, ind2_name, ind2_period,
                initial_cash=10000, exposure=exposure, reverse=reverse,
                start_date=start_date
            )

            # Recompute position to compare last two days
            # NOTE: Don't apply .shift(1) here — the shift is for backtesting
            # (trade the day after signal) but for notifications we want to
            # detect the crossover on the day it happens.
            ind1_s = result['ind1_series']
            ind2_s = result['ind2_series']
            above = ind1_s > ind2_s
            if reverse:
                above = ~above
            position = bt._apply_exposure(above, exposure).fillna(0)
            nan_mask = ind1_s.isna() | ind2_s.isna()
            position[nan_mask] = 0

            if len(position) < 2:
                continue

            pos_today = position.iloc[-1]
            pos_yesterday = position.iloc[-2]

            if pos_today == pos_yesterday:
                continue  # No signal change

            signal = "BUY" if pos_today > pos_yesterday else "SELL"

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
                asset=asset.replace('-', ' ').title(),
                signal=signal,
                ind1=result['ind1_label'],
                ind2=result['ind2_label'],
                long_lev=long_lev,
                short_lev=short_lev,
                link=link
            )

            # Send via Telegram
            url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
            payload = _json.dumps({
                'chat_id': TELEGRAM_SIGNAL_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': False
            }).encode('utf-8')
            req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=15)
            log.info("Sent %s signal for backtest %s (%s)", signal, row['id'], asset)

        except Exception:
            log.exception("Failed to check signals for backtest %s", row.get('id', '?'))


if __name__ == "__main__":
    main()
