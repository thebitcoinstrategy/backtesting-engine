"""SQLite database for saved/published backtests, likes, and comments."""

import os
import sqlite3
import uuid
import string
import random
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtests.db")
ADMIN_EMAIL = "kuschnik.gerhard@gmail.com"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS backtests (
            id TEXT PRIMARY KEY,
            short_code TEXT UNIQUE,
            user_id TEXT NOT NULL,
            user_email TEXT NOT NULL,
            title TEXT,
            description TEXT,
            params TEXT NOT NULL,
            query_string TEXT NOT NULL,
            cached_html TEXT,
            visibility TEXT NOT NULL DEFAULT 'private',
            likes_count INTEGER DEFAULT 0,
            comments_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS likes (
            user_id TEXT NOT NULL,
            backtest_id TEXT NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, backtest_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            backtest_id TEXT NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
            parent_id TEXT REFERENCES comments(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            user_email TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_backtests_visibility ON backtests(visibility);
        CREATE INDEX IF NOT EXISTS idx_backtests_user ON backtests(user_id);
        CREATE INDEX IF NOT EXISTS idx_backtests_short_code ON backtests(short_code);
        CREATE INDEX IF NOT EXISTS idx_comments_backtest ON comments(backtest_id);
        CREATE INDEX IF NOT EXISTS idx_likes_backtest ON likes(backtest_id);

        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            actor_id TEXT,
            actor_email TEXT,
            backtest_id TEXT REFERENCES backtests(id) ON DELETE CASCADE,
            comment_id TEXT REFERENCES comments(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            message TEXT,
            link TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read);
    """)
    # Add thumbnail column if missing (migration for existing DBs)
    try:
        conn.execute("ALTER TABLE backtests ADD COLUMN thumbnail TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Add sort_order column if missing (migration for existing DBs)
    try:
        conn.execute("ALTER TABLE backtests ADD COLUMN sort_order INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Add avatar and notification prefs to users table
    try:
        conn.execute("ALTER TABLE backtests ADD COLUMN views_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN notify_comments INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN notify_replies INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN welcomed INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Add message and link columns to notifications (for welcome notifications)
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN message TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN link TEXT")
    except sqlite3.OperationalError:
        pass
    conn.close()


def generate_short_code():
    """Generate a unique 6-char alphanumeric short code."""
    chars = string.ascii_lowercase + string.digits
    conn = _get_conn()
    for _ in range(100):
        code = ''.join(random.choices(chars, k=6))
        row = conn.execute("SELECT 1 FROM backtests WHERE short_code=?", (code,)).fetchone()
        if not row:
            conn.close()
            return code
    conn.close()
    # Fallback: use 8 chars
    return ''.join(random.choices(chars, k=8))


def _row_to_dict(row):
    """Convert sqlite3.Row to dict."""
    if row is None:
        return None
    return dict(row)


def get_display_name(user_id):
    """Get user's display name, or None if not set."""
    conn = _get_conn()
    row = conn.execute("SELECT display_name FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row['display_name'] if row else None


def set_display_name(user_id, email, display_name):
    """Set or update user's display name."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO users (user_id, email, display_name) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET display_name=?, email=?""",
        (user_id, email, display_name, display_name, email)
    )
    conn.commit()
    conn.close()


def save_backtest(user_id, email, params, query_string, cached_html, visibility='private', title=None, description=None, thumbnail=None):
    """Save a backtest. Returns the backtest dict."""
    conn = _get_conn()
    bt_id = str(uuid.uuid4())
    short_code = generate_short_code()
    now = datetime.utcnow().isoformat()
    # Place new backtests at the end of the sort order
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM backtests").fetchone()[0]
    new_order = max_order + 1
    conn.execute(
        """INSERT INTO backtests (id, short_code, user_id, user_email, title, description,
           params, query_string, cached_html, visibility, thumbnail, sort_order, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (bt_id, short_code, user_id, email, title, description,
         params, query_string, cached_html, visibility, thumbnail, new_order, now, now)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM backtests WHERE id=?", (bt_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_backtest(bt_id):
    """Get a backtest by ID."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM backtests WHERE id=?", (bt_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_backtest_by_short_code(code):
    """Get a backtest by short code."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM backtests WHERE short_code=?", (code,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def list_backtests(visibility=None, sort='newest', page=1, per_page=20):
    """List backtests filtered by visibility. Returns (list, total_count)."""
    conn = _get_conn()
    where = ""
    params = []
    if visibility:
        if isinstance(visibility, (list, tuple)):
            placeholders = ','.join('?' * len(visibility))
            where = f"WHERE visibility IN ({placeholders})"
            params = list(visibility)
        else:
            where = "WHERE visibility=?"
            params = [visibility]

    if sort == 'manual':
        order = "sort_order ASC, created_at DESC"
    elif sort == 'newest':
        order = "created_at DESC"
    else:
        order = "likes_count DESC, created_at DESC"
    offset = (page - 1) * per_page

    total = conn.execute(f"SELECT COUNT(*) FROM backtests {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM backtests {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows], total


def list_user_backtests(user_id):
    """List all backtests for a user."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM backtests WHERE user_id=? ORDER BY created_at DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def delete_backtest(bt_id, user_id):
    """Delete a backtest. Owner or admin only. Returns True if deleted."""
    conn = _get_conn()
    row = conn.execute("SELECT user_id, user_email FROM backtests WHERE id=?", (bt_id,)).fetchone()
    if not row:
        conn.close()
        return False
    # Check ownership or admin
    bt = _row_to_dict(row)
    # Compare as strings to avoid int/str type mismatch (JWT sends int, DB stores text)
    if str(bt['user_id']) != str(user_id):
        conn.close()
        return False
    conn.execute("DELETE FROM backtests WHERE id=?", (bt_id,))
    conn.commit()
    conn.close()
    return True


def delete_backtest_admin(bt_id):
    """Admin delete — no ownership check."""
    conn = _get_conn()
    conn.execute("DELETE FROM backtests WHERE id=?", (bt_id,))
    conn.commit()
    conn.close()
    return True


def reorder_backtests(ordered_ids):
    """Update sort_order for a list of backtest IDs. Index = order position."""
    conn = _get_conn()
    for i, bt_id in enumerate(ordered_ids):
        conn.execute("UPDATE backtests SET sort_order=? WHERE id=?", (i, bt_id))
    conn.commit()
    conn.close()
    return True


def update_visibility(bt_id, new_visibility):
    """Update backtest visibility. Returns True if updated."""
    conn = _get_conn()
    now = datetime.utcnow().isoformat()
    # Place at end of sort order when promoting to featured
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM backtests").fetchone()[0]
    new_order = max_order + 1
    conn.execute(
        "UPDATE backtests SET visibility=?, sort_order=?, updated_at=? WHERE id=?",
        (new_visibility, new_order, now, bt_id)
    )
    conn.commit()
    conn.close()
    return True


def update_backtest(bt_id, user_id, title=None, description=None):
    """Update title/description of a backtest. Owner only. Returns updated dict or None."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM backtests WHERE id=?", (bt_id,)).fetchone()
    if not row:
        conn.close()
        return None
    bt = _row_to_dict(row)
    if str(bt['user_id']) != str(user_id):
        conn.close()
        return None
    now = datetime.utcnow().isoformat()
    new_title = title if title is not None else bt['title']
    new_desc = description if description is not None else bt['description']
    conn.execute(
        "UPDATE backtests SET title=?, description=?, updated_at=? WHERE id=?",
        (new_title, new_desc, now, bt_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM backtests WHERE id=?", (bt_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def toggle_like(user_id, backtest_id):
    """Toggle like. Returns (new_likes_count, liked)."""
    conn = _get_conn()
    existing = conn.execute(
        "SELECT 1 FROM likes WHERE user_id=? AND backtest_id=?",
        (user_id, backtest_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM likes WHERE user_id=? AND backtest_id=?", (user_id, backtest_id))
        conn.execute("UPDATE backtests SET likes_count = MAX(0, likes_count - 1) WHERE id=?", (backtest_id,))
    else:
        conn.execute("INSERT INTO likes (user_id, backtest_id) VALUES (?, ?)", (user_id, backtest_id))
        conn.execute("UPDATE backtests SET likes_count = likes_count + 1 WHERE id=?", (backtest_id,))
    conn.commit()
    count = conn.execute("SELECT likes_count FROM backtests WHERE id=?", (backtest_id,)).fetchone()
    conn.close()
    liked = not bool(existing)
    return (count[0] if count else 0, liked)


def has_liked(user_id, backtest_id):
    """Check if user has liked a backtest."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM likes WHERE user_id=? AND backtest_id=?",
        (user_id, backtest_id)
    ).fetchone()
    conn.close()
    return bool(row)


def add_comment(backtest_id, user_id, email, body, parent_id=None):
    """Add a comment. Creates notifications for relevant users. Returns the comment dict."""
    conn = _get_conn()
    comment_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO comments (id, backtest_id, parent_id, user_id, user_email, body, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (comment_id, backtest_id, parent_id, user_id, email, body, now)
    )
    conn.execute("UPDATE backtests SET comments_count = comments_count + 1 WHERE id=?", (backtest_id,))

    # --- Create notifications (respecting user prefs) ---
    notified_user = None
    notify_email_targets = []  # list of (recipient_user_id, recipient_email, notif_type)
    # Case 1: Reply to a comment -> notify the parent comment's author
    if parent_id:
        parent = conn.execute("SELECT user_id, user_email FROM comments WHERE id=?", (parent_id,)).fetchone()
        if parent and str(parent['user_id']) != str(user_id):
            notified_user = str(parent['user_id'])
            # Check reply notification pref
            pref = conn.execute("SELECT notify_replies FROM users WHERE user_id=?", (notified_user,)).fetchone()
            should_notify = not pref or pref['notify_replies'] is None or pref['notify_replies'] == 1
            if should_notify:
                conn.execute(
                    """INSERT INTO notifications (id, user_id, actor_id, actor_email, backtest_id, comment_id, type, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'reply', ?)""",
                    (str(uuid.uuid4()), notified_user, user_id, email, backtest_id, comment_id, now)
                )
                notify_email_targets.append((notified_user, parent['user_email'], 'reply'))
    # Case 2: Comment on a backtest -> notify the backtest owner
    bt = conn.execute("SELECT user_id, user_email FROM backtests WHERE id=?", (backtest_id,)).fetchone()
    if bt and str(bt['user_id']) != str(user_id):
        bt_owner = str(bt['user_id'])
        if bt_owner != notified_user:  # avoid double notification
            pref = conn.execute("SELECT notify_comments FROM users WHERE user_id=?", (bt_owner,)).fetchone()
            should_notify = not pref or pref['notify_comments'] is None or pref['notify_comments'] == 1
            if should_notify:
                conn.execute(
                    """INSERT INTO notifications (id, user_id, actor_id, actor_email, backtest_id, comment_id, type, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'backtest_comment', ?)""",
                    (str(uuid.uuid4()), bt_owner, user_id, email, backtest_id, comment_id, now)
                )
                notify_email_targets.append((bt_owner, bt['user_email'], 'backtest_comment'))

    conn.commit()
    row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    conn.close()
    result = _row_to_dict(row)
    result['_email_targets'] = notify_email_targets
    return result


def get_comments(backtest_id):
    """Get threaded comments for a backtest. Returns list of top-level comments with 'replies' key."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM comments WHERE backtest_id=? ORDER BY created_at ASC",
        (backtest_id,)
    ).fetchall()
    conn.close()

    comments = [_row_to_dict(r) for r in rows]
    top_level = []
    by_id = {}
    for c in comments:
        c['replies'] = []
        by_id[c['id']] = c

    for c in comments:
        if c['parent_id'] and c['parent_id'] in by_id:
            by_id[c['parent_id']]['replies'].append(c)
        else:
            top_level.append(c)

    return top_level


def delete_comment(comment_id, user_id):
    """Delete a comment. Owner only. Returns True if deleted."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    if not row:
        conn.close()
        return False
    comment = _row_to_dict(row)
    if str(comment['user_id']) != str(user_id):
        conn.close()
        return False
    backtest_id = comment['backtest_id']
    # Count this comment + its replies
    reply_count = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE parent_id=?", (comment_id,)
    ).fetchone()[0]
    conn.execute("DELETE FROM comments WHERE id=? OR parent_id=?", (comment_id, comment_id))
    conn.execute(
        "UPDATE backtests SET comments_count = MAX(0, comments_count - ?) WHERE id=?",
        (1 + reply_count, backtest_id)
    )
    conn.commit()
    conn.close()
    return True


def delete_comment_admin(comment_id):
    """Admin delete comment — no ownership check."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    if not row:
        conn.close()
        return False
    comment = _row_to_dict(row)
    backtest_id = comment['backtest_id']
    reply_count = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE parent_id=?", (comment_id,)
    ).fetchone()[0]
    conn.execute("DELETE FROM comments WHERE id=? OR parent_id=?", (comment_id, comment_id))
    conn.execute(
        "UPDATE backtests SET comments_count = MAX(0, comments_count - ?) WHERE id=?",
        (1 + reply_count, backtest_id)
    )
    conn.commit()
    conn.close()
    return True


def get_unread_notifications(user_id, limit=20):
    """Get unread notifications for a user, with backtest title. Returns list of dicts."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT n.*, b.title as backtest_title
           FROM notifications n
           LEFT JOIN backtests b ON n.backtest_id = b.id
           WHERE n.user_id=? AND n.is_read=0
           ORDER BY n.created_at DESC LIMIT ?""",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_unread_count(user_id):
    """Get count of unread notifications for a user."""
    conn = _get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
        (user_id,)
    ).fetchone()[0]
    conn.close()
    return count


def mark_notifications_read(user_id):
    """Mark all notifications as read for a user."""
    conn = _get_conn()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (user_id,))
    conn.commit()
    conn.close()


def get_user_liked_ids(user_id, backtest_ids):
    """Get set of backtest IDs that user has liked from a list."""
    if not backtest_ids:
        return set()
    conn = _get_conn()
    placeholders = ','.join('?' * len(backtest_ids))
    rows = conn.execute(
        f"SELECT backtest_id FROM likes WHERE user_id=? AND backtest_id IN ({placeholders})",
        [user_id] + list(backtest_ids)
    ).fetchall()
    conn.close()
    return {r['backtest_id'] for r in rows}


# --- Avatar & profile ---

def get_user_avatar(user_id):
    """Get user's avatar filename, or None."""
    conn = _get_conn()
    row = conn.execute("SELECT avatar FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row['avatar'] if row else None


def set_user_avatar(user_id, filename):
    """Set user's avatar filename."""
    conn = _get_conn()
    conn.execute("UPDATE users SET avatar=? WHERE user_id=?", (filename, user_id))
    conn.commit()
    conn.close()


def remove_user_avatar(user_id):
    """Remove user's avatar."""
    conn = _get_conn()
    conn.execute("UPDATE users SET avatar=NULL WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_user_profiles(user_ids):
    """Batch fetch display_name and avatar for a set of user IDs.
    Returns {user_id: {'display_name': ..., 'avatar': ...}}."""
    if not user_ids:
        return {}
    conn = _get_conn()
    placeholders = ','.join('?' * len(user_ids))
    rows = conn.execute(
        f"SELECT user_id, display_name, avatar FROM users WHERE user_id IN ({placeholders})",
        list(user_ids)
    ).fetchall()
    conn.close()
    return {r['user_id']: {'display_name': r['display_name'], 'avatar': r['avatar']} for r in rows}


def get_notification_prefs(user_id):
    """Get notification preferences. Returns dict with notify_comments and notify_replies (default 1)."""
    conn = _get_conn()
    row = conn.execute("SELECT notify_comments, notify_replies FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if row:
        return {'notify_comments': row['notify_comments'] if row['notify_comments'] is not None else 1,
                'notify_replies': row['notify_replies'] if row['notify_replies'] is not None else 1}
    return {'notify_comments': 1, 'notify_replies': 1}


def set_notification_prefs(user_id, notify_comments, notify_replies):
    """Set notification preferences."""
    conn = _get_conn()
    conn.execute("UPDATE users SET notify_comments=?, notify_replies=? WHERE user_id=?",
                 (notify_comments, notify_replies, user_id))
    conn.commit()
    conn.close()


def ensure_welcome_notification(user_id):
    """Create welcome notification if user hasn't been welcomed yet. Returns True if created."""
    conn = _get_conn()
    row = conn.execute("SELECT welcomed FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row['welcomed']:
        conn.close()
        return False
    # Create welcome notification (disable FK checks since backtest_id is empty for system notifications)
    now = datetime.utcnow().isoformat()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """INSERT INTO notifications (id, user_id, actor_id, actor_email, backtest_id, comment_id, type, message, link, created_at)
           VALUES (?, ?, 'system', 'system', '', '', 'welcome', ?, ?, ?)""",
        (str(uuid.uuid4()), user_id,
         'Welcome to Bitcoin Strategy Analytics! We\'d love to hear your feedback.',
         '/feedback', now)
    )
    conn.execute("PRAGMA foreign_keys = ON")
    # Mark user as welcomed (upsert in case user row doesn't exist yet)
    if row:
        conn.execute("UPDATE users SET welcomed=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True


def backfill_welcome_notifications():
    """Create welcome notifications for all existing users who haven't been welcomed."""
    conn = _get_conn()
    conn.execute("PRAGMA foreign_keys = OFF")
    # Get all unique user_ids from backtests who don't have a welcome notification yet
    rows = conn.execute(
        """SELECT DISTINCT user_id FROM backtests
           WHERE user_id NOT IN (SELECT user_id FROM notifications WHERE type='welcome')"""
    ).fetchall()
    now = datetime.utcnow().isoformat()
    for row in rows:
        uid = row['user_id']
        conn.execute(
            """INSERT INTO notifications (id, user_id, actor_id, actor_email, backtest_id, comment_id, type, message, link, created_at)
               VALUES (?, ?, 'system', 'system', '', '', 'welcome', ?, ?, ?)""",
            (str(uuid.uuid4()), uid,
             'Welcome to Bitcoin Strategy Analytics! We\'d love to hear your feedback.',
             '/feedback', now)
        )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()
    return len(rows)


def increment_views(backtest_id):
    """Increment view count for a backtest."""
    conn = _get_conn()
    conn.execute("UPDATE backtests SET views_count = COALESCE(views_count, 0) + 1 WHERE id=?", (backtest_id,))
    conn.commit()
    conn.close()


def get_recent_comments(limit=10):
    """Get the most recent comments across all public backtests. Returns list of dicts with backtest info."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT c.*, b.title as backtest_title, b.id as bt_id
           FROM comments c
           JOIN backtests b ON c.backtest_id = b.id
           WHERE b.visibility IN ('featured', 'community')
           ORDER BY c.created_at DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]
