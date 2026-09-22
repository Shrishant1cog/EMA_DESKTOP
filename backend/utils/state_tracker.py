import os
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger("StateTracker")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = BASE_DIR / "assistant_v2.db"

_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set = set()


def get_db_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Creates a high-performance, WAL-configured SQLite connection with memory limits."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row

    # Concurrency and memory pragmas
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA cache_size=-2000;")         # 2MB memory cap
    conn.execute("PRAGMA wal_autocheckpoint=1000;")  # Prevents unbounded WAL growth
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH, force: bool = False):
    """Initializes tables, ensures COLLATE NOCASE migrations on existing tables, and builds indices."""
    db_key = str(db_path.resolve())
    with _INIT_LOCK:
        if not force and db_key in _INITIALIZED_DBS and db_path.exists():
            return

        conn = get_db_connection(db_path)
        try:
            cur = conn.cursor()

            # 1. Accounts Table Base Creation
            cur.execute("""
                CREATE TABLE IF NOT EXISTS google_accounts (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    name TEXT,
                    picture TEXT,
                    token_data TEXT,
                    is_monitored INTEGER DEFAULT 1,
                    created_at TEXT,
                    session_id TEXT
                )
            """)

            # Safe migration: ensure session_id column exists
            cur.execute("PRAGMA table_info(google_accounts)")
            columns = [row[1] for row in cur.fetchall()]
            if "session_id" not in columns:
                cur.execute("ALTER TABLE google_accounts ADD COLUMN session_id TEXT")

            # Collation Migration: Check if google_accounts email column has COLLATE NOCASE
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='google_accounts'")
            row = cur.fetchone()
            if row and "COLLATE NOCASE" not in row[0].upper():
                cur.execute("ALTER TABLE google_accounts RENAME TO _google_accounts_old")
                cur.execute("""
                    CREATE TABLE google_accounts (
                        email TEXT PRIMARY KEY COLLATE NOCASE,
                        name TEXT,
                        picture TEXT,
                        token_data TEXT,
                        is_monitored INTEGER DEFAULT 1,
                        created_at TEXT,
                        session_id TEXT
                    )
                """)
                cur.execute("""
                    INSERT OR IGNORE INTO google_accounts (email, name, picture, token_data, is_monitored, created_at, session_id)
                    SELECT email, name, picture, token_data, is_monitored, created_at, session_id FROM _google_accounts_old
                """)
                cur.execute("DROP TABLE _google_accounts_old")

            # 2. Email Actions History Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS email_actions (
                    email_id TEXT PRIMARY KEY,
                    account_email TEXT COLLATE NOCASE,
                    sender TEXT,
                    subject TEXT,
                    category TEXT,
                    priority TEXT,
                    summary TEXT,
                    action_taken TEXT,
                    status TEXT,
                    processed_at TIMESTAMP
                )
            """)

            # Collation Migration: Check email_actions account_email column
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='email_actions'")
            row_actions = cur.fetchone()
            if row_actions and "COLLATE NOCASE" not in row_actions[0].upper():
                cur.execute("ALTER TABLE email_actions RENAME TO _email_actions_old")
                cur.execute("""
                    CREATE TABLE email_actions (
                        email_id TEXT PRIMARY KEY,
                        account_email TEXT COLLATE NOCASE,
                        sender TEXT,
                        subject TEXT,
                        category TEXT,
                        priority TEXT,
                        summary TEXT,
                        action_taken TEXT,
                        status TEXT,
                        processed_at TIMESTAMP
                    )
                """)
                cur.execute("""
                    INSERT OR IGNORE INTO email_actions
                    SELECT email_id, account_email, sender, subject, category, priority, summary, action_taken, status, processed_at
                    FROM _email_actions_old
                """)
                cur.execute("DROP TABLE _email_actions_old")

            # 3. System Settings Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # 4. Performance Indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email_nocase ON google_accounts (email COLLATE NOCASE);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_session ON google_accounts (session_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_actions_account ON email_actions (account_email COLLATE NOCASE);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_actions_status ON email_actions (status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_actions_processed ON email_actions (processed_at DESC);")

            conn.commit()
            _INITIALIZED_DBS.add(db_key)
        finally:
            conn.close()


# ---------------- Account Management Helpers ---------------- #

def upsert_google_account(
    email: str,
    name: str,
    picture: str,
    token_data: str,
    session_id: Optional[str] = None,
    is_monitored: int = 1,
    db_path: Path = DEFAULT_DB_PATH
):
    clean_email = email.strip().lower()
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        now = datetime.now().isoformat()
        cur.execute("""
            INSERT INTO google_accounts (email, name, picture, token_data, is_monitored, created_at, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name,
                picture = excluded.picture,
                token_data = excluded.token_data,
                is_monitored = excluded.is_monitored,
                session_id = COALESCE(excluded.session_id, google_accounts.session_id)
        """, (clean_email, name, picture, token_data, is_monitored, now, session_id))
        conn.commit()
    finally:
        conn.close()


def list_all_google_accounts(session_id: Optional[str] = None, db_path: Path = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT email, name, picture, is_monitored, created_at, session_id "
            "FROM google_accounts ORDER BY created_at ASC"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_google_account(email: str, db_path: Path = DEFAULT_DB_PATH) -> Optional[Dict[str, Any]]:
    """Strict case-insensitive, whitespace-stripped lookup."""
    if not email:
        return None
    clean_email = email.strip().lower()
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM google_accounts WHERE email = ? COLLATE NOCASE", (clean_email,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_account_monitoring_status(email: str, is_monitored: bool, db_path: Path = DEFAULT_DB_PATH):
    clean_email = email.strip().lower()
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE google_accounts SET is_monitored = ? WHERE email = ? COLLATE NOCASE", (1 if is_monitored else 0, clean_email))
        conn.commit()
    finally:
        conn.close()


def delete_google_account(email: str, db_path: Path = DEFAULT_DB_PATH):
    clean_email = email.strip().lower()
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM google_accounts WHERE email = ? COLLATE NOCASE", (clean_email,))
        conn.commit()
    finally:
        conn.close()


# ---------------- Processing Records Helpers ---------------- #

def is_email_processed(email_id: str, account_email: Optional[str] = None, db_path: Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        if account_email:
            cur.execute(
                "SELECT 1 FROM email_actions WHERE email_id = ? AND account_email = ? COLLATE NOCASE",
                (str(email_id), str(account_email).strip().lower())
            )
        else:
            cur.execute("SELECT 1 FROM email_actions WHERE email_id = ?", (str(email_id),))
        row = cur.fetchone()
        return row is not None
    finally:
        conn.close()


def record_email_action(
    email_id: str,
    account_email: str,
    sender: str,
    subject: str,
    category: str,
    priority: str,
    summary: str,
    action_taken: str,
    status: str,
    db_path: Path = DEFAULT_DB_PATH
):
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        now = datetime.now().isoformat()
        cur.execute("""
            INSERT OR REPLACE INTO email_actions 
            (email_id, account_email, sender, subject, category, priority, summary, action_taken, status, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(email_id), str(account_email).strip().lower(), sender, subject,
            category, priority, summary, action_taken, status, now
        ))
        conn.commit()
    finally:
        conn.close()


def get_scoped_stats(session_id: Optional[str] = None, db_path: Path = DEFAULT_DB_PATH) -> Dict[str, int]:
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM email_actions").fetchone()[0]
        actioned = cur.execute("SELECT COUNT(*) FROM email_actions WHERE status = 'ACTIONED'").fetchone()[0]
        processed = cur.execute("SELECT COUNT(*) FROM email_actions WHERE status = 'PROCESSED'").fetchone()[0]
        filtered = cur.execute("SELECT COUNT(*) FROM email_actions WHERE status = 'FILTERED'").fetchone()[0]
        return {"total": total, "actioned": actioned, "processed": processed, "filtered": filtered}
    finally:
        conn.close()


def list_scoped_emails(
    session_id: Optional[str] = None,
    category_filter: str = "ALL",
    limit: int = 50,
    offset: int = 0,
    db_path: Path = DEFAULT_DB_PATH
) -> List[Dict[str, Any]]:
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        conditions = []
        params = []

        if category_filter and category_filter != "ALL":
            conditions.append("category = ?")
            params.append(category_filter)

        where_str = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        query = f"SELECT * FROM email_actions {where_str} ORDER BY processed_at DESC LIMIT ? OFFSET ?"
        cur.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------- System Settings Helpers ---------------- #

def get_setting(key: str, default: str = "", db_path: Path = DEFAULT_DB_PATH) -> str:
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str, db_path: Path = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def delete_setting(key: str, db_path: Path = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM system_settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()