import os, re, asyncio, aiohttp, logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Suppress telethon warnings
logging.basicConfig(level=logging.WARNING)
for logger_name in ['telethon']:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

api_id = 33243817
api_hash = '84b76a174eabcccd6bba85ec9eb4daf3'
SESSION_STRING = os.getenv('SESSION_STRING')

MONITOR_CHANNEL = -1002380293749
FORWARD_CHANNEL = -5134396719
ATH_CHECK_INTERVAL = 5 * 60
DEAD_MC_THRESHOLD = 5000

tracking = {}

def get_milestone(mult):
    if mult < 2: return None
    elif mult < 3: return 2
    elif mult < 4: return 3
    elif mult < 5: return 4
    elif mult < 10: return 5
    elif mult < 15: return 10
    elif mult < 20: return 15
    elif mult < 50: return 20
    elif mult < 100: return 50
    else: return int(mult)

async def get_price_and_mc(ca):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200: return None, None
                data = await resp.json()
                pairs = data.get('pairs') or []
                if not pairs: return None, None
                sol_pairs = [p for p in pairs if p.get('chainId') == 'solana']
                if not sol_pairs: sol_pairs = pairs
                best = max(sol_pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
                price = best.get('priceUsd')
                mc = best.get('marketCap') or best.get('fdv')
                return (float(price) if price else None, float(mc) if mc else None)
    except:
        return None, None

def extract_metrics(text):
    metrics = {}
    
    name_match = re.search(r'^([^\n]+?)\s*\n', text)
    metrics['name'] = name_match.group(1) if name_match else 'Unknown'
    
    ca_match = re.search(r'([1-9A-HJ-NP-Za-km-z]{32,44}pump)', text)
    metrics['ca'] = ca_match.group(1) if ca_match else 'N/A'
    
    cap_match = re.search(r'Cap:\s*([0-9.]+)([KMB]?)', text)
    if cap_match:
        cap_val = float(cap_match.group(1))
        cap_unit = cap_match.group(2) or 'K'
        mult = {'K': 1000, 'M': 1_000_000, 'B': 1_000_000_000}
        metrics['cap'] = cap_val * mult.get(cap_unit, 1)
        metrics['cap_str'] = f"{cap_match.group(1)}{cap_match.group(2) or 'K'}"
    else:
        metrics['cap'] = 0
        metrics['cap_str'] = 'N/A'
    
    age_match = re.search(r'⌛️\s*([0-9]+)m', text)
    metrics['age'] = age_match.group(1) if age_match else 'N/A'
    
    vol_match = re.search(r'Vol:\s*([0-9.]+)([KMB]?)', text)
    metrics['vol'] = f"{vol_match.group(1)}{vol_match.group(2) or 'K'}" if vol_match else 'N/A'
    
    buy_match = re.search(r'🅑\s*(\d+)', text)
    metrics['buy_tx'] = buy_match.group(1) if buy_match else 'N/A'
    
    sell_match = re.search(r'🅢\s*(\d+)', text)
    metrics['sell_tx'] = sell_match.group(1) if sell_match else 'N/A'
    
    bonding_match = re.search(r'Bonding Curve:\s*([0-9.]+)%', text)
    metrics['bonding'] = bonding_match.group(1) if bonding_match else 'N/A'
    
    holders_match = re.search(r'TH:\s*(\d+)', text)
    metrics['holders'] = holders_match.group(1) if holders_match else 'N/A'
    
    top10_match = re.search(r'Top 10:\s*([0-9.]+)%', text)
    metrics['top10'] = top10_match.group(1) if top10_match else 'N/A'
    
    dist_match = re.search(r'Top 10:\s*[0-9.]+%\s*\n\s*└([0-9.\|]+)', text)
    metrics['distribution'] = dist_match.group(1) if dist_match else 'N/A'
    
    buy_pct_match = re.search(r'Sum 🅑:([0-9.]+)%', text)
    metrics['buy_pct'] = buy_pct_match.group(1) if buy_pct_match else 'N/A'
    
    sell_pct_match = re.search(r'Sum 🅢:\s*([0-9.]+)%', text)
    metrics['sell_pct'] = sell_pct_match.group(1) if sell_pct_match else 'N/A'
    
    sniper_match = re.search(r'Sniper:\s*(\d+)\s+buy\s+([0-9.]+)%\s+with\s+([0-9.]+)\s+SOL', text)
    if sniper_match:
        metrics['snipers'] = f"{sniper_match.group(1)} buy {sniper_match.group(2)}% with {sniper_match.group(3)} SOL"
    else:
        metrics['snipers'] = 'N/A'
    
    bundle_match = re.search(r'Bundle:\s*(\d+)(?:\s+buy\s+([0-9.]+)%)?', text)
    if bundle_match and bundle_match.group(1) != '0':
        metrics['bundles'] = f"{bundle_match.group(1)} buy {bundle_match.group(2) or '0'}%"
    else:
        metrics['bundles'] = '0'
    
    kols_match = re.search(r'KOLs:\s*(\d+)', text)
    metrics['kols'] = kols_match.group(1) if kols_match else 'N/A'
    
    insiders_match = re.search(r'Insiders:\s*(\d+)', text)
    metrics['insiders'] = insiders_match.group(1) if insiders_match else 'N/A'
    
    hold_match = re.search(r'🔴\s+Hold\s+(\d+)', text)
    metrics['hold'] = hold_match.group(1) if hold_match else 'N/A'
    
    sold_part_match = re.search(r'🟡\s+Sold part\s+(\d+)', text)
    metrics['sold_part'] = sold_part_match.group(1) if sold_part_match else 'N/A'
    
    sold_match = re.search(r'🟢\s+Sold\s+(\d+)', text)
    metrics['sold'] = sold_match.group(1) if sold_match else 'N/A'
    
    dev_match = re.search(r'Dev:(✅|❌)', text)
    metrics['dev'] = '✅ SOLD' if dev_match and dev_match.group(1) == '✅' else '❌ HOLDING'
    
    return metrics

def format_milestone(mult, metrics, current_mc):
    if mult >= 100: icon = "🚀🚀🚀"
    elif mult >= 50: icon = "🚀🚀"
    elif mult >= 20: icon = "🚀"
    else: icon = "📈"
    
    return f"""{icon} **{mult:.2f}x MILESTONE!**

📊 **Token:** {metrics['name']}
🎯 **CA:** `{metrics['ca']}`

💰 **Entry:** ${metrics['cap']:,.0f}
📈 **Current:** ${current_mc:,.0f}
✅ **Multiplier:** {mult:.2f}x

📋 **Metrics:**
├─ Age: {metrics['age']}m
├─ Cap: {metrics['cap_str']}
├─ Vol: {metrics['vol']}
├─ Buy Txs: {metrics['buy_tx']}
├─ Sell Txs: {metrics['sell_tx']}
├─ Bonding: {metrics['bonding']}%
├─ Holders: {metrics['holders']}
├─ Top10: {metrics['top10']}%
├─ Distribution: {metrics['distribution']}
├─ Buy %: {metrics['buy_pct']}%
├─ Sell %: {metrics['sell_pct']}%
├─ Snipers: {metrics['snipers']}
├─ Bundles: {metrics['bundles']}
├─ KOLs: {metrics['kols']}
├─ Insiders: {metrics['insiders']}
├─ Hold: {metrics['hold']}
├─ Sold Part: {metrics['sold_part']}
├─ Sold: {metrics['sold']}
└─ Dev: {metrics['dev']}"""

def format_final(metrics, entry_mc, ath_mc, mult, elapsed_min, outcome):
    if outcome == "WIN": icon = "🟢🟢🟢" if mult >= 5 else "🟢🟢" if mult >= 3 else "🟢"
    else: icon = "🔴"
    
    return f"""{icon} **{outcome} - {mult:.2f}x**

📊 **Token:** {metrics['name']}
🎯 **CA:** `{metrics['ca']}`

💰 **Entry:** ${entry_mc:,.0f}
📈 **ATH:** ${ath_mc:,.0f}
✅ **Multiplier:** {mult:.2f}x
⏱️ **Elapsed:** {elapsed_min}m"""

async def track_ath(ca, metrics, client):
    entry_mc = metrics['cap']
    name = metrics['name']
    ath_mc = entry_mc
    ath_mult = 1.0
    elapsed = 0
    last_milestone = None
    last_msg_id = None
    
    print(f"🚀 Tracking {name}: ${entry_mc:,.0f}", flush=True)
    
    while True:
        try:
            await asyncio.sleep(ATH_CHECK_INTERVAL)
            elapsed += ATH_CHECK_INTERVAL
            
            price, mc = await get_price_and_mc(ca)
            if mc is None: continue
            
            mult = mc / entry_mc if entry_mc > 0 else 0
            if mc > ath_mc:
                ath_mc = mc
                ath_mult = mult
            
            print(f"📊 {name[:20]} ${mc:,.0f} {mult:.2f}x (ATH: {ath_mult:.2f}x)", flush=True)
            
            milestone = get_milestone(ath_mult)
            if milestone and milestone != last_milestone:
                print(f"🎯 Milestone {milestone}x!", flush=True)
                msg_text = format_milestone(milestone, metrics, ath_mc)
                
                try:
                    if last_msg_id:
                        try:
                            await client.delete_messages(FORWARD_CHANNEL, last_msg_id)
                        except: pass
                    
                    response = await client.send_message(FORWARD_CHANNEL, msg_text)
                    last_msg_id = response.id
                    last_milestone = milestone
                    print(f"✅ Sent {milestone}x", flush=True)
                except Exception as e:
                    print(f"Error: {e}", flush=True)
            
            if mc <= DEAD_MC_THRESHOLD:
                outcome = "WIN" if ath_mult >= 2.0 else "LOSS"
                elapsed_min = elapsed // 60
                
                print(f"💀 {outcome} {ath_mult:.2f}x", flush=True)
                
                if last_msg_id:
                    try:
                        await client.delete_messages(FORWARD_CHANNEL, last_msg_id)
                    except: pass
                
                msg_text = format_final(metrics, entry_mc, ath_mc, ath_mult, elapsed_min, outcome)
                try:
                    await client.send_message(FORWARD_CHANNEL, msg_text)
                except: pass
                
                break
        except Exception as e:
            print(f"Track error: {e}", flush=True)
            await asyncio.sleep(10)

async def create_client():
    client = TelegramClient(StringSession(SESSION_STRING), api_id, api_hash)
    
    @client.on(events.NewMessage(chats=MONITOR_CHANNEL))
    async def message_handler(event):
        print(f"📡 Message detected", flush=True)
        text = event.message.text
        if not text or 'pump' not in text: return
        
        try:
            metrics = extract_metrics(text)
            if metrics['cap'] <= 0: return
            
            ca = metrics['ca']
            if ca in tracking: return
            
            tracking[ca] = True
            print(f"📡 NEW: {metrics['name']} ${metrics['cap']:,.0f}", flush=True)
            asyncio.create_task(track_ath(ca, metrics, client))
        except Exception as e:
            print(f"Error: {e}", flush=True)
    
    return client

async def main():
    print("=" * 60, flush=True)
    print("🔑 Bot Starting...", flush=True)
    print("=" * 60, flush=True)
    
    while True:
        client = None
        try:
            client = await create_client()
            await client.connect()
            print("✅ Connected!", flush=True)
            print(f"Monitor: {MONITOR_CHANNEL}", flush=True)
            print(f"Forward: {FORWARD_CHANNEL}", flush=True)
            await client.run_until_disconnected()
        except Exception as e:
            print(f"⚠️ Connection error: {e}", flush=True)
        finally:
            if client:
                try:
                    await client.disconnect()
                except: pass
            print("⏳ Reconnecting in 10s...", flush=True)
            await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(main())
