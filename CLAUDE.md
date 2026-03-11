# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bitcoin SMA backtesting engine with CLI (`backtest.py`) and Flask web UI (`app.py`). Tests Simple Moving Average trading strategies against historical BTC daily price data (`bitcoin.csv`, ~5710 rows from 2011 to 2026).

## Commands

```bash
pip install -r requirements.txt          # Install deps: pandas, matplotlib, flask

# CLI
python backtest.py --examples            # Show all CLI usage examples
python backtest.py --sma 40              # Single SMA strategy
python backtest.py --mode sweep-chart    # Sweep all SMA periods, plot annualized returns
python backtest.py --mode sweep-dual     # Heatmap of all fast/slow SMA combinations
python backtest.py --mode dual           # Dual SMA crossover

# Web UI
python app.py                            # Flask server at http://localhost:5000
```

## Architecture

**`backtest.py`** — All backtesting logic and CLI interface. Core functions:
- `load_data()` / `compute_sma()` — Data loading and SMA computation
- `run_single_sma_strategy()` / `run_dual_sma_strategy()` — Strategy execution with signal generation, fee deduction, and position tracking
- `_apply_exposure(above_sma, exposure)` — Maps SMA signals to positions for three exposure modes: `long-cash`, `short-cash`, `long-short`
- `sweep_sma_periods()` — Loops through SMA period range, collects and ranks results
- `generate_chart()` / `generate_sweep_chart()` / `generate_dual_sweep_heatmap()` — Matplotlib chart generation (two-panel price+equity, line sweep, heatmap)
- Look-ahead bias prevention: signals shifted by 1 day

**`app.py`** — Flask web UI wrapping `backtest.py`. Uses inline HTML template with dark theme. Loads CSV once at startup into global `DF`. Charts rendered as base64-encoded PNGs inline. `Params` class parses form defaults. `_enrich_best()` adds annualized and buy-and-hold comparison metrics.

**`results/`** — Output directory for CLI-generated chart PNGs. Filenames are auto-generated with timestamps to avoid overwrites: `{timestamp}_{mode}_{params}_{exposure}.png`.

## Key Design Decisions

- Charts use log scale on price y-axis (BTC spans $0.08 to $80k+); USD formatter handles sub-$1 values with 2 decimal places
- Sharpe ratio annualized with √365 (crypto = 365-day year)
- Default trading fee: 0.1% per transaction
- Default start date: 2015-01-01 (web UI has "All data" button to use full range)
- `backtest.py` is imported by `app.py` as `bt` — all strategy logic stays in one module

## Conventions

- When adding new CLI parameters or modes, update the `EXAMPLES` string constant in `backtest.py`
- Web UI field visibility is toggled via JS `toggleFields()` using a `.hidden` CSS class with `!important`
- After editing `app.py`, kill existing Flask process before restarting (`taskkill //F //IM python.exe` on Windows)
