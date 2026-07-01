import os
import sys
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

api_id   = 33243817
api_hash = '84b76a174eabcccd6bba85ec9eb4daf3'
SESSION_STRING = os.getenv('SESSION_STRING', '')
CHANNEL_ID = -1002380293749

async def main():
    if not SESSION_STRING:
        print("❌ SESSION_STRING not set!", flush=True)
        return
    
    print("🔑 Connecting...", flush=True)
    
    client = TelegramClient(StringSession(SESSION_STRING), api_id, api_hash)
    
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ SESSION EXPIRED!", flush=True)
        return
    
    print("✅ Connected!", flush=True)
    
    # Try to read channel
    try:
        async for message in client.iter_messages(CHANNEL_ID, limit=5):
            print(f"📨 Message: {message.text[:100]}", flush=True)
        print("✅ Can read channel!", flush=True)
    except Exception as e:
        print(f"❌ Cannot read channel: {e}", flush=True)
    
    # Set up event listener
    @client.on(events.NewMessage(chats=CHANNEL_ID))
    async def handler(event):
        print(f"🎯 NEW MESSAGE DETECTED: {event.message.text[:100]}", flush=True)
    
    print("👂 Listening for new messages... (Press Ctrl+C to stop)", flush=True)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
