import os
import logging
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait,
    UserNotParticipant,
    ChannelPrivate,
    PeerIdInvalid,
    MessageIdInvalid,
    PhoneNumberInvalid,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    PasswordHashInvalid
)

from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Ensure downloads directory exists
os.makedirs("downloads", exist_ok=True)

# Track active batch jobs and cancel requests
active_jobs = {}
cancel_requests = {}

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

app = Client("save-restricted-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ------------------- PROGRESS FUNCTIONS -------------------
_last_progress_update = {}

async def progress(current, total, status_msg):
    uid = status_msg.chat.id
    import time
    now = time.time()
    if uid not in _last_progress_update:
        _last_progress_update[uid] = 0
    if now - _last_progress_update[uid] < 1:
        return
    _last_progress_update[uid] = now
    percentage = current * 100 / total if total else 0
    await status_msg.edit_text(f"⬆️ Uploading: {percentage:.1f}%")

async def download_progress(current, total, status_msg):
    uid = status_msg.chat.id
    import time
    now = time.time()
    if uid not in _last_progress_update:
        _last_progress_update[uid] = 0
    if now - _last_progress_update[uid] < 1:
        return
    _last_progress_update[uid] = now
    percentage = current * 100 / total if total else 0
    await status_msg.edit_text(f"⬇️ Downloading: {percentage:.1f}%")

# ------------------- LOGIN HELPERS -------------------
def get_user_client(user_id: int):
    session_str = database.get_session(user_id)
    if not session_str:
        return None
    return Client(":memory:", session_string=session_str, api_id=API_ID, api_hash=API_HASH)

# ------------------- BOT COMMANDS -------------------
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
    sess = database.get_session(message.from_user.id)
    if sess:
        database.save_session(message.from_user.id, "")
        await message.reply_text("✅ Session removed.")
    else:
        await message.reply_text("❌ No active session found.")

# ------------------- SAVE SINGLE MESSAGE -------------------
@app.on_message(filters.command("save"))
@ensure_allowed
async def cmd_save(client: Client, message: Message):
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
        msg = await u.get_messages(target, msg_id)
        if not msg or msg.empty:
            return await status_msg.edit_text("⚠️ Message not found or deleted.")
        
        # handle media
        if msg.media:
            file_status = await status_msg.edit_text("📥 Downloading: 0%")
            file_path = await u.download_media(msg, file_name="downloads/", progress=download_progress, progress_args=(file_status,))
            if not file_path or not os.path.exists(file_path):
                return await file_status.edit_text("❌ Failed to download media.")
            
            caption = f"**Message {msg_id}:** {msg.caption or ''}"
            with open(file_path, 'rb') as f:
                if msg.photo:
                    await message.reply_photo(f, caption=caption, disable_web_page_preview=True, progress=progress, progress_args=(file_status,))
                elif msg.video:
                    thumb_path = None
                    if msg.video.thumbs:
                        thumb_file = msg.video.thumbs[-1].file_id
                        if thumb_file:
                            thumb_path = await u.download_media(thumb_file, file_name=f"downloads/thumb_{msg_id}.jpg")
                    await message.reply_video(f, caption=caption, thumb=thumb_path, progress=progress, progress_args=(file_status,))
                    if thumb_path and os.path.exists(thumb_path):
                        os.remove(thumb_path)
                elif msg.document:
                    await message.reply_document(f, caption=caption, progress=progress, progress_args=(file_status,))
                elif msg.audio:
                    await message.reply_audio(f, caption=caption, progress=progress, progress_args=(file_status,))
                elif msg.voice:
                    await message.reply_voice(f, caption=caption, progress=progress, progress_args=(file_status,))
                elif msg.animation:
                    await message.reply_animation(f, caption=caption, progress=progress, progress_args=(file_status,))
                elif msg.sticker:
                    await message.reply_sticker(f)
                else:
                    await message.reply_document(f, caption=caption, progress=progress, progress_args=(file_status,))
            os.remove(file_path)
            await file_status.delete()
        elif msg.text:
            await message.reply_text(f"📄 **Message {msg_id}:**\n\n{msg.text}", disable_web_page_preview=True)
    except FloodWait as e:
        await message.reply_text(f"⏳ Rate limit: wait {e.value}s.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")
    finally:
        try: await u.disconnect()
        except: pass

# ------------------- RANGE -------------------
@app.on_message(filters.command("range"))
@ensure_allowed
async def cmd_range(client: Client, message: Message):
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
        return await message.reply_text("❌ Range too large. Use smaller range.")

    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")
    
    uid = message.from_user.id
    active_jobs[uid] = True
    cancel_requests[uid] = False
    
    try:
        await u.connect()
        status_msg = await message.reply_text(f"📦 Fetching messages {start_id} to {end_id} from `{target}`...")
        success_count = 0
        for msg_id in range(start_id, end_id+1):
            if cancel_requests.get(uid):
                await status_msg.edit_text("⏹️ Batch cancelled by user.")
                break
            try:
                msg = await u.get_messages(target, msg_id)
                if not msg or msg.empty:
                    continue

                # handle media
                if msg.media:
                    file_status = await message.reply_text(f"📥 Downloading/⬆️ Uploading message {msg_id}: 0%")
                    file_path = await u.download_media(msg, file_name="downloads/", progress=download_progress, progress_args=(file_status,))
                    if not file_path or not os.path.exists(file_path):
                        await file_status.edit_text("❌ Download error.")
                        continue

                    caption = f"**Message {msg_id}:** {msg.caption or ''}"
                    with open(file_path, 'rb') as f:
                        if msg.photo:
                            await message.reply_photo(f, caption=caption, disable_web_page_preview=True, progress=progress, progress_args=(file_status,))
                        elif msg.video:
                            thumb_path = None
                            if msg.video.thumbs:
                                thumb_file = msg.video.thumbs[-1].file_id
                                if thumb_file:
                                    thumb_path = await u.download_media(thumb_file, file_name=f"downloads/thumb_{msg_id}.jpg")
                            await message.reply_video(f, caption=caption, thumb=thumb_path, progress=progress, progress_args=(file_status,))
                            if thumb_path and os.path.exists(thumb_path):
                                os.remove(thumb_path)
                        elif msg.document:
                            await message.reply_document(f, caption=caption, progress=progress, progress_args=(file_status,))
                        elif msg.audio:
                            await message.reply_audio(f, caption=caption, progress=progress, progress_args=(file_status,))
                        elif msg.voice:
                            await message.reply_voice(f, caption=caption, progress=progress, progress_args=(file_status,))
                        elif msg.animation:
                            await message.reply_animation(f, caption=caption, progress=progress, progress_args=(file_status,))
                        elif msg.sticker:
                            await message.reply_sticker(f)
                        else:
                            await message.reply_document(f, caption=caption, progress=progress, progress_args=(file_status,))
                    os.remove(file_path)
                    await file_status.delete()
                    success_count += 1
                elif msg.text:
                    await message.reply_text(f"📄 **{msg_id}:** {msg.text}", disable_web_page_preview=True)
                    success_count += 1
            except FloodWait as e:
                await status_msg.edit_text(f"⏳ Rate limit: waiting {e.value}s...")
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                logger.error(f"Message {msg_id} failed: {e}")
                continue
        await status_msg.edit_text(f"✅ Range complete! Total messages fetched: {success_count}")
    except Exception as e:
        await message.reply_text(f"❌ Range error: {e}")
    finally:
        active_jobs[uid] = False
        cancel_requests[uid] = False
        try: await u.disconnect()
        except: pass

# ------------------- BATCH -------------------
@app.on_message(filters.command("batch"))
@ensure_allowed
async def cmd_batch(client: Client, message: Message):
    uid = message.from_user.id
    await message.reply_text("🔢 Enter link and range (e.g. `https://t.me/channel 10-20`). Send `/cancel` to abort.")
    try:
        reply = await client.ask(uid, "", timeout=300)
        if not reply or not reply.text:
            return await message.reply_text("❌ No input received.")
        text = reply.text.strip()
        if text == "/cancel":
            return await message.reply_text("❌ Batch cancelled.")
        parts = text.split()
        if len(parts) != 2:
            return await message.reply_text("❌ Invalid format. Use `link start-end`.")
        link, range_part = parts
        fake = Message(
            message_id=message.message_id,
            date=message.date,
            chat=message.chat,
            from_user=message.from_user,
            text=f"/range {link} {range_part}"
        )
        await cmd_range(client, fake)
    except asyncio.TimeoutError:
        await message.reply_text("⏰ Timeout. Send `/batch` again.")
    except Exception as e:
        await message.reply_text(f"❌ Batch error: {e}")

# ------------------- CANCEL -------------------
@app.on_message(filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    uid = message.from_user.id
    if active_jobs.get(uid):
        cancel_requests[uid] = True
        await message.reply_text("⏹️ Cancelled.")
    else:
        await message.reply_text("❌ No active operation to cancel.")

# ------------------- MAIN -------------------
if __name__ == "__main__":
    start_health_server()
    logger.info("Starting Telegram Save-Restricted Bot...")
    app.run()
