import os
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "assistant_v2.db"


def get_db_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for thread-safe concurrent access
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH):
    """Initializes the multi-account, actions, and settings tables."""
    conn = get_db_connection(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS google_accounts (
            email TEXT PRIMARY KEY,
            name TEXT,
            picture TEXT,
            token_data TEXT,
            is_monitored INTEGER DEFAULT 1,
            created_at TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_actions (
            email_id TEXT PRIMARY KEY,
            account_email TEXT,
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
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Performance indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_actions_account ON email_actions (account_email);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_actions_status ON email_actions (status);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_actions_processed ON email_actions (processed_at DESC);")

    conn.commit()
    conn.close()


# ---------------- Account Management Helpers ---------------- #

def upsert_google_account(
    email: str,
    name: str,
    picture: str,
    token_data: str,
    is_monitored: int = 1,
    db_path: Path = DEFAULT_DB_PATH
):
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute("""
        INSERT INTO google_accounts (email, name, picture, token_data, is_monitored, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            name = excluded.name,
            picture = excluded.picture,
            token_data = excluded.token_data
    """, (email, name, picture, token_data, is_monitored, now))
    conn.commit()
    conn.close()


def list_all_google_accounts(db_path: Path = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT email, name, picture, is_monitored, created_at FROM google_accounts ORDER BY created_at ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_google_account(email: str, db_path: Path = DEFAULT_DB_PATH) -> Optional[Dict[str, Any]]:
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM google_accounts WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def set_account_monitoring_status(email: str, is_monitored: bool, db_path: Path = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE google_accounts SET is_monitored = ? WHERE email = ?", (1 if is_monitored else 0, email))
    conn.commit()
    conn.close()


def delete_google_account(email: str, db_path: Path = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM google_accounts WHERE email = ?", (email,))
    conn.commit()
    conn.close()


# ---------------- Processing Records Helpers ---------------- #

def is_email_processed(email_id: str, account_email: Optional[str] = None, db_path: Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    if account_email:
        cur.execute(
            "SELECT 1 FROM email_actions WHERE email_id = ? AND account_email = ?",
            (str(email_id), str(account_email))
        )
    else:
        cur.execute("SELECT 1 FROM email_actions WHERE email_id = ?", (str(email_id),))
    row = cur.fetchone()
    conn.close()
    return row is not None


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
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute("""
        INSERT OR REPLACE INTO email_actions 
        (email_id, account_email, sender, subject, category, priority, summary, action_taken, status, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(email_id), account_email, sender, subject,
        category, priority, summary, action_taken, status, now
    ))
    conn.commit()
    conn.close()


# ---------------- System Settings Helpers ---------------- #

def get_setting(key: str, default: str = "", db_path: Path = DEFAULT_DB_PATH) -> str:
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str, db_path: Path = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def delete_setting(key: str, db_path: Path = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = get_db_connection(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM system_settings WHERE key = ?", (key,))
    conn.commit()
    conn.close()