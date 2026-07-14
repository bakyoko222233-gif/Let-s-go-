import os, sys, asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

api_id, api_hash = 33243817, '84b76a174eabcccd6bba85ec9eb4daf3'
SESSION_STRING = os.getenv('SESSION_STRING', '')
MONITOR_CHANNEL = -1002380293749
FORWARD_CHANNEL = -5134396719

async def main():
    if not SESSION_STRING:
        print("❌ SESSION_STRING not set!", flush=True)
        sys.exit(1)
    
    client = TelegramClient(StringSession(SESSION_STRING), api_id, api_hash)
    
    @client.on(events.NewMessage(chats=MONITOR_CHANNEL))
    async def handler(event):
        text = event.message.message
        print(f"📡 DETECTED: {text[:100]}", flush=True)
        
        try:
            await client.send_message(FORWARD_CHANNEL, f"RAW:\n\n{text}")
            print(f"✅ FORWARDED", flush=True)
        except Exception as e:
            print(f"❌ ERROR: {e}", flush=True)
    
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ SESSION EXPIRED", flush=True)
        sys.exit(1)
    
    print("✅ Bot Running - forwarding ALL messages!", flush=True)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
