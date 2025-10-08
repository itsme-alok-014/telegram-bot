import os

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Persistent session DB (SQLite file). For Render, mount a Disk and set DB_PATH=/var/data/sessions.db
DB_PATH = os.environ.get("DB_PATH", "sessions.db")

# Optional: restrict usage to specific Telegram numeric user IDs (comma-separated)
ALLOWED_USER_IDS = set(
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
)

# Render will pass PORT; default for local dev
PORT = int(os.environ.get("PORT", "8080"))
