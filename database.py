# database.py
import sqlite3
from config import DB_PATH

# Connect to SQLite (file-based). The database file will be created if it doesn't exist.
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# Create table for sessions: maps Telegram user_id -> session string.
cursor.execute("""
CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY,
    session TEXT
)
""")
conn.commit()

def save_session(user_id: int, session_str: str):
    """
    Save or update the session string for the given Telegram user_id.
    """
    cursor.execute(
        "REPLACE INTO sessions (user_id, session) VALUES (?, ?)",
        (user_id, session_str)
    )
    conn.commit()

def get_session(user_id: int) -> str:
    """
    Retrieve the session string for the given user_id, or None if not found.
    """
    cursor.execute("SELECT session FROM sessions WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None
