"""Tests for per-user email signal alerts."""

import sys
import os
import re
import uuid
import sqlite3
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database as db


def _setup_test_db():
    """Create a fresh in-memory test database using a named shared cache
    so it persists across multiple _get_conn() calls."""
    import random
    db_name = f"test_{random.randint(0, 999999)}"
    uri = f"file:{db_name}?mode=memory&cache=shared"
    original_get_conn = db._get_conn

    def _test_get_conn():
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    db._get_conn = _test_get_conn
    # Keep a reference to prevent the shared memory DB from being garbage collected
    _anchor = sqlite3.connect(uri, uri=True)
    db.init_db()
    conn = _test_get_conn()
    return conn, original_get_conn, _anchor


ADMIN_EMAIL = "kuschnik.gerhard@gmail.com"


def _create_test_user(conn, user_id=None, email=ADMIN_EMAIL):
    """Insert a test user. Uses admin email by default to avoid accidental sends."""
    uid = user_id or str(uuid.uuid4())
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, email, display_name) VALUES (?, ?, ?)",
        (uid, email, "Test User")
    )
    conn.commit()
    return uid


def _create_test_backtest(conn, user_id, visibility="community", title="Test BT"):
    """Insert a test backtest."""
    bt_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO backtests (id, short_code, user_id, user_email, title,
           params, query_string, visibility, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (bt_id, bt_id[:6], user_id, ADMIN_EMAIL,
         title, json.dumps({"asset": "bitcoin"}), "asset=bitcoin", visibility)
    )
    conn.commit()
    return bt_id


class TestEmailAlertSchema:
    """Verify the email_alerts table exists and has correct structure."""

    def test_table_exists(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='email_alerts'"
            ).fetchone()
            assert row is not None, "email_alerts table should exist after init_db()"
        finally:
            db._get_conn = restore

    def test_columns(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            rows = conn.execute("PRAGMA table_info(email_alerts)").fetchall()
            col_names = {r['name'] for r in rows}
            expected = {'id', 'user_id', 'backtest_id', 'unsubscribe_token', 'is_active', 'created_at'}
            assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"
        finally:
            db._get_conn = restore

    def test_unique_constraint(self):
        """Same user + backtest should not create duplicate rows."""
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt_id = _create_test_backtest(conn, uid)
            db.create_email_alert(uid, bt_id)
            # Second create should reactivate, not fail
            db.create_email_alert(uid, bt_id)
            count = conn.execute(
                "SELECT COUNT(*) FROM email_alerts WHERE user_id=? AND backtest_id=?",
                (uid, bt_id)
            ).fetchone()[0]
            assert count == 1, "Should not create duplicate alerts"
        finally:
            db._get_conn = restore


class TestEmailAlertCRUD:
    """Test create, read, delete, deactivate operations."""

    def test_create_and_get(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt_id = _create_test_backtest(conn, uid)
            alert = db.create_email_alert(uid, bt_id)
            assert alert is not None
            assert alert['user_id'] == uid
            assert alert['backtest_id'] == bt_id
            assert alert['is_active'] == 1
            assert alert['unsubscribe_token'] is not None

            # get_email_alert should find it
            found = db.get_email_alert(uid, bt_id)
            assert found is not None
            assert found['id'] == alert['id']
        finally:
            db._get_conn = restore

    def test_delete(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt_id = _create_test_backtest(conn, uid)
            db.create_email_alert(uid, bt_id)
            db.delete_email_alert(uid, bt_id)
            assert db.get_email_alert(uid, bt_id) is None
        finally:
            db._get_conn = restore

    def test_deactivate_by_token(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt_id = _create_test_backtest(conn, uid)
            alert = db.create_email_alert(uid, bt_id)
            token = alert['unsubscribe_token']

            result = db.deactivate_email_alert_by_token(token)
            assert result is True
            # get_email_alert should not find it (is_active=0)
            assert db.get_email_alert(uid, bt_id) is None
            # But the row still exists
            row = conn.execute("SELECT is_active FROM email_alerts WHERE unsubscribe_token=?", (token,)).fetchone()
            assert row['is_active'] == 0
        finally:
            db._get_conn = restore

    def test_invalid_token_deactivate(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            result = db.deactivate_email_alert_by_token("nonexistent-token")
            assert result is False
        finally:
            db._get_conn = restore

    def test_get_by_token(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt_id = _create_test_backtest(conn, uid, title="My Strategy")
            alert = db.create_email_alert(uid, bt_id)
            found = db.get_email_alert_by_token(alert['unsubscribe_token'])
            assert found is not None
            assert found['backtest_title'] == "My Strategy"
        finally:
            db._get_conn = restore

    def test_reactivate_after_deactivate(self):
        """Creating an alert after unsubscribe should reactivate it."""
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt_id = _create_test_backtest(conn, uid)
            alert = db.create_email_alert(uid, bt_id)
            db.deactivate_email_alert_by_token(alert['unsubscribe_token'])
            assert db.get_email_alert(uid, bt_id) is None
            # Re-create should reactivate
            alert2 = db.create_email_alert(uid, bt_id)
            assert alert2['is_active'] == 1
            assert db.get_email_alert(uid, bt_id) is not None
        finally:
            db._get_conn = restore


class TestEmailAlertLimit:
    """Test the per-user alert limit."""

    def test_limit_enforced(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            # Create max alerts
            for i in range(db.MAX_EMAIL_ALERTS_PER_USER):
                bt_id = _create_test_backtest(conn, uid, title=f"BT {i}")
                db.create_email_alert(uid, bt_id)

            # One more should fail
            extra_bt = _create_test_backtest(conn, uid, title="BT extra")
            try:
                db.create_email_alert(uid, extra_bt)
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "Maximum" in str(e)
        finally:
            db._get_conn = restore

    def test_count(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt1 = _create_test_backtest(conn, uid, title="BT1")
            bt2 = _create_test_backtest(conn, uid, title="BT2")
            db.create_email_alert(uid, bt1)
            db.create_email_alert(uid, bt2)
            assert db.count_user_email_alerts(uid) == 2
        finally:
            db._get_conn = restore


class TestEmailAlertListing:
    """Test listing and grouping functions."""

    def test_list_user_alerts(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt1 = _create_test_backtest(conn, uid, title="Strategy A")
            bt2 = _create_test_backtest(conn, uid, title="Strategy B")
            db.create_email_alert(uid, bt1)
            db.create_email_alert(uid, bt2)
            alerts = db.list_user_email_alerts(uid)
            assert len(alerts) == 2
            titles = {a['title'] for a in alerts}
            assert 'Strategy A' in titles
            assert 'Strategy B' in titles
        finally:
            db._get_conn = restore

    def test_get_alerted_backtest_ids(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid = _create_test_user(conn)
            bt1 = _create_test_backtest(conn, uid, title="A")
            bt2 = _create_test_backtest(conn, uid, title="B")
            bt3 = _create_test_backtest(conn, uid, title="C")
            db.create_email_alert(uid, bt1)
            db.create_email_alert(uid, bt3)
            result = db.get_user_alerted_backtest_ids(uid, [bt1, bt2, bt3])
            assert result == {bt1, bt3}
        finally:
            db._get_conn = restore

    def test_grouped_alerts_join(self):
        conn, restore, _anchor = _setup_test_db()
        try:
            uid1 = _create_test_user(conn, email=ADMIN_EMAIL)
            uid2 = _create_test_user(conn, email=ADMIN_EMAIL)  # same admin email for safety
            bt_id = _create_test_backtest(conn, uid1, title="Shared Strategy")
            db.create_email_alert(uid1, bt_id)
            db.create_email_alert(uid2, bt_id)
            grouped = db.list_active_email_alerts_grouped()
            user_ids = {a['user_id'] for a in grouped if a['backtest_id'] == bt_id}
            assert uid1 in user_ids
            assert uid2 in user_ids
            # All emails should be admin email
            for a in grouped:
                assert a['user_email'] == ADMIN_EMAIL
        finally:
            db._get_conn = restore


class TestEmailAlertRoutes:
    """Source-code invariant tests: verify routes and JS exist in app.py."""

    def _read_app(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_email_alert_api_route_exists(self):
        src = self._read_app()
        assert "/api/backtest/<bt_id>/email-alert" in src, "Email alert toggle API route should exist"

    def test_unsubscribe_route_exists(self):
        src = self._read_app()
        assert "/unsubscribe/<token>" in src, "Unsubscribe route should exist"

    def test_my_email_alerts_api_exists(self):
        src = self._read_app()
        assert "/api/my-email-alerts" in src, "My email alerts listing API should exist"

    def test_toggle_email_alert_js_exists(self):
        src = self._read_app()
        assert "toggleEmailAlert" in src, "toggleEmailAlert JS function should exist in detail template"

    def test_unsubscribe_html_template_exists(self):
        src = self._read_app()
        assert "UNSUBSCRIBE_HTML" in src, "UNSUBSCRIBE_HTML template should exist"

    def test_email_alert_button_in_detail(self):
        src = self._read_app()
        assert "has_email_alert" in src, "Detail template should reference has_email_alert variable"


class TestEmailAlertAccessControl:
    """Verify access control logic exists for email alerts."""

    def _read_app(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_visibility_check_in_toggle(self):
        """The email alert toggle should check backtest visibility."""
        src = self._read_app()
        # Find the api_toggle_email_alert function
        match = re.search(r'def api_toggle_email_alert.*?(?=\n@app\.route|\nclass |\Z)', src, re.DOTALL)
        assert match, "api_toggle_email_alert function should exist"
        func_src = match.group()
        assert 'community' in func_src and 'featured' in func_src, \
            "Should check for community/featured visibility"
        assert '403' in func_src, "Should return 403 for unauthorized access"
