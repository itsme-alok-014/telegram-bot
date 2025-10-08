import re

def parse_message_link(link: str):
    if not link:
        return None, None
    link = link.strip().rstrip("/")

    # Private channel/group: https://t.me/c/<short_id>/<msg_id>
    m = re.match(r'^https?://t\.me/c/(\d+)/(\d+)$', link)
    if m:
        short_id = m.group(1)
        msg_id = int(m.group(2))
        chat_id = int(f"-100{short_id}")
        return chat_id, msg_id

    # Public channel/group: https://t.me/<username>/<msg_id>
    m = re.match(r'^https?://t\.me/([\w\d_]+)/(\d+)$', link)
    if m:
        username = m.group(1)
        msg_id = int(m.group(2))
        return username, msg_id

    return None, None

def clamp_int(text: str, default=None):
    try:
        return int(text)
    except Exception:
        return default
