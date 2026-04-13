#!/usr/bin/env python3
"""One-time migration: import all CSV price data into PostgreSQL.

Usage:
    python migrate_csv_to_pg.py

Requires PRICE_DB_URL env var (or uses default localhost connection).
"""

import os
import sys
import time

import backtest as bt
import price_db

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Maps asset name (CSV filename without .csv) to source config.
# CoinGecko IDs for crypto, yfinance tickers for everything else.
ASSET_CONFIG = {
    # Crypto — CoinGecko
    "bitcoin":       {"category": "crypto",    "source": "coingecko", "source_id": "bitcoin"},
    "ethereum":      {"category": "crypto",    "source": "coingecko", "source_id": "ethereum"},
    "solana":        {"category": "crypto",    "source": "coingecko", "source_id": "solana"},
    "XRP":           {"category": "crypto",    "source": "coingecko", "source_id": "ripple"},
    "BNB":           {"category": "crypto",    "source": "coingecko", "source_id": "binancecoin"},
    "Cardano":       {"category": "crypto",    "source": "coingecko", "source_id": "cardano"},
    "Dogecoin":      {"category": "crypto",    "source": "coingecko", "source_id": "dogecoin"},
    "Monero":        {"category": "crypto",    "source": "coingecko", "source_id": "monero"},
    "Bitcoin Cash":  {"category": "crypto",    "source": "coingecko", "source_id": "bitcoin-cash"},
    "Chainlink":     {"category": "crypto",    "source": "coingecko", "source_id": "chainlink"},
    "Hyperliquid":   {"category": "crypto",    "source": "coingecko", "source_id": "hyperliquid"},
    "Bittensor":     {"category": "crypto",    "source": "coingecko", "source_id": "bittensor"},
    # Stocks — yfinance
    "Apple":         {"category": "stock",     "source": "yfinance",  "source_id": "AAPL"},
    "Microsoft":     {"category": "stock",     "source": "yfinance",  "source_id": "MSFT"},
    "Amazon":        {"category": "stock",     "source": "yfinance",  "source_id": "AMZN"},
    "Alphabet":      {"category": "stock",     "source": "yfinance",  "source_id": "GOOGL"},
    "Tesla":         {"category": "stock",     "source": "yfinance",  "source_id": "TSLA"},
    "Nvidia":        {"category": "stock",     "source": "yfinance",  "source_id": "NVDA"},
    "Meta":          {"category": "stock",     "source": "yfinance",  "source_id": "META"},
    "Netflix":       {"category": "stock",     "source": "yfinance",  "source_id": "NFLX"},
    "Coinbase":      {"category": "stock",     "source": "yfinance",  "source_id": "COIN"},
    "Strategy":      {"category": "stock",     "source": "yfinance",  "source_id": "MSTR"},
    # Indices — yfinance
    "SP500":         {"category": "index",     "source": "yfinance",  "source_id": "^GSPC"},
    "Nasdaq100":     {"category": "index",     "source": "yfinance",  "source_id": "^NDX"},
    "Dow Jones":     {"category": "index",     "source": "yfinance",  "source_id": "^DJI"},
    "Dax":           {"category": "index",     "source": "yfinance",  "source_id": "^GDAXI"},
    "Hang Seng":     {"category": "index",     "source": "yfinance",  "source_id": "^HSI"},
    # Metals — yfinance
    "Gold":          {"category": "metal",     "source": "yfinance",  "source_id": "GC=F"},
    "Silver":        {"category": "metal",     "source": "yfinance",  "source_id": "SI=F"},
    "Palladium":     {"category": "metal",     "source": "yfinance",  "source_id": "PA=F"},
    "Copper":        {"category": "metal",     "source": "yfinance",  "source_id": "HG=F"},
    # Commodities — yfinance
    "Oil (Brent)":   {"category": "commodity", "source": "yfinance",  "source_id": "BZ=F"},
    "Oil (Wti)":     {"category": "commodity", "source": "yfinance",  "source_id": "CL=F"},
}


def main():
    print("Initializing PostgreSQL schema...")
    price_db.init_db()

    csv_files = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".csv"))
    print(f"Found {len(csv_files)} CSV files in {DATA_DIR}\n")

    total_rows = 0
    errors = []

    for fname in csv_files:
        name = fname.replace(".csv", "")
        path = os.path.join(DATA_DIR, fname)

        # Skip metadata files
        if name.startswith("_"):
            continue

        try:
            df = bt.load_data(path)
            csv_rows = len(df)

            # Look up source config, default to csv-only for unknown assets
            config = ASSET_CONFIG.get(name, {
                "category": "crypto",
                "source": "csv",
                "source_id": None,
            })

            asset_id = price_db.get_or_create_asset(
                name=name,
                category=config["category"],
                source=config["source"],
                source_id=config["source_id"],
            )

            inserted = price_db.upsert_prices(asset_id, df)

            # Verify row count
            db_rows = price_db.get_price_count(name)
            status = "OK" if db_rows == csv_rows else f"MISMATCH (csv={csv_rows}, db={db_rows})"

            print(f"  {name:20s} | {csv_rows:>6,} rows | {config['source']:>10s} | {status}")
            total_rows += csv_rows

        except Exception as e:
            print(f"  {name:20s} | ERROR: {e}")
            errors.append((name, str(e)))

    print(f"\nDone. Total rows imported: {total_rows:,}")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for name, err in errors:
            print(f"  - {name}: {err}")
        sys.exit(1)
    else:
        print("All assets imported successfully.")


if __name__ == "__main__":
    main()
