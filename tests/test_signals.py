"""Tests for signal detection and live chart correctness."""

import pandas as pd
import numpy as np
import sys
import os
import re

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest as bt


# ---------------------------------------------------------------------------
# Bug fix: Telegram signal notification must detect crossover on the same day
# (no .shift(1) in notification logic — shift is only for backtesting)
# ---------------------------------------------------------------------------

class TestSignalDetectionNoShift:
    """Verify that check_and_send_signals logic detects crossovers immediately,
    not one day late.  We replicate the signal detection from fetch_prices.py
    and assert it fires on the day the crossover occurs."""

    def _make_df(self, prices):
        """Build a minimal DataFrame with a 'close' column."""
        dates = pd.date_range("2025-01-01", periods=len(prices), freq="D", tz="UTC")
        return pd.DataFrame({"close": prices}, index=dates)

    def _detect_signal(self, df, ind1_name="price", ind1_period=None,
                       ind2_name="sma", ind2_period=5, exposure="long-cash",
                       reverse=False):
        """Replicate the signal detection logic from fetch_prices.py.
        Returns (pos_today, pos_yesterday, signal_or_none)."""
        result = bt.run_strategy(
            df, ind1_name, ind1_period, ind2_name, ind2_period,
            initial_cash=10000, exposure=exposure, reverse=reverse,
        )
        ind1_s = result['ind1_series']
        ind2_s = result['ind2_series']
        above = ind1_s > ind2_s
        if reverse:
            above = ~above
        # CRITICAL: no .shift(1) here — notifications detect on the day it happens
        position = bt._apply_exposure(above, exposure).fillna(0)
        nan_mask = ind1_s.isna() | ind2_s.isna()
        position[nan_mask] = 0

        if len(position) < 2:
            return 0, 0, None

        pos_today = position.iloc[-1]
        pos_yesterday = position.iloc[-2]

        if pos_today == pos_yesterday:
            return pos_today, pos_yesterday, None
        return pos_today, pos_yesterday, "BUY" if pos_today > pos_yesterday else "SELL"

    def test_sell_signal_detected_on_crossover_day(self):
        """Price drops below SMA on the last day — signal must fire immediately."""
        # 10 days of rising prices (above SMA), then a sharp drop on day 11
        prices = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 90]
        df = self._make_df(prices)
        pos_today, pos_yesterday, signal = self._detect_signal(df, ind2_period=5)
        assert signal == "SELL", (
            f"Expected SELL on crossover day, got signal={signal} "
            f"(pos_today={pos_today}, pos_yesterday={pos_yesterday})"
        )

    def test_buy_signal_detected_on_crossover_day(self):
        """Price rises above SMA on the last day — signal must fire immediately."""
        # 10 days of falling prices (below SMA), then a sharp rise on day 11
        prices = [100, 98, 96, 94, 92, 90, 88, 86, 84, 82, 120]
        df = self._make_df(prices)
        pos_today, pos_yesterday, signal = self._detect_signal(df, ind2_period=5)
        assert signal == "BUY", (
            f"Expected BUY on crossover day, got signal={signal} "
            f"(pos_today={pos_today}, pos_yesterday={pos_yesterday})"
        )

    def test_no_signal_when_position_unchanged(self):
        """Steady uptrend with price always above SMA — no signal."""
        prices = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120]
        df = self._make_df(prices)
        _, _, signal = self._detect_signal(df, ind2_period=5)
        assert signal is None, f"Expected no signal, got {signal}"

    def test_reverse_mode_inverts_signal(self):
        """In reverse mode, price dropping below SMA should be a BUY."""
        prices = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 90]
        df = self._make_df(prices)
        _, _, signal = self._detect_signal(df, ind2_period=5, reverse=True)
        assert signal == "BUY", f"Expected BUY in reverse mode, got {signal}"


# ---------------------------------------------------------------------------
# Bug fix: Live chart must pass __lwVsAsset for ratio charts and use
# price division (asset_price / vs_asset_price) in fetchLivePrice()
# ---------------------------------------------------------------------------

class TestLiveChartRatioMode:
    """Verify that the rendered HTML includes __lwVsAsset and ratio fetch logic
    for backtests that use a vs_asset."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_lwVsAsset_variable_exists_in_main_template(self):
        """The main backtest template must declare __lwVsAsset."""
        src = self._read_app_source()
        assert "__lwVsAsset" in src, "app.py must define __lwVsAsset for ratio chart live updates"

    def test_lwVsAsset_injected_in_backtest_detail(self):
        """The backtest detail route must inject __lwVsAsset into cached HTML."""
        src = self._read_app_source()
        # The injection builds a <script> block with both __lwAsset and __lwVsAsset
        assert re.search(r"__lwVsAsset\s*=\s*\{?json_mod\.dumps", src), (
            "Backtest detail route must inject __lwVsAsset into cached HTML"
        )

    def _read_chart_js(self):
        """Read chart.js if it exists, otherwise return empty string."""
        chart_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "chart.js")
        if os.path.isfile(chart_path):
            with open(chart_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def test_fetchLivePrice_has_ratio_division(self):
        """fetchLivePrice() must divide prices when vsAsset is set."""
        # Check both app.py and static/js/chart.js (post-refactor)
        src = self._read_app_source() + self._read_chart_js()
        matches = list(re.finditer(r"d1\.price\s*/\s*d2\.price", src))
        assert len(matches) >= 1, (
            f"Expected at least 1 instance of ratio division (d1.price / d2.price) "
            f"in fetchLivePrice(), found {len(matches)}"
        )

    def test_fetchLivePrice_fetches_both_assets(self):
        """fetchLivePrice() must fetch both asset and vsAsset prices in ratio mode."""
        # Check both app.py and static/js/chart.js (post-refactor)
        src = self._read_app_source() + self._read_chart_js()
        assert "Promise.all" in src, (
            "fetchLivePrice must use Promise.all to fetch both asset prices for ratio mode"
        )


# ---------------------------------------------------------------------------
# Regression: fetch_prices.py must NOT use .shift(1) in signal detection
# ---------------------------------------------------------------------------

class TestFetchPricesNoShift:
    """Directly check that fetch_prices.py signal detection code does not
    apply .shift(1) — the root cause of the delayed notification bug."""

    def test_no_shift_in_signal_detection(self):
        fp_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fetch_prices.py")
        with open(fp_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Find lines inside check_and_send_signals that are actual code (not comments)
        in_func = False
        for line in lines:
            if "def check_and_send_signals" in line:
                in_func = True
                continue
            if in_func and re.match(r"^def ", line):
                break
            if in_func:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # skip comments
                assert ".shift(1)" not in line, (
                    f"check_and_send_signals() must NOT use .shift(1) on position — "
                    f"this delays signal detection by one day. "
                    f"The shift is only for backtesting (look-ahead bias), not notifications.\n"
                    f"Offending line: {line.strip()}"
                )


# ---------------------------------------------------------------------------
# Bug fix: Asset name case-insensitive resolution
# "solana" must resolve to "Solana", not silently fall back to bitcoin
# ---------------------------------------------------------------------------

class TestAssetCaseResolution:
    """Verify that asset names are resolved case-insensitively so saved
    backtests with lowercase names (e.g. 'solana') still work after
    asset names were capitalized in the DB."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_resolve_asset_function_exists(self):
        """app.py must define a _resolve_asset helper for case-insensitive lookup."""
        src = self._read_app_source()
        assert "def _resolve_asset" in src, (
            "app.py must have a _resolve_asset() function for case-insensitive asset lookup"
        )

    def test_params_uses_resolve_asset(self):
        """Params.__init__ must use _resolve_asset for both asset and vs_asset."""
        src = self._read_app_source()
        # Find the Params __init__ method
        init_match = re.search(
            r"class Params:.*?def __init__\(self.*?\):(.*?)(?=\n    def |\nclass )",
            src, re.DOTALL
        )
        assert init_match, "Params.__init__ not found"
        init_body = init_match.group(1)
        assert "_resolve_asset" in init_body, (
            "Params.__init__ must use _resolve_asset() to normalize asset names — "
            "without this, lowercase asset names from saved backtests or URLs "
            "silently fall back to the default asset (bitcoin)"
        )

    def test_backtest_detail_uses_resolve_asset(self):
        """backtest_detail route must resolve asset names from saved params."""
        src = self._read_app_source()
        # Find the backtest_detail function
        detail_match = re.search(
            r"def backtest_detail\(bt_id\):(.*?)(?=\n@app\.route|\ndef \w+\()",
            src, re.DOTALL
        )
        assert detail_match, "backtest_detail() not found"
        detail_body = detail_match.group(1)
        assert "_resolve_asset" in detail_body, (
            "backtest_detail() must use _resolve_asset() when reading asset from "
            "saved params — saved backtests may have lowercase names"
        )

    def test_price_db_case_insensitive(self):
        """price_db.get_asset_df must use case-insensitive name matching."""
        pdb_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "price_db.py")
        with open(pdb_path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "LOWER" in src, (
            "price_db.py must use case-insensitive (LOWER) matching for asset names "
            "so that 'solana' matches 'Solana' in the database"
        )


# ---------------------------------------------------------------------------
# Asset rename must propagate to all backtests
# ---------------------------------------------------------------------------

class TestAssetRenamePropagation:
    """Verify that renaming an asset updates all backtests that reference it."""

    def test_rename_asset_in_backtests_exists(self):
        """database.py must have rename_asset_in_backtests function."""
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database.py")
        with open(db_path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "def rename_asset_in_backtests" in src, (
            "database.py must have rename_asset_in_backtests() to propagate "
            "asset renames to saved backtests"
        )

    def test_rename_endpoint_calls_propagation(self):
        """The rename-asset API must call rename_asset_in_backtests."""
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        # Find the api_rename_asset function
        func_match = re.search(
            r"def api_rename_asset\(\):(.*?)(?=\n@app\.route|\ndef \w+\()",
            src, re.DOTALL
        )
        assert func_match, "api_rename_asset() not found"
        func_body = func_match.group(1)
        assert "rename_asset_in_backtests" in func_body, (
            "api_rename_asset() must call db.rename_asset_in_backtests() to "
            "propagate the rename to all saved backtests"
        )

    def test_rename_updates_tickers_and_meta(self):
        """The rename endpoint must also update ASSET_TICKERS and _ASSET_META."""
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        func_match = re.search(
            r"def api_rename_asset\(\):(.*?)(?=\n@app\.route|\ndef \w+\()",
            src, re.DOTALL
        )
        assert func_match, "api_rename_asset() not found"
        func_body = func_match.group(1)
        assert "ASSET_TICKERS" in func_body, (
            "api_rename_asset() must update ASSET_TICKERS when renaming"
        )
        assert "_ASSET_META" in func_body, (
            "api_rename_asset() must update _ASSET_META when renaming"
        )


# ---------------------------------------------------------------------------
# No silent fallbacks — errors instead of wrong data
# ---------------------------------------------------------------------------

class TestNoSilentFallbacks:
    """Verify that missing assets produce errors, not silent fallback to bitcoin."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_no_fallback_in_run_handler(self):
        """_run_post_handler must not silently fall back to DEFAULT_ASSET."""
        src = self._read_app_source()
        # Find the _run_post_handler function
        func_match = re.search(
            r"def _run_post_handler\(.*?\):(.*?)(?=\ndef \w+\()",
            src, re.DOTALL
        )
        assert func_match, "_run_post_handler() not found"
        func_body = func_match.group(1)
        assert "ASSETS.get(p.asset, ASSETS[DEFAULT_ASSET])" not in func_body, (
            "_run_post_handler must NOT silently fall back to DEFAULT_ASSET — "
            "show an error instead so the user knows the asset wasn't found"
        )

    def test_no_fallback_in_backtest_detail(self):
        """backtest_detail must not silently fall back to DEFAULT_ASSET for live chart."""
        src = self._read_app_source()
        func_match = re.search(
            r"def backtest_detail\(bt_id\):(.*?)(?=\n@app\.route|\ndef \w+\()",
            src, re.DOTALL
        )
        assert func_match, "backtest_detail() not found"
        func_body = func_match.group(1)
        assert "ASSETS.get(_asset, ASSETS.get(DEFAULT_ASSET))" not in func_body, (
            "backtest_detail must NOT silently fall back to DEFAULT_ASSET for "
            "live chart data — raise an error so the cached HTML is served as-is"
        )

    def test_error_message_for_missing_asset(self):
        """The run handler must show an error when asset is not found."""
        src = self._read_app_source()
        func_match = re.search(
            r"def _run_post_handler\(.*?\):(.*?)(?=\ndef \w+\()",
            src, re.DOTALL
        )
        assert func_match, "_run_post_handler() not found"
        func_body = func_match.group(1)
        assert "not found" in func_body.lower() or "not in ASSETS" in func_body, (
            "_run_post_handler must show a user-visible error when the asset is not found"
        )


class TestSavedBacktestPeriods:
    """Verify that saved backtests include indicator periods from best results,
    and that the detail page displays all backtest parameters."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def _extract_detail_html(self):
        """Extract the DETAIL_HTML section from app.py source (includes NAV_HTML concat)."""
        src = self._read_app_source()
        # DETAIL_HTML is built with """ + NAV_HTML + """, so extract the full
        # source region between DETAIL_HTML = and the next top-level assignment.
        match = re.search(r'(DETAIL_HTML\s*=\s*""".*?)(?=\n[A-Z_]+_HTML\s*=)', src, re.DOTALL)
        assert match, "DETAIL_HTML template not found in app.py"
        return match.group(1)

    # --- Save/publish mechanism: periods survive disabled form fields ---

    def test_best_params_element_in_results(self):
        """Results panel must include a hidden element with best ind2_period
        so that save/publish captures the period even when the form field is disabled."""
        src = self._read_app_source()
        assert 'id="best-params"' in src, (
            "Results HTML must contain a hidden #best-params element that stores "
            "the best result's indicator periods for save/publish"
        )
        assert 'data-ind2-period' in src, (
            "#best-params must have a data-ind2-period attribute"
        )

    def test_save_merges_best_params(self):
        """saveBacktest() must call _mergeBestParams to include periods from sweep results."""
        src = self._read_app_source()
        func_match = re.search(
            r"function saveBacktest\(\)(.*?)(?=\nfunction \w+)",
            src, re.DOTALL
        )
        assert func_match, "saveBacktest() not found"
        func_body = func_match.group(1)
        assert "_mergeBestParams(params)" in func_body, (
            "saveBacktest() must call _mergeBestParams(params) to include "
            "indicator periods from sweep/heatmap best results"
        )

    def test_publish_merges_best_params(self):
        """publishBacktest() must call _mergeBestParams to include periods from sweep results."""
        src = self._read_app_source()
        func_match = re.search(
            r"function publishBacktest\(.*?\)(.*?)(?=\nfunction \w+)",
            src, re.DOTALL
        )
        assert func_match, "publishBacktest() not found"
        func_body = func_match.group(1)
        assert "_mergeBestParams(params)" in func_body, (
            "publishBacktest() must call _mergeBestParams(params) to include "
            "indicator periods from sweep/heatmap best results"
        )

    # --- Detail page: all saved params must be visible ---

    def test_detail_shows_asset(self):
        """Detail page must display the asset name."""
        html = self._extract_detail_html()
        assert 'bt_params.asset' in html, "Detail page must show bt_params.asset"

    def test_detail_shows_mode(self):
        """Detail page must display the backtest mode."""
        html = self._extract_detail_html()
        assert 'bt_params.mode' in html or "bt_params.get('mode')" in html, \
            "Detail page must show the backtest mode"

    def test_detail_shows_indicator1_with_period(self):
        """Detail page must display indicator 1 name and period."""
        html = self._extract_detail_html()
        assert 'bt_params.ind1_name' in html, "Detail page must show ind1_name"
        assert "bt_params.get('period1')" in html or 'bt_params.get("period1")' in html, \
            "Detail page must show period1 when present"

    def test_detail_shows_indicator2_with_period(self):
        """Detail page must display indicator 2 name and period."""
        html = self._extract_detail_html()
        assert 'bt_params.ind2_name' in html, "Detail page must show ind2_name"
        assert "bt_params.get('period2')" in html or 'bt_params.get("period2")' in html, \
            "Detail page must show period2 when present"

    def test_detail_shows_exposure(self):
        """Detail page must display the exposure type."""
        html = self._extract_detail_html()
        assert 'bt_params.exposure' in html, "Detail page must show exposure"

    def test_detail_shows_leverage(self):
        """Detail page must display leverage settings when non-default."""
        html = self._extract_detail_html()
        assert 'long_leverage' in html, "Detail page must show long leverage"
        assert 'short_leverage' in html, "Detail page must show short leverage"

    def test_detail_shows_date_range(self):
        """Detail page must display start and end dates."""
        html = self._extract_detail_html()
        assert 'start_date' in html, "Detail page must show start_date"
        assert 'end_date' in html, "Detail page must show end_date"

    def test_detail_shows_fee(self):
        """Detail page must display the trading fee."""
        html = self._extract_detail_html()
        assert 'fee' in html, "Detail page must show fee"

    def test_detail_shows_capital(self):
        """Detail page must display initial capital."""
        html = self._extract_detail_html()
        assert 'initial_cash' in html, "Detail page must show initial_cash"

    def test_detail_shows_sizing(self):
        """Detail page must display the position sizing mode."""
        html = self._extract_detail_html()
        assert 'sizing' in html, "Detail page must show sizing mode"

    def test_detail_shows_oscillator_params(self):
        """Detail page must display oscillator params when applicable."""
        html = self._extract_detail_html()
        assert 'osc_name' in html, "Detail page must show oscillator name when applicable"
        assert 'osc_period' in html, "Detail page must show oscillator period when applicable"
        assert 'buy_threshold' in html, "Detail page must show buy threshold when applicable"
        assert 'sell_threshold' in html, "Detail page must show sell threshold when applicable"

    def test_detail_shows_reverse_flag(self):
        """Detail page must indicate when signal is reversed."""
        html = self._extract_detail_html()
        assert 'reverse' in html.lower(), "Detail page must show reverse signal flag"

    # --- Backfill: existing backtests with missing period2 ---

    def test_detail_backfills_missing_period2(self):
        """backtest_detail() must backfill period2 from cached HTML for older saves."""
        src = self._read_app_source()
        func_match = re.search(
            r"def backtest_detail\(bt_id\):(.*?)(?=\n@app\.route|\ndef \w+\()",
            src, re.DOTALL
        )
        assert func_match, "backtest_detail() not found"
        func_body = func_match.group(1)
        assert "period2" in func_body and "ind2Label" in func_body or "ind2-period" in func_body, (
            "backtest_detail must backfill missing period2 from cached HTML "
            "so older saved backtests still show the indicator period"
        )
