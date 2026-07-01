import os
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

api_id   = 33243817
api_hash = '84b76a174eabcccd6bba85ec9eb4daf3'
SESSION_STRING = os.getenv('SESSION_STRING', '')
FORWARD_CHANNEL = -5134396719

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
    
    # Try to send test message
    try:
        test_msg = "🧪 TEST MESSAGE FROM BOT"
        await client.send_message(FORWARD_CHANNEL, test_msg)
        print(f"✅ SENT TEST MESSAGE TO {FORWARD_CHANNEL}!", flush=True)
    except Exception as e:
        print(f"❌ CANNOT SEND: {e}", flush=True)
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
