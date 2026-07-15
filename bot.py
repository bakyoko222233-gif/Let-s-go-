import os
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import asyncio

api_id = 33243817
api_hash = '84b76a174eabcccd6bba85ec9eb4daf3'
SESSION_STRING = os.getenv('SESSION_STRING')

client = TelegramClient(StringSession(SESSION_STRING), api_id, api_hash)

@client.on(events.NewMessage(chats=-1002380293749))
async def forward_handler(event):
    await event.forward_to(-5134396719)
    print(f"Forwarded: {event.message.text[:50]}")

async def main():
    await client.start()
    print("Bot running - forwarding all messages")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
