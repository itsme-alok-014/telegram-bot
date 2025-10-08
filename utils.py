# utils.py
import re

def parse_message_link(link: str):
    """
    Parse a Telegram message link. Returns (chat, msg_id):
      - For private chats/channels (t.me/c/12345/67), returns (chat_id, msg_id)
        where chat_id = -10012345 (the Telethon chat_id includes -100 prefix:contentReference[oaicite:4]{index=4}).
      - For public (t.me/<username>/67), returns (username, msg_id).
      - If parsing fails, returns (None, None).
    """
    # Private channel link (numeric ID)
    m = re.match(r'https?://t\.me/c/(\d+)/(\d+)', link)
    if m:
        raw_id = m.group(1)
        msg_id = int(m.group(2))
        # Telethon uses full chat ID (with -100 prefix)
        chat_id = int(f"-100{raw_id}")
        return chat_id, msg_id

    # Public channel or group link
    m = re.match(r'https?://t\.me/([\w\d_]+)/(\d+)', link)
    if m:
        username = m.group(1)
        msg_id = int(m.group(2))
        return username, msg_id

    return None, None
