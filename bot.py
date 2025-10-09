# bot.py
import os
import time
import math
import logging
import tempfile
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple, Dict

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, UserNotParticipant, ChannelPrivate, PeerIdInvalid, MessageIdInvalid,
    PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid
)

# --- Your existing config + database modules (unchanged) ---
from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

# -------------- Logging --------------
logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------- Tunables --------------
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# concurrency and pacing
MAX_CONCURRENT_DOWNLOADS = 2          # how many simultaneous message downloads per user
MSG_DOWNLOAD_DELAY = 0.6              # pause between each download to avoid rate-limits (seconds)
CHUNK_PROCESS_SIZE = 50               # process ranges in chunks internally (keeps memory bounded)
PROGRESS_EDIT_INTERVAL = 0.5          # seconds between status message edits
LARGE_RANGE_WARNING = 5000            # warn if user requests a huge range

# -------------- Job control structures --------------
active_jobs: Dict[int, asyncio.Event] = {}      # user_id -> cancel_event
job_locks: Dict[int, asyncio.Semaphore] = {}    # per-user concurrency limit

# -------------- Utils --------------
def ensure_allowed(func):
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id if message.from_user else None
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await message.reply_text("🚫 Not authorized.")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

def parse_link(link: str) -> Tuple[Optional[object], Optional[int]]:
    """Return (target, msg_id) where target is username or -100... chat id"""
    if not link:
        return None, None
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

def parse_range(text: str) -> Tuple[Optional[int], Optional[int]]:
    if not text:
        return None, None
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
        def log_message(self, *args):
            pass
    def run():
        try:
            server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
            logger.info(f"Health server at 0.0.0.0:{PORT}")
            server.serve_forever()
        except Exception as e:
            logger.exception("Health server error")
    threading.Thread(target=run, daemon=True).start()

# -------------- Client (bot) --------------
app = Client("save-restricted-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# -------------- Helpers for user auth client --------------
def get_session_string_for_user(user_id: int) -> Optional[str]:
    """Get the saved session string from your database (as before)."""
    return database.get_session(user_id)

def get_user_client(user_id: int) -> Optional[Client]:
    """
    Create a Pyrogram Client for the user using stored session_string.
    We prefer session_string to keep it portable across hosts.
    """
    session_str = get_session_string_for_user(user_id)
    if not session_str:
        return None
    # Use a named session so Pyrogram can reuse auth/DC information across runs
    # Using session_string allows restoring the account without needing a physical file
    return Client(f"user_{user_id}", api_id=API_ID, api_hash=API_HASH, session_string=session_str)

# -------------- Progress callback factories --------------
def make_progress_callback(status_msg: Message):
    """Return a coroutine function suitable as Pyrogram's progress callback.
       It edits the provided status_msg, throttling edits to avoid spamming.
    """
    last_call = {"t": 0.0}

    async def _progress(current, total, *args):
        now = time.time()
        if now - last_call["t"] < PROGRESS_EDIT_INTERVAL and current != total:
            return  # throttle edits
        last_call["t"] = now
        try:
            pct = (current * 100 / total) if total else 0
            await status_msg.edit_text(f"⬆️ Uploading: {pct:.1f}%")
        except Exception:
            pass

    return _progress

def make_download_progress(status_msg: Message):
    last_call = {"t": 0.0}
    async def _progress(current, total, *args):
        now = time.time()
        if now - last_call["t"] < PROGRESS_EDIT_INTERVAL and current != total:
            return
        last_call["t"] = now
        try:
            pct = (current * 100 / total) if total else 0
            await status_msg.edit_text(f"📥 Downloading: {pct:.1f}%")
        except Exception:
            pass
    return _progress

# -------------- Commands --------------
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
        "• `/batch` — interactive range fetch (enter `link start-end`)\n"
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

# Login flow (keeps your prior flows but tidier)
@app.on_message(filters.command("login"))
@ensure_allowed
async def cmd_login(bot: Client, message: Message):
    user = message.from_user
    uid = user.id
    if database.get_session(uid):
        await message.reply_text("✅ Already logged in. Use `/logout` to reset.")
        return

    try:
        phone_msg = await bot.ask(uid,
            "📞 Send your phone number with country code (e.g. +919876543210)\n\nSend `/cancel` to abort.",
            timeout=300
        )
        if phone_msg.text == "/cancel":
            return await phone_msg.reply("❌ Login cancelled.")
        phone = phone_msg.text.strip()
        if not (phone.startswith("+") and len(phone) >= 8):
            return await phone_msg.reply("❌ Invalid phone format.")

        # Create temporary client for authentication
        temp_client = Client(f"temp_login_{uid}", api_id=API_ID, api_hash=API_HASH)
        await temp_client.connect()

        await phone_msg.reply("📤 Sending OTP...")

        try:
            code = await temp_client.send_code(phone)
        except PhoneNumberInvalid:
            await phone_msg.reply("❌ Invalid phone number.")
            await temp_client.disconnect()
            return
        except FloodWait as e:
            await phone_msg.reply(f"⏳ Too many attempts. Wait {e.value} seconds.")
            await temp_client.disconnect()
            return
        except Exception as e:
            await phone_msg.reply(f"❌ Error sending code: {e}")
            await temp_client.disconnect()
            return

        code_msg = await bot.ask(uid, "🔐 Enter the OTP (or `/cancel`):", timeout=300)
        if code_msg.text == "/cancel":
            await code_msg.reply("❌ Login cancelled.")
            await temp_client.disconnect()
            return
        phone_code = code_msg.text.replace(" ", "").replace("-", "")

        try:
            await temp_client.sign_in(phone, code.phone_code_hash, phone_code)
        except PhoneCodeInvalid:
            await code_msg.reply("❌ Invalid OTP code.")
            await temp_client.disconnect()
            return
        except PhoneCodeExpired:
            await code_msg.reply("❌ OTP expired. Try again.")
            await temp_client.disconnect()
            return
        except SessionPasswordNeeded:
            pwd_msg = await bot.ask(uid, "🔒 2FA enabled. Send your password (or `/cancel`):", timeout=300)
            if pwd_msg.text == "/cancel":
                await pwd_msg.reply("❌ Login cancelled.")
                await temp_client.disconnect()
                return
            try:
                await temp_client.check_password(password=pwd_msg.text)
            except PasswordHashInvalid:
                await pwd_msg.reply("❌ Invalid 2FA password.")
                await temp_client.disconnect()
                return

        # Export session string and save
        session_str = await temp_client.export_session_string()
        await temp_client.disconnect()
        database.save_session(uid, session_str)
        await bot.send_message(uid, "✅ Logged in and session saved. You can now use `/save`, `/range`, `/batch`.")
    except asyncio.TimeoutError:
        await message.reply_text("⏰ Timeout. Use `/login` again.")
    except Exception as e:
        await message.reply_text(f"❌ Login error: {e}")

# Core single-save command with progress and thumbnail handling
@app.on_message(filters.command("save"))
@ensure_allowed
async def cmd_save(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/save <telegram_link>`")
    link = message.command[1]
    target, msg_id = parse_link(link)
    if not target:
        return await message.reply_text(f"❌ Cannot parse link: `{link}`")
    uclient = get_user_client(message.from_user.id)
    if not uclient:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")
    try:
        await uclient.connect()
        status_msg = await message.reply_text(f"🔍 Fetching message {msg_id} from `{target}`...")
        msg = await uclient.get_messages(target, msg_id)
        if not msg or msg.empty:
            return await status_msg.edit_text("⚠️ Message not found or not accessible.")
        if msg.media:
            # Download
            download_status = await status_msg.edit_text("📥 Downloading: 0%")
            dp = make_download_progress(download_status)
            # include retry wrapper for upload.GetFile timeout
            file_path = None
            for attempt in range(4):
                try:
                    file_path = await uclient.download_media(msg, file_name=DOWNLOAD_DIR + "/", progress=dp, progress_args=())
                    break
                except Exception as e:
                    logger.warning(f"download attempt {attempt+1} failed: {e}")
                    await asyncio.sleep(1 + attempt * 2)
            if not file_path or not os.path.exists(file_path):
                return await status_msg.edit_text("❌ Failed to download media after retries.")
            # Upload (bot client sends to the chat)
            await download_status.edit_text("⬆️ Uploading: 0%")
            progress_cb = make_progress_callback(download_status)
            caption = msg.caption or ""
            # Preserve thumbnails for video
            thumb_path = None
            try:
                if msg.video and getattr(msg.video, "thumbs", None):
                    thumb = msg.video.thumbs[-1]
                    thumb_path = await uclient.download_media(thumb.file_id, file_name=DOWNLOAD_DIR + f"/thumb_{msg.id}.jpg")
            except Exception:
                thumb_path = None
            # Use the correct reply_* based on msg type (Pyrogram will detect file type by the file object)
            with open(file_path, "rb") as fp:
                if msg.photo:
                    await message.reply_photo(fp, caption=caption, disable_web_page_preview=True, progress=progress_cb, progress_args=())
                elif msg.video:
                    await message.reply_video(fp, caption=caption, thumb=thumb_path, progress=progress_cb, progress_args=())
                elif msg.document:
                    await message.reply_document(fp, caption=caption, progress=progress_cb, progress_args=())
                elif msg.audio:
                    await message.reply_audio(fp, caption=caption, progress=progress_cb, progress_args=())
                elif msg.voice:
                    await message.reply_voice(fp, caption=caption, progress=progress_cb, progress_args=())
                elif msg.animation:
                    await message.reply_animation(fp, caption=caption, progress=progress_cb, progress_args=())
                elif msg.sticker:
                    await message.reply_sticker(fp)
                else:
                    await message.reply_document(fp, caption=caption, progress=progress_cb, progress_args=())
            # cleanup
            try:
                os.remove(file_path)
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except Exception:
                pass
            await download_status.delete()
            await status_msg.delete()
        elif msg.text:
            await status_msg.delete()
            await message.reply_text(f"📄 {msg.text}", disable_web_page_preview=True)
        else:
            await status_msg.edit_text("⚠️ Message has no transferable content.")
    except FloodWait as e:
        await message.reply_text(f"⏳ Rate limited. Wait {e.value} seconds.")
    except UserNotParticipant:
        await message.reply_text("❌ Not a member of that chat.")
    except ChannelPrivate:
        await message.reply_text("❌ Private channel. Join first.")
    except PeerIdInvalid:
        await message.reply_text("❌ Invalid chat/link.")
    except MessageIdInvalid:
        await message.reply_text("❌ Invalid message id.")
    except Exception as e:
        logger.exception("save error")
        await message.reply_text(f"❌ Error: {e}")
    finally:
        try:
            await uclient.disconnect()
        except:
            pass

# Range/batch processing core worker
async def process_message_and_forward(uclient: Client, bot_client: Client, target, msg_id: int, dest_chat_id: int, cancel_event: asyncio.Event):
    """Downloads a single msg from target via uclient and forwards to dest_chat_id via bot_client.
       Returns (True/False, reason_str)
    """
    if cancel_event.is_set():
        return False, "cancelled"
    try:
        msg = await uclient.get_messages(target, msg_id)
        if not msg or msg.empty:
            return False, "not_found"
        if msg.media:
            status_msg = await bot_client.send_message(dest_chat_id, f"📥 Downloading/⬆️ Uploading message {msg_id}: 0%")
            dp = make_download_progress(status_msg)
            # retry download on transient timeouts
            file_path = None
            for attempt in range(4):
                try:
                    file_path = await uclient.download_media(msg, file_name=DOWNLOAD_DIR + "/", progress=dp, progress_args=())
                    break
                except Exception as e:
                    logger.warning(f"[{msg_id}] download attempt {attempt+1} failed: {e}")
                    await asyncio.sleep(1 + attempt * 2)
                    if cancel_event.is_set():
                        await status_msg.edit_text("⏹️ Cancelled")
                        return False, "cancelled"
            if not file_path or not os.path.exists(file_path):
                await status_msg.edit_text("❌ Download failed")
                return False, "download_failed"
            # prepare upload
            await status_msg.edit_text("⬆️ Uploading: 0%")
            upload_cb = make_progress_callback(status_msg)
            caption = msg.caption or ""
            thumb_path = None
            try:
                if msg.video and getattr(msg.video, "thumbs", None):
                    thumb = msg.video.thumbs[-1]
                    thumb_path = await uclient.download_media(thumb.file_id, file_name=DOWNLOAD_DIR + f"/thumb_{msg.id}.jpg")
            except Exception:
                thumb_path = None
            # send
            try:
                with open(file_path, "rb") as fp:
                    if msg.photo:
                        await bot_client.send_photo(dest_chat_id, fp, caption=caption, disable_web_page_preview=True, progress=upload_cb, progress_args=())
                    elif msg.video:
                        await bot_client.send_video(dest_chat_id, fp, caption=caption, thumb=thumb_path, progress=upload_cb, progress_args=())
                    elif msg.document:
                        await bot_client.send_document(dest_chat_id, fp, caption=caption, progress=upload_cb, progress_args=())
                    elif msg.audio:
                        await bot_client.send_audio(dest_chat_id, fp, caption=caption, progress=upload_cb, progress_args=())
                    elif msg.voice:
                        await bot_client.send_voice(dest_chat_id, fp, caption=caption, progress=upload_cb, progress_args=())
                    elif msg.animation:
                        await bot_client.send_animation(dest_chat_id, fp, caption=caption, progress=upload_cb, progress_args=())
                    elif msg.sticker:
                        await bot_client.send_sticker(dest_chat_id, fp)
                    else:
                        await bot_client.send_document(dest_chat_id, fp, caption=caption, progress=upload_cb, progress_args=())
            except FloodWait as e:
                await status_msg.edit_text(f"⏳ Rate limited: wait {e.value}s")
                await asyncio.sleep(e.value + 1)
                return False, "floodwait"
            except Exception as e:
                logger.exception("upload error")
                await status_msg.edit_text(f"❌ Upload failed: {e}")
                return False, "upload_failed"
            finally:
                # cleanup
                try:
                    os.remove(file_path)
                except:
                    pass
                if thumb_path:
                    try:
                        os.remove(thumb_path)
                    except:
                        pass
                try:
                    await status_msg.delete()
                except:
                    pass
            return True, "ok"
        elif msg.text:
            # simple text, forward as text
            await bot_client.send_message(dest_chat_id, f"📄 {msg.text}", disable_web_page_preview=True)
            return True, "text_ok"
        else:
            return False, "no_content"
    except FloodWait as e:
        logger.warning("FloodWait in worker")
        return False, "floodwait"
    except Exception as e:
        logger.exception("process error")
        return False, "error"

@app.on_message(filters.command("range"))
@ensure_allowed
async def cmd_range(client: Client, message: Message):
    if len(message.command) < 3:
        return await message.reply_text("Usage: `/range <link> <start-end>` ; example: `/range https://t.me/channel 5-15`")
    link = message.command[1]
    range_text = message.command[2]
    target, _ = parse_link(link)
    if not target:
        return await message.reply_text("❌ Invalid link format.")
    start_id, end_id = parse_range(range_text)
    if start_id is None:
        return await message.reply_text("❌ Invalid range.")
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    total = end_id - start_id + 1
    if total <= 0:
        return await message.reply_text("❌ Empty range.")
    if total > LARGE_RANGE_WARNING:
        await message.reply_text(f"⚠️ Large range requested ({total} messages). Processing will proceed but may take time.")
    # check login
    uclient = get_user_client(message.from_user.id)
    if not uclient:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")
    uid = message.from_user.id
    # create per-user control primitives
    cancel_event = asyncio.Event()
    active_jobs[uid] = cancel_event
    job_locks[uid] = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    try:
        await uclient.connect()
        status_msg = await message.reply_text(f"📦 Starting range {start_id}-{end_id} ({total} messages). Press /cancel to stop.")
        succeeded = 0
        failed = 0
        # Process in internal chunks to keep memory and pacing controlled
        current = start_id
        while current <= end_id:
            if cancel_event.is_set():
                await status_msg.edit_text("⏹️ Batch cancelled by user.")
                break
            chunk_end = min(current + CHUNK_PROCESS_SIZE - 1, end_id)
            # process messages in chunk sequentially (you can parallelize within semaphore if desired)
            for msg_id in range(current, chunk_end + 1):
                if cancel_event.is_set():
                    break
                # respect pacing
                await asyncio.sleep(MSG_DOWNLOAD_DELAY)
                ok, reason = await process_message_and_forward(uclient, client, target, msg_id, message.chat.id, cancel_event)
                if ok:
                    succeeded += 1
                else:
                    failed += 1
                # update progress summary occasionally
                if (msg_id - start_id + 1) % 10 == 0:
                    await status_msg.edit_text(f"📦 Progress: {msg_id}/{end_id}\n✅ {succeeded} | ❌ {failed}")
            current = chunk_end + 1
            # small pause between chunks to avoid hitting DC edge cases
            await asyncio.sleep(0.8)
        if not cancel_event.is_set():
            await status_msg.edit_text(f"✅ Range complete!\nDownloaded: {succeeded}\nFailed: {failed}\nRange: {start_id}-{end_id}")
    except Exception as e:
        logger.exception("range error")
        await message.reply_text(f"❌ Range error: {e}")
    finally:
        # cleanup
        active_jobs.pop(uid, None)
        job_locks.pop(uid, None)
        try:
            await uclient.disconnect()
        except:
            pass

@app.on_message(filters.command("batch"))
@ensure_allowed
async def cmd_batch(client: Client, message: Message):
    uid = message.from_user.id
    await message.reply_text("🔢 Enter `link start-end` (example: `https://t.me/channel 4-20`). Send `/cancel` to abort.")
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
        # call range handler
        fake_msg = Message(
            message_id=message.message_id,
            date=message.date,
            chat=message.chat,
            from_user=message.from_user,
            text=f"/range {link} {range_part}"
        )
        # Directly call the handler function to reuse logic
        await cmd_range(client, fake_msg)
    except asyncio.TimeoutError:
        await message.reply_text("⏰ Timeout. Send /batch again.")
    except Exception as e:
        logger.exception("batch error")
        await message.reply_text(f"❌ Batch error: {e}")

@app.on_message(filters.command("cancel"))
@ensure_allowed
async def cmd_cancel(client: Client, message: Message):
    uid = message.from_user.id
    ev = active_jobs.get(uid)
    if ev and not ev.is_set():
        ev.set()
        await message.reply_text("⏹️ Cancel requested. Stopping current job...")
    else:
        await message.reply_text("❌ No active operation to cancel.")

# -------------- Start --------------
if __name__ == "__main__":
    start_health_server()
    logger.info("Starting Telegram Save-Restricted Bot...")
    app.run()
