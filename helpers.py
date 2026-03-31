"""Shared helper functions used by app.py, backtest.py, and fetch_prices.py."""


def compute_ratio_prices(df, df_vs):
    """Divide df's close by df_vs's close on common dates.

    Both DataFrames must have a DatetimeIndex and a 'close' column.
    Returns a modified copy of df with only the overlapping dates,
    where close = df.close / df_vs.close.

    Raises ValueError if there are no overlapping dates.
    """
    df = df.copy()
    df_vs = df_vs.copy()
    df.index = df.index.normalize()
    df_vs.index = df_vs.index.normalize()
    df = df[~df.index.duplicated(keep='first')]
    df_vs = df_vs[~df_vs.index.duplicated(keep='first')]
    common = df.index.intersection(df_vs.index)
    if len(common) == 0:
        raise ValueError("No overlapping dates between the two assets")
    df = df.loc[common]
    df["close"] = df["close"] / df_vs.loc[common, "close"]
    return df
