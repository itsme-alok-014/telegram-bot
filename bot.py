import os
import logging
from telegram import Update
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, RPCError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)

import database
from utils import parse_message_link, clamp_int

# Logging setup
logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
ASK_PHONE, ASK_CODE, ASK_PASSWORD = range(3)

# Helper for allowed users
def ensure_allowed(func):
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        uid = getattr(update.effective_user, "id", None)
        if os.environ.get("ALLOWED_USER_IDS"):
            allowed_ids = set(int(x) for x in os.environ.get("ALLOWED_USER_IDS").split(",") if x.strip().isdigit())
            if uid not in allowed_ids:
                if update.message:
                    update.message.reply_text("🚫 Not authorized.")
                return ConversationHandler.END
        return func(update, context, *args, **kwargs)
    return wrapper

# Health server (optional, for uptime monitoring)
def start_health_server():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
    def run():
        server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), HealthHandler)
        server.serve_forever()
    threading.Thread(target=run, daemon=True).start()

@ensure_allowed
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "🤖 Save-Restricted Extractor Bot\n\n"
        "Commands:\n"
        "/start — show this message\n"
        "/login — login with phone and OTP\n"
        "/logout — remove session\n"
        "/save <t.me link> — fetch message or media\n"
        "/range <link> <start_id> <end_id> — fetch range\n"
        "/me — your status"
    )

@ensure_allowed
def me(update: Update, context: CallbackContext):
    sess = database.get_session(update.effective_user.id)
    status = "✅ Logged in" if sess else "❌ Not logged in"
    update.message.reply_text(status)

@ensure_allowed
def logout_cmd(update: Update, context: CallbackContext):
    database.delete_session(update.effective_user.id)
    update.message.reply_text("✅ Session removed.")

@ensure_allowed
def login_start(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("📞 Send phone number (+91...):")
    return ASK_PHONE

@ensure_allowed
def ask_code(update: Update, context: CallbackContext) -> int:
    phone = update.message.text.strip()
    context.user_data["phone"] = phone
    client = TelegramClient(StringSession(), int(os.environ.get("API_ID", 0)), os.environ.get("API_HASH", ""))
    try:
        client.connect()
        client.send_code_request(phone)
        context.user_data["client"] = client
        update.message.reply_text("🔐 Enter code (OTP):")
        return ASK_CODE
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
        client.disconnect()
        return ConversationHandler.END

@ensure_allowed
def finalize_login(update: Update, context: CallbackContext) -> int:
    code = update.message.text.strip()
    client: TelegramClient = context.user_data.get("client")
    phone = context.user_data.get("phone")
    if not client or not phone:
        update.message.reply_text("❌ Session lost. Try /login again.")
        return ConversationHandler.END
    try:
        client.sign_in(phone=phone, code=code)
        sess = client.session.save()
        database.save_session(update.effective_user.id, sess)
        update.message.reply_text("✅ Logged in and session saved.")
    except SessionPasswordNeededError:
        update.message.reply_text("🔒 2FA enabled. Send your password:")
        return ASK_PASSWORD
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        update.message.reply_text(f"❌ Error: {e}")
        client.disconnect()
        return ConversationHandler.END
    except RPCError as e:
        update.message.reply_text(f"❌ Telegram error: {e}")
        client.disconnect()
        return ConversationHandler.END
    finally:
        client.disconnect()
    return ConversationHandler.END

@ensure_allowed
def ask_password(update: Update, context: CallbackContext) -> int:
    password = update.message.text.strip()
    client: TelegramClient = context.user_data.get("client")
    if not client:
        update.message.reply_text("❌ Session lost. Try /login.")
        return ConversationHandler.END
    try:
        client.sign_in(password=password)
        sess = client.session.save()
        database.save_session(update.effective_user.id, sess)
        update.message.reply_text("✅ Logged in with 2FA.")
    except RPCError as e:
        update.message.reply_text(f"❌ Error: {e}")
    finally:
        client.disconnect()
        context.user_data.clear()
    return ConversationHandler.END

@ensure_allowed
def save_message(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /save <t.me link>")
        return
    link = context.args[0]
    target, msg_id = parse_message_link(link)
    if target is None:
        update.message.reply_text("❌ Invalid link.")
        return
    sess = database.get_session(update.effective_user.id)
    if not sess:
        update.message.reply_text("❌ Not logged in.")
        return
    client = TelegramClient(StringSession(sess), int(os.environ.get("API_ID", 0)), os.environ.get("API_HASH", ""))
    try:
        client.connect()
        msg = client.get_messages(target, ids=msg_id)
        if msg.media:
            fp = client.download_media(msg, file="downloads/")
            with open(fp, "rb") as f:
                update.message.reply_document(f)
        else:
            update.message.reply_text(msg.message or "(no text)")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    finally:
        client.disconnect()

@ensure_allowed
def save_range(update: Update, context: CallbackContext):
    if len(context.args) < 3:
        update.message.reply_text("Usage: /range <link> <start_id> <end_id>")
        return
    link = context.args[0]
    start_id = clamp_int(context.args[1])
    end_id = clamp_int(context.args[2])
    if None in (start_id, end_id):
        update.message.reply_text("Invalid range.")
        return
    target, _ = parse_message_link(link)
    sess = database.get_session(update.effective_user.id)
    if not sess:
        update.message.reply_text("❌ Not logged in.")
        return
    client = TelegramClient(StringSession(sess), int(os.environ.get("API_ID", 0)), os.environ.get("API_HASH", ""))
    try:
        client.connect()
        for mid in range(start_id, end_id + 1):
            msg = client.get_messages(target, ids=mid)
            if not msg:
                continue
            if msg.media:
                fp = client.download_media(msg, file="downloads/")
                with open(fp, "rb") as f:
                    update.message.reply_document(f)
            else:
                update.message.reply_text(msg.message or "(no text)")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    finally:
        client.disconnect()

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("❎ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

def main():
    # Initialize updater
    token = os.environ.get("BOT_TOKEN")
    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher

    # Add handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("me", me))
    dp.add_handler(CommandHandler("logout", logout_cmd))
    dp.add_handler(CommandHandler("save", save_message))
    dp.add_handler(CommandHandler("range", save_range))
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            ASK_PHONE: [MessageHandler(Filters.text & ~Filters.command, ask_code)],
            ASK_CODE: [MessageHandler(Filters.text & ~Filters.command, finalize_login)],
            ASK_PASSWORD: [MessageHandler(Filters.text & ~Filters.command, ask_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    dp.add_handler(conv_handler)

    # Start webhook
    updater.start_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        url_path=os.environ.get("BOT_TOKEN")
    )

    # Set webhook URL (no port)
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('BOT_TOKEN')}"
    updater.bot.set_webhook(webhook_url)
    print(f"Webhook set to {webhook_url}")

    updater.idle()

if __name__ == "__main__":
    main()
