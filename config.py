import os

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DB_PATH = os.environ.get("DB_PATH", "sessions.db")

ALLOWED_USER_IDS = set(
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
)

PORT = int(os.environ.get("PORT", "8080"))
