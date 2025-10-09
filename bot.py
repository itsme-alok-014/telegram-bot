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

# Constants
MAX_BATCH = int(os.environ.get("MAX_BATCH", "200"))

# Health server for platform checks
def start_health_server():
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever(),
        daemon=True
    ).start()
    logger.info(f"Health server started on port {PORT}")

# Helper: check allowed users
def ensure_allowed(func):
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            return await message.reply_text("🚫 Not authorized")
        return await func(client, message, *args, **kwargs)
    return wrapper

# Parse link
def parse_link(link: str):
    link = link.strip().rstrip("/")
    if "/c/" in link:
        parts = link.split("/")
        if len(parts) >= 6:
            try:
                short = parts[4]
                mid = int(parts[5].split("?")[0])
                return int(f"-100{short}"), mid
            except:
                pass
    elif "t.me/" in link:
        parts = link.split("/")
        if len(parts) >= 5:
            try:
                username = parts[3].lstrip("@")
                mid = int(parts[4].split("?")[0])
                return username, mid
            except:
                pass
    return None, None

# Global state for batch tasks
active_batches = {}

# Start bot
app = Client("save_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message(filters.command("start"))
@ensure_allowed
async def start_cmd(c, m):
    await m.reply_text(
        "**Save-Restricted Bot**\n\n"
        "/login – authenticate\n"
        "/logout – clear session\n"
        "/save <link> – fetch single\n"
        "/batch – interactive batch\n"
        "/me – status",
        disable_web_page_preview=True
    )

@app.on_message(filters.command("me"))
@ensure_allowed
async def me_cmd(c, m):
    sess = database.get_session(m.from_user.id)
    status = "✅ Logged in" if sess else "❌ Not logged in"
    await m.reply_text(status)

@app.on_message(filters.command("logout"))
@ensure_allowed
async def logout_cmd(c, m):
    database.save_session(m.from_user.id, "")
    await m.reply_text("✅ Logged out")

# Login flow omitted for brevity – reuse your existing login command

# SINGLE FETCH
@app.on_message(filters.command("save"))
@ensure_allowed
async def save_cmd(c, m):
    if len(m.command) < 2:
        return await m.reply_text("Usage: /save <t.me link>")
    target, mid = parse_link(m.command[1])
    if not target:
        return await m.reply_text("❌ Invalid link format")
    sess = database.get_session(m.from_user.id)
    if not sess:
        return await m.reply_text("❌ /login first")
    u = Client(":memory:", session_string=sess, api_id=API_ID, api_hash=API_HASH)
    await u.connect()
    status = await m.reply_text(f"🔍 Fetching {mid}...")
    try:
        msg = await u.get_messages(target, mid)
        if not msg or msg.empty:
            return await status.edit_text("⚠️ Message not found or no access")
        if msg.media:
            path = await u.download_media(msg, file_name="downloads/")
            with open(path, "rb") as f:
                if msg.photo:
                    await m.reply_photo(f)
                elif msg.video:
                    await m.reply_video(f)
                else:
                    await m.reply_document(f)
            os.remove(path)
        else:
            await m.reply_text(msg.text or "(no text)")
        await status.delete()
    except FloodWait as e:
        await status.edit_text(f"⏳ Flood wait {e.value}s")
    finally:
        await u.disconnect()

# BATCH INTERACTIVE FLOW
@app.on_message(filters.command("batch"))
@ensure_allowed
async def batch_start(c, m):
    # Prompt for link
    msg = await m.reply_text("🔗 Send the message link to batch from:")
    active_batches[m.from_user.id] = {"step": "link", "msg": msg}
    
@app.on_message(filters.private)
@ensure_allowed
async def batch_handler(c, m):
    state = active_batches.get(m.from_user.id)
    if not state:
        return
    step = state["step"]
    
    # Cancel button always available
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="batch_cancel")]])
    
    if step == "link":
        target, _ = parse_link(m.text)
        if not target:
            return await m.reply_text("❌ Invalid link. Try again", reply_markup=cancel_kb)
        state["target"] = m.text.strip()
        state["step"] = "range"
        return await m.reply_text(
            "🔢 Now send range (e.g. `4-50`).\nMax: " + str(MAX_BATCH),
            reply_markup=cancel_kb
        )
    
    if step == "range":
        parts = m.text.replace(" ", "").split("-")
        try:
            start, end = int(parts[0]), int(parts[1])
        except:
            return await m.reply_text("❌ Invalid range. Use `start-end`", reply_markup=cancel_kb)
        if end < start: start, end = end, start
        if end - start + 1 > MAX_BATCH:
            return await m.reply_text(f"❌ Max batch size is {MAX_BATCH}", reply_markup=cancel_kb)
        
        # Begin batch
        link = state["target"]
        del active_batches[m.from_user.id]  # clear state
        await m.reply_text("▶️ Starting batch...", reply_markup=cancel_kb)
        asyncio.create_task(run_batch(m.from_user.id, link, start, end, m))
        
async def run_batch(user_id, link, start, end, orig_msg):
    sess = database.get_session(user_id)
    u = Client(":memory:", session_string=sess, api_id=API_ID, api_hash=API_HASH)
    await u.connect()
    
    status = await orig_msg.reply_text(f"📦 Batch 0/{end-start+1}")
    target, _ = parse_link(link)
    success = 0
    total = end - start + 1
    
    for i, mid in enumerate(range(start, end+1), 1):
        # Check for cancellation
        if active_batches.get(user_id) == "cancel":
            await status.edit_text("❌ Batch cancelled by user")
            break
        try:
            msg = await u.get_messages(target, mid)
            if msg and not msg.empty:
                path = await u.download_media(msg, file_name="downloads/")
                with open(path, "rb") as f:
                    if msg.photo: await orig_msg.reply_photo(f)
                    elif msg.video: await orig_msg.reply_video(f)
                    else: await orig_msg.reply_document(f)
                os.remove(path)
                success += 1
        except FloodWait as e:
            await status.edit_text(f"⏳ Waiting {e.value}s at {mid}/{end}")
            await asyncio.sleep(e.value+1)
        await status.edit_text(f"📦 {i}/{total} — Success: {success}")
    
    else:
        # Completed without cancellation
        await status.edit_text(f"✅ Batch done: {success}/{total}")
    
    await u.disconnect()

@app.on_callback_query(filters.regex("^batch_cancel$"))
async def cancel_batch(c, cq):
    uid = cq.from_user.id
    if uid in active_batches:
        active_batches[uid] = "cancel"
        await cq.answer("Cancelling batch...")
    else:
        await cq.answer("No active batch to cancel")

if __name__ == "__main__":
    start_health_server()
    logger.info("Bot is up")
    app.run()
