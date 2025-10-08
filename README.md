# Telegram Restricted Content Saver Bot

A simple Telegram bot that lets a user log in with their personal Telegram account (via Telethon session string) and save/download messages (including media) from private/restricted groups/channels where they are a member.

> NOTE: This bot acts on *user* sessions (Telethon) — it can access content only from chats where the logged-in user is present.

## Features
- `/login` : login with phone → save session (StringSession)
- `/save <t.me link>` : save a single message (downloads media or sends text)
- `/batch <start_id> <end_id>` : (optional) batch-download by message id range
- Stores session strings in a local SQLite DB (`sessions.db` by default)

## Quick start (local)
1. Create a Python virtual env and install:
```bash
python -m venv venv
source venv/bin/activate       # or venv\Scripts\activate on Windows
pip install -r requirements.txt
