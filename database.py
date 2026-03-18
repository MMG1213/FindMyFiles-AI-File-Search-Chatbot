import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from contextlib import contextmanager
import secrets

DATABASE_PATH = "email_assistant.db"


@contextmanager
def get_db_connection():
    """Context manager for database connections with WAL mode for concurrency."""
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode allows concurrent readers + one writer without "database is locked" errors
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")   # wait up to 5s before raising locked error
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def initialize_database():
    """Create all required tables if they don't exist."""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_gmail_connected INTEGER DEFAULT 0,
                is_drive_connected INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                encrypted_token TEXT NOT NULL,
                token_created_at TIMESTAMP,
                token_updated_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                cache_key TEXT NOT NULL,
                cache_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token   ON user_sessions(session_token)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user    ON user_sessions(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_user        ON chat_history(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user   ON user_downloads(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cache_user       ON search_cache(user_id)")

    print("✅ Database initialized successfully")


# ── User operations ───────────────────────────────────────────────────────────

def create_user(username: str, email: str, password_hash: str) -> Optional[int]:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                (username, email, password_hash),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_user_by_username(username: str) -> Optional[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_last_login(user_id: int):
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now(), user_id))


def update_gmail_connection_status(user_id: int, is_connected: bool):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET is_gmail_connected = ? WHERE id = ?",
            (1 if is_connected else 0, user_id),
        )


def update_drive_connection_status(user_id: int, is_connected: bool):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET is_drive_connected = ? WHERE id = ?",
            (1 if is_connected else 0, user_id),
        )


# ── Token operations ──────────────────────────────────────────────────────────

def save_user_token(user_id: int, encrypted_token: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM user_tokens WHERE user_id = ?", (user_id,))
        exists = cursor.fetchone()
        if exists:
            cursor.execute(
                "UPDATE user_tokens SET encrypted_token = ?, token_updated_at = ? WHERE user_id = ?",
                (encrypted_token, datetime.now(), user_id),
            )
        else:
            cursor.execute(
                "INSERT INTO user_tokens (user_id, encrypted_token, token_created_at, token_updated_at) VALUES (?, ?, ?, ?)",
                (user_id, encrypted_token, datetime.now(), datetime.now()),
            )


def get_user_token(user_id: int) -> Optional[str]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT encrypted_token FROM user_tokens WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row["encrypted_token"] if row else None


def delete_user_token(user_id: int):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))


# ── Session operations ────────────────────────────────────────────────────────

def create_session(user_id: int, session_duration_hours: int = 24) -> str:
    session_token = secrets.token_urlsafe(32)
    expires_at    = datetime.now() + timedelta(hours=session_duration_hours)
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO user_sessions (user_id, session_token, expires_at) VALUES (?, ?, ?)",
            (user_id, session_token, expires_at),
        )
    return session_token


def validate_session(session_token: str) -> Optional[int]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, expires_at FROM user_sessions WHERE session_token = ?",
            (session_token,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.now() > expires_at:
            delete_session(session_token)
            return None
        return row["user_id"]


def delete_session(session_token: str):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))


def delete_user_sessions(user_id: int):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))


def cleanup_expired_sessions():
    with get_db_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM user_sessions WHERE expires_at < ?", (datetime.now(),)
        )
        if cursor.rowcount > 0:
            print(f"🧹 Cleaned up {cursor.rowcount} expired sessions")


# ── Chat history operations ───────────────────────────────────────────────────

def save_chat_message(user_id: int, role: str, content: str):
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )


def get_chat_history(user_id: int, limit: int = 20) -> List[Dict]:
    """
    Return the most recent `limit` messages (default 20).
    Reduced from 100 to prevent overflowing the LLM context window.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT role, content, timestamp FROM chat_history
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cursor.fetchall()
    # Return in chronological order
    return [dict(r) for r in reversed(rows)]


def clear_chat_history(user_id: int):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))


# ── File download operations ──────────────────────────────────────────────────

def save_download_record(user_id: int, filename: str, file_path: str, file_size: int):
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO user_downloads (user_id, filename, file_path, file_size) VALUES (?, ?, ?, ?)",
            (user_id, filename, file_path, file_size),
        )


def get_user_downloads(user_id: int) -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT filename, file_path, file_size, downloaded_at
            FROM user_downloads WHERE user_id = ?
            ORDER BY downloaded_at DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in cursor.fetchall()]


def delete_download_record(user_id: int, file_path: str):
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM user_downloads WHERE user_id = ? AND file_path = ?",
            (user_id, file_path),
        )


# ── Search cache operations ───────────────────────────────────────────────────

def save_search_cache(user_id: int, cache_key: str, cache_data: str):
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM search_cache WHERE user_id = ? AND cache_key = ?",
            (user_id, cache_key),
        )
        conn.execute(
            "INSERT INTO search_cache (user_id, cache_key, cache_data) VALUES (?, ?, ?)",
            (user_id, cache_key, cache_data),
        )


def get_search_cache(user_id: int, cache_key: str) -> Optional[str]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT cache_data FROM search_cache
            WHERE user_id = ? AND cache_key = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, cache_key),
        )
        row = cursor.fetchone()
        return row["cache_data"] if row else None


def clear_search_cache(user_id: int):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM search_cache WHERE user_id = ?", (user_id,))


# ── Admin / utility ───────────────────────────────────────────────────────────

def get_user_stats(user_id: int) -> Dict:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM chat_history    WHERE user_id = ?", (user_id,))
        msg_count = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) as count FROM user_downloads  WHERE user_id = ?", (user_id,))
        dl_count  = cursor.fetchone()["count"]
        cursor.execute(
            "SELECT COUNT(*) as count FROM user_sessions WHERE user_id = ? AND expires_at > ?",
            (user_id, datetime.now()),
        )
        sessions  = cursor.fetchone()["count"]
    return {"message_count": msg_count, "download_count": dl_count, "active_sessions": sessions}


if __name__ == "__main__":
    initialize_database()
    print("Database setup complete!")
