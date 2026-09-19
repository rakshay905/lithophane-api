import sqlite3, os, time

DB_PATH = os.path.join(os.path.dirname(__file__), 'litho.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name          TEXT DEFAULT '',
    is_verified   INTEGER DEFAULT 0,
    created_at    INTEGER DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS email_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    token      TEXT UNIQUE NOT NULL,
    purpose    TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    plan                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    razorpay_sub_id     TEXT,
    razorpay_payment_id TEXT,
    started_at          INTEGER,
    expires_at          INTEGER,
    created_at          INTEGER DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    ip         TEXT,
    tool       TEXT,
    created_at INTEGER DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at INTEGER DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id),
    email      TEXT NOT NULL,
    subject    TEXT NOT NULL,
    message    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    priority   TEXT NOT NULL DEFAULT 'normal',
    created_at INTEGER DEFAULT (unixepoch()),
    updated_at INTEGER DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS ticket_replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    is_admin   INTEGER NOT NULL DEFAULT 0,
    message    TEXT NOT NULL,
    created_at INTEGER DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_tickets_status   ON tickets(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ticket_replies   ON ticket_replies(ticket_id);

CREATE INDEX IF NOT EXISTS idx_usage_user_day ON usage_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_ip_day   ON usage_log(ip, created_at);
CREATE INDEX IF NOT EXISTS idx_sub_user       ON subscriptions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tokens_token   ON email_tokens(token, purpose);
"""

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with _conn() as c:
        c.executescript(SCHEMA)

# ── Users ──────────────────────────────────────────────────────────────────────

def get_user_by_email(email):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()

def get_user_by_id(user_id):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

def create_user(email, password_hash, name=''):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO users (email, password_hash, name) VALUES (?,?,?)",
            (email.lower().strip(), password_hash, name),
        )
        return cur.lastrowid

def verify_user(user_id):
    with _conn() as c:
        c.execute("UPDATE users SET is_verified=1 WHERE id=?", (user_id,))

def update_password(user_id, password_hash):
    with _conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))

def update_name(user_id, name):
    with _conn() as c:
        c.execute("UPDATE users SET name=? WHERE id=?", (name, user_id))

# ── Email tokens ───────────────────────────────────────────────────────────────

def create_email_token(user_id, purpose):
    """Creates and returns a new token string. Expires in 24h for verify, 1h for reset."""
    import secrets
    token = secrets.token_hex(32)
    ttl = 86400 if purpose == 'verify' else 3600
    expires_at = int(time.time()) + ttl
    with _conn() as c:
        # Invalidate old unused tokens for same user+purpose
        c.execute("UPDATE email_tokens SET used=1 WHERE user_id=? AND purpose=? AND used=0",
                  (user_id, purpose))
        c.execute(
            "INSERT INTO email_tokens (user_id, token, purpose, expires_at) VALUES (?,?,?,?)",
            (user_id, token, purpose, expires_at),
        )
    return token

def consume_email_token(token, purpose):
    """Validates and marks used. Returns user_id or None."""
    now = int(time.time())
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM email_tokens WHERE token=? AND purpose=? AND used=0 AND expires_at>?",
            (token, purpose, now),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE email_tokens SET used=1 WHERE id=?", (row['id'],))
        return row['user_id']

# ── Subscriptions ──────────────────────────────────────────────────────────────

def get_active_subscription(user_id):
    now = int(time.time())
    with _conn() as c:
        return c.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (user_id, now),
        ).fetchone()

def create_subscription(user_id, plan, razorpay_sub_id=None, razorpay_payment_id=None, expires_at=None):
    now = int(time.time())
    with _conn() as c:
        # Mark any existing active subs as superseded
        c.execute("UPDATE subscriptions SET status='cancelled' WHERE user_id=? AND status='active'",
                  (user_id,))
        cur = c.execute(
            """INSERT INTO subscriptions
               (user_id, plan, status, razorpay_sub_id, razorpay_payment_id, started_at, expires_at)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, plan, 'active', razorpay_sub_id, razorpay_payment_id, now, expires_at),
        )
        return cur.lastrowid

def update_subscription_status(razorpay_sub_id, status, expires_at=None):
    with _conn() as c:
        if expires_at:
            c.execute(
                "UPDATE subscriptions SET status=?, expires_at=? WHERE razorpay_sub_id=?",
                (status, expires_at, razorpay_sub_id),
            )
        else:
            c.execute(
                "UPDATE subscriptions SET status=? WHERE razorpay_sub_id=?",
                (status, razorpay_sub_id),
            )

def cancel_subscription_by_user(user_id):
    with _conn() as c:
        c.execute(
            "UPDATE subscriptions SET status='cancelled' WHERE user_id=? AND status='active'",
            (user_id,),
        )

def override_subscription(user_id, plan, days):
    """Admin: manually grant/extend a plan for N days."""
    expires_at = int(time.time()) + days * 86400
    create_subscription(user_id, plan, razorpay_sub_id=None, expires_at=expires_at)

def cancel_subscription_by_id(sub_id):
    with _conn() as c:
        c.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (sub_id,))

# ── Usage log ──────────────────────────────────────────────────────────────────

def log_usage(user_id, ip, tool):
    with _conn() as c:
        c.execute("INSERT INTO usage_log (user_id, ip, tool) VALUES (?,?,?)",
                  (user_id, ip, tool))

def count_today_usage(user_id=None, ip=None):
    """Count generations today (midnight UTC) for this user or IP."""
    day_start = int(time.time() // 86400 * 86400)
    with _conn() as c:
        if user_id:
            return c.execute(
                "SELECT COUNT(*) FROM usage_log WHERE user_id=? AND created_at>=?",
                (user_id, day_start),
            ).fetchone()[0]
        elif ip:
            return c.execute(
                "SELECT COUNT(*) FROM usage_log WHERE ip=? AND user_id IS NULL AND created_at>=?",
                (ip, day_start),
            ).fetchone()[0]
        return 0

def get_last_usage_time(user_id=None, ip=None):
    """Return unix timestamp of the most recent generation, or 0 if none."""
    with _conn() as c:
        if user_id:
            row = c.execute(
                "SELECT created_at FROM usage_log WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        elif ip:
            row = c.execute(
                "SELECT created_at FROM usage_log WHERE ip=? AND user_id IS NULL ORDER BY created_at DESC LIMIT 1",
                (ip,),
            ).fetchone()
        else:
            return 0
        return row[0] if row else 0

# ── Admin helpers ──────────────────────────────────────────────────────────────

def get_all_users(limit=200, offset=0):
    with _conn() as c:
        return c.execute(
            """SELECT u.*, s.plan as sub_plan, s.expires_at as sub_expires,
               (SELECT COUNT(*) FROM usage_log ul WHERE ul.user_id=u.id) as total_gens
               FROM users u
               LEFT JOIN subscriptions s ON s.user_id=u.id AND s.status='active' AND s.expires_at>unixepoch()
               ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()

def count_users():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def search_users(query, limit=50, offset=0):
    q = f'%{query}%'
    with _conn() as c:
        return c.execute(
            "SELECT u.*, "
            "(SELECT plan FROM subscriptions WHERE user_id=u.id AND status='active' AND expires_at>unixepoch() ORDER BY id DESC LIMIT 1) as sub_plan, "
            "(SELECT COUNT(*) FROM usage_log WHERE user_id=u.id) as total_gens "
            "FROM users u WHERE u.email LIKE ? OR u.name LIKE ? "
            "ORDER BY u.id DESC LIMIT ? OFFSET ?",
            (q, q, limit, offset)
        ).fetchall()

def count_search_users(query):
    q = f'%{query}%'
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM users WHERE email LIKE ? OR name LIKE ?", (q, q)
        ).fetchone()[0]

def get_usage_stats():
    with _conn() as c:
        day_start = int(time.time() // 86400 * 86400)
        total_users   = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        verified_users = c.execute("SELECT COUNT(*) FROM users WHERE is_verified=1").fetchone()[0]
        pro_users     = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE status='active' AND expires_at>unixepoch() AND plan LIKE 'pro%'"
        ).fetchone()[0]
        plus_users    = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE status='active' AND expires_at>unixepoch() AND plan LIKE 'plus%'"
        ).fetchone()[0]
        today_gens    = c.execute(
            "SELECT COUNT(*) FROM usage_log WHERE created_at>=?", (day_start,)
        ).fetchone()[0]
        total_gens    = c.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]
        return dict(
            total_users=total_users, verified_users=verified_users,
            pro_users=pro_users, plus_users=plus_users,
            today_gens=today_gens, total_gens=total_gens,
        )

def get_recent_usage(limit=100):
    with _conn() as c:
        return c.execute(
            """SELECT ul.*, u.email FROM usage_log ul
               LEFT JOIN users u ON u.id=ul.user_id
               ORDER BY ul.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

# ── Settings (key-value store) ─────────────────────────────────────────────────

def get_setting(key, default=''):
    """Read a setting from DB; fall back to env var of the same name, then default."""
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row[0]:
        return row[0]
    return os.getenv(key, default)

def set_setting(key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,unixepoch()) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=unixepoch()",
            (key, value),
        )

def get_all_settings():
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {r['key']: r['value'] for r in rows}

def bulk_save_settings(kv: dict):
    with _conn() as c:
        for key, value in kv.items():
            c.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,unixepoch()) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=unixepoch()",
                (key, value),
            )

# ── Tickets ────────────────────────────────────────────────────────────────────

def create_ticket(email, subject, message, user_id=None, priority='normal'):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tickets (user_id, email, subject, message, priority) VALUES (?,?,?,?,?)",
            (user_id, email, subject, message, priority),
        )
        return cur.lastrowid

def get_tickets(status=None, limit=100, offset=0):
    with _conn() as c:
        if status:
            return c.execute(
                "SELECT t.*, u.name as user_name, "
                "(SELECT COUNT(*) FROM ticket_replies WHERE ticket_id=t.id) as reply_count "
                "FROM tickets t LEFT JOIN users u ON u.id=t.user_id "
                "WHERE t.status=? ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return c.execute(
            "SELECT t.*, u.name as user_name, "
            "(SELECT COUNT(*) FROM ticket_replies WHERE ticket_id=t.id) as reply_count "
            "FROM tickets t LEFT JOIN users u ON u.id=t.user_id "
            "ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, "
            "t.updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

def get_ticket(ticket_id):
    with _conn() as c:
        return c.execute(
            "SELECT t.*, u.name as user_name, u.email as user_email FROM tickets t "
            "LEFT JOIN users u ON u.id=t.user_id WHERE t.id=?", (ticket_id,)
        ).fetchone()

def get_ticket_replies(ticket_id):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM ticket_replies WHERE ticket_id=? ORDER BY created_at ASC",
            (ticket_id,),
        ).fetchall()

def add_ticket_reply(ticket_id, message, is_admin=False):
    with _conn() as c:
        c.execute(
            "INSERT INTO ticket_replies (ticket_id, message, is_admin) VALUES (?,?,?)",
            (ticket_id, message, 1 if is_admin else 0),
        )
        new_status = 'in_progress' if is_admin else 'open'
        c.execute(
            "UPDATE tickets SET status=?, updated_at=unixepoch() WHERE id=? AND status!='closed'",
            (new_status, ticket_id),
        )

def set_ticket_status(ticket_id, status):
    with _conn() as c:
        c.execute(
            "UPDATE tickets SET status=?, updated_at=unixepoch() WHERE id=?",
            (status, ticket_id),
        )

def count_tickets(status=None):
    with _conn() as c:
        if status:
            return c.execute("SELECT COUNT(*) FROM tickets WHERE status=?", (status,)).fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

# ── Payments / Subscriptions ───────────────────────────────────────────────────

def get_all_subscriptions(limit=100, offset=0, status=None):
    with _conn() as c:
        if status:
            return c.execute(
                "SELECT s.*, u.email, u.name FROM subscriptions s "
                "LEFT JOIN users u ON u.id=s.user_id "
                "WHERE s.status=? ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return c.execute(
            "SELECT s.*, u.email, u.name FROM subscriptions s "
            "LEFT JOIN users u ON u.id=s.user_id "
            "ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

def count_subscriptions(status=None):
    with _conn() as c:
        if status:
            return c.execute("SELECT COUNT(*) FROM subscriptions WHERE status=?", (status,)).fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]

# ── Insights ───────────────────────────────────────────────────────────────────

def get_daily_signups(days=30):
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT date(created_at, 'unixepoch') as day, COUNT(*) as cnt "
            "FROM users WHERE created_at>=? GROUP BY day ORDER BY day",
            (cutoff,),
        ).fetchall()
    return [{'day': r['day'], 'count': r['cnt']} for r in rows]

def get_daily_generations(days=30):
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT date(created_at, 'unixepoch') as day, COUNT(*) as cnt "
            "FROM usage_log WHERE created_at>=? GROUP BY day ORDER BY day",
            (cutoff,),
        ).fetchall()
    return [{'day': r['day'], 'count': r['cnt']} for r in rows]

def get_tier_distribution():
    now = int(time.time())
    with _conn() as c:
        pro   = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE status='active' AND expires_at>? AND plan LIKE 'pro%'", (now,)
        ).fetchone()[0]
        plus  = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE status='active' AND expires_at>? AND plan LIKE 'plus%'", (now,)
        ).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        free  = max(0, total - pro - plus)
    return {'free': free, 'plus': plus, 'pro': pro}

def get_monthly_revenue_estimate(months=6):
    """Estimate revenue from active subscriptions by plan (display prices from settings)."""
    cutoff = int(time.time()) - months * 30 * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT strftime('%Y-%m', created_at, 'unixepoch') as month, plan, COUNT(*) as cnt "
            "FROM subscriptions WHERE created_at>=? AND status IN ('active','cancelled') "
            "GROUP BY month, plan ORDER BY month",
            (cutoff,),
        ).fetchall()
    return [{'month': r['month'], 'plan': r['plan'], 'count': r['cnt']} for r in rows]

def get_tool_usage_breakdown():
    with _conn() as c:
        rows = c.execute(
            "SELECT tool, COUNT(*) as cnt FROM usage_log GROUP BY tool ORDER BY cnt DESC"
        ).fetchall()
    return [{'tool': r['tool'] or 'unknown', 'count': r['cnt']} for r in rows]
