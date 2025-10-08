# config.py
import os

# Telegram API credentials (get these from my.telegram.org)
API_ID = int(os.environ.get("API_ID", "0"))    # e.g. 123456
API_HASH = os.environ.get("API_HASH", "")

# Bot API token (get this from @BotFather)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Database file path
DB_PATH = os.environ.get("DB_PATH", "sessions.db")
