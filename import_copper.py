#!/usr/bin/env python3
"""One-time import: historical daily COMEX copper prices from MacroTrends.

Fetches the MacroTrends copper dataset (daily, USD/lb, back to 1959), inserts
the Copper asset, and bulk-upserts the price history. After this runs,
fetch_prices.py keeps Copper fresh via yfinance HG=F on the daily cron.

Usage:
    # Fetch live from MacroTrends:
    PRICE_DB_URL=postgresql://... python import_copper.py
    # Or read from a pre-downloaded JSON file (for servers blocked by MT):
    PRICE_DB_URL=postgresql://... python import_copper.py copper_data.json
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

import price_db

MACROTRENDS_URL = "https://www.macrotrends.net/economic-data/1476/D"
REFERER = "https://www.macrotrends.net/1476/copper-prices-historical-chart-data"


def fetch_macrotrends_copper(source):
    if source and os.path.exists(source):
        payload = json.load(open(source))
    else:
        import requests
        r = requests.get(
            MACROTRENDS_URL,
            headers={"User-Agent": "Mozilla/5.0", "Referer": REFERER},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
    rows = payload["data"]
    epoch = datetime(1970, 1, 1)
    dates = [epoch + timedelta(milliseconds=ms) for ms, _ in rows]
    closes = [float(v) for _, v in rows]
    df = pd.DataFrame({"close": closes}, index=pd.to_datetime(dates, utc=True))
    df.index.name = "date"
    return df


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"Loading MacroTrends copper data (source={source or 'live URL'})...")
    df = fetch_macrotrends_copper(source)
    print(f"Got {len(df)} rows: {df.index[0].date()} -> {df.index[-1].date()}")

    price_db.init_db()
    asset_id = price_db.get_or_create_asset(
        name="Copper",
        category="metal",
        source="yfinance",
        source_id="HG=F",
    )
    print(f"Asset id: {asset_id}")

    count = price_db.upsert_prices(asset_id, df)
    print(f"Upserted {count} rows.")


if __name__ == "__main__":
    sys.exit(main())
