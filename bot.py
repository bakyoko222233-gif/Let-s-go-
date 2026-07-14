import os, sys, re, asyncio, aiohttp, json, logging
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import AuthKeyDuplicatedError
from asyncio import Lock

logging.basicConfig(level=logging.WARNING)

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

def parse_pumpfun(text):
    metrics = {}
    
    print(f"DEBUG RAW: {text[:300]}", flush=True)
    
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
    else: metrics['age_min'] = 0
    
    vol_txs_match = re.search(PUMPFUN_PATTERNS['volume_and_txs'], text)
    if vol_txs_match:
        vol_val = float(vol_txs_match.group(1))
        unit = vol_txs_match.group(2) or 'K'
        mult = {'K': 1000, 'M': 1_000_000, 'B': 1_000_000_000}
        metrics['vol_5m'] = vol_val * mult.get(unit, 1)
        metrics['buy_tx'] = int(vol_txs_match.group(3))
        metrics['sell_tx'] = int(vol_txs_match.group(4))
    else:
        metrics['vol_5m'] = 0
        metrics['buy_tx'] = 0
        metrics['sell_tx'] = 0
    
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
    
    print(f"DEBUG PARSED: Age={metrics['age_min']} Vol={metrics['vol_5m']} BuyTx={metrics['buy_tx']}", flush=True)
    
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

📋 **Metrics:**
├─ Age: {metrics.get('age_min', 0):.0f}m
├─ Vol: ${metrics.get('vol_5m', 0):,.0f}
├─ Buy/Sell: {metrics.get('buy_tx', 0)}/{metrics.get('sell_tx', 0)}
├─ Holders: {metrics.get('total_holders', 0)}
└─ Dev: {'✅ SOLD' if metrics.get('dev_sold') else '❌ HOLDING'}
"""
    return msg.strip()

async def track_ath(ca: str, metrics: dict, forward_channel, client):
    entry_mc = metrics.get('mc', 0)
    token_name = metrics.get('token_name', 'Unknown')
    
    print(f"🚀 {token_name} entry: ${entry_mc:,.0f}", flush=True)
    
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
            
            print(f"💀 {outcome} {ath_mult:.2f}x", flush=True)
            
            msg = format_final(token_name, ca, metrics, entry_mc, ath_mc, ath_mult, outcome, elapsed_min)
            
            try:
                await client.send_message(forward_channel, msg)
                print(f"✅ SENT", flush=True)
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
        client = None
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
            print("=" * 60, flush=True)
            
            await client.run_until_disconnected()
        
        except AuthKeyDuplicatedError:
            print("⚠️ Auth duplicate, reconnecting...", flush=True)
            await asyncio.sleep(10)
        except Exception as e:
            print(f"⚠️ Error: {e}", flush=True)
            await asyncio.sleep(10)
        finally:
            if client:
                try:
                    await client.disconnect()
                except:
                    pass

if __name__ == "__main__":
    asyncio.run(main())
