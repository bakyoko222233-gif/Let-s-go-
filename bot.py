import os, sys, re, asyncio, aiohttp, json
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import AuthKeyDuplicatedError
from asyncio import Lock

api_id, api_hash = 33243817, '84b76a174eabcccd6bba85ec9eb4daf3'
SESSION_STRING = os.getenv('SESSION_STRING', '')

CHANNEL_MAPPING = {
    'pumpfun_ultimate': {
        'monitor': -1002380293749,
        'forward_channel': -5134396719,
    }
}

TRACKING_FILES = {ch: f"tracking_{ch}.json" for ch in CHANNEL_MAPPING.keys()}
processing_locks = {ch: Lock() for ch in CHANNEL_MAPPING.keys()}
ATH_CHECK_INTERVAL, DEAD_MC_THRESHOLD = 5 * 60, 5000

# Milestone messages - store message IDs to delete when new milestone reached
MILESTONE_MESSAGES = {}  # {ca: {'2x': msg_id, '3x': msg_id, ...}}

PUMPFUN_PATTERNS = {
    'token_name': r'^([^\n]+?)\s*\n([1-9A-HJ-NP-Za-km-z]{32,44}pump)',
    'ca': r'([1-9A-HJ-NP-Za-km-z]{32,44}pump)',
    'scanner': r'by\s+([^\n]+)',
    'mc': r'Cap:\s*([0-9.]+)([KMB]?)',
    'age': r'🕐\s*([0-9]+h:[0-9]+m|[0-9]+h|[0-9]+m|[0-9]+s)',
    'volume_and_txs': r'Vol:\s*([0-9.]+)([KMB]?)\s*\|\s*●\s*(\d+)\s*\|\s*●\s*(\d+)',
    'bonding_curve': r'Bonding Curve:\s*([0-9.]+)%',
    'dev_status': r'Dev:\s*(✅|❌)',
    'insiders': r'Insiders:\s*(\d+)',
    'kols': r'KOLs:\s*(\d+)',
    'total_holders': r'TH:\s*(\d+)',
    'top10_pct': r'Top 10:\s*([0-9.]+)%',
    'holder_distribution': r'Top 10:.*?\n\s*└([0-9.\s|]+)',
    'snipers': r'Sniper:\s*(\d+)\s+buy\s+([0-9.]+)%\s+with\s+([0-9.]+)\s*SOL',
    'bundles': r'Bundle:\s*(\d+)\s+buy\s+([0-9.]+)%',
    'buy_pct': r'Sum\s+●:([0-9.]+)%\s*\|',
    'sell_pct': r'Sum\s+●:\s*([0-9.]+)%(?!\s*\|)',
}

def load_seen(path):
    try:
        with open(path, 'r') as f: return set(json.load(f))
    except: return set()

def save_seen(seen, path):
    try:
        with open(path, 'w') as f: json.dump(list(seen), f)
    except: pass

def load_tracking(path):
    try:
        with open(path, 'r') as f: return json.load(f)
    except: return {}

def save_tracking(state, path):
    try:
        with open(path, 'w') as f: json.dump(state, f, indent=2)
    except: pass

def add_tracking(ca, metrics, path):
    state = load_tracking(path)
    if ca not in state:
        state[ca] = {
            'ca': ca,
            'token_name': metrics.get('token_name', 'Unknown'),
            'entry_mc': metrics.get('mc', 0),
            'ath_mc': metrics.get('mc', 0),
            'ath_mult': 1.0,
        }
        save_tracking(state, path)

def update_tracking(ca, path, **kwargs):
    state = load_tracking(path)
    if ca in state:
        state[ca].update(kwargs)
        save_tracking(state, path)

def remove_tracking(ca, path):
    state = load_tracking(path)
    if ca in state:
        del state[ca]
        save_tracking(state, path)

def get_milestone(mult):
    """Get milestone for multiplier (2x, 3x, 4x, 5x, 10x, 15x, 20x, 50x, 100x)"""
    if mult < 2: return None
    elif mult < 3: return 2
    elif mult < 4: return 3
    elif mult < 5: return 4
    elif mult < 10: return 5
    elif mult < 15: return 10
    elif mult < 20: return 15
    elif mult < 50: return 20
    elif mult < 100: return 50
    else: return int(mult)  # For 100x+, use exact multiple

def parse_pumpfun(text):
    metrics = {}
    
    # DEBUG: Print first 500 chars of message
    print(f"DEBUG RAW TEXT: {text[:500]}", flush=True)
    
    token_match = re.search(PUMPFUN_PATTERNS['token_name'], text, re.MULTILINE)
    if token_match:
        metrics['token_name'] = token_match.group(1).strip()
        metrics['ca'] = token_match.group(2)
    else:
        ca_match = re.search(PUMPFUN_PATTERNS['ca'], text)
        if ca_match:
            metrics['ca'] = ca_match.group(1)
            metrics['token_name'] = 'Unknown'
        else: return {}
    
    scanner_match = re.search(PUMPFUN_PATTERNS['scanner'], text)
    metrics['scanner'] = scanner_match.group(1) if scanner_match else 'Unknown'
    
    mc_match = re.search(PUMPFUN_PATTERNS['mc'], text)
    if mc_match:
        mc_val = float(mc_match.group(1))
        unit = mc_match.group(2) or 'K'
        mult = {'K': 1000, 'M': 1_000_000, 'B': 1_000_000_000}
        metrics['mc'] = mc_val * mult.get(unit, 1)
    else: metrics['mc'] = 0
    
    age_match = re.search(PUMPFUN_PATTERNS['age'], text)
    if age_match:
        age_str = age_match.group(1)
        print(f"DEBUG Age: {age_str}", flush=True)
        if 'h:' in age_str:
            h, m = age_str.split('h:')
            metrics['age_min'] = int(h) * 60 + int(m.replace('m', ''))
        elif 'h' in age_str:
            metrics['age_min'] = int(age_str.replace('h', '')) * 60
        elif 'm' in age_str:
            metrics['age_min'] = int(age_str.replace('m', ''))
        elif 's' in age_str:
            metrics['age_min'] = int(age_str.replace('s', '')) / 60
        else: metrics['age_min'] = 0
    else: 
        metrics['age_min'] = 0
        print(f"DEBUG Age NOT FOUND", flush=True)
    
    vol_txs_match = re.search(PUMPFUN_PATTERNS['volume_and_txs'], text)
    if vol_txs_match:
        vol_val = float(vol_txs_match.group(1))
        unit = vol_txs_match.group(2) or 'K'
        mult = {'K': 1000, 'M': 1_000_000, 'B': 1_000_000_000}
        metrics['vol_5m'] = vol_val * mult.get(unit, 1)
        metrics['buy_tx'] = int(vol_txs_match.group(3))
        metrics['sell_tx'] = int(vol_txs_match.group(4))
        print(f"DEBUG Vol/Txs: {metrics['vol_5m']} / {metrics['buy_tx']} / {metrics['sell_tx']}", flush=True)
    else:
        metrics['vol_5m'] = 0
        metrics['buy_tx'] = 0
        metrics['sell_tx'] = 0
        print(f"DEBUG Vol/Txs NOT FOUND", flush=True)
    
    bonding_match = re.search(PUMPFUN_PATTERNS['bonding_curve'], text)
    metrics['bonding_curve_pct'] = float(bonding_match.group(1)) if bonding_match else 0
    
    dev_match = re.search(PUMPFUN_PATTERNS['dev_status'], text)
    metrics['dev_sold'] = (dev_match.group(1) == '✅') if dev_match else False
    
    insiders = re.search(PUMPFUN_PATTERNS['insiders'], text)
    metrics['insiders'] = int(insiders.group(1)) if insiders else 0
    
    kols = re.search(PUMPFUN_PATTERNS['kols'], text)
    metrics['kols'] = int(kols.group(1)) if kols else 0
    
    holders = re.search(PUMPFUN_PATTERNS['total_holders'], text)
    metrics['total_holders'] = int(holders.group(1)) if holders else 0
    
    top10 = re.search(PUMPFUN_PATTERNS['top10_pct'], text)
    metrics['top10_pct'] = float(top10.group(1)) if top10 else 0
    
    holder_dist = re.search(PUMPFUN_PATTERNS['holder_distribution'], text)
    metrics['holder_distribution'] = holder_dist.group(1).strip() if holder_dist else ''
    
    sniper = re.search(PUMPFUN_PATTERNS['snipers'], text)
    if sniper:
        metrics['snipers'] = int(sniper.group(1))
        metrics['sniper_pct'] = float(sniper.group(2))
        metrics['sniper_sol'] = float(sniper.group(3))
    else:
        metrics['snipers'], metrics['sniper_pct'], metrics['sniper_sol'] = 0, 0, 0
    
    bundle = re.search(PUMPFUN_PATTERNS['bundles'], text)
    if bundle:
        metrics['bundles'] = int(bundle.group(1))
        metrics['bundle_pct'] = float(bundle.group(2))
    else:
        metrics['bundles'], metrics['bundle_pct'] = 0, 0
    
    buy_pct_match = re.search(PUMPFUN_PATTERNS['buy_pct'], text)
    metrics['buy_pct'] = float(buy_pct_match.group(1)) if buy_pct_match else 0
    
    sell_pct_match = re.search(PUMPFUN_PATTERNS['sell_pct'], text)
    metrics['sell_pct'] = float(sell_pct_match.group(1)) if sell_pct_match else 0
    
    print(f"DEBUG Parsed: Age={metrics['age_min']}, Vol={metrics['vol_5m']}, Buy%={metrics['buy_pct']}", flush=True)
    
    return metrics

async def get_price_and_mc(ca: str):
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
                price, mc = best.get('priceUsd'), best.get('marketCap') or best.get('fdv')
                return (float(price) if price else None, float(mc) if mc else None)
    except: return None, None

def format_milestone(token_name, ca, metrics, ath_mult, ath_mc, elapsed_min):
    """Format milestone message with FULL token info"""
    if ath_mult >= 100:
        icon = "🚀🚀🚀"
    elif ath_mult >= 50:
        icon = "🚀🚀"
    elif ath_mult >= 20:
        icon = "🚀"
    else:
        icon = "📈"
    
    msg = f"""{icon} **{ath_mult:.2f}x MILESTONE!**

📊 **Token:** {token_name}
🎯 **CA:** `{ca}`

💰 **Entry:** ${metrics.get('entry_mc', 0):,.0f}
📈 **Current:** ${ath_mc:,.0f}
✅ **Multiplier:** {ath_mult:.2f}x
⏱️ **Elapsed:** {elapsed_min}m

📋 **Entry Metrics:**
├─ Age: {metrics.get('age_min', 0):.0f}m
├─ Bonding: {metrics.get('bonding_curve_pct', 0):.1f}%
├─ Holders: {metrics.get('total_holders', 0)}
├─ Top10: {metrics.get('top10_pct', 0):.1f}%
├─ KOLs: {metrics.get('kols', 0)} | Insiders: {metrics.get('insiders', 0)}
├─ Snipers: {metrics.get('snipers', 0)} ({metrics.get('sniper_pct', 0):.1f}%)
├─ Bundles: {metrics.get('bundles', 0)} ({metrics.get('bundle_pct', 0):.1f}%)
└─ Dev: {'✅ SOLD' if metrics.get('dev_sold') else '❌ HOLDING'}

🔍 **Activity:**
├─ Vol: ${metrics.get('vol_5m', 0):,.0f}
├─ Buy/Sell Txs: {metrics.get('buy_tx', 0)} / {metrics.get('sell_tx', 0)}
└─ Scanner: {metrics.get('scanner', 'Unknown')}
"""
    return msg.strip()

def format_final(token_name, ca, metrics, entry_mc, ath_mc, ath_mult, outcome, elapsed_min):
    if outcome == "WIN":
        icon = "🟢🟢🟢" if ath_mult >= 5 else "🟢🟢" if ath_mult >= 3 else "🟢"
    else:
        icon = "🔴"
    
    msg = f"""{icon} **{outcome} - {ath_mult:.2f}x**

📊 **Token:** {token_name}
🎯 **CA:** `{ca}`

💰 **Entry:** ${entry_mc:,.0f}
📈 **ATH:** ${ath_mc:,.0f}
✅ **Multiplier:** {ath_mult:.2f}x
⏱️ **Elapsed:** {elapsed_min}m

📋 **Entry Metrics:**
├─ Age: {metrics.get('age_min', 0):.0f}m
├─ Bonding: {metrics.get('bonding_curve_pct', 0):.1f}%
├─ Holders: {metrics.get('total_holders', 0)}
├─ Top10: {metrics.get('top10_pct', 0):.1f}%
├─ Distribution: {metrics.get('holder_distribution', 'N/A')}
├─ KOLs: {metrics.get('kols', 0)} | Insiders: {metrics.get('insiders', 0)}
├─ Snipers: {metrics.get('snipers', 0)} ({metrics.get('sniper_pct', 0):.1f}%)
├─ Bundles: {metrics.get('bundles', 0)} ({metrics.get('bundle_pct', 0):.1f}%)
├─ Buy/Sell Txs: {metrics.get('buy_tx', 0)} / {metrics.get('sell_tx', 0)}
├─ Buy/Sell %: {metrics.get('buy_pct', 0):.1f}% / {metrics.get('sell_pct', 0):.1f}%
└─ Dev: {'✅ SOLD' if metrics.get('dev_sold') else '❌ HOLDING'}

🔍 **Activity:**
├─ Vol: ${metrics.get('vol_5m', 0):,.0f}
└─ Scanner: {metrics.get('scanner', 'Unknown')}
"""
    return msg.strip()

async def track_ath(ca: str, metrics: dict, forward_channel, client):
    entry_mc = metrics.get('mc', 0)
    token_name = metrics.get('token_name', 'Unknown')
    
    print(f"🚀 {token_name} entry: ${entry_mc:,.0f}", flush=True)
    
    ath_mc, ath_mult, elapsed = entry_mc, 1.0, 0
    last_milestone = None
    last_milestone_msg_id = None
    
    while True:
        await asyncio.sleep(ATH_CHECK_INTERVAL)
        elapsed += ATH_CHECK_INTERVAL
        
        price, mc = await get_price_and_mc(ca)
        if mc is None: continue
        
        mult = mc / entry_mc if entry_mc > 0 else 0
        if mc > ath_mc:
            ath_mc, ath_mult = mc, mult
        
        print(f"📊 {token_name[:20]} ${mc:,.0f} {mult:.2f}x", flush=True)
        
        # Check if milestone reached
        current_milestone = get_milestone(ath_mult)
        if current_milestone and current_milestone != last_milestone:
            print(f"🎯 MILESTONE: {current_milestone}x", flush=True)
            elapsed_min = elapsed // 60
            milestone_msg = format_milestone(token_name, ca, metrics, current_milestone, ath_mc, elapsed_min)
            
            try:
                # Delete previous milestone message
                if last_milestone_msg_id:
                    try:
                        await client.delete_messages(forward_channel, last_milestone_msg_id)
                        print(f"🗑️ Deleted {last_milestone}x message", flush=True)
                    except:
                        pass
                
                # Send new milestone
                response = await client.send_message(forward_channel, milestone_msg)
                last_milestone_msg_id = response.id
                last_milestone = current_milestone
                print(f"📤 Sent {current_milestone}x milestone", flush=True)
            except Exception as e:
                print(f"⚠️ Milestone error: {e}", flush=True)
        
        if mc <= DEAD_MC_THRESHOLD:
            outcome = "WIN" if ath_mult >= 2.0 else "LOSS"
            elapsed_min = elapsed // 60
            
            print(f"💀 {outcome} {ath_mult:.2f}x", flush=True)
            
            # Delete last milestone and send final
            if last_milestone_msg_id:
                try:
                    await client.delete_messages(forward_channel, last_milestone_msg_id)
                    print(f"🗑️ Deleted milestone before final", flush=True)
                except:
                    pass
            
            msg = format_final(token_name, ca, metrics, entry_mc, ath_mc, ath_mult, outcome, elapsed_min)
            
            try:
                await client.send_message(forward_channel, msg)
                print(f"✅ FINAL SENT", flush=True)
            except: pass
            
            break

async def create_handler(channel_name, forward_channel):
    async def handler(event):
        text = event.message.message or ""
        ca_match = re.search(r'([1-9A-HJ-NP-Za-km-z]{32,44}pump)', text)
        if not ca_match: return
        
        ca = ca_match.group(0)
        seen_file = f"seen_{channel_name}.json"
        seen = load_seen(seen_file)
        
        if ca in seen: return
        
        async with processing_locks[channel_name]:
            seen = load_seen(seen_file)
            if ca in seen: return
            seen.add(ca)
            save_seen(seen, seen_file)
        
        metrics = parse_pumpfun(text)
        if not metrics or metrics.get('mc', 0) <= 0: return
        
        print(f"📡 NEW: {metrics.get('token_name', 'Unknown')}", flush=True)
        asyncio.create_task(track_ath(ca, metrics, forward_channel, event.client))
    
    return handler

async def main():
    if not SESSION_STRING:
        print("❌ SESSION_STRING not set!", flush=True)
        sys.exit(1)
    
    print("🔑 Connecting...", flush=True)
    
    while True:
        try:
            client = TelegramClient(StringSession(SESSION_STRING), api_id, api_hash)
            
            for channel_name, config in CHANNEL_MAPPING.items():
                handler = await create_handler(channel_name, config['forward_channel'])
                client.add_event_handler(handler, events.NewMessage(chats=config['monitor']))
            
            await client.connect()
            if not await client.is_user_authorized():
                print("❌ SESSION EXPIRED", flush=True)
                sys.exit(1)
            
            print("✅ Bot Running!", flush=True)
            print("=" * 60, flush=True)
            print("📡 PumpFun Monitor with Milestones", flush=True)
            print("=" * 60, flush=True)
            
            await client.run_until_disconnected()
        
        except AuthKeyDuplicatedError:
            await asyncio.sleep(60)
        except Exception as e:
            print(f"⚠️ {e}", flush=True)
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
