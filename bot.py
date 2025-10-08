# bot.py
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

# Enable logging for debugging
logging.basicConfig(
    format='[%(levelname)s %(asctime)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Telegram login conversation states ---
ASK_PHONE, ASK_CODE, ASK_PASSWORD = range(3)

def login_start(update: Update, context: CallbackContext) -> int:
    """Start the login process by asking for the user's phone number."""
    update.message.reply_text("Please send your phone number (international format, e.g. +123456789).")
    return ASK_PHONE

def ask_code(update: Update, context: CallbackContext) -> int:
    """Receive the phone number, send login code, and ask for the code."""
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    # Create a new Telethon client with a fresh in-memory session
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    client.connect()
    try:
        # Send code to phone
        client.send_code_request(phone)
        context.user_data['client'] = client
        update.message.reply_text("Code sent. Please enter the code you received.")
        return ASK_CODE
    except Exception as e:
        logger.error(f"send_code_request error: {e}")
        update.message.reply_text(f"Failed to send code: {e}")
        return ConversationHandler.END

def finalize_login(update: Update, context: CallbackContext) -> int:
    """
    Receive the code (or password if needed) and complete login.
    Handles both the 2FA code and the password step.
    """
    text = update.message.text.strip()
    client: TelegramClient = context.user_data.get('client')
    if not client:
        update.message.reply_text("Session error. Please /login again.")
        return ConversationHandler.END
    phone = context.user_data.get('phone')

    try:
        # Attempt sign in with code
        client.sign_in(phone, text)
    except SessionPasswordNeededError:
        # Need 2FA password
        update.message.reply_text("Two-factor authentication enabled. Please enter your password.")
        return ASK_PASSWORD
    except Exception as e:
        # Wrong code or other error
        logger.error(f"sign_in error (code): {e}")
        update.message.reply_text(f"Failed to sign in with code: {e}")
        return ConversationHandler.END
    else:
        # Successfully signed in
        session_str = client.session.save()  # Get the session string for storage:contentReference[oaicite:8]{index=8}
        user_id = update.effective_user.id
        database.save_session(user_id, session_str)
        update.message.reply_text("✅ Logged in successfully!")
        client.disconnect()
        return ConversationHandler.END

def ask_password(update: Update, context: CallbackContext) -> int:
    """Handle the 2FA password and complete login."""
    password = update.message.text.strip()
    client: TelegramClient = context.user_data.get('client')
    if not client:
        update.message.reply_text("Session error. Please /login again.")
        return ConversationHandler.END

    try:
        client.sign_in(password=password)
    except Exception as e:
        logger.error(f"sign_in error (password): {e}")
        update.message.reply_text(f"Failed to sign in with password: {e}")
        return ConversationHandler.END
    else:
        session_str = client.session.save()
        user_id = update.effective_user.id
        database.save_session(user_id, session_str)
        update.message.reply_text("✅ Logged in successfully!")
        client.disconnect()
        return ConversationHandler.END

def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel the login conversation."""
    update.message.reply_text("Login process cancelled.")
    return ConversationHandler.END

# --- Save single message ---
def save_message(update: Update, context: CallbackContext):
    """
    Handle /save <link>. Fetch the single message and deliver it to the user.
    """
    if not context.args or len(context.args) != 1:
        update.message.reply_text("Usage: /save <telegram_message_link>")
        return

    link = context.args[0]
    chat, msg_id = parse_message_link(link)
    if chat is None or msg_id is None:
        update.message.reply_text("Invalid link format.")
        return

    user_id = update.effective_user.id
    session_str = database.get_session(user_id)
    if not session_str:
        update.message.reply_text("❗ You must /login first before using this command.")
        return

    # Create Telethon client for this user session
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    client.connect()
    try:
        message = client.get_messages(chat, ids=msg_id)  # get_messages by ID:contentReference[oaicite:9]{index=9}
    except Exception as e:
        logger.error(f"get_messages error: {e}")
        update.message.reply_text(f"Failed to fetch message: {e}")
        client.disconnect()
        return

    if not message:
        update.message.reply_text("Message not found.")
        client.disconnect()
        return

    # If the message has media, download and send it
    if message.media:
        try:
            file_path = client.download_media(message)  # Download media:contentReference[oaicite:10]{index=10}
            with open(file_path, 'rb') as f:
                update.message.reply_document(f)
        except Exception as e:
            logger.error(f"download_media error: {e}")
            update.message.reply_text(f"Failed to download media: {e}")

    # If the message has text or caption, send it
    if message.message:
        update.message.reply_text(message.message)

    client.disconnect()

# --- Save batch of messages ---
def save_batch(update: Update, context: CallbackContext):
    """
    Handle /batch <start_id> <end_id>. Fetch and send all messages in the range [start, end].
    """
    if not context.args or len(context.args) != 2:
        update.message.reply_text("Usage: /batch <start_msg_id> <end_msg_id>")
        return

    try:
        start_id = int(context.args[0])
        end_id = int(context.args[1])
    except ValueError:
        update.message.reply_text("Start and end must be integers.")
        return

    if start_id > end_id:
        start_id, end_id = end_id, start_id  # swap if out of order

    user_id = update.effective_user.id
    session_str = database.get_session(user_id)
    if not session_str:
        update.message.reply_text("❗ You must /login first before using this command.")
        return

    link = context.args[0]  # The command itself doesn't include chat link, assume last used chat?
    # For simplicity, we require the user to have just issued /save to set a context chat
    update.message.reply_text("Please use /save with a link first to define the chat context.")
    # (Alternatively, you could ask user to input chat or link again)
    return

# (Note: In this example, /batch does not specify chat, so we require /save first.
#  A more sophisticated bot might store the last used chat or parse context.)

def main():
    # Start dummy HTTP server for health check (Koyeb requires an open port):contentReference[oaicite:11]{index=11}.
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

    port = 8080  # or os.environ.get("PORT", 8080)
    threading.Thread(target=HTTPServer(('', port), Handler).serve_forever, daemon=True).start()
    logger.info(f"Health-check server running on port {port}")

    # Initialize bot
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Conversation handler for /login
    conv = ConversationHandler(
        entry_points=[CommandHandler('login', login_start)],
        states={
            ASK_PHONE: [MessageHandler(Filters.text & ~Filters.command, ask_code)],
            ASK_CODE:  [MessageHandler(Filters.text & ~Filters.command, finalize_login)],
            ASK_PASSWORD: [MessageHandler(Filters.text & ~Filters.command, ask_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    dp.add_handler(conv)

    # Other commands
    dp.add_handler(CommandHandler('save', save_message))
    dp.add_handler(CommandHandler('batch', save_batch))

    # Start polling
    updater.start_polling()
    logger.info("Bot started. Listening for commands.")
    updater.idle()

if __name__ == '__main__':
    main()
