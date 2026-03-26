"""PostgreSQL database for historical price data (assets + daily close prices)."""

import os
import pandas as pd
import psycopg2
import psycopg2.extras

PRICE_DB_URL = os.environ.get(
    "PRICE_DB_URL",
    "postgresql://backtesting:backtesting@localhost/backtesting_prices",
)


def _get_conn():
    """Return a new PostgreSQL connection."""
    conn = psycopg2.connect(PRICE_DB_URL)
    conn.autocommit = False
    return conn


def init_db():
    """Create tables and indexes if they don't exist."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS assets (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL DEFAULT 'crypto',
                    source TEXT,
                    source_id TEXT,
                    logo_url TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS prices (
                    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                    date DATE NOT NULL,
                    close DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (asset_id, date)
                );

                CREATE INDEX IF NOT EXISTS idx_prices_asset_date
                    ON prices(asset_id, date);
            """)
        conn.commit()
    finally:
        conn.close()


def get_or_create_asset(name, category="crypto", source=None, source_id=None,
                        logo_url=None):
    """Insert or update an asset row. Returns the asset id."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO assets (name, category, source, source_id, logo_url)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    category  = COALESCE(EXCLUDED.category, assets.category),
                    source    = COALESCE(EXCLUDED.source, assets.source),
                    source_id = COALESCE(EXCLUDED.source_id, assets.source_id),
                    logo_url  = COALESCE(EXCLUDED.logo_url, assets.logo_url)
                RETURNING id
            """, (name, category, source, source_id, logo_url))
            asset_id = cur.fetchone()[0]
        conn.commit()
        return asset_id
    finally:
        conn.close()


def upsert_prices(asset_id, df):
    """Bulk-insert daily prices from a DataFrame (DatetimeIndex + 'close' column).

    Uses ON CONFLICT to update existing rows.
    """
    if df.empty:
        return 0
    rows = [
        (asset_id, idx.date() if hasattr(idx, "date") else idx, float(row["close"]))
        for idx, row in df.iterrows()
    ]
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO prices (asset_id, date, close)
                   VALUES %s
                   ON CONFLICT (asset_id, date) DO UPDATE SET close = EXCLUDED.close""",
                rows,
                page_size=1000,
            )
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def get_asset_df(name):
    """Load price data for one asset as a DataFrame.

    Returns the same format as backtest.load_data():
    DatetimeIndex (UTC, tz-aware) with a single 'close' column.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.date, p.close
                FROM prices p
                JOIN assets a ON p.asset_id = a.id
                WHERE a.name = %s
                ORDER BY p.date
            """, (name,))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return pd.DataFrame(columns=["close"])

    df = pd.DataFrame(rows, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("date").sort_index()
    return df


def get_all_assets():
    """Load all assets as a dict of {name: DataFrame}.

    Returns the same structure as the ASSETS dict built from CSVs.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.name, p.date, p.close
                FROM prices p
                JOIN assets a ON p.asset_id = a.id
                ORDER BY a.name, p.date
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    assets = {}
    if not rows:
        return assets

    # Group rows by asset name
    current_name = None
    dates, closes = [], []
    for name, date, close in rows:
        if name != current_name:
            if current_name is not None and dates:
                df = pd.DataFrame({"close": closes}, index=pd.to_datetime(dates, utc=True))
                df.index.name = "date"
                assets[current_name] = df
            current_name = name
            dates, closes = [], []
        dates.append(date)
        closes.append(close)

    # Don't forget the last group
    if current_name is not None and dates:
        df = pd.DataFrame({"close": closes}, index=pd.to_datetime(dates, utc=True))
        df.index.name = "date"
        assets[current_name] = df

    return assets


def get_all_asset_metadata():
    """Return metadata for all assets (for the fetcher script).

    Returns list of dicts with keys: id, name, category, source, source_id, logo_url.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, category, source, source_id, logo_url
                FROM assets
                ORDER BY name
            """)
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def get_asset_id(name):
    """Return the asset id for a given name, or None if not found."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM assets WHERE name = %s", (name,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def delete_asset(name):
    """Delete an asset and all its prices (CASCADE)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM assets WHERE name = %s", (name,))
        conn.commit()
    finally:
        conn.close()


def rename_asset(old_name, new_name):
    """Rename an asset."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE assets SET name = %s WHERE name = %s",
                (new_name, old_name),
            )
        conn.commit()
    finally:
        conn.close()


def update_asset_category(name, category):
    """Update an asset's category."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE assets SET category = %s WHERE name = %s",
                (category, name),
            )
        conn.commit()
    finally:
        conn.close()


def get_asset_last_date(name):
    """Return the latest date in the prices table for an asset, or None."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(p.date)
                FROM prices p
                JOIN assets a ON p.asset_id = a.id
                WHERE a.name = %s
            """, (name,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def get_price_count(name):
    """Return the number of price rows for an asset."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM prices p
                JOIN assets a ON p.asset_id = a.id
                WHERE a.name = %s
            """, (name,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def delete_prices_on_or_after(date):
    """Delete all price rows on or after the given date (for all assets).

    Useful for removing incomplete (today's) data.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prices WHERE date >= %s", (date,))
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()
