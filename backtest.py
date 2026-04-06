#!/usr/bin/env python3
"""Backtesting Engine — CLI app to backtest indicator crossover trading strategies."""

import argparse
from datetime import datetime
import os
import numpy as np
import pandas as pd
import sys

from helpers import compute_ratio_prices

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def resample_to_weekly(df):
    """Resample daily OHLCV data to weekly (Monday start). Takes last close per week."""
    weekly = df["close"].resample("W-MON", label="left", closed="left").last().dropna()
    return pd.DataFrame({"close": weekly}, index=weekly.index)


def load_data(path):
    """Read CSV with unix timestamp + close price, return DataFrame with datetime index."""
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("date").sort_index()
    df = df[["close"]].copy()
    return df


def load_asset(name, data_dir=None, use_db=None):
    """Load an asset by name — from PostgreSQL if available, else from CSV.

    Args:
        name: Asset name (e.g., "bitcoin").
        data_dir: Directory containing CSV files (default: data/ next to this file).
        use_db: Force DB mode (True), CSV mode (False), or auto-detect (None).
                Auto-detect checks for PRICE_DB_URL env var.
    Returns:
        DataFrame with DatetimeIndex(UTC) and 'close' column.
    """
    if use_db is None:
        use_db = bool(os.environ.get("PRICE_DB_URL"))

    if use_db:
        import price_db
        df = price_db.get_asset_df(name)
        if df.empty:
            raise FileNotFoundError(f"Asset '{name}' not found in database")
        return df

    if data_dir is None:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    path = os.path.join(data_dir, f"{name}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV not found: {path}")
    return load_data(path)


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
                            reverse=False, sizing="compound", start_date=None,
                            periods_per_year=365, financing_rate=0):
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
    # Force cash when oscillator primary is NaN (warmup period — no valid signal)
    nan_mask = osc_data["primary"].isna()
    df.loc[nan_mask, "position"] = 0

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

    # Determine financing parameters
    apply_fin = _should_apply_financing(financing_rate, exposure, long_leverage, short_leverage, sizing)
    financing_cost_long = 0.0
    financing_cost_short = 0.0
    if apply_fin:
        fin_daily_long = _financing_daily_rate(long_leverage, financing_rate, periods_per_year)
        fin_daily_short = _financing_daily_rate(short_leverage, financing_rate, periods_per_year)
    else:
        fin_daily_long = fin_daily_short = 0.0

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
        equity_arr, liquidated, financing_cost_long, financing_cost_short = _compute_equity_set_and_forget(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee, fin_daily_long, fin_daily_short)
        df["equity"] = equity_arr
    elif lev_mode == "optimal":
        equity_arr, liquidated, financing_cost_long, financing_cost_short = _compute_equity_optimal(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee, fin_daily_long, fin_daily_short)
        df["equity"] = equity_arr
    else:
        leverage = np.where(df["position"] > 0, long_leverage,
                   np.where(df["position"] < 0, short_leverage, 1))
        df["strategy_return"] = df["position"] * daily_return * leverage
        trade_mask = df["position"].diff().fillna(0).abs() > 0
        df.loc[trade_mask, "strategy_return"] -= fee
        if apply_fin:
            fin_rate = _financing_daily_rate(leverage, financing_rate, periods_per_year)
            df["strategy_return"] -= df["position"] * fin_rate
        equity_arr, liquidated = _compute_equity_with_liquidation(df["strategy_return"].values, initial_cash)
        df["equity"] = equity_arr
        if apply_fin:
            eq_shifted = pd.Series(equity_arr, index=df.index).shift(1).fillna(initial_cash)
            pos_fin = df["position"] * fin_rate * eq_shifted
            financing_cost_long = pos_fin[df["position"] > 0].sum()
            financing_cost_short = (-pos_fin[df["position"] < 0]).sum()

    df["buyhold"] = initial_cash * (1 + daily_return).cumprod()

    trade_mask = df["position"].diff().fillna(0).abs() > 0
    trades = trade_mask.sum()

    total_return = (df["equity"].iloc[-1] / initial_cash - 1) * 100
    buyhold_return = (df["buyhold"].iloc[-1] / initial_cash - 1) * 100
    max_drawdown_val = _max_drawdown(df["equity"])

    equity_returns = pd.Series(df["equity"].values).pct_change().fillna(0)
    mean_daily = equity_returns.mean()
    std_daily = equity_returns.std()
    _ppy = periods_per_year
    sharpe = (mean_daily / std_daily * np.sqrt(_ppy)) if std_daily > 0 else 0.0

    volatility = std_daily * np.sqrt(_ppy) * 100
    sortino = _sortino_ratio(equity_returns, _ppy)
    beta_val = _beta(equity_returns.values, daily_return.values)
    n_days = len(df)
    ann_ret = _annualized_return(total_return, n_days, _ppy)
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
        "total_financing_cost": financing_cost_long + financing_cost_short,
        "financing_cost_long": financing_cost_long,
        "financing_cost_short": financing_cost_short,
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


def _should_apply_financing(financing_rate, exposure, long_leverage, short_leverage, sizing):
    """Determine if financing fees should be applied."""
    if financing_rate <= 0 or sizing == "fixed":
        return False
    if exposure == "long-cash" and long_leverage == 1:
        return False
    if exposure == "short-cash" and short_leverage == 1:
        return False
    return True


def _financing_daily_rate(leverage, financing_rate, periods_per_year):
    """Compute daily financing rate per unit of position.
    Crypto (>=365 periods): full notional (leverage * rate / 365).
    Tradfi (<365 periods): borrowed portion (max(leverage-1, 0) * rate / periods_per_year).
    """
    if periods_per_year >= 365:
        return leverage * financing_rate / 365
    else:
        return np.maximum(leverage - 1, 0) * financing_rate / periods_per_year


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


def _compute_equity_set_and_forget(positions, daily_returns, initial_cash, long_leverage, short_leverage, fee,
                                   financing_daily_long=0, financing_daily_short=0):
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
    financing_long = 0.0
    financing_short = 0.0

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
                # Daily financing cost (long pays)
                fin_cost = current_equity * financing_daily_long
                current_equity -= fin_cost
                entry_equity -= fin_cost  # adjust entry so future cum_return calc stays correct
                financing_long += fin_cost
            else:
                lev = short_leverage
                current_equity = entry_equity * (1 + lev * (1 - cum_return))
                # Daily financing income (short earns)
                fin_cost = current_equity * financing_daily_short
                current_equity += fin_cost
                entry_equity += fin_cost
                financing_short += fin_cost

        if current_equity <= 0:
            current_equity = 0.0
            liquidated = True

        equity[i] = current_equity

    return equity, liquidated, financing_long, financing_short


def _compute_equity_optimal(positions, daily_returns, initial_cash, long_leverage, short_leverage, fee,
                            financing_daily_long=0, financing_daily_short=0):
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
    financing_long = 0.0
    financing_short = 0.0

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
            fin_cost = current_equity * financing_daily_long
            current_equity -= fin_cost
            financing_long += fin_cost
        elif current_pos < 0:
            # Short: set-and-forget
            cum_return *= (1 + dr)
            current_equity = entry_equity * (1 + short_leverage * (1 - cum_return))
            fin_cost = current_equity * financing_daily_short
            current_equity += fin_cost
            entry_equity += fin_cost
            financing_short += fin_cost

        if current_equity <= 0:
            current_equity = 0.0
            liquidated = True

        equity[i] = current_equity

    return equity, liquidated, financing_long, financing_short


def _max_drawdown(equity_series):
    """Compute max drawdown as a percentage."""
    cummax = equity_series.cummax()
    drawdown = (equity_series - cummax) / cummax.replace(0, np.nan)
    return drawdown.min() * 100 if not drawdown.isna().all() else -100.0


def _annualized_return(total_return_pct, n_periods, periods_per_year=365):
    """Convert total return % over n_periods into annualized return %.
    periods_per_year: 365 for daily data, 52 for weekly data."""
    growth = 1 + total_return_pct / 100
    if growth <= 0 or n_periods <= 0:
        return -100.0
    return (growth ** (periods_per_year / n_periods) - 1) * 100


def _sortino_ratio(returns, periods_per_year=365):
    """Sortino ratio: mean / downside deviation, annualized."""
    mean_d = returns.mean()
    downside = returns[returns < 0]
    down_std = downside.std() if len(downside) > 1 else 0.0
    return (mean_d / down_std * np.sqrt(periods_per_year)) if down_std > 0 else 0.0


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
                 reverse=False, sizing="compound", start_date=None,
                 periods_per_year=365, financing_rate=0):
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
    # Force cash when either indicator is NaN (warmup period — no valid signal)
    nan_mask = ind1_series.isna() | ind2_series.isna()
    df.loc[nan_mask, "position"] = 0

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

    # Determine financing parameters
    apply_fin = _should_apply_financing(financing_rate, exposure, long_leverage, short_leverage, sizing)
    financing_cost_long = 0.0
    financing_cost_short = 0.0
    if apply_fin:
        fin_daily_long = _financing_daily_rate(long_leverage, financing_rate, periods_per_year)
        fin_daily_short = _financing_daily_rate(short_leverage, financing_rate, periods_per_year)
    else:
        fin_daily_long = fin_daily_short = 0.0

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
        equity_arr, liquidated, financing_cost_long, financing_cost_short = _compute_equity_set_and_forget(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee, fin_daily_long, fin_daily_short)
        df["equity"] = equity_arr
    elif lev_mode == "optimal":
        equity_arr, liquidated, financing_cost_long, financing_cost_short = _compute_equity_optimal(
            df["position"].values, daily_return.values, initial_cash,
            long_leverage, short_leverage, fee, fin_daily_long, fin_daily_short)
        df["equity"] = equity_arr
    else:
        leverage = np.where(df["position"] > 0, long_leverage,
                   np.where(df["position"] < 0, short_leverage, 1))
        df["strategy_return"] = df["position"] * daily_return * leverage
        trade_mask = df["position"].diff().fillna(0).abs() > 0
        df.loc[trade_mask, "strategy_return"] -= fee
        if apply_fin:
            fin_rate = _financing_daily_rate(leverage, financing_rate, periods_per_year)
            df["strategy_return"] -= df["position"] * fin_rate
        equity_arr, liquidated = _compute_equity_with_liquidation(df["strategy_return"].values, initial_cash)
        df["equity"] = equity_arr
        if apply_fin:
            eq_shifted = pd.Series(equity_arr, index=df.index).shift(1).fillna(initial_cash)
            pos_fin = df["position"] * fin_rate * eq_shifted
            financing_cost_long = pos_fin[df["position"] > 0].sum()
            financing_cost_short = (-pos_fin[df["position"] < 0]).sum()

    df["buyhold"] = initial_cash * (1 + daily_return).cumprod()

    trade_mask = df["position"].diff().fillna(0).abs() > 0
    trades = trade_mask.sum()

    total_return = (df["equity"].iloc[-1] / initial_cash - 1) * 100
    buyhold_return = (df["buyhold"].iloc[-1] / initial_cash - 1) * 100
    max_drawdown = _max_drawdown(df["equity"])

    equity_returns = pd.Series(df["equity"].values).pct_change().fillna(0)
    mean_daily = equity_returns.mean()
    std_daily = equity_returns.std()
    _ppy = periods_per_year
    sharpe = (mean_daily / std_daily * np.sqrt(_ppy)) if std_daily > 0 else 0.0

    # Additional metrics
    volatility = std_daily * np.sqrt(_ppy) * 100
    sortino = _sortino_ratio(equity_returns, _ppy)
    beta_val = _beta(equity_returns.values, daily_return.values)
    n_days = len(df)
    ann_ret = _annualized_return(total_return, n_days, _ppy)
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
        "total_financing_cost": financing_cost_long + financing_cost_short,
        "financing_cost_long": financing_cost_long,
        "financing_cost_short": financing_cost_short,
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
                  sizing="compound", start_date=None, periods_per_year=365,
                  financing_rate=0):
    """Sweep one indicator's period across a range. sweep_target: 'ind1' or 'ind2'."""
    results = []
    for period in range(sweep_min, sweep_max + 1):
        p1 = period if sweep_target == "ind1" else ind1_period
        p2 = period if sweep_target == "ind2" else ind2_period
        r = run_strategy(df, ind1_name, p1, ind2_name, p2,
                         initial_cash, fee, exposure, long_leverage, short_leverage, lev_mode,
                         sizing=sizing, start_date=start_date, periods_per_year=periods_per_year,
                         financing_rate=financing_rate)
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


# --- Rolling Window Analysis ---

def generate_rolling_windows(df, window_years, step_years, periods_per_year=365):
    """Generate (start, end) date pairs for rolling windows across the dataset.
    Raises ValueError if dataset is shorter than one window."""
    data_start = df.index.min()
    data_end = df.index.max()
    window_offset = pd.DateOffset(years=window_years)
    step_offset = pd.DateOffset(years=step_years)

    # Check dataset can fit at least one window
    if data_start + window_offset > data_end:
        data_span = (data_end - data_start).days / 365.25
        raise ValueError(
            f"Dataset spans {data_span:.1f} years but window requires {window_years} years. "
            f"Need at least one full window.")

    windows = []
    start = data_start
    expected_days = window_years * periods_per_year
    while start + window_offset <= data_end:
        end = start + window_offset
        # Discard partial windows (<80% of expected data)
        actual_days = len(df[(df.index >= start) & (df.index < end)])
        if actual_days >= expected_days * 0.8:
            if window_years == 1:
                label = str(start.year)
            else:
                label = f"{start.year}-{end.year}"
            windows.append({"start": start, "end": end, "label": label})
        start = start + step_offset
    if not windows:
        raise ValueError("No valid windows could be generated from this dataset.")
    return windows


def rolling_window_evaluate(df, windows, ind1_name, ind1_period, ind2_name, ind2_period,
                            initial_cash, fee=0.001, exposure="long-cash",
                            long_leverage=1, short_leverage=1, lev_mode="rebalance",
                            reverse=False, sizing="compound", periods_per_year=365,
                            financing_rate=0):
    """Run a fixed strategy across all windows. Indicators computed once on full df."""
    df = df.copy()
    ind1_series, ind1_label = compute_indicator_from_spec(df, ind1_name, ind1_period)
    ind2_series, ind2_label = compute_indicator_from_spec(df, ind2_name, ind2_period)

    above = ind1_series > ind2_series
    if reverse:
        above = ~above
    position = _apply_exposure(above, exposure).shift(1).fillna(0)
    nan_mask = ind1_series.isna() | ind2_series.isna()
    position[nan_mask] = 0
    daily_return = df["close"].pct_change().fillna(0)

    results = []
    for w in windows:
        mask = (df.index >= w["start"]) & (df.index < w["end"])
        w_pos = position[mask]
        w_ret = daily_return[mask]
        if len(w_pos) < 2:
            continue

        # Compute equity for this window
        leverage = np.where(w_pos > 0, long_leverage,
                   np.where(w_pos < 0, short_leverage, 1))
        strat_ret = w_pos * w_ret * leverage
        trade_mask_arr = np.abs(np.diff(w_pos.values, prepend=0)) > 0
        strat_ret_arr = strat_ret.values.copy()
        strat_ret_arr[trade_mask_arr] -= fee

        equity_arr, _ = _compute_equity_with_liquidation(strat_ret_arr, initial_cash)
        equity = pd.Series(equity_arr, index=w_pos.index)

        # Buy-and-hold for this window
        bh = initial_cash * (1 + w_ret).cumprod()

        n_days = len(w_pos)
        total_return = (equity.iloc[-1] / initial_cash - 1) * 100
        buyhold_return = (bh.iloc[-1] / initial_cash - 1) * 100
        alpha = total_return - buyhold_return
        annualized = _annualized_return(total_return, n_days, periods_per_year)

        eq_returns = pd.Series(equity_arr).pct_change().fillna(0)
        std_d = eq_returns.std()
        sharpe = (eq_returns.mean() / std_d * np.sqrt(periods_per_year)) if std_d > 0 else 0.0
        max_dd = _max_drawdown(equity)

        results.append({
            "window": w,
            "total_return": total_return,
            "buyhold_return": buyhold_return,
            "alpha": alpha,
            "annualized": annualized,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "equity": equity,
        })
    return results


def compute_consistency_score(window_results, metric="total_return"):
    """Compute a 0-100 consistency score based on the given metric.
    Returns (score, label) where label is Excellent/Good/Fair/Poor."""
    if not window_results:
        return 0.0, "Poor"

    values = [r[metric] for r in window_results]
    n = len(values)

    # Threshold for "positive"
    threshold = 0.5 if metric == "sharpe" else 0.0
    positive_count = sum(1 for v in values if v > threshold)
    positive_ratio = positive_count / n

    # Coefficient of variation penalty
    mean_val = np.mean(values)
    std_val = np.std(values)
    cv = (std_val / abs(mean_val)) if abs(mean_val) > 1e-9 else 1.0
    cv = min(cv, 1.0)

    score = positive_ratio * 100 * (1 - 0.3 * cv)
    score = max(0.0, min(100.0, score))

    if score >= 80:
        label = "Excellent"
    elif score >= 60:
        label = "Good"
    elif score >= 40:
        label = "Fair"
    else:
        label = "Poor"
    return score, label


def rolling_window_sweep(df, windows, ind1_name, ind1_period, ind2_name,
                         sweep_target, sweep_min, sweep_max, sweep_step,
                         initial_cash, fee=0.001, exposure="long-cash",
                         long_leverage=1, short_leverage=1, lev_mode="rebalance",
                         reverse=False, sizing="compound", periods_per_year=365,
                         financing_rate=0, metric="total_return"):
    """Sweep one indicator's period across all windows.
    Returns dict with windows, periods, matrix (n_windows x n_periods), best_per_window."""
    periods = list(range(sweep_min, sweep_max + 1, sweep_step))
    df = df.copy()
    daily_return = df["close"].pct_change().fillna(0)

    # Pre-compute all indicator series and position arrays
    position_cache = {}
    for period in periods:
        p1 = period if sweep_target == "ind1" else ind1_period
        p2 = period if sweep_target == "ind2" else ind2_period
        i1_name = ind1_name
        i2_name = ind2_name
        s1, _ = compute_indicator_from_spec(df, i1_name, p1)
        s2, _ = compute_indicator_from_spec(df, i2_name, p2)
        above = s1 > s2
        if reverse:
            above = ~above
        pos = _apply_exposure(above, exposure).shift(1).fillna(0)
        nan_mask = s1.isna() | s2.isna()
        pos[nan_mask] = 0
        position_cache[period] = pos

    n_windows = len(windows)
    n_periods = len(periods)
    matrix = np.full((n_windows, n_periods), np.nan)
    best_per_window = []

    for i, w in enumerate(windows):
        mask = (df.index >= w["start"]) & (df.index < w["end"])
        w_ret = daily_return[mask]
        if len(w_ret) < 2:
            best_per_window.append((None, None))
            continue
        n_days = len(w_ret)
        best_val = -np.inf
        best_p = periods[0]

        for j, period in enumerate(periods):
            w_pos = position_cache[period][mask]
            leverage = np.where(w_pos > 0, long_leverage,
                       np.where(w_pos < 0, short_leverage, 1))
            strat_ret = w_pos * w_ret * leverage
            trade_mask_arr = np.abs(np.diff(w_pos.values, prepend=0)) > 0
            strat_ret_arr = strat_ret.values.copy()
            strat_ret_arr[trade_mask_arr] -= fee
            equity_arr, _ = _compute_equity_with_liquidation(strat_ret_arr, initial_cash)

            total_return = (equity_arr[-1] / initial_cash - 1) * 100

            if metric == "total_return":
                val = total_return
            elif metric == "alpha":
                bh_return = ((1 + w_ret).cumprod().iloc[-1] - 1) * 100
                val = total_return - bh_return
            elif metric == "sharpe":
                eq_rets = np.diff(equity_arr, prepend=initial_cash) / np.maximum(np.abs(np.concatenate([[initial_cash], equity_arr[:-1]])), 1e-10)
                std_d = np.std(eq_rets)
                val = (np.mean(eq_rets) / std_d * np.sqrt(periods_per_year)) if std_d > 0 else 0.0
            elif metric == "annualized":
                val = _annualized_return(total_return, n_days, periods_per_year)
            else:
                val = total_return

            matrix[i, j] = val
            if val > best_val:
                best_val = val
                best_p = period

        best_per_window.append((best_p, best_val))

    return {
        "windows": [w["label"] for w in windows],
        "periods": periods,
        "matrix": matrix,
        "best_per_window": best_per_window,
    }


# --- DCA Functions ---

DCA_SIGNAL_TYPES = {
    "oscillator": "Oscillator (RSI, Stochastic, etc.)",
    "ma_distance": "Distance from Moving Average",
    "ath_drawdown": "Drawdown from All-Time High",
}


def compute_dca_signal(df, signal_type, signal_name=None, signal_period=None):
    """Compute a 0-1 signal series for DCA multiplier scaling.

    signal_type: 'oscillator', 'ma_distance', or 'ath_drawdown'
    Returns: (signal_series, label) where signal_series is 0..1
             (0 = cheapest/most oversold, 1 = most expensive/overbought)
    """
    close = df["close"]

    if signal_type == "oscillator":
        name = signal_name or "rsi"
        osc_data = compute_oscillator(df, name, signal_period)
        primary = osc_data["primary"]
        spec = osc_data["spec"]
        osc_range = spec.get("range")
        if osc_range:
            lo, hi = osc_range
            signal = (primary - lo) / (hi - lo)
        else:
            # Unbounded oscillators: use rolling percentile rank
            signal = primary.rolling(252, min_periods=20).apply(
                lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
            )
        label = osc_data["label"]

    elif signal_type == "ma_distance":
        name = signal_name or "sma"
        period = signal_period or 200
        ma_series, _ = compute_indicator_from_spec(df, name, period)
        pct_distance = (close - ma_series) / ma_series
        # Normalize using rolling min/max to get 0-1
        roll_min = pct_distance.rolling(504, min_periods=50).min()
        roll_max = pct_distance.rolling(504, min_periods=50).max()
        denom = (roll_max - roll_min).replace(0, np.nan)
        signal = (pct_distance - roll_min) / denom
        label = f"Distance from {name.upper()}({period})"

    elif signal_type == "ath_drawdown":
        lookback_years = signal_period or 5
        lookback_days = int(lookback_years * 365)
        ath = close.cummax()
        drawdown = (close - ath) / ath  # 0 at ATH, negative below
        # signal=1 at ATH (expensive, buy less), signal=0 at worst drawdown (cheap, buy more)
        roll_min_dd = drawdown.rolling(lookback_days, min_periods=50).min()
        denom = roll_min_dd.replace(0, np.nan)
        signal = 1.0 - (drawdown / denom)  # 1 at ATH, 0 at worst drawdown
        label = f"ATH Drawdown ({lookback_years}y)"

    else:
        raise ValueError(f"Unknown DCA signal type: {signal_type}")

    signal = signal.clip(0, 1)
    return signal, label


def compute_dca_multipliers(signal_series, max_multiplier=3.0):
    """Convert 0-1 signal into DCA multipliers.

    signal=0 → max_multiplier (cheapest → buy more)
    signal=1 → 1/max_multiplier (expensive → buy less)
    Linear interpolation between.

    Multipliers are then normalized so the total spend matches constant DCA.
    """
    # signal=0 → max_mult, signal=1 → 1/max_mult
    low = 1.0 / max_multiplier
    high = max_multiplier
    raw_mult = high + (low - high) * signal_series  # linear from high to low
    return raw_mult


def run_dca_compare(df, frequency="daily", amount=100.0, signal_type="oscillator",
                    signal_name=None, signal_period=None, max_multiplier=3.0,
                    fee=0.001, start_date=None, show_lump_sum=True,
                    reverse=False, periods_per_year=365):
    """Compare constant DCA vs dynamic (signal-adjusted) DCA.

    frequency: 'daily', 'weekly', or 'monthly'
    Returns dict with equity series and metrics for both strategies + optional lump sum.
    """
    df = df.copy()

    if start_date is not None:
        ts = pd.Timestamp(start_date)
        if df.index.tz is not None:
            ts = ts.tz_localize(df.index.tz)
        df = df[df.index >= ts]

    close = df["close"]
    n = len(close)
    if n < 2:
        return None

    # Build buy mask based on frequency
    if frequency == "daily":
        buy_mask = np.ones(n, dtype=bool)
    elif frequency == "weekly":
        # Buy on Mondays (or first day of each week)
        buy_mask = np.zeros(n, dtype=bool)
        last_week = None
        for i, dt in enumerate(df.index):
            wk = dt.isocalendar()[1]
            yr = dt.year
            key = (yr, wk)
            if key != last_week:
                buy_mask[i] = True
                last_week = key
        buy_mask = np.array(buy_mask)
    elif frequency == "monthly":
        buy_mask = np.zeros(n, dtype=bool)
        last_month = None
        for i, dt in enumerate(df.index):
            key = (dt.year, dt.month)
            if key != last_month:
                buy_mask[i] = True
                last_month = key
        buy_mask = np.array(buy_mask)
    else:
        buy_mask = np.ones(n, dtype=bool)

    n_buys = buy_mask.sum()
    total_budget = amount * n_buys

    # --- Constant DCA ---
    const_units = np.zeros(n)
    const_spent = np.zeros(n)
    prices = close.values
    for i in range(n):
        if buy_mask[i]:
            cost = amount * (1 + fee)
            const_units[i] = amount / prices[i]
            const_spent[i] = cost

    const_cum_units = np.cumsum(const_units)
    const_cum_spent = np.cumsum(const_spent)
    const_equity = const_cum_units * prices

    # --- Dynamic DCA ---
    signal_series, signal_label = compute_dca_signal(df, signal_type, signal_name, signal_period)
    if reverse:
        signal_series = 1.0 - signal_series
        signal_label = f"{signal_label} (reversed)"
    raw_mult = compute_dca_multipliers(signal_series, max_multiplier)

    # Normalize multipliers so total spend matches constant DCA total
    buy_mults = raw_mult.values[buy_mask]
    buy_mults_clean = np.where(np.isnan(buy_mults), 1.0, buy_mults)
    mult_sum = buy_mults_clean.sum()
    if mult_sum > 0:
        scale_factor = n_buys / mult_sum
    else:
        scale_factor = 1.0

    dyn_units = np.zeros(n)
    dyn_spent = np.zeros(n)
    buy_idx = 0
    for i in range(n):
        if buy_mask[i]:
            m = buy_mults_clean[buy_idx] * scale_factor
            dyn_amount = amount * m
            cost = dyn_amount * (1 + fee)
            dyn_units[i] = dyn_amount / prices[i]
            dyn_spent[i] = cost
            buy_idx += 1

    dyn_cum_units = np.cumsum(dyn_units)
    dyn_cum_spent = np.cumsum(dyn_spent)
    dyn_equity = dyn_cum_units * prices

    # Metrics helper
    def _dca_metrics(equity_arr, cum_spent_arr, cum_units_arr, per_purchase_spent, label):
        eq = pd.Series(equity_arr, index=df.index)
        cum_invested = pd.Series(cum_spent_arr, index=df.index)
        cum_units = pd.Series(cum_units_arr, index=df.index)
        spent = cum_spent_arr[-1]
        final_val = equity_arr[-1]
        total_ret = (final_val / spent - 1) * 100 if spent > 0 else 0
        n_periods = len(equity_arr)
        ann_ret = _annualized_return(total_ret, n_periods, periods_per_year)
        # Max drawdown of portfolio value
        max_dd = _max_drawdown(eq.replace(0, np.nan).dropna()) if eq.max() > 0 else 0
        dd_duration = _max_drawdown_duration(eq.replace(0, np.nan).dropna()) if eq.max() > 0 else 0
        # Sharpe from period-to-period returns of equity
        eq_clean = eq.replace(0, np.nan).dropna()
        if len(eq_clean) > 2:
            rets = eq_clean.pct_change().dropna()
            mean_r = rets.mean()
            std_r = rets.std()
            sharpe = (mean_r / std_r * np.sqrt(periods_per_year)) if std_r > 0 else 0
            sortino = _sortino_ratio(rets, periods_per_year)
            volatility = std_r * np.sqrt(periods_per_year) * 100
        else:
            sharpe = sortino = volatility = 0

        total_units = equity_arr[-1] / prices[-1] if prices[-1] > 0 else 0
        avg_cost_per_unit = spent / total_units if total_units > 0 else 0

        # Per-purchase stats (from non-zero entries)
        buy_amounts = per_purchase_spent[per_purchase_spent > 0]
        n_purchases = len(buy_amounts)
        if n_purchases > 0:
            avg_buy = float(np.mean(buy_amounts))
            min_buy = float(np.min(buy_amounts))
            max_buy = float(np.max(buy_amounts))
            median_buy = float(np.median(buy_amounts))
        else:
            avg_buy = min_buy = max_buy = median_buy = 0.0

        return {
            "label": label,
            "equity": eq,
            "cum_invested": cum_invested,
            "cum_units": cum_units,
            "total_invested": spent,
            "final_value": final_val,
            "total_return": total_ret,
            "annualized": ann_ret,
            "max_drawdown": max_dd,
            "max_dd_duration": dd_duration,
            "sharpe": sharpe,
            "sortino": sortino,
            "volatility": volatility,
            "total_units": total_units,
            "avg_cost_per_unit": avg_cost_per_unit,
            "n_purchases": n_purchases,
            "avg_buy_amount": avg_buy,
            "min_buy_amount": min_buy,
            "max_buy_amount": max_buy,
            "median_buy_amount": median_buy,
        }

    const_metrics = _dca_metrics(const_equity, const_cum_spent, const_cum_units, const_spent, f"Constant DCA (${amount:.0f}/{frequency})")
    dyn_label = f"Dynamic DCA ({signal_label}, {max_multiplier:.1f}x)"
    dyn_metrics = _dca_metrics(dyn_equity, dyn_cum_spent, dyn_cum_units, dyn_spent, dyn_label)

    result = {
        "constant": const_metrics,
        "dynamic": dyn_metrics,
        "signal_series": signal_series,
        "signal_label": signal_label,
        "frequency": frequency,
        "amount": amount,
        "max_multiplier": max_multiplier,
        "n_buys": int(n_buys),
        "total_budget": total_budget,
        "buy_mask": pd.Series(buy_mask, index=df.index),
        "prices": close,
    }

    if show_lump_sum:
        # Lump sum: invest entire budget on day 1
        lump_units = total_budget / (prices[0] * (1 + fee))
        lump_equity = lump_units * prices
        lump_spent_arr = np.zeros(n)
        lump_spent_arr[0] = total_budget * (1 + fee)
        lump_cum_units = np.full(n, lump_units)
        lump_metrics = _dca_metrics(lump_equity, lump_spent_arr.cumsum(), lump_cum_units, lump_spent_arr,
                                     f"Lump Sum (${total_budget:,.0f})")
        # Override spent for lump sum
        lump_metrics["total_invested"] = total_budget * (1 + fee)
        result["lump_sum"] = lump_metrics

    return result


def run_dca_sweep(df, sweep_param="multiplier", frequency="daily", amount=100.0,
                  signal_type="oscillator", signal_name=None, signal_period=None,
                  max_multiplier=3.0, fee=0.001, start_date=None,
                  sweep_min=None, sweep_max=None, sweep_step=None,
                  show_lump_sum=True, periods_per_year=365):
    """Sweep one DCA parameter and return results for each value.

    sweep_param: 'multiplier' or 'period'
    Returns list of result dicts with final_value, annualized, etc.
    """
    results = []

    if sweep_param == "multiplier":
        s_min = sweep_min or 1.0
        s_max = sweep_max or 10.0
        s_step = sweep_step or 0.5
        values = np.arange(s_min, s_max + s_step / 2, s_step)
        for val in values:
            r = run_dca_compare(df, frequency, amount, signal_type, signal_name,
                                signal_period, val, fee, start_date, show_lump_sum=False,
                                periods_per_year=periods_per_year)
            if r is None:
                continue
            results.append({
                "param_value": float(val),
                "param_label": f"{val:.1f}x",
                "dynamic": r["dynamic"],
                "constant": r["constant"],
            })
    elif sweep_param == "period":
        s_min = int(sweep_min or 5)
        s_max = int(sweep_max or 200)
        s_step = int(sweep_step or 5)
        for val in range(s_min, s_max + 1, s_step):
            r = run_dca_compare(df, frequency, amount, signal_type, signal_name,
                                val, max_multiplier, fee, start_date, show_lump_sum=False,
                                periods_per_year=periods_per_year)
            if r is None:
                continue
            results.append({
                "param_value": val,
                "param_label": str(val),
                "dynamic": r["dynamic"],
                "constant": r["constant"],
            })

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

    # Forward log return: ln(close[i+forward_days] / close[i]) * 100 (as percentage)
    # Log returns have better statistical properties (additive, more normal distribution)
    price_ratio = df["close"].shift(-forward_days) / df["close"]
    log_forward_return = np.log(price_ratio) * 100

    # Combine and drop NaN
    combined = pd.DataFrame({
        "osc": primary,
        "fwd_log_return": log_forward_return,
    }).dropna()

    osc_values = combined["osc"].values
    log_returns = combined["fwd_log_return"].values
    n_points = len(combined)

    # Linear regression on log returns
    if n_points >= 3:
        slope, intercept, r_value, p_value, std_err = stats.linregress(osc_values, log_returns)
        spearman_r, spearman_p = stats.spearmanr(osc_values, log_returns)
    else:
        slope = intercept = r_value = p_value = std_err = 0
        spearman_r = spearman_p = 0

    r_squared = r_value ** 2

    # Convert log returns back to simple returns: (exp(log_ret/100) - 1) * 100
    def _log_to_simple(log_pct):
        return (np.exp(log_pct / 100) - 1) * 100

    # Simple returns for display (zone stats, chart y-axis)
    forward_returns = _log_to_simple(log_returns)

    # Convert regression intercept to simple return for display
    intercept_simple = float(_log_to_simple(intercept))

    # Zone analysis (using simple returns for display)
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
        "log_returns": log_returns,
        "forward_days": forward_days,
        "r_squared": r_squared,
        "pearson_r": r_value,
        "spearman_r": spearman_r,
        "spearman_p": spearman_p,
        "p_value": p_value,
        "slope": slope,
        "intercept": intercept,
        "intercept_simple": intercept_simple,
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
        forward_return = np.log(df["close"].shift(-fwd) / df["close"]) * 100
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


def generate_regression_sweep_chart(sweep_result, theme="dark"):
    """Generate line chart of R² vs forward days. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from io import BytesIO
    import base64

    t = _get_theme(theme)
    days = sweep_result["days"]
    r_sq = sweep_result["r_squared"]
    spearman = sweep_result["spearman"]

    fig, ax = plt.subplots(figsize=(14, 5), dpi=150)
    _apply_dark_theme(fig, ax, theme)

    ax.plot(days, r_sq, color=t["blue"], linewidth=1.5, label="R²")
    ax.scatter([sweep_result["best_days"]], [sweep_result["best_r_squared"]],
               color=t["accent"], s=60, zorder=5,
               label=f"Best R²: {sweep_result['best_days']}d ({sweep_result['best_r_squared']:.4f})")

    ax.set_xlabel("Forward Days")
    ax.set_ylabel("R²")
    ax.set_title(f"{sweep_result['osc_label']} — Predictive Power by Forward Horizon")
    ax.legend(loc="best", fontsize=9, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["text"])
    ax.grid(True, alpha=0.3, color=t["grid"])
    ax.set_xlim(days[0], days[-1])
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def generate_regression_chart(result, theme="dark"):
    """Generate scatter plot of oscillator values vs forward returns. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = _get_theme(theme)
    osc_values = result["osc_values"]
    log_returns = result["log_returns"]
    buy_thr = result["buy_threshold"]
    sell_thr = result["sell_threshold"]

    fig, ax = plt.subplots(figsize=(14, 9), dpi=150)
    _apply_dark_theme(fig, ax, theme)

    # Color points by zone
    oversold_mask = osc_values < buy_thr
    overbought_mask = osc_values > sell_thr
    neutral_mask = ~oversold_mask & ~overbought_mask

    ax.scatter(osc_values[neutral_mask], log_returns[neutral_mask],
               c=t["muted"], alpha=0.25, s=8, label="Neutral", rasterized=True)
    ax.scatter(osc_values[oversold_mask], log_returns[oversold_mask],
               c=t["green"], alpha=0.35, s=12, label="Oversold", rasterized=True)
    ax.scatter(osc_values[overbought_mask], log_returns[overbought_mask],
               c=t["red"], alpha=0.35, s=12, label="Overbought", rasterized=True)

    # Regression line (fitted in log space — straight line through log return data)
    x_range = np.linspace(osc_values.min(), osc_values.max(), 100)
    y_pred = result["slope"] * x_range + result["intercept"]
    ax.plot(x_range, y_pred, color=t["accent"], linewidth=2, label="Regression line")

    # Threshold lines
    ax.axvline(x=buy_thr, color=t["green"], linestyle="--", linewidth=1, alpha=0.7,
               label=f"Buy threshold ({buy_thr})")
    ax.axvline(x=sell_thr, color=t["red"], linestyle="--", linewidth=1, alpha=0.7,
               label=f"Sell threshold ({sell_thr})")

    # Zero return line
    ax.axhline(y=0, color=t["muted"], linestyle="--", linewidth=0.8, alpha=0.5)

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
            color=t["text"], bbox=dict(boxstyle="round,pad=0.5",
            facecolor=t["panel"], edgecolor=t["grid"], alpha=0.9))

    osc_label = result["osc_data"]["label"]
    ax.set_xlabel(f"{osc_label} Value")
    ax.set_ylabel(f"Forward {result['forward_days']}-Day Log Return (%)")
    ax.set_title(f"{osc_label} vs Forward {result['forward_days']}-Day Log Return — Regression Analysis")
    ax.legend(loc="upper right", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"], labelcolor=t["text"])
    ax.grid(True, alpha=0.3, color=t["grid"])
    plt.tight_layout()

    from io import BytesIO
    import base64
    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


CHART_THEMES = {
    "dark": {
        "bg": "#080a10",
        "panel": "#161922",
        "text": "#e8eaf0",
        "muted": "#8890a4",
        "grid": "#252a3a",
        "price": "#e8eaf0",
        "blue": "#6495ED",
        "accent": "#f7931a",
        "green": "#34d399",
        "red": "#ef4444",
        "branding_color": "#ffffff",
        "branding_alpha": 0.5,
    },
    "light": {
        "bg": "#ffffff",
        "panel": "#f0f2f5",
        "text": "#1a1a2e",
        "muted": "#5a6078",
        "grid": "#d0d4e0",
        "price": "#1a1a2e",
        "blue": "#3a6fd8",
        "accent": "#d97706",
        "green": "#059669",
        "red": "#dc2626",
        "branding_color": "#000000",
        "branding_alpha": 0.3,
    },
}

def _get_theme(theme="dark"):
    """Return a chart theme palette dict."""
    return CHART_THEMES.get(theme, CHART_THEMES["dark"])


def _apply_dark_theme(fig, axes, theme="dark"):
    """Apply a color palette to a matplotlib figure and axes."""
    t = _get_theme(theme)
    BG = t["bg"]
    PANEL = t["panel"]
    TEXT = t["text"]
    MUTED = t["muted"]
    GRID = t["grid"]

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
             color=t["branding_color"], alpha=t["branding_alpha"],
             ha="right", va="bottom", transform=fig.transFigure)


def generate_chart(df, best_result, output_path, asset_name="Bitcoin", theme="dark"):
    """Generate a two-panel PNG chart: price+indicators with markers, and equity curves."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    t = _get_theme(theme)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 10), dpi=150,
        gridspec_kw={"height_ratios": [7, 3]}, sharex=True
    )
    _apply_dark_theme(fig, [ax1, ax2], theme)

    # Top panel: price + indicators + buy/sell markers
    ax1.plot(df.index, df["close"], label=f"{asset_name} Price", color=t["price"], linewidth=0.8)

    # Plot ind2 (always — the main/slow indicator)
    ax1.plot(
        best_result["ind2_series"].index, best_result["ind2_series"],
        label=best_result["ind2_label"], color=t["blue"], linewidth=0.8, alpha=0.8
    )

    # Plot ind1 if not price (crossover strategy)
    if best_result.get("ind1_name") != "price":
        ax1.plot(
            best_result["ind1_series"].index, best_result["ind1_series"],
            label=best_result["ind1_label"], color=t["accent"], linewidth=0.8, alpha=0.8
        )

    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
    ax1.yaxis.set_minor_formatter(_minor_fmt())
    ax1.tick_params(axis='y', which='minor', labelsize=6)
    ax1.set_ylabel(f"{asset_name} Price (log scale)")
    ax1.set_title(f"{asset_name} Backtest — Best: {best_result['label']} "
                  f"({best_result['total_return']:.1f}% return)")
    ax1.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"],
               labelcolor=t["text"])
    ax1.grid(True, which="major", alpha=0.3, color=t["grid"])
    ax1.grid(True, which="minor", alpha=0.15, color=t["grid"])

    # Bottom panel: equity curve vs buy-and-hold
    ax2.plot(best_result["equity"].index, best_result["equity"],
             label="Strategy Equity", color=t["blue"], linewidth=1)
    ax2.plot(best_result["buyhold"].index, best_result["buyhold"],
             label="Buy & Hold", color=t["muted"], linewidth=1, alpha=0.7)
    ax2.set_yscale("log")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.2f}" if x < 1 else f"${x:,.0f}"))
    ax2.yaxis.set_minor_formatter(_minor_fmt())
    ax2.tick_params(axis='y', which='minor', labelsize=6)
    ax2.set_ylabel("Portfolio Value (log)")
    ax2.set_xlabel("Date")
    ax2.legend(loc="upper left", fontsize=8, facecolor=t["panel"], edgecolor=t["grid"],
               labelcolor=t["text"])
    ax2.grid(True, which="major", alpha=0.3, color=t["grid"])
    ax2.grid(True, which="minor", alpha=0.15, color=t["grid"])

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
                         long_leverage=1, short_leverage=1, lev_mode="rebalance", theme="dark"):
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

    t = _get_theme(theme)
    fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
    _apply_dark_theme(fig, ax, theme)

    ax.plot(periods, annualized_returns, color=t["blue"], linewidth=1)
    ax.axhline(y=bh_annualized, color=t["muted"], linestyle="--", linewidth=1,
               label=f"Buy & Hold ({bh_annualized:.1f}%)")
    ax.scatter([best_period], [best_ann], color=t["accent"], s=60, zorder=5,
               label=f"Best: {best_label} ({best_ann:.1f}%)")

    ax.set_xlabel(f"{ind2_upper} Period (days)")
    ax.set_ylabel("Annualized Return (%)")
    title_prefix = f"{ind1_label_str} vs " if ind1_name != "price" else ""
    ax.set_title(f"Annualized Return by {title_prefix}{ind2_upper} Period ({sweep_min}\u2013{sweep_max})")
    ax.legend(loc="best", fontsize=9, facecolor=t["panel"], edgecolor=t["grid"],
              labelcolor=t["text"])
    ax.grid(True, alpha=0.3, color=t["grid"])

    plt.tight_layout()
    plt.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close()
    print(f"Best: {best_label} with {best_ann:.2f}% annualized return")
    print(f"Buy & Hold: {bh_annualized:.2f}% annualized")
    print(f"Chart saved to {output_path}")


def generate_dual_sweep_heatmap(df, ind1_name, ind2_name,
                                 period_min, period_max, period_step,
                                 initial_cash, output_path, fee=0.001, exposure="long-cash",
                                 long_leverage=1, short_leverage=1, sizing="compound", theme="dark"):
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

    t = _get_theme(theme)
    fig, ax = plt.subplots(figsize=(14, 12), dpi=150)
    _apply_dark_theme(fig, ax, theme)

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
    cbar.set_label("Annualized Return (%)", color=t["muted"])
    cbar.ax.yaxis.set_tick_params(color=t["muted"])
    cbar.outline.set_edgecolor(t["grid"])
    for label in cbar.ax.get_yticklabels():
        label.set_color(t["muted"])

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


# --- Rolling Window Chart Generation ---

def generate_rolling_timeline_chart(window_results, metric, strategy_label, score, score_label, theme="dark"):
    """Horizontal bar chart: one bar per window, green/red by metric sign. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from io import BytesIO
    import base64

    t = _get_theme(theme)
    labels = [r["window"]["label"] for r in window_results]
    values = [r[metric] for r in window_results]
    colors = [t["green"] if v > (0.5 if metric == "sharpe" else 0) else t["red"] for v in values]

    metric_names = {"total_return": "Total Return %", "alpha": "Alpha vs Buy & Hold %", "sharpe": "Sharpe Ratio"}
    metric_label = metric_names.get(metric, metric)

    fig, ax = plt.subplots(figsize=(12, max(4, len(labels) * 0.6)), dpi=150)
    y_pos = range(len(labels))
    bars = ax.barh(y_pos, values, color=colors, height=0.6, edgecolor='none', alpha=0.85)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=10)
    ax.set_xlabel(metric_label, fontsize=11, color=t["muted"])
    ax.axvline(x=(0.5 if metric == "sharpe" else 0), color=t["muted"], linewidth=0.8, linestyle="--", alpha=0.5)
    ax.set_title(f"{strategy_label} — Rolling Window Consistency\n"
                 f"Score: {score:.0f}/100 ({score_label})  |  {metric_label}",
                 fontsize=13, color=t["text"], pad=12)

    # Value labels on bars
    for bar, val in zip(bars, values):
        x_pos = bar.get_width()
        ha = "left" if x_pos >= 0 else "right"
        offset = abs(max(values) - min(values)) * 0.02 if values else 1
        ax.text(x_pos + (offset if x_pos >= 0 else -offset), bar.get_y() + bar.get_height()/2,
                f"{val:.1f}", va="center", ha=ha, fontsize=9, color=t["text"])

    ax.invert_yaxis()
    ax.grid(axis="x", color=t["grid"], alpha=0.3)
    _apply_dark_theme(fig, ax, theme)
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def generate_rolling_equity_overlay(window_results, strategy_label, theme="dark"):
    """Overlay per-window equity curves normalized to 100. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from io import BytesIO
    import base64

    t = _get_theme(theme)
    n = len(window_results)
    cmap = plt.cm.viridis

    fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
    for i, r in enumerate(window_results):
        eq = r["equity"]
        normalized = eq / eq.iloc[0] * 100
        alpha = 0.3 + 0.7 * (i / max(n - 1, 1))
        lw = 1.0 + 1.5 * (i / max(n - 1, 1))
        color = cmap(i / max(n - 1, 1))
        ax.plot(normalized.index, normalized.values, color=color, alpha=alpha,
                linewidth=lw, label=r["window"]["label"])

    ax.axhline(y=100, color=t["muted"], linewidth=0.8, linestyle="--", alpha=0.4)
    ax.set_ylabel("Normalized Equity (start = 100)", fontsize=11, color=t["muted"])
    ax.set_title(f"{strategy_label} — Equity Curves Per Window", fontsize=13, color=t["text"], pad=12)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.7)
    ax.grid(color=t["grid"], alpha=0.3)
    _apply_dark_theme(fig, ax, theme)
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def generate_rolling_heatmap(sweep_data, metric, strategy_label, theme="dark"):
    """2D heatmap: rows=windows, columns=periods, color=metric. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from io import BytesIO
    import base64

    t = _get_theme(theme)
    matrix = sweep_data["matrix"]
    win_labels = sweep_data["windows"]
    periods = sweep_data["periods"]
    best_per_window = sweep_data["best_per_window"]
    n_win, n_per = matrix.shape

    metric_names = {"total_return": "Total Return %", "alpha": "Alpha %", "sharpe": "Sharpe Ratio"}
    metric_label = metric_names.get(metric, metric)

    fig, ax = plt.subplots(figsize=(max(10, n_per * 0.4), max(4, n_win * 0.7)), dpi=150)
    im = ax.imshow(matrix, aspect="auto", cmap="RdYlGn", interpolation="nearest")
    cbar = fig.colorbar(im, ax=ax, shrink=0.8, pad=0.02)
    cbar.set_label(metric_label, fontsize=10, color=t["muted"])
    cbar.ax.tick_params(colors=t["muted"])

    ax.set_xticks(range(n_per))
    ax.set_xticklabels(periods, fontsize=max(6, min(9, 200 // n_per)), rotation=45)
    ax.set_yticks(range(n_win))
    ax.set_yticklabels(win_labels, fontsize=9)
    ax.set_xlabel("Period", fontsize=11, color=t["muted"])
    ax.set_ylabel("Window", fontsize=11, color=t["muted"])
    ax.set_title(f"{strategy_label} — Period Performance Over Time\n{metric_label}",
                 fontsize=13, color=t["text"], pad=12)

    # Mark best period per window with a star
    for i, (best_p, best_v) in enumerate(best_per_window):
        if best_p is not None and best_p in periods:
            j = periods.index(best_p)
            ax.plot(j, i, marker="*", color="white", markersize=12, markeredgecolor="black", markeredgewidth=0.5)

    # Annotate values if grid is small enough
    if n_win * n_per <= 200:
        for i in range(n_win):
            for j in range(n_per):
                val = matrix[i, j]
                if not np.isnan(val):
                    color = "black" if val > np.nanmedian(matrix) else "white"
                    ax.text(j, i, f"{val:.0f}" if abs(val) >= 10 else f"{val:.1f}",
                            ha="center", va="center", fontsize=max(5, min(7, 150 // max(n_win, n_per))), color=color)

    _apply_dark_theme(fig, ax, theme)
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def generate_rolling_3d_surface(sweep_data, metric, strategy_label, theme="dark"):
    """3D surface: X=window, Y=period, Z=metric. Returns base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    from io import BytesIO
    import base64

    t = _get_theme(theme)
    matrix = sweep_data["matrix"]
    win_labels = sweep_data["windows"]
    periods = sweep_data["periods"]
    n_win, n_per = matrix.shape

    metric_names = {"total_return": "Return %", "alpha": "Alpha %", "sharpe": "Sharpe"}
    metric_label = metric_names.get(metric, metric)

    fig = plt.figure(figsize=(14, 9), dpi=150)
    ax = fig.add_subplot(111, projection="3d")

    X, Y = np.meshgrid(range(n_win), range(n_per), indexing="ij")
    Z = np.nan_to_num(matrix, nan=0.0)

    surf = ax.plot_surface(X, Y, Z, cmap="RdYlGn", rstride=1, cstride=1,
                           alpha=0.85, edgecolor="none", antialiased=True)
    fig.colorbar(surf, ax=ax, shrink=0.5, pad=0.08, label=metric_label)

    ax.set_xticks(range(n_win))
    ax.set_xticklabels(win_labels, fontsize=7, rotation=20)
    ax.set_yticks(range(0, n_per, max(1, n_per // 8)))
    ax.set_yticklabels([periods[i] for i in range(0, n_per, max(1, n_per // 8))], fontsize=7)
    ax.set_xlabel("Window", fontsize=9, color=t["muted"], labelpad=10)
    ax.set_ylabel("Period", fontsize=9, color=t["muted"], labelpad=10)
    ax.set_zlabel(metric_label, fontsize=9, color=t["muted"], labelpad=8)
    ax.set_title(f"{strategy_label} — 3D Performance Surface\n{metric_label} by Window & Period",
                 fontsize=13, color=t["text"], pad=20)

    ax.view_init(elev=25, azim=-45)

    # Theme the 3D axes
    fig.patch.set_facecolor(t["panel"])
    ax.set_facecolor(t["bg"])
    ax.tick_params(colors=t["muted"])
    ax.xaxis.pane.fill = False
    ax.yaxis.pane.fill = False
    ax.zaxis.pane.fill = False
    ax.xaxis.pane.set_edgecolor(t["grid"])
    ax.yaxis.pane.set_edgecolor(t["grid"])
    ax.zaxis.pane.set_edgecolor(t["grid"])
    fig.text(0.98, 0.01, "the-bitcoin-strategy.com", fontsize=9,
             color=t["branding_color"], alpha=t["branding_alpha"],
             ha="right", va="bottom", transform=fig.transFigure)

    fig.subplots_adjust(left=0.05, right=0.92, top=0.92, bottom=0.08)

    buf = BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


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
  python backtest.py --asset ethereum --vs bitcoin --ind2 sma --period2 40  # ETH/BTC ratio with SMA(40)
  python backtest.py --asset ethereum --vs bitcoin --mode sweep-chart       # sweep on ETH/BTC ratio
  python backtest.py --asset solana --vs ethereum --mode sweep-dual         # SOL/ETH heatmap
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
    parser.add_argument("--vs", default=None,
                        help="Denominator asset for relative price mode (e.g., --asset ethereum --vs bitcoin)")
    parser.add_argument("--data", default=None, help="CSV file path (overrides --asset)")
    parser.add_argument("--db", action="store_true",
                        help="Force loading from PostgreSQL (requires PRICE_DB_URL env var)")

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

        data_dir_osc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        use_db_osc = True if args.db else None
        if args.data:
            df = load_data(args.data)
        else:
            df = load_asset(args.asset, data_dir=data_dir_osc, use_db=use_db_osc)
        if args.vs:
            if args.data:
                df_vs = load_data(os.path.join(data_dir_osc, f"{args.vs}.csv"))
            else:
                df_vs = load_asset(args.vs, data_dir=data_dir_osc, use_db=use_db_osc)
            common_idx = df.index.intersection(df_vs.index)
            if len(common_idx) == 0:
                print(f"Error: No overlapping dates between {args.asset} and {args.vs}.")
                sys.exit(1)
            df = df.loc[common_idx]
            df["close"] = df["close"] / df_vs.loc[common_idx, "close"]
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
        asset = f"{args.asset}_vs_{args.vs}" if args.vs else args.asset
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
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    use_db = True if args.db else None  # None = auto-detect via PRICE_DB_URL

    asset_label = args.asset
    if args.data:
        print(f"Loading {args.asset} data from {args.data}...")
        df = load_data(args.data)
    else:
        source = "database" if (use_db or (use_db is None and os.environ.get("PRICE_DB_URL"))) else f"data/{args.asset}.csv"
        print(f"Loading {args.asset} data from {source}...")
        df = load_asset(args.asset, data_dir=data_dir, use_db=use_db)

    # Relative price mode: divide by denominator asset
    if args.vs:
        print(f"Loading {args.vs} data for relative price (ratio)...")
        if args.data:
            vs_path = os.path.join(data_dir, f"{args.vs}.csv")
            df_vs = load_data(vs_path)
        else:
            df_vs = load_asset(args.vs, data_dir=data_dir, use_db=use_db)
        try:
            df = compute_ratio_prices(df, df_vs)
        except ValueError:
            print(f"Error: No overlapping dates between {args.asset} and {args.vs}.")
            sys.exit(1)
        asset_label = f"{args.asset}/{args.vs}"

    if args.start_date:
        df = df[df.index >= pd.Timestamp(args.start_date, tz="UTC")]
    if args.end_date:
        df = df[df.index <= pd.Timestamp(args.end_date, tz="UTC")]
    asset_display = f"{args.asset.capitalize()} / {args.vs.capitalize()}" if args.vs else args.asset.capitalize()
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
        generate_chart(df, best, args.chart_file, asset_display)


if __name__ == "__main__":
    main()
