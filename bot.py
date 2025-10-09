import os
import logging
import threading
import asyncio
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, UserNotParticipant, ChannelPrivate, PeerIdInvalid, MessageIdInvalid
)
from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

os.makedirs("downloads", exist_ok=True)
active_jobs = {}
cancel_requests = {}

# --------------------- Helpers ---------------------
def ensure_allowed(func):
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id if message.from_user else None
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await message.reply_text("🚫 Not authorized.")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

def parse_link(link: str):
    if not link: return None, None
    link = link.strip().rstrip("/")
    if "/c/" in link:
        parts = link.split("/")
        if len(parts) >= 6:
            try:
                short_id = parts[4]
                msg_id = int(parts[5].split("?")[0].split("-")[0])
                return int(f"-100{short_id}"), msg_id
            except:
                pass
    elif "t.me/" in link:
        parts = link.split("/")
        if len(parts) >= 5:
            try:
                username = parts[3]
                if username.startswith("@"):
                    username = username[1:]
                msg_id = int(parts[4].split("?")[0].split("-")[0])
                return username, msg_id
            except:
                pass
    return None, None

def parse_range(text: str):
    text = text.strip().replace(" ", "")
    if "-" in text:
        try:
            a, b = text.split("-")
            return int(a), int(b)
        except:
            return None, None
    try:
        n = int(text)
        return n, n
    except:
        return None, None

def start_health_server():
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass
    def run():
        try:
            server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
            logger.info(f"Health server at 0.0.0.0:{PORT}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"Health server error: {e}")
    threading.Thread(target=run, daemon=True).start()

def get_user_client(user_id: int):
    session_str = database.get_session(user_id)
    if not session_str:
        return None
    return Client(":memory:", session_string=session_str, api_id=API_ID, api_hash=API_HASH)

# --------------------- Bot ---------------------
app = Client("save-restricted-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --------------------- Login & Session ---------------------
@app.on_message(filters.command("start"))
@ensure_allowed
async def cmd_start(client: Client, message: Message):
    await message.reply_text(
        "🤖 **Save-Restricted Extractor Bot**\n\n"
        "**Commands:**\n"
        "• `/login` — login with phone, OTP, and 2FA\n"
        "• `/logout` — remove saved session\n"
        "• `/save <link>` — fetch one message/media\n"
        "• `/range <link> <start-end>` — fetch range (e.g. 100-110)\n"
        "• `/batch` — interactive range fetch\n"
        "• `/me` — show login status\n"
        "• `/cancel` — cancel current batch\n\n"
        "**Link formats:**\n"
        "• Public: `https://t.me/channel/123`\n"
        "• Private: `https://t.me/c/1234567/123`\n\n"
        "**Note:** You must be a member of private groups/channels."
    )

@app.on_message(filters.command("me"))
@ensure_allowed
async def cmd_me(client: Client, message: Message):
    sess = database.get_session(message.from_user.id)
    status = "✅ Logged in" if sess else "❌ Not logged in"
    await message.reply_text(f"**Status:** {status}")

@app.on_message(filters.command("logout"))
@ensure_allowed
async def cmd_logout(client: Client, message: Message):
    if database.get_session(message.from_user.id):
        database.save_session(message.from_user.id, "")
        await message.reply_text("✅ Session removed.")
    else:
        await message.reply_text("❌ No active session found.")

# --------------------- Media Upload Helpers ---------------------
async def upload_media_memory(msg, client, chat_id):
    if not msg.media:
        return False
    file_buffer = BytesIO()
    await client.download_media(msg, file_name=file_buffer)
    file_buffer.seek(0)
    caption = f"**Message {msg.message_id}:** {msg.caption or ''}"
    try:
        if msg.photo:
            await client.send_photo(chat_id, file_buffer, caption=caption, disable_web_page_preview=True)
        elif msg.video:
            thumb_buffer = None
            if msg.video.thumbs:
                thumb_buffer = BytesIO()
                await client.download_media(msg.video.thumbs[-1].file_id, file_name=thumb_buffer)
                thumb_buffer.seek(0)
            await client.send_video(chat_id, file_buffer, caption=caption, thumb=thumb_buffer)
            if thumb_buffer:
                thumb_buffer.close()
        elif msg.document:
            await client.send_document(chat_id, file_buffer, caption=caption)
        elif msg.audio:
            await client.send_audio(chat_id, file_buffer, caption=caption)
        elif msg.voice:
            await client.send_voice(chat_id, file_buffer, caption=caption)
        elif msg.animation:
            await client.send_animation(chat_id, file_buffer, caption=caption)
        elif msg.sticker:
            await client.send_sticker(chat_id, file_buffer)
        else:
            await client.send_document(chat_id, file_buffer, caption=caption)
    finally:
        file_buffer.close()
    return True

# --------------------- /save Command ---------------------
@app.on_message(filters.command("save"))
@ensure_allowed
async def cmd_save(client, message):
    if len(message.command) < 2:
        return await message.reply_text("**Usage:** `/save <telegram_link>`")
    link = message.command[1]
    target, msg_id = parse_link(link)
    if target is None:
        return await message.reply_text(f"❌ Invalid link: {link}")
    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")
    try:
        await u.connect()
        status_msg = await message.reply_text(f"🔍 Fetching message {msg_id}...")
        try:
            msg = await u.get_messages(target, msg_id)
        except MessageIdInvalid:
            await status_msg.edit_text("⚠️ Message not found.")
            return
        if not msg:
            await status_msg.edit_text("⚠️ Message empty.")
            return
        if msg.media:
            await status_msg.edit_text("📥 Processing media...")
            await upload_media_memory(msg, client, message.chat.id)
            await status_msg.delete()
        elif msg.text:
            await status_msg.delete()
            await message.reply_text(f"📄 **Message {msg_id}:**\n\n{msg.text}", disable_web_page_preview=True)
    finally:
        await u.disconnect()

# --------------------- /range Command ---------------------
@app.on_message(filters.command("range"))
@ensure_allowed
async def cmd_range(client, message):
    if len(message.command) < 3:
        return await message.reply_text("**Usage:** `/range <link> <start-end>`")
    link = message.command[1]
    range_text = message.command[2]
    target, _ = parse_link(link)
    if target is None:
        return await message.reply_text(f"❌ Invalid link: {link}")
    start_id, end_id = parse_range(range_text)
    if start_id is None:
        return await message.reply_text(f"❌ Invalid range: {range_text}")
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    if end_id - start_id > 1000:
        return await message.reply_text("❌ Range too large. Please use a smaller range.")

    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")

    uid = message.from_user.id
    active_jobs[uid] = True
    cancel_requests[uid] = False

    try:
        await u.connect()
        status_msg = await message.reply_text(f"📦 Fetching messages {start_id}-{end_id} from `{target}`...")
        msg_ids = list(range(start_id, end_id + 1))

        semaphore = asyncio.Semaphore(10)
        async def fetch_upload(msg_id):
            async with semaphore:
                if cancel_requests.get(uid):
                    return "cancelled"
                try:
                    msg = await u.get_messages(target, msg_id)
                    if not msg:
                        return "missing"
                    if msg.media:
                        await upload_media_memory(msg, client, message.chat.id)
                        return "success"
                    elif msg.text:
                        await client.send_message(message.chat.id, f"📄 **{msg_id}:** {msg.text}", disable_web_page_preview=True)
                        return "success"
                    return "skipped"
                except MessageIdInvalid:
                    return "missing"
                except Exception:
                    return "error"

        results = await asyncio.gather(*[fetch_upload(mid) for mid in msg_ids])
        success_count = results.count("success")
        error_count = results.count("error")
        missing_count = results.count("missing")

        if cancel_requests.get(uid):
            await status_msg.edit_text("⏹️ Batch cancelled by user.")
        else:
            await status_msg.edit_text(f"✅ **Range complete!**\nSuccess: {success_count}\nErrors: {error_count}\nMissing: {missing_count}")
    finally:
        active_jobs[uid] = False
        cancel_requests[uid] = False
        await u.disconnect()

# --------------------- /batch Command ---------------------
@app.on_message(filters.command("batch"))
@ensure_allowed
async def cmd_batch(client, message):
    uid = message.from_user.id
    await message.reply_text("🔢 **Send link and range** (e.g., `https://t.me/channel 10-20`). Send /cancel to abort.")
    try:
        reply = await client.ask(uid, "", timeout=300)
        if not reply or not reply.text:
            return await message.reply_text("❌ No input received.")
        if reply.text == "/cancel":
            return await message.reply_text("❌ Batch cancelled.")
        parts = reply.text.strip().split()
        if len(parts) != 2:
            return await message.reply_text("❌ Invalid format. Use `link start-end`.")
        link, range_part = parts
        fake_msg = Message(
            message_id=message.message_id,
            date=message.date,
            chat=message.chat,
            from_user=message.from_user,
            text=f"/range {link} {range_part}"
        )
        await cmd_range(client, fake_msg)
    except asyncio.TimeoutError:
        await message.reply_text("⏰ Timeout. Send `/batch` again.")
    except Exception as e:
        await message.reply_text(f"❌ Batch error: {e}")

# --------------------- /cancel Command ---------------------
@app.on_message(filters.command("cancel"))
async def cmd_cancel(client, message):
    uid = message.from_user.id
    if active_jobs.get(uid):
        cancel_requests[uid] = True
        await message.reply_text("⏹️ Cancelled.")
    else:
        await message.reply_text("❌ No active operation to cancel.")

# --------------------- Run ---------------------
if __name__ == "__main__":
    start_health_server()
    logger.info("Starting Telegram Save-Restricted Bot...")
    app.run()
