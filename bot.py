# bot.py  — high-performance version tuned for Koyeb free
import os
import time
import math
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple, Dict, Any

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, UserNotParticipant, ChannelPrivate, PeerIdInvalid, MessageIdInvalid,
    PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid
)

# local modules (unchanged)
from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

# ===== Logging =====
logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== Tunables (tune for speed) =====
DOWNLOAD_DIR = "downloads"
SESSION_DIR = "sessions"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

# Choose conservative defaults for Koyeb free. Increase `MAX_PARALLEL` if you move to stronger host.
MAX_PARALLEL = 4               # parallel download+upload workers per user (increase for more speed)
CHUNK_SIZE = 100               # process messages in chunks (internal batch size)
MSG_PAUSE = 0.25               # pause between starting each message task (seconds)
CHUNK_PAUSE = 0.6              # pause between chunks (seconds)
PROGRESS_INTERVAL = 0.4        # throttle progress edits (seconds)
MAX_RETRIES = 4                # retries for transient download/upload errors
LARGE_RANGE_WARN = 5000        # warn if range is very large

# ===== Job control =====
active_jobs: Dict[int, asyncio.Event] = {}        # cancel events per user
user_semaphores: Dict[int, asyncio.Semaphore] = {} # concurrency control per user

# ===== Bot client =====
# Important: disable parse_mode globally to avoid ENTITY_BOUNDS_INVALID issues
app = Client("savebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, parse_mode=None)

# ===== Helpers =====
def ensure_allowed(func):
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id if message.from_user else None
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await message.reply_text("🚫 Not authorized.", parse_mode=None)
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

def parse_link(link: str) -> Tuple[Optional[object], Optional[int]]:
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
    if "t.me/" in link:
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
    t = text.strip().replace(" ", "")
    if "-" in t:
        try:
            a, b = t.split("-")
            return int(a), int(b)
        except:
            return None, None
    try:
        n = int(t)
        return n, n
    except:
        return None, None

def start_health_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass
    def run():
        try:
            server = HTTPServer(("0.0.0.0", PORT), H)
            logger.info(f"Health server running on 0.0.0.0:{PORT}")
            server.serve_forever()
        except Exception as e:
            logger.exception("health server failed")
    threading.Thread(target=run, daemon=True).start()

# ===== Session helpers: write per-user .session file from saved session string =====
def get_session_string(user_id: int) -> Optional[str]:
    return database.get_session(user_id)

def get_user_client(user_id: int) -> Optional[Client]:
    """
    Return a file-backed Client for the user's saved session string.
    Creating with session_string + session_name will persist a .session file under sessions/.
    """
    session_str = get_session_string(user_id)
    if not session_str:
        return None
    session_name = os.path.join(SESSION_DIR, f"user_{user_id}")
    # Client will create session file at session_name + ".session"
    return Client(session_name, api_id=API_ID, api_hash=API_HASH, session_string=session_str, parse_mode=None)

# ===== Progress callbacks (throttled) =====
def progress_edit_throttled(status_msg, label="Uploading"):
    last = {"t": 0.0}
    async def cb(current, total, *args):
        now = time.time()
        if now - last["t"] < PROGRESS_INTERVAL and current != total:
            return
        last["t"] = now
        try:
            percent = (current * 100 / total) if total else 0
            await status_msg.edit_text(f"{'⬆️' if label=='Uploading' else '📥'} {label}: {percent:.1f}%", parse_mode=None)
        except Exception:
            pass
    return cb

# ===== Core single-message download+upload (with retries & thumbnail preservation) =====
async def fetch_and_forward_single(uclient: Client, bot_client: Client, target, msg_id: int, dest_chat: int, cancel_event: asyncio.Event, sem: asyncio.Semaphore) -> Tuple[bool, str]:
    """Download message `msg_id` from `target` using uclient and send to dest_chat via bot_client.
       Respects cancel_event and uses semaphore to limit concurrency.
    """
    if cancel_event.is_set():
        return False, "cancelled"
    async with sem:
        try:
            # fetch meta
            msg = await uclient.get_messages(target, msg_id)
            if not msg or msg.empty:
                return False, "not_found"

            # text-only
            if msg.text and not msg.media:
                await bot_client.send_message(dest_chat, f"📄 {msg.text}", disable_web_page_preview=True, parse_mode=None)
                return True, "text"

            # handle media
            status_msg = await bot_client.send_message(dest_chat, f"📥 Downloading / ⬆️ Uploading {msg_id}: 0%", parse_mode=None)
            download_cb = progress_edit_throttled(status_msg, label="Downloading")
            file_path = None
            # download with retries and incremental backoff
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    file_path = await uclient.download_media(msg, file_name=DOWNLOAD_DIR + "/", progress=download_cb, progress_args=())
                    break
                except Exception as e:
                    logger.warning(f"[{msg_id}] download attempt {attempt} failed: {e}")
                    if attempt == MAX_RETRIES:
                        await status_msg.edit_text("❌ Download failed after retries", parse_mode=None)
                        try: await asyncio.sleep(0.2)  # small pause to allow message to be sent
                        except: pass
                        return False, "download_failed"
                    await asyncio.sleep(0.5 * attempt)

            if not file_path or not os.path.exists(file_path):
                await status_msg.edit_text("❌ Download failed", parse_mode=None)
                return False, "no_file"

            # attempt download thumbnail for video (preserve original)
            thumb_path = None
            try:
                if getattr(msg, "video", None) and getattr(msg.video, "thumbs", None):
                    thumb = msg.video.thumbs[-1]
                    thumb_path = await uclient.download_media(thumb.file_id, file_name=DOWNLOAD_DIR + f"/thumb_{msg.id}.jpg")
            except Exception:
                thumb_path = None

            # upload with retries
            upload_cb = progress_edit_throttled(status_msg, label="Uploading")
            caption = msg.caption or ""
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    with open(file_path, "rb") as fh:
                        if msg.photo:
                            await bot_client.send_photo(dest_chat, fh, caption=caption, disable_web_page_preview=True, progress=upload_cb, progress_args=())
                        elif msg.video:
                            await bot_client.send_video(dest_chat, fh, caption=caption, thumb=thumb_path, progress=upload_cb, progress_args=())
                        elif msg.document:
                            await bot_client.send_document(dest_chat, fh, caption=caption, progress=upload_cb, progress_args=())
                        elif msg.audio:
                            await bot_client.send_audio(dest_chat, fh, caption=caption, progress=upload_cb, progress_args=())
                        elif msg.voice:
                            await bot_client.send_voice(dest_chat, fh, caption=caption, progress=upload_cb, progress_args=())
                        elif msg.animation:
                            await bot_client.send_animation(dest_chat, fh, caption=caption, progress=upload_cb, progress_args=())
                        elif msg.sticker:
                            await bot_client.send_sticker(dest_chat, fh)
                        else:
                            await bot_client.send_document(dest_chat, fh, caption=caption, progress=upload_cb, progress_args=())
                    break
                except FloodWait as e:
                    logger.warning(f"[{msg_id}] upload floodwait {e.value}s")
                    await status_msg.edit_text(f"⏳ FloodWait: waiting {e.value}s", parse_mode=None)
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    logger.warning(f"[{msg_id}] upload attempt {attempt} failed: {e}")
                    if attempt == MAX_RETRIES:
                        try:
                            await status_msg.edit_text("❌ Upload failed after retries", parse_mode=None)
                        except:
                            pass
                        return False, "upload_failed"
                    await asyncio.sleep(0.8 * attempt)

            # cleanup and remove status
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

        except Exception as e:
            logger.exception("worker error")
            return False, "error"

# ===== Commands =====
@app.on_message(filters.command("start"))
@ensure_allowed
async def cmd_start(c: Client, m: Message):
    await m.reply_text(
        "🤖 Save-Restricted Extractor (fast)\n\n"
        "Commands:\n"
        "• /login — login with phone (session saved)\n"
        "• /logout — remove saved session\n"
        "• /save <link> — fetch one message\n"
        "• /range <link> <start-end> — fetch range\n"
        "• /batch — interactive range fetch\n"
        "• /cancel — cancel current job\n",
        parse_mode=None
    )

@app.on_message(filters.command("me"))
@ensure_allowed
async def cmd_me(c: Client, m: Message):
    sess = database.get_session(m.from_user.id)
    await m.reply_text(("Status: ✅ Logged in" if sess else "Status: ❌ Not logged in"), parse_mode=None)

@app.on_message(filters.command("logout"))
@ensure_allowed
async def cmd_logout(c: Client, m: Message):
    uid = m.from_user.id
    if database.get_session(uid):
        database.save_session(uid, "")
        # remove session file if exists
        path = os.path.join(SESSION_DIR, f"user_{uid}.session")
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass
        await m.reply_text("✅ Session removed.", parse_mode=None)
    else:
        await m.reply_text("❌ No active session.", parse_mode=None)

@app.on_message(filters.command("login"))
@ensure_allowed
async def cmd_login(bot: Client, m: Message):
    uid = m.from_user.id
    if database.get_session(uid):
        return await m.reply_text("✅ Already logged in. Use /logout to reset.", parse_mode=None)
    try:
        phone_msg = await bot.ask(uid, "📞 Send phone with country code (e.g. +919876543210). /cancel to abort.", timeout=300)
        if not phone_msg or not phone_msg.text:
            return await bot.send_message(uid, "❌ No phone provided.", parse_mode=None)
        if phone_msg.text.strip() == "/cancel":
            return await bot.send_message(uid, "❌ Login cancelled.", parse_mode=None)
        phone = phone_msg.text.strip()
        if not (phone.startswith("+") and len(phone) >= 8):
            return await bot.send_message(uid, "❌ Invalid phone.", parse_mode=None)

        temp = Client(f"temp_login_{uid}", api_id=API_ID, api_hash=API_HASH, parse_mode=None)
        await temp.connect()
        await bot.send_message(uid, "📤 Sending OTP...", parse_mode=None)
        try:
            code = await temp.send_code(phone)
        except PhoneNumberInvalid:
            await bot.send_message(uid, "❌ Invalid phone number.", parse_mode=None)
            await temp.disconnect(); return
        except FloodWait as e:
            await bot.send_message(uid, f"⏳ Too many attempts. Wait {e.value}s", parse_mode=None)
            await temp.disconnect(); return
        except Exception as e:
            await bot.send_message(uid, f"❌ Error: {e}", parse_mode=None)
            await temp.disconnect(); return

        code_msg = await bot.ask(uid, "🔐 Send OTP (or /cancel):", timeout=300)
        if not code_msg or not code_msg.text:
            await temp.disconnect(); return await bot.send_message(uid, "❌ No OTP provided.", parse_mode=None)
        if code_msg.text.strip() == "/cancel":
            await temp.disconnect(); return await bot.send_message(uid, "❌ Cancelled.", parse_mode=None)
        phone_code = code_msg.text.replace(" ", "").replace("-", "")
        try:
            await temp.sign_in(phone, code.phone_code_hash, phone_code)
        except PhoneCodeInvalid:
            await bot.send_message(uid, "❌ Invalid OTP.", parse_mode=None); await temp.disconnect(); return
        except PhoneCodeExpired:
            await bot.send_message(uid, "❌ OTP expired.", parse_mode=None); await temp.disconnect(); return
        except SessionPasswordNeeded:
            pwd_msg = await bot.ask(uid, "🔒 2FA: send password (or /cancel):", timeout=300)
            if not pwd_msg or not pwd_msg.text or pwd_msg.text.strip() == "/cancel":
                await temp.disconnect(); return await bot.send_message(uid, "❌ Cancelled.", parse_mode=None)
            try:
                await temp.check_password(password=pwd_msg.text)
            except PasswordHashInvalid:
                await bot.send_message(uid, "❌ Invalid 2FA password.", parse_mode=None); await temp.disconnect(); return

        session_str = await temp.export_session_string()
        await temp.disconnect()
        # save session string in your database (database.save_session)
        database.save_session(uid, session_str)
        await bot.send_message(uid, "✅ Logged in. Session saved.", parse_mode=None)
    except asyncio.TimeoutError:
        await m.reply_text("⏰ Timeout. Try /login again.", parse_mode=None)
    except Exception as e:
        logger.exception("login error")
        await m.reply_text(f"❌ Login failed: {e}", parse_mode=None)

# ---- /save (single) ----
@app.on_message(filters.command("save"))
@ensure_allowed
async def cmd_save(c: Client, m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply_text("Usage: /save <telegram_link>", parse_mode=None)
    link = parts[1].strip()
    target, msg_id = parse_link(link)
    if not target or not msg_id:
        return await m.reply_text("❌ Cannot parse link.", parse_mode=None)
    uclient = get_user_client(m.from_user.id)
    if not uclient:
        return await m.reply_text("❌ Not logged in. Use /login first.", parse_mode=None)
    # use a small semaphore for single command to still get concurrency if needed
    sem = asyncio.Semaphore(MAX_PARALLEL)
    cancel_ev = asyncio.Event()
    active_jobs[m.from_user.id] = cancel_ev
    user_semaphores[m.from_user.id] = sem
    try:
        await uclient.connect()
        ok, reason = await fetch_and_forward_single(uclient, c, target, msg_id, m.chat.id, cancel_ev, sem)
        if ok:
            await m.reply_text("✅ Saved.", parse_mode=None)
        else:
            await m.reply_text(f"⚠️ Failed: {reason}", parse_mode=None)
    except Exception as e:
        logger.exception("save command")
        await m.reply_text(f"❌ Error: {e}", parse_mode=None)
    finally:
        active_jobs.pop(m.from_user.id, None)
        user_semaphores.pop(m.from_user.id, None)
        try:
            await uclient.disconnect()
        except:
            pass

# ---- /range (batch worker with parallelism) ----
@app.on_message(filters.command("range"))
@ensure_allowed
async def cmd_range(c: Client, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: /range <link> <start-end>", parse_mode=None)
    link = parts[1].strip(); rng = parts[2].strip()
    target, _ = parse_link(link)
    if not target:
        return await m.reply_text("❌ Invalid link.", parse_mode=None)
    s, e = parse_range(rng)
    if s is None:
        return await m.reply_text("❌ Invalid range.", parse_mode=None)
    if s > e: s, e = e, s
    total = e - s + 1
    if total > LARGE_RANGE_WARN:
        await m.reply_text(f"⚠️ Large range requested ({total}). This can take long.", parse_mode=None)

    uclient = get_user_client(m.from_user.id)
    if not uclient:
        return await m.reply_text("❌ Not logged in.", parse_mode=None)

    uid = m.from_user.id
    cancel_ev = asyncio.Event()
    active_jobs[uid] = cancel_ev
    sem = asyncio.Semaphore(MAX_PARALLEL)
    user_semaphores[uid] = sem

    try:
        await uclient.connect()
        status = await m.reply_text(f"📦 Starting range {s}-{e} ({total}). Use /cancel to stop.", parse_mode=None)
        succeeded = 0; failed = 0
        cur = s
        while cur <= e:
            if cancel_ev.is_set():
                await status.edit_text("⏹️ Cancelled by user.", parse_mode=None)
                break
            chunk_end = min(cur + CHUNK_SIZE - 1, e)
            # create tasks for chunk with limited concurrency
            tasks = []
            for msg_id in range(cur, chunk_end + 1):
                if cancel_ev.is_set():
                    break
                # small pacing to avoid spike
                await asyncio.sleep(MSG_PAUSE)
                task = asyncio.create_task(fetch_and_forward_single(uclient, c, target, msg_id, m.chat.id, cancel_ev, sem))
                tasks.append((msg_id, task))
            # await tasks as they complete and count success/fail
            for msg_id, t in tasks:
                try:
                    ok, reason = await t
                    if ok:
                        succeeded += 1
                    else:
                        failed += 1
                except Exception as ex:
                    logger.exception("task exception")
                    failed += 1
                # update every 10 messages
                processed = (msg_id - s + 1)
                if processed % 10 == 0:
                    await status.edit_text(f"📦 Progress: {msg_id}/{e}\n✅ {succeeded} | ❌ {failed}", parse_mode=None)
            cur = chunk_end + 1
            await asyncio.sleep(CHUNK_PAUSE)
        if not cancel_ev.is_set():
            await status.edit_text(f"✅ Range complete!\nDownloaded: {succeeded}\nFailed: {failed}\nRange: {s}-{e}", parse_mode=None)
    except Exception as ex:
        logger.exception("range")
        await m.reply_text(f"❌ Range error: {ex}", parse_mode=None)
    finally:
        active_jobs.pop(uid, None)
        user_semaphores.pop(uid, None)
        try:
            await uclient.disconnect()
        except:
            pass

# ---- /batch (interactive) ----
@app.on_message(filters.command("batch"))
@ensure_allowed
async def cmd_batch(c: Client, m: Message):
    await m.reply_text("🔢 Send: <link> <start-end> (eg: https://t.me/channel 4-20) or /cancel", parse_mode=None)
    try:
        reply = await c.ask(m.from_user.id, "", timeout=300)
        if not reply or not reply.text:
            return await m.reply_text("❌ No input.", parse_mode=None)
        if reply.text.strip() == "/cancel":
            return await m.reply_text("❌ Batch cancelled.", parse_mode=None)
        parts = reply.text.strip().split()
        if len(parts) != 2:
            return await m.reply_text("❌ Invalid format. Use: <link> <start-end>", parse_mode=None)
        link, rng = parts
        # call /range logic by constructing a fake message
        fake = Message(message_id=m.message_id, date=m.date, chat=m.chat, from_user=m.from_user, text=f"/range {link} {rng}")
        await cmd_range(c, fake)
    except asyncio.TimeoutError:
        await m.reply_text("⏰ Timeout. Send /batch again.", parse_mode=None)
    except Exception as e:
        logger.exception("batch")
        await m.reply_text(f"❌ Batch error: {e}", parse_mode=None)

# ---- /cancel ----
@app.on_message(filters.command("cancel"))
@ensure_allowed
async def cmd_cancel(c: Client, m: Message):
    uid = m.from_user.id
    ev = active_jobs.get(uid)
    if ev and not ev.is_set():
        ev.set()
        await m.reply_text("⏹️ Cancel requested; stopping...", parse_mode=None)
    else:
        await m.reply_text("❌ No active job to cancel.", parse_mode=None)

# ===== Start server =====
if __name__ == "__main__":
    start_health_server()
    logger.info("Starting savebot (optimized for Koyeb free)...")
    app.run()
