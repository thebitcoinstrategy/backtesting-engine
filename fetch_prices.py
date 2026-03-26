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


def fetch_coingecko(coin_id):
    """Fetch last 2 days of daily close prices from CoinGecko.

    Returns DataFrame with DatetimeIndex(UTC) + 'close' column, or empty DataFrame on failure.
    """
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": 2, "interval": "daily"}
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
    df["date"] = pd.to_datetime(df["time_ms"], unit="ms", utc=True).dt.normalize()
    df = df.drop_duplicates(subset="date", keep="last")
    df = df.set_index("date")[["close"]].sort_index()
    return df


# ---------------------------------------------------------------------------
# yfinance
# ---------------------------------------------------------------------------
def fetch_yfinance(ticker):
    """Fetch last 5 trading days from Yahoo Finance.

    Returns DataFrame with DatetimeIndex(UTC) + 'close' column, or empty DataFrame on failure.
    """
    import yfinance as yf

    data = yf.download(ticker, period="5d", auto_adjust=True, progress=False)
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
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SIGNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_asset_signal")


def main():
    log.info("Starting daily price fetch...")

    price_db.init_db()
    assets = price_db.get_all_asset_metadata()
    log.info("Found %d assets in database", len(assets))

    updated = 0
    errors = 0

    # Group assets by source to maintain proper rate limiting
    coingecko_assets = [a for a in assets if a["source"] == "coingecko" and a["source_id"]]
    yfinance_assets = [a for a in assets if a["source"] == "yfinance" and a["source_id"]]

    # Fetch yfinance first (no strict rate limit)
    log.info("Fetching %d yfinance assets...", len(yfinance_assets))
    for asset in yfinance_assets:
        try:
            df = fetch_yfinance(asset["source_id"])
            if df.empty:
                log.warning("No data returned for %s (yfinance:%s)", asset["name"], asset["source_id"])
                continue
            price_db.upsert_prices(asset["id"], df)
            log.info("Updated %s: %d rows, latest=%s", asset["name"], len(df), df.index[-1].date())
            updated += 1
        except Exception:
            log.exception("Failed to fetch %s (yfinance:%s)", asset["name"], asset["source_id"])
            errors += 1

    # Fetch CoinGecko with proper rate limiting (2.5s between each call)
    log.info("Fetching %d CoinGecko assets...", len(coingecko_assets))
    for asset in coingecko_assets:
        try:
            df = fetch_coingecko(asset["source_id"])
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

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
