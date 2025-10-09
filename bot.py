# bot.py — optimized for Koyeb free, non-interactive
import os
import time
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple, Dict

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait, SessionPasswordNeeded, PasswordHashInvalid

# local modules
from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

# ===== Logging =====
logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== Tunables =====
DOWNLOAD_DIR = "downloads"
SESSION_DIR = "sessions"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)
MAX_PARALLEL = 4
CHUNK_SIZE = 100
MSG_PAUSE = 0.25
CHUNK_PAUSE = 0.6
PROGRESS_INTERVAL = 0.4
MAX_RETRIES = 4
LARGE_RANGE_WARN = 5000

# ===== Job control =====
active_jobs: Dict[int, asyncio.Event] = {}
user_semaphores: Dict[int, asyncio.Semaphore] = {}

# ===== Bot client =====
app = Client("savebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ===== Helpers =====
def ensure_allowed(func):
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id if message.from_user else None
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await message.reply_text("🚫 Not authorized.")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

def parse_link(link: str) -> Tuple[Optional[str], Optional[int]]:
    if not link:
        return None, None
    link = link.strip().rstrip("/")
    if "/c/" in link:
        parts = link.split("/")
        if len(parts) >= 6:
            try:
                short_id = parts[4]
                msg_id = int(parts[5].split("?")[0].split("-")[0])
                return f"-100{short_id}", msg_id
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

def get_session_string(user_id: int) -> Optional[str]:
    return database.get_session(user_id)

def get_user_client(user_id: int) -> Optional[Client]:
    session_str = get_session_string(user_id)
    if not session_str:
        return None
    session_name = os.path.join(SESSION_DIR, f"user_{user_id}")
    return Client(session_name, api_id=API_ID, api_hash=API_HASH, session_string=session_str)

def progress_edit_throttled(status_msg, label="Uploading"):
    last = {"t": 0.0}
    async def cb(current, total, *args):
        now = time.time()
        if now - last["t"] < PROGRESS_INTERVAL and current != total:
            return
        last["t"] = now
        try:
            percent = (current * 100 / total) if total else 0
            await status_msg.edit_text(f"{'⬆️' if label=='Uploading' else '📥'} {label}: {percent:.1f}%")
        except Exception:
            pass
    return cb

# ===== Core download+upload =====
async def fetch_and_forward_single(uclient: Client, bot_client: Client, target, msg_id: int, dest_chat: int, cancel_event: asyncio.Event, sem: asyncio.Semaphore):
    if cancel_event.is_set():
        return False, "cancelled"
    async with sem:
        try:
            msg = await uclient.get_messages(target, msg_id)
            if not msg or msg.empty:
                return False, "not_found"

            # text-only
            if msg.text and not msg.media:
                await bot_client.send_message(dest_chat, f"📄 {msg.text}", disable_web_page_preview=True)
                return True, "text"

            status_msg = await bot_client.send_message(dest_chat, f"📥 Downloading / ⬆️ Uploading {msg_id}: 0%")
            download_cb = progress_edit_throttled(status_msg, label="Downloading")
            file_path = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    file_path = await uclient.download_media(msg, file_name=DOWNLOAD_DIR + "/", progress=download_cb)
                    break
                except Exception as e:
                    logger.warning(f"[{msg_id}] download attempt {attempt} failed: {e}")
                    if attempt == MAX_RETRIES:
                        await status_msg.edit_text("❌ Download failed after retries")
                        return False, "download_failed"
                    await asyncio.sleep(0.5 * attempt)

            if not file_path or not os.path.exists(file_path):
                await status_msg.edit_text("❌ Download failed")
                return False, "no_file"

            thumb_path = None
            try:
                if getattr(msg, "video", None) and getattr(msg.video, "thumbs", None):
                    thumb = msg.video.thumbs[-1]
                    thumb_path = await uclient.download_media(thumb.file_id, file_name=DOWNLOAD_DIR + f"/thumb_{msg.id}.jpg")
            except:
                thumb_path = None

            upload_cb = progress_edit_throttled(status_msg, label="Uploading")
            caption = msg.caption or ""
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    with open(file_path, "rb") as fh:
                        if msg.photo:
                            await bot_client.send_photo(dest_chat, fh, caption=caption, disable_web_page_preview=True, progress=upload_cb)
                        elif msg.video:
                            await bot_client.send_video(dest_chat, fh, caption=caption, thumb=thumb_path, progress=upload_cb)
                        elif msg.document:
                            await bot_client.send_document(dest_chat, fh, caption=caption, progress=upload_cb)
                        elif msg.audio:
                            await bot_client.send_audio(dest_chat, fh, caption=caption, progress=upload_cb)
                        elif msg.voice:
                            await bot_client.send_voice(dest_chat, fh, caption=caption, progress=upload_cb)
                        elif msg.animation:
                            await bot_client.send_animation(dest_chat, fh, caption=caption, progress=upload_cb)
                        elif msg.sticker:
                            await bot_client.send_sticker(dest_chat, fh)
                        else:
                            await bot_client.send_document(dest_chat, fh, caption=caption, progress=upload_cb)
                    break
                except FloodWait as e:
                    await status_msg.edit_text(f"⏳ FloodWait: {e.value}s")
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    logger.warning(f"[{msg_id}] upload attempt {attempt} failed: {e}")
                    if attempt == MAX_RETRIES:
                        await status_msg.edit_text("❌ Upload failed after retries")
                        return False, "upload_failed"
                    await asyncio.sleep(0.8 * attempt)

            try:
                os.remove(file_path)
            except: pass
            if thumb_path:
                try: os.remove(thumb_path)
                except: pass
            try: await status_msg.delete()
            except: pass

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
        "• /login <phone> — login with phone\n"
        "• /logout — remove saved session\n"
        "• /save <link> — fetch one message\n"
        "• /range <link> <start-end> — fetch range\n"
        "• /batch <link> <start-end> — batch range\n"
        "• /cancel — cancel current job"
    )

@app.on_message(filters.command("me"))
@ensure_allowed
async def cmd_me(c: Client, m: Message):
    sess = database.get_session(m.from_user.id)
    await m.reply_text(("Status: ✅ Logged in" if sess else "Status: ❌ Not logged in"))

@app.on_message(filters.command("logout"))
@ensure_allowed
async def cmd_logout(c: Client, m: Message):
    uid = m.from_user.id
    if database.get_session(uid):
        database.save_session(uid, "")
        path = os.path.join(SESSION_DIR, f"user_{uid}.session")
        try: os.remove(path)
        except: pass
        await m.reply_text("✅ Session removed.")
    else:
        await m.reply_text("❌ No active session.")

# ---- /save ----
@app.on_message(filters.command("save"))
@ensure_allowed
async def cmd_save(c: Client, m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply_text("Usage: /save <telegram_link>")
    link = parts[1].strip()
    target, msg_id = parse_link(link)
    if not target or not msg_id:
        return await m.reply_text("❌ Cannot parse link.")
    uclient = get_user_client(m.from_user.id)
    if not uclient:
        return await m.reply_text("❌ Not logged in. Use /login first.")
    sem = asyncio.Semaphore(MAX_PARALLEL)
    cancel_ev = asyncio.Event()
    active_jobs[m.from_user.id] = cancel_ev
    user_semaphores[m.from_user.id] = sem
    try:
        await uclient.connect()
        ok, reason = await fetch_and_forward_single(uclient, c, target, msg_id, m.chat.id, cancel_ev, sem)
        await m.reply_text("✅ Saved." if ok else f"⚠️ Failed: {reason}")
    except Exception as e:
        logger.exception("save command")
        await m.reply_text(f"❌ Error: {e}")
    finally:
        active_jobs.pop(m.from_user.id, None)
        user_semaphores.pop(m.from_user.id, None)
        try: await uclient.disconnect()
        except: pass

# ---- /range ----
@app.on_message(filters.command("range"))
@ensure_allowed
async def cmd_range(c: Client, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: /range <link> <start-end>")
    link = parts[1].strip(); rng = parts[2].strip()
    target, _ = parse_link(link)
    if not target:
        return await m.reply_text("❌ Invalid link.")
    s, e = parse_range(rng)
    if s is None:
        return await m.reply_text("❌ Invalid range.")
    if s > e: s, e = e, s
    total = e - s + 1
    if total > LARGE_RANGE_WARN:
        await m.reply_text(f"⚠️ Large range requested ({total}). This can take long.")

    uclient = get_user_client(m.from_user.id)
    if not uclient:
        return await m.reply_text("❌ Not logged in.")

    uid = m.from_user.id
    cancel_ev = asyncio.Event()
    active_jobs[uid] = cancel_ev
    sem = asyncio.Semaphore(MAX_PARALLEL)
    user_semaphores[uid] = sem

    try:
        await uclient.connect()
        status = await m.reply_text(f"📦 Starting range {s}-{e} ({total}). Use /cancel to stop.")
        succeeded = 0; failed = 0
        cur = s
        while cur <= e:
            if cancel_ev.is_set():
                await status.edit_text("⏹️ Cancelled by user.")
                break
            chunk_end = min(cur + CHUNK_SIZE - 1, e)
            tasks = []
            for msg_id in range(cur, chunk_end + 1):
                if cancel_ev.is_set(): break
                await asyncio.sleep(MSG_PAUSE)
                task = asyncio.create_task(fetch_and_forward_single(uclient, c, target, msg_id, m.chat.id, cancel_ev, sem))
                tasks.append((msg_id, task))
            for msg_id, t in tasks:
                try:
                    ok, reason = await t
                    if ok: succeeded += 1
                    else: failed += 1
                except Exception:
                    failed += 1
                processed = (msg_id - s + 1)
                if processed % 10 == 0:
                    try: await status.edit_text(f"📦 Progress: {msg_id}/{e}\n✅ {succeeded} | ❌ {failed}")
                    except: pass
            cur = chunk_end + 1
            await asyncio.sleep(CHUNK_PAUSE)
        if not cancel_ev.is_set():
            await status.edit_text(f"✅ Range complete!\nDownloaded: {succeeded}\nFailed: {failed}\nRange: {s}-{e}")
    except Exception as ex:
        logger.exception("range")
        await m.reply_text(f"❌ Range error: {ex}")
    finally:
        active_jobs.pop(uid, None)
        user_semaphores.pop(uid, None)
        try: await uclient.disconnect()
        except: pass

# ---- /batch (shortcut to /range) ----
@app.on_message(filters.command("batch"))
@ensure_allowed
async def cmd_batch(c: Client, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: /batch <link> <start-end>")
    await cmd_range(c, m)  # simply reuse range logic

# ---- /cancel ----
@app.on_message(filters.command("cancel"))
@ensure_allowed
async def cmd_cancel(c: Client, m: Message):
    uid = m.from_user.id
    ev = active_jobs.get(uid)
    if ev:
        ev.set()
        await m.reply_text("⏹️ Current job cancelled.")
    else:
        await m.reply_text("⚠️ No active job.")

# ===== Main =====
if __name__ == "__main__":
    start_health_server()
    logger.info("Bot started...")
    app.run()
