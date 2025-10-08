import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, ParseMode
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
from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
from utils import parse_message_link, clamp_int

logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

ASK_PHONE, ASK_CODE, ASK_PASSWORD = range(3)

def ensure_allowed(func):
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        uid = getattr(update.effective_user, "id", None)
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            if update.message:
                update.message.reply_text("🚫 Not authorized.")
            return ConversationHandler.END
        return func(update, context, *args, **kwargs)
    return wrapper

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    def run():
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"Health server at 0.0.0.0:{PORT}")
        server.serve_forever()
    t = threading.Thread(target=run, daemon=True)
    t.start()

def build_help_text():
    return (
        "🤖 Save-Restricted Extractor Bot\n\n"
        "Commands:\n"
        "/start — show this help\n"
        "/login — authenticate with phone, OTP, and optional 2FA\n"
        "/logout — remove saved session\n"
        "/save <t.me link> — fetch one message (media or text)\n"
        "/range <t.me link> <start_id> <end_id> — fetch a range, in order\n"
        "/me — show login status\n"
    )

@ensure_allowed
def start(update: Update, context: CallbackContext):
    update.message.reply_text(build_help_text())

@ensure_allowed
def me(update: Update, context: CallbackContext):
    sess = database.get_session(update.effective_user.id)
    status = "✅ Logged in" if sess else "❌ Not logged in"
    update.message.reply_text(f"{status}")

@ensure_allowed
def logout_cmd(update: Update, context: CallbackContext):
    database.delete_session(update.effective_user.id)
    update.message.reply_text("✅ Session removed.")

@ensure_allowed
def login_start(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("📞 Send phone number in international format, e.g., +911234567890")
    return ASK_PHONE

@ensure_allowed
def ask_code(update: Update, context: CallbackContext) -> int:
    phone = update.message.text.strip()
    context.user_data["phone"] = phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        client.connect()
        client.send_code_request(phone)
        context.user_data["client"] = client
        update.message.reply_text("🔐 Enter the code you received (Telegram app or SMS).")
        return ASK_CODE
    except FloodWaitError as e:
        update.message.reply_text(f"⏳ Flood wait: retry in {e.seconds}s.")
    except RPCError as e:
        update.message.reply_text(f"❌ Telegram error: {e}")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    finally:
        # keep client open for next step only if stored
        if "client" not in context.user_data:
            client.disconnect()
    return ConversationHandler.END

@ensure_allowed
def finalize_login(update: Update, context: CallbackContext) -> int:
    code = update.message.text.strip()
    client: TelegramClient = context.user_data.get("client")
    phone = context.user_data.get("phone")
    if not client or not phone:
        update.message.reply_text("❌ Session lost. Start /login again.")
        return ConversationHandler.END
    try:
        client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        update.message.reply_text("🔒 2FA enabled. Send your password now.")
        return ASK_PASSWORD
    except PhoneCodeInvalidError:
        update.message.reply_text("❌ Invalid code. Use /login to retry.")
        client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END
    except PhoneCodeExpiredError:
        update.message.reply_text("⌛ Code expired. Use /login to retry.")
        client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END
    except RPCError as e:
        update.message.reply_text(f"❌ Telegram error: {e}")
        client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END

    sess = client.session.save()
    database.save_session(update.effective_user.id, sess)
    client.disconnect()
    context.user_data.clear()
    update.message.reply_text("✅ Logged in and session saved.")
    return ConversationHandler.END

@ensure_allowed
def ask_password(update: Update, context: CallbackContext) -> int:
    password = update.message.text.strip()
    client: TelegramClient = context.user_data.get("client")
    if not client:
        update.message.reply_text("❌ Session lost. Start /login again.")
        return ConversationHandler.END
    try:
        client.sign_in(password=password)
        sess = client.session.save()
        database.save_session(update.effective_user.id, sess)
        update.message.reply_text("✅ Logged in with 2FA and session saved.")
    except RPCError as e:
        update.message.reply_text(f"❌ Telegram error: {e}")
    finally:
        client.disconnect()
        context.user_data.clear()
    return ConversationHandler.END

def open_client_for(user_id: int):
    sess = database.get_session(user_id)
    if not sess:
        return None
    client = TelegramClient(StringSession(sess), API_ID, API_HASH)
    client.connect()
    return client

@ensure_allowed
def save_one(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /save <t.me link>")
        return
    target, msg_id = parse_message_link(context.args[0])
    if target is None:
        update.message.reply_text("❌ Unsupported link. Use https://t.me/username/123 or https://t.me/c/12345/678")
        return

    client = open_client_for(update.effective_user.id)
    if not client:
        update.message.reply_text("❌ Not logged in. Use /login first.")
        return

    try:
        msg = client.get_messages(target, ids=msg_id)
        if not msg:
            update.message.reply_text("⚠️ Message not found or no access.")
            return
        if msg.media:
            fp = client.download_media(msg, file="downloads/")
            if fp and os.path.exists(fp):
                with open(fp, "rb") as f:
                    update.message.reply_document(f, caption=f"ID {msg.id}")
            else:
                update.message.reply_text("⚠️ Failed to download media.")
        else:
            text = msg.message or "(no text)"
            update.message.reply_text(text[:4000])
    except RPCError as e:
        update.message.reply_text(f"❌ Telegram error: {e}")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    finally:
        client.disconnect()

@ensure_allowed
def save_range(update: Update, context: CallbackContext):
    if len(context.args) < 3:
        update.message.reply_text("Usage: /range <t.me link> <start_id> <end_id>")
        return
    link = context.args[0]
    start_id = clamp_int(context.args[1])
    end_id = clamp_int(context.args[2])
    if start_id is None or end_id is None or start_id <= 0 or end_id <= 0 or start_id > end_id:
        update.message.reply_text("❌ Invalid IDs. Provide positive integers with start_id ≤ end_id.")
        return

    target, _ = parse_message_link(link)
    if target is None:
        update.message.reply_text("❌ Unsupported link.")
        return

    client = open_client_for(update.effective_user.id)
    if not client:
        update.message.reply_text("❌ Not logged in. Use /login first.")
        return

    sent = 0
    try:
        update.message.reply_text(f"▶️ Starting fetch {start_id} → {end_id} ...")
        for mid in range(start_id, end_id + 1):
            try:
                msg = client.get_messages(target, ids=mid)
                if not msg:
                    continue
                if msg.media:
                    fp = client.download_media(msg, file="downloads/")
                    if fp and os.path.exists(fp):
                        with open(fp, "rb") as f:
                            update.message.reply_document(f, caption=f"ID {msg.id}")
                    else:
                        continue
                else:
                    text = msg.message or "(no text)"
                    update.message.reply_text(text[:4000])
                sent += 1
            except FloodWaitError as e:
                update.message.reply_text(f"⏳ Flood wait for {e.seconds}s at ID {mid}; pausing.")
                import time
                time.sleep(e.seconds + 1)
            except RPCError:
                continue
        update.message.reply_text(f"✅ Done. Sent {sent} messages.")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    finally:
        client.disconnect()

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("❎ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")
    if not API_ID or not API_HASH:
        raise ValueError("API_ID/API_HASH not set")

    start_health_server()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('me', me))
    dp.add_handler(CommandHandler('logout', logout_cmd))
    dp.add_handler(CommandHandler('save', save_one))
    dp.add_handler(CommandHandler('range', save_range))
    dp.add_handler(CommandHandler('cancel', cancel))

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

    # Webhook: Render routes traffic to PORT; use secret path = token
    updater.start_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN)
    # No external URL hardcoded. Render health checks hit our health server; Telegram will POST to our service’s public URL you configure at BotFather or by reverse proxy.
    logger.info(f"Bot webhook listening on 0.0.0.0:{PORT}/{BOT_TOKEN}")
    updater.idle()

if __name__ == "__main__":
    main()
