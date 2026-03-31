# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-asset backtesting engine with CLI (`backtest.py`) and Flask web UI (`app.py`). Tests indicator crossover trading strategies (any indicator vs any indicator) against historical daily price data. Supports 11 cryptocurrencies via CSV files in `data/`.

## Commands

```bash
pip install -r requirements.txt          # Install deps: pandas, matplotlib, flask

# CLI (new style — any indicator vs any indicator)
python backtest.py --examples            # Show all CLI usage examples
python backtest.py --ind2 sma --period2 40                    # Price vs SMA(40)
python backtest.py --ind1 ema --period1 20 --ind2 sma --period2 100  # EMA(20)/SMA(100) crossover
python backtest.py --ind2 ema --mode sweep-chart              # Sweep EMA periods
python backtest.py --ind1 sma --ind2 ema --mode sweep-dual    # SMA/EMA crossover heatmap

# CLI (classic style — still works)
python backtest.py --sma 40              # Single SMA strategy
python backtest.py --mode sweep-chart    # Sweep all SMA periods
python backtest.py --mode sweep-dual     # Heatmap of all fast/slow SMA combinations
python backtest.py --mode dual           # Dual SMA crossover

# Web UI
python app.py                            # Flask server at http://localhost:5000
```

## Architecture

**`backtest.py`** — All backtesting logic and CLI interface. Two-indicator model where any indicator can be paired with any other:
- `INDICATORS` registry dict: maps 15 indicator names to `{fn, needs_period}` specs: price, sma, ema, wma, hma, dema, tema, kama, zlema, smma, lsma, alma, frama, t3, mcginley.
- `compute_indicator_from_spec(df, name, period)` — Returns `(series, label)` from registry
- `run_strategy(df, ind1_name, ind1_period, ind2_name, ind2_period, ...)` — Unified strategy: signal = `ind1 > ind2`, returns dict with `ind1_series`, `ind2_series`, `ind1_label`, `ind2_label`, `label`, plus all metrics
- `run_single_sma_strategy()` / `run_dual_sma_strategy()` — Legacy wrappers calling `run_strategy`, adding old keys (`sma_series`, `fast_sma_series`, `sma_period`) for backward compat
- `sweep_periods(df, ind1_name, ind1_period, ind2_name, ind2_period, sweep_target, ...)` — Sweep one indicator's period across a range
- `sweep_sma_periods()` — Legacy wrapper for `sweep_periods`
- `_apply_exposure(above, exposure)` — Maps signals to positions for: `long-cash`, `short-cash`, `long-short`
- `generate_chart()` / `generate_sweep_chart()` / `generate_dual_sweep_heatmap()` — Matplotlib chart generation
- Look-ahead bias prevention: signals shifted by 1 day

**`app.py`** — Flask web UI wrapping `backtest.py`. Uses inline HTML template with dark theme. Loads all CSVs from `data/` at startup into `ASSETS` dict. Charts rendered as base64-encoded PNGs inline. `Params` class parses form defaults. `_enrich_best()` adds annualized and buy-and-hold comparison metrics.
- **Modes**: backtest, sweep (find best period), heatmap (find best combo), sweep-lev (find best leverage)
- **Indicator selectors**: Indicator 1 (price + 14 MAs) + Period 1, Indicator 2 (14 MAs) + Period 2
- Heatmap auto-promotes ind1 from "price" to ind2's type when needed

**`data/`** — CSV files auto-detected at startup (one per asset: bitcoin.csv, ethereum.csv, solana.csv, etc.)

**`results/`** — Output directory for CLI-generated chart PNGs. Filenames are auto-generated with timestamps to avoid overwrites.

## Key Design Decisions

- **No silent fallbacks**: Never substitute a default value when data is missing — show an explicit error. Wrong data is worse than an error message. This applies to asset lookups, parameter parsing, and any place where a missing value could produce incorrect results.
- **Asset renames propagate**: When an asset is renamed, all saved backtests referencing it are updated via `db.rename_asset_in_backtests()`
- Charts use log scale on price y-axis (BTC spans $0.08 to $80k+); USD formatter handles sub-$1 values with 2 decimal places
- Sharpe ratio annualized with √365 (crypto = 365-day year)
- Default trading fee: 0.1% per transaction
- Default start date: 2015-01-01 (web UI has "All data" button to use full range)
- `backtest.py` is imported by `app.py` as `bt` — all strategy logic stays in one module
- Price = special case indicator with no period param
- Heatmap with same indicator type: upper triangle only (skip redundant combos)
- Heatmap with mixed types (e.g., EMA vs SMA): all combos valid

## Mandatory Deploy Flow

**Every code change MUST be committed, pushed, and deployed to production. No exceptions.**

```bash
# 1. Commit and push
git add <files> && git commit -m "..." && git push origin master

# 2. Deploy to production
ssh root@209.97.172.76 "cd /opt/backtesting-engine && git pull && systemctl restart backtesting"
```

- Server path: `/opt/backtesting-engine` (NOT `/root/`)
- Service: `backtesting` (Gunicorn on 127.0.0.1:5000 behind Nginx)
- Production URL: https://analytics.the-bitcoin-strategy.com/

## Testing

- **Every bug fix MUST include a regression test** in `tests/test_signals.py` (or a new test file under `tests/`)
- Pre-commit hook runs `pytest tests/` automatically — commits are blocked if tests fail
- Run tests manually: `python -m pytest tests/ -v`
- Tests cover: signal detection, live chart ratio mode, asset name resolution, source code invariants

## Conventions

- When adding new CLI parameters or modes, update the `EXAMPLES` string constant in `backtest.py`
- When adding new indicators, add entry to `INDICATORS` dict in `backtest.py` with `{fn, needs_period}`
- Web UI field visibility is toggled via JS `toggleFields()` using a `.hidden` CSS class with `!important`
- After editing `app.py`, kill existing Flask process before restarting (`taskkill //F //IM python.exe` on Windows)
