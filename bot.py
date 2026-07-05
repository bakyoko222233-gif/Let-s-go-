import os
import sys
import re
import asyncio
import aiohttp
import json
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import AuthKeyDuplicatedError
from asyncio import Lock

# ==================== CONFIG ====================
api_id   = 33243817
api_hash = '84b76a174eabcccd6bba85ec9eb4daf3'
SESSION_STRING = os.getenv('SESSION_STRING', '')

# PUMPFUN ULTIMATE CHANNEL
CHANNEL_MAPPING = {
    'pumpfun_ultimate': {
        'monitor': -1002380293749,      # PumpFun Ultimate Channel
        'forward_channel': -5134396719, # Your Alert Group
    }
}

TRACKING_FILES = {ch: f"tracking_{ch}.json" for ch in CHANNEL_MAPPING.keys()}
processing_locks = {ch: Lock() for ch in CHANNEL_MAPPING.keys()}

ATH_CHECK_INTERVAL = 5 * 60
DEAD_MC_THRESHOLD = 5000

# ==================== PUMPFUN PATTERNS ====================
PUMPFUN_PATTERNS = {
    'token_name': r'^([^\n]+?)\s*(?:\([A-Z0-9]+\))?\s*\n([1-9A-HJ-NP-Za-km-z]{32,44}pump)',
    'ca': r'([1-9A-HJ-NP-Za-km-z]{32,44}pump)',
    'scanner': r'by\s+([^\n]+)',
    'mc': r'Cap:\s*([0-9.]+)([KMB]?)',
    'age': r'🕐\s*([0-9]+[smh]?)',  # Get "12m" or "1h" or "30s"
    'volume_and_txs': r'Vol:\s*([0-9.]+)([KMB]?)\s*\|\s*●\s*(\d+)\s*\|\s*●\s*(\d+)',  # Vol | BuyTx | SellTx on same line
    'bonding_curve': r'Bonding Curve:\s*([0-9.]+)%',
    'dev_status': r'Dev:\s*(✅|❌)\s*\(([^)]+)\)',
    'insiders': r'Insiders:\s*(\d+)',
    'kols': r'KOLs:\s*(\d+)',
    'total_holders': r'TH:\s*(\d+)',
    'top10_pct': r'Top 10:\s*([0-9.]+)%',
    'holder_distribution': r'Top 10:\s*[0-9.]+%\s*\n\s*└([0-9.\s|]+)',  # Parse distribution line
    'snipers': r'Sniper:\s*(\d+)\s+buy\s+([0-9.]+)%\s+with\s+([0-9.]+)\s*SOL',  # "13 buy 34.3% with 18.6 SOL"
    'bundles': r'Bundle:\s*(\d+)',
    'buy_pct': r'Sum\s+🅑:([0-9.]+)%',
    'sell_pct': r'Sum\s+🅢:\s*([0-9.]+)%',
}

# ==================== HELPERS ====================
def load_seen(path):
    try:
        with open(path, 'r') as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen, path):
    try:
        with open(path, 'w') as f:
            json.dump(list(seen), f)
    except:
        pass

def load_tracking(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_tracking(state, path):
    try:
        with open(path, 'w') as f:
            json.dump(state, f, indent=2)
    except:
        pass

def add_tracking(ca, metrics, path):
    state = load_tracking(path)
    if ca not in state:
        now = datetime.now(timezone.utc)
        state[ca] = {
            'ca': ca,
            'token_name': metrics.get('token_name', 'Unknown'),
            'entry_mc': metrics.get('mc', 0),
            'current_mc': metrics.get('mc', 0),
            'ath_mc': metrics.get('mc', 0),
            'ath_mult': 1.0,
            'metrics': metrics,
            'detected_time': now.isoformat(),
            'last_update': now.isoformat(),
            'status': 'TRACKING',
        }
        save_tracking(state, path)

def update_tracking(ca, path, **kwargs):
    state = load_tracking(path)
    if ca in state:
        state[ca].update(kwargs)
        state[ca]['last_update'] = datetime.now(timezone.utc).isoformat()
        save_tracking(state, path)

def remove_tracking(ca, path):
    state = load_tracking(path)
    if ca in state:
        del state[ca]
        save_tracking(state, path)

# ==================== PARSING ====================
def parse_value(val_str):
    if not val_str:
        return 0
    try:
        val_str = str(val_str).replace(',', '').strip()
        num = float(re.sub(r'[KMBkmb]', '', val_str))
        if 'K' in val_str or 'k' in val_str:
            num *= 1000
        elif 'M' in val_str or 'm' in val_str:
            num *= 1000000
        elif 'B' in val_str or 'b' in val_str:
            num *= 1000000000
        return num
    except:
        return 0

def extract_metric(text, pattern):
    if not pattern:
        return None
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None

# ==================== PUMPFUN PARSER ====================
def parse_pumpfun_ultimate(text):
    """Parse PumpFun Ultimate alert format"""
    metrics = {}
    
    token_match = re.search(PUMPFUN_PATTERNS['token_name'], text, re.MULTILINE)
    if token_match:
        metrics['token_name'] = token_match.group(1).strip()
        metrics['ca'] = token_match.group(2)
    else:
        ca_match = re.search(PUMPFUN_PATTERNS['ca'], text)
        if ca_match:
            metrics['ca'] = ca_match.group(1)
            metrics['token_name'] = 'Unknown'
        else:
            return {}
    
    scanner_match = re.search(PUMPFUN_PATTERNS['scanner'], text)
    if scanner_match:
        metrics['scanner'] = scanner_match.group(1)
    
    mc_match = re.search(PUMPFUN_PATTERNS['mc'], text)
    if mc_match:
        mc_val = float(mc_match.group(1))
        unit = mc_match.group(2) or 'K'
        multipliers = {'K': 1000, 'M': 1_000_000, 'B': 1_000_000_000}
        metrics['mc'] = mc_val * multipliers.get(unit, 1)
    else:
        metrics['mc'] = 0
    
    # Age: convert to minutes
    age_match = re.search(PUMPFUN_PATTERNS['age'], text)
    if age_match:
        age_str = age_match.group(1)  # "12m" or "1h" or "30s"
        age_num = int(re.search(r'(\d+)', age_str).group(1))
        age_unit = re.search(r'[smh]', age_str).group(0)
        if age_unit == 's':
            metrics['age_min'] = age_num / 60
        elif age_unit == 'h':
            metrics['age_min'] = age_num * 60
        else:  # m
            metrics['age_min'] = age_num
    else:
        metrics['age_min'] = 0
    
    # Volume and Txs on same line: Vol: 38.7K | ● 629 | ● 408
    vol_txs_match = re.search(PUMPFUN_PATTERNS['volume_and_txs'], text)
    if vol_txs_match:
        vol_val = float(vol_txs_match.group(1))
        unit = vol_txs_match.group(2) or 'K'
        multipliers = {'K': 1000, 'M': 1_000_000, 'B': 1_000_000_000}
        metrics['vol_5m'] = vol_val * multipliers.get(unit, 1)
        metrics['buy_tx'] = int(vol_txs_match.group(3))
        metrics['sell_tx'] = int(vol_txs_match.group(4))
    else:
        metrics['vol_5m'] = 0
        metrics['buy_tx'] = 0
        metrics['sell_tx'] = 0
    
    bonding_match = re.search(PUMPFUN_PATTERNS['bonding_curve'], text)
    metrics['bonding_curve_pct'] = float(bonding_match.group(1)) if bonding_match else 0
    
    dev_match = re.search(PUMPFUN_PATTERNS['dev_status'], text)
    if dev_match:
        metrics['dev_sold'] = (dev_match.group(1) == '✅')
    else:
        metrics['dev_sold'] = False
    
    insiders_match = re.search(PUMPFUN_PATTERNS['insiders'], text)
    metrics['insiders'] = int(insiders_match.group(1)) if insiders_match else 0
    
    kols_match = re.search(PUMPFUN_PATTERNS['kols'], text)
    metrics['kols'] = int(kols_match.group(1)) if kols_match else 0
    
    holders_match = re.search(PUMPFUN_PATTERNS['total_holders'], text)
    metrics['total_holders'] = int(holders_match.group(1)) if holders_match else 0
    
    top10_match = re.search(PUMPFUN_PATTERNS['top10_pct'], text)
    metrics['top10_pct'] = float(top10_match.group(1)) if top10_match else 0
    
    # Holder distribution: "3.2|2.9|2.8|2.6|1.9|1.9|1.7|1.6|1.5"
    holder_dist_match = re.search(PUMPFUN_PATTERNS['holder_distribution'], text)
    if holder_dist_match:
        dist_str = holder_dist_match.group(1).strip()
        metrics['holder_distribution'] = dist_str  # Store raw string
        # Parse individual percentages
        holder_pcts = [float(x.strip()) for x in dist_str.split('|') if x.strip()]
        metrics['holder_dist_list'] = holder_pcts
    else:
        metrics['holder_distribution'] = ''
        metrics['holder_dist_list'] = []
    
    sniper_match = re.search(PUMPFUN_PATTERNS['snipers'], text)
    if sniper_match:
        metrics['snipers'] = int(sniper_match.group(1))
        metrics['sniper_pct'] = float(sniper_match.group(2))
        metrics['sniper_sol'] = float(sniper_match.group(3))
    else:
        metrics['snipers'] = 0
        metrics['sniper_pct'] = 0
        metrics['sniper_sol'] = 0
    
    bundle_match = re.search(PUMPFUN_PATTERNS['bundles'], text)
    metrics['bundles'] = int(bundle_match.group(1)) if bundle_match else 0
    
    buy_pct_match = re.search(PUMPFUN_PATTERNS['buy_pct'], text)
    metrics['buy_pct'] = float(buy_pct_match.group(1)) if buy_pct_match else 0
    
    sell_pct_match = re.search(PUMPFUN_PATTERNS['sell_pct'], text)
    metrics['sell_pct'] = float(sell_pct_match.group(1)) if sell_pct_match else 0
    
    return metrics

# ==================== DEXSCREENER API ====================
async def get_price_and_mc(ca: str):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
                pairs = data.get('pairs') or []
                if not pairs:
                    return None, None
                sol_pairs = [p for p in pairs if p.get('chainId') == 'solana']
                if not sol_pairs:
                    sol_pairs = pairs
                best = max(sol_pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
                price = best.get('priceUsd')
                mc = best.get('marketCap') or best.get('fdv')
                return (float(price) if price else None, float(mc) if mc else None)
    except:
        return None, None

SOLANA_CA_PATTERN = r'[1-9A-HJ-NP-Za-km-z]{32,44}pump'

# ==================== FINAL RESULT FORMATTING ====================
def format_final_result(token_name, ca, metrics, entry_mc, ath_mc, ath_mult, outcome, elapsed_min):
    """Format final WIN/LOSS result"""
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
├─ Age: {metrics.get('age_min', 0)}m
├─ Bonding: {metrics.get('bonding_curve_pct', 0):.1f}%
├─ Holders: {metrics.get('total_holders', 0)}
├─ Top10: {metrics.get('top10_pct', 0):.1f}%
├─ Distribution: {metrics.get('holder_distribution', 'N/A')}
├─ KOLs: {metrics.get('kols', 0)}
├─ Insiders: {metrics.get('insiders', 0)}
├─ Snipers: {metrics.get('snipers', 0)} ({metrics.get('sniper_pct', 0):.1f}% with {metrics.get('sniper_sol', 0):.2f} SOL)
├─ Bundles: {metrics.get('bundles', 0)}
├─ Buy/Sell: {metrics.get('buy_pct', 0):.1f}% / {metrics.get('sell_pct', 0):.1f}%
└─ Dev Sold: {'✅ YES' if metrics.get('dev_sold') else '❌ NO'}

🔍 **Activity:**
├─ Vol 5m: ${metrics.get('vol_5m', 0):,.0f}
├─ Buy Txs: {metrics.get('buy_tx', 0)}
├─ Sell Txs: {metrics.get('sell_tx', 0)}
└─ Scanner: {metrics.get('scanner', 'Unknown')}
"""
    return msg.strip()

# ==================== ATH TRACKER ====================
async def track_ath(ca: str, metrics: dict, forward_channel, client):
    """Track token ATH and send final result"""
    entry_mc = metrics.get('mc', 0)
    token_name = metrics.get('token_name', 'Unknown')
    
    print(f"🚀 Tracking {token_name} ({ca[:12]}) entry MC: ${entry_mc:,.0f}", flush=True)
    
    ath_mc = entry_mc
    ath_mult = 1.0
    elapsed = 0
    
    add_tracking(ca, metrics, TRACKING_FILES['pumpfun_ultimate'])
    
    while True:
        await asyncio.sleep(ATH_CHECK_INTERVAL)
        elapsed += ATH_CHECK_INTERVAL
        
        price, mc = await get_price_and_mc(ca)
        if price is None or mc is None:
            continue
        
        mult = mc / entry_mc if entry_mc > 0 else 0
        
        if mc > ath_mc:
            ath_mc = mc
            ath_mult = mult
        
        update_tracking(ca, TRACKING_FILES['pumpfun_ultimate'],
            current_mc=mc, ath_mc=ath_mc, ath_mult=ath_mult, elapsed_seconds=elapsed
        )
        
        print(f"📊 {token_name[:20]} MC:${mc:,.0f} {mult:.2f}x (ATH:{ath_mult:.2f}x)", flush=True)
        
        if mc <= DEAD_MC_THRESHOLD:
            outcome = "WIN" if ath_mult >= 2.0 else "LOSS"
            elapsed_min = elapsed // 60
            
            print(f"💀 {token_name} DEAD - {outcome} {ath_mult:.2f}x", flush=True)
            
            # Format & send final result
            final_msg = format_final_result(token_name, ca, metrics, entry_mc, ath_mc, ath_mult, outcome, elapsed_min)
            
            try:
                await client.send_message(forward_channel, final_msg)
                print(f"✅ SENT: {token_name} {outcome} {ath_mult:.2f}x", flush=True)
            except Exception as e:
                print(f"⚠️ Error: {e}", flush=True)
            
            # Update tracking
            update_tracking(ca, TRACKING_FILES['pumpfun_ultimate'], status='DEAD', verdict=outcome)
            remove_tracking(ca, TRACKING_FILES['pumpfun_ultimate'])
            break

# ==================== EVENT HANDLER ====================
async def create_handler(channel_name, forward_channel):
    async def handler(event):
        text = event.message.message or ""
        
        ca_match = re.search(SOLANA_CA_PATTERN, text)
        if not ca_match:
            return
        
        ca = ca_match.group(0)
        seen_file = f"seen_{channel_name}.json"
        seen = load_seen(seen_file)
        
        if ca in seen:
            return
        
        async with processing_locks[channel_name]:
            seen = load_seen(seen_file)
            if ca in seen:
                return
            seen.add(ca)
            save_seen(seen, seen_file)
        
        metrics = parse_pumpfun_ultimate(text)
        
        if not metrics or metrics.get('mc', 0) <= 0:
            return
        
        token_name = metrics.get('token_name', 'Unknown')
        print(f"📡 NEW: {token_name} MC:${metrics.get('mc', 0):,.0f}", flush=True)
        
        asyncio.create_task(track_ath(ca, metrics, forward_channel, event.client))
    
    return handler

# ==================== MAIN ====================
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
            print("📡 PumpFun Ultimate Monitor", flush=True)
            print("📤 Forwarding to: -5134396719", flush=True)
            print("=" * 60, flush=True)
            
            await client.run_until_disconnected()
        
        except AuthKeyDuplicatedError:
            await asyncio.sleep(60)
        except Exception as e:
            print(f"⚠️ ERROR: {e}", flush=True)
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
