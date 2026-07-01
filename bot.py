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

# ==================== PATTERNS ====================
PATTERNS = {
    'token_name': r'^([^\n]+)\s*\n([1-9A-HJ-NP-Za-km-z]{32,44}pump)',
    'ca': r'([1-9A-HJ-NP-Za-km-z]{32,44}pump)',
    'scanner': r'by\s+([^\n]+)',
    'cap_age_txs': r'Cap:\s*([0-9.]+)([KMB]?)\s*\|\s*🕐\s*([0-9]+[smh]?)\s*\|.*?Vol:\s*([0-9.]+)([KMB]?)\s*\|\s*●\s*(\d+)\s*\|\s*●\s*(\d+)',
    'bonding': r'Bonding Curve:\s*([0-9.]+)%',
    'dev': r'Dev:\s*(✅|❌)\s*\(([^)]+)\)',
    'insiders': r'Insiders:\s*(\d+)',
    'kols': r'KOLs:\s*(\d+)',
    'holders': r'TH:\s*(\d+)',
    'top10': r'Top 10:\s*([0-9.]+)%',
    'holder_dist': r'Top 10:.*?\n\s*└([0-9.\s|]+)',
    'snipers': r'Sniper:\s*(\d+)\s+buy\s+([0-9.]+)%\s+with\s+([0-9.]+)\s*SOL',
    'bundle': r'Bundle:\s*(\d+)\s+buy\s+([0-9.]+)%(?:\s+with\s+([0-9.]+)\s*SOL)?',
    'buy_sell_pct': r'Sum\s+●:([0-9.]+)%\s*\|\s*Sum\s+●:\s*([0-9.]+)%',
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

def parse_value(val_str):
    if not val_str: return 0
    try:
        val_str = str(val_str).replace(',', '').strip()
        num = float(re.sub(r'[KMBkmb]', '', val_str))
        if 'K' in val_str or 'k' in val_str: num *= 1000
        elif 'M' in val_str or 'm' in val_str: num *= 1000000
        elif 'B' in val_str or 'b' in val_str: num *= 1000000000
        return num
    except: return 0

def extract_metric(text, pattern):
    if not pattern: return None
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None

def parse_pumpfun(text):
    metrics = {}
    
    token_match = re.search(PATTERNS['token_name'], text, re.MULTILINE)
    if token_match:
        metrics['token_name'] = token_match.group(1).strip()
        metrics['ca'] = token_match.group(2)
    else:
        ca_match = re.search(PATTERNS['ca'], text)
        if ca_match:
            metrics['ca'] = ca_match.group(1)
            metrics['token_name'] = 'Unknown'
        else: return {}
    
    scanner_match = re.search(PATTERNS['scanner'], text)
    metrics['scanner'] = scanner_match.group(1) if scanner_match else 'Unknown'
    
    # Cap, Age, Vol, Buy/Sell Txs all on same line!
    cap_match = re.search(PATTERNS['cap_age_txs'], text, re.DOTALL)
    if cap_match:
        cap_val = float(cap_match.group(1))
        cap_unit = cap_match.group(2) or 'K'
        mult = {'K': 1000, 'M': 1_000_000, 'B': 1_000_000_000}
        metrics['mc'] = cap_val * mult.get(cap_unit, 1)
        
        age_str = cap_match.group(3)
        age_num = int(re.search(r'(\d+)', age_str).group(1)) if re.search(r'(\d+)', age_str) else 0
        age_unit = re.search(r'[smh]', age_str).group(0) if re.search(r'[smh]', age_str) else 'm'
        if age_unit == 's': metrics['age_min'] = age_num / 60
        elif age_unit == 'h': metrics['age_min'] = age_num * 60
        else: metrics['age_min'] = age_num
        
        vol_val = float(cap_match.group(4))
        vol_unit = cap_match.group(5) or 'K'
        metrics['vol_5m'] = vol_val * mult.get(vol_unit, 1)
        metrics['buy_tx'] = int(cap_match.group(6))
        metrics['sell_tx'] = int(cap_match.group(7))
    else:
        metrics['mc'], metrics['age_min'], metrics['vol_5m'], metrics['buy_tx'], metrics['sell_tx'] = 0, 0, 0, 0, 0
    
    bonding_match = re.search(PATTERNS['bonding'], text)
    metrics['bonding_curve_pct'] = float(bonding_match.group(1)) if bonding_match else 0
    
    dev_match = re.search(PATTERNS['dev'], text)
    if dev_match:
        metrics['dev_sold'] = (dev_match.group(1) == '✅')
    else:
        metrics['dev_sold'] = False
    
    insiders = re.search(PATTERNS['insiders'], text)
    metrics['insiders'] = int(insiders.group(1)) if insiders else 0
    
    kols = re.search(PATTERNS['kols'], text)
    metrics['kols'] = int(kols.group(1)) if kols else 0
    
    holders = re.search(PATTERNS['holders'], text)
    metrics['total_holders'] = int(holders.group(1)) if holders else 0
    
    top10 = re.search(PATTERNS['top10'], text)
    metrics['top10_pct'] = float(top10.group(1)) if top10 else 0
    
    holder_dist = re.search(PATTERNS['holder_dist'], text)
    if holder_dist:
        metrics['holder_distribution'] = holder_dist.group(1).strip()
    else:
        metrics['holder_distribution'] = ''
    
    sniper = re.search(PATTERNS['snipers'], text)
    if sniper:
        metrics['snipers'] = int(sniper.group(1))
        metrics['sniper_pct'] = float(sniper.group(2))
        metrics['sniper_sol'] = float(sniper.group(3))
    else:
        metrics['snipers'], metrics['sniper_pct'], metrics['sniper_sol'] = 0, 0, 0
    
    bundle = re.search(PATTERNS['bundle'], text)
    if bundle:
        metrics['bundles'] = int(bundle.group(1))
        metrics['bundle_pct'] = float(bundle.group(2))
        metrics['bundle_sol'] = float(bundle.group(3)) if bundle.group(3) else 0
    else:
        metrics['bundles'], metrics['bundle_pct'], metrics['bundle_sol'] = 0, 0, 0
    
    buy_sell = re.search(PATTERNS['buy_sell_pct'], text)
    if buy_sell:
        metrics['buy_pct'] = float(buy_sell.group(1))
        metrics['sell_pct'] = float(buy_sell.group(2))
    else:
        metrics['buy_pct'], metrics['sell_pct'] = 0, 0
    
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
                price = best.get('priceUsd')
                mc = best.get('marketCap') or best.get('fdv')
                return (float(price) if price else None, float(mc) if mc else None)
    except: return None, None

def format_final_result(token_name, ca, metrics, entry_mc, ath_mc, ath_mult, outcome, elapsed_min):
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

📋 **Metrics:**
├─ Age: {metrics.get('age_min', 0):.0f}m
├─ Bonding: {metrics.get('bonding_curve_pct', 0):.1f}%
├─ Holders: {metrics.get('total_holders', 0)}
├─ Top10: {metrics.get('top10_pct', 0):.1f}%
├─ Dist: {metrics.get('holder_distribution', 'N/A')}
├─ KOLs: {metrics.get('kols', 0)} | Insiders: {metrics.get('insiders', 0)}
├─ Snipers: {metrics.get('snipers', 0)} ({metrics.get('sniper_pct', 0):.1f}% / {metrics.get('sniper_sol', 0):.2f}◎)
├─ Bundles: {metrics.get('bundles', 0)} ({metrics.get('bundle_pct', 0):.1f}%)
├─ Vol: ${metrics.get('vol_5m', 0):,.0f} | Buy/Sell: {metrics.get('buy_pct', 0):.1f}%/{metrics.get('sell_pct', 0):.1f}%
└─ Dev: {'✅ SOLD' if metrics.get('dev_sold') else '❌ HOLDING'}
"""
    return msg.strip()

async def track_ath(ca: str, metrics: dict, forward_channel, client):
    entry_mc = metrics.get('mc', 0)
    token_name = metrics.get('token_name', 'Unknown')
    
    print(f"🚀 {token_name} ({ca[:12]}) entry: ${entry_mc:,.0f}", flush=True)
    
    ath_mc, ath_mult, elapsed = entry_mc, 1.0, 0
    
    while True:
        await asyncio.sleep(ATH_CHECK_INTERVAL)
        elapsed += ATH_CHECK_INTERVAL
        
        price, mc = await get_price_and_mc(ca)
        if mc is None: continue
        
        mult = mc / entry_mc if entry_mc > 0 else 0
        if mc > ath_mc:
            ath_mc, ath_mult = mc, mult
        
        print(f"📊 {token_name[:20]} ${mc:,.0f} {mult:.2f}x", flush=True)
        
        if mc <= DEAD_MC_THRESHOLD:
            outcome = "WIN" if ath_mult >= 2.0 else "LOSS"
            elapsed_min = elapsed // 60
            
            print(f"💀 {token_name} {outcome} {ath_mult:.2f}x", flush=True)
            
            final_msg = format_final_result(token_name, ca, metrics, entry_mc, ath_mc, ath_mult, outcome, elapsed_min)
            
            try:
                await client.send_message(forward_channel, final_msg)
                print(f"✅ SENT: {outcome} {ath_mult:.2f}x", flush=True)
            except Exception as e:
                print(f"❌ Error: {e}", flush=True)
            
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
        
        token_name = metrics.get('token_name', 'Unknown')
        print(f"📡 NEW: {token_name} ${metrics.get('mc', 0):,.0f}", flush=True)
        
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
            print("📡 PumpFun Monitor", flush=True)
            print("📤 Forward to: -5134396719", flush=True)
            print("=" * 60, flush=True)
            
            await client.run_until_disconnected()
        
        except AuthKeyDuplicatedError:
            await asyncio.sleep(60)
        except Exception as e:
            print(f"⚠️ ERROR: {e}", flush=True)
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
