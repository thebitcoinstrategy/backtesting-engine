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
            actor_id TEXT NOT NULL,
            actor_email TEXT NOT NULL,
            backtest_id TEXT NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
            comment_id TEXT NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
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

    # --- Create notifications ---
    notified_user = None
    # Case 1: Reply to a comment -> notify the parent comment's author
    if parent_id:
        parent = conn.execute("SELECT user_id FROM comments WHERE id=?", (parent_id,)).fetchone()
        if parent and str(parent['user_id']) != str(user_id):
            notified_user = str(parent['user_id'])
            conn.execute(
                """INSERT INTO notifications (id, user_id, actor_id, actor_email, backtest_id, comment_id, type, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'reply', ?)""",
                (str(uuid.uuid4()), notified_user, user_id, email, backtest_id, comment_id, now)
            )
    # Case 2: Comment on a backtest -> notify the backtest owner
    bt = conn.execute("SELECT user_id FROM backtests WHERE id=?", (backtest_id,)).fetchone()
    if bt and str(bt['user_id']) != str(user_id):
        bt_owner = str(bt['user_id'])
        if bt_owner != notified_user:  # avoid double notification
            conn.execute(
                """INSERT INTO notifications (id, user_id, actor_id, actor_email, backtest_id, comment_id, type, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'backtest_comment', ?)""",
                (str(uuid.uuid4()), bt_owner, user_id, email, backtest_id, comment_id, now)
            )

    conn.commit()
    row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


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
           JOIN backtests b ON n.backtest_id = b.id
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
