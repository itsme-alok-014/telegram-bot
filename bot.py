import os
import logging
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_BATCH = int(os.environ.get("MAX_BATCH", "500"))
active_batches = {}  # Maps user_id -> batch state

def start_health_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        def log_message(self,*args): pass
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), H).serve_forever(), daemon=True).start()
    logger.info(f"Health server on port {PORT}")

def ensure_allowed(fn):
    async def wrapper(c, m: Message, *a, **k):
        if m.command[0] not in ["start", "login"] and ALLOWED_USER_IDS and m.from_user.id not in ALLOWED_USER_IDS:
            return await m.reply_text("🚫 Not authorized")
        return await fn(c, m, *a, **k)
    return wrapper

def parse_link(link: str):
    link = link.strip().rstrip("/")
    if "/c/" in link:
        p = link.split("/")
        try: return int(f"-100{p[4]}"), int(p[5].split("?")[0])
        except: return None, None
    if "t.me/" in link:
        p = link.split("/")
        try: return p[3].lstrip("@"), int(p[4].split("?")[0])
        except: return None, None
    return None, None

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message(filters.command("start"))
async def start_cmd(c, m: Message):
    await m.reply_text(
        "**Save Bot**\n"
        "/login – auth\n"
        "/logout – clear session\n"
        "/save <link> – single\n"
        "/batch – batch\n"
        "/me – status"
    )

@app.on_message(filters.command("login"))
async def login_cmd(c, m: Message):
    # (reuse your login flow here; unprotected)
    pass

@app.on_message(filters.command("logout"))
@ensure_allowed
async def logout_cmd(c, m: Message):
    database.save_session(m.from_user.id, "")
    await m.reply_text("✅ Logged out")

@app.on_message(filters.command("me"))
@ensure_allowed
async def me_cmd(c, m: Message):
    sess = database.get_session(m.from_user.id)
    await m.reply_text("✅ Logged in" if sess else "❌ Not logged in")

@app.on_message(filters.command("save"))
@ensure_allowed
async def save_cmd(c, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: /save <link>")
    target, mid = parse_link(m.command[1])
    if not target:
        return await m.reply_text("❌ Invalid link")
    sess = database.get_session(m.from_user.id)
    if not sess:
        return await m.reply_text("❌ /login first")
    u = Client(":memory:", session_string=sess, api_id=API_ID, api_hash=API_HASH)
    await u.connect()
    msg = await m.reply_text(f"🔍 Fetching {mid}...")
    try:
        tm = await u.get_messages(target, mid)
        if not tm or tm.empty:
            return await msg.edit_text("⚠️ Not found or no access")
        if tm.media:
            path = await u.download_media(tm, file_name="downloads/")
            with open(path,"rb") as f:
                if tm.photo: await m.reply_photo(f)
                elif tm.video: await m.reply_video(f)
                else: await m.reply_document(f)
            os.remove(path)
        else:
            await m.reply_text(tm.text or "(no text)")
        await msg.delete()
    except FloodWait as e:
        await msg.edit_text(f"⏳ Flood wait {e.value}s")
    finally:
        await u.disconnect()

@app.on_message(filters.command("batch"))
@ensure_allowed
async def batch_start(c, m: Message):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="batch_cancel")]])
    await m.reply_text("🔗 Send link and range as `link/start-end`", reply_markup=keyboard)
    active_batches[m.from_user.id] = {"state": "awaiting", "cancel": False}

@app.on_message(filters.private & ~filters.command)
@ensure_allowed
async def batch_handler(c, m: Message):
    state = active_batches.get(m.from_user.id)
    if not state or state["state"] != "awaiting":
        return
    if m.text.lower().startswith("/cancel"):
        return
    parts = m.text.split()
    if len(parts)!=2 or "-" not in parts[1]:
        return await m.reply_text("❌ Format: `link start-end`")
    link, rng = parts
    start, end = map(int, rng.replace(" ","").split("-"))
    if end-start+1>MAX_BATCH:
        return await m.reply_text(f"❌ Max batch is {MAX_BATCH}")
    state["state"] = "running"
    asyncio.create_task(run_batch(m, link, start, end))

async def run_batch(orig_msg: Message, link: str, start: int, end: int):
    uid = orig_msg.from_user.id
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="batch_cancel")]])
    status = await orig_msg.reply_text(f"📦 0/{end-start+1}", reply_markup=keyboard)
    sess = database.get_session(uid)
    u = Client(":memory:", session_string=sess, api_id=API_ID, api_hash=API_HASH)
    await u.connect()
    success=0
    for i,msg_id in enumerate(range(start,end+1),1):
        state = active_batches.get(uid)
        if state and state.get("cancel"):
            await status.edit_text("❌ Batch cancelled")
            break
        try:
            tm = await u.get_messages(*parse_link(link), msg_id)
            if tm and not tm.empty and tm.media:
                path = await u.download_media(tm, file_name="downloads/")
                with open(path,"rb") as f:
                    if tm.photo: await orig_msg.reply_photo(f)
                    elif tm.video: await orig_msg.reply_video(f)
                    else: await orig_msg.reply_document(f)
                os.remove(path)
                success+=1
        except FloodWait as e:
            await status.edit_text(f"⏳ Wait {e.value}s")
            await asyncio.sleep(e.value+1)
        await status.edit_text(f"📦 {i}/{end-start+1} — ✅{success}")
    else:
        await status.edit_text(f"✅ Batch done {success}/{end-start+1}")
    await u.disconnect()
    active_batches.pop(uid, None)

@app.on_callback_query(filters.regex("^batch_cancel$"))
async def cancel_batch(c, cq):
    uid = cq.from_user.id
    state = active_batches.get(uid)
    if state:
        state["cancel"] = True
        await cq.answer("Cancelling...")

if __name__ == "__main__":
    start_health_server()
    app.run()
