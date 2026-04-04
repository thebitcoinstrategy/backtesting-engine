"""Pre-refactor tests: verify existing behavior before extracting shared code.

These tests lock down the current behavior of:
1. Ratio price computation (4 duplicated blocks)
2. render_template_string parameter passing
3. JavaScript function presence in each inline template
4. Nav/chart/social JS duplication across templates

Run BEFORE and AFTER refactoring to ensure nothing breaks.
"""

import pandas as pd
import numpy as np
import sys
import os
import re

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest as bt

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_file(filename):
    path = os.path.join(PROJECT_ROOT, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _app_source():
    return _read_file("app.py")


# ---------------------------------------------------------------------------
# Template boundary helpers — find where each template starts and ends
# ---------------------------------------------------------------------------

def _template_boundaries(src):
    """Return dict mapping template name to (start_line, end_line) in app.py source."""
    templates = {}
    lines = src.split('\n')
    # Template variable assignments
    template_starts = {}
    for i, line in enumerate(lines):
        m = re.match(r'^(HTML|COMMUNITY_HTML|DETAIL_HTML|MY_BACKTESTS_HTML|ADMIN_ASSETS_HTML|COLLECTION_DETAIL_HTML|ACCOUNT_HTML|FEEDBACK_HTML|_ERROR_HTML|NAV_HTML)\s*=\s*[r"\']{1,3}', line)
        if m:
            template_starts[m.group(1)] = i
    # Find end of each template (next template start or end of file)
    sorted_names = sorted(template_starts.keys(), key=lambda n: template_starts[n])
    for idx, name in enumerate(sorted_names):
        start = template_starts[name]
        if idx + 1 < len(sorted_names):
            end = template_starts[sorted_names[idx + 1]]
        else:
            end = len(lines)
        templates[name] = (start, end)
    return templates


def _get_template_source(src, template_name):
    """Extract the source code of a specific template from app.py."""
    bounds = _template_boundaries(src)
    if template_name not in bounds:
        return ""
    start, end = bounds[template_name]
    lines = src.split('\n')
    return '\n'.join(lines[start:end])


# ===========================================================================
# 1. RATIO PRICE COMPUTATION TESTS
# ===========================================================================

class TestRatioComputation:
    """Verify the ratio computation logic that will be extracted to helpers.py.
    Tests the pure math: normalize, deduplicate, intersect, divide."""

    def _make_df(self, prices, start="2025-01-01"):
        dates = pd.date_range(start, periods=len(prices), freq="D", tz="UTC")
        return pd.DataFrame({"close": prices}, index=dates)

    def test_basic_ratio_division(self):
        """Ratio = numerator close / denominator close."""
        df1 = self._make_df([100, 200, 300])
        df2 = self._make_df([50, 100, 150])
        # Simulate the ratio computation pattern from app.py
        df1.index = df1.index.normalize()
        df2.index = df2.index.normalize()
        common = df1.index.intersection(df2.index)
        df1 = df1.loc[common]
        df1["close"] = df1["close"] / df2.loc[common, "close"]
        assert list(df1["close"]) == [2.0, 2.0, 2.0]

    def test_ratio_with_different_lengths(self):
        """Only overlapping dates should be used."""
        df1 = self._make_df([100, 200, 300, 400], start="2025-01-01")
        df2 = self._make_df([50, 100, 150], start="2025-01-02")
        df1.index = df1.index.normalize()
        df2.index = df2.index.normalize()
        df1 = df1[~df1.index.duplicated(keep='first')]
        df2 = df2[~df2.index.duplicated(keep='first')]
        common = df1.index.intersection(df2.index)
        assert len(common) == 3  # Jan 2, Jan 3, Jan 4 overlap
        df1 = df1.loc[common]
        df1["close"] = df1["close"] / df2.loc[common, "close"]
        assert len(df1) == 3

    def test_ratio_no_overlap_raises_or_empty(self):
        """Non-overlapping dates should produce empty intersection."""
        df1 = self._make_df([100, 200], start="2025-01-01")
        df2 = self._make_df([50, 100], start="2025-06-01")
        df1.index = df1.index.normalize()
        df2.index = df2.index.normalize()
        common = df1.index.intersection(df2.index)
        assert len(common) == 0

    def test_ratio_handles_duplicate_dates(self):
        """Duplicates from normalize() should be dropped (keep first)."""
        dates = pd.to_datetime(["2025-01-01 00:00", "2025-01-01 12:00", "2025-01-02 00:00"]).tz_localize("UTC")
        df1 = pd.DataFrame({"close": [100, 999, 200]}, index=dates)
        df2 = self._make_df([50, 100], start="2025-01-01")
        df1.index = df1.index.normalize()
        df2.index = df2.index.normalize()
        df1 = df1[~df1.index.duplicated(keep='first')]
        df2 = df2[~df2.index.duplicated(keep='first')]
        common = df1.index.intersection(df2.index)
        df1 = df1.loc[common]
        df1["close"] = df1["close"] / df2.loc[common, "close"]
        # First value (100) kept, 999 dropped as duplicate
        assert df1["close"].iloc[0] == 2.0  # 100/50

    def test_ratio_preserves_original(self):
        """Ratio computation should not modify the original dataframe."""
        df1 = self._make_df([100, 200])
        df2 = self._make_df([50, 100])
        original_close = df1["close"].copy()
        # Work on copies (as app.py does)
        df1_copy = df1.copy()
        df2_copy = df2.copy()
        df1_copy.index = df1_copy.index.normalize()
        df2_copy.index = df2_copy.index.normalize()
        common = df1_copy.index.intersection(df2_copy.index)
        df1_copy = df1_copy.loc[common]
        df1_copy["close"] = df1_copy["close"] / df2_copy.loc[common, "close"]
        # Original should be unchanged
        assert list(df1["close"]) == list(original_close)


# ===========================================================================
# 2. RATIO CODE PRESENCE IN ALL 4 FILES
# ===========================================================================

class TestRatioCodePresence:
    """Verify ratio computation exists in expected locations (pre-refactor)
    or is replaced by a helper call (post-refactor)."""

    def _has_ratio_pattern(self, src):
        """Check if source contains the inline ratio computation pattern."""
        return bool(re.search(r'\["close"\]\s*/\s*\w+\.loc\[', src))

    def _has_helper_call(self, src):
        """Check if source uses the extracted helper function."""
        return "compute_ratio_prices" in src

    def test_ratio_in_app_run_handler(self):
        """_run_post_handler must have ratio logic (inline or helper)."""
        src = _app_source()
        func = re.search(
            r'def _run_post_handler\(.*?\):(.*?)(?=\ndef \w+\()',
            src, re.DOTALL
        )
        assert func, "_run_post_handler not found"
        body = func.group(1)
        assert self._has_ratio_pattern(body) or self._has_helper_call(body), \
            "_run_post_handler must contain ratio computation (inline or via helper)"

    def test_ratio_in_app_backtest_detail(self):
        """backtest_detail must have ratio logic (inline or helper)."""
        src = _app_source()
        func = re.search(
            r'def backtest_detail\(bt_id\):(.*?)(?=\n@app\.route|\ndef \w+\()',
            src, re.DOTALL
        )
        assert func, "backtest_detail not found"
        body = func.group(1)
        assert self._has_ratio_pattern(body) or self._has_helper_call(body), \
            "backtest_detail must contain ratio computation (inline or via helper)"

    def test_ratio_in_fetch_prices(self):
        """fetch_prices.py signal logic must have ratio logic (in check_and_send_signals or its helper _compute_signal)."""
        src = _read_file("fetch_prices.py")
        # Check either check_and_send_signals or _compute_signal for the ratio pattern
        func = re.search(
            r'def (?:check_and_send_signals|_compute_signal)\(.*?\):(.*?)(?=\ndef \w+\(|\Z)',
            src, re.DOTALL
        )
        assert func, "check_and_send_signals or _compute_signal not found"
        body = func.group(1)
        assert self._has_ratio_pattern(body) or self._has_helper_call(body), \
            "signal logic in fetch_prices.py must contain ratio computation (inline or via helper)"

    def test_ratio_in_backtest_main(self):
        """backtest.py main must have ratio logic."""
        src = _read_file("backtest.py")
        # Look for the ratio pattern anywhere after args.vs
        assert self._has_ratio_pattern(src) or self._has_helper_call(src), \
            "backtest.py must contain ratio computation (inline or via helper)"


# ===========================================================================
# 3. RENDER_TEMPLATE_STRING PARAMETER TESTS
# ===========================================================================

class TestRenderParams:
    """Verify that render_template_string calls pass required asset metadata params.
    After refactoring, these params may come via _render_main() instead."""

    REQUIRED_PARAMS = [
        "asset_names", "priority_assets", "other_assets",
        "asset_starts_json", "asset_logos", "asset_tickers",
    ]

    def _find_render_calls_in_func(self, src, func_pattern):
        """Find render_template_string calls in a function body."""
        func = re.search(func_pattern, src, re.DOTALL)
        if not func:
            return []
        body = func.group(1)
        # Find all render_template_string calls
        return re.findall(r'render_template_string\([^)]+\)', body, re.DOTALL)

    def test_index_passes_asset_params(self):
        """index() render call must include all asset metadata."""
        src = _app_source()
        # The index function renders with HTML template
        func = re.search(r'def index\(\):(.*?)(?=\ndef \w+\()', src, re.DOTALL)
        assert func, "index() not found"
        body = func.group(1)
        # Either passes params directly or uses _render_main helper
        has_direct = all(p in body for p in self.REQUIRED_PARAMS)
        has_helper = "_render_main" in body
        assert has_direct or has_helper, \
            "index() must pass asset metadata (directly or via _render_main)"

    def test_run_handler_passes_asset_params(self):
        """_run_post_handler render calls must include asset metadata."""
        src = _app_source()
        func = re.search(
            r'def _run_post_handler\(.*?\):(.*?)(?=\ndef \w+\()',
            src, re.DOTALL
        )
        assert func, "_run_post_handler not found"
        body = func.group(1)
        has_direct = all(p in body for p in self.REQUIRED_PARAMS)
        has_helper = "_render_main" in body
        assert has_direct or has_helper, \
            "_run_post_handler must pass asset metadata (directly or via _render_main)"

    def test_run_handler_has_multiple_render_calls(self):
        """_run_post_handler has multiple render calls, all must have asset params."""
        src = _app_source()
        func = re.search(
            r'def _run_post_handler\(.*?\):(.*?)(?=\ndef \w+\()',
            src, re.DOTALL
        )
        assert func, "_run_post_handler not found"
        body = func.group(1)
        # Count either direct render_template_string or _render_main calls
        render_count = body.count("render_template_string") + body.count("_render_main")
        # Must have multiple render calls (various error/success paths)
        assert render_count >= 3, (
            f"_run_post_handler should have 3+ render calls, found {render_count}"
        )


# ===========================================================================
# 4. NAV JS FUNCTION TESTS — each template must have nav functions
# ===========================================================================

class TestNavJSPresence:
    """Verify that every template requiring nav has the nav JS functions.
    Pre-refactor: inline in each template.
    Post-refactor: via <script src="/static/js/nav.js"> reference."""

    NAV_FUNCTIONS = [
        "toggleTheme",
        "toggleNotifDropdown",
        "toggleAvatarDropdown",
        "fetchNotifications",
        "_escHtml",
    ]

    # Templates that MUST have nav functionality
    # (COLLECTION_DETAIL_HTML has no navbar so excluded)
    NAV_TEMPLATES = [
        "HTML", "COMMUNITY_HTML", "DETAIL_HTML",
        "MY_BACKTESTS_HTML", "ADMIN_ASSETS_HTML",
        "ACCOUNT_HTML", "FEEDBACK_HTML",
    ]

    def _template_has_nav(self, template_src):
        """Check if template has nav functions (inline or via static file)."""
        has_inline = all(f in template_src for f in self.NAV_FUNCTIONS)
        has_static = "nav.js" in template_src
        return has_inline or has_static

    def test_all_nav_templates_have_nav_functions(self):
        """Every template with a navbar must include nav JS functions."""
        src = _app_source()
        for tpl_name in self.NAV_TEMPLATES:
            tpl_src = _get_template_source(src, tpl_name)
            assert self._template_has_nav(tpl_src), (
                f"{tpl_name} must include nav JS functions "
                f"(inline or via nav.js static file)"
            )

    def test_swal_mixin_in_nav_templates(self):
        """Every template with nav must have _swal mixin."""
        src = _app_source()
        for tpl_name in self.NAV_TEMPLATES:
            tpl_src = _get_template_source(src, tpl_name)
            has_inline = "_swal" in tpl_src
            has_static = "nav.js" in tpl_src
            assert has_inline or has_static, (
                f"{tpl_name} must include _swal mixin (inline or via nav.js)"
            )


# ===========================================================================
# 5. CHART JS FUNCTION TESTS — backtester + detail must have chart functions
# ===========================================================================

class TestChartJSPresence:
    """Verify that templates with live charts have chart JS functions."""

    CHART_FUNCTIONS = [
        "loadLWChart",
        "fetchLivePrice",
        "startLivePolling",
        "switchChartTab",
    ]

    CHART_TEMPLATES = ["HTML", "DETAIL_HTML"]

    def _template_has_chart(self, template_src):
        has_inline = all(f in template_src for f in self.CHART_FUNCTIONS)
        has_static = "chart.js" in template_src
        return has_inline or has_static

    def test_chart_templates_have_chart_functions(self):
        """HTML and DETAIL_HTML must have chart JS functions."""
        src = _app_source()
        for tpl_name in self.CHART_TEMPLATES:
            tpl_src = _get_template_source(src, tpl_name)
            assert self._template_has_chart(tpl_src), (
                f"{tpl_name} must include chart JS functions "
                f"(inline or via chart.js static file)"
            )

    def test_chart_templates_have_ratio_support(self):
        """Chart templates must support vs_asset ratio mode."""
        src = _app_source()
        for tpl_name in self.CHART_TEMPLATES:
            tpl_src = _get_template_source(src, tpl_name)
            has_inline_ratio = "d1.price" in tpl_src and "d2.price" in tpl_src
            has_static = "chart.js" in tpl_src
            assert has_inline_ratio or has_static, (
                f"{tpl_name} must support ratio mode in fetchLivePrice "
                f"(inline d1.price/d2.price or via chart.js)"
            )

    def test_chart_templates_have_watermark(self):
        """Chart templates must include ticker watermark."""
        src = _app_source()
        for tpl_name in self.CHART_TEMPLATES:
            tpl_src = _get_template_source(src, tpl_name)
            has_inline = "watermark" in tpl_src.lower() or "wm.textContent" in tpl_src or "__lwAsset" in tpl_src
            has_static = "chart.js" in tpl_src
            assert has_inline or has_static, (
                f"{tpl_name} must include ticker watermark (inline or via chart.js)"
            )

    def test_lw_data_variables_in_chart_templates(self):
        """Chart templates must declare __lwData and __lwAsset variables."""
        src = _app_source()
        # These are always inline (data injection), even after refactoring
        tpl_src = _get_template_source(src, "HTML")
        assert "__lwData" in tpl_src, "HTML must declare __lwData"
        assert "__lwAsset" in tpl_src, "HTML must declare __lwAsset"


# ===========================================================================
# 6. SOCIAL JS FUNCTION TESTS
# ===========================================================================

class TestSocialJSPresence:
    """Verify social interaction functions exist where needed."""

    def test_toggleLike_in_interactive_templates(self):
        """toggleLike must exist in templates with like buttons."""
        src = _app_source()
        for tpl_name in ["HTML", "DETAIL_HTML"]:
            tpl_src = _get_template_source(src, tpl_name)
            has_inline = "toggleLike" in tpl_src
            has_static = "social.js" in tpl_src
            assert has_inline or has_static, (
                f"{tpl_name} must have toggleLike (inline or via social.js)"
            )

    def test_submitComment_in_interactive_templates(self):
        """submitComment must exist in templates with comment forms."""
        src = _app_source()
        for tpl_name in ["HTML", "DETAIL_HTML"]:
            tpl_src = _get_template_source(src, tpl_name)
            has_inline = "submitComment" in tpl_src
            has_static = "social.js" in tpl_src
            assert has_inline or has_static, (
                f"{tpl_name} must have submitComment (inline or via social.js)"
            )

    def test_deleteBacktest_in_management_templates(self):
        """deleteBacktest must exist in templates with delete buttons."""
        src = _app_source()
        for tpl_name in ["HTML", "COMMUNITY_HTML", "MY_BACKTESTS_HTML"]:
            tpl_src = _get_template_source(src, tpl_name)
            has_inline = "deleteBacktest" in tpl_src
            has_static = "social.js" in tpl_src
            assert has_inline or has_static, (
                f"{tpl_name} must have deleteBacktest (inline or via social.js)"
            )

    def test_featureBacktest_in_admin_templates(self):
        """featureBacktest must exist in templates with feature buttons."""
        src = _app_source()
        for tpl_name in ["HTML", "COMMUNITY_HTML", "DETAIL_HTML"]:
            tpl_src = _get_template_source(src, tpl_name)
            has_inline = "featureBacktest" in tpl_src
            has_static = "social.js" in tpl_src
            assert has_inline or has_static, (
                f"{tpl_name} must have featureBacktest (inline or via social.js)"
            )


# ===========================================================================
# 7. NAV_HTML INJECTION TESTS
# ===========================================================================

class TestNavHTMLInjection:
    """Verify NAV_HTML is injected into all templates that need it."""

    def test_nav_html_defined(self):
        """NAV_HTML must be defined as a Python string."""
        src = _app_source()
        assert re.search(r'^NAV_HTML\s*=\s*', src, re.MULTILINE), \
            "NAV_HTML must be defined in app.py"

    def test_nav_html_injected_in_templates(self):
        """NAV_HTML must be concatenated into templates that need navigation.
        After refactoring, this could be via Jinja include or static file."""
        src = _app_source()
        # Count how many templates use NAV_HTML (either via concatenation or include)
        nav_injections = src.count('NAV_HTML')
        # Must be defined once + injected into 7+ templates
        assert nav_injections >= 7, (
            f"NAV_HTML should be referenced 7+ times (1 definition + 6+ injections), "
            f"found {nav_injections}"
        )


# ===========================================================================
# 8. BACKTEST.PY RUN_STRATEGY TESTS (ensure core logic still works)
# ===========================================================================

class TestRunStrategyIntegrity:
    """Verify that backtest.run_strategy() produces consistent results.
    This is the core engine — must not break during refactoring."""

    def _make_df(self, prices):
        dates = pd.date_range("2025-01-01", periods=len(prices), freq="D", tz="UTC")
        return pd.DataFrame({"close": prices}, index=dates)

    def test_price_vs_sma_basic(self):
        """Price vs SMA(5) on rising data should produce positive returns."""
        prices = list(range(100, 130))  # 30 days of rising prices
        df = self._make_df(prices)
        result = bt.run_strategy(df, "price", None, "sma", 5, initial_cash=10000)
        assert "ind1_series" in result
        assert "ind2_series" in result
        assert "total_return" in result
        assert result["total_return"] > 0

    def test_run_strategy_returns_required_keys(self):
        """run_strategy must return all expected keys."""
        prices = list(range(100, 130))
        df = self._make_df(prices)
        result = bt.run_strategy(df, "price", None, "sma", 5, initial_cash=10000)
        required_keys = [
            "ind1_series", "ind2_series", "ind1_label", "ind2_label",
            "label", "total_return", "sharpe",
        ]
        for key in required_keys:
            assert key in result, f"run_strategy result missing key: {key}"

    def test_apply_exposure_long_cash(self):
        """_apply_exposure with long-cash: True→1, False→0."""
        above = pd.Series([True, False, True, False])
        pos = bt._apply_exposure(above, "long-cash")
        assert list(pos) == [1, 0, 1, 0]

    def test_apply_exposure_long_short(self):
        """_apply_exposure with long-short: True→1, False→-1."""
        above = pd.Series([True, False, True, False])
        pos = bt._apply_exposure(above, "long-short")
        assert list(pos) == [1, -1, 1, -1]

    def test_compute_indicator_sma(self):
        """compute_indicator_from_spec must return valid SMA series."""
        prices = list(range(100, 120))
        df = self._make_df(prices)
        series, label = bt.compute_indicator_from_spec(df, "sma", 5)
        assert len(series) == len(df)
        assert "SMA" in label
        # First 4 values should be NaN (period=5)
        assert series.iloc[:4].isna().all()
        assert not series.iloc[4:].isna().any()

    def test_compute_indicator_ema(self):
        """compute_indicator_from_spec must return valid EMA series."""
        prices = list(range(100, 120))
        df = self._make_df(prices)
        series, label = bt.compute_indicator_from_spec(df, "ema", 10)
        assert len(series) == len(df)
        assert "EMA" in label


# ===========================================================================
# 9. TEMPLATE STRUCTURE TESTS — each template must be valid HTML
# ===========================================================================

class TestTemplateStructure:
    """Basic structural tests for inline HTML templates."""

    TEMPLATES_WITH_DOCTYPE = [
        "HTML", "COMMUNITY_HTML", "DETAIL_HTML", "MY_BACKTESTS_HTML",
        "ADMIN_ASSETS_HTML", "ACCOUNT_HTML", "FEEDBACK_HTML",
    ]

    def test_templates_have_doctype(self):
        """All full-page templates must start with <!DOCTYPE html>."""
        src = _app_source()
        for tpl_name in self.TEMPLATES_WITH_DOCTYPE:
            tpl_src = _get_template_source(src, tpl_name)
            assert "<!DOCTYPE html>" in tpl_src or "<!doctype html>" in tpl_src.lower(), (
                f"{tpl_name} must contain <!DOCTYPE html>"
            )

    def test_templates_have_closing_html(self):
        """All templates must close their HTML tags."""
        src = _app_source()
        for tpl_name in self.TEMPLATES_WITH_DOCTYPE:
            tpl_src = _get_template_source(src, tpl_name)
            assert "</html>" in tpl_src, f"{tpl_name} must close </html> tag"

    def test_templates_have_script_tags(self):
        """Templates with JS must have <script> tags."""
        src = _app_source()
        for tpl_name in ["HTML", "DETAIL_HTML", "COMMUNITY_HTML"]:
            tpl_src = _get_template_source(src, tpl_name)
            assert "<script" in tpl_src, f"{tpl_name} must have <script> tags"


# ===========================================================================
# 10. STATIC FILE TESTS (for post-refactor verification)
# ===========================================================================

class TestStaticFiles:
    """After refactoring, static JS files should exist and be referenced.
    Pre-refactor: these tests check that either inline code exists OR
    static files exist — so they pass both before and after."""

    STATIC_DIR = os.path.join(PROJECT_ROOT, "static", "js")

    def _static_exists(self, filename):
        return os.path.isfile(os.path.join(self.STATIC_DIR, filename))

    def test_nav_js_inline_or_static(self):
        """Nav JS must be inline in templates OR extracted to nav.js."""
        src = _app_source()
        has_inline = "function toggleTheme()" in src
        has_static = self._static_exists("nav.js")
        assert has_inline or has_static, \
            "Nav JS must exist either inline in app.py or as static/js/nav.js"

    def test_chart_js_inline_or_static(self):
        """Chart JS must be inline in templates OR extracted to chart.js."""
        src = _app_source()
        has_inline = "function loadLWChart()" in src
        has_static = self._static_exists("chart.js")
        assert has_inline or has_static, \
            "Chart JS must exist either inline in app.py or as static/js/chart.js"

    def test_social_js_inline_or_static(self):
        """Social JS must be inline in templates OR extracted to social.js."""
        src = _app_source()
        has_inline = "function toggleLike(" in src
        has_static = self._static_exists("social.js")
        assert has_inline or has_static, \
            "Social JS must exist either inline in app.py or as static/js/social.js"


# ===========================================================================
# 11. HELPERS MODULE TESTS (for post-refactor verification)
# ===========================================================================

class TestHelpersModule:
    """After refactoring, helpers.py should exist with compute_ratio_prices.
    Pre-refactor: skip if helpers.py doesn't exist yet."""

    def test_helpers_module_or_inline(self):
        """Either helpers.py with compute_ratio_prices exists,
        or inline ratio code exists in app.py."""
        helpers_path = os.path.join(PROJECT_ROOT, "helpers.py")
        has_helpers = os.path.isfile(helpers_path)
        if has_helpers:
            src = _read_file("helpers.py")
            assert "def compute_ratio_prices" in src, \
                "helpers.py must define compute_ratio_prices()"
        else:
            # Pre-refactor: inline code must exist
            src = _app_source()
            assert re.search(r'\["close"\]\s*/\s*\w+\.loc\[', src), \
                "Inline ratio computation must exist in app.py (or extract to helpers.py)"
