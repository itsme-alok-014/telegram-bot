import os
import logging
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import (
    ApiIdInvalid, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid, UsernameNotOccupied, 
    FloodWait, ChatAdminRequired, UserNotParticipant, ChannelPrivate,
    PeerIdInvalid, MessageNotModified, MessageIdInvalid
)

from config import API_ID, API_HASH, BOT_TOKEN, PORT, ALLOWED_USER_IDS
import database

logging.basicConfig(format='[%(levelname)s %(asctime)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Ensure downloads directory exists
if not os.path.exists("downloads"):
    os.makedirs("downloads")

def ensure_allowed(func):
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id if message.from_user else None
        if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
            await message.reply_text("🚫 Not authorized.")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

def parse_link(link: str):
    """Parse Telegram links - supports multiple formats"""
    if not link:
        return None, None
        
    link = link.strip().rstrip("/")
    
    # Private channel/supergroup: https://t.me/c/1234567/123
    if "/c/" in link:
        parts = link.split("/")
        if len(parts) >= 6:
            try:
                short_id = parts[4] 
                msg_id = int(parts[5].split("?")[0].split("-")[0])  # Handle ranges like 123-125
                chat_id = int(f"-100{short_id}")
                return chat_id, msg_id
            except (ValueError, IndexError):
                pass
    
    # Public channel/group: https://t.me/username/123
    elif "t.me/" in link and "/c/" not in link:
        parts = link.split("/")
        if len(parts) >= 5:
            try:
                username = parts[3]
                if username.startswith("@"):
                    username = username[1:]
                msg_id = int(parts[4].split("?")[0].split("-")[0])
                return username, msg_id
            except (ValueError, IndexError):
                pass
    
    return None, None

def parse_range(text: str):
    """Parse range from text like '123-130' or '123 - 130'"""
    text = text.strip().replace(" ", "")
    if "-" in text:
        try:
            start, end = text.split("-")
            return int(start), int(end)
        except:
            pass
    try:
        return int(text), int(text)
    except:
        return None, None

def start_health_server():
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass  # Suppress access logs
    
    def run():
        try:
            server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
            logger.info(f"Health server at 0.0.0.0:{PORT}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"Health server error: {e}")
    
    threading.Thread(target=run, daemon=True).start()

# Initialize Pyrogram client
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
        "🤖 **Save-Restricted Extractor Bot**\n\n"
        "**Commands:**\n"
        "• `/login` — login with phone, OTP, and 2FA\n"
        "• `/logout` — remove saved session\n"
        "• `/save <link>` — fetch one message/media\n"
        "• `/range <link> <start-end>` — fetch range (e.g. 100-110)\n"
        "• `/me` — show login status\n\n"
        "**Link formats:**\n"
        "• Public: `https://t.me/channel/123`\n"
        "• Private: `https://t.me/c/1234567/123`\n\n"
        "**Note:** You must be a member of private groups/channels."
    )

@app.on_message(filters.command(["me"]))
@ensure_allowed
async def cmd_me(client: Client, message: Message):
    sess = database.get_session(message.from_user.id)
    status = "✅ Logged in" if sess else "❌ Not logged in"
    await message.reply_text(f"**Status:** {status}")

@app.on_message(filters.command(["logout"]))
@ensure_allowed
async def cmd_logout(client: Client, message: Message):
    sess = database.get_session(message.from_user.id)
    if sess:
        database.save_session(message.from_user.id, "")
        await message.reply_text("✅ Session removed.")
    else:
        await message.reply_text("❌ No active session found.")

@app.on_message(filters.command(["login"]))
@ensure_allowed
async def cmd_login(bot: Client, message: Message):
    # Check if already logged in
    if database.get_session(message.from_user.id):
        await message.reply_text("✅ Already logged in. Use `/logout` to reset.")
        return
    
    user_id = message.from_user.id
    
    try:
        # Ask for phone number
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
        
        # Validate phone format
        if not phone.startswith("+") or len(phone) < 8:
            return await phone_msg.reply("❌ Invalid phone format. Use: +919876543210")
        
        # Create temporary user client for authentication
        u = Client(":memory:", api_id=API_ID, api_hash=API_HASH)
        await u.connect()
        
        await phone_msg.reply("📤 Sending OTP...")
        
        try:
            # Send verification code
            code = await u.send_code(phone)
        except PhoneNumberInvalid:
            await phone_msg.reply("❌ Invalid phone number.")
            await u.disconnect()
            return
        except FloodWait as e:
            await phone_msg.reply(f"⏳ Too many attempts. Wait {e.value} seconds.")
            await u.disconnect()
            return
        except Exception as e:
            await phone_msg.reply(f"❌ Error sending code: {str(e)}")
            await u.disconnect()
            return
        
        # Ask for OTP
        code_msg = await bot.ask(
            user_id, 
            "🔐 **Enter the OTP** you received\n\n"
            "Format: `1 2 3 4 5` (with spaces)\n"
            "Send `/cancel` to cancel.", 
            filters=filters.text, 
            timeout=300
        )
        
        if code_msg.text == "/cancel":
            await code_msg.reply("❌ Login cancelled.")
            await u.disconnect()
            return
        
        phone_code = code_msg.text.replace(" ", "").replace("-", "")
        
        try:
            # Sign in with OTP
            await u.sign_in(phone, code.phone_code_hash, phone_code)
            
        except PhoneCodeInvalid:
            await code_msg.reply("❌ Invalid OTP code.")
            await u.disconnect()
            return
        except PhoneCodeExpired:
            await code_msg.reply("❌ OTP expired. Try `/login` again.")
            await u.disconnect()
            return
        except SessionPasswordNeeded:
            # Handle 2FA
            pwd_msg = await bot.ask(
                user_id, 
                "🔒 **2FA enabled**\n\n"
                "Send your password:\n"
                "Send `/cancel` to cancel.", 
                filters=filters.text, 
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
        
        # Export and save session
        session_string = await u.export_session_string()
        await u.disconnect()
        
        database.save_session(user_id, session_string)
        
        await bot.send_message(
            user_id, 
            "✅ **Logged in successfully!**\n\n"
            "Session saved. You can now use `/save` and `/range` commands.\n\n"
            "⚠️ If you get **AUTH_KEY** errors later, use `/logout` then `/login` again."
        )
        
    except asyncio.TimeoutError:
        await message.reply("⏰ Timeout. Use `/login` to try again.")
    except Exception as e:
        await message.reply(f"❌ Login error: {str(e)}")

def get_user_client(user_id: int):
    """Get authenticated user client"""
    session_str = database.get_session(user_id)
    if not session_str:
        return None
    
    return Client(
        f":memory:", 
        session_string=session_str, 
        api_id=API_ID, 
        api_hash=API_HASH
    )

@app.on_message(filters.command(["save"]))
@ensure_allowed
async def cmd_save(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "**Usage:** `/save <telegram_link>`\n\n"
            "**Examples:**\n"
            "• `/save https://t.me/channel/123`\n"
            "• `/save https://t.me/c/1234567/123`"
        )
    
    link = message.command[1]
    target, msg_id = parse_link(link)
    
    if target is None:
        return await message.reply_text(
            f"❌ **Cannot parse link:**\n`{link}`\n\n"
            "**Supported formats:**\n"
            "• `https://t.me/channel/123`\n"
            "• `https://t.me/c/1234567/123`"
        )
    
    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")
    
    try:
        await u.connect()
        status_msg = await message.reply_text(f"🔍 **Fetching message {msg_id}** from `{target}`...")
        
        # Get the message
        msg = await u.get_messages(target, msg_id)
        
        if not msg or msg.empty:
            return await status_msg.edit_text(
                "⚠️ **Message not found**\n\n"
                "**Check if:**\n"
                "• You're a member of this chat\n"
                "• Message ID exists\n"
                "• Link is correct"
            )
        
        # Handle media messages
        if msg.media:
            await status_msg.edit_text("📥 **Downloading media...**")
            try:
                file_path = await u.download_media(msg, file_name="downloads/")
                if file_path and os.path.exists(file_path):
                    # Send the file
                    with open(file_path, 'rb') as f:
                        if msg.photo:
                            await message.reply_photo(f, caption=f"📷 Message {msg_id}")
                        elif msg.video:
                            await message.reply_video(f, caption=f"🎥 Message {msg_id}")
                        elif msg.document:
                            await message.reply_document(f, caption=f"📄 Message {msg_id}")
                        else:
                            await message.reply_document(f, caption=f"📎 Message {msg_id}")
                    
                    # Clean up
                    os.remove(file_path)
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ **Failed to download media**")
            except Exception as e:
                await status_msg.edit_text(f"❌ **Download error:** {str(e)}")
        
        # Handle text messages
        elif msg.text:
            await status_msg.delete()
            await message.reply_text(
                f"📄 **Message {msg_id}:**\n\n{msg.text}",
                disable_web_page_preview=True
            )
        
        else:
            await status_msg.edit_text("⚠️ **Message has no text or media**")
            
    except FloodWait as e:
        await message.reply_text(f"⏳ **Rate limit:** Wait {e.value} seconds and try again")
    except UserNotParticipant:
        await message.reply_text("❌ **Not a member** of this chat")
    except ChannelPrivate:
        await message.reply_text("❌ **Private channel** - join first or check link")
    except PeerIdInvalid:
        await message.reply_text("❌ **Invalid chat** - check the link")
    except MessageIdInvalid:
        await message.reply_text("❌ **Invalid message ID** - check the number")
    except Exception as e:
        await message.reply_text(f"❌ **Error:** {str(e)}")
    finally:
        try:
            await u.disconnect()
        except:
            pass

@app.on_message(filters.command(["range"]))
@ensure_allowed
async def cmd_range(client: Client, message: Message):
    if len(message.command) < 3:
        return await message.reply_text(
            "**Usage:** `/range <link> <start-end>`\n\n"
            "**Examples:**\n"
            "• `/range https://t.me/channel/123 100-110`\n"
            "• `/range https://t.me/c/1234567/123 5-15`\n\n"
            "**Max 50 messages per batch**"
        )
    
    link = message.command[1]
    range_text = message.command[2]
    
    target, _ = parse_link(link)
    if target is None:
        return await message.reply_text(f"❌ **Invalid link format:** `{link}`")
    
    start_id, end_id = parse_range(range_text)
    if start_id is None or end_id is None:
        return await message.reply_text(f"❌ **Invalid range:** `{range_text}`\n\nUse format: `100-110`")
    
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    
    if end_id - start_id > 50:
        return await message.reply_text("❌ **Range too large**\n\nMax 50 messages per batch")
    
    u = get_user_client(message.from_user.id)
    if not u:
        return await message.reply_text("❌ Not logged in. Use `/login` first.")
    
    try:
        await u.connect()
        status_msg = await message.reply_text(f"📦 **Fetching {start_id} → {end_id}** from `{target}`...")
        
        success_count = 0
        error_count = 0
        
        for msg_id in range(start_id, end_id + 1):
            try:
                msg = await u.get_messages(target, msg_id)
                
                if not msg or msg.empty:
                    error_count += 1
                    continue
                
                # Send media
                if msg.media:
                    file_path = await u.download_media(msg, file_name="downloads/")
                    if file_path and os.path.exists(file_path):
                        with open(file_path, 'rb') as f:
                            if msg.photo:
                                await message.reply_photo(f, caption=f"📷 {msg_id}")
                            elif msg.video:
                                await message.reply_video(f, caption=f"🎥 {msg_id}")
                            else:
                                await message.reply_document(f, caption=f"📄 {msg_id}")
                        os.remove(file_path)
                        success_count += 1
                    else:
                        error_count += 1
                
                # Send text
                elif msg.text:
                    await message.reply_text(
                        f"📄 **{msg_id}:** {msg.text[:1000]}{'...' if len(msg.text) > 1000 else ''}",
                        disable_web_page_preview=True
                    )
                    success_count += 1
                else:
                    error_count += 1
                
                # Update progress every 10 messages
                if (msg_id - start_id + 1) % 10 == 0:
                    await status_msg.edit_text(
                        f"📦 **Progress:** {msg_id}/{end_id}\n"
                        f"✅ Success: {success_count} | ❌ Failed: {error_count}"
                    )
                
            except FloodWait as e:
                await status_msg.edit_text(f"⏳ **Rate limit at {msg_id}:** Waiting {e.value}s...")
                await asyncio.sleep(e.value + 1)
            except Exception:
                error_count += 1
                continue
        
        # Final summary
        await status_msg.edit_text(
            f"✅ **Range complete!**\n\n"
            f"**Downloaded:** {success_count}\n"
            f"**Failed:** {error_count}\n"
            f"**Range:** {start_id}-{end_id}"
        )
        
    except Exception as e:
        await message.reply_text(f"❌ **Range error:** {str(e)}")
    finally:
        try:
            await u.disconnect()
        except:
            pass

if __name__ == "__main__":
    start_health_server()
    logger.info("Starting Telegram Save-Restricted Bot...")
    app.run()
