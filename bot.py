import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import (
    ApiIdInvalid, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid, UsernameNotOccupied, FloodWait
)

from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

def ensure_allowed(func):
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id if message.from_user else None
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await message.reply_text("🚫 Not authorized.")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

def parse_link(link: str):
    link = link.strip().rstrip("/")
    if "://t.me/c/" in link:
        # https://t.me/c/<short>/<id>
        parts = link.split("/")
        if len(parts) >= 6:
            short = parts[4]
            mid = int(parts[5].split("?")[0])
            chat_id = int(f"-100{short}")
            return chat_id, mid
    elif "://t.me/" in link:
        parts = link.split("/")
        if len(parts) >= 5:
            username = parts[3]
            mid = int(parts[4].split("?")[0])
            return username, mid
    return None, None

def start_health_server():
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
    def run():
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"Health server at 0.0.0.0:{PORT}")
        server.serve_forever()
    threading.Thread(target=run, daemon=True).start()

app = Client(
    "save-restricted-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.command(["start"]))
@ensure_allowed
async def cmd_start(client: Client, message: Message):
    await message.reply_text(
        "🤖 Save-Restricted Extractor Bot\n\n"
        "/login — login with phone, OTP, and optional 2FA\n"
        "/logout — remove saved session\n"
        "/save <t.me link> — fetch one message\n"
        "/range <t.me link> <start_id> <end_id> — fetch range\n"
        "/me — show login status"
    )

@app.on_message(filters.command(["me"]))
@ensure_allowed
async def cmd_me(client: Client, message: Message):
    sess = database.get_session(message.from_user.id)
    await message.reply_text("✅ Logged in" if sess else "❌ Not logged in")

@app.on_message(filters.command(["logout"]))
@ensure_allowed
async def cmd_logout(client: Client, message: Message):
    if database.get_session(message.from_user.id):
        database.save_session(message.from_user.id, None)
    await message.reply_text("✅ Session removed.")

@app.on_message(filters.command(["login"]))
@ensure_allowed
async def cmd_login(bot: Client, message: Message):
    if database.get_session(message.from_user.id):
        await message.reply_text("Already logged in. Use /logout to reset.")
        return
    user_id = message.from_user.id
    # Ask phone
    phone_msg = await bot.ask(user_id, "📞 Send phone number with country code, e.g., +919999999999")
    if phone_msg.text == "/cancel":
        return await phone_msg.reply("Cancelled.")
    phone = phone_msg.text

    u = Client(":memory:", api_id=API_ID, api_hash=API_HASH)
    await u.connect()
    await phone_msg.reply("Sending OTP...")
    try:
        code = await u.send_code(phone)
        code_msg = await bot.ask(user_id, "Enter OTP as '1 2 3 4 5' (spaces). Use /cancel to cancel.", filters=filters.text, timeout=600)
    except PhoneNumberInvalid:
        await phone_msg.reply("Invalid phone number.")
        await u.disconnect()
        return
    if code_msg.text == "/cancel":
        await code_msg.reply("Cancelled.")
        await u.disconnect()
        return
    try:
        phone_code = code_msg.text.replace(" ", "")
        await u.sign_in(phone, code.phone_code_hash, phone_code)
    except PhoneCodeInvalid:
        await code_msg.reply("Invalid OTP.")
        await u.disconnect()
        return
    except PhoneCodeExpired:
        await code_msg.reply("OTP expired.")
        await u.disconnect()
        return
    except SessionPasswordNeeded:
        pwd_msg = await bot.ask(user_id, "2FA enabled. Send your password. /cancel to cancel.", filters=filters.text, timeout=300)
        if pwd_msg.text == "/cancel":
            await pwd_msg.reply("Cancelled.")
            await u.disconnect()
            return
        try:
            await u.check_password(password=pwd_msg.text)
        except PasswordHashInvalid:
            await pwd_msg.reply("Invalid password.")
            await u.disconnect()
            return

    # Save string session
    s = await u.export_session_string()
    await u.disconnect()
    database.save_session(user_id, s)
    await bot.send_message(user_id, "✅ Logged in and session saved.\nIf you get AUTH KEY errors later, /logout then /login again.")

def get_user_client(user_id: int):
    s = database.get_session(user_id)
    if not s:
        return None
    u = Client(":memory:", session_string=s, api_id=API_ID, api_hash=API_HASH)
    return u

@app.on_message(filters.command(["save"]))
@ensure_allowed
async def cmd_save(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /save <t.me link>")
    target, mid = parse_link(message.command[1])
    if target is None:
        return await message.reply_text("❌ Unsupported link.")
    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use /login first.")
    await u.connect()
    try:
        msg = await u.get_messages(target, mid)
        if not msg:
            return await message.reply_text("⚠️ Message not found or no access.")
        if msg.media:
            path = await u.download_media(msg)
            if path:
                with open(path, "rb") as f:
                    await message.reply_document(f)
        else:
            await message.reply_text(msg.text or "(no text)")
    except FloodWait as e:
        await message.reply_text(f"⏳ Flood wait: {e.value}s")
    finally:
        await u.disconnect()

@app.on_message(filters.command(["range"]))
@ensure_allowed
async def cmd_range(client: Client, message: Message):
    if len(message.command) < 4:
        return await message.reply_text("Usage: /range <link> <start_id> <end_id>")
    link, s_id, e_id = message.command[1], message.command[2], message.command[3]
    try:
        start_id, end_id = int(s_id), int(e_id)
    except:
        return await message.reply_text("start_id and end_id must be integers.")
    if start_id > end_id or end_id - start_id > 500:
        return await message.reply_text("Invalid range. Max 500 per batch.")
    target, _ = parse_link(link)
    if target is None:
        return await message.reply_text("❌ Unsupported link.")

    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use /login first.")
    await u.connect()
    sent = 0
    try:
        await message.reply_text(f"▶️ Fetching {start_id} → {end_id} ...")
        for mid in range(start_id, end_id + 1):
            try:
                msg = await u.get_messages(target, mid)
                if not msg:
                    continue
                if msg.media:
                    path = await u.download_media(msg)
                    if path:
                        with open(path, "rb") as f:
                            await message.reply_document(f)
                else:
                    await message.reply_text(msg.text or "(no text)")
                sent += 1
            except FloodWait as e:
                await message.reply_text(f"⏳ Flood wait {e.value}s at {mid}")
                import asyncio
                await asyncio.sleep(e.value + 1)
        await message.reply_text(f"✅ Done. Sent {sent} messages.")
    finally:
        await u.disconnect()

if __name__ == "__main__":
    start_health_server()
    app.run()
