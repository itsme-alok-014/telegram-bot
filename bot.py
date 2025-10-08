import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, ConversationHandler, CallbackContext
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
import database
from config import API_ID, API_HASH, BOT_TOKEN
from utils import parse_message_link
import asyncio
import os

# ========== Logging Setup ==========
logging.basicConfig(
    format='[%(levelname)s %(asctime)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== Conversation States ==========
ASK_PHONE, ASK_CODE, ASK_PASSWORD = range(3)

# ===============================================================
# /login Command Flow
# ===============================================================

def login_start(update: Update, context: CallbackContext) -> int:
    """Start login flow by asking for user's phone number."""
    update.message.reply_text("📱 Please send your phone number (e.g., +919876543210):")
    return ASK_PHONE


def ask_code(update: Update, context: CallbackContext) -> int:
    """Receive phone number, send login code, and ask for the code."""
    phone = update.message.text.strip()
    context.user_data['phone'] = phone

    # ✅ Create new asyncio loop for Telethon to avoid "no event loop" error
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = TelegramClient(StringSession(), API_ID, API_HASH, loop=loop)
    client.connect()
    try:
        client.send_code_request(phone)
        context.user_data['client'] = client
        update.message.reply_text("📩 Code sent! Please enter the code you received:")
        return ASK_CODE
    except Exception as e:
        logger.error(f"send_code_request error: {e}")
        update.message.reply_text(f"❌ Failed to send code: {e}")
        client.disconnect()
        return ConversationHandler.END


def finalize_login(update: Update, context: CallbackContext) -> int:
    """Receive the code and complete login (ask password if 2FA enabled)."""
    code = update.message.text.strip()
    client: TelegramClient = context.user_data.get('client')
    if not client:
        update.message.reply_text("⚠️ Session expired. Please /login again.")
        return ConversationHandler.END

    phone = context.user_data.get('phone')

    try:
        client.sign_in(phone, code)
    except SessionPasswordNeededError:
        update.message.reply_text("🔐 Two-Step Verification is enabled. Please enter your password:")
        return ASK_PASSWORD
    except Exception as e:
        logger.error(f"sign_in error: {e}")
        update.message.reply_text(f"❌ Failed to sign in: {e}")
        client.disconnect()
        return ConversationHandler.END
    else:
        session_str = client.session.save()
        user_id = update.effective_user.id
        database.save_session(user_id, session_str)
        update.message.reply_text("✅ Logged in successfully!")
        client.disconnect()
        return ConversationHandler.END


def ask_password(update: Update, context: CallbackContext) -> int:
    """Handle Two-Step Verification password."""
    password = update.message.text.strip()
    client: TelegramClient = context.user_data.get('client')
    if not client:
        update.message.reply_text("⚠️ Session expired. Please /login again.")
        return ConversationHandler.END

    try:
        client.sign_in(password=password)
    except Exception as e:
        logger.error(f"sign_in error (password): {e}")
        update.message.reply_text(f"❌ Failed to sign in: {e}")
        client.disconnect()
        return ConversationHandler.END
    else:
        session_str = client.session.save()
        user_id = update.effective_user.id
        database.save_session(user_id, session_str)
        update.message.reply_text("✅ Logged in successfully!")
        client.disconnect()
        return ConversationHandler.END


def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel login process."""
    update.message.reply_text("❎ Login process cancelled.")
    return ConversationHandler.END


# ===============================================================
# /save Command
# ===============================================================

def save_message(update: Update, context: CallbackContext):
    """Fetch and send a single message (text/media) from given Telegram link."""
    if not context.args or len(context.args) != 1:
        update.message.reply_text("⚙️ Usage: /save <telegram_message_link>")
        return

    link = context.args[0]
    chat, msg_id = parse_message_link(link)
    if chat is None or msg_id is None:
        update.message.reply_text("❌ Invalid Telegram link.")
        return

    user_id = update.effective_user.id
    session_str = database.get_session(user_id)
    if not session_str:
        update.message.reply_text("⚠️ You must /login first.")
        return

    # ✅ Create a proper asyncio loop for Telethon client
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH, loop=loop)
    client.connect()

    try:
        message = client.get_messages(chat, ids=msg_id)
    except Exception as e:
        logger.error(f"get_messages error: {e}")
        update.message.reply_text(f"❌ Failed to fetch message: {e}")
        client.disconnect()
        return

    if not message:
        update.message.reply_text("⚠️ Message not found or inaccessible.")
        client.disconnect()
        return

    update.message.reply_text("📥 Fetching message... Please wait.")

    if message.media:
        try:
            file_path = client.download_media(message)
            if file_path and os.path.exists(file_path):
                with open(file_path, 'rb') as f:
                    update.message.reply_document(f)
                os.remove(file_path)
        except Exception as e:
            logger.error(f"download_media error: {e}")
            update.message.reply_text(f"❌ Failed to download media: {e}")

    if message.message:
        update.message.reply_text(message.message)

    client.disconnect()


# ===============================================================
# /batch Command (Optional)
# ===============================================================

def save_batch(update: Update, context: CallbackContext):
    """Download multiple messages in a range."""
    if len(context.args) < 3:
        update.message.reply_text("⚙️ Usage: /batch <link> <start_id> <end_id>")
        return

    link = context.args[0]
    start_id = int(context.args[1])
    end_id = int(context.args[2])

    chat, _ = parse_message_link(link)
    if chat is None:
        update.message.reply_text("❌ Invalid Telegram link.")
        return

    user_id = update.effective_user.id
    session_str = database.get_session(user_id)
    if not session_str:
        update.message.reply_text("⚠️ You must /login first.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH, loop=loop)
    client.connect()

    update.message.reply_text(f"📦 Downloading messages {start_id} to {end_id}...")

    for msg_id in range(start_id, end_id + 1):
        try:
            message = client.get_messages(chat, ids=msg_id)
            if message:
                if message.media:
                    file_path = client.download_media(message)
                    if file_path and os.path.exists(file_path):
                        with open(file_path, 'rb') as f:
                            update.message.reply_document(f)
                        os.remove(file_path)
                elif message.message:
                    update.message.reply_text(message.message)
        except Exception as e:
            logger.error(f"batch download error msg_id={msg_id}: {e}")

    update.message.reply_text("✅ Batch download completed!")
    client.disconnect()


# ===============================================================
# Health Server (Koyeb Requirement)
# ===============================================================

def start_health_server():
    """Run dummy HTTP server for Koyeb health check."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('', port), Handler)
    logger.info(f"Health-check server running on port {port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ===============================================================
# Main Function
# ===============================================================

def main():
    start_health_server()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Conversation for /login
    conv = ConversationHandler(
        entry_points=[CommandHandler('login', login_start)],
        states={
            ASK_PHONE: [MessageHandler(Filters.text & ~Filters.command, ask_code)],
            ASK_CODE: [MessageHandler(Filters.text & ~Filters.command, finalize_login)],
            ASK_PASSWORD: [MessageHandler(Filters.text & ~Filters.command, ask_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    dp.add_handler(conv)

    # Other command handlers
    dp.add_handler(CommandHandler('save', save_message))
    dp.add_handler(CommandHandler('batch', save_batch))
    dp.add_handler(CommandHandler('cancel', cancel))

    # Start polling
    updater.start_polling()
    logger.info("🤖 Bot started successfully. Listening for commands...")
    updater.idle()


if __name__ == '__main__':
    main()
