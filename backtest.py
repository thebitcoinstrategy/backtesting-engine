#!/usr/bin/env python3
"""Bitcoin SMA Backtesting Engine — CLI app to backtest SMA trading strategies."""

import argparse
from datetime import datetime
import os
import numpy as np
import pandas as pd
import sys

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def load_data(path):
    """Read CSV with unix timestamp + close price, return DataFrame with datetime index."""
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("date").sort_index()
    df = df[["close"]].copy()
    return df


def compute_sma(df, period):
    """Return rolling mean Series for the close column."""
    return df["close"].rolling(window=period).mean()


def _apply_exposure(above_sma, exposure):
    """Convert boolean above/below SMA signal to position based on exposure mode.
    long-cash:  above=1, below=0
    short-cash: above=0, below=-1
    long-short: above=1, below=-1
    """
    if exposure == "long-cash":
        return above_sma.astype(int)
    elif exposure == "short-cash":
        return -(~above_sma).astype(int)
    else:  # long-short
        return above_sma.astype(int).replace(0, -1)


def run_single_sma_strategy(df, sma_period, initial_cash, fee=0.001, exposure="long-cash"):
    """Trade based on price vs SMA. Signal shifted by 1 day."""
    df = df.copy()
    df["sma"] = compute_sma(df, sma_period)

    above_sma = df["close"] > df["sma"]
    df["position"] = _apply_exposure(above_sma, exposure).shift(1).fillna(0)

    daily_return = df["close"].pct_change().fillna(0)
    df["strategy_return"] = df["position"] * daily_return

    # Apply trading fee on each position change
    trade_mask = df["position"].diff().fillna(0).abs() > 0
    df.loc[trade_mask, "strategy_return"] -= fee

    df["equity"] = initial_cash * (1 + df["strategy_return"]).cumprod()
    df["buyhold"] = initial_cash * (1 + daily_return).cumprod()

    # Trade count: position changes
    trades = trade_mask.sum()

    # Metrics
    total_return = (df["equity"].iloc[-1] / initial_cash - 1) * 100
    buyhold_return = (df["buyhold"].iloc[-1] / initial_cash - 1) * 100
    max_drawdown = _max_drawdown(df["equity"])

    mean_daily = df["strategy_return"].mean()
    std_daily = df["strategy_return"].std()
    sharpe = (mean_daily / std_daily * np.sqrt(365)) if std_daily > 0 else 0.0

    # Buy/sell markers: position increases = buy, decreases = sell
    pos_diff = df["position"].diff().fillna(0)
    buy_signals = df.index[pos_diff > 0]
    sell_signals = df.index[pos_diff < 0]

    return {
        "sma_period": sma_period,
        "total_return": total_return,
        "buyhold_return": buyhold_return,
        "max_drawdown": max_drawdown,
        "trades": int(trades),
        "sharpe": sharpe,
        "equity": df["equity"],
        "buyhold": df["buyhold"],
        "sma_series": df["sma"],
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "label": f"SMA({sma_period})",
    }


def run_dual_sma_strategy(df, fast_period, slow_period, initial_cash, fee=0.001, exposure="long-cash"):
    """Trade based on fast SMA vs slow SMA. Signal shifted by 1 day."""
    df = df.copy()
    df["fast_sma"] = compute_sma(df, fast_period)
    df["slow_sma"] = compute_sma(df, slow_period)

    above_sma = df["fast_sma"] > df["slow_sma"]
    df["position"] = _apply_exposure(above_sma, exposure).shift(1).fillna(0)

    daily_return = df["close"].pct_change().fillna(0)
    df["strategy_return"] = df["position"] * daily_return

    # Apply trading fee on each position change
    trade_mask = df["position"].diff().fillna(0).abs() > 0
    df.loc[trade_mask, "strategy_return"] -= fee

    df["equity"] = initial_cash * (1 + df["strategy_return"]).cumprod()
    df["buyhold"] = initial_cash * (1 + daily_return).cumprod()

    trades = trade_mask.sum()

    total_return = (df["equity"].iloc[-1] / initial_cash - 1) * 100
    buyhold_return = (df["buyhold"].iloc[-1] / initial_cash - 1) * 100
    max_drawdown = _max_drawdown(df["equity"])

    mean_daily = df["strategy_return"].mean()
    std_daily = df["strategy_return"].std()
    sharpe = (mean_daily / std_daily * np.sqrt(365)) if std_daily > 0 else 0.0

    pos_diff = df["position"].diff().fillna(0)
    buy_signals = df.index[pos_diff > 0]
    sell_signals = df.index[pos_diff < 0]

    return {
        "sma_period": slow_period,
        "fast_period": fast_period,
        "total_return": total_return,
        "buyhold_return": buyhold_return,
        "max_drawdown": max_drawdown,
        "trades": int(trades),
        "sharpe": sharpe,
        "equity": df["equity"],
        "buyhold": df["buyhold"],
        "sma_series": df["slow_sma"],
        "fast_sma_series": df["fast_sma"],
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "label": f"SMA({fast_period}/{slow_period})",
    }


def _max_drawdown(equity_series):
    """Compute max drawdown as a percentage."""
    cummax = equity_series.cummax()
    drawdown = (equity_series - cummax) / cummax
    return drawdown.min() * 100


def sweep_sma_periods(df, sma_min, sma_max, initial_cash, mode, fast_sma, fee=0.001, exposure="long-cash"):
    """Run strategy across a range of SMA periods, return results sorted by total return."""
    results = []
    periods = range(sma_min, sma_max + 1)

    for period in periods:
        if mode == "single":
            result = run_single_sma_strategy(df, period, initial_cash, fee, exposure)
        else:
            # In dual mode, the sweep varies the slow SMA while fast stays fixed
            if period <= fast_sma:
                continue
            result = run_dual_sma_strategy(df, fast_sma, period, initial_cash, fee, exposure)
        results.append(result)

    results.sort(key=lambda r: r["total_return"], reverse=True)
    return results


def print_results_table(results, mode):
    """Print an ASCII table of results."""
    if not results:
        print("No results to display.")
        return

    if mode == "single":
        header = f"{'SMA Period':>12} {'Total Ret %':>12} {'B&H Ret %':>12} {'Max DD %':>10} {'Trades':>8} {'Sharpe':>8}"
        sep = "-" * len(header)
        print(sep)
        print(header)
        print(sep)
        for r in results:
            print(
                f"{r['sma_period']:>12d} "
                f"{r['total_return']:>11.2f}% "
                f"{r['buyhold_return']:>11.2f}% "
                f"{r['max_drawdown']:>9.2f}% "
                f"{r['trades']:>8d} "
                f"{r['sharpe']:>8.2f}"
            )
        print(sep)
    else:
        header = f"{'Fast/Slow':>12} {'Total Ret %':>12} {'B&H Ret %':>12} {'Max DD %':>10} {'Trades':>8} {'Sharpe':>8}"
        sep = "-" * len(header)
        print(sep)
        print(header)
        print(sep)
        for r in results:
            label = f"{r['fast_period']}/{r['sma_period']}"
            print(
                f"{label:>12s} "
                f"{r['total_return']:>11.2f}% "
                f"{r['buyhold_return']:>11.2f}% "
                f"{r['max_drawdown']:>9.2f}% "
                f"{r['trades']:>8d} "
                f"{r['sharpe']:>8.2f}"
            )
        print(sep)


def _annualized_return(total_return_pct, n_days):
    """Convert total return % over n_days into annualized return %."""
    growth = 1 + total_return_pct / 100
    if growth <= 0 or n_days <= 0:
        return -100.0
    return (growth ** (365 / n_days) - 1) * 100


def generate_sweep_chart(df, sma_min, sma_max, initial_cash, output_path, fee=0.001, exposure="long-cash"):
    """Sweep every SMA period from sma_min to sma_max and plot annualized return vs period."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n_days = len(df)
    periods = list(range(sma_min, sma_max + 1))
    annualized_returns = []

    print(f"Sweeping SMA periods {sma_min} to {sma_max} ({len(periods)} strategies, exposure: {exposure})...")
    print(f"Trading fee: {fee * 100:.2f}% per transaction")
    for period in periods:
        result = run_single_sma_strategy(df, period, initial_cash, fee, exposure)
        ann = _annualized_return(result["total_return"], n_days)
        annualized_returns.append(ann)

    # Buy-and-hold annualized return for reference line
    bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    bh_annualized = _annualized_return(bh_total, n_days)

    # Find best
    best_idx = np.argmax(annualized_returns)
    best_period = periods[best_idx]
    best_ann = annualized_returns[best_idx]

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
    ax.plot(periods, annualized_returns, color="steelblue", linewidth=1)
    ax.axhline(y=bh_annualized, color="gray", linestyle="--", linewidth=1,
               label=f"Buy & Hold ({bh_annualized:.1f}%)")
    ax.scatter([best_period], [best_ann], color="red", s=60, zorder=5,
               label=f"Best: SMA({best_period}) ({best_ann:.1f}%)")

    ax.set_xlabel("SMA Period (days)")
    ax.set_ylabel("Annualized Return (%)")
    ax.set_title(f"Annualized Return by SMA Period ({sma_min}–{sma_max})")
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    print(f"Best: SMA({best_period}) with {best_ann:.2f}% annualized return")
    print(f"Buy & Hold: {bh_annualized:.2f}% annualized")
    print(f"Chart saved to {output_path}")


def generate_dual_sweep_heatmap(df, sma_min, sma_max, sma_step, initial_cash, output_path,
                                fee=0.001, exposure="long-cash"):
    """Sweep all fast/slow SMA permutations and generate a heatmap of annualized returns."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n_days = len(df)
    periods = list(range(sma_min, sma_max + 1, sma_step))
    n = len(periods)

    print(f"Sweeping dual SMA crossovers: {n}x{n} = {n*n} permutations "
          f"(SMA {sma_min}-{sma_max}, step {sma_step})...")

    # Precompute all SMAs
    sma_cache = {}
    for p in periods:
        sma_cache[p] = compute_sma(df, p)

    daily_return = df["close"].pct_change().fillna(0)

    # Build matrix: rows = fast SMA, cols = slow SMA
    matrix = np.full((n, n), np.nan)
    best_ann = -np.inf
    best_fast = best_slow = None

    for i, fast in enumerate(periods):
        for j, slow in enumerate(periods):
            if fast >= slow:
                continue
            above_sma = sma_cache[fast] > sma_cache[slow]
            position = _apply_exposure(above_sma, exposure).shift(1).fillna(0)
            strat_return = position * daily_return
            trade_mask = position.diff().fillna(0).abs() > 0
            strat_return = strat_return.copy()
            strat_return[trade_mask] -= fee
            equity_final = initial_cash * (1 + strat_return).prod()
            total_ret = (equity_final / initial_cash - 1) * 100
            ann = _annualized_return(total_ret, n_days)
            matrix[i, j] = ann
            if ann > best_ann:
                best_ann = ann
                best_fast = fast
                best_slow = slow

    print(f"Best: SMA({best_fast}/{best_slow}) with {best_ann:.2f}% annualized return")

    # Buy-and-hold reference
    bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    bh_ann = _annualized_return(bh_total, n_days)
    print(f"Buy & Hold: {bh_ann:.2f}% annualized")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 12), dpi=150)
    im = ax.imshow(matrix, cmap="RdYlGn", aspect="auto", origin="lower",
                   interpolation="nearest")

    ax.set_xticks(range(n))
    ax.set_xticklabels(periods, rotation=90, fontsize=max(4, min(8, 200 // n)))
    ax.set_yticks(range(n))
    ax.set_yticklabels(periods, fontsize=max(4, min(8, 200 // n)))
    ax.set_xlabel("Slow SMA Period")
    ax.set_ylabel("Fast SMA Period")
    ax.set_title(f"Dual SMA Crossover — Annualized Return % (step={sma_step})\n"
                 f"Best: SMA({best_fast}/{best_slow}) = {best_ann:.1f}% | "
                 f"B&H: {bh_ann:.1f}% | {exposure}")

    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label("Annualized Return (%)")

    # Add text values if grid is small enough
    if n <= 30:
        for i in range(n):
            for j in range(n):
                val = matrix[i, j]
                if not np.isnan(val):
                    color = "black" if abs(val - np.nanmean(matrix)) < np.nanstd(matrix) else "white"
                    ax.text(j, i, f"{val:.0f}", ha="center", va="center",
                            fontsize=max(4, min(7, 150 // n)), color=color)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    print(f"Chart saved to {output_path}")

    return matrix, periods, best_fast, best_slow, best_ann


def generate_chart(df, best_result, output_path):
    """Generate a two-panel PNG chart: price+SMA with markers, and equity curves."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 10), dpi=150,
        gridspec_kw={"height_ratios": [7, 3]}, sharex=True
    )

    # Top panel: price + SMA + buy/sell markers
    ax1.plot(df.index, df["close"], label="BTC Price", color="black", linewidth=0.8)
    ax1.plot(
        best_result["sma_series"].index, best_result["sma_series"],
        label=best_result["label"], color="blue", linewidth=0.8, alpha=0.8
    )
    if "fast_sma_series" in best_result:
        ax1.plot(
            best_result["fast_sma_series"].index, best_result["fast_sma_series"],
            label=f"SMA({best_result['fast_period']})", color="orange",
            linewidth=0.8, alpha=0.8
        )

    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
    ax1.set_ylabel("BTC Price (log scale)")
    ax1.set_title(f"Bitcoin SMA Backtest — Best: {best_result['label']} "
                  f"({best_result['total_return']:.1f}% return)")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)

    # Bottom panel: equity curve vs buy-and-hold
    ax2.plot(best_result["equity"].index, best_result["equity"],
             label="Strategy Equity", color="blue", linewidth=1)
    ax2.plot(best_result["buyhold"].index, best_result["buyhold"],
             label="Buy & Hold", color="gray", linewidth=1, alpha=0.7)
    ax2.set_yscale("log")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax2.set_ylabel("Portfolio Value (log)")
    ax2.set_xlabel("Date")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.xaxis.set_major_locator(mdates.YearLocator(2))

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    print(f"Chart saved to {output_path}")


EXAMPLES = """\
Examples:

  python backtest.py                              # sweep SMA 2-365, table + chart
  python backtest.py --sma 40                     # single SMA chart
  python backtest.py --mode dual                  # dual SMA crossover (default: fast=20)
  python backtest.py --mode sweep-chart           # annualized return vs SMA period chart
  python backtest.py --mode sweep-dual            # dual crossover heatmap (default: step=5)
  python backtest.py --mode sweep-dual --sma-step 10  # coarser grid for speed
  python backtest.py --exposure long-cash          # long above SMA, cash below (default)
  python backtest.py --exposure short-cash         # cash above SMA, short below
  python backtest.py --exposure long-short         # long above SMA, short below
  python backtest.py --fee 0.5                    # custom fee (default: 0.1%)
  python backtest.py --fee 0                      # no fees
  python backtest.py --sma-min 10 --sma-max 100   # custom SMA range (default: 2-365)
  python backtest.py --initial-cash 50000         # custom starting capital (default: 10000)
  python backtest.py --start-date 2017-01-01     # filter start date (default: all data)
  python backtest.py --end-date 2023-12-31       # filter end date (default: all data)
  python backtest.py --help                       # show all parameters
  python app.py                                   # launch web interface on port 5000
"""


def main():
    parser = argparse.ArgumentParser(
        description="Backtest SMA trading strategies on Bitcoin historical data.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--examples", action="store_true", help="Show usage examples and exit")
    parser.add_argument("--data", default="bitcoin.csv", help="CSV file path")
    parser.add_argument("--sma", type=int, default=None, help="Single SMA period (shorthand for --sma-min X --sma-max X)")
    parser.add_argument("--sma-min", type=int, default=2, help="Shortest SMA period (default: 2)")
    parser.add_argument("--sma-max", type=int, default=365, help="Longest SMA period (default: 365)")
    parser.add_argument("--initial-cash", type=float, default=10000, help="Starting capital")
    parser.add_argument("--mode", choices=["single", "dual", "sweep-chart", "sweep-dual"], default="single",
                        help="single SMA vs price, dual SMA crossover, sweep-chart, or sweep-dual heatmap")
    parser.add_argument("--sma-step", type=int, default=5,
                        help="Step for sweep-dual heatmap grid (default: 5)")
    parser.add_argument("--fast-sma", type=int, default=20, help="Fast SMA period (dual mode)")
    parser.add_argument("--exposure", choices=["long-cash", "short-cash", "long-short"],
                        default="long-cash",
                        help="long-cash: long above SMA, cash below | "
                             "short-cash: cash above SMA, short below | "
                             "long-short: long above SMA, short below (default: long-cash)")
    parser.add_argument("--fee", type=float, default=0.1,
                        help="Trading fee per transaction in percent (default: 0.1)")
    parser.add_argument("--start-date", default="2015-01-01",
                        help="Start date YYYY-MM-DD (default: 2015-01-01)")
    parser.add_argument("--end-date", default=None,
                        help="End date YYYY-MM-DD (default: end of data)")
    parser.add_argument("--chart-file", default=None,
                        help="Output chart filename (auto-generated if omitted)")
    args = parser.parse_args()

    if args.examples:
        print(EXAMPLES)
        return

    if args.sma is not None:
        args.sma_min = args.sma
        args.sma_max = args.sma

    fee = args.fee / 100  # convert percent to fraction

    # Auto-generate chart filename if not specified
    if args.chart_file is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        exp = f"_{args.exposure}"
        if args.mode == "sweep-dual":
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_sweep-dual_sma{args.sma_min}-{args.sma_max}_step{args.sma_step}{exp}.png")
        elif args.mode == "sweep-chart":
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_sweep-chart_sma{args.sma_min}-{args.sma_max}{exp}.png")
        elif args.mode == "dual":
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_dual_fast{args.fast_sma}_sma{args.sma_min}-{args.sma_max}{exp}.png")
        else:
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_single_sma{args.sma_min}-{args.sma_max}{exp}.png")

    print(f"Loading data from {args.data}...")
    df = load_data(args.data)
    if args.start_date:
        df = df[df.index >= pd.Timestamp(args.start_date, tz="UTC")]
    if args.end_date:
        df = df[df.index <= pd.Timestamp(args.end_date, tz="UTC")]
    print(f"Loaded {len(df)} daily rows from {df.index[0].date()} to {df.index[-1].date()}")

    print(f"Trading fee: {args.fee:.2f}% per transaction | Exposure: {args.exposure}")

    if args.mode == "sweep-dual":
        generate_dual_sweep_heatmap(df, args.sma_min, args.sma_max, args.sma_step,
                                     args.initial_cash, args.chart_file, fee, args.exposure)
        return

    if args.mode == "sweep-chart":
        generate_sweep_chart(df, args.sma_min, args.sma_max, args.initial_cash, args.chart_file, fee, args.exposure)
        return

    print(f"\nRunning {args.mode} SMA sweep (SMA {args.sma_min}-{args.sma_max})...")
    if args.mode == "dual":
        print(f"Fast SMA fixed at {args.fast_sma}")

    results = sweep_sma_periods(
        df, args.sma_min, args.sma_max,
        args.initial_cash, args.mode, args.fast_sma, fee, args.exposure
    )

    print()
    print_results_table(results, args.mode)

    if results:
        best = results[0]
        print(f"\nBest strategy: {best['label']} — "
              f"Return: {best['total_return']:.2f}%, "
              f"Sharpe: {best['sharpe']:.2f}, "
              f"Max DD: {best['max_drawdown']:.2f}%")

    if results:
        generate_chart(df, results[0], args.chart_file)


if __name__ == "__main__":
    main()
