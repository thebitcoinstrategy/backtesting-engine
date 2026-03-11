#!/usr/bin/env python3
"""Web interface for the Bitcoin SMA Backtesting Engine."""

import os
import base64
from io import BytesIO
from flask import Flask, render_template_string, request
import backtest as bt

app = Flask(__name__)

HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>BTC SMA Backtester</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               background: #0f1117; color: #e0e0e0; min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        h1 { text-align: center; margin-bottom: 24px; color: #f7931a; font-size: 1.8em; }
        .layout { display: flex; flex-direction: column; gap: 24px; }
        .panel { background: #1a1d27; border-radius: 12px; padding: 24px; border: 1px solid #2a2d3a; }
        .form-group { margin-bottom: 12px; }
        .form-row { display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-end; }
        .form-row .form-group { flex: 1; min-width: 140px; margin-bottom: 0; }
        label { display: block; font-size: 0.85em; color: #9ca3af; margin-bottom: 4px; font-weight: 500; }
        input, select { width: 100%; padding: 8px 12px; border-radius: 6px; border: 1px solid #2a2d3a;
                        background: #0f1117; color: #e0e0e0; font-size: 0.95em; }
        input:focus, select:focus { outline: none; border-color: #f7931a; }
        .row { display: flex; gap: 12px; }
        .row .form-group { flex: 1; }
        button { width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 1em;
                 font-weight: 600; cursor: pointer; background: #f7931a; color: #0f1117;
                 margin-top: 8px; transition: background 0.2s; }
        button:hover { background: #e8850f; }
        button:disabled { background: #555; cursor: wait; }
        .chart-img { width: 100%; border-radius: 8px; }
        .results-table { width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 0.85em; }
        .results-table th, .results-table td { padding: 6px 10px; text-align: right; border-bottom: 1px solid #2a2d3a; }
        .results-table th { color: #9ca3af; font-weight: 500; }
        .results-table tr:hover { background: #22253a; }
        .best { color: #22c55e; font-weight: 600; }
        .placeholder { text-align: center; color: #555; padding: 80px 20px; font-size: 1.1em; }
        .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
        .stat { flex: 1; min-width: 120px; background: #0f1117; border-radius: 8px; padding: 12px; text-align: center; }
        .stat-value { font-size: 1.3em; font-weight: 700; color: #f7931a; }
        .stat-label { font-size: 0.75em; color: #9ca3af; margin-top: 2px; }
        .hidden { display: none !important; }
        .spinner { display: none; }
        .loading .spinner { display: inline-block; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
<div class="container">
    <h1>&#x20bf; BTC SMA Backtester</h1>
    <div class="layout">
        <div class="panel">
            <form method="POST" id="form" onsubmit="document.getElementById('btn').disabled=true; document.getElementById('btn').textContent='Running...';">
                <div class="form-row">
                    <div class="form-group">
                        <label>Mode</label>
                        <select name="mode" id="mode" onchange="toggleFields()">
                            <option value="single" {{ 'selected' if p.mode=='single' }}>Single SMA</option>
                            <option value="dual" {{ 'selected' if p.mode=='dual' }}>Dual SMA Crossover</option>
                            <option value="sweep-chart" {{ 'selected' if p.mode=='sweep-chart' }}>Sweep Chart</option>
                            <option value="sweep-dual" {{ 'selected' if p.mode=='sweep-dual' }}>Sweep Dual Crossover</option>
                        </select>
                    </div>
                    <div class="form-group" id="sma-group">
                        <label>SMA Period</label>
                        <input type="number" name="sma" value="{{ p.sma or '' }}" placeholder="e.g. 44" min="2">
                    </div>
                    <div class="form-group" id="slow-sma-group">
                        <label>Slow SMA</label>
                        <input type="number" name="slow_sma" value="{{ p.slow_sma or '' }}" placeholder="e.g. 100" min="2">
                    </div>
                    <div class="form-group" id="fast-sma-group">
                        <label>Fast SMA</label>
                        <input type="number" name="fast_sma" value="{{ p.fast_sma }}" min="2">
                    </div>
                    <div class="form-group" id="range-min-group">
                        <label>SMA Min</label>
                        <input type="number" name="sma_min" value="{{ p.sma_min }}" min="2">
                    </div>
                    <div class="form-group" id="range-max-group">
                        <label>SMA Max</label>
                        <input type="number" name="sma_max" value="{{ p.sma_max }}" min="2">
                    </div>
                    <div class="form-group" id="step-group">
                        <label>Step</label>
                        <input type="number" name="sma_step" value="{{ p.sma_step }}" min="1">
                    </div>
                    <div class="form-group">
                        <label>Exposure</label>
                        <select name="exposure">
                            <option value="long-cash" {{ 'selected' if p.exposure=='long-cash' }}>Long + Cash</option>
                            <option value="short-cash" {{ 'selected' if p.exposure=='short-cash' }}>Short + Cash</option>
                            <option value="long-short" {{ 'selected' if p.exposure=='long-short' }}>Long + Short</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Fee (%)</label>
                        <input type="number" name="fee" value="{{ p.fee }}" step="0.01" min="0">
                    </div>
                    <div class="form-group">
                        <label>Initial Cash</label>
                        <div style="position:relative">
                            <span style="position:absolute;left:8px;top:50%;transform:translateY(-50%);color:#9ca3af;font-size:0.9em">$</span>
                            <input type="number" name="initial_cash" value="{{ p.initial_cash }}" min="1" style="padding-left:20px">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Start Date <button type="button" onclick="document.getElementById('start_date').value='{{ data_start }}'"
                                style="background:#2a2d3a;color:#e0e0e0;font-size:0.65em;padding:2px 6px;border:1px solid #444;border-radius:3px;cursor:pointer;margin-left:4px;vertical-align:middle">All data</button></label>
                        <input type="date" name="start_date" id="start_date" value="{{ p.start_date }}">
                    </div>
                    <div class="form-group">
                        <label>End Date</label>
                        <input type="date" name="end_date" value="{{ p.end_date }}">
                    </div>
                    <div class="form-group" style="min-width:auto">
                        <label>&nbsp;</label>
                        <button type="submit" id="btn">Run Backtest</button>
                    </div>
                </div>
            </form>
        </div>
        <div class="panel">
            {% if chart %}
                {% if best %}
                <table class="results-table" style="margin-bottom:16px">
                    <tr>
                        <th style="text-align:left">Strategy</th>
                        <th>Ann. Return</th>
                        <th>Max Drawdown</th>
                        <th>Trades</th>
                    </tr>
                    <tr class="best">
                        <td style="text-align:left">
                            {% if best.get('fast_period') %}SMA({{ best.fast_period }}/{{ best.sma_period }})
                            {% elif best.get('sma_period') %}SMA({{ best.sma_period }})
                            {% else %}Strategy{% endif %}
                        </td>
                        <td>{{ "%.2f"|format(best.annualized) }}%</td>
                        <td>{{ "%.2f"|format(best.max_drawdown) }}%</td>
                        <td>{{ best.trades }}</td>
                    </tr>
                    <tr>
                        <td style="text-align:left">Buy & Hold</td>
                        <td>{{ "%.2f"|format(best.buyhold_annualized) }}%</td>
                        <td>{{ "%.2f"|format(best.buyhold_max_drawdown) }}%</td>
                        <td>1</td>
                    </tr>
                </table>
                {% endif %}
                {% if table_rows %}
                <details style="margin-bottom:16px">
                    <summary style="cursor:pointer;color:#9ca3af;font-size:0.9em">Show all results ({{ table_rows|length }})</summary>
                    <div style="max-height:300px;overflow-y:auto;margin-top:8px">
                    <table class="results-table">
                        <tr><th>{{ col_header }}</th><th>Return %</th><th>B&H %</th><th>Max DD %</th><th>Trades</th></tr>
                        {% for r in table_rows %}
                        <tr{% if loop.first %} class="best"{% endif %}>
                            <td>{{ r.label }}</td><td>{{ "%.2f"|format(r.total_return) }}</td>
                            <td>{{ "%.2f"|format(r.buyhold_return) }}</td><td>{{ "%.2f"|format(r.max_drawdown) }}</td>
                            <td>{{ r.trades }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                    </div>
                </details>
                {% endif %}
                <img class="chart-img" src="data:image/png;base64,{{ chart }}" />
            {% else %}
                <div class="placeholder">Configure parameters and press Run Backtest</div>
            {% endif %}
        </div>
    </div>
</div>
<script>
function toggleFields() {
    var mode = document.getElementById('mode').value;
    var rules = [
        ['sma-group', mode === 'single'],
        ['fast-sma-group', mode === 'dual'],
        ['slow-sma-group', mode === 'dual'],
        ['range-min-group', mode === 'sweep-chart' || mode === 'sweep-dual'],
        ['range-max-group', mode === 'sweep-chart' || mode === 'sweep-dual'],
        ['step-group', mode === 'sweep-dual']
    ];
    for (var i = 0; i < rules.length; i++) {
        var el = document.getElementById(rules[i][0]);
        var show = rules[i][1];
        if (show) { el.classList.remove('hidden'); } else { el.classList.add('hidden'); }
        var inputs = el.querySelectorAll('input');
        for (var j = 0; j < inputs.length; j++) inputs[j].disabled = !show;
    }
}
toggleFields();
</script>
</body>
</html>
"""


def _chart_to_base64(chart_func, *args, **kwargs):
    """Run a chart function but capture to base64 instead of file."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chart_func(*args, **kwargs)
    return None  # we use file-based approach instead


class Params:
    """Hold form parameters with defaults."""
    def __init__(self, form=None):
        if form:
            self.mode = form.get("mode", "single")
            sma_val = form.get("sma", "").strip()
            self.sma = int(sma_val) if sma_val else None
            slow_val = form.get("slow_sma", "").strip()
            self.slow_sma = int(slow_val) if slow_val else None
            self.sma_min = int(form.get("sma_min", 2))
            self.sma_max = int(form.get("sma_max", 365))
            self.sma_step = int(form.get("sma_step", 5))
            self.fast_sma = int(form.get("fast_sma", 20))
            self.exposure = form.get("exposure", "long-cash")
            self.fee = float(form.get("fee", 0.1))
            self.initial_cash = float(form.get("initial_cash", 10000))
            self.start_date = form.get("start_date", "").strip()
            self.end_date = form.get("end_date", "").strip()
        else:
            self.mode = "single"
            self.sma = None
            self.slow_sma = None
            self.sma_min = 2
            self.sma_max = 365
            self.sma_step = 5
            self.fast_sma = 20
            self.exposure = "long-cash"
            self.fee = 0.1
            self.initial_cash = 10000
            self.start_date = "2015-01-01"
            self.end_date = str(DF.index[-1].date())


# Load data once at startup
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bitcoin.csv")
DF = bt.load_data(DATA_PATH)
DATA_START = str(DF.index[0].date())


def _enrich_best(result, df):
    """Add annualized return and buy-and-hold metrics to a result dict."""
    import numpy as np
    n_days = len(df)
    result["annualized"] = bt._annualized_return(result["total_return"], n_days)
    result["buyhold_annualized"] = bt._annualized_return(result["buyhold_return"], n_days)
    result["buyhold_max_drawdown"] = bt._max_drawdown(result["buyhold"])
    # Buy-and-hold sharpe
    daily_return = df["close"].pct_change().fillna(0)
    mean_d = daily_return.mean()
    std_d = daily_return.std()
    result["buyhold_sharpe"] = (mean_d / std_d * np.sqrt(365)) if std_d > 0 else 0.0
    return result


@app.route("/", methods=["GET", "POST"])
def index():
    chart_b64 = None
    best = None
    table_rows = None
    col_header = "SMA"

    if request.method == "GET":
        return render_template_string(HTML, p=Params(), chart=None, best=None, table_rows=None, col_header=col_header, data_start=DATA_START)

    p = Params(request.form)
    import pandas as pd_mod
    df = DF.copy()
    if p.start_date:
        df = df[df.index >= pd_mod.Timestamp(p.start_date, tz="UTC")]
    if p.end_date:
        df = df[df.index <= pd_mod.Timestamp(p.end_date, tz="UTC")]
    if not p.start_date:
        p.start_date = str(df.index[0].date())
    if not p.end_date:
        p.end_date = str(df.index[-1].date())

    sma_min = p.sma_min
    sma_max = p.sma_max
    if p.mode == "single" and p.sma is not None:
        sma_min = p.sma
        sma_max = p.sma
    elif p.mode == "dual" and p.slow_sma is not None:
        sma_min = p.slow_sma
        sma_max = p.slow_sma

    fee = p.fee / 100

    if p.mode == "sweep-dual":
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        n_days = len(df)
        periods = list(range(sma_min, sma_max + 1, p.sma_step))
        n = len(periods)

        # Precompute SMAs
        sma_cache = {}
        for per in periods:
            sma_cache[per] = bt.compute_sma(df, per)
        daily_return = df["close"].pct_change().fillna(0)

        matrix = np.full((n, n), np.nan)
        best_ann = -np.inf
        best_fast = best_slow = None
        for i, fast in enumerate(periods):
            for j, slow in enumerate(periods):
                if fast >= slow:
                    continue
                above_sma = sma_cache[fast] > sma_cache[slow]
                position = bt._apply_exposure(above_sma, p.exposure).shift(1).fillna(0)
                strat_return = position * daily_return
                trade_mask = position.diff().fillna(0).abs() > 0
                strat_return = strat_return.copy()
                strat_return[trade_mask] -= fee
                equity_final = p.initial_cash * (1 + strat_return).prod()
                total_ret = (equity_final / p.initial_cash - 1) * 100
                ann = bt._annualized_return(total_ret, n_days)
                matrix[i, j] = ann
                if ann > best_ann:
                    best_ann = ann
                    best_fast = fast
                    best_slow = slow

        bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_ann = bt._annualized_return(bh_total, n_days)

        fig, ax = plt.subplots(figsize=(14, 12), dpi=150)
        im = ax.imshow(matrix, cmap="RdYlGn", aspect="auto", origin="lower",
                       interpolation="nearest")
        ax.set_xticks(range(n))
        ax.set_xticklabels(periods, rotation=90, fontsize=max(4, min(8, 200 // n)))
        ax.set_yticks(range(n))
        ax.set_yticklabels(periods, fontsize=max(4, min(8, 200 // n)))
        ax.set_xlabel("Slow SMA Period")
        ax.set_ylabel("Fast SMA Period")
        ax.set_title(f"Dual SMA Crossover — Annualized Return % (step={p.sma_step})\n"
                     f"Best: SMA({best_fast}/{best_slow}) = {best_ann:.1f}% | "
                     f"B&H: {bh_ann:.1f}% | {p.exposure}")
        cbar = fig.colorbar(im, ax=ax, shrink=0.8)
        cbar.set_label("Annualized Return (%)")
        if n <= 30:
            for i in range(n):
                for j in range(n):
                    val = matrix[i, j]
                    if not np.isnan(val):
                        color = "black" if abs(val - np.nanmean(matrix)) < np.nanstd(matrix) else "white"
                        ax.text(j, i, f"{val:.0f}", ha="center", va="center",
                                fontsize=max(4, min(7, 150 // n)), color=color)
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png")
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

        # Run full strategy for the best pair to get all metrics
        best_result = bt.run_dual_sma_strategy(df, best_fast, best_slow, p.initial_cash, fee, p.exposure)
        best = _enrich_best(best_result, df)

        return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=None, col_header=col_header, data_start=DATA_START)

    elif p.mode == "sweep-chart":
        # Generate sweep chart to a buffer
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        n_days = len(df)
        periods = list(range(sma_min, sma_max + 1))
        annualized_returns = []
        for period in periods:
            result = bt.run_single_sma_strategy(df, period, p.initial_cash, fee, p.exposure)
            ann = bt._annualized_return(result["total_return"], n_days)
            annualized_returns.append(ann)

        import numpy as np
        bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_annualized = bt._annualized_return(bh_total, n_days)
        best_idx = np.argmax(annualized_returns)
        best_period = periods[best_idx]
        best_ann = annualized_returns[best_idx]

        fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
        ax.plot(periods, annualized_returns, color="steelblue", linewidth=1)
        ax.axhline(y=bh_annualized, color="gray", linestyle="--", linewidth=1,
                    label=f"Buy & Hold ({bh_annualized:.1f}%)")
        ax.scatter([best_period], [best_ann], color="red", s=60, zorder=5,
                    label=f"Best: SMA({best_period}) ({best_ann:.1f}%)")
        ax.set_xlabel("SMA Period (days)")
        ax.set_ylabel("Annualized Return (%)")
        ax.set_title(f"Annualized Return by SMA Period ({sma_min}-{sma_max}) | {p.exposure}")
        ax.legend(loc="best", fontsize=9)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        buf = BytesIO()
        plt.savefig(buf, format="png")
        plt.close()
        buf.seek(0)
        chart_b64 = base64.b64encode(buf.read()).decode()

    else:
        # Single or dual mode
        results = bt.sweep_sma_periods(df, sma_min, sma_max, p.initial_cash, p.mode, p.fast_sma, fee, p.exposure)
        if results:
            best = _enrich_best(results[0], df)
            if p.mode == "dual":
                col_header = "Fast/Slow"
                table_rows = [{"label": f"{r['fast_period']}/{r['sma_period']}", **r} for r in results]
            else:
                table_rows = [{"label": str(r["sma_period"]), **r} for r in results]

            # Generate chart to buffer
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), dpi=150,
                                            gridspec_kw={"height_ratios": [7, 3]}, sharex=True)
            ax1.plot(df.index, df["close"], label="BTC Price", color="black", linewidth=0.8)
            ax1.plot(best["sma_series"].index, best["sma_series"],
                     label=best["label"], color="blue", linewidth=0.8, alpha=0.8)
            if "fast_sma_series" in best:
                ax1.plot(best["fast_sma_series"].index, best["fast_sma_series"],
                         label=f"SMA({best['fast_period']})", color="orange", linewidth=0.8, alpha=0.8)
            ax1.set_yscale("log")
            ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
            ax1.set_ylabel("BTC Price (log scale)")
            ax1.set_title(f"Bitcoin SMA Backtest - Best: {best['label']} "
                          f"({best['total_return']:.1f}% return) | {p.exposure}")
            ax1.legend(loc="upper left", fontsize=8)
            ax1.grid(True, alpha=0.3)

            ax2.plot(best["equity"].index, best["equity"], label="Strategy Equity", color="blue", linewidth=1)
            ax2.plot(best["buyhold"].index, best["buyhold"], label="Buy & Hold", color="gray", linewidth=1, alpha=0.7)
            ax2.set_yscale("log")
            ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
            ax2.set_ylabel("Portfolio Value (log)")
            ax2.set_xlabel("Date")
            ax2.legend(loc="upper left", fontsize=8)
            ax2.grid(True, alpha=0.3)
            ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            ax2.xaxis.set_major_locator(mdates.YearLocator(2))
            plt.tight_layout()

            buf = BytesIO()
            plt.savefig(buf, format="png")
            plt.close()
            buf.seek(0)
            chart_b64 = base64.b64encode(buf.read()).decode()

    return render_template_string(HTML, p=p, chart=chart_b64, best=best, table_rows=table_rows, col_header=col_header, data_start=DATA_START)


if __name__ == "__main__":
    print("Starting BTC SMA Backtester at http://localhost:5000")
    app.run(debug=False, port=5000)
