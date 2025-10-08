import sqlite3
from config import DB_PATH

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY,
    session TEXT
)
""")
conn.commit()

def save_session(user_id: int, session_str: str):
    cursor.execute("REPLACE INTO sessions (user_id, session) VALUES (?, ?)", (user_id, session_str))
    conn.commit()

def get_session(user_id: int):
    cursor.execute("SELECT session FROM sessions WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def delete_session(user_id: int):
    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
