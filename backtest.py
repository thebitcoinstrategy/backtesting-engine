#!/usr/bin/env python3
"""Backtesting Engine — CLI app to backtest indicator crossover trading strategies."""

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


def compute_ema(df, period):
    """Return exponential moving average Series for the close column."""
    return df["close"].ewm(span=period, adjust=False).mean()


def compute_wma(df, period):
    """Return linearly weighted moving average Series."""
    weights = np.arange(1, period + 1, dtype=float)
    return df["close"].rolling(window=period).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def compute_hma(df, period):
    """Return Hull Moving Average: WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    half_period = max(1, int(period / 2))
    sqrt_period = max(1, int(np.sqrt(period)))
    wma_half = compute_wma(df, half_period)
    wma_full = compute_wma(df, period)
    diff = 2 * wma_half - wma_full
    weights = np.arange(1, sqrt_period + 1, dtype=float)
    return diff.rolling(window=sqrt_period).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def compute_dema(df, period):
    """Return Double Exponential Moving Average: 2*EMA - EMA(EMA)."""
    ema1 = df["close"].ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    return 2 * ema1 - ema2


def compute_tema(df, period):
    """Return Triple Exponential Moving Average: 3*EMA - 3*EMA(EMA) + EMA(EMA(EMA))."""
    ema1 = df["close"].ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return 3 * ema1 - 3 * ema2 + ema3


def compute_kama(df, period):
    """Return Kaufman Adaptive Moving Average using efficiency ratio."""
    close = df["close"].values
    n = len(close)
    kama = np.full(n, np.nan)
    if n <= period:
        return pd.Series(kama, index=df.index)
    kama[period - 1] = close[period - 1]
    fast_sc = 2.0 / (2 + 1)
    slow_sc = 2.0 / (30 + 1)
    for i in range(period, n):
        direction = abs(close[i] - close[i - period])
        volatility = sum(abs(close[j] - close[j - 1]) for j in range(i - period + 1, i + 1))
        er = direction / volatility if volatility != 0 else 0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (close[i] - kama[i - 1])
    return pd.Series(kama, index=df.index)


def compute_zlema(df, period):
    """Return Zero-Lag EMA: EMA of (close + (close - close.shift(lag)))."""
    lag = (period - 1) // 2
    adjusted = df["close"] + (df["close"] - df["close"].shift(lag))
    return adjusted.ewm(span=period, adjust=False).mean()


def compute_smma(df, period):
    """Return Smoothed Moving Average (equivalent to EMA with alpha=1/period)."""
    return df["close"].ewm(alpha=1.0 / period, adjust=False).mean()


def compute_lsma(df, period):
    """Return Least Squares Moving Average (linear regression endpoint)."""
    x = np.arange(period, dtype=float)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()
    def _lsma(vals):
        y_mean = vals.mean()
        slope = ((x - x_mean) * (vals - y_mean)).sum() / x_var
        return y_mean + slope * (period - 1 - x_mean)
    return df["close"].rolling(window=period).apply(_lsma, raw=True)


def compute_alma(df, period, offset=0.85, sigma=6):
    """Return Arnaud Legoux Moving Average with Gaussian weighting."""
    m = offset * (period - 1)
    s = period / sigma
    w = np.exp(-((np.arange(period) - m) ** 2) / (2 * s * s))
    w = w / w.sum()
    return df["close"].rolling(window=period).apply(lambda x: np.dot(x, w), raw=True)


def compute_frama(df, period):
    """Return Fractal Adaptive Moving Average (close-only adaptation)."""
    close = df["close"].values
    n = len(close)
    frama = np.full(n, np.nan)
    half = max(1, period // 2)
    if n < period:
        return pd.Series(frama, index=df.index)
    frama[period - 1] = close[period - 1]
    for i in range(period, n):
        # Fractal dimension from close prices
        h1 = max(close[i - period:i - half]) - min(close[i - period:i - half])
        h2 = max(close[i - half:i]) - min(close[i - half:i])
        h3 = max(close[i - period:i]) - min(close[i - period:i])
        if h1 + h2 > 0 and h3 > 0:
            d = (np.log(h1 + h2) - np.log(h3)) / np.log(2)
        else:
            d = 1.0
        alpha = np.exp(-4.6 * (d - 1))
        alpha = max(0.01, min(1.0, alpha))
        frama[i] = alpha * close[i] + (1 - alpha) * frama[i - 1]
    return pd.Series(frama, index=df.index)


def compute_t3(df, period, vfactor=0.7):
    """Return T3 indicator: 6-pass EMA chain with volume factor."""
    c1 = -(vfactor ** 3)
    c2 = 3 * vfactor ** 2 + 3 * vfactor ** 3
    c3 = -6 * vfactor ** 2 - 3 * vfactor - 3 * vfactor ** 3
    c4 = 1 + 3 * vfactor + vfactor ** 3 + 3 * vfactor ** 2
    e1 = df["close"].ewm(span=period, adjust=False).mean()
    e2 = e1.ewm(span=period, adjust=False).mean()
    e3 = e2.ewm(span=period, adjust=False).mean()
    e4 = e3.ewm(span=period, adjust=False).mean()
    e5 = e4.ewm(span=period, adjust=False).mean()
    e6 = e5.ewm(span=period, adjust=False).mean()
    return c1 * e6 + c2 * e5 + c3 * e4 + c4 * e3


def compute_mcginley(df, period):
    """Return McGinley Dynamic indicator: MD += (close - MD) / (N * (close/MD)^4)."""
    close = df["close"].values
    n = len(close)
    md = np.full(n, np.nan)
    if n == 0:
        return pd.Series(md, index=df.index)
    md[0] = close[0]
    for i in range(1, n):
        if md[i - 1] == 0 or np.isnan(md[i - 1]):
            md[i] = close[i]
        else:
            ratio = close[i] / md[i - 1]
            md[i] = md[i - 1] + (close[i] - md[i - 1]) / (period * ratio ** 4)
    return pd.Series(md, index=df.index)


# --- Indicator Registry ---

INDICATORS = {
    "price":    {"fn": lambda df, period: df["close"], "needs_period": False},
    "sma":      {"fn": compute_sma,      "needs_period": True},
    "ema":      {"fn": compute_ema,      "needs_period": True},
    "wma":      {"fn": compute_wma,      "needs_period": True},
    "hma":      {"fn": compute_hma,      "needs_period": True},
    "dema":     {"fn": compute_dema,     "needs_period": True},
    "tema":     {"fn": compute_tema,     "needs_period": True},
    "kama":     {"fn": compute_kama,     "needs_period": True},
    "zlema":    {"fn": compute_zlema,    "needs_period": True},
    "smma":     {"fn": compute_smma,     "needs_period": True},
    "lsma":     {"fn": compute_lsma,     "needs_period": True},
    "alma":     {"fn": compute_alma,     "needs_period": True},
    "frama":    {"fn": compute_frama,    "needs_period": True},
    "t3":       {"fn": compute_t3,       "needs_period": True},
    "mcginley": {"fn": compute_mcginley, "needs_period": True},
}


def compute_indicator_from_spec(df, name, period=None):
    """Compute an indicator series and its label from the registry."""
    spec = INDICATORS[name]
    if spec["needs_period"]:
        series = spec["fn"](df, period)
        label = f"{name.upper()}({period})"
    else:
        series = spec["fn"](df, period)
        label = "Price"
    return series, label


# --- Oscillator Functions ---

def compute_rsi(df, period=14):
    """Return Relative Strength Index (0-100)."""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_macd(df, fast=12, slow=26, signal=9):
    """Return MACD line, signal line, and histogram."""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_stochastic(df, period=14, smooth_k=3):
    """Return Stochastic %K (smoothed) and %D. Uses rolling high/low of close."""
    high = df["close"].rolling(window=period).max()
    low = df["close"].rolling(window=period).min()
    raw_k = 100 * (df["close"] - low) / (high - low).replace(0, np.nan)
    k = raw_k.rolling(window=smooth_k).mean()
    d = k.rolling(window=3).mean()
    return k, d


def compute_cci(df, period=20):
    """Return Commodity Channel Index. Uses close as typical price proxy."""
    tp = df["close"]
    sma_tp = tp.rolling(window=period).mean()
    mad = tp.rolling(window=period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    cci = (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))
    return cci


def compute_roc(df, period=12):
    """Return Rate of Change as percentage."""
    roc = (df["close"] / df["close"].shift(period) - 1) * 100
    return roc


def compute_momentum(df, period=10):
    """Return raw price momentum (close - close[N])."""
    return df["close"] - df["close"].shift(period)


def compute_williams_r(df, period=14):
    """Return Williams %R (-100 to 0). Uses rolling high/low of close."""
    high = df["close"].rolling(window=period).max()
    low = df["close"].rolling(window=period).min()
    wr = -100 * (high - df["close"]) / (high - low).replace(0, np.nan)
    return wr


# --- Oscillator Registry ---

OSCILLATORS = {
    "rsi": {
        "fn": lambda df, period: (compute_rsi(df, period),),
        "period": 14,
        "buy_threshold": 30,
        "sell_threshold": 70,
        "range": (0, 100),
        "lines": ["RSI"],
        "label": "RSI",
        "description": "Relative Strength Index — buy when dropping below 30 (oversold/cheap), sell when rising above 70 (overbought/expensive)",
    },
    "macd": {
        "fn": lambda df, period: compute_macd(df, fast=12, slow=26, signal=period),
        "period": 9,
        "buy_threshold": 0,
        "sell_threshold": 0,
        "range": None,
        "lines": ["MACD", "Signal"],
        "label": "MACD",
        "signal_mode": "crossover",
        "description": "MACD — buy when MACD crosses above signal line, sell when below",
    },
    "stochastic": {
        "fn": lambda df, period: compute_stochastic(df, period),
        "period": 14,
        "buy_threshold": 20,
        "sell_threshold": 80,
        "range": (0, 100),
        "lines": ["%K", "%D"],
        "label": "Stochastic",
        "description": "Stochastic Oscillator — buy when %K drops below 20 (oversold/cheap), sell when rising above 80 (overbought/expensive)",
    },
    "cci": {
        "fn": lambda df, period: (compute_cci(df, period),),
        "period": 20,
        "buy_threshold": -100,
        "sell_threshold": 100,
        "range": None,
        "lines": ["CCI"],
        "label": "CCI",
        "description": "Commodity Channel Index — buy when dropping below -100 (oversold), sell when rising above +100 (overbought)",
    },
    "roc": {
        "fn": lambda df, period: (compute_roc(df, period),),
        "period": 12,
        "buy_threshold": 0,
        "sell_threshold": 0,
        "range": None,
        "lines": ["ROC"],
        "label": "ROC",
        "description": "Rate of Change — buy when dropping below 0 (negative momentum/cheap), sell when rising above 0",
    },
    "momentum": {
        "fn": lambda df, period: (compute_momentum(df, period),),
        "period": 10,
        "buy_threshold": 0,
        "sell_threshold": 0,
        "range": None,
        "lines": ["MOM"],
        "label": "Momentum",
        "description": "Price Momentum — buy when dropping below 0 (downward momentum/cheap), sell when rising above 0",
    },
    "williams_r": {
        "fn": lambda df, period: (compute_williams_r(df, period),),
        "period": 14,
        "buy_threshold": -80,
        "sell_threshold": -20,
        "range": (-100, 0),
        "lines": ["%R"],
        "label": "Williams %R",
        "description": "Williams %R — buy when dropping below -80 (oversold/cheap), sell when rising above -20 (overbought/expensive)",
    },
}


def compute_oscillator(df, osc_name, period=None):
    """Compute oscillator series. Returns dict with 'primary' series (for signals),
    all component series, and metadata."""
    spec = OSCILLATORS[osc_name]
    p = period if period is not None else spec["period"]
    result = spec["fn"](df, p)

    if osc_name == "macd":
        macd_line, signal_line, histogram = result
        return {
            "primary": macd_line,
            "series": {"MACD": macd_line, "Signal": signal_line, "Histogram": histogram},
            "label": f"MACD(12,26,{p})",
            "spec": spec,
        }
    elif osc_name == "stochastic":
        k, d = result
        return {
            "primary": k,
            "series": {"%K": k, "%D": d},
            "label": f"Stoch({p})",
            "spec": spec,
        }
    else:
        series = result[0]
        return {
            "primary": series,
            "series": {spec["lines"][0]: series},
            "label": f"{spec['label']}({p})",
            "spec": spec,
        }


def _oscillator_signal(osc_data, buy_threshold, sell_threshold):
    """Generate buy/sell boolean signal from oscillator using mean-reversion hysteresis.
    Buy when oscillator drops below buy_threshold (enters oversold zone — it's cheap).
    Hold position until oscillator rises above sell_threshold (enters overbought — it's expensive).
    Stay out until oscillator drops below buy_threshold again.
    Between thresholds: no change, hold whatever state we're in."""
    spec = osc_data["spec"]
    primary = osc_data["primary"]

    if spec.get("signal_mode") == "crossover":
        # MACD: signal is MACD > signal line, thresholds ignored for signal generation
        signal_line = osc_data["series"]["Signal"]
        above = primary > signal_line
        return above

    # Mean-reversion hysteresis:
    #   Oscillator drops below buy_threshold → BUY (cheap/oversold)
    #   Oscillator rises above sell_threshold → SELL (expensive/overbought)
    #   Between thresholds → hold current position
    n = len(primary)
    position = np.zeros(n, dtype=int)
    in_position = False
    for i in range(n):
        val = primary.iloc[i]
        if np.isnan(val):
            position[i] = 1 if in_position else 0
            continue
        if not in_position:
            if val < buy_threshold:
                in_position = True
        else:
            if val > sell_threshold:
                in_position = False
        position[i] = 1 if in_position else 0
    return pd.Series(position, index=primary.index).astype(bool)


def run_oscillator_strategy(df, osc_name, osc_period, buy_threshold, sell_threshold,
                            initial_cash, fee=0.001, exposure="long-cash",
                            long_leverage=1, short_leverage=1, lev_mode="rebalance",
                            reverse=False, sizing="compound", start_date=None):
    """Run strategy based on oscillator threshold signals.
    Returns dict compatible with run_strategy output.
    If start_date is given, indicators are computed on the full df for warmup,
    then data is trimmed to start_date before equity computation."""
    df = df.copy()

    osc_data = compute_oscillator(df, osc_name, osc_period)
    above = _oscillator_signal(osc_data, buy_threshold, sell_threshold)

    if reverse:
        above = ~above
    df["position"] = _apply_exposure(above, exposure).shift(1).fillna(0)

    daily_return = df["close"].pct_change().fillna(0)

    # Trim to start_date AFTER indicators/positions are computed (preserves warmup)
    if start_date is not None:
        ts = pd.Timestamp(start_date)
        if df.index.tz is not None:
            ts = ts.tz_localize(df.index.tz)
        mask = df.index >= ts
        df = df[mask]
        daily_return = daily_return[mask]
        osc_data = {k: (v[mask] if hasattr(v, 'loc') else v) for k, v in osc_data.items()}

    if sizing == "fixed":
        leverage = np.where(df["position"] > 0, long_leverage,
                   np.where(df["position"] < 0, short_leverage, 1))
        daily_pnl = initial_cash * df["position"] * daily_return * leverage
        trade_mask = df["position"].diff().fillna(0).abs() > 0
        daily_pnl[trade_mask] -= initial_cash * fee
        equity_arr = initial_cash + daily_pnl.cumsum().values
        liquidated = False
        df["equity"] = equity_arr
    elif lev_mode == "set-forget":
        equity_arr, liquidated = _compute_equity_set_and_forget(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee)
        df["equity"] = equity_arr
    elif lev_mode == "optimal":
        equity_arr, liquidated = _compute_equity_optimal(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee)
        df["equity"] = equity_arr
    else:
        leverage = np.where(df["position"] > 0, long_leverage,
                   np.where(df["position"] < 0, short_leverage, 1))
        df["strategy_return"] = df["position"] * daily_return * leverage
        trade_mask = df["position"].diff().fillna(0).abs() > 0
        df.loc[trade_mask, "strategy_return"] -= fee
        equity_arr, liquidated = _compute_equity_with_liquidation(df["strategy_return"].values, initial_cash)
        df["equity"] = equity_arr

    df["buyhold"] = initial_cash * (1 + daily_return).cumprod()

    trade_mask = df["position"].diff().fillna(0).abs() > 0
    trades = trade_mask.sum()

    total_return = (df["equity"].iloc[-1] / initial_cash - 1) * 100
    buyhold_return = (df["buyhold"].iloc[-1] / initial_cash - 1) * 100
    max_drawdown_val = _max_drawdown(df["equity"])

    equity_returns = pd.Series(df["equity"].values).pct_change().fillna(0)
    mean_daily = equity_returns.mean()
    std_daily = equity_returns.std()
    sharpe = (mean_daily / std_daily * np.sqrt(365)) if std_daily > 0 else 0.0

    volatility = std_daily * np.sqrt(365) * 100
    sortino = _sortino_ratio(equity_returns)
    beta_val = _beta(equity_returns.values, daily_return.values)
    n_days = len(df)
    ann_ret = _annualized_return(total_return, n_days)
    calmar = abs(ann_ret / max_drawdown_val) if max_drawdown_val != 0 else 0.0
    dd_duration = _max_drawdown_duration(df["equity"])
    yearly = _yearly_returns(df["equity"])
    best_year = max(yearly.items(), key=lambda x: x[1]) if yearly else (None, 0)
    worst_year = min(yearly.items(), key=lambda x: x[1]) if yearly else (None, 0)
    tstats = _trade_stats(df["equity"], df["position"])
    time_in_market = (df["position"] != 0).sum() / len(df) * 100

    pos_diff = df["position"].diff().fillna(0)
    buy_signals = df.index[pos_diff > 0]
    sell_signals = df.index[pos_diff < 0]

    return {
        "osc_name": osc_name,
        "osc_period": osc_period if osc_period is not None else OSCILLATORS[osc_name]["period"],
        "osc_data": osc_data,
        "buy_threshold": buy_threshold,
        "sell_threshold": sell_threshold,
        "label": osc_data["label"],
        "total_return": total_return,
        "buyhold_return": buyhold_return,
        "max_drawdown": max_drawdown_val,
        "trades": int(trades),
        "sharpe": sharpe,
        "volatility": volatility,
        "sortino": sortino,
        "beta": beta_val,
        "calmar": calmar,
        "max_dd_duration": dd_duration,
        "win_rate": tstats["win_rate"],
        "avg_win": tstats["avg_win"],
        "avg_loss": tstats["avg_loss"],
        "profit_factor": tstats["profit_factor"],
        "avg_trade_duration": tstats["avg_trade_duration"],
        "time_in_market": time_in_market,
        "best_year": best_year,
        "worst_year": worst_year,
        "equity": df["equity"],
        "buyhold": df["buyhold"],
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
    }


def compute_indicator(df, period, indicator_type="sma"):
    """Return SMA or EMA Series based on indicator_type. (Legacy wrapper)"""
    if indicator_type == "ema":
        return compute_ema(df, period)
    return compute_sma(df, period)


def _apply_exposure(above_sma, exposure):
    """Convert boolean above/below signal to position based on exposure mode.
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


def _compute_equity_with_liquidation(strategy_returns, initial_cash):
    """Compute equity series with liquidation: if equity hits 0, stay at 0."""
    equity = np.empty(len(strategy_returns))
    val = initial_cash
    liquidated = False
    for i, r in enumerate(strategy_returns):
        if liquidated:
            equity[i] = 0.0
        else:
            val = val * (1 + r)
            if val <= 0:
                val = 0.0
                liquidated = True
            equity[i] = val
    return equity, liquidated


def _compute_equity_set_and_forget(positions, daily_returns, initial_cash, long_leverage, short_leverage, fee):
    """Compute equity with set-and-forget leverage.

    Leverage is applied at position entry and drifts naturally until the position closes.
    Long: equity = entry_equity * (lev * cum_return - (lev - 1))
    Short: equity = entry_equity * (1 + lev * (1 - cum_return))
    """
    n = len(positions)
    equity = np.empty(n)
    current_equity = initial_cash
    current_pos = 0
    cum_return = 1.0
    entry_equity = current_equity
    liquidated = False

    for i in range(n):
        if liquidated:
            equity[i] = 0.0
            continue

        pos = positions[i]
        dr = daily_returns[i]

        if pos != current_pos:
            # Position changed — apply fee, start new position
            current_equity *= (1 - fee)
            current_pos = pos
            entry_equity = current_equity
            cum_return = 1.0

        if current_pos != 0:
            cum_return *= (1 + dr)
            if current_pos > 0:
                lev = long_leverage
                current_equity = entry_equity * (lev * cum_return - (lev - 1))
            else:
                lev = short_leverage
                current_equity = entry_equity * (1 + lev * (1 - cum_return))

        if current_equity <= 0:
            current_equity = 0.0
            liquidated = True

        equity[i] = current_equity

    return equity, liquidated


def _compute_equity_optimal(positions, daily_returns, initial_cash, long_leverage, short_leverage, fee):
    """Compute equity with optimal leverage mode.

    Long positions: daily rebalance (leverage reset each day).
    Short positions: set-and-forget (leverage drifts naturally).
    """
    n = len(positions)
    equity = np.empty(n)
    current_equity = initial_cash
    current_pos = 0
    cum_return = 1.0
    entry_equity = current_equity
    liquidated = False

    for i in range(n):
        if liquidated:
            equity[i] = 0.0
            continue

        pos = positions[i]
        dr = daily_returns[i]

        if pos != current_pos:
            current_equity *= (1 - fee)
            current_pos = pos
            entry_equity = current_equity
            cum_return = 1.0

        if current_pos > 0:
            # Long: daily rebalance
            current_equity *= (1 + dr * long_leverage)
        elif current_pos < 0:
            # Short: set-and-forget
            cum_return *= (1 + dr)
            current_equity = entry_equity * (1 + short_leverage * (1 - cum_return))

        if current_equity <= 0:
            current_equity = 0.0
            liquidated = True

        equity[i] = current_equity

    return equity, liquidated


def _max_drawdown(equity_series):
    """Compute max drawdown as a percentage."""
    cummax = equity_series.cummax()
    drawdown = (equity_series - cummax) / cummax.replace(0, np.nan)
    return drawdown.min() * 100 if not drawdown.isna().all() else -100.0


def _annualized_return(total_return_pct, n_days):
    """Convert total return % over n_days into annualized return %."""
    growth = 1 + total_return_pct / 100
    if growth <= 0 or n_days <= 0:
        return -100.0
    return (growth ** (365 / n_days) - 1) * 100


def _sortino_ratio(daily_returns):
    """Sortino ratio: mean / downside deviation, annualized with sqrt(365)."""
    mean_d = daily_returns.mean()
    downside = daily_returns[daily_returns < 0]
    down_std = downside.std() if len(downside) > 1 else 0.0
    return (mean_d / down_std * np.sqrt(365)) if down_std > 0 else 0.0


def _beta(strategy_returns, market_returns):
    """Beta: covariance(strategy, market) / variance(market)."""
    if len(strategy_returns) < 2:
        return 0.0
    cov = np.cov(strategy_returns, market_returns)
    var_market = cov[1, 1]
    return float(cov[0, 1] / var_market) if var_market > 0 else 0.0


def _max_drawdown_duration(equity_series):
    """Longest peak-to-recovery period in days."""
    cummax = equity_series.cummax()
    in_dd = equity_series < cummax
    max_dur = 0
    cur_dur = 0
    for v in in_dd:
        if v:
            cur_dur += 1
            if cur_dur > max_dur:
                max_dur = cur_dur
        else:
            cur_dur = 0
    return max_dur


def _yearly_returns(equity_series):
    """Return dict of {year: return_pct} for each calendar year."""
    if len(equity_series) < 2:
        return {}
    years = {}
    eq = equity_series
    grouped = eq.groupby(eq.index.year)
    for year, grp in grouped:
        start_val = grp.iloc[0]
        end_val = grp.iloc[-1]
        if start_val > 0:
            years[year] = (end_val / start_val - 1) * 100
    return years


def _trade_stats(equity_series, position_series):
    """Compute trade-level statistics from equity and position series."""
    pos = position_series.values
    eq = equity_series.values
    # Find trade boundaries (where position changes)
    changes = np.where(np.diff(pos, prepend=pos[0] - 1) != 0)[0]
    trades = []
    for i in range(len(changes)):
        start = changes[i]
        end = changes[i + 1] if i + 1 < len(changes) else len(pos)
        if pos[start] == 0:
            continue  # skip cash periods
        entry_eq = eq[start]
        exit_eq = eq[end - 1]
        if entry_eq > 0:
            ret = (exit_eq / entry_eq - 1) * 100
            trades.append({"return": ret, "days": end - start})
    if not trades:
        return {"win_rate": 0, "avg_win": 0, "avg_loss": 0,
                "profit_factor": 0, "avg_trade_duration": 0}
    wins = [t for t in trades if t["return"] > 0]
    losses = [t for t in trades if t["return"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_win = np.mean([t["return"] for t in wins]) if wins else 0.0
    avg_loss = np.mean([t["return"] for t in losses]) if losses else 0.0
    gross_profit = sum(t["return"] for t in wins)
    gross_loss = abs(sum(t["return"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_duration = np.mean([t["days"] for t in trades])
    return {"win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
            "profit_factor": profit_factor, "avg_trade_duration": avg_duration}


# --- Core Strategy Functions ---

def run_strategy(df, ind1_name, ind1_period, ind2_name, ind2_period,
                 initial_cash, fee=0.001, exposure="long-cash",
                 long_leverage=1, short_leverage=1, lev_mode="rebalance",
                 reverse=False, sizing="compound", start_date=None):
    """Unified strategy: go long when ind1 > ind2, apply exposure mode.
    Returns dict with ind1/ind2 series, labels, and all metrics.
    If start_date is given, indicators are computed on the full df for warmup,
    then data is trimmed to start_date before equity computation."""
    df = df.copy()

    ind1_series, ind1_label = compute_indicator_from_spec(df, ind1_name, ind1_period)
    ind2_series, ind2_label = compute_indicator_from_spec(df, ind2_name, ind2_period)

    above = ind1_series > ind2_series
    if reverse:
        above = ~above
    df["position"] = _apply_exposure(above, exposure).shift(1).fillna(0)

    daily_return = df["close"].pct_change().fillna(0)

    # Trim to start_date AFTER indicators/positions are computed (preserves warmup)
    if start_date is not None:
        ts = pd.Timestamp(start_date)
        if df.index.tz is not None:
            ts = ts.tz_localize(df.index.tz)
        mask = df.index >= ts
        df = df[mask]
        daily_return = daily_return[mask]
        ind1_series = ind1_series[mask]
        ind2_series = ind2_series[mask]

    if sizing == "fixed":
        leverage = np.where(df["position"] > 0, long_leverage,
                   np.where(df["position"] < 0, short_leverage, 1))
        daily_pnl = initial_cash * df["position"] * daily_return * leverage
        trade_mask = df["position"].diff().fillna(0).abs() > 0
        daily_pnl[trade_mask] -= initial_cash * fee
        equity_arr = initial_cash + daily_pnl.cumsum().values
        liquidated = False
        df["equity"] = equity_arr
    elif lev_mode == "set-forget":
        equity_arr, liquidated = _compute_equity_set_and_forget(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee)
        df["equity"] = equity_arr
    elif lev_mode == "optimal":
        equity_arr, liquidated = _compute_equity_optimal(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee)
        df["equity"] = equity_arr
    else:
        leverage = np.where(df["position"] > 0, long_leverage,
                   np.where(df["position"] < 0, short_leverage, 1))
        df["strategy_return"] = df["position"] * daily_return * leverage
        trade_mask = df["position"].diff().fillna(0).abs() > 0
        df.loc[trade_mask, "strategy_return"] -= fee
        equity_arr, liquidated = _compute_equity_with_liquidation(df["strategy_return"].values, initial_cash)
        df["equity"] = equity_arr

    df["buyhold"] = initial_cash * (1 + daily_return).cumprod()

    trade_mask = df["position"].diff().fillna(0).abs() > 0
    trades = trade_mask.sum()

    total_return = (df["equity"].iloc[-1] / initial_cash - 1) * 100
    buyhold_return = (df["buyhold"].iloc[-1] / initial_cash - 1) * 100
    max_drawdown = _max_drawdown(df["equity"])

    equity_returns = pd.Series(df["equity"].values).pct_change().fillna(0)
    mean_daily = equity_returns.mean()
    std_daily = equity_returns.std()
    sharpe = (mean_daily / std_daily * np.sqrt(365)) if std_daily > 0 else 0.0

    # Additional metrics
    volatility = std_daily * np.sqrt(365) * 100
    sortino = _sortino_ratio(equity_returns)
    beta_val = _beta(equity_returns.values, daily_return.values)
    n_days = len(df)
    ann_ret = _annualized_return(total_return, n_days)
    calmar = abs(ann_ret / max_drawdown) if max_drawdown != 0 else 0.0
    dd_duration = _max_drawdown_duration(df["equity"])
    yearly = _yearly_returns(df["equity"])
    best_year = max(yearly.items(), key=lambda x: x[1]) if yearly else (None, 0)
    worst_year = min(yearly.items(), key=lambda x: x[1]) if yearly else (None, 0)
    tstats = _trade_stats(df["equity"], df["position"])
    time_in_market = (df["position"] != 0).sum() / len(df) * 100

    pos_diff = df["position"].diff().fillna(0)
    buy_signals = df.index[pos_diff > 0]
    sell_signals = df.index[pos_diff < 0]

    return {
        "ind1_name": ind1_name,
        "ind2_name": ind2_name,
        "ind1_period": ind1_period,
        "ind2_period": ind2_period,
        "ind1_series": ind1_series,
        "ind2_series": ind2_series,
        "ind1_label": ind1_label,
        "ind2_label": ind2_label,
        "label": f"{ind1_label}/{ind2_label}",
        "total_return": total_return,
        "buyhold_return": buyhold_return,
        "max_drawdown": max_drawdown,
        "trades": int(trades),
        "sharpe": sharpe,
        "volatility": volatility,
        "sortino": sortino,
        "beta": beta_val,
        "calmar": calmar,
        "max_dd_duration": dd_duration,
        "win_rate": tstats["win_rate"],
        "avg_win": tstats["avg_win"],
        "avg_loss": tstats["avg_loss"],
        "profit_factor": tstats["profit_factor"],
        "avg_trade_duration": tstats["avg_trade_duration"],
        "time_in_market": time_in_market,
        "best_year": best_year,
        "worst_year": worst_year,
        "equity": df["equity"],
        "buyhold": df["buyhold"],
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
    }


# --- Legacy Wrappers ---

def run_single_sma_strategy(df, sma_period, initial_cash, fee=0.001, exposure="long-cash",
                            long_leverage=1, short_leverage=1, lev_mode="rebalance",
                            indicator_type="sma"):
    """Legacy wrapper: price vs indicator."""
    result = run_strategy(df, "price", None, indicator_type, sma_period,
                          initial_cash, fee, exposure, long_leverage, short_leverage, lev_mode)
    result["sma_period"] = result["ind2_period"]
    result["sma_series"] = result["ind2_series"]
    result["label"] = f"{indicator_type.upper()}({sma_period})"
    return result


def run_dual_sma_strategy(df, fast_period, slow_period, initial_cash, fee=0.001, exposure="long-cash",
                          long_leverage=1, short_leverage=1, lev_mode="rebalance",
                          indicator_type="sma"):
    """Legacy wrapper: fast vs slow indicator crossover."""
    result = run_strategy(df, indicator_type, fast_period, indicator_type, slow_period,
                          initial_cash, fee, exposure, long_leverage, short_leverage, lev_mode)
    result["sma_period"] = result["ind2_period"]
    result["fast_period"] = result["ind1_period"]
    result["sma_series"] = result["ind2_series"]
    result["fast_sma_series"] = result["ind1_series"]
    result["label"] = f"{indicator_type.upper()}({fast_period}/{slow_period})"
    return result


# --- Sweep Functions ---

def sweep_periods(df, ind1_name, ind1_period, ind2_name, ind2_period,
                  sweep_target, sweep_min, sweep_max,
                  initial_cash, fee=0.001, exposure="long-cash",
                  long_leverage=1, short_leverage=1, lev_mode="rebalance",
                  sizing="compound", start_date=None):
    """Sweep one indicator's period across a range. sweep_target: 'ind1' or 'ind2'."""
    results = []
    for period in range(sweep_min, sweep_max + 1):
        p1 = period if sweep_target == "ind1" else ind1_period
        p2 = period if sweep_target == "ind2" else ind2_period
        r = run_strategy(df, ind1_name, p1, ind2_name, p2,
                         initial_cash, fee, exposure, long_leverage, short_leverage, lev_mode,
                         sizing=sizing, start_date=start_date)
        results.append(r)
    results.sort(key=lambda r: r["total_return"], reverse=True)
    return results


def sweep_sma_periods(df, sma_min, sma_max, initial_cash, mode, fast_sma, fee=0.001, exposure="long-cash",
                      long_leverage=1, short_leverage=1, lev_mode="rebalance", indicator_type="sma"):
    """Legacy wrapper for sweep_periods."""
    if mode == "single":
        results = sweep_periods(df, "price", None, indicator_type, None,
                                "ind2", sma_min, sma_max,
                                initial_cash, fee, exposure, long_leverage, short_leverage, lev_mode)
        for r in results:
            r["sma_period"] = r["ind2_period"]
            r["sma_series"] = r["ind2_series"]
            r["label"] = f"{indicator_type.upper()}({r['ind2_period']})"
    else:  # dual
        results = sweep_periods(df, indicator_type, fast_sma, indicator_type, None,
                                "ind2", sma_min, sma_max,
                                initial_cash, fee, exposure, long_leverage, short_leverage, lev_mode)
        results = [r for r in results if r["ind2_period"] > fast_sma]
        for r in results:
            r["sma_period"] = r["ind2_period"]
            r["fast_period"] = r["ind1_period"]
            r["sma_series"] = r["ind2_series"]
            r["fast_sma_series"] = r["ind1_series"]
            r["label"] = f"{indicator_type.upper()}({fast_sma}/{r['ind2_period']})"
        results.sort(key=lambda r: r["total_return"], reverse=True)
    return results


# --- Output Functions ---

def print_results_table(results, mode=None):
    """Print an ASCII table of results."""
    if not results:
        print("No results to display.")
        return

    header = f"{'Strategy':>20} {'Total Ret %':>12} {'B&H Ret %':>12} {'Max DD %':>10} {'Trades':>8} {'Sharpe':>8}"
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)
    for r in results:
        label = r.get("label", "N/A")
        print(
            f"{label:>20s} "
            f"{r['total_return']:>11.2f}% "
            f"{r['buyhold_return']:>11.2f}% "
            f"{r['max_drawdown']:>9.2f}% "
            f"{r['trades']:>8d} "
            f"{r['sharpe']:>8.2f}"
        )
    print(sep)


# --- Chart Generation ---

def _minor_fmt():
    """Return a formatter that shows every 2nd minor tick label as USD."""
    from matplotlib.ticker import FuncFormatter
    state = {"count": 0}
    def _fmt(x, pos):
        state["count"] += 1
        if state["count"] % 2 == 0:
            return ""
        return f"${x:,.2f}" if x < 1 else f"${x:,.0f}"
    return FuncFormatter(_fmt)


def run_regression_analysis(df, osc_name, osc_period, forward_days=365,
                            buy_threshold=None, sell_threshold=None):
    """Analyze relationship between oscillator values and forward N-day returns.
    Returns dict with scatter data, regression stats, and zone analysis."""
    from scipy import stats

    osc_data = compute_oscillator(df, osc_name, osc_period)
    spec = osc_data["spec"]
    primary = osc_data["primary"]

    if buy_threshold is None:
        buy_threshold = spec["buy_threshold"]
    if sell_threshold is None:
        sell_threshold = spec["sell_threshold"]

    # Forward return: (close[i+forward_days] / close[i]) - 1 as percentage
    forward_return = (df["close"].shift(-forward_days) / df["close"] - 1) * 100

    # Combine and drop NaN
    combined = pd.DataFrame({
        "osc": primary,
        "fwd_return": forward_return,
    }).dropna()

    osc_values = combined["osc"].values
    forward_returns = combined["fwd_return"].values
    n_points = len(combined)

    # Linear regression
    if n_points >= 3:
        slope, intercept, r_value, p_value, std_err = stats.linregress(osc_values, forward_returns)
        spearman_r, spearman_p = stats.spearmanr(osc_values, forward_returns)
    else:
        slope = intercept = r_value = p_value = std_err = 0
        spearman_r = spearman_p = 0

    r_squared = r_value ** 2

    # Zone analysis
    def _zone_stats(mask):
        vals = forward_returns[mask]
        if len(vals) == 0:
            return {"mean": 0, "median": 0, "count": 0, "win_rate": 0}
        return {
            "mean": float(np.mean(vals)),
            "median": float(np.median(vals)),
            "count": int(len(vals)),
            "win_rate": float(np.sum(vals > 0) / len(vals) * 100),
        }

    oversold_mask = osc_values < buy_threshold
    overbought_mask = osc_values > sell_threshold
    neutral_mask = ~oversold_mask & ~overbought_mask

    zone_stats = {
        "oversold": _zone_stats(oversold_mask),
        "neutral": _zone_stats(neutral_mask),
        "overbought": _zone_stats(overbought_mask),
    }

    return {
        "osc_data": osc_data,
        "osc_values": osc_values,
        "forward_returns": forward_returns,
        "forward_days": forward_days,
        "r_squared": r_squared,
        "pearson_r": r_value,
        "spearman_r": spearman_r,
        "spearman_p": spearman_p,
        "p_value": p_value,
        "slope": slope,
        "intercept": intercept,
        "std_err": std_err,
        "n_points": n_points,
        "zone_stats": zone_stats,
        "buy_threshold": buy_threshold,
        "sell_threshold": sell_threshold,
    }


def sweep_regression_r_squared(df, osc_name, osc_period, buy_threshold=None, sell_threshold=None,
                               sweep_min=1, sweep_max=361, sweep_step=10):
    """Sweep forward_days and return R² for each. Returns dict with days list and r_squared list."""
    from scipy import stats

    osc_data = compute_oscillator(df, osc_name, osc_period)
    spec = osc_data["spec"]
    primary = osc_data["primary"]

    if buy_threshold is None:
        buy_threshold = spec["buy_threshold"]
    if sell_threshold is None:
        sell_threshold = spec["sell_threshold"]

    days_list = list(range(sweep_min, sweep_max + 1, sweep_step))
    r_squared_list = []
    spearman_list = []

    for fwd in days_list:
        forward_return = (df["close"].shift(-fwd) / df["close"] - 1) * 100
        combined = pd.DataFrame({"osc": primary, "fwd": forward_return}).dropna()
        if len(combined) >= 3:
            _, _, r_value, _, _ = stats.linregress(combined["osc"].values, combined["fwd"].values)
            sp_r, _ = stats.spearmanr(combined["osc"].values, combined["fwd"].values)
            r_squared_list.append(r_value ** 2)
            spearman_list.append(abs(sp_r))
        else:
            r_squared_list.append(0)
            spearman_list.append(0)

    best_idx = int(np.argmax(r_squared_list))

    return {
        "days": days_list,
        "r_squared": r_squared_list,
        "spearman": spearman_list,
        "best_days": days_list[best_idx],
        "best_r_squared": r_squared_list[best_idx],
        "osc_label": osc_data["label"],
    }


def generate_regression_sweep_chart(sweep_result):
    """Generate line chart of R² vs forward days. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from io import BytesIO
    import base64

    days = sweep_result["days"]
    r_sq = sweep_result["r_squared"]
    spearman = sweep_result["spearman"]

    fig, ax = plt.subplots(figsize=(14, 5), dpi=150)
    _apply_dark_theme(fig, ax)

    ax.plot(days, r_sq, color="#6495ED", linewidth=1.5, label="R²")
    ax.scatter([sweep_result["best_days"]], [sweep_result["best_r_squared"]],
               color="#f7931a", s=60, zorder=5,
               label=f"Best R²: {sweep_result['best_days']}d ({sweep_result['best_r_squared']:.4f})")

    ax.set_xlabel("Forward Days")
    ax.set_ylabel("R²")
    ax.set_title(f"{sweep_result['osc_label']} — Predictive Power by Forward Horizon")
    ax.legend(loc="best", fontsize=9, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
    ax.grid(True, alpha=0.3, color="#252a3a")
    ax.set_xlim(days[0], days[-1])
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def generate_regression_chart(result):
    """Generate scatter plot of oscillator values vs forward returns. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    osc_values = result["osc_values"]
    forward_returns = result["forward_returns"]
    buy_thr = result["buy_threshold"]
    sell_thr = result["sell_threshold"]

    fig, ax = plt.subplots(figsize=(14, 9), dpi=150)
    _apply_dark_theme(fig, ax)

    # Color points by zone
    oversold_mask = osc_values < buy_thr
    overbought_mask = osc_values > sell_thr
    neutral_mask = ~oversold_mask & ~overbought_mask

    ax.scatter(osc_values[neutral_mask], forward_returns[neutral_mask],
               c="#8890a4", alpha=0.25, s=8, label="Neutral", rasterized=True)
    ax.scatter(osc_values[oversold_mask], forward_returns[oversold_mask],
               c="#34d399", alpha=0.35, s=12, label="Oversold", rasterized=True)
    ax.scatter(osc_values[overbought_mask], forward_returns[overbought_mask],
               c="#ef4444", alpha=0.35, s=12, label="Overbought", rasterized=True)

    # Regression line
    x_range = np.linspace(osc_values.min(), osc_values.max(), 100)
    y_pred = result["slope"] * x_range + result["intercept"]
    ax.plot(x_range, y_pred, color="#f7931a", linewidth=2, label="Regression line")

    # Threshold lines
    ax.axvline(x=buy_thr, color="#34d399", linestyle="--", linewidth=1, alpha=0.7,
               label=f"Buy threshold ({buy_thr})")
    ax.axvline(x=sell_thr, color="#ef4444", linestyle="--", linewidth=1, alpha=0.7,
               label=f"Sell threshold ({sell_thr})")

    # Zero return line
    ax.axhline(y=0, color="#8890a4", linestyle="--", linewidth=0.8, alpha=0.5)

    # Stats annotation
    stats_text = (
        f"R² = {result['r_squared']:.4f}\n"
        f"Pearson r = {result['pearson_r']:.4f}\n"
        f"Spearman ρ = {result['spearman_r']:.4f}\n"
        f"p-value = {result['p_value']:.2e}\n"
        f"n = {result['n_points']:,}"
    )
    ax.text(0.02, 0.97, stats_text, transform=ax.transAxes, fontsize=10,
            verticalalignment="top", fontfamily="monospace",
            color="#e8eaf0", bbox=dict(boxstyle="round,pad=0.5",
            facecolor="#161922", edgecolor="#252a3a", alpha=0.9))

    osc_label = result["osc_data"]["label"]
    ax.set_xlabel(f"{osc_label} Value")
    ax.set_ylabel(f"Forward {result['forward_days']}-Day Return (%)")
    ax.set_title(f"{osc_label} vs Forward {result['forward_days']}-Day Return — Regression Analysis")
    ax.legend(loc="upper right", fontsize=8, facecolor="#161922", edgecolor="#252a3a", labelcolor="#e8eaf0")
    ax.grid(True, alpha=0.3, color="#252a3a")
    plt.tight_layout()

    from io import BytesIO
    import base64
    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def _apply_dark_theme(fig, axes):
    """Apply the web UI's dark color palette to a matplotlib figure and axes."""
    BG = "#080a10"
    PANEL = "#161922"
    TEXT = "#e8eaf0"
    MUTED = "#8890a4"
    GRID = "#252a3a"

    fig.patch.set_facecolor(PANEL)
    if not hasattr(axes, '__iter__'):
        axes = [axes]
    for ax in axes:
        ax.set_facecolor(BG)
        ax.tick_params(colors=MUTED, which="both")
        ax.xaxis.label.set_color(MUTED)
        ax.yaxis.label.set_color(MUTED)
        ax.title.set_color(TEXT)
        for spine in ax.spines.values():
            spine.set_color(GRID)

    # URL branding at bottom right
    fig.text(0.98, 0.01, "the-bitcoin-strategy.com", fontsize=9,
             color="#ffffff", alpha=0.5, ha="right", va="bottom",
             transform=fig.transFigure)


def generate_chart(df, best_result, output_path, asset_name="Bitcoin"):
    """Generate a two-panel PNG chart: price+indicators with markers, and equity curves."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 10), dpi=150,
        gridspec_kw={"height_ratios": [7, 3]}, sharex=True
    )
    _apply_dark_theme(fig, [ax1, ax2])

    # Top panel: price + indicators + buy/sell markers
    ax1.plot(df.index, df["close"], label=f"{asset_name} Price", color="#e8eaf0", linewidth=0.8)

    # Plot ind2 (always — the main/slow indicator)
    ax1.plot(
        best_result["ind2_series"].index, best_result["ind2_series"],
        label=best_result["ind2_label"], color="#6495ED", linewidth=0.8, alpha=0.8
    )

    # Plot ind1 if not price (crossover strategy)
    if best_result.get("ind1_name") != "price":
        ax1.plot(
            best_result["ind1_series"].index, best_result["ind1_series"],
            label=best_result["ind1_label"], color="#f7931a", linewidth=0.8, alpha=0.8
        )

    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
    ax1.yaxis.set_minor_formatter(_minor_fmt())
    ax1.tick_params(axis='y', which='minor', labelsize=6)
    ax1.set_ylabel(f"{asset_name} Price (log scale)")
    ax1.set_title(f"{asset_name} Backtest — Best: {best_result['label']} "
                  f"({best_result['total_return']:.1f}% return)")
    ax1.legend(loc="upper left", fontsize=8, facecolor="#161922", edgecolor="#252a3a",
               labelcolor="#e8eaf0")
    ax1.grid(True, which="major", alpha=0.3, color="#252a3a")
    ax1.grid(True, which="minor", alpha=0.15, color="#252a3a")

    # Bottom panel: equity curve vs buy-and-hold
    ax2.plot(best_result["equity"].index, best_result["equity"],
             label="Strategy Equity", color="#6495ED", linewidth=1)
    ax2.plot(best_result["buyhold"].index, best_result["buyhold"],
             label="Buy & Hold", color="#8890a4", linewidth=1, alpha=0.7)
    ax2.set_yscale("log")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
    ax2.yaxis.set_minor_formatter(_minor_fmt())
    ax2.tick_params(axis='y', which='minor', labelsize=6)
    ax2.set_ylabel("Portfolio Value (log)")
    ax2.set_xlabel("Date")
    ax2.legend(loc="upper left", fontsize=8, facecolor="#161922", edgecolor="#252a3a",
               labelcolor="#e8eaf0")
    ax2.grid(True, which="major", alpha=0.3, color="#252a3a")
    ax2.grid(True, which="minor", alpha=0.15, color="#252a3a")

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    import math
    date_range_years = (df.index[-1] - df.index[0]).days / 365.25
    year_step = max(1, math.ceil(date_range_years / 18))
    ax2.xaxis.set_major_locator(mdates.YearLocator(year_step))

    plt.tight_layout()
    plt.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close()
    print(f"Chart saved to {output_path}")


def generate_sweep_chart(df, ind1_name, ind1_period, ind2_name, sweep_min, sweep_max,
                         initial_cash, output_path, fee=0.001, exposure="long-cash",
                         long_leverage=1, short_leverage=1, lev_mode="rebalance"):
    """Sweep ind2 period from sweep_min to sweep_max and plot annualized return vs period."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n_days = len(df)
    periods = list(range(sweep_min, sweep_max + 1))
    annualized_returns = []

    ind1_label_str = f"{ind1_name.upper()}({ind1_period})" if ind1_name != "price" else "Price"
    ind2_upper = ind2_name.upper()

    print(f"Sweeping {ind1_label_str} vs {ind2_upper} periods {sweep_min} to {sweep_max} "
          f"({len(periods)} strategies, exposure: {exposure})...")
    print(f"Trading fee: {fee * 100:.2f}% per transaction")

    for period in periods:
        result = run_strategy(df, ind1_name, ind1_period, ind2_name, period,
                              initial_cash, fee, exposure, long_leverage, short_leverage, lev_mode)
        ann = _annualized_return(result["total_return"], n_days)
        annualized_returns.append(ann)

    # Buy-and-hold annualized return for reference line
    bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    bh_annualized = _annualized_return(bh_total, n_days)

    best_idx = np.argmax(annualized_returns)
    best_period = periods[best_idx]
    best_ann = annualized_returns[best_idx]

    if ind1_name != "price":
        best_label = f"{ind1_label_str}/{ind2_upper}({best_period})"
    else:
        best_label = f"{ind2_upper}({best_period})"

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
    _apply_dark_theme(fig, ax)

    ax.plot(periods, annualized_returns, color="#6495ED", linewidth=1)
    ax.axhline(y=bh_annualized, color="#8890a4", linestyle="--", linewidth=1,
               label=f"Buy & Hold ({bh_annualized:.1f}%)")
    ax.scatter([best_period], [best_ann], color="#f7931a", s=60, zorder=5,
               label=f"Best: {best_label} ({best_ann:.1f}%)")

    ax.set_xlabel(f"{ind2_upper} Period (days)")
    ax.set_ylabel("Annualized Return (%)")
    title_prefix = f"{ind1_label_str} vs " if ind1_name != "price" else ""
    ax.set_title(f"Annualized Return by {title_prefix}{ind2_upper} Period ({sweep_min}\u2013{sweep_max})")
    ax.legend(loc="best", fontsize=9, facecolor="#161922", edgecolor="#252a3a",
              labelcolor="#e8eaf0")
    ax.grid(True, alpha=0.3, color="#252a3a")

    plt.tight_layout()
    plt.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close()
    print(f"Best: {best_label} with {best_ann:.2f}% annualized return")
    print(f"Buy & Hold: {bh_annualized:.2f}% annualized")
    print(f"Chart saved to {output_path}")


def generate_dual_sweep_heatmap(df, ind1_name, ind2_name,
                                 period_min, period_max, period_step,
                                 initial_cash, output_path, fee=0.001, exposure="long-cash",
                                 long_leverage=1, short_leverage=1, sizing="compound"):
    """Sweep all ind1/ind2 period permutations and generate a heatmap of annualized returns."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n_days = len(df)
    periods = list(range(period_min, period_max + 1, period_step))
    n = len(periods)
    same_type = (ind1_name == ind2_name)

    ind1_upper = ind1_name.upper()
    ind2_upper = ind2_name.upper()

    print(f"Sweeping {ind1_upper}/{ind2_upper} crossovers: {n}x{n} grid "
          f"(periods {period_min}-{period_max}, step {period_step})...")

    # Precompute all indicators
    ind1_cache = {}
    ind2_cache = {}
    for p in periods:
        ind1_cache[p], _ = compute_indicator_from_spec(df, ind1_name, p)
        if same_type:
            ind2_cache[p] = ind1_cache[p]
        else:
            ind2_cache[p], _ = compute_indicator_from_spec(df, ind2_name, p)

    daily_return = df["close"].pct_change().fillna(0)

    # Build matrix: rows = ind1 period, cols = ind2 period
    matrix = np.full((n, n), np.nan)
    best_ann = -np.inf
    best_p1 = best_p2 = None

    for i, p1 in enumerate(periods):
        for j, p2 in enumerate(periods):
            if same_type and p1 >= p2:
                continue
            above = ind1_cache[p1] > ind2_cache[p2]
            position = _apply_exposure(above, exposure).shift(1).fillna(0)
            leverage = np.where(position > 0, long_leverage,
                       np.where(position < 0, short_leverage, 1))
            if sizing == "fixed":
                daily_pnl = initial_cash * position * daily_return * leverage
                trade_mask = position.diff().fillna(0).abs() > 0
                daily_pnl = daily_pnl.copy()
                daily_pnl[trade_mask] -= initial_cash * fee
                equity_arr = initial_cash + daily_pnl.cumsum().values
            else:
                strat_return = position * daily_return * leverage
                trade_mask = position.diff().fillna(0).abs() > 0
                strat_return = strat_return.copy()
                strat_return[trade_mask] -= fee
                equity_arr, _ = _compute_equity_with_liquidation(strat_return.values, initial_cash)
            equity_final = equity_arr[-1] if len(equity_arr) > 0 else initial_cash
            total_ret = (equity_final / initial_cash - 1) * 100
            ann = _annualized_return(total_ret, n_days)
            matrix[i, j] = ann
            if ann > best_ann:
                best_ann = ann
                best_p1 = p1
                best_p2 = p2

    print(f"Best: {ind1_upper}({best_p1})/{ind2_upper}({best_p2}) with {best_ann:.2f}% annualized return")

    bh_total = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    bh_ann = _annualized_return(bh_total, n_days)
    print(f"Buy & Hold: {bh_ann:.2f}% annualized")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 12), dpi=150)
    _apply_dark_theme(fig, ax)

    im = ax.imshow(matrix, cmap="RdYlGn", aspect="auto", origin="lower",
                   interpolation="nearest")

    ax.set_xticks(range(n))
    ax.set_xticklabels(periods, rotation=90, fontsize=max(4, min(8, 200 // n)))
    ax.set_yticks(range(n))
    ax.set_yticklabels(periods, fontsize=max(4, min(8, 200 // n)))

    if same_type:
        ax.set_xlabel(f"Slow {ind1_upper} Period")
        ax.set_ylabel(f"Fast {ind1_upper} Period")
    else:
        ax.set_xlabel(f"{ind2_upper} Period")
        ax.set_ylabel(f"{ind1_upper} Period")

    ax.set_title(f"{ind1_upper}/{ind2_upper} Crossover \u2014 Annualized Return % (step={period_step})\n"
                 f"Best: {ind1_upper}({best_p1})/{ind2_upper}({best_p2}) = {best_ann:.1f}% | "
                 f"B&H: {bh_ann:.1f}% | {exposure}")

    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label("Annualized Return (%)", color="#8890a4")
    cbar.ax.yaxis.set_tick_params(color="#8890a4")
    cbar.outline.set_edgecolor("#252a3a")
    for label in cbar.ax.get_yticklabels():
        label.set_color("#8890a4")

    if n <= 30:
        for i in range(n):
            for j in range(n):
                val = matrix[i, j]
                if not np.isnan(val):
                    color = "black" if abs(val - np.nanmean(matrix)) < np.nanstd(matrix) else "white"
                    ax.text(j, i, f"{val:.0f}", ha="center", va="center",
                            fontsize=max(4, min(7, 150 // n)), color=color)

    plt.tight_layout()
    plt.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close()
    print(f"Chart saved to {output_path}")

    return matrix, periods, best_p1, best_p2, best_ann


# --- CLI ---

EXAMPLES = """\
Examples (new style — any indicator vs any indicator):

  python backtest.py --ind2 sma --period2 40                    # Price vs SMA(40) backtest
  python backtest.py --ind2 ema --period2 40                    # Price vs EMA(40) backtest
  python backtest.py --ind1 ema --period1 20 --ind2 sma --period2 100  # EMA(20)/SMA(100) crossover
  python backtest.py --ind1 sma --period1 10 --ind2 ema --period2 50   # SMA(10)/EMA(50) crossover
  python backtest.py --ind2 ema --mode sweep-chart              # sweep EMA periods, find best
  python backtest.py --ind1 sma --ind2 ema --mode sweep-dual    # SMA/EMA crossover heatmap
  python backtest.py --ind1 ema --period1 20 --ind2 sma --mode sweep-chart  # EMA(20) vs SMA sweep

Examples (classic style — still works):

  python backtest.py                              # sweep SMA 2-365, table + chart
  python backtest.py --sma 40                     # single SMA chart
  python backtest.py --mode dual                  # dual SMA crossover (default: fast=20)
  python backtest.py --mode sweep-chart           # annualized return vs SMA period chart
  python backtest.py --mode sweep-dual            # dual crossover heatmap (default: step=5)
  python backtest.py --mode sweep-dual --sma-step 10  # coarser grid for speed
  python backtest.py --indicator ema              # use EMA instead of SMA
  python backtest.py --indicator ema --sma 40     # single EMA(40) chart
  python backtest.py --indicator ema --mode dual  # dual EMA crossover
  python backtest.py --exposure long-cash          # long above SMA, cash below (default)
  python backtest.py --exposure short-cash         # cash above SMA, short below
  python backtest.py --exposure long-short         # long above SMA, short below
  python backtest.py --fee 0.5                    # custom fee (default: 0.1%)
  python backtest.py --fee 0                      # no fees
  python backtest.py --long-leverage 2             # 2x leverage on long positions (default: 1)
  python backtest.py --short-leverage 3            # 3x leverage on short positions (default: 1)
  python backtest.py --long-leverage 2 --short-leverage 2 --exposure long-short  # leveraged long-short
  python backtest.py --lev-mode optimal             # rebalance long, set-forget short (default)
  python backtest.py --lev-mode rebalance           # reset leverage daily on all positions
  python backtest.py --lev-mode set-forget          # set leverage once, let it drift
  python backtest.py --sma-min 10 --sma-max 100   # custom SMA range (default: 2-365)
  python backtest.py --initial-cash 50000         # custom starting capital (default: 10000)
  python backtest.py --asset ethereum             # use ethereum data (default: bitcoin)
  python backtest.py --asset ethereum --sma 40   # ethereum with SMA 40
  python backtest.py --start-date 2017-01-01     # filter start date (default: all data)
  python backtest.py --end-date 2023-12-31       # filter end date (default: all data)
  python backtest.py --help                       # show all parameters
  python app.py                                   # launch web interface on port 5000

Examples (oscillators — threshold-based signals):

  python backtest.py --oscillator rsi                           # RSI(14) with default thresholds (buy>30, sell<70)
  python backtest.py --oscillator rsi --osc-period 21           # RSI(21) custom period
  python backtest.py --oscillator rsi --buy-threshold 25 --sell-threshold 75  # Custom thresholds
  python backtest.py --oscillator macd                          # MACD(12,26,9) signal line crossover
  python backtest.py --oscillator stochastic                    # Stochastic(14) buy>20, sell<80
  python backtest.py --oscillator cci --osc-period 20           # CCI(20) buy>-100, sell<100
  python backtest.py --oscillator roc                           # ROC(12) zero-line crossover
  python backtest.py --oscillator momentum                      # Momentum(10) zero-line crossover
  python backtest.py --oscillator williams_r                    # Williams %R(14) buy>-80, sell<-20
"""


def main():
    parser = argparse.ArgumentParser(
        description="Backtest indicator crossover trading strategies on historical data.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--examples", action="store_true", help="Show usage examples and exit")
    parser.add_argument("--asset", default="bitcoin",
                        help="Asset name — looks for data/{asset}.csv (default: bitcoin)")
    parser.add_argument("--data", default=None, help="CSV file path (overrides --asset)")

    # New indicator args
    parser.add_argument("--ind1", choices=list(INDICATORS.keys()), default=None,
                        help="Indicator 1 type: price, sma, ema (default: price, or auto for crossover modes)")
    parser.add_argument("--period1", type=int, default=None,
                        help="Period for indicator 1 (ignored when ind1=price)")
    parser.add_argument("--ind2", choices=list(INDICATORS.keys()), default=None,
                        help="Indicator 2 type: price, sma, ema (default: from --indicator or sma)")
    parser.add_argument("--period2", type=int, default=None,
                        help="Period for indicator 2")

    # Legacy args (still work)
    parser.add_argument("--sma", type=int, default=None, help="Single SMA period (shorthand for --period2)")
    parser.add_argument("--sma-min", type=int, default=2, help="Shortest sweep period (default: 2)")
    parser.add_argument("--sma-max", type=int, default=365, help="Longest sweep period (default: 365)")
    parser.add_argument("--initial-cash", type=float, default=10000, help="Starting capital")
    parser.add_argument("--indicator", choices=["sma", "ema"], default="sma",
                        help="Indicator type for legacy modes (default: sma)")
    parser.add_argument("--mode", choices=["single", "dual", "sweep-chart", "sweep-dual"], default="single",
                        help="single, dual, sweep-chart, sweep-dual")
    parser.add_argument("--sma-step", type=int, default=5,
                        help="Step for heatmap grid (default: 5)")
    parser.add_argument("--fast-sma", type=int, default=20, help="Fast SMA period (dual mode)")
    parser.add_argument("--exposure", choices=["long-cash", "short-cash", "long-short"],
                        default="long-cash",
                        help="long-cash | short-cash | long-short (default: long-cash)")
    parser.add_argument("--fee", type=float, default=0.1,
                        help="Trading fee per transaction in percent (default: 0.1)")
    parser.add_argument("--long-leverage", type=float, default=1,
                        help="Leverage multiplier for long positions (default: 1)")
    parser.add_argument("--short-leverage", type=float, default=1,
                        help="Leverage multiplier for short positions (default: 1)")
    parser.add_argument("--lev-mode", choices=["rebalance", "set-forget", "optimal"], default="optimal",
                        help="rebalance | set-forget | optimal (default: optimal)")
    parser.add_argument("--start-date", default="2015-01-01",
                        help="Start date YYYY-MM-DD (default: 2015-01-01)")
    parser.add_argument("--end-date", default=None,
                        help="End date YYYY-MM-DD (default: end of data)")
    parser.add_argument("--chart-file", default=None,
                        help="Output chart filename (auto-generated if omitted)")

    # Oscillator args
    parser.add_argument("--oscillator", choices=list(OSCILLATORS.keys()), default=None,
                        help="Oscillator type: rsi, macd, stochastic, cci, roc, momentum, williams_r")
    parser.add_argument("--osc-period", type=int, default=None,
                        help="Oscillator period (default varies by oscillator)")
    parser.add_argument("--buy-threshold", type=float, default=None,
                        help="Buy when oscillator crosses above this threshold")
    parser.add_argument("--sell-threshold", type=float, default=None,
                        help="Sell when oscillator crosses below this threshold")
    args = parser.parse_args()

    if args.examples:
        print(EXAMPLES)
        return

    # --- Oscillator mode ---
    if args.oscillator:
        osc_name = args.oscillator
        osc_spec = OSCILLATORS[osc_name]
        osc_period = args.osc_period if args.osc_period is not None else osc_spec["period"]
        buy_thr = args.buy_threshold if args.buy_threshold is not None else osc_spec["buy_threshold"]
        sell_thr = args.sell_threshold if args.sell_threshold is not None else osc_spec["sell_threshold"]

        data_path = args.data or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", f"{args.asset}.csv"
        )
        df = load_data(data_path)
        if args.start_date:
            df = df[df.index >= pd.Timestamp(args.start_date, tz="UTC")]
        if args.end_date:
            df = df[df.index <= pd.Timestamp(args.end_date, tz="UTC")]

        fee = args.fee / 100
        result = run_oscillator_strategy(df, osc_name, osc_period, buy_thr, sell_thr,
                                          args.initial_cash, fee, args.exposure,
                                          args.long_leverage, args.short_leverage, args.lev_mode)
        print(f"\n{osc_spec['label']}({osc_period}) — Buy > {buy_thr}, Sell < {sell_thr}")
        print(f"  Total Return: {result['total_return']:.2f}%")
        print(f"  Buy & Hold:   {result['buyhold_return']:.2f}%")
        print(f"  Max Drawdown: {result['max_drawdown']:.2f}%")
        print(f"  Trades:       {result['trades']}")
        print(f"  Sharpe:       {result['sharpe']:.3f}")
        return

    # --- Resolve indicator settings from new + legacy args ---

    # ind2: new --ind2 takes priority over legacy --indicator
    ind2_name = args.ind2 if args.ind2 is not None else args.indicator

    # ind1: new --ind1 takes priority; auto-set for crossover modes
    if args.ind1 is not None:
        ind1_name = args.ind1
    elif args.mode in ("dual", "sweep-dual"):
        ind1_name = ind2_name  # same type for legacy crossover modes
    else:
        ind1_name = "price"

    # period2: new --period2 takes priority over legacy --sma
    if args.period2 is not None:
        ind2_period = args.period2
    elif args.sma is not None:
        ind2_period = args.sma
    else:
        ind2_period = None

    # period1: new --period1 takes priority over legacy --fast-sma
    if args.period1 is not None:
        ind1_period = args.period1
    elif args.mode in ("dual", "sweep-dual"):
        ind1_period = args.fast_sma
    else:
        ind1_period = None

    # Narrow sweep range if a specific period is set
    sma_min = args.sma_min
    sma_max = args.sma_max
    if args.sma is not None and args.period2 is None:
        sma_min = args.sma
        sma_max = args.sma
    if ind2_period is not None and args.mode in ("single", "dual"):
        sma_min = ind2_period
        sma_max = ind2_period

    # Validate
    if ind1_name == "price" and ind2_name == "price":
        print("Error: Both indicators cannot be 'price'.")
        sys.exit(1)
    if ind1_name == "price" and args.mode == "sweep-dual" and args.ind1 is None:
        # Legacy sweep-dual auto-promotes ind1
        ind1_name = ind2_name
        ind1_period = args.fast_sma

    fee = args.fee / 100

    # Resolve mode
    mode = args.mode

    # Auto-generate chart filename
    if args.chart_file is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        asset = args.asset
        exp = f"_{args.exposure}"
        if mode == "sweep-dual":
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_{asset}_heatmap_{ind1_name}-{ind2_name}_{sma_min}-{sma_max}_step{args.sma_step}{exp}.png")
        elif mode == "sweep-chart":
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_{asset}_sweep_{ind1_name}-{ind2_name}_{sma_min}-{sma_max}{exp}.png")
        elif mode == "dual":
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_{asset}_backtest_{ind1_name}{ind1_period}_{ind2_name}{sma_min}-{sma_max}{exp}.png")
        else:
            args.chart_file = os.path.join(RESULTS_DIR, f"{ts}_{asset}_backtest_{ind1_name}-{ind2_name}_{sma_min}-{sma_max}{exp}.png")

    # Load data
    if args.data is None:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        args.data = os.path.join(data_dir, f"{args.asset}.csv")

    print(f"Loading {args.asset} data from {args.data}...")
    df = load_data(args.data)
    if args.start_date:
        df = df[df.index >= pd.Timestamp(args.start_date, tz="UTC")]
    if args.end_date:
        df = df[df.index <= pd.Timestamp(args.end_date, tz="UTC")]
    print(f"Loaded {len(df)} daily rows from {df.index[0].date()} to {df.index[-1].date()}")

    print(f"Trading fee: {args.fee:.2f}% per transaction | Exposure: {args.exposure}")

    long_lev = args.long_leverage
    short_lev = args.short_leverage
    lev_mode = args.lev_mode

    # --- Execute based on mode ---

    if mode == "sweep-dual":
        # Heatmap: sweep both indicator periods
        if ind1_name == "price":
            ind1_name = ind2_name
            ind1_period = args.fast_sma

        generate_dual_sweep_heatmap(df, ind1_name, ind2_name,
                                     sma_min, sma_max, args.sma_step,
                                     args.initial_cash, args.chart_file, fee, args.exposure,
                                     long_lev, short_lev)
        return

    if mode == "sweep-chart":
        # Line chart: sweep ind2 period
        generate_sweep_chart(df, ind1_name, ind1_period, ind2_name, sma_min, sma_max,
                             args.initial_cash, args.chart_file, fee, args.exposure,
                             long_lev, short_lev, lev_mode)
        return

    # Single or dual mode: sweep range or single run
    ind1_desc = f"{ind1_name.upper()}({ind1_period})" if ind1_name != "price" else "Price"
    ind2_desc = ind2_name.upper()
    print(f"\nRunning {ind1_desc} vs {ind2_desc} sweep ({sma_min}-{sma_max})...")

    results = sweep_periods(df, ind1_name, ind1_period, ind2_name, None,
                            "ind2", sma_min, sma_max,
                            args.initial_cash, fee, args.exposure,
                            long_lev, short_lev, lev_mode)

    # For same-type crossover, filter out invalid combos
    if ind1_name != "price" and ind1_name == ind2_name and ind1_period is not None:
        results = [r for r in results if r["ind2_period"] > ind1_period]
        results.sort(key=lambda r: r["total_return"], reverse=True)

    print()
    print_results_table(results)

    if results:
        best = results[0]
        print(f"\nBest strategy: {best['label']} \u2014 "
              f"Return: {best['total_return']:.2f}%, "
              f"Sharpe: {best['sharpe']:.2f}, "
              f"Max DD: {best['max_drawdown']:.2f}%")
        generate_chart(df, best, args.chart_file, args.asset.capitalize())


if __name__ == "__main__":
    main()
