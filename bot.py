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

# MOODENG ONLY - 5 FILTERS TO 5 FORWARD CHANNELS
CHANNEL_MAPPING = {
    'moodeng': {
        'monitor': -1002270552242,
        'forward_filter1': -5134396719,
        'forward_filter2': -5231579644,
        'forward_filter3a': -5106136423,
        'forward_filter3b': -5035266819,
        'forward_filter3c': -1003936002395,
    }
}

SEEN_FILES = {ch: f"seen_{ch}.json" for ch in CHANNEL_MAPPING.keys()}
TRACKING_FILES = {ch: f"tracking_{ch}.json" for ch in CHANNEL_MAPPING.keys()}
processing_locks = {ch: Lock() for ch in CHANNEL_MAPPING.keys()}

ATH_CHECK_INTERVAL = 5 * 60
DEAD_MC_THRESHOLD = 5000

# ==================== FILTER OPTIONS ====================
# Based on analysis of 850+ Moodeng tokens
# WINNERS vs LOSERS comparison:
# - Scans: winners avg 20, losers avg 1.7 (1300% difference)
# - Age: winners avg 23.5 min, losers avg 5 min (90% difference)
# - Holders: winners avg 129, losers avg 219 (52% difference)
# - Dev Sold: winners avg 13.7%, losers avg 18% (7% difference)

# FILTER 1: Balanced (35% coverage, 100% win rate)
FILTER_1 = {
    'scans_min': 5,
    'age_min': 3,
    'holders_max': 180,
    'dev_sold_max': 25,
}

# FILTER 2: Aggressive (43.5% coverage, 100% win rate)
FILTER_2 = {
    'scans_min': 5,
    'age_min': 3,
    'holders_max': None,
    'dev_sold_max': 25,
}

# FILTER 3A: Super Aggressive + Holders<250 (78.3% coverage, 100% win rate)
FILTER_3A = {
    'scans_min': 3,
    'age_min': 1,
    'holders_max': 250,
    'dev_sold_max': 30,
}

# FILTER 3B: Liberal Scans (78.3% coverage, 100% win rate)
FILTER_3B = {
    'scans_min': 4,
    'age_min': 1,
    'holders_max': None,
    'dev_sold_max': 35,
}

# FILTER 3C: Super Chill - Max Coverage (82.6% coverage, 94.7% win rate)
FILTER_3C = {
    'scans_min': 3,
    'age_min': 1,
    'holders_max': None,
    'dev_sold_max': 30,
}

def passes_filter(metrics, filter_rules):
    """Check if token passes filter criteria"""
    try:
        # SCANS CHECK (REQUIRED)
        scans = metrics.get('scans', 0)
        if scans is None:
            scans = 0
        if isinstance(scans, str):
            scans = float(scans) if scans else 0
        if scans < filter_rules['scans_min']:
            return False
        
        # AGE CHECK (REQUIRED)
        age = metrics.get('age_min', 0)
        if age is None:
            age = 0
        if isinstance(age, str):
            age = float(age) if age else 0
        if age < filter_rules['age_min']:
            return False
        
        # HOLDERS CHECK (OPTIONAL)
        if filter_rules['holders_max'] is not None:
            holders = metrics.get('holders', 999)
            if holders is None:
                holders = 999
            if isinstance(holders, str):
                holders = float(holders) if holders else 999
            if holders > filter_rules['holders_max']:
                return False
        
        # DEV SOLD CHECK (REQUIRED)
        dev_sold = metrics.get('dev_sold', 30)
        if dev_sold is None:
            dev_sold = 30
        if isinstance(dev_sold, str):
            dev_sold = float(dev_sold) if dev_sold else 30
        if dev_sold > filter_rules['dev_sold_max']:
            return False
        
        return True
    except:
        return False

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
            'metrics': metrics,
            'entry_mc': metrics.get('mc', 0),
            'current_mc': metrics.get('mc', 0),
            'ath_mc': metrics.get('mc', 0),
            'ath_mult': 1.0,
            'elapsed_seconds': 0,
            'detected_time': now.isoformat(),
            'last_update': now.isoformat(),
            'status': 'TRACKING',
            'verdict': 'TRACKING'
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

# ==================== MOODENG PARSER ====================
def parse_moodeng(text):
    metrics = {}
    
    metrics['name'] = extract_metric(text, r'⚡️\s*‎?(.+?)\s*｜\s*\$') or 'Unknown'
    metrics['symbol'] = extract_metric(text, r'\$([A-Z0-9]+)') or ''
    metrics['ca'] = extract_metric(text, r'CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44}pump)') or ''
    
    metrics['age'] = extract_metric(text, r'⌛️\s*Pool Age:\s*(\d+[smh])') or '0m'
    metrics['mc'] = parse_value(extract_metric(text, r'Market Cap:\s*\$?([\d,\.]+[KMB]?)'))
    metrics['liquidity'] = parse_value(extract_metric(text, r'Liquid:\s*\$?([\d,\.]+[KMB]?)'))
    metrics['fake_liq'] = parse_value(extract_metric(text, r'Fake:\s*\$?([\d,\.]+[KMB]?)')) if 'Fake:' in text else 0
    
    metrics['dex_paid'] = '✅' if 'Paid✅' in text else '❌'
    metrics['dex_ads'] = '✅' if 'Ads✅' in text else '❌'
    metrics['scans'] = int(extract_metric(text, r'Scans:\s*(\d+)') or 0)
    
    metrics['holders'] = int(extract_metric(text, r'Holders:\s*(\d+)') or 0)
    metrics['top10_pct'] = float(extract_metric(text, r'TOP 10:\s*([\d\.]+)%') or 0)
    metrics['fake_holders'] = int(extract_metric(text, r'Fake:\s*(\d+)') or 0) if 'Fake:' in text.split('Holders')[1:] else 0
    
    metrics['bundles'] = int(extract_metric(text, r'Bundles:\s*(\d+)') or 0)
    metrics['bundles_before'] = float(extract_metric(text, r'Bundles:\s*\d+\s*｜?\s*([\d\.]+)%') or 0)
    metrics['bundles_after'] = float(extract_metric(text, r'→\s*([\d\.]+)%') or 0)
    
    metrics['snipers'] = int(extract_metric(text, r'Snipers:\s*(\d+)') or 0)
    metrics['snipers_before'] = float(extract_metric(text, r'Snipers:\s*\d+\s*｜?\s*([\d\.]+)%') or 0)
    metrics['snipers_after'] = float(extract_metric(text, r'Snipers:.*?→\s*([\d\.]+)%') or 0)
    
    metrics['first20'] = float(extract_metric(text, r'First 20:\s*([\d\.]+)%') or 0)
    
    metrics['dev_sol'] = float(extract_metric(text, r'Dev:\s*([\d\.]+)\s*SOL') or 0)
    metrics['dev_pct'] = float(extract_metric(text, r'Dev:.*?\|([\d\.]+)%') or 0)
    metrics['dev_bundled'] = float(extract_metric(text, r'Bundled:\s*([\d\.]+)%') or 0)
    metrics['dev_sold'] = float(extract_metric(text, r'Sold:\s*([\d\.]+)%') or 0)
    metrics['dev_airdrop'] = float(extract_metric(text, r'Airdrop:\s*([\d\.]+)%') or 0)
    
    metrics['vol_5m'] = parse_value(extract_metric(text, r'Last 5m:\s*\$([\d,\.]+[KMB]?)'))
    metrics['vol_5m_buy'] = parse_value(extract_metric(text, r'Last 5m:.*?B:\s*\$([\d,\.]+[KMB]?)'))
    metrics['vol_5m_sell'] = parse_value(extract_metric(text, r'Last 5m:.*?S:\s*\$([\d,\.]+[KMB]?)'))
    metrics['vol_5m_tx'] = int(extract_metric(text, r'Last 5m:.*?(\d+)\s*tx') or 0)
    
    metrics['vol_1h'] = parse_value(extract_metric(text, r'Last 1h:\s*\$([\d,\.]+[KMB]?)'))
    metrics['vol_1h_buy'] = parse_value(extract_metric(text, r'Last 1h:.*?B:\s*\$([\d,\.]+[KMB]?)'))
    metrics['vol_1h_sell'] = parse_value(extract_metric(text, r'Last 1h:.*?S:\s*\$([\d,\.]+[KMB]?)'))
    metrics['vol_1h_tx'] = int(extract_metric(text, r'Last 1h:.*?(\d+)\s*tx') or 0)
    
    metrics['vol_24h'] = parse_value(extract_metric(text, r'TOTAL Volume 24h:\s*\$([\d,\.]+[KMB]?)'))
    
    return metrics

# ==================== KOL SIGNAL PARSER ====================
def parse_kol_signal(text):
    metrics = {}
    
    metrics['name'] = extract_metric(text, r'⚡️\s*(.+?)\s*｜\s*\$') or 'Unknown'
    metrics['symbol'] = extract_metric(text, r'\$([A-Z0-9]+)') or ''
    metrics['ca'] = extract_metric(text, r'CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44}pump)') or ''
    
    metrics['mc'] = parse_value(extract_metric(text, r'MC:\s*\$?\s*([\d,\.]+[KMB]?)'))
    metrics['dev_sold'] = '✅' if 'Dev SOLD:  ✅' in text else '❌'
    metrics['holders'] = int(extract_metric(text, r'Holders:\s*(\d+)') or 0)
    metrics['top10_pct'] = float(extract_metric(text, r'TOP 10:\s*([\d\.]+)%') or 0)
    
    kol_buys = extract_metric(text, r'(\d+)\s*KOL BUY')
    metrics['kol_buys_count'] = int(kol_buys) if kol_buys else 0
    
    kol_pattern = r'🌙\s*(\w+)\s*⇨\s*📈\s*BUY\s*([\d\.]+)\s*SOL\s*-\s*(.+?)(?=🌙|🎮)'
    kol_list = re.findall(kol_pattern, text, re.DOTALL)
    metrics['kol_details'] = kol_list
    metrics['kol_total_sol'] = sum([float(kol[1]) for kol in kol_list])
    
    metrics['volume'] = parse_value(extract_metric(text, r'Total Volume:\s*\$?([\d,\.]+[KMB]?)'))
    metrics['txns'] = int(extract_metric(text, r'Txns:\s*(\d+)') or 0)
    metrics['age'] = extract_metric(text, r'Age:\s*(.+?)(?:\n|$)') or '0m'
    
    socials = extract_metric(text, r'Socials:\s*(.+?)(?:\n|$)')
    metrics['socials'] = socials or ''
    
    return metrics

# ==================== TESTALPHABOT PARSER ====================
def parse_testalphabot(text):
    metrics = {}
    
    # Extract full name and symbol
    name_match = extract_metric(text, r'Name:\s*(.+?)\s*｜')
    if name_match:
        metrics['name'] = name_match
    else:
        metrics['name'] = 'Unknown'
    
    metrics['symbol'] = extract_metric(text, r'\$([A-Z0-9]+)') or ''
    metrics['ca'] = extract_metric(text, r'CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44}[a-zA-Z0-9]*)') or ''
    
    metrics['age'] = extract_metric(text, r'Age:\s*(.+?)(?:\n|├)') or '0m'
    metrics['mc'] = parse_value(extract_metric(text, r'Market Cap:\s*\$?([\d,\.]+[KMB]?)'))
    metrics['liquidity'] = parse_value(extract_metric(text, r'Liq:\s*\$?([\d,\.]+[KMB]?)'))
    
    # Whales Score
    whales = extract_metric(text, r'Whales Score:\s*(\d+)')
    metrics['whales_score'] = int(whales) if whales else 0
    
    metrics['holders'] = int(extract_metric(text, r'Holders:\s*(\d+)') or 0)
    metrics['top10_pct'] = float(extract_metric(text, r'TOP 10:\s*([\d\.]+)%') or 0)
    
    # Holder distribution - all percentages after TOP 10
    distribution_pattern = r'TOP 10:.*?%\s*\n└\s*([\d\.]+(?:\s*｜\s*[\d\.]+)*)'
    distribution = extract_metric(text, distribution_pattern)
    if distribution:
        metrics['holder_distribution'] = distribution
    
    dev_status = extract_metric(text, r'Devs:\s*(✅|❌)')
    metrics['dev_sold'] = '✅' if dev_status == '✅' else '❌'
    
    # Trades - Buy and Sell counts
    buys = extract_metric(text, r'🅑\s*(\d+)')
    metrics['buys'] = int(buys) if buys else 0
    
    sells = extract_metric(text, r'🅢\s*(\d+)')
    metrics['sells'] = int(sells) if sells else 0
    
    metrics['volume'] = parse_value(extract_metric(text, r'Volume:\s*\$?([\d,\.]+[KMB]?)'))
    
    socials_count = extract_metric(text, r'Socials \((\d+)\)')
    metrics['socials_count'] = int(socials_count) if socials_count else 0
    
    socials = extract_metric(text, r'Socials \(\d+\)\s*:\s*(.+?)(?:\n|$)')
    metrics['socials'] = socials or ''
    
    return metrics

# ==================== DEX BOOSTER PARSER ====================
def parse_dex_booster(text):
    metrics = {}
    
    metrics['name'] = extract_metric(text, r'\[⚡️\s*SOL\]\s*-\s*(.+?)\s*｜') or 'Unknown'
    metrics['symbol'] = extract_metric(text, r'\$([A-Z0-9]+)') or ''
    metrics['ca'] = extract_metric(text, r'CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44}[a-zA-Z0-9]*?)(?:\n|$)') or ''
    
    metrics['mc'] = parse_value(extract_metric(text, r'Market Cap:\s*\$?([\d,\.]+[KMB]?)'))
    metrics['liquidity'] = parse_value(extract_metric(text, r'Liq:\s*\$?([\d,\.]+[KMB]?)'))
    
    boost = extract_metric(text, r'Total Boost:\s*⚡️(\d+)')
    metrics['boost_count'] = int(boost) if boost else 0
    
    metrics['dex_paid'] = '✅' if 'DEX Paid: ✅' in text else '❌'
    
    metrics['age'] = extract_metric(text, r'Age:\s*(.+?)(?:\n|├)') or '0m'
    
    metrics['volume'] = parse_value(extract_metric(text, r'Volume:\s*\$?([\d,\.]+[KMB]?)'))
    metrics['bundle_pct'] = float(extract_metric(text, r'Bundle:\s*([\d\.]+)%') or 0)
    metrics['total_fees'] = float(extract_metric(text, r'Total Fees:\s*([\d\.]+)') or 0)
    
    metrics['holders'] = int(extract_metric(text, r'Holders:\s*(\d+)') or 0)
    metrics['top10_pct'] = float(extract_metric(text, r'TOP 10:\s*([\d\.]+)%') or 0)
    
    distribution_pattern = r'└\s*([\d\.]+(?:\s*｜\s*[\d\.]+)*)'
    distribution = extract_metric(text, distribution_pattern)
    if distribution:
        metrics['holder_distribution'] = distribution
    
    dev_status = extract_metric(text, r'Dev:\s*(✅|❌)')
    metrics['dev_sold'] = '✅' if dev_status == '✅' else '❌'
    
    dev_created = extract_metric(text, r'Dev Tokens Created:\s*(\d+)')
    metrics['dev_created'] = int(dev_created) if dev_created else 0
    
    dev_bonded = extract_metric(text, r'Dev Bonded:\s*(\d+)')
    metrics['dev_bonded'] = int(dev_bonded) if dev_bonded else 0
    
    insiders = extract_metric(text, r'Insiders:\s*([\d\.]+)%')
    metrics['insiders_pct'] = float(insiders) if insiders else 0
    
    snipers = extract_metric(text, r'Snipers:\s*([\d\.]+)%')
    metrics['snipers_pct'] = float(snipers) if snipers else 0
    
    bots = extract_metric(text, r'BOTs:\s*(\d+)')
    metrics['bots'] = int(bots) if bots else 0
    
    kols = extract_metric(text, r'KOLs:\s*(\d+)')
    metrics['kols'] = int(kols) if kols else 0
    
    socials = extract_metric(text, r'Socials \((\d+)\)\s*:\s*(.+?)(?:\n|$)')
    metrics['socials'] = socials or ''
    
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

# ==================== REPORT FORMATTING ====================
def format_report(metrics, entry_mc, ath_mc, ath_mult, verdict, elapsed_min):
    """Format comprehensive token report with ALL metrics for Telegram"""
    
    name = metrics.get('name', 'Unknown')
    ca = metrics.get('ca', '')
    
    if verdict == "WIN":
        icon = "🟢🟢🟢" if ath_mult >= 5 else "🟢🟢" if ath_mult >= 3 else "🟢"
    else:
        icon = "🔴"
    
    report = f"{icon} {verdict} — {ath_mult:.2f}x\n\n"
    report += f"Token: {name}\n"
    report += f"CA: `{ca}`\n\n"
    
    # TRACKING INFO
    report += f"Entry: ${entry_mc:,.0f}\n"
    report += f"ATH: ${ath_mc:,.0f}\n"
    report += f"Multiplier: {ath_mult:.2f}x\n"
    report += f"Elapsed: {elapsed_min}m\n\n"
    
    # ==================== MOODENG METRICS ====================
    if metrics.get('age'):
        report += f"Age: {metrics.get('age')}\n"
    if metrics.get('mc'):
        report += f"MC: ${metrics.get('mc'):,.0f}\n"
    if metrics.get('liquidity'):
        report += f"Liq: ${metrics.get('liquidity'):,.0f}\n"
    if metrics.get('fake_liq'):
        report += f"Fake Liq: ${metrics.get('fake_liq'):,.0f}\n"
    if metrics.get('dex_paid'):
        report += f"DEX Paid: {metrics.get('dex_paid')}\n"
    if metrics.get('scans'):
        report += f"Scans: {metrics.get('scans')}\n"
    if metrics.get('holders'):
        report += f"Holders: {metrics.get('holders')}\n"
    if metrics.get('top10_pct'):
        report += f"Top10: {metrics.get('top10_pct'):.2f}%\n"
    if metrics.get('fake_holders'):
        report += f"Fake Holders: {metrics.get('fake_holders')}\n"
    if metrics.get('bundles'):
        report += f"Bundles: {metrics.get('bundles')} ({metrics.get('bundles_before', 0):.1f}% → {metrics.get('bundles_after', 0):.1f}%)\n"
    if metrics.get('snipers'):
        report += f"Snipers: {metrics.get('snipers')} ({metrics.get('snipers_before', 0):.1f}% → {metrics.get('snipers_after', 0):.1f}%)\n"
    if metrics.get('first20'):
        report += f"First20: {metrics.get('first20'):.2f}%\n"
    if metrics.get('dev_sol'):
        report += f"Dev SOL: {metrics.get('dev_sol'):.2f}\n"
    if metrics.get('dev_pct'):
        report += f"Dev%: {metrics.get('dev_pct'):.2f}%\n"
    if metrics.get('dev_bundled'):
        report += f"Dev Bundled: {metrics.get('dev_bundled'):.2f}%\n"
    if metrics.get('dev_sold'):
        report += f"Dev Sold: {metrics.get('dev_sold'):.2f}%\n"
    if metrics.get('dev_airdrop'):
        report += f"Dev Airdrop: {metrics.get('dev_airdrop'):.2f}%\n"
    
    # ==================== TESTALPHABOT/ALFA METRICS ====================
    if metrics.get('whales_score'):
        report += f"Whales Score: {metrics.get('whales_score')}\n"
    if metrics.get('buys'):
        report += f"Buys: {metrics.get('buys')} | Sells: {metrics.get('sells')}\n"
    if metrics.get('holder_distribution'):
        report += f"Holder Distribution: {metrics.get('holder_distribution')}\n"
    
    # ==================== DEX BOOSTER METRICS ====================
    if metrics.get('boost_count'):
        report += f"Boosts: {metrics.get('boost_count')}\n"
    if metrics.get('bundle_pct'):
        report += f"Bundle: {metrics.get('bundle_pct'):.2f}%\n"
    if metrics.get('total_fees'):
        report += f"Total Fees: {metrics.get('total_fees'):.2f} SOL\n"
    if metrics.get('dev_created'):
        report += f"Dev Created: {metrics.get('dev_created')}\n"
    if metrics.get('dev_bonded'):
        report += f"Dev Bonded: {metrics.get('dev_bonded')}\n"
    if metrics.get('insiders_pct'):
        report += f"Insiders: {metrics.get('insiders_pct'):.2f}%\n"
    if metrics.get('snipers_pct'):
        report += f"Snipers: {metrics.get('snipers_pct'):.2f}%\n"
    if metrics.get('bots'):
        report += f"BOTs: {metrics.get('bots')}\n"
    if metrics.get('kols'):
        report += f"KOLs: {metrics.get('kols')}\n"
    
    # ==================== KOL SIGNAL METRICS ====================
    if metrics.get('kol_buys_count'):
        report += f"\nKOL Buys: {metrics.get('kol_buys_count')}\n"
        report += f"KOL SOL: {metrics.get('kol_total_sol'):.2f}◎\n"
        if metrics.get('kol_details'):
            for kol in metrics.get('kol_details', []):
                report += f"  {kol[0]}: {kol[1]}◎ ({kol[2]})\n"
    
    # ==================== VOLUME INFO ====================
    if metrics.get('vol_5m'):
        report += f"\nVol5m: ${metrics.get('vol_5m'):,.0f}\n"
    if metrics.get('vol_5m_buy'):
        report += f"Vol5m Buy: ${metrics.get('vol_5m_buy'):,.0f} | Sell: ${metrics.get('vol_5m_sell'):,.0f}\n"
    if metrics.get('vol_5m_tx'):
        report += f"Vol5m Tx: {metrics.get('vol_5m_tx')}\n"
    if metrics.get('vol_1h'):
        report += f"Vol1h: ${metrics.get('vol_1h'):,.0f}\n"
    if metrics.get('vol_1h_buy'):
        report += f"Vol1h Buy: ${metrics.get('vol_1h_buy'):,.0f} | Sell: ${metrics.get('vol_1h_sell'):,.0f}\n"
    if metrics.get('vol_1h_tx'):
        report += f"Vol1h Tx: {metrics.get('vol_1h_tx')}\n"
    if metrics.get('vol_24h'):
        report += f"Vol24h: ${metrics.get('vol_24h'):,.0f}\n"
    elif metrics.get('volume'):
        report += f"Volume: ${metrics.get('volume'):,.0f}\n"
    if metrics.get('txns'):
        report += f"Txns: {metrics.get('txns')}\n"
    
    # ==================== SOCIALS ====================
    if metrics.get('socials'):
        report += f"\nSocials: {metrics.get('socials')}\n"
    if metrics.get('socials_count'):
        report += f"Socials Count: {metrics.get('socials_count')}\n"
    
    return report

# ==================== ATH TRACKER ====================
async def track_ath(ca: str, metrics: dict, channel_name: str, tracking_file: str, forward_chat: int, client):
    entry_mc = metrics.get('mc', 0)
    print(f"🚀 {channel_name} Tracking {ca[:12]} entry MC: ${entry_mc:,.0f}", flush=True)
    
    ath_mc = entry_mc
    ath_mult = 1.0
    elapsed = 0
    
    add_tracking(ca, metrics, tracking_file)
    
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
        
        update_tracking(ca, tracking_file,
            current_mc=mc,
            ath_mc=ath_mc,
            ath_mult=ath_mult,
            elapsed_seconds=elapsed
        )
        
        print(f"📊 {ca[:12]} MC:${mc:,.0f} {mult:.2f}x (ATH:{ath_mult:.2f}x)", flush=True)
        
        if mc <= DEAD_MC_THRESHOLD:
            verdict = "WIN" if ath_mult >= 2.0 else "LOSS"
            elapsed_min = elapsed // 60
            
            print(f"💀 {ca[:12]} DEAD - {verdict} {ath_mult:.2f}x", flush=True)
            
            update_tracking(ca, tracking_file, status='DEAD', verdict=verdict)
            
            # Format and send comprehensive report
            report = format_report(metrics, entry_mc, ath_mc, ath_mult, verdict, elapsed_min)
            
            try:
                await client.send_message(forward_chat, report)
                print(f"📤 Report sent to {channel_name}", flush=True)
            except Exception as e:
                print(f"⚠️ Send error: {e}", flush=True)
            
            remove_tracking(ca, tracking_file)
            break

# ==================== EVENT HANDLERS ====================
async def create_handler(channel_name):
    async def handler(event):
        text = event.message.message or ""
        
        ca_match = re.search(SOLANA_CA_PATTERN, text)
        if not ca_match:
            return
        
        ca = ca_match.group(0)
        seen_file = SEEN_FILES[channel_name]
        seen = load_seen(seen_file)
        
        if ca in seen:
            return
        
        async with processing_locks[channel_name]:
            seen = load_seen(seen_file)
            if ca in seen:
                return
            seen.add(ca)
            save_seen(seen, seen_file)
        
        # Parse metrics based on channel
        if channel_name == 'moodeng':
            metrics = parse_moodeng(text)
        elif channel_name == 'kol_signal':
            metrics = parse_kol_signal(text)
        elif channel_name == 'dex_screener':
            metrics = parse_dex_booster(text)
        elif channel_name == 'alfa_100x':
            metrics = parse_testalphabot(text)
        else:
            metrics = {'name': 'Unknown', 'ca': ca, 'mc': 0}
        
        if metrics.get('mc', 0) <= 0:
            return
        
        config = CHANNEL_MAPPING[channel_name]
        
        # ===== APPLY ALL 5 FILTERS =====
        filters = [
            ('FILTER 1', FILTER_1, config.get('forward_filter1')),
            ('FILTER 2', FILTER_2, config.get('forward_filter2')),
            ('FILTER 3A', FILTER_3A, config.get('forward_filter3a')),
            ('FILTER 3B', FILTER_3B, config.get('forward_filter3b')),
            ('FILTER 3C', FILTER_3C, config.get('forward_filter3c')),
        ]
        
        for filter_name, filter_rules, forward_chat in filters:
            if passes_filter(metrics, filter_rules):
                print(f"✅ {filter_name} {metrics.get('name', 'Unknown')[:20]} PASSED - MC:${metrics.get('mc', 0):,.0f} → {forward_chat}", flush=True)
                tracking_file = TRACKING_FILES[channel_name]
                asyncio.create_task(track_ath(ca, metrics, f"{channel_name}_{filter_name}", tracking_file, forward_chat, event.client))
            else:
                print(f"❌ {filter_name} {metrics.get('name', 'Unknown')[:20]} FILTERED OUT", flush=True)
    
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
                handler = await create_handler(channel_name)
                client.add_event_handler(handler, events.NewMessage(chats=config['monitor']))
            
            await client.connect()
            if not await client.is_user_authorized():
                print("❌ SESSION EXPIRED", flush=True)
                sys.exit(1)
            
            print("✅ Bot Running!", flush=True)
            print("=" * 55, flush=True)
            for ch, cfg in CHANNEL_MAPPING.items():
                print(f"📡 {ch}: {cfg['monitor']} → {cfg['forward']}", flush=True)
            print("=" * 55, flush=True)
            
            await client.run_until_disconnected()
        
        except AuthKeyDuplicatedError:
            await asyncio.sleep(60)
        except Exception as e:
            print(f"⚠️ ERROR: {e}", flush=True)
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())

