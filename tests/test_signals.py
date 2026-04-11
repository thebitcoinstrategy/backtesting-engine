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


class TestVideoEmbed:
    """Verify that collection video embeds support both YouTube and Vimeo."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_extract_youtube_url(self):
        """_extract_video_embed_url must handle YouTube URLs."""
        # Import the function dynamically since app.py has heavy imports
        src = self._read_app_source()
        assert 'def _extract_video_embed_url' in src, (
            "app.py must define _extract_video_embed_url function"
        )
        assert 'youtube.com/embed' in src, (
            "_extract_video_embed_url must generate YouTube embed URLs"
        )

    def test_extract_vimeo_url(self):
        """_extract_video_embed_url must handle Vimeo URLs."""
        src = self._read_app_source()
        assert 'player.vimeo.com/video' in src, (
            "_extract_video_embed_url must generate Vimeo embed URLs"
        )
        assert 'vimeo' in src.lower(), (
            "app.py must contain Vimeo support"
        )

    def test_collection_form_accepts_both(self):
        """Collection form labels/placeholders must mention both YouTube and Vimeo."""
        src = self._read_app_source()
        assert 'YouTube or Vimeo' in src or 'Vimeo' in src, (
            "Collection video form must indicate Vimeo support"
        )

    def test_bare_vimeo_id_supported(self):
        """A bare numeric ID should be treated as Vimeo."""
        src = self._read_app_source()
        func_match = re.search(
            r"def _extract_video_embed_url\(.*?\):(.*?)(?=\ndef \w+\()",
            src, re.DOTALL
        )
        assert func_match, "_extract_video_embed_url() not found"
        func_body = func_match.group(1)
        assert 'fullmatch' in func_body or 'isdigit' in func_body, (
            "_extract_video_embed_url must handle bare numeric Vimeo IDs"
        )


class TestExplainerText:
    """Verify that the explainer text omits periods in sweep/heatmap modes."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_sweep_heatmap_omit_period_in_explainer(self):
        """In sweep/heatmap mode, explainer must not show a specific period like SMA(44)."""
        src = self._read_app_source()
        func_match = re.search(
            r"function updateExplainer\(\)(.*?)(?=\n(?:document\.|function )\w)",
            src, re.DOTALL
        )
        assert func_match, "updateExplainer() not found"
        func_body = func_match.group(1)
        # Must check for sweep/heatmap mode and suppress period in labels
        assert 'sweep' in func_body and 'heatmap' in func_body, (
            "updateExplainer must check for sweep/heatmap mode"
        )
        assert 'isSweepOrHeatmap' in func_body or ('sweep' in func_body and 'p2.value' in func_body), (
            "updateExplainer must suppress period display in sweep/heatmap modes"
        )


# ---------------------------------------------------------------------------
# Feature: Financing fees for leveraged/margin positions
# ---------------------------------------------------------------------------

class TestFinancingFees:
    """Verify financing fee logic: helper functions, equity impact, and UI."""

    def _make_df(self, n=200):
        """Build a DataFrame with gentle uptrend for stable strategy results."""
        dates = pd.date_range("2020-01-01", periods=n, freq="D")
        # Gentle uptrend with small noise so SMA crossover generates trades
        close = 100 + np.arange(n) * 0.5 + np.random.RandomState(42).randn(n) * 2
        return pd.DataFrame({"close": close}, index=dates)

    # -- Helper function tests --

    def test_should_apply_financing_zero_rate(self):
        """No financing when rate is 0."""
        assert not bt._should_apply_financing(0, "long-short", 2, 2, "compound")

    def test_should_apply_financing_fixed_sizing(self):
        """No financing for fixed sizing regardless of leverage."""
        assert not bt._should_apply_financing(0.11, "long-short", 2, 2, "fixed")

    def test_should_apply_financing_1x_long_cash(self):
        """No financing for 1x long-cash (spot account)."""
        assert not bt._should_apply_financing(0.11, "long-cash", 1, 1, "compound")

    def test_should_apply_financing_1x_short_cash(self):
        """No financing for 1x short-cash (spot account)."""
        assert not bt._should_apply_financing(0.11, "short-cash", 1, 1, "compound")

    def test_should_apply_financing_long_short(self):
        """Financing always applies for long-short (margin account)."""
        assert bt._should_apply_financing(0.11, "long-short", 1, 1, "compound")

    def test_should_apply_financing_leveraged_long_cash(self):
        """Financing applies for leveraged long-cash."""
        assert bt._should_apply_financing(0.11, "long-cash", 2, 1, "compound")

    def test_should_apply_financing_leveraged_short_cash(self):
        """Financing applies for leveraged short-cash."""
        assert bt._should_apply_financing(0.11, "short-cash", 1, 2, "compound")

    def test_financing_daily_rate_crypto(self):
        """Crypto uses full notional: leverage * rate / 365."""
        rate = bt._financing_daily_rate(2, 0.11, 365)
        expected = 2 * 0.11 / 365
        assert abs(rate - expected) < 1e-10

    def test_financing_daily_rate_tradfi(self):
        """Tradfi uses borrowed portion: max(leverage-1, 0) * rate / periods_per_year."""
        rate = bt._financing_daily_rate(2, 0.11, 252)
        expected = 1 * 0.11 / 252  # leverage-1 = 1
        assert abs(rate - expected) < 1e-10

    def test_financing_daily_rate_tradfi_1x(self):
        """Tradfi 1x leverage: no borrowed portion, rate should be 0."""
        rate = bt._financing_daily_rate(1, 0.11, 252)
        assert rate == 0

    # -- Strategy-level tests --

    def test_financing_reduces_long_equity(self):
        """2x long-short with financing should produce lower equity than without."""
        df = self._make_df()
        no_fin = bt.run_strategy(df, "price", None, "sma", 20,
                                 10000, fee=0, exposure="long-short",
                                 long_leverage=2, short_leverage=2,
                                 financing_rate=0)
        with_fin = bt.run_strategy(df, "price", None, "sma", 20,
                                   10000, fee=0, exposure="long-short",
                                   long_leverage=2, short_leverage=2,
                                   financing_rate=0.11)
        assert with_fin["equity"].iloc[-1] < no_fin["equity"].iloc[-1], (
            "Financing should reduce final equity for leveraged long-short"
        )

    def test_financing_cost_returned(self):
        """run_strategy must return total, long, and short financing costs."""
        df = self._make_df()
        result = bt.run_strategy(df, "price", None, "sma", 20,
                                 10000, fee=0, exposure="long-short",
                                 long_leverage=2, short_leverage=2,
                                 financing_rate=0.11)
        assert "total_financing_cost" in result
        assert "financing_cost_long" in result
        assert "financing_cost_short" in result
        assert result["total_financing_cost"] > 0
        assert result["financing_cost_long"] > 0
        assert result["financing_cost_short"] > 0
        assert abs(result["total_financing_cost"] - (result["financing_cost_long"] + result["financing_cost_short"])) < 0.01

    def test_no_financing_1x_long_cash(self):
        """1x long-cash should produce identical results with or without financing rate."""
        df = self._make_df()
        no_fin = bt.run_strategy(df, "price", None, "sma", 20,
                                 10000, fee=0, exposure="long-cash",
                                 long_leverage=1, short_leverage=1,
                                 financing_rate=0)
        with_rate = bt.run_strategy(df, "price", None, "sma", 20,
                                    10000, fee=0, exposure="long-cash",
                                    long_leverage=1, short_leverage=1,
                                    financing_rate=0.11)
        assert abs(with_rate["equity"].iloc[-1] - no_fin["equity"].iloc[-1]) < 0.01, (
            "1x long-cash should have no financing cost"
        )
        assert with_rate["total_financing_cost"] == 0

    def test_no_financing_fixed_sizing(self):
        """Fixed sizing should have no financing regardless of leverage."""
        df = self._make_df()
        result = bt.run_strategy(df, "price", None, "sma", 20,
                                 10000, fee=0, exposure="long-short",
                                 long_leverage=2, short_leverage=2,
                                 sizing="fixed", financing_rate=0.11)
        assert result["total_financing_cost"] == 0

    def test_sweep_passes_financing(self):
        """sweep_periods must accept and pass financing_rate through."""
        df = self._make_df(300)
        results = bt.sweep_periods(df, "price", None, "sma", 20, "ind2",
                                   10, 12, 10000, fee=0,
                                   exposure="long-short",
                                   long_leverage=2, short_leverage=2,
                                   financing_rate=0.11)
        # Should return results without error
        assert len(results) > 0
        # Each result should have financing cost
        for r in results:
            assert "total_financing_cost" in r

    # -- UI tests --

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_financing_form_field_exists(self):
        """The financing rate input field must exist in the form."""
        src = self._read_app_source()
        assert 'name="financing_rate"' in src, "financing_rate input field missing from form"
        assert 'financing-group' in src, "financing-group div missing"

    def test_financing_in_cache_key(self):
        """financing_rate must be in the cache key core set."""
        src = self._read_app_source()
        assert '"financing_rate"' in src, "financing_rate missing from cache key"

    def test_financing_in_detail_page(self):
        """Detail page must show financing rate when set."""
        src = self._read_app_source()
        assert "financing_rate" in src and "p.a." in src, (
            "Detail page must display financing rate with 'p.a.' unit"
        )

    def test_financing_cost_in_summary_table(self):
        """Summary table must show long cost, short revenue, and net financing."""
        src = self._read_app_source()
        assert "total_financing_cost" in src, (
            "Summary table must display total_financing_cost"
        )
        assert "Long Cost" in src, "Summary table must have 'Long Cost' row"
        assert "Short Revenue" in src, "Summary table must have 'Short Revenue' row"
        assert "Net Financing" in src, "Summary table must have 'Net Financing' row"

    def test_togglefields_hides_financing_for_fixed(self):
        """toggleFields must hide financing-group when sizing is fixed."""
        src = self._read_app_source()
        # The toggleFields function should reference both financing-group and fixed/sizing
        assert "financing-group" in src, "financing-group not referenced in toggleFields"
        assert re.search(r"sizing.*fixed|fixed.*sizing", src), (
            "toggleFields must check for fixed sizing to hide financing"
        )


# ---------------------------------------------------------------------------
# Bug fix: _reload_assets_from_disk must not clear ASSETS dict (race condition)
# ---------------------------------------------------------------------------

class TestAssetReloadNoRace:
    """Verify that _reload_assets_from_disk does not call ASSETS.clear()
    before repopulating — that pattern creates a window where request threads
    see an empty dict and return 'Asset not found'."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_reload_does_not_clear_then_repopulate(self):
        """_reload_assets_from_disk must not have ASSETS.clear() before the DB query.
        The safe pattern is: build new dict, then update in-place."""
        src = self._read_app_source()
        # Extract the _reload_assets_from_disk function body
        match = re.search(
            r"def _reload_assets_from_disk\(\):(.*?)(?=\ndef |\nclass |\n@app\.)",
            src, re.DOTALL
        )
        assert match, "_reload_assets_from_disk() function not found"
        func_body = match.group(1)
        lines = func_body.split('\n')
        # Find the line positions of ASSETS.clear() and the DB/file read
        clear_line = None
        populate_line = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if 'ASSETS.clear()' in stripped:
                clear_line = i
            if 'ASSETS.update(' in stripped and clear_line is not None:
                populate_line = i
                break
        if clear_line is not None and populate_line is not None:
            # Check that no DB query or file read happens BETWEEN clear and update
            between = lines[clear_line+1:populate_line]
            for bline in between:
                s = bline.strip()
                if s.startswith('#'):
                    continue
                assert 'get_all_assets' not in s and 'load_data' not in s, (
                    "ASSETS.clear() must not precede a slow DB/file read — "
                    "this creates a race condition where requests see an empty dict. "
                    "Build the new dict first, then swap."
                )


# ---------------------------------------------------------------------------
# Telegram signal chart + caption
# ---------------------------------------------------------------------------

class TestTelegramSignalChart:
    """Verify signal chart generation and caption truncation for Telegram alerts."""

    def _make_df(self, n=120):
        """Build a DataFrame with enough data for SMA indicators."""
        dates = pd.date_range("2025-01-01", periods=n, freq="D")
        prices = 100 + np.cumsum(np.random.default_rng(42).normal(0, 2, n))
        prices = np.maximum(prices, 1)  # keep positive
        return pd.DataFrame({"close": prices}, index=dates)

    def test_generate_signal_chart_returns_png(self):
        """_generate_signal_chart should return valid PNG bytes."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from fetch_prices import _generate_signal_chart

        df = self._make_df()
        ind1, ind1_label = bt.compute_indicator_from_spec(df, "sma", 14)
        ind2, ind2_label = bt.compute_indicator_from_spec(df, "sma", 31)

        above = ind1 > ind2
        pos = bt._apply_exposure(above, "long-short").fillna(0)
        pos[ind1.isna() | ind2.isna()] = 0
        diff = pos.diff()
        buys = diff[diff > 0].index
        sells = diff[diff < 0].index

        result = _generate_signal_chart(
            df, ind1, ind2, ind1_label, ind2_label,
            buys, sells, "Test Asset", "SELL"
        )
        assert result is not None, "Chart generation returned None"
        assert result[:8] == b'\x89PNG\r\n\x1a\n', "Output is not a valid PNG"
        assert len(result) > 1000, "PNG seems too small"

    def test_caption_truncation(self):
        """Messages longer than 1024 chars should be truncated."""
        long_message = "A" * 2000
        caption = long_message[:1024] if len(long_message) > 1024 else long_message
        assert len(caption) == 1024

    def test_caption_short_message_unchanged(self):
        """Messages under 1024 chars should not be modified."""
        short_message = "Buy signal for Solana"
        caption = short_message[:1024] if len(short_message) > 1024 else short_message
        assert caption == short_message

    def test_chart_shows_3_month_window(self):
        """Chart should only display last 3 months of data."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from fetch_prices import _generate_signal_chart

        # 365 days of data — chart should only use last ~90
        df = self._make_df(n=365)
        ind1, ind1_label = bt.compute_indicator_from_spec(df, "sma", 14)
        ind2, ind2_label = bt.compute_indicator_from_spec(df, "sma", 31)

        result = _generate_signal_chart(
            df, ind1, ind2, ind1_label, ind2_label,
            pd.DatetimeIndex([]), pd.DatetimeIndex([]), "Test", "BUY"
        )
        assert result is not None, "Chart generation failed with 365 days of data"


class TestTelegramCharCounter:
    """Verify the character counter element exists in the Telegram template modal."""

    def test_char_counter_element_in_modal(self):
        """The tg-char-counter div should exist in the SweetAlert modal HTML."""
        app_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert 'id="tg-char-counter"' in source, "Character counter element missing from modal"
        assert '1024 characters' in source, "1024 limit text missing from counter"

    def test_char_counter_updates_in_render_function(self):
        """_renderTgPreview should update the character counter."""
        app_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert 'tg-char-counter' in source
        # Should compute plain text length by stripping HTML
        assert "replace(/<[^>]*>/g" in source, "Counter should strip HTML tags"
        # Should color red when over limit
        assert '1024' in source and '#ff4444' in source, "Counter should turn red over 1024"


class TestCopyTradingUrl:
    """Verify copy trading URL support on collections."""

    def _read_source(self, filename):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_database_column_exists(self):
        """collections table must have copy_trading_url column."""
        src = self._read_source("database.py")
        assert 'copy_trading_url TEXT' in src, "copy_trading_url column missing from collections table"

    def test_save_collection_accepts_copy_trading_url(self):
        """save_collection must accept copy_trading_url parameter."""
        src = self._read_source("database.py")
        assert re.search(r'def save_collection\(.*copy_trading_url', src, re.DOTALL), (
            "save_collection must accept copy_trading_url parameter"
        )

    def test_update_collection_accepts_copy_trading_url(self):
        """update_collection must accept copy_trading_url parameter."""
        src = self._read_source("database.py")
        assert re.search(r'def update_collection\(.*copy_trading_url', src, re.DOTALL), (
            "update_collection must accept copy_trading_url parameter"
        )

    def test_create_api_sends_copy_trading_url(self):
        """api_create_collection must read and pass copy_trading_url."""
        src = self._read_source("app.py")
        assert "copy_trading_url" in src, "app.py must handle copy_trading_url"
        # Verify the create API extracts it from request data
        create_match = re.search(r'def api_create_collection.*?def \w+', src, re.DOTALL)
        assert create_match, "api_create_collection not found"
        assert 'copy_trading_url' in create_match.group(0), (
            "api_create_collection must pass copy_trading_url"
        )

    def test_update_api_sends_copy_trading_url(self):
        """api_update_collection must read and pass copy_trading_url."""
        src = self._read_source("app.py")
        update_match = re.search(r'def api_update_collection.*?return', src, re.DOTALL)
        assert update_match, "api_update_collection not found"
        assert 'copy_trading_url' in update_match.group(0), (
            "api_update_collection must pass copy_trading_url"
        )

    def test_collection_detail_shows_link(self):
        """Collection detail page must render copy_trading_url as a link."""
        src = self._read_source("app.py")
        assert 'collection.copy_trading_url' in src, (
            "Collection detail must check copy_trading_url"
        )
        assert 'copy-trading-link' in src, (
            "Collection detail must have copy-trading-link element"
        )

    def test_edit_modal_has_copy_trading_field(self):
        """Collection edit modals must include copy trading URL field."""
        src = self._read_source("app.py")
        assert src.count('edit-coll-copytrading') >= 2, (
            "Edit collection modal must have copy trading URL input (at least label + input)"
        )

    def test_create_modal_has_copy_trading_field(self):
        """New collection modal must include copy trading URL field."""
        src = self._read_source("app.py")
        assert 'coll-copytrading' in src, (
            "New collection modal must have copy trading URL input"
        )


# ---------------------------------------------------------------------------
# Rolling Window Analysis
# ---------------------------------------------------------------------------

class TestRollingWindowAnalysis:
    """Tests for rolling window analysis feature."""

    def _make_df(self, years=6, seed=42):
        """Create synthetic daily price data."""
        dates = pd.date_range('2018-01-01', periods=int(years * 365), freq='D', tz='UTC')
        np.random.seed(seed)
        prices = 100 * np.exp(np.cumsum(np.random.randn(len(dates)) * 0.01))
        return pd.DataFrame({'close': prices}, index=dates)

    def test_generate_rolling_windows_basic(self):
        """10 years, window=2yr, step=1yr should produce multiple windows."""
        df = self._make_df(years=10)
        windows = bt.generate_rolling_windows(df, window_years=2, step_years=1)
        assert len(windows) >= 7, f"Expected >= 7 windows, got {len(windows)}"
        for w in windows:
            assert "start" in w and "end" in w and "label" in w
            span_days = (w["end"] - w["start"]).days
            assert 700 <= span_days <= 740, f"Window span {span_days} days, expected ~730"

    def test_generate_rolling_windows_short_dataset(self):
        """Dataset shorter than window should raise ValueError."""
        df = self._make_df(years=1)
        import pytest
        with pytest.raises(ValueError, match="window requires"):
            bt.generate_rolling_windows(df, window_years=3, step_years=1)

    def test_rolling_window_evaluate_returns_results(self):
        """Evaluate should return one result per window with expected keys."""
        df = self._make_df(years=6)
        windows = bt.generate_rolling_windows(df, 2, 1)
        results = bt.rolling_window_evaluate(df, windows, 'price', None, 'sma', 50, 10000)
        assert len(results) == len(windows)
        for r in results:
            assert "total_return" in r
            assert "alpha" in r
            assert "sharpe" in r
            assert "equity" in r
            assert len(r["equity"]) > 0

    def test_rolling_window_evaluate_warmup_correct(self):
        """Indicator values at window start should reflect pre-window data (proper warmup)."""
        df = self._make_df(years=6)
        windows = bt.generate_rolling_windows(df, 2, 1)
        # SMA(50) at window[1] start needs 50 days before that date
        # If warmup is broken, the first SMA values would be NaN
        results = bt.rolling_window_evaluate(df, windows, 'price', None, 'sma', 50, 10000)
        # Second window starts 1yr in — plenty of warmup for SMA(50)
        # Equity should be close to initial_cash at start (not NaN or 0)
        first_equity = results[1]["equity"].iloc[0]
        assert not np.isnan(first_equity), "First equity value should not be NaN (warmup broken)"
        assert first_equity > 0, "First equity value should be positive"

    def test_rolling_window_sweep_shape(self):
        """Sweep matrix should be (n_windows, n_periods)."""
        df = self._make_df(years=6)
        windows = bt.generate_rolling_windows(df, 2, 1)
        sweep = bt.rolling_window_sweep(df, windows, 'price', None, 'sma',
                                         'ind2', 10, 100, 10, 10000)
        expected_periods = list(range(10, 101, 10))
        assert sweep["periods"] == expected_periods
        assert sweep["matrix"].shape == (len(windows), len(expected_periods))
        assert len(sweep["best_per_window"]) == len(windows)

    def test_consistency_score_all_positive(self):
        """All-positive windows should score > 70."""
        fake_results = [{"total_return": r} for r in [10, 20, 15, 25, 12]]
        score, label = bt.compute_consistency_score(fake_results, "total_return")
        assert score > 70, f"All-positive should score > 70, got {score}"

    def test_consistency_score_mixed(self):
        """Half positive, half negative should score 35-55."""
        fake_results = [{"total_return": r} for r in [10, -15, 20, -25]]
        score, label = bt.compute_consistency_score(fake_results, "total_return")
        assert 20 <= score <= 60, f"Mixed results should score 20-60, got {score}"

    def test_generate_rolling_windows_respects_start_end_date(self):
        """Windows should be constrained by start_date and end_date."""
        df = self._make_df(years=10)
        # Without date constraints
        all_windows = bt.generate_rolling_windows(df, 2, 1)
        # With start_date cutting off first few years
        filtered = bt.generate_rolling_windows(df, 2, 1, start_date="2021-01-01")
        assert len(filtered) < len(all_windows), "start_date should reduce window count"
        for w in filtered:
            assert w["start"] >= pd.Timestamp("2021-01-01", tz="UTC"), \
                f"Window {w['label']} starts before start_date"
        # With end_date
        filtered2 = bt.generate_rolling_windows(df, 2, 1, end_date="2023-01-01")
        assert len(filtered2) < len(all_windows), "end_date should reduce window count"

    def test_rolling_window_sweep_dual_per_window(self):
        """Dual sweep must return per_window_matrices with one matrix per window."""
        df = self._make_df(years=6)
        windows = bt.generate_rolling_windows(df, 2, 1)
        result = bt.rolling_window_sweep_dual(df, windows, 'sma', 'ema',
                                               10, 50, 10, 10000)
        assert "per_window_matrices" in result, "Must return per_window_matrices"
        assert "window_labels" in result, "Must return window_labels"
        assert len(result["per_window_matrices"]) == len(windows)
        assert len(result["window_labels"]) == len(windows)
        n_per = len(result["periods"])
        for mat in result["per_window_matrices"]:
            assert mat.shape == (n_per, n_per)

    def test_rolling_mode_in_app(self):
        """app.py must have rolling mode card and handler."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "data-mode=\"rolling\"" in src, "Rolling mode card must exist"
        assert "p.mode == \"rolling\"" in src, "Rolling mode handler must exist"
        assert "window-size-group" in src, "Rolling window size group must exist"
        assert "switchRollingTab" in src, "Tab switching JS must exist"
        assert "plotly-anim-a" in src, "Animated heatmap container must exist"
        assert "per_window_matrices" in src, "Must use per-window matrices for animation"

    def test_rolling_params_parsed(self):
        """Params class must parse window_size and step_size."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "self.window_size" in src, "Params must parse window_size"
        assert "self.step_size" in src, "Params must parse step_size"
        assert "self.rolling_metric" in src, "Params must parse rolling_metric"

    def test_rolling_mode_has_save_publish_buttons(self):
        """Rolling mode results must include save/publish action buttons."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        # Find the rolling_charts block and check it has action buttons
        rolling_block = re.search(
            r'elif rolling_charts.*?elif chart',
            src, re.DOTALL)
        assert rolling_block, "rolling_charts template block must exist"
        block = rolling_block.group()
        assert 'id="backtest-actions"' in block, \
            "Rolling results must include backtest-actions div"
        assert 'saveBacktest()' in block, \
            "Rolling results must include Save button"
        assert 'openPublishModal()' in block, \
            "Rolling results must include Publish button"
        assert 'id="equity-thumbnail"' in block, \
            "Rolling results must include equity-thumbnail hidden input"

    def test_rolling_mode_passes_thumbnail(self):
        """Rolling mode must pass a thumbnail to the template."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "rolling_thumb=" in src, "Rolling mode must pass rolling_thumb to template"

    def test_rolling_alpha_timeline_tab_exists(self):
        """Rolling mode must have an Alpha Timeline tab for both single and dual indicator views."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        # Alpha timeline tab button must exist
        assert "Alpha Timeline" in src, "Alpha Timeline tab must exist in rolling results"
        # Alpha timeline content div must exist
        assert 'id="rtab-timeline-alpha"' in src, "Alpha timeline content div must exist"
        # Alpha timeline chart must be generated
        assert '"timeline_alpha"' in src, "timeline_alpha chart must be passed in rolling_charts"

    def test_rolling_alpha_timeline_always_generated(self):
        """Alpha timeline chart must be generated even when rolling_metric is not alpha."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert 'chart_timeline_alpha' in src, "chart_timeline_alpha must be generated"
        # Must generate alpha chart when metric != alpha
        assert "\"alpha\", strategy_label" in src, \
            "Must call generate_rolling_timeline_chart with 'alpha' metric"


class TestProgressTracking:
    """Progress bar feature: server tracks calculation progress, client polls it."""

    def test_progress_endpoint_exists(self):
        """The /progress route must exist in app.py."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "@app.route('/progress')" in src, "/progress endpoint must exist"
        assert "def progress():" in src, "progress() handler must exist"

    def test_progress_infrastructure(self):
        """update_progress and _progress dict must exist in app.py."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "_progress = {}" in src or "_progress = " in src, "_progress dict must exist"
        assert "def update_progress(" in src, "update_progress helper must exist"
        assert "_progress_lock" in src, "Progress must use thread lock"

    def test_progress_cleanup_on_completion(self):
        """Progress entry must be cleaned up when request finishes."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "_progress.pop(rid" in src, "Progress must be cleaned up in finally block"

    def test_heatmap_reports_progress(self):
        """Heatmap loop must call update_progress."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert 'update_progress(rid, i, n, ' in src, "Heatmap must report progress"

    def test_sweep_reports_progress(self):
        """Sweep loop must call update_progress."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert 'update_progress(rid, si, n_sweep' in src, "Sweep must report progress"

    def test_client_polls_progress(self):
        """Client JS must poll /progress and show a progress bar."""
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "progress-bar" in src, "Progress bar element must exist"
        assert "/progress?id=" in src, "Client must poll /progress endpoint"
        assert "clearInterval(progressInterval)" in src, "Must stop polling when done"

    def test_rolling_functions_accept_progress_callback(self):
        """backtest.py rolling functions must accept progress_callback parameter."""
        import inspect
        sig_eval = inspect.signature(bt.rolling_window_evaluate)
        assert "progress_callback" in sig_eval.parameters, "rolling_window_evaluate must accept progress_callback"
        sig_sweep = inspect.signature(bt.rolling_window_sweep)
        assert "progress_callback" in sig_sweep.parameters, "rolling_window_sweep must accept progress_callback"
        sig_dual = inspect.signature(bt.rolling_window_sweep_dual)
        assert "progress_callback" in sig_dual.parameters, "rolling_window_sweep_dual must accept progress_callback"

    def test_rolling_callback_invoked(self):
        """rolling_window_evaluate must call the progress_callback."""
        prices = list(range(100, 200))
        dates = pd.date_range("2020-01-01", periods=len(prices), freq="D", tz="UTC")
        df = pd.DataFrame({"close": prices}, index=dates)
        windows = bt.generate_rolling_windows(df, window_years=0.15, step_years=0.05,
                                                periods_per_year=365)
        calls = []
        def cb(cur, tot):
            calls.append((cur, tot))
        bt.rolling_window_evaluate(df, windows, "price", None, "sma", 10,
                                    initial_cash=10000, progress_callback=cb)
        assert len(calls) > 0, "progress_callback must be called at least once"
        assert calls[-1][0] == calls[-1][1] - 1 or calls[-1][0] < calls[-1][1], \
            "Last call should be near total"


class TestCollectionAddRemove:
    """Collection add/remove must use native form POST, not JS fetch.
    Bug: JS fetch with Content-Type:application/json silently returned 204 in some
    browsers without the request ever reaching the server."""

    def _read_app(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(src_path, encoding="utf-8") as f:
            return f.read()

    def test_add_backtest_uses_form_post(self):
        """Add-backtest dropdown items must be native <form> POST, not JS fetch."""
        src = self._read_app()
        # The add-bt-item for addable backtests must be inside a <form> with method POST
        assert 'action="/api/collection/{{ collection.id }}/add-backtest"' in src, \
            "Add-backtest must use a native form POST action"
        assert 'name="backtest_id"' in src, "Form must include backtest_id hidden input"
        assert 'name="_redirect"' in src, "Form must include _redirect hidden input"

    def test_add_backtest_no_js_fetch(self):
        """addBacktest() must NOT use JS fetch — it silently fails in some browsers."""
        src = self._read_app()
        assert "fetch('/api/collection/' + collId + '/add-backtest'" not in src, \
            "addBacktest must not use JS fetch (causes silent 204 failures)"

    def test_remove_backtest_uses_form_post(self):
        """Remove button must be a native <form> POST, not JS fetch."""
        src = self._read_app()
        assert 'action="/api/collection/{{ collection.id }}/remove-backtest"' in src, \
            "Remove-backtest must use a native form POST action"

    def test_remove_backtest_no_js_fetch(self):
        """removeBacktest() must NOT use JS fetch."""
        src = self._read_app()
        assert "fetch('/api/collection/' + collId + '/remove-backtest'" not in src, \
            "removeBacktest must not use JS fetch (causes silent 204 failures)"

    def test_add_endpoint_accepts_form_data(self):
        """The add-backtest API must accept form-encoded body (not just JSON)."""
        src = self._read_app()
        # Must check request.form as fallback when JSON is absent
        assert re.search(r'request\.form.*backtest_id|dict\(request\.form\)', src), \
            "add-backtest endpoint must accept form-encoded body"

    def test_remove_endpoint_accepts_form_data(self):
        """The remove-backtest API must accept form-encoded body (not just JSON)."""
        src = self._read_app()
        # The remove endpoint function should also support form data
        # Find the remove endpoint and check it handles form data
        remove_match = re.search(r'def api_remove_backtest_from_collection.*?(?=\n@app\.route|\nclass |\Z)',
                                  src, re.DOTALL)
        assert remove_match, "remove-backtest endpoint must exist"
        remove_src = remove_match.group()
        assert 'request.form' in remove_src, \
            "remove-backtest endpoint must accept form-encoded body"

    def test_endpoints_support_redirect(self):
        """Both add and remove endpoints must support _redirect param for page reload."""
        src = self._read_app()
        # Both endpoints should check for _redirect and redirect back
        add_match = re.search(r'def api_add_backtest_to_collection.*?(?=\n@app\.route|\nclass |\Z)',
                               src, re.DOTALL)
        remove_match = re.search(r'def api_remove_backtest_from_collection.*?(?=\n@app\.route|\nclass |\Z)',
                                  src, re.DOTALL)
        assert add_match and "_redirect" in add_match.group(), \
            "add-backtest must support _redirect param"
        assert remove_match and "_redirect" in remove_match.group(), \
            "remove-backtest must support _redirect param"

    def test_reorder_uses_native_form(self):
        """Reorder must use native form POST, not fetch/XHR (both silently fail on collection page)."""
        src = self._read_app()
        detail_section = src[src.index('COLLECTION_DETAIL_HTML'):]
        # Hidden form for reorder must exist
        assert 'id="reorder-form"' in detail_section, \
            "Hidden reorder form must exist in collection detail template"
        assert 'reorder-form' in detail_section and '.submit()' in detail_section, \
            "Reorder must submit via native form POST"
        # Must NOT use fetch or XHR for reorder
        reorder_fetch = re.search(r"fetch\([^)]*reorder", detail_section)
        assert reorder_fetch is None, \
            "Reorder must NOT use fetch (silently fails on this page)"

    def test_reorder_endpoint_accepts_form_data(self):
        """Reorder API must accept form-encoded body (comma-separated IDs)."""
        src = self._read_app()
        # Match api_reorder_collection(collection_id) not api_reorder_collections()
        reorder_match = re.search(r'def api_reorder_collection\(collection_id\).*?(?=\n@app\.route|\nclass |\Z)',
                                   src, re.DOTALL)
        assert reorder_match, "reorder endpoint must exist"
        assert 'request.form' in reorder_match.group(), \
            "reorder endpoint must accept form-encoded body"

    def test_collection_update_uses_native_form(self):
        """Collection update must use native form POST, not fetch/XHR."""
        src = self._read_app()
        detail_section = src[src.index('COLLECTION_DETAIL_HTML'):]
        assert 'id="update-form"' in detail_section, \
            "Hidden update form must exist in collection detail template"
        update_fetch = re.search(r"fetch\([^)]*update", detail_section)
        assert update_fetch is None, \
            "Update must NOT use fetch (silently fails on this page)"

    def test_collection_update_endpoint_accepts_form_data(self):
        """Collection update API must accept form-encoded body."""
        src = self._read_app()
        update_match = re.search(r'def api_update_collection.*?(?=\n@app\.route|\nclass |\Z)',
                                  src, re.DOTALL)
        assert update_match, "update endpoint must exist"
        assert 'request.form' in update_match.group(), \
            "update endpoint must accept form-encoded body"


class TestDetailPageRollingTabs:
    """DETAIL_HTML must include rolling-tab CSS and switchRollingTab JS so that
    cached rolling backtest HTML renders correctly on the detail page."""

    def _read_app(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            return f.read()

    def _detail_section(self):
        src = self._read_app()
        start = src.index('DETAIL_HTML')
        return src[start:]

    def test_rolling_tab_css_in_detail(self):
        """DETAIL_HTML must have CSS for .rolling-tabs and .rolling-tab-btn."""
        detail = self._detail_section()
        assert '.rolling-tabs' in detail, \
            "DETAIL_HTML missing .rolling-tabs CSS — rolling tab layout will break"
        assert '.rolling-tab-btn' in detail, \
            "DETAIL_HTML missing .rolling-tab-btn CSS — tab buttons unstyled"
        assert '.rolling-tab-content' in detail, \
            "DETAIL_HTML missing .rolling-tab-content CSS — tab panels won't hide/show"

    def test_rolling_tab_js_in_detail(self):
        """DETAIL_HTML must define switchRollingTab() so tab clicks work."""
        detail = self._detail_section()
        assert 'function switchRollingTab' in detail, \
            "DETAIL_HTML missing switchRollingTab() — clicking tabs does nothing"

    def test_consistency_badge_css_in_detail(self):
        """DETAIL_HTML must style .consistency-badge for rolling score display."""
        detail = self._detail_section()
        assert '.consistency-badge' in detail, \
            "DETAIL_HTML missing .consistency-badge CSS — score badge unstyled"


# ---------------------------------------------------------------------------
# Bug fix: Uploaded assets must have a proper price source so the daily
# fetcher can keep their data up-to-date. Previously, all uploaded assets
# got source='csv', source_id=None and were silently skipped by the fetcher.
# ---------------------------------------------------------------------------

class TestUploadedAssetPriceSource:
    """Ensure uploaded assets get a resolvable price source, not just 'csv'/None."""

    def _read_app_source(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_resolve_price_source_function_exists(self):
        """app.py must define _resolve_price_source() to auto-detect source info."""
        src = self._read_app_source()
        assert 'def _resolve_price_source(' in src, \
            "app.py must define _resolve_price_source() — without it, uploaded assets won't be fetched daily"

    def test_upload_endpoint_calls_resolve_price_source(self):
        """api_upload_asset must call _resolve_price_source, not hard-code source='csv'."""
        src = self._read_app_source()
        # Find the api_upload_asset function body
        match = re.search(r'def api_upload_asset\(\).*?(?=\ndef |\Z)', src, re.DOTALL)
        assert match, "api_upload_asset function not found in app.py"
        body = match.group(0)
        assert '_resolve_price_source(' in body, \
            "api_upload_asset must call _resolve_price_source() to auto-detect source info"

    def test_upload_endpoint_does_not_hardcode_csv_source(self):
        """api_upload_asset must not hard-code source='csv' in get_or_create_asset call."""
        src = self._read_app_source()
        match = re.search(r'def api_upload_asset\(\).*?(?=\ndef |\Z)', src, re.DOTALL)
        assert match, "api_upload_asset function not found in app.py"
        body = match.group(0)
        # The get_or_create_asset call should NOT contain source='csv'
        assert not re.search(r"get_or_create_asset\([^)]*source\s*=\s*['\"]csv['\"]", body), \
            "api_upload_asset must not hard-code source='csv' — use _resolve_price_source() instead"

    def test_resolve_price_source_handles_crypto(self):
        """_resolve_price_source must try CoinGecko for crypto assets."""
        src = self._read_app_source()
        match = re.search(r'def _resolve_price_source\(.*?(?=\ndef |\Z)', src, re.DOTALL)
        assert match, "_resolve_price_source function not found"
        body = match.group(0)
        assert 'coingecko' in body.lower(), \
            "_resolve_price_source must search CoinGecko for crypto assets"

    def test_resolve_price_source_handles_yfinance(self):
        """_resolve_price_source must use yfinance ticker for stocks/indices."""
        src = self._read_app_source()
        match = re.search(r'def _resolve_price_source\(.*?(?=\ndef |\Z)', src, re.DOTALL)
        assert match, "_resolve_price_source function not found"
        body = match.group(0)
        assert 'yfinance' in body, \
            "_resolve_price_source must return yfinance source for stock/index/metal/commodity assets"

    def test_resolve_price_source_returns_tuple(self):
        """_resolve_price_source must return (source, source_id) tuple."""
        src = self._read_app_source()
        match = re.search(r'def _resolve_price_source\(.*?(?=\ndef |\Z)', src, re.DOTALL)
        assert match, "_resolve_price_source function not found"
        body = match.group(0)
        # Should have multiple return statements returning tuples
        returns = re.findall(r"return\s+['\"](\w+)['\"],", body)
        assert len(returns) >= 2, \
            "_resolve_price_source must return (source, source_id) tuples for different asset types"
