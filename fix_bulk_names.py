#!/usr/bin/env python3
"""One-time script: fix bulk-uploaded assets with TradingView-style names.
Resolves ticker symbols via CoinGecko, renames in DB, downloads logos."""

import re
import json
import time
import urllib.request
import urllib.parse
import os
import sys

# Must run from project dir with PRICE_DB_URL set
import price_db

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
LOGOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logos")

def extract_ticker(name):
    """Extract ticker from TradingView-style name like 'COINBASE ARBUSD, 1D' -> 'ARB'."""
    # Remove the ', 1D' suffix
    name = re.sub(r',\s*\d+[DWMH]$', '', name).strip()
    # Remove exchange prefix (COINBASE, BINANCE, CRYPTO, BITSTAMP, CRYPTOCOM)
    name = re.sub(r'^(COINBASE|BINANCE|BITSTAMP|CRYPTO(?:COM)?)\s+', '', name).strip()
    # Remove USD/USDT suffix
    ticker = re.sub(r'USD[T]?$', '', name).strip()
    return ticker


def resolve_crypto_name(ticker):
    """Resolve crypto ticker to full name via CoinGecko search."""
    search_url = f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(ticker)}"
    headers = {'User-Agent': 'BacktestingEngine/1.0'}
    if COINGECKO_API_KEY:
        headers['x-cg-demo-api-key'] = COINGECKO_API_KEY
    req = urllib.request.Request(search_url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        coins = data.get('coins', [])
        # Exact symbol match first
        for coin in coins:
            if coin.get('symbol', '').upper() == ticker.upper():
                return coin.get('name'), coin.get('id'), coin.get('large') or coin.get('thumb')
        # Fallback: first result
        if coins:
            return coins[0].get('name'), coins[0].get('id'), coins[0].get('large') or coins[0].get('thumb')
    return None, None, None


def download_logo(asset_name, img_url):
    """Download logo image and return filename."""
    safe_name = re.sub(r'[^a-zA-Z0-9]', '-', asset_name.lower()).strip('-')
    filename = f"{safe_name}-logo.png"
    filepath = os.path.join(LOGOS_DIR, filename)
    try:
        req = urllib.request.Request(img_url, headers={'User-Agent': 'BacktestingEngine/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            with open(filepath, 'wb') as f:
                f.write(resp.read())
        return filename
    except Exception as e:
        print(f"  Logo download failed: {e}")
        return None


def main():
    assets = price_db.get_all_asset_metadata()
    # Find all TradingView-style names
    bad_assets = [a for a in assets if re.match(r'^(COINBASE|BINANCE|BITSTAMP|CRYPTO)', a['name'])]

    existing_names = {a['name'] for a in assets}

    # Load existing logos file
    logos_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_logos.json")
    try:
        with open(logos_file) as f:
            logos = json.load(f)
    except Exception:
        logos = {}

    print(f"Found {len(bad_assets)} assets to fix\n")

    for i, asset in enumerate(bad_assets):
        old_name = asset['name']
        ticker = extract_ticker(old_name)
        print(f"[{i+1}/{len(bad_assets)}] {old_name} -> ticker: {ticker}")

        try:
            name, cg_id, img_url = resolve_crypto_name(ticker)
        except Exception as e:
            print(f"  ERROR resolving: {e}")
            time.sleep(1)
            continue

        if not name:
            print(f"  SKIP: could not resolve ticker '{ticker}'")
            continue

        # Check if resolved name already exists
        if name in existing_names and name != old_name:
            print(f"  SKIP: '{name}' already exists, would be duplicate")
            continue

        print(f"  Resolved: {name} (coingecko: {cg_id})")

        # Rename in DB
        try:
            price_db.rename_asset(old_name, name)
            existing_names.discard(old_name)
            existing_names.add(name)
            print(f"  Renamed in DB")
        except Exception as e:
            print(f"  ERROR renaming: {e}")
            continue

        # Update source info and ticker
        if cg_id:
            try:
                conn = price_db._get_conn()
                with conn.cursor() as cur:
                    cur.execute("UPDATE assets SET source = 'coingecko', source_id = %s, ticker = %s WHERE name = %s", (cg_id, ticker, name))
                conn.commit()
                conn.close()
                print(f"  Set source: coingecko/{cg_id}, ticker: {ticker}")
            except Exception as e:
                print(f"  WARN: could not update source: {e}")

        # Download logo
        if img_url:
            logo_file = download_logo(name, img_url)
            if logo_file:
                logos[name] = logo_file
                print(f"  Logo: {logo_file}")

        # Rate limit for CoinGecko (free tier: ~10 req/min)
        time.sleep(8)

    # Save logos file
    with open(logos_file, 'w') as f:
        json.dump(logos, f, indent=2)
    print(f"\nSaved logos to {logos_file}")

    # --- Phase 2: Populate ticker symbols for all assets missing one ---
    print("\n=== Populating ticker symbols for all assets ===")
    assets = price_db.get_all_asset_metadata()
    missing_ticker = [a for a in assets if not a.get('ticker')
                      and not re.match(r'^(COINBASE|BINANCE|BITSTAMP|CRYPTO)', a['name'])]

    for i, asset in enumerate(missing_ticker):
        name = asset['name']
        source = asset.get('source')
        source_id = asset.get('source_id')
        ticker = None

        # For coingecko assets, look up the symbol
        if source == 'coingecko' and source_id:
            try:
                url = f"https://api.coingecko.com/api/v3/coins/{source_id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false"
                headers = {'User-Agent': 'BacktestingEngine/1.0'}
                if COINGECKO_API_KEY:
                    headers['x-cg-demo-api-key'] = COINGECKO_API_KEY
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    ticker = data.get('symbol', '').upper()
            except Exception as e:
                print(f"  [{i+1}/{len(missing_ticker)}] {name}: CG lookup error: {e}")
                time.sleep(2)
                continue
        elif source == 'yfinance' and source_id:
            # yfinance source_id IS the ticker
            ticker = source_id.upper()
        else:
            # Try CoinGecko search as fallback
            try:
                _, _, _ = resolve_crypto_name(name)
                search_url = f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(name)}"
                headers = {'User-Agent': 'BacktestingEngine/1.0'}
                if COINGECKO_API_KEY:
                    headers['x-cg-demo-api-key'] = COINGECKO_API_KEY
                req = urllib.request.Request(search_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    coins = json.loads(resp.read()).get('coins', [])
                    for coin in coins:
                        if coin.get('name', '').lower() == name.lower():
                            ticker = coin.get('symbol', '').upper()
                            break
                    if not ticker and coins:
                        ticker = coins[0].get('symbol', '').upper()
            except Exception as e:
                print(f"  [{i+1}/{len(missing_ticker)}] {name}: search error: {e}")
                time.sleep(2)
                continue

        if ticker:
            try:
                conn = price_db._get_conn()
                with conn.cursor() as cur:
                    cur.execute("UPDATE assets SET ticker = %s WHERE name = %s", (ticker, name))
                conn.commit()
                conn.close()
                print(f"  [{i+1}/{len(missing_ticker)}] {name} -> {ticker}")
            except Exception as e:
                print(f"  [{i+1}/{len(missing_ticker)}] {name}: DB error: {e}")
        else:
            print(f"  [{i+1}/{len(missing_ticker)}] {name}: could not resolve ticker")

        time.sleep(8)

    print("\nDone! Restart the service: systemctl restart backtesting")


if __name__ == '__main__':
    main()
