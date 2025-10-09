import os
import logging
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
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


from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait, UserNotParticipant, ChannelPrivate, PeerIdInvalid, MessageIdInvalid

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
    # (Same parse_link function as original)
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

@app.on_message(filters.command("login"))
@ensure_allowed
async def cmd_login(bot: Client, message: Message):
    if database.get_session(message.from_user.id):
        await message.reply_text("✅ Already logged in. Use `/logout` to reset.")
        return
    user_id = message.from_user.id
    try:
        phone_msg = await bot.ask(
            user_id,
            "📞 **Send your phone number** with country code\n\n"
            "Example: `+919876543210`\n\n"
            "Send `/cancel` to cancel.", 
            timeout=300
        )
        if phone_msg.text == "/cancel":
            return await phone_msg.reply("❌ Login cancelled.")
        phone = phone_msg.text.strip()
        if not (phone.startswith("+") and len(phone) >= 8):
            return await phone_msg.reply("❌ Invalid phone format.")
        
        u = Client(":memory:", api_id=API_ID, api_hash=API_HASH)
        await u.connect()
        await phone_msg.reply("📤 Sending OTP...")
        try:
            code = await u.send_code(phone)
        except PhoneNumberInvalid:
            await phone_msg.reply("❌ Invalid phone number.")
            await u.disconnect()
            return
        except FloodWait as e:
            await phone_msg.reply(f"⏳ Wait {e.value} seconds.")
            await u.disconnect()
            return
        except Exception as e:
            await phone_msg.reply(f"❌ Error: {e}")
            await u.disconnect()
            return

        code_msg = await bot.ask(
            user_id,
            "🔐 **Enter the OTP** you received\n\n"
            "Send `/cancel` to cancel.", 
            timeout=300
        )
        if code_msg.text == "/cancel":
            await code_msg.reply("❌ Login cancelled.")
            await u.disconnect()
            return
        phone_code = code_msg.text.replace(" ", "").replace("-", "")
        try:
            await u.sign_in(phone, code.phone_code_hash, phone_code)
        except PhoneCodeInvalid:
            await code_msg.reply("❌ Invalid OTP code.")
            await u.disconnect()
            return
        except PhoneCodeExpired:
            await code_msg.reply("❌ OTP expired.")
            await u.disconnect()
            return
        except SessionPasswordNeeded:
            pwd_msg = await bot.ask(
                user_id,
                "🔒 **2FA enabled**\n\nSend your password:", 
                timeout=300
            )
            if pwd_msg.text == "/cancel":
                await pwd_msg.reply("❌ Login cancelled.")
                await u.disconnect()
                return
            try:
                await u.check_password(password=pwd_msg.text)
            except PasswordHashInvalid:
                await pwd_msg.reply("❌ Invalid 2FA password.")
                await u.disconnect()
                return
        
        session_string = await u.export_session_string()
        await u.disconnect()
        database.save_session(user_id, session_string)
        await bot.send_message(user_id,
            "✅ **Logged in successfully!**\n\n"
            "Session saved. You can now use `/save` and `/range`.\n\n"
            "⚠️ If you get **AUTH_KEY** errors later, use `/logout` then `/login` again."
        )
    except asyncio.TimeoutError:
        await message.reply("⏰ Timeout. Use `/login` to try again.")
    except Exception as e:
        await message.reply(f"❌ Login error: {e}")

def get_user_client(user_id: int):
    session_str = database.get_session(user_id)
    if not session_str:
        return None
    return Client(":memory:", session_string=session_str, api_id=API_ID, api_hash=API_HASH)

# Progress callback for editing a status message
async def progress(current, total, status_msg):
    percentage = current * 100 / total if total else 0
    await status_msg.edit_text(f"⬆️ Uploading: {percentage:.1f}%")

# Progress callback for download
async def download_progress(current, total, status_msg):
    percentage = current * 100 / total if total else 0
    await status_msg.edit_text(f"⬇️ Downloading: {percentage:.1f}%")

@app.on_message(filters.command("save"))
@ensure_allowed
async def cmd_save(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "**Usage:** `/save <telegram_link>`\n"
            "Example: `/save https://t.me/channel/123`"
        )
    link = message.command[1]
    target, msg_id = parse_link(link)
    if target is None:
        return await message.reply_text(f"❌ **Invalid link:** {link}")
    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")
    try:
        await u.connect()
        status_msg = await message.reply_text(f"🔍 Fetching message {msg_id}...")
        msg = await u.get_messages(target, msg_id)
        if not msg or msg.empty:
            return await status_msg.edit_text(
                "⚠️ **Message not found**\n"
                "• Ensure you're a member of this chat.\n"
                "• Check the message ID and link."
            )
        if msg.media:
            # Download media with progress
            download_status = await status_msg.edit_text("📥 Downloading: 0%")
            file_path = await u.download_media(msg, file_name="downloads/", progress=download_progress, progress_args=(download_status,))
            if file_path and os.path.exists(file_path):
                # Upload media with progress
                await download_status.edit_text("⬆️ Uploading: 0%")
                with open(file_path, 'rb') as f:
                    caption = f"**Message {msg_id}:** {msg.caption or ''}"
                    if msg.photo:
                        await message.reply_photo(f, caption=caption, disable_web_page_preview=True, progress=progress, progress_args=(download_status,))
                    elif msg.video:
                        # Download thumbnail if available:contentReference[oaicite:5]{index=5}
                        thumb_path = None
                        if msg.video and msg.video.thumbs:
                            thumb_file = msg.video.thumbs[-1].file_id
                            thumb_path = await u.download_media(thumb_file, file_name=f"downloads/thumb_{msg_id}.jpg")
                        await message.reply_video(f, caption=caption, thumb=thumb_path, progress=progress, progress_args=(download_status,))
                        if thumb_path and os.path.exists(thumb_path):
                            os.remove(thumb_path)
                    elif msg.document:
                        await message.reply_document(f, caption=caption, progress=progress, progress_args=(download_status,))
                    elif msg.audio:
                        await message.reply_audio(f, caption=caption, progress=progress, progress_args=(download_status,))
                    elif msg.voice:
                        await message.reply_voice(f, caption=caption, progress=progress, progress_args=(download_status,))
                    elif msg.animation:
                        await message.reply_animation(f, caption=caption, progress=progress, progress_args=(download_status,))
                    elif msg.sticker:
                        await message.reply_sticker(f)
                    else:
                        await message.reply_document(f, caption=caption, progress=progress, progress_args=(download_status,))
                os.remove(file_path)
                await download_status.delete()
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ Failed to download media.")
        elif msg.text:
            await status_msg.delete()
            await message.reply_text(f"📄 **Message {msg_id}:**\n\n{msg.text}", disable_web_page_preview=True)
        else:
            await status_msg.edit_text("⚠️ **Message has no text or media**")
    except FloodWait as e:
        await message.reply_text(f"⏳ **Rate limit:** Wait {e.value}s.")
    except UserNotParticipant:
        await message.reply_text("❌ **Not a member** of this chat.")
    except ChannelPrivate:
        await message.reply_text("❌ **Private channel** - join first or check the link.")
    except PeerIdInvalid:
        await message.reply_text("❌ **Invalid chat** - check the link.")
    except MessageIdInvalid:
        await message.reply_text("❌ **Invalid message ID** - check the number.")
    except Exception as e:
        await message.reply_text(f"❌ **Error:** {e}")
    finally:
        try:
            await u.disconnect()
        except:
            pass

@app.on_message(filters.command("range"))
@ensure_allowed
async def cmd_range(client: Client, message: Message):
    if len(message.command) < 3:
        return await message.reply_text(
            "**Usage:** `/range <link> <start-end>`\n"
            "Example: `/range https://t.me/channel 5-15`\n"
            "Use `/batch` for an interactive flow."
        )
    link = message.command[1]
    range_text = message.command[2]
    target, _ = parse_link(link)
    if target is None:
        return await message.reply_text(f"❌ **Invalid link:** {link}")
    start_id, end_id = parse_range(range_text)
    if start_id is None:
        return await message.reply_text(f"❌ **Invalid range:** {range_text}")
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    # No strict limit; warn if huge
    if end_id - start_id > 1000:
        return await message.reply_text("❌ **Range too large.** Please use a smaller range.")
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
        error_count = 0
        for msg_id in range(start_id, end_id+1):
            if cancel_requests.get(uid):
                await status_msg.edit_text("⏹️ **Batch cancelled by user.**")
                break
            try:
                msg = await u.get_messages(target, msg_id)
                if not msg or msg.empty:
                    error_count += 1
                    continue
                if msg.media:
                    file_status = await message.reply_text(f"📥 Downloading/⬆️ Uploading message {msg_id}: 0%")
                    file_path = await u.download_media(msg, file_name="downloads/", progress=download_progress, progress_args=(file_status,))
                    if file_path and os.path.exists(file_path):
                        await file_status.edit_text("⬆️ Uploading: 0%")
                        caption = f"**Message {msg_id}:** {msg.caption or ''}"
                        with open(file_path, 'rb') as f:
                            if msg.photo:
                                await message.reply_photo(f, caption=caption, disable_web_page_preview=True,
                                                          progress=progress, progress_args=(file_status,))
                            elif msg.video:
                                thumb_path = None
                                if msg.video and msg.video.thumbs:
                                    thumb_file = msg.video.thumbs[-1].file_id
                                    thumb_path = await u.download_media(thumb_file, file_name=f"downloads/thumb_{msg_id}.jpg")
                                await message.reply_video(f, caption=caption, thumb=thumb_path,
                                                          progress=progress, progress_args=(file_status,))
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
                    else:
                        await file_status.edit_text("❌ Download error.")
                        error_count += 1
                elif msg.text:
                    await message.reply_text(f"📄 **{msg_id}:** {msg.text}", disable_web_page_preview=True)
                    success_count += 1
            except FloodWait as e:
                await status_msg.edit_text(f"⏳ **Rate limit:** waiting {e.value}s...")
                await asyncio.sleep(e.value+1)
            except Exception:
                error_count += 1
                continue
        if not cancel_requests.get(uid):
            await status_msg.edit_text(
                f"✅ **Range complete!**\n"
                f"Downloaded: {success_count}\nFailed: {error_count}\n"
                f"Range: {start_id}-{end_id}"
            )
    except Exception as e:
        await message.reply_text(f"❌ **Range error:** {e}")
    finally:
        active_jobs[uid] = False
        cancel_requests[uid] = False
        try:
            await u.disconnect()
        except:
            pass

@app.on_message(filters.command("batch"))
@ensure_allowed
async def cmd_batch(client: Client, message: Message):
    uid = message.from_user.id
    await message.reply_text("🔢 **Enter link and range** (e.g. `https://t.me/channel 10-20`). Send `/cancel` to abort.")
    try:
        reply = await client.ask(uid, "", timeout=300)  # Wait for user response
        if not reply or not reply.text:
            return await message.reply_text("❌ No input received.")
        text = reply.text.strip()
        if text == "/cancel":
            return await message.reply_text("❌ Batch cancelled.")
        parts = text.split()
        if len(parts) != 2:
            return await message.reply_text("❌ Invalid format. Use `link start-end`.")
        link, range_part = parts
        target, _ = parse_link(link)
        if target is None:
            return await message.reply_text(f"❌ Invalid link: {link}")
        start_id, end_id = parse_range(range_part)
        if start_id is None:
            return await message.reply_text(f"❌ Invalid range: {range_part}")
        # Invoke the range logic
        fake = Message(
            message_id=message.message_id,
            date=message.date,
            chat=message.chat,
            from_user=message.from_user,
            text=f"/range {link} {start_id}-{end_id}"
        )
        await cmd_range(client, fake)
    except asyncio.TimeoutError:
        await message.reply_text("⏰ Timeout. Send `/batch` again to start.")
    except Exception as e:
        await message.reply_text(f"❌ Batch error: {e}")

@app.on_message(filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    uid = message.from_user.id
    if active_jobs.get(uid):
        cancel_requests[uid] = True
        await message.reply_text("⏹️ Cancelled.")
    else:
        await message.reply_text("❌ No active operation to cancel.")

if __name__ == "__main__":
    start_health_server()
    logger.info("Starting Telegram Save-Restricted Bot...")
    app.run()
