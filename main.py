import os
import time
import random
import string
import json
import requests
import psycopg2
import psycopg2.extras
from collections import defaultdict
from datetime import datetime

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OWNER_CHAT_ID = TELEGRAM_CHAT_ID
DATABASE_URL = os.environ["DATABASE_URL"]
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

MCAP_MIN = 30_000
MCAP_MAX = 3_000_000
VOLUME_MIN = 20_000
MIN_LIQUIDITY = 5_000
MIN_PRICE_CHANGE = 10
GRADUATION_MCAP_MIN = 25_000

EARLY_MCAP_MIN = 10_000
EARLY_MCAP_MAX = 80_000
EARLY_VOLUME_MIN = 6_000
EARLY_VOLUME_MAX = 30_000
EARLY_LIQUIDITY_MIN = 1_000
EARLY_AGE_MIN_MINUTES = 4
EARLY_AGE_MAX_MINUTES = 30

RUNNER_MCAP_THRESHOLD = 300_000
COHORT_MIN_HITS = 3
NETWORK_MIN_SIZE = 3
NETWORK_MIN_HITS = 2
MILESTONES = [50, 100, 200, 300, 500, 1000]

FAST_INTERVAL = 30
MEDIUM_INTERVAL = 60
SLOW_INTERVAL = 180
DAILY_REPORT_INTERVAL = 86400

# ── MAYHEM MODE FILTER ────────────────────────────────────────

MAYHEM_KEYWORDS = [
    "mayhem", "mayhemmode", "mayhem mode", "chaos mode", "nuke", "berserk",
    "rampage", "frenzy mode", "meltdown", "going crazy", "insane mode",
    "turbo mode", "ultra mode", "god mode", "beast mode", "ape mode"
]

# ── IN-MEMORY STATE ───────────────────────────────────────────

active_groups = {}
access_codes = {}
last_update_id = 0
last_fast_scan = 0
last_medium_scan = 0
last_slow_scan = 0
last_daily_report = 0
graduation_watchlist = {}
token_buy_windows = defaultdict(list)
holder_history = defaultdict(list)
early_wallets_cache = {}
cohort_wallets = {}
wallet_networks = []
pumpfun_curve_history = defaultdict(list)

# ── DATABASE ──────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            token_address TEXT NOT NULL,
            token_name TEXT,
            token_symbol TEXT,
            timestamp BIGINT,
            mcap_at_alert REAL,
            liquidity REAL,
            volume REAL,
            buys INTEGER,
            sells INTEGER,
            price_change_5m REAL,
            price_change_1h REAL,
            price_change_24h REAL,
            early_score REAL DEFAULT 0,
            confirmation_score REAL DEFAULT 0,
            triggered_signals JSONB,
            source TEXT,
            alert_tier TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            token_address TEXT PRIMARY KEY,
            alert_mcap REAL,
            peak_mcap REAL,
            time_to_2x INTEGER,
            time_to_5x INTEGER,
            final_classification TEXT,
            alerted_at BIGINT,
            resolved_at BIGINT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            wallet_address TEXT PRIMARY KEY,
            score REAL DEFAULT 1.0,
            win_rate REAL DEFAULT 0.0,
            total_trades INTEGER DEFAULT 0,
            runner_trades INTEGER DEFAULT 0,
            last_seen BIGINT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_weights (
            signal_name TEXT PRIMARY KEY,
            weight REAL DEFAULT 1.0,
            success_rate REAL DEFAULT 0.0,
            total_uses INTEGER DEFAULT 0,
            successful_uses INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tokens_seen (
            token_address TEXT PRIMARY KEY,
            first_seen BIGINT,
            last_seen BIGINT,
            lifecycle_stage TEXT DEFAULT 'new',
            alerted BOOLEAN DEFAULT FALSE,
            blacklisted BOOLEAN DEFAULT FALSE,
            honeypot BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracking (
            token_address TEXT PRIMARY KEY,
            token_name TEXT,
            token_symbol TEXT,
            alert_mcap REAL,
            alerted_at BIGINT,
            milestones_hit JSONB DEFAULT '[]',
            dex_url TEXT,
            alert_tier TEXT,
            early_score REAL DEFAULT 0,
            confirmation_score REAL DEFAULT 0,
            triggered_signals JSONB DEFAULT '[]'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blacklists (
            address TEXT PRIMARY KEY,
            reason TEXT,
            blacklist_type TEXT,
            added_at BIGINT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("Database initialized")

def load_state_from_db():
    global cohort_wallets
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT signal_name, weight FROM signal_weights")
        rows = cur.fetchall()
        loaded_weights = {r["signal_name"]: r["weight"] for r in rows}
        for k, v in loaded_weights.items():
            if k in DEFAULT_SIGNAL_WEIGHTS:
                DEFAULT_SIGNAL_WEIGHTS[k] = v
        print(f"Loaded {len(loaded_weights)} signal weights from DB")
        cur.execute("SELECT wallet_address, score, win_rate, runner_trades FROM wallets WHERE runner_trades >= %s", (COHORT_MIN_HITS,))
        rows = cur.fetchall()
        for r in rows:
            cohort_wallets[r["wallet_address"]] = {
                "hits": r["runner_trades"],
                "score": r["score"],
                "win_rate": r["win_rate"],
                "last_seen": time.time()
            }
        print(f"Loaded {len(cohort_wallets)} cohort wallets from DB")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"State load error: {e}")

def is_blacklisted(address):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM blacklists WHERE address = %s", (address,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except:
        return False

def add_to_blacklist(address, reason, blacklist_type):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO blacklists (address, reason, blacklist_type, added_at)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (address, reason, blacklist_type, int(time.time())))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Blacklist error: {e}")

def save_alert_to_db(token_address, token_name, token_symbol, mcap, liquidity,
                     volume, buys, sells, pc5m, pc1h, pc24h,
                     early_score, confirmation_score, signals, source, tier):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alerts (token_address, token_name, token_symbol, timestamp,
                mcap_at_alert, liquidity, volume, buys, sells,
                price_change_5m, price_change_1h, price_change_24h,
                early_score, confirmation_score, triggered_signals, source, alert_tier)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (token_address, token_name, token_symbol, int(time.time()),
              mcap, liquidity, volume, buys, sells, pc5m, pc1h, pc24h,
              early_score, confirmation_score, json.dumps(signals), source, tier))
        cur.execute("""
            INSERT INTO outcomes (token_address, alert_mcap, peak_mcap, alerted_at)
            VALUES (%s, %s, %s, %s) ON CONFLICT (token_address) DO NOTHING
        """, (token_address, mcap, mcap, int(time.time())))
        cur.execute("""
            INSERT INTO tokens_seen (token_address, first_seen, last_seen, alerted)
            VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (token_address) DO UPDATE SET last_seen = %s, alerted = TRUE
        """, (token_address, int(time.time()), int(time.time()), int(time.time())))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Save alert error: {e}")

def save_tracking_to_db(token_address, token_name, token_symbol, mcap, dex_url,
                        tier, early_score, confirmation_score, signals):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tracking (token_address, token_name, token_symbol, alert_mcap,
                alerted_at, milestones_hit, dex_url, alert_tier, early_score,
                confirmation_score, triggered_signals)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (token_address) DO NOTHING
        """, (token_address, token_name, token_symbol, mcap, int(time.time()),
              json.dumps([]), dex_url, tier, early_score, confirmation_score,
              json.dumps(signals)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Save tracking error: {e}")

def update_outcome_in_db(token_address, current_mcap, hours_since):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT alert_mcap, peak_mcap, time_to_2x, time_to_5x, final_classification FROM outcomes WHERE token_address = %s", (token_address,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return None
        alert_mcap = row["alert_mcap"] or 0
        if alert_mcap <= 0 or current_mcap <= 0:
            cur.close()
            conn.close()
            return None
        peak_mcap = max(row["peak_mcap"] or 0, current_mcap)
        multiplier = peak_mcap / alert_mcap
        minutes_since = int(hours_since * 60)
        time_to_2x = row["time_to_2x"]
        time_to_5x = row["time_to_5x"]
        if multiplier >= 2 and time_to_2x is None:
            time_to_2x = minutes_since
        if multiplier >= 5 and time_to_5x is None:
            time_to_5x = minutes_since
        outcome = row["final_classification"]
        if hours_since >= 24 and outcome is None:
            if multiplier >= 10:
                outcome = "10x"
            elif multiplier >= 5:
                outcome = "5x"
            elif multiplier >= 2:
                outcome = "2x"
            elif multiplier <= 0.3:
                outcome = "rug"
            elif multiplier >= 1.3:
                outcome = "small_win"
            else:
                outcome = "flat"
        cur.execute("""
            UPDATE outcomes SET peak_mcap=%s, time_to_2x=%s, time_to_5x=%s,
            final_classification=%s, resolved_at=%s WHERE token_address=%s
        """, (peak_mcap, time_to_2x, time_to_5x, outcome, int(time.time()), token_address))
        conn.commit()
        cur.close()
        conn.close()
        if outcome:
            update_signal_weights_in_db(token_address, outcome)
        return outcome
    except Exception as e:
        print(f"Outcome update error: {e}")
        return None

def update_signal_weights_in_db(token_address, outcome):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT triggered_signals FROM alerts WHERE token_address = %s ORDER BY timestamp DESC LIMIT 1", (token_address,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return
        signals = row["triggered_signals"] or []
        is_success = outcome in ["2x", "5x", "10x"]
        is_early_success = outcome in ["5x", "10x"]
        is_rug = outcome == "rug"
        for signal in signals:
            cur.execute("""
                INSERT INTO signal_weights (signal_name, weight, total_uses, successful_uses)
                VALUES (%s, 1.0, 1, %s)
                ON CONFLICT (signal_name) DO UPDATE SET
                total_uses = signal_weights.total_uses + 1,
                successful_uses = signal_weights.successful_uses + %s
            """, (signal, 1 if is_success else 0, 1 if is_success else 0))
            cur.execute("SELECT weight, total_uses, successful_uses FROM signal_weights WHERE signal_name = %s", (signal,))
            sw = cur.fetchone()
            if sw and sw["total_uses"] >= 5:
                success_rate = sw["successful_uses"] / sw["total_uses"]
                current_weight = sw["weight"]
                if is_early_success:
                    new_weight = min(5.0, current_weight * 1.05)
                elif is_success:
                    new_weight = min(5.0, current_weight * 1.02)
                elif is_rug:
                    new_weight = max(0.1, current_weight * 0.90)
                else:
                    new_weight = max(0.1, current_weight * 0.98)
                cur.execute("UPDATE signal_weights SET weight=%s, success_rate=%s WHERE signal_name=%s",
                            (new_weight, success_rate, signal))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Signal weight update error: {e}")

def update_wallet_in_db(wallet_address, is_runner=False):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            INSERT INTO wallets (wallet_address, score, total_trades, runner_trades, last_seen)
            VALUES (%s, 1.0, 1, %s, %s)
            ON CONFLICT (wallet_address) DO UPDATE SET
            total_trades = wallets.total_trades + 1,
            runner_trades = wallets.runner_trades + %s,
            last_seen = %s
        """, (wallet_address, 1 if is_runner else 0, int(time.time()),
              1 if is_runner else 0, int(time.time())))
        cur.execute("SELECT total_trades, runner_trades FROM wallets WHERE wallet_address = %s", (wallet_address,))
        row = cur.fetchone()
        if row and row["total_trades"] >= 3:
            win_rate = row["runner_trades"] / row["total_trades"]
            score = 1.0 + (win_rate * 4.0)
            cur.execute("UPDATE wallets SET win_rate=%s, score=%s WHERE wallet_address=%s",
                        (win_rate, score, wallet_address))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Wallet update error: {e}")

def get_wallet_scores(wallet_addresses):
    if not wallet_addresses:
        return {}
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT wallet_address, score, win_rate, runner_trades FROM wallets WHERE wallet_address = ANY(%s)",
                    (list(wallet_addresses),))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {r["wallet_address"]: {"score": r["score"], "win_rate": r["win_rate"], "runner_trades": r["runner_trades"]} for r in rows}
    except Exception as e:
        print(f"Get wallet scores error: {e}")
        return {}

def get_signal_weight(signal_name):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT weight FROM signal_weights WHERE signal_name = %s", (signal_name,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return row["weight"]
    except:
        pass
    return DEFAULT_SIGNAL_WEIGHTS.get(signal_name, 1.0)

def was_alerted(token_address):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT alerted FROM tokens_seen WHERE token_address = %s", (token_address,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            return True
        return False
    except:
        return False

def get_tracking_list():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cutoff = int(time.time()) - (48 * 3600)
        cur.execute("SELECT * FROM tracking WHERE alerted_at > %s", (cutoff,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {r["token_address"]: dict(r) for r in rows}
    except Exception as e:
        print(f"Get tracking error: {e}")
        return {}

def update_milestone_in_db(token_address, milestone):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT milestones_hit FROM tracking WHERE token_address = %s", (token_address,))
        row = cur.fetchone()
        if row:
            hits = row["milestones_hit"] or []
            if milestone not in hits:
                hits.append(milestone)
                cur.execute("UPDATE tracking SET milestones_hit = %s WHERE token_address = %s",
                            (json.dumps(hits), token_address))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Milestone update error: {e}")

# ── DEFAULT SIGNAL WEIGHTS ────────────────────────────────────

DEFAULT_SIGNAL_WEIGHTS = {
    "pumpfun_active": 1.0,
    "pumpfun_graduated": 3.0,
    "dexscreener": 1.0,
    "birdeye": 1.0,
    "price_acceleration": 3.0,
    "price_change_5m_20": 3.0,
    "price_change_5m_10": 2.0,
    "price_change_1h_100": 3.0,
    "price_change_1h_50": 2.0,
    "price_change_1h_10": 1.0,
    "high_volume_ratio": 2.0,
    "heavy_buy_pressure": 2.0,
    "buy_pressure_acceleration": 3.0,
    "buys_5m_10": 2.0,
    "buys_5m_5": 1.0,
    "strong_liquidity": 1.0,
    "very_fresh": 2.0,
    "fresh": 1.0,
    "ultra_micro_mcap": 3.0,
    "micro_mcap": 2.0,
    "low_mcap": 1.0,
    "bullish_narrative_strong": 2.0,
    "bullish_narrative": 1.0,
    "ai_narrative": 3.0,
    "community_narrative": 2.0,
    "fair_launch": 3.0,
    "dip_entry": 2.0,
    "graduation_runner": 3.0,
    "wallet_swarm": 3.0,
    "buy_momentum_ladder": 3.0,
    "holder_dispersion": 2.0,
    "silent_accumulation": 2.0,
    "cohort_wallet_hit": 4.0,
    "wallet_network_hit": 5.0,
    "wallet_burst": 4.0,
    "buy_velocity_spike": 4.0,
    "micro_volume_acceleration": 3.0,
    "dev_holding": 2.0,
    "silence_break": 3.0,
    "bonding_curve_fast_fill": 5.0,
    "smart_wallet_early_entry": 5.0,
    "early_organic_entry": 3.0,
}

# ── CONTENT FILTERS ───────────────────────────────────────────

def is_mayhem_token(name, symbol, description):
    combined = f"{name.lower()} {symbol.lower()} {(description or '').lower()}"
    for keyword in MAYHEM_KEYWORDS:
        if keyword in combined:
            print(f"Mayhem filter: skipping {name} — matched '{keyword}'")
            return True
    return False

def is_based_narrative(name, symbol, description):
    combined = f"{name.lower()} {symbol.lower()} {(description or '').lower()}"
    based_triggers = ["based", "base chain", "basedafi", "base narrative"]
    for trigger in based_triggers:
        if trigger in combined:
            print(f"Based filter: skipping {name} — matched '{trigger}'")
            return True
    return False

# ── TELEGRAM ──────────────────────────────────────────────────

def send_telegram(message, chat_id=None):
    if chat_id is None:
        chat_id = TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def broadcast(message):
    send_telegram(message, TELEGRAM_CHAT_ID)
    for group_id in list(active_groups.keys()):
        try:
            send_telegram(message, group_id)
        except Exception as e:
            print(f"Broadcast error {group_id}: {e}")

# ── SCORING SYSTEM ────────────────────────────────────────────

def calculate_scores(signals, source, mcap, narratives, is_early=False):
    early_signals = {
        "wallet_burst", "buy_velocity_spike", "micro_volume_acceleration",
        "bonding_curve_fast_fill", "smart_wallet_early_entry", "wallet_swarm",
        "silence_break", "early_organic_entry", "dev_holding", "very_fresh",
        "ultra_micro_mcap", "cohort_wallet_hit", "wallet_network_hit"
    }
    confirmation_signals = {
        "price_acceleration", "price_change_5m_20", "price_change_1h_100",
        "price_change_1h_50", "heavy_buy_pressure", "buy_pressure_acceleration",
        "buys_5m_10", "buy_momentum_ladder", "graduation_runner", "dip_entry",
        "high_volume_ratio", "strong_liquidity", "holder_dispersion",
        "pumpfun_graduated", "bullish_narrative_strong"
    }
    early_score = 0.0
    confirmation_score = 0.0
    for signal in signals:
        weight = get_signal_weight(signal)
        if signal in early_signals:
            early_score += weight
        elif signal in confirmation_signals:
            confirmation_score += weight
        else:
            confirmation_score += weight * 0.5
    early_score = min(10.0, early_score)
    confirmation_score = min(10.0, confirmation_score)
    return round(early_score, 1), round(confirmation_score, 1)

def get_alert_tier(early_score, confirmation_score):
    if early_score >= 6:
        return "EARLY", "🔵"
    elif confirmation_score >= 8:
        return "VERY STRONG", "🔴"
    elif confirmation_score >= 5:
        return "STRONG", "🟠"
    elif confirmation_score >= 3 or early_score >= 3:
        return "WATCH", "🟡"
    return None, None

def get_confidence_pct(signals, early_score, confirmation_score):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        rates = []
        for signal in signals:
            cur.execute("SELECT success_rate, total_uses FROM signal_weights WHERE signal_name = %s", (signal,))
            row = cur.fetchone()
            if row and row["total_uses"] >= 5:
                rates.append(row["success_rate"])
        cur.close()
        conn.close()
        if rates:
            avg = sum(rates) / len(rates)
            return int(avg * 100)
    except:
        pass
    base = (early_score + confirmation_score) / 20 * 100
    return int(min(95, max(20, base)))

# ── HONEYPOT CHECK ────────────────────────────────────────────

def is_honeypot(token_address):
    if is_blacklisted(token_address):
        return True, "Previously flagged"
    try:
        url = f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}&chainID=1399811149"
        r = requests.get(url, timeout=8)
        data = r.json()
        honeypot_result = data.get("honeypotResult", {})
        simulation = data.get("simulationResult", {})
        is_hp = honeypot_result.get("isHoneypot", False)
        reason = honeypot_result.get("honeypotReason", "")
        sell_tax = simulation.get("sellTax", 0) or 0
        buy_tax = simulation.get("buyTax", 0) or 0
        if is_hp:
            add_to_blacklist(token_address, f"Honeypot: {reason}", "honeypot")
            return True, f"Honeypot: {reason}"
        if sell_tax > 15:
            add_to_blacklist(token_address, f"High sell tax: {sell_tax:.0f}%", "honeypot")
            return True, f"High sell tax: {sell_tax:.0f}%"
        if buy_tax > 15:
            return True, f"High buy tax: {buy_tax:.0f}%"
        return False, "Clean"
    except Exception as e:
        print(f"Honeypot error: {e}")
        return False, "Unknown"

# ── WALLET INTELLIGENCE ───────────────────────────────────────

def get_pool_wallets(token_address, limit=20):
    wallets = set()
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        pairs = r.json().get("pairs", [])
        if not pairs:
            return wallets
        pair_address = pairs[0].get("pairAddress", "")
        if not pair_address:
            return wallets
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                   "params": [pair_address, {"limit": limit}]}
        r2 = requests.post(SOLANA_RPC, json=payload, timeout=10)
        sigs = r2.json().get("result", [])
        for sig_info in sigs[:10]:
            try:
                sig = sig_info.get("signature", "")
                if not sig:
                    continue
                payload2 = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                            "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}]}
                r3 = requests.post(SOLANA_RPC, json=payload2, timeout=8)
                tx = r3.json().get("result", {})
                if tx:
                    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    if account_keys:
                        wallets.add(account_keys[0])
                time.sleep(0.15)
            except:
                continue
    except Exception as e:
        print(f"Wallet fetch error: {e}")
    return wallets

def detect_wallet_burst(token_address, buys_5m):
    try:
        if buys_5m < 3:
            return False, "Not enough buys"
        wallets = get_pool_wallets(token_address, limit=10)
        if 3 <= len(wallets) <= 8:
            return True, f"💥 Wallet burst: {len(wallets)} unique wallets"
        return False, "No burst"
    except:
        return False, "Error"

def detect_buy_velocity_spike(token_address, buys_5m):
    if buys_5m >= 8:
        return True, f"⚡ Buy velocity spike: {buys_5m} buys/5min"
    return False, "Normal velocity"

def detect_micro_volume_acceleration(token_address, volume_data):
    windows = token_buy_windows.get(token_address, [])
    if len(windows) < 2:
        return False, "Not enough data"
    recent_vols = [w.get("volume", 0) for w in windows[-3:]]
    if len(recent_vols) >= 2 and recent_vols[-1] > recent_vols[0] * 3 and recent_vols[-1] >= 3000:
        return True, f"📈 Volume accel: ${recent_vols[0]:,.0f} → ${recent_vols[-1]:,.0f}"
    return False, "Normal volume"

def detect_silence_break(token_address, buys_5m, buys_1h):
    windows = token_buy_windows.get(token_address, [])
    if len(windows) < 3:
        return False, "Not enough data"
    old_avg = sum(w["buys"] for w in windows[:-2]) / max(len(windows) - 2, 1)
    if old_avg <= 1 and buys_5m >= 5:
        return True, f"🔇→🔊 Silence break: {old_avg:.0f} → {buys_5m} buys"
    return False, "No silence break"

def check_smart_wallet_entry(token_address, current_wallets, entry_mcap):
    if not current_wallets or entry_mcap > 50_000:
        return False, 0, "Mcap too high"
    wallet_scores = get_wallet_scores(current_wallets)
    high_score = {w: d for w, d in wallet_scores.items() if d["score"] >= 2.0 and d["runner_trades"] >= 2}
    if len(high_score) >= 2:
        return True, len(high_score), f"🎯 {len(high_score)} smart wallets @ ${entry_mcap:,.0f}"
    return False, 0, "No smart wallet entry"

def detect_wallet_swarm(token_address, buys_5m, buys_1h):
    try:
        if buys_5m < 8:
            return False, 0, "Not enough buys"
        wallets = get_pool_wallets(token_address, limit=15)
        unique_count = len(wallets)
        token_buy_windows[token_address].append({"buys": buys_5m, "wallets": wallets, "timestamp": time.time()})
        if unique_count >= 6 and buys_5m >= 8:
            return True, unique_count, f"🐝 {unique_count} unique wallets in 5min"
        return False, unique_count, "Normal"
    except:
        return False, 0, "Error"

def detect_buy_momentum_ladder(token_address, buys_5m, buys_1h):
    windows = token_buy_windows.get(token_address, [])
    if len(windows) < 3:
        token_buy_windows[token_address].append({"buys": buys_5m, "wallets": set(), "timestamp": time.time(), "volume": 0})
        return False, "Not enough windows"
    recent = [w["buys"] for w in windows[-4:]]
    increasing = all(recent[i] < recent[i+1] for i in range(len(recent)-1))
    if increasing and recent[-1] >= 5:
        return True, f"📈 Momentum ladder: {' → '.join(str(b) for b in recent)}"
    if len(recent) >= 3 and recent[-1] > recent[0] * 1.5 and recent[-1] >= 5:
        return True, f"📈 Buy accel: {recent[0]} → {recent[-1]}"
    return False, "No ladder"

def detect_holder_dispersion(token_address):
    try:
        history = holder_history[token_address]
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]}
        r = requests.post(SOLANA_RPC, json=payload, timeout=10)
        accounts = r.json().get("result", {}).get("value", [])
        holder_count = len([a for a in accounts if float(a.get("uiAmount", 0) or 0) > 0])
        now = time.time()
        history.append((now, holder_count))
        if len(history) > 20:
            holder_history[token_address] = history[-20:]
        recent = [(t, h) for t, h in history if now - t <= 300]
        if len(recent) < 2:
            return False, "Not enough data"
        oldest_h = recent[0][1]
        newest_h = recent[-1][1]
        if oldest_h > 0:
            growth_pct = ((newest_h - oldest_h) / oldest_h) * 100
            if growth_pct >= 30 and newest_h >= 10:
                return True, f"👥 Holders +{growth_pct:.0f}% in 5min ({newest_h} total)"
        return False, "Normal growth"
    except:
        return False, "Error"

def detect_silent_accumulation(token_address, buys_5m):
    windows = token_buy_windows.get(token_address, [])
    token_buy_windows[token_address].append({"buys": buys_5m, "wallets": set(), "timestamp": time.time(), "volume": 0})
    if len(windows) < 3:
        return False, "Not enough windows"
    recent = [w["buys"] for w in windows[-5:]]
    if len(recent) >= 3:
        all_increasing = all(recent[i] <= recent[i+1] for i in range(len(recent)-1))
        if all_increasing and recent[0] <= 3 and recent[-1] >= 5:
            return True, f"🤫 Silent accum: {' → '.join(str(b) for b in recent)}"
    return False, "No accumulation"

def detect_price_acceleration(price_history):
    if len(price_history) < 3:
        return False, "Not enough data"
    recent = price_history[-4:]
    up_moves = sum(1 for i in range(len(recent)-1) if recent[i+1] > recent[i])
    if up_moves >= 3:
        total_change = ((recent[-1] - recent[0]) / recent[0] * 100) if recent[0] > 0 else 0
        return True, f"🚀 Price accelerating: +{total_change:.0f}%"
    return False, "No acceleration"

def detect_buy_pressure_acceleration(buys_history):
    if len(buys_history) < 3:
        return False, "Not enough data"
    recent = buys_history[-3:]
    if recent[-1] > recent[0] * 1.5 and recent[-1] >= recent[-2]:
        return True, f"💚 Buy pressure accel: {recent[0]} → {recent[-1]}"
    return False, "Normal"

# ── BONDING CURVE ─────────────────────────────────────────────

def get_bonding_curve_fill(token_address, coin_data=None):
    try:
        if coin_data:
            total_supply = coin_data.get("total_supply", 0) or 0
            real_token = coin_data.get("real_token_reserves", 0) or 0
            if total_supply > 0:
                sold_pct = ((total_supply - real_token) / total_supply) * 100
                return sold_pct
        mcap = coin_data.get("usd_market_cap", 0) if coin_data else 0
        if mcap > 0:
            fill_pct = (mcap / 85000) * 100
            return min(100, fill_pct)
    except:
        pass
    return None

def detect_fast_bonding_curve(token_address, current_fill, coin_data=None):
    if current_fill is None:
        return False, "No curve data"
    history = pumpfun_curve_history[token_address]
    now = time.time()
    history.append((now, current_fill))
    if len(history) > 20:
        pumpfun_curve_history[token_address] = history[-20:]
    recent = [(t, f) for t, f in history if now - t <= 180]
    if len(recent) >= 2:
        oldest_fill = recent[0][1]
        newest_fill = recent[-1][1]
        fill_speed = newest_fill - oldest_fill
        if fill_speed >= 40 and newest_fill <= 80:
            return True, f"🎯 Bonding curve {oldest_fill:.0f}% → {newest_fill:.0f}% in 3min!"
        elif newest_fill >= 50 and fill_speed >= 20:
            return True, f"🎯 Fast fill: {newest_fill:.0f}% filled"
    return False, "Normal fill"

# ── NARRATIVE DETECTION ───────────────────────────────────────

def get_narrative_tags(description, name, symbol):
    combined = f"{(description or '').lower()} {name.lower()} {symbol.lower()}"
    tags = []
    detected_narratives = []
    score = 0
    bullish = {
        "ai": ("🤖 AI", "ai_narrative"),
        "agent": ("🤖 Agent", "ai_narrative"),
        "meme": ("😂 Meme", "meme_narrative"),
        "dog": ("🐕 Dog", "dog_narrative"),
        "cat": ("🐈 Cat", "cat_narrative"),
        "pepe": ("🐸 Pepe", "pepe_narrative"),
        "elon": ("⚡ Elon", "elon_narrative"),
        "trump": ("🇺🇸 Political", "political_narrative"),
        "community": ("👥 Community", "community_narrative"),
        "viral": ("📱 Viral", "viral_narrative"),
        "fair launch": ("✅ Fair", "fair_launch"),
        "renounced": ("✅ Renounced", "fair_launch"),
        "burned": ("🔥 Burned", "fair_launch"),
        "gas": ("⛽ Utility", "utility_narrative"),
    }
    for keyword, (tag, narrative_key) in bullish.items():
        if keyword in combined:
            tags.append(tag)
            score += 2
            if narrative_key not in detected_narratives:
                detected_narratives.append(narrative_key)
    for bad in ["rug", "scam", "fake", "honeypot"]:
        if bad in combined:
            score -= 5
    return tags, score, detected_narratives

# ── WALLET CONCENTRATION ──────────────────────────────────────

def check_wallet_concentration(token_address):
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]}
        r = requests.post(SOLANA_RPC, json=payload, timeout=10)
        accounts = r.json().get("result", {}).get("value", [])
        if not accounts:
            return None, "Unknown"
        total = sum(float(a.get("uiAmount", 0) or 0) for a in accounts)
        if total == 0:
            return None, "Unknown"
        top10 = sum(float(a.get("uiAmount", 0) or 0) for a in accounts[:10])
        concentration = (top10 / total) * 100
        if concentration > 50:
            return concentration, f"🔴 HIGH {concentration:.0f}%"
        elif concentration > 30:
            return concentration, f"🟡 MED {concentration:.0f}%"
        else:
            return concentration, f"🟢 LOW {concentration:.0f}%"
    except:
        return None, "?"

# ── RUG RISK ──────────────────────────────────────────────────

def get_rug_risk(pair, source):
    risk_score = 0
    flags = []
    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
    mcap = pair.get("marketCap", 0) or 0
    volume_24h = pair.get("volume", {}).get("h24", 0) or 0
    txns = pair.get("txns", {})
    buys_24h = txns.get("h24", {}).get("buys", 0)
    sells_24h = txns.get("h24", {}).get("sells", 0)
    buys_1h = txns.get("h1", {}).get("buys", 0)
    price_change_24h = pair.get("priceChange", {}).get("h24", 0) or 0
    if mcap > 0 and liquidity < mcap * 0.05:
        risk_score += 2
        flags.append("Low liq/mcap")
    if sells_24h > buys_24h * 1.5 and buys_24h > 0:
        risk_score += 2
        flags.append("Sell pressure")
    if price_change_24h > 500:
        risk_score += 1
        flags.append("Extreme pump")
    if liquidity < 20_000:
        risk_score += 1
        flags.append("Low liquidity")
    if volume_24h > mcap * 3 and mcap > 0:
        risk_score += 1
        flags.append("Suspicious vol")
    if buys_1h < 5 and source != "pumpfun":
        risk_score += 1
        flags.append("Few buyers")
    if risk_score == 0:
        flags.append("No major flags")
    label = "🔴 HIGH" if risk_score >= 4 else "🟡 MED" if risk_score >= 2 else "🟢 LOW"
    return label, flags, risk_score

def is_real_pump(buy_vol, sell_vol, buys, sells, price_change_5m):
    if buy_vol <= 0 and sell_vol <= 0:
        return True, "No vol data"
    if sell_vol > buy_vol * 2:
        return False, "Sell dominated"
    if price_change_5m > 20 and buys < 5:
        return False, "<5 buyers"
    return True, "Real"

# ── ALERT FORMATTING ──────────────────────────────────────────

def format_alert(
    token_name, token_symbol, token_address,
    mcap, liquidity, volume, buys, sells,
    price_change_5m, price_change_1h, price_change_24h,
    age_hours, early_score, confirmation_score,
    tier_label, tier_emoji, confidence_pct,
    signals, reasons, narrative_tags, narrative_score,
    concentration_label, rug_label, rug_flags,
    dex_url, source_label, social_links="",
    smart_wallet_count=0, smart_wallet_msg="",
    graduation_mcap=None, change_from_graduation=None,
    alert_type_extra=""
):
    entry_class = "EARLY" if mcap < 80_000 else "MID" if mcap < 300_000 else "LATE"
    score_display = max(early_score, confirmation_score)
    trojan = f"https://t.me/paris_trojanbot?start=snipe_{token_address}"
    raydium = f"https://raydium.io/swap/?inputCurrency=SOL&outputCurrency={token_address}"

    msg = f"{tier_emoji} *{tier_label} — {token_name}* (${token_symbol})\n"
    msg += f"Score: `{score_display}/10` | Entry: `{entry_class}` | Conf: `{confidence_pct}%`\n"
    if alert_type_extra:
        msg += f"_{alert_type_extra}_\n"
    msg += "\n"

    grad_line = f"Migration: `${graduation_mcap:,.0f}` → " if graduation_mcap else ""
    msg += f"*📊 STATS*\n"
    msg += f"Mcap: `{grad_line}${mcap:,.0f}`\n"
    msg += f"Vol/Liq: `${volume:,.0f}` / `${liquidity:,.0f}`\n"
    msg += f"Buys/Sells: `{buys}` / `{sells}`\n"
    msg += f"Age: `{f'{age_hours:.1f}hrs' if age_hours else '<1hr'}`\n"
    msg += f"Price: `{price_change_5m:+.0f}%` 5m | `{price_change_1h:+.0f}%` 1h | `{price_change_24h:+.0f}%` 24h\n"
    msg += "\n"

    if reasons:
        msg += f"*⚡ SIGNALS*\n"
        for r in reasons[:6]:
            msg += f"• {r}\n"
        msg += "\n"

    if smart_wallet_count >= 2:
        msg += f"*🎯 SMART MONEY*\n"
        msg += f"{smart_wallet_msg}\n\n"

    msg += f"*🛡 RISK*\n"
    msg += f"Rug: `{rug_label}` | Wallets: `{concentration_label}`\n"
    if rug_flags:
        msg += f"_{', '.join(rug_flags[:2])}_\n"
    msg += "\n"

    if narrative_tags:
        msg += f"*🧠 NARRATIVE*\n"
        msg += " ".join(narrative_tags[:5]) + "\n\n"

    if social_links:
        msg += f"*🔗 SOCIALS* {social_links}\n\n"

    msg += f"*CA:* `{token_address}`\n\n"
    msg += f"[⚡ Snipe]({trojan}) | [🔄 Buy]({raydium}) | [📊 Chart]({dex_url})\n\n"
    msg += f"_Targets: +50% +100% +200% +500% +1000%_\n"
    msg += f"_⚠️ DYOR. Not financial advice._"
    return msg

def format_milestone_alert(info, token_address, alert_mcap, current_mcap, change_pct, hours_since, milestone):
    emoji = "🤯" if milestone >= 500 else "🚀🚀" if milestone >= 200 else "🚀"
    trojan = f"https://t.me/paris_trojanbot?start=snipe_{token_address}"
    dex_url = info.get("dex_url", "")
    next_milestones = [m for m in MILESTONES if m > milestone]
    next_str = " | ".join([f"+{m}%" for m in next_milestones[:3]])
    early_score = info.get("early_score", 0)
    confirmation_score = info.get("confirmation_score", 0)

    msg = f"{emoji} *+{milestone}% HIT — {info['token_name']}* (${info['token_symbol']})\n\n"
    msg += f"*📈 PERFORMANCE*\n"
    msg += f"Entry: `${alert_mcap:,.0f}` → Now: `${current_mcap:,.0f}`\n"
    msg += f"Gain: `+{change_pct:.0f}%` in `{hours_since:.1f}hrs`\n"
    msg += f"Original score: `{max(early_score, confirmation_score)}/10`\n\n"
    if next_str:
        msg += f"*Next targets:* {next_str}\n\n"
    msg += f"[📊 Chart]({dex_url}) | [⚡ Snipe]({trojan})"
    return msg

# ── DATA FETCHING ─────────────────────────────────────────────

def fetch_dexscreener_new_pairs():
    pairs = []
    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/search?q=solana", timeout=10)
        pairs = [p for p in r.json().get("pairs", []) if p.get("chainId") == "solana"]
        print(f"Dexscreener new: {len(pairs)}")
    except Exception as e:
        print(f"Dexscreener new error: {e}")
    return pairs

def fetch_dexscreener_trending():
    pairs = []
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10)
        data = r.json()
        solana = [t for t in (data if isinstance(data, list) else data.get("pairs", [])) if t.get("chainId") == "solana"]
        for addr in [t.get("tokenAddress") for t in solana if t.get("tokenAddress")][:20]:
            try:
                r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=10)
                d2 = r2.json()
                if isinstance(d2, dict):
                    pairs.extend(d2.get("pairs", []))
                time.sleep(0.2)
            except:
                continue
        print(f"Dexscreener trending: {len(pairs)}")
    except Exception as e:
        print(f"Dexscreener trending error: {e}")
    return pairs

def fetch_dexscreener_gainers():
    pairs = []
    try:
        for query in ["pump", "sol", "meme", "cat", "dog", "pepe", "ai"]:
            try:
                r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={query}", timeout=10)
                pairs.extend([p for p in r.json().get("pairs", []) if p.get("chainId") == "solana"])
                time.sleep(0.2)
            except:
                continue
        print(f"Dexscreener gainers: {len(pairs)}")
    except Exception as e:
        print(f"Dexscreener gainers error: {e}")
    return pairs

def fetch_pumpfun_graduated():
    try:
        url = "https://frontend-api-v3.pump.fun/coins?limit=50&sort=usd_market_cap&order=desc&includeNsfw=false"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        coins = r.json()
        coin_list = coins if isinstance(coins, list) else coins.get("coins", [])
        graduated = [c for c in coin_list if c.get("complete") == True]
        print(f"Pump.fun graduated: {len(graduated)}")
        for coin in graduated:
            try:
                token_address = coin.get("mint", "")
                name = coin.get("name", "Unknown")
                symbol = coin.get("symbol", "?")
                description = coin.get("description", "") or ""

                if not token_address or is_blacklisted(token_address) or was_alerted(token_address):
                    continue
                if is_mayhem_token(name, symbol, description):
                    continue
                if is_based_narrative(name, symbol, description):
                    continue

                mcap = coin.get("usd_market_cap", 0) or 0
                if mcap < GRADUATION_MCAP_MIN or mcap > MCAP_MAX:
                    continue
                if (coin.get("reply_count", 0) or 0) < 3:
                    continue
                if token_address not in graduation_watchlist:
                    graduation_watchlist[token_address] = {
                        "name": name,
                        "symbol": symbol,
                        "description": description,
                        "twitter": coin.get("twitter", "") or "",
                        "telegram": coin.get("telegram", "") or "",
                        "website": coin.get("website", "") or "",
                        "reply_count": coin.get("reply_count", 0) or 0,
                        "added_at": time.time(),
                        "graduation_mcap": mcap,
                        "price_history": [],
                        "buy_vol_history": [],
                        "sell_vol_history": [],
                        "buy_count_history": [],
                        "sell_count_history": [],
                        "consecutive_rug_signals": 0,
                        "dip_detected": False,
                        "dip_low_mcap": None,
                        "alerted": False,
                        "honeypot_checked": False,
                        "url": f"https://dexscreener.com/solana/{token_address}",
                        "coin_data": coin
                    }
                    print(f"Watching: {name} — ${mcap:,.0f}")
            except:
                continue
    except Exception as e:
        print(f"Pump.fun graduated error: {e}")

def fetch_pumpfun_active():
    pairs = []
    early_pairs = []
    try:
        url = "https://frontend-api-v3.pump.fun/coins?limit=50&sort=usd_market_cap&order=desc&includeNsfw=false"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        coins = r.json()
        coin_list = coins if isinstance(coins, list) else coins.get("coins", [])
        for coin in [c for c in coin_list if not c.get("complete")]:
            try:
                mcap = coin.get("usd_market_cap", 0) or 0
                token_address = coin.get("mint", "")
                name = coin.get("name", "Unknown")
                symbol = coin.get("symbol", "?")
                description = coin.get("description", "") or ""
                created_timestamp = coin.get("created_timestamp", 0)
                age_hours = (time.time() - created_timestamp / 1000) / 3600 if created_timestamp else None
                age_minutes = age_hours * 60 if age_hours else None

                if is_mayhem_token(name, symbol, description):
                    continue
                if is_based_narrative(name, symbol, description):
                    continue

                if (EARLY_MCAP_MIN <= mcap <= EARLY_MCAP_MAX and
                        age_minutes and EARLY_AGE_MIN_MINUTES <= age_minutes <= EARLY_AGE_MAX_MINUTES):
                    fill_pct = get_bonding_curve_fill(token_address, coin)
                    early_pairs.append({
                        "baseToken": {"address": token_address, "name": name, "symbol": symbol},
                        "marketCap": mcap,
                        "volume": {"h24": coin.get("volume", 0) or 0, "m5": 0},
                        "liquidity": {"usd": (coin.get("virtual_sol_reserves", 0) or 0) * 150},
                        "priceChange": {"h1": 0, "h24": 0, "m5": 0},
                        "priceUsd": str(coin.get("price", 0)),
                        "txns": {"h1": {"buys": 0, "sells": 0}, "h24": {"buys": 0, "sells": 0}, "m5": {"buys": 0, "sells": 0}},
                        "pairCreatedAt": created_timestamp,
                        "url": f"https://pump.fun/{token_address}",
                        "source": "pumpfun_early",
                        "description": description,
                        "reply_count": coin.get("reply_count", 0) or 0,
                        "fill_pct": fill_pct,
                        "coin_data": coin,
                        "twitter": coin.get("twitter", "") or "",
                        "telegram": coin.get("telegram", "") or "",
                        "website": coin.get("website", "") or "",
                    })

                if MCAP_MIN <= mcap <= MCAP_MAX:
                    if age_hours and (age_hours < 0.5 or age_hours > 24):
                        continue
                    if (coin.get("reply_count", 0) or 0) < 5:
                        continue
                    pairs.append({
                        "baseToken": {"address": token_address, "name": name, "symbol": symbol},
                        "marketCap": mcap,
                        "volume": {"h24": coin.get("volume", 0) or 0, "m5": 0},
                        "liquidity": {"usd": (coin.get("virtual_sol_reserves", 0) or 0) * 150},
                        "priceChange": {"h1": 0, "h24": 0, "m5": 0},
                        "priceUsd": str(coin.get("price", 0)),
                        "txns": {"h1": {"buys": 0, "sells": 0}, "h24": {"buys": 0, "sells": 0}, "m5": {"buys": 0, "sells": 0}},
                        "pairCreatedAt": created_timestamp,
                        "url": f"https://pump.fun/{token_address}",
                        "source": "pumpfun",
                        "description": description,
                        "reply_count": coin.get("reply_count", 0) or 0,
                        "twitter": coin.get("twitter", "") or "",
                        "telegram": coin.get("telegram", "") or "",
                        "website": coin.get("website", "") or "",
                    })
            except:
                continue
        print(f"Pump.fun active: {len(pairs)} | Early pool: {len(early_pairs)}")
    except Exception as e:
        print(f"Pump.fun active error: {e}")
    return pairs, early_pairs

def fetch_birdeye_trending():
    pairs = []
    try:
        url = "https://public-api.birdeye.so/defi/token_trending?sort_by=v24hUSD&sort_type=desc&offset=0&limit=20"
        r = requests.get(url, headers={"X-API-KEY": "public", "x-chain": "solana"}, timeout=10)
        data = r.json()
        tokens = data.get("data", {}).get("tokens", [])
        print(f"Birdeye: {len(tokens)}")
        for token in tokens:
            try:
                addr = token.get("address", "")
                if not addr:
                    continue
                r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=10)
                d2 = r2.json()
                if isinstance(d2, dict):
                    pairs.extend(d2.get("pairs", []))
                time.sleep(0.2)
            except:
                continue
    except Exception as e:
        print(f"Birdeye error: {e}")
    return pairs

# ── MILESTONE TRACKER ─────────────────────────────────────────

def get_current_mcap(token_address):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        pairs = r.json().get("pairs", [])
        if pairs:
            return pairs[0].get("marketCap", 0) or 0
    except:
        pass
    return 0

def check_milestones():
    tracking = get_tracking_list()
    for token_address, info in tracking.items():
        try:
            alerted_at = info.get("alerted_at", 0)
            hours_since = (time.time() - alerted_at) / 3600
            if hours_since > 48:
                continue
            current_mcap = get_current_mcap(token_address)
            alert_mcap = info.get("alert_mcap", 0)
            if current_mcap <= 0 or alert_mcap <= 0:
                continue
            change_pct = ((current_mcap - alert_mcap) / alert_mcap) * 100
            for checkpoint in [1, 4, 24]:
                if hours_since >= checkpoint:
                    update_outcome_in_db(token_address, current_mcap, hours_since)
            milestones_hit = info.get("milestones_hit") or []
            if isinstance(milestones_hit, str):
                milestones_hit = json.loads(milestones_hit)
            for milestone in MILESTONES:
                if milestone in milestones_hit:
                    continue
                if change_pct >= milestone:
                    msg = format_milestone_alert(
                        info, token_address, alert_mcap, current_mcap,
                        change_pct, hours_since, milestone
                    )
                    broadcast(msg)
                    update_milestone_in_db(token_address, milestone)
                    milestones_hit.append(milestone)
        except Exception as e:
            print(f"Milestone error: {e}")

# ── GRADUATION WATCHLIST ──────────────────────────────────────

def monitor_graduation_watchlist():
    to_remove = []
    for token_address, info in list(graduation_watchlist.items()):
        try:
            if info.get("alerted"):
                to_remove.append(token_address)
                continue
            if (time.time() - info["added_at"]) / 3600 > 12:
                to_remove.append(token_address)
                continue
            if not info.get("honeypot_checked"):
                hp, hp_reason = is_honeypot(token_address)
                info["honeypot_checked"] = True
                if hp:
                    to_remove.append(token_address)
                    continue
            r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
            data = r.json()
            pairs = data.get("pairs", [])
            if not pairs:
                continue
            pair = pairs[0]
            current_mcap = pair.get("marketCap", 0) or 0
            txns = pair.get("txns", {})
            buys_5m = txns.get("m5", {}).get("buys", 0) or 0
            sells_5m = txns.get("m5", {}).get("sells", 0) or 0
            buys_1h = txns.get("h1", {}).get("buys", 0) or 0
            sells_1h = txns.get("h1", {}).get("sells", 0) or 0
            liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
            dex_url = pair.get("url", info["url"])
            buy_vol_5m = pair.get("volume", {}).get("m5", 0) or 0
            sell_vol_5m = buy_vol_5m * 0.4
            if current_mcap <= 0:
                continue
            graduation_mcap = info["graduation_mcap"]
            change_from_graduation = ((current_mcap - graduation_mcap) / graduation_mcap) * 100
            info["price_history"].append(current_mcap)
            info["buy_count_history"].append(buys_5m)
            info["sell_count_history"].append(sells_5m)
            info["buy_vol_history"].append(buy_vol_5m)
            info["sell_vol_history"].append(sell_vol_5m)
            for key in ["price_history", "buy_count_history", "sell_count_history", "buy_vol_history", "sell_vol_history"]:
                if len(info[key]) > 30:
                    info[key] = info[key][-30:]
            recent_buy_vol = sum(info["buy_vol_history"][-5:]) if len(info["buy_vol_history"]) >= 5 else buy_vol_5m
            recent_sell_vol = sum(info["sell_vol_history"][-5:]) if len(info["sell_vol_history"]) >= 5 else sell_vol_5m
            if recent_sell_vol > recent_buy_vol * 3 and sells_5m > buys_5m * 2:
                info["consecutive_rug_signals"] += 1
            else:
                info["consecutive_rug_signals"] = max(0, info["consecutive_rug_signals"] - 1)
            if info["consecutive_rug_signals"] >= 5:
                add_to_blacklist(token_address, "Rug signals", "rug")
                to_remove.append(token_address)
                continue
            if change_from_graduation < -10 and not info["dip_detected"]:
                info["dip_detected"] = True
                info["dip_low_mcap"] = current_mcap
            if info["dip_detected"] and info["dip_low_mcap"]:
                if current_mcap < info["dip_low_mcap"]:
                    info["dip_low_mcap"] = current_mcap
                dip_low = info["dip_low_mcap"]
                recovery_pct = ((current_mcap - dip_low) / dip_low) * 100 if dip_low > 0 else 0
                dip_depth = ((graduation_mcap - dip_low) / graduation_mcap) * 100 if graduation_mcap > 0 else 0
                if recovery_pct > 10 and buys_5m >= 3 and buys_5m > sells_5m and liquidity > MIN_LIQUIDITY:
                    alert_type = f"💎 DIP ENTRY — dipped {dip_depth:.0f}%, recovering +{recovery_pct:.0f}%"
                    _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url)
                    info["alerted"] = True
                    continue
            if change_from_graduation > 30 and buys_1h > sells_1h and current_mcap <= MCAP_MAX:
                alert_type = "🚀 GRADUATION RUNNER — pumping since migration"
                _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url)
                info["alerted"] = True
        except Exception as e:
            print(f"Graduation monitor error: {e}")
    for addr in to_remove:
        if addr in graduation_watchlist:
            del graduation_watchlist[addr]

def _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url):
    name = info["name"]
    symbol = info["symbol"]
    graduation_mcap = info["graduation_mcap"]
    txns = pair.get("txns", {})
    buys_1h = txns.get("h1", {}).get("buys", 0)
    sells_1h = txns.get("h1", {}).get("sells", 0)
    buys_5m = txns.get("m5", {}).get("buys", 0)
    volume_24h = pair.get("volume", {}).get("h24", 0) or 0
    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
    pc5m = pair.get("priceChange", {}).get("m5", 0) or 0
    pc1h = pair.get("priceChange", {}).get("h1", 0) or 0
    pc24h = pair.get("priceChange", {}).get("h24", 0) or 0
    narrative_tags, narrative_score, detected_narratives = get_narrative_tags(info.get("description", ""), name, symbol)
    concentration, concentration_label = check_wallet_concentration(token_address)
    if concentration and concentration > 50:
        return
    rug_label, rug_flags, rug_score = get_rug_risk(pair, "pumpfun_graduated")
    signals = ["pumpfun_graduated"]
    reasons = [alert_type]
    if "dip" in alert_type.lower():
        signals.append("dip_entry")
    else:
        signals.append("graduation_runner")
    early_score, confirmation_score = calculate_scores(signals, "pumpfun_graduated", current_mcap, detected_narratives)
    tier_label, tier_emoji = get_alert_tier(early_score, confirmation_score)
    if not tier_label:
        tier_label, tier_emoji = "WATCH", "🟡"
    confidence_pct = get_confidence_pct(signals, early_score, confirmation_score)
    social_links = ""
    if info.get("twitter"):
        social_links += f"[Twitter]({info['twitter']}) "
    if info.get("telegram"):
        social_links += f"[TG]({info['telegram']}) "
    if info.get("website"):
        social_links += f"[Web]({info['website']})"
    msg = format_alert(
        name, symbol, token_address, current_mcap, liquidity, volume_24h,
        buys_1h, sells_1h, pc5m, pc1h, pc24h, None,
        early_score, confirmation_score, tier_label, tier_emoji, confidence_pct,
        signals, reasons, narrative_tags, narrative_score,
        concentration_label, rug_label, rug_flags, dex_url, "Pump.fun Grad",
        social_links=social_links, graduation_mcap=graduation_mcap,
        change_from_graduation=change_from_graduation
    )
    save_alert_to_db(token_address, name, symbol, current_mcap, liquidity, volume_24h,
                     buys_1h, sells_1h, pc5m, pc1h, pc24h,
                     early_score, confirmation_score, signals, "pumpfun_graduated", tier_label)
    save_tracking_to_db(token_address, name, symbol, current_mcap, dex_url,
                        tier_label, early_score, confirmation_score, signals)
    broadcast(msg)
    print(f"Graduation alert: {name} — {tier_label}")

# ── EARLY DETECTION ENGINE ────────────────────────────────────

def analyze_early(early_pairs):
    for pair in early_pairs:
        try:
            token_address = pair.get("baseToken", {}).get("address", "")
            if not token_address or is_blacklisted(token_address) or was_alerted(token_address):
                continue
            token_name = pair.get("baseToken", {}).get("name", "Unknown")
            token_symbol = pair.get("baseToken", {}).get("symbol", "?")
            description = pair.get("description", "") or ""

            if is_mayhem_token(token_name, token_symbol, description):
                continue
            if is_based_narrative(token_name, token_symbol, description):
                continue

            mcap = pair.get("marketCap", 0) or 0
            volume = pair.get("volume", {}).get("h24", 0) or 0
            liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
            txns = pair.get("txns", {})
            buys_5m = txns.get("m5", {}).get("buys", 0) or 0
            sells_5m = txns.get("m5", {}).get("sells", 0) or 0
            buys_1h = txns.get("h1", {}).get("buys", 0) or 0
            dex_url = pair.get("url", "")
            fill_pct = pair.get("fill_pct")
            pair_created = pair.get("pairCreatedAt")
            age_hours = (time.time() - pair_created / 1000) / 3600 if pair_created else None

            if liquidity < EARLY_LIQUIDITY_MIN:
                continue
            if volume < EARLY_VOLUME_MIN:
                continue

            hp, _ = is_honeypot(token_address)
            if hp:
                continue

            active_signals = []
            reasons = []

            if fill_pct is not None:
                fast_fill, fill_msg = detect_fast_bonding_curve(token_address, fill_pct, pair.get("coin_data"))
                if fast_fill:
                    active_signals.append("bonding_curve_fast_fill")
                    reasons.append(fill_msg)

            burst, burst_msg = detect_wallet_burst(token_address, buys_5m)
            if burst:
                active_signals.append("wallet_burst")
                reasons.append(burst_msg)

            vel, vel_msg = detect_buy_velocity_spike(token_address, buys_5m)
            if vel:
                active_signals.append("buy_velocity_spike")
                reasons.append(vel_msg)

            silence, silence_msg = detect_silence_break(token_address, buys_5m, buys_1h)
            if silence:
                active_signals.append("silence_break")
                reasons.append(silence_msg)

            current_wallets = set()
            if buys_5m >= 3:
                current_wallets = get_pool_wallets(token_address, limit=10)
            smart_hit, smart_count, smart_msg = check_smart_wallet_entry(token_address, current_wallets, mcap)
            if smart_hit:
                active_signals.append("smart_wallet_early_entry")
                reasons.append(smart_msg)

            vol_accel, vol_msg = detect_micro_volume_acceleration(token_address, volume)
            if vol_accel:
                active_signals.append("micro_volume_acceleration")
                reasons.append(vol_msg)

            if not active_signals:
                continue

            narrative_tags, narrative_score, detected_narratives = get_narrative_tags(description, token_name, token_symbol)
            early_score, confirmation_score = calculate_scores(active_signals, "pumpfun_early", mcap, detected_narratives, is_early=True)

            if early_score < 3:
                continue

            tier_label = "EARLY"
            tier_emoji = "🔵"
            confidence_pct = get_confidence_pct(active_signals, early_score, confirmation_score)
            concentration, concentration_label = check_wallet_concentration(token_address)
            if concentration and concentration > 60:
                continue
            rug_label, rug_flags, rug_score = get_rug_risk(pair, "pumpfun_early")
            if rug_score >= 4:
                continue

            social_links = ""
            if pair.get("twitter"):
                social_links += f"[Twitter]({pair['twitter']}) "
            if pair.get("telegram"):
                social_links += f"[TG]({pair['telegram']}) "
            if pair.get("website"):
                social_links += f"[Web]({pair['website']})"

            msg = format_alert(
                token_name, token_symbol, token_address, mcap, liquidity, volume,
                buys_5m, sells_5m, 0, 0, 0, age_hours,
                early_score, confirmation_score, tier_label, tier_emoji, confidence_pct,
                active_signals, reasons, narrative_tags, narrative_score,
                concentration_label, rug_label, rug_flags, dex_url, "Pump.fun Early",
                social_links=social_links, smart_wallet_count=smart_count, smart_wallet_msg=smart_msg
            )
            save_alert_to_db(token_address, token_name, token_symbol, mcap, liquidity, volume,
                             buys_5m, sells_5m, 0, 0, 0,
                             early_score, confirmation_score, active_signals, "pumpfun_early", "EARLY")
            save_tracking_to_db(token_address, token_name, token_symbol, mcap, dex_url,
                                "EARLY", early_score, confirmation_score, active_signals)
            broadcast(msg)
            print(f"Early alert: {token_name} — score {early_score}/10")
        except Exception as e:
            print(f"Early analysis error: {e}")

# ── MAIN SCANNER ──────────────────────────────────────────────

def analyze_and_alert(pairs):
    seen = set()
    for pair in pairs:
        try:
            token_address = pair.get("baseToken", {}).get("address", "")
            if not token_address or token_address in seen or is_blacklisted(token_address):
                continue
            seen.add(token_address)
            if was_alerted(token_address):
                continue

            token_name = pair.get("baseToken", {}).get("name", "Unknown")
            token_symbol = pair.get("baseToken", {}).get("symbol", "?")
            description = pair.get("description", "") or ""

            if is_mayhem_token(token_name, token_symbol, description):
                continue
            if is_based_narrative(token_name, token_symbol, description):
                continue

            mcap = pair.get("marketCap", 0) or 0
            volume_24h = pair.get("volume", {}).get("h24", 0) or 0
            liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
            price_change_1h = pair.get("priceChange", {}).get("h1", 0) or 0
            price_change_5m = pair.get("priceChange", {}).get("m5", 0) or 0
            price_change_24h = pair.get("priceChange", {}).get("h24", 0) or 0
            price_usd = pair.get("priceUsd", "0")
            dex_url = pair.get("url", "")
            source = pair.get("source", "dexscreener")
            txns = pair.get("txns", {})
            buys_1h = txns.get("h1", {}).get("buys", 0)
            sells_1h = txns.get("h1", {}).get("sells", 0)
            buys_5m = txns.get("m5", {}).get("buys", 0)
            sells_5m = txns.get("m5", {}).get("sells", 0)
            buy_vol_5m = pair.get("volume", {}).get("m5", 0) or 0
            sell_vol_5m = buy_vol_5m * 0.4
            age_hours = None
            pair_created = pair.get("pairCreatedAt")
            if pair_created:
                age_hours = (time.time() - pair_created / 1000) / 3600

            if not (MCAP_MIN <= mcap <= MCAP_MAX):
                continue
            if volume_24h < VOLUME_MIN and source != "pumpfun":
                continue
            if liquidity < MIN_LIQUIDITY and source != "pumpfun":
                continue
            if source != "pumpfun" and price_change_1h < MIN_PRICE_CHANGE:
                continue
            if age_hours and age_hours > 48:
                continue
            if age_hours and age_hours < 0.5:
                continue
            if sells_1h > buys_1h * 2 and buys_1h > 0:
                continue

            is_real, _ = is_real_pump(buy_vol_5m, sell_vol_5m, buys_5m, sells_5m, price_change_5m)
            if not is_real:
                continue

            hp, _ = is_honeypot(token_address)
            if hp:
                continue

            active_signals = []
            reasons = []

            if source == "pumpfun":
                active_signals.append("pumpfun_active")
                reasons.append("🌊 Pump.fun active")
            elif "birdeye" in source:
                active_signals.append("birdeye")
                reasons.append("🦅 Birdeye trending")
            else:
                active_signals.append("dexscreener")
                reasons.append("📊 Dexscreener")

            price_hist = [w.get("price", 0) for w in token_buy_windows.get(token_address, [])]
            if price_hist:
                pa, pa_msg = detect_price_acceleration(price_hist)
                if pa:
                    active_signals.append("price_acceleration")
                    reasons.append(pa_msg)

            if price_change_5m >= 20:
                active_signals.append("price_change_5m_20")
                reasons.append(f"⚡ {price_change_5m:.0f}% in 5min")
            elif price_change_5m >= 10:
                active_signals.append("price_change_5m_10")
                reasons.append(f"📈 {price_change_5m:.0f}% in 5min")
            if price_change_1h >= 100:
                active_signals.append("price_change_1h_100")
                reasons.append("🔥 100%+ in 1hr")
            elif price_change_1h >= 50:
                active_signals.append("price_change_1h_50")
                reasons.append("⚡ 50%+ in 1hr")
            elif price_change_1h >= 10:
                active_signals.append("price_change_1h_10")
                reasons.append("📈 10%+ in 1hr")

            buy_hist = [w["buys"] for w in token_buy_windows.get(token_address, [])]
            if buy_hist:
                bpa, bpa_msg = detect_buy_pressure_acceleration(buy_hist)
                if bpa:
                    active_signals.append("buy_pressure_acceleration")
                    reasons.append(bpa_msg)

            if volume_24h > mcap * 0.5 and mcap > 0:
                active_signals.append("high_volume_ratio")
                reasons.append("📊 High vol/mcap")
            if buys_1h > sells_1h * 2 and buys_1h > 0:
                active_signals.append("heavy_buy_pressure")
                reasons.append("💚 Heavy buy pressure")
            if buys_5m >= 10:
                active_signals.append("buys_5m_10")
                reasons.append(f"🔥 {buys_5m} buys/5min")
            elif buys_5m >= 5:
                active_signals.append("buys_5m_5")
                reasons.append(f"👥 {buys_5m} buys/5min")
            if liquidity > 100_000:
                active_signals.append("strong_liquidity")
                reasons.append("💧 Strong liquidity")
            if age_hours and age_hours < 1:
                active_signals.append("very_fresh")
                reasons.append("🆕 <1hr old")
            elif age_hours and age_hours < 6:
                active_signals.append("fresh")
                reasons.append("🆕 <6hrs old")
            if mcap < 100_000:
                active_signals.append("ultra_micro_mcap")
                reasons.append("🎯 Ultra micro mcap")
            elif mcap < 200_000:
                active_signals.append("micro_mcap")
                reasons.append("🎯 Micro mcap")
            elif mcap < 300_000:
                active_signals.append("low_mcap")
                reasons.append("🎯 Low mcap")

            narrative_tags, narrative_score, detected_narratives = get_narrative_tags(description, token_name, token_symbol)
            if narrative_score >= 6:
                active_signals.append("bullish_narrative_strong")
                reasons.append("🧠 Very bullish narrative")
            elif narrative_score >= 3:
                active_signals.append("bullish_narrative")
                reasons.append("🧠 Bullish narrative")

            swarm, unique_w, swarm_msg = detect_wallet_swarm(token_address, buys_5m, buys_1h)
            if swarm:
                active_signals.append("wallet_swarm")
                reasons.append(swarm_msg)
            ladder, ladder_msg = detect_buy_momentum_ladder(token_address, buys_5m, buys_1h)
            if ladder:
                active_signals.append("buy_momentum_ladder")
                reasons.append(ladder_msg)
            disp, disp_msg = detect_holder_dispersion(token_address)
            if disp:
                active_signals.append("holder_dispersion")
                reasons.append(disp_msg)
            accum, accum_msg = detect_silent_accumulation(token_address, buys_5m)
            if accum:
                active_signals.append("silent_accumulation")
                reasons.append(accum_msg)

            current_wallets = get_pool_wallets(token_address, limit=15) if buys_5m >= 5 else set()
            smart_count = 0
            smart_msg_str = ""
            if current_wallets:
                if token_address not in early_wallets_cache:
                    early_wallets_cache[token_address] = current_wallets
                wallet_scores = get_wallet_scores(current_wallets)
                high_score_wallets = {w for w, d in wallet_scores.items() if d["score"] >= 2.0}
                if len(high_score_wallets) >= 2:
                    active_signals.append("cohort_wallet_hit")
                    smart_count = len(high_score_wallets)
                    smart_msg_str = f"🎯 {smart_count} smart wallets entering"
                    reasons.append(smart_msg_str)

            early_score, confirmation_score = calculate_scores(active_signals, source, mcap, detected_narratives)
            tier_label, tier_emoji = get_alert_tier(early_score, confirmation_score)
            if not tier_label:
                continue

            rug_label, rug_flags, rug_score = get_rug_risk(pair, source)
            if source == "pumpfun" and rug_score >= 2:
                continue
            if source != "pumpfun" and rug_score >= 4:
                continue
            concentration, concentration_label = check_wallet_concentration(token_address)
            if concentration and concentration > 50:
                continue

            confidence_pct = get_confidence_pct(active_signals, early_score, confirmation_score)
            source_label = "🌊 Pump.fun" if source == "pumpfun" else "📊 Dexscreener"

            social_links = ""
            if pair.get("twitter"):
                social_links += f"[Twitter]({pair['twitter']}) "
            if pair.get("telegram"):
                social_links += f"[TG]({pair['telegram']}) "
            if pair.get("website"):
                social_links += f"[Web]({pair['website']})"

            msg = format_alert(
                token_name, token_symbol, token_address, mcap, liquidity, volume_24h,
                buys_1h, sells_1h, price_change_5m, price_change_1h, price_change_24h,
                age_hours, early_score, confirmation_score, tier_label, tier_emoji,
                confidence_pct, active_signals, reasons, narrative_tags, narrative_score,
                concentration_label, rug_label, rug_flags, dex_url, source_label,
                social_links=social_links, smart_wallet_count=smart_count, smart_wallet_msg=smart_msg_str
            )
            save_alert_to_db(token_address, token_name, token_symbol, mcap, liquidity, volume_24h,
                             buys_1h, sells_1h, price_change_5m, price_change_1h, price_change_24h,
                             early_score, confirmation_score, active_signals, source, tier_label)
            save_tracking_to_db(token_address, token_name, token_symbol, mcap, dex_url,
                                tier_label, early_score, confirmation_score, active_signals)
            broadcast(msg)
            print(f"Alerted: {token_name} — {tier_label} — E:{early_score} C:{confirmation_score}")
        except Exception as e:
            print(f"Analysis error: {e}")

# ── COMMANDS ──────────────────────────────────────────────────

def generate_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def handle_commands():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 2}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        updates = data.get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            message = update.get("message", {})
            if not message:
                continue
            text = message.get("text", "").strip()
            chat_id = str(message.get("chat", {}).get("id", ""))
            chat_type = message.get("chat", {}).get("type", "")
            username = message.get("from", {}).get("username", "Unknown")
            if not text or not chat_id:
                continue

            if chat_id == str(OWNER_CHAT_ID):
                if text == "/gencode":
                    code = generate_code()
                    access_codes[code] = {"created_at": time.time(), "used": False}
                    send_telegram(f"🔑 *New Access Code:*\n\n`{code}`\n\nThey type `/activate {code}` in their group.", OWNER_CHAT_ID)
                elif text == "/listgroups":
                    if not active_groups:
                        send_telegram("📋 No active groups yet.", OWNER_CHAT_ID)
                    else:
                        msg = "📋 *Active Groups:*\n\n"
                        for gid, info in active_groups.items():
                            msg += f"• {info.get('name', 'Unknown')} (`{gid}`)\n"
                        send_telegram(msg, OWNER_CHAT_ID)
                elif text.startswith("/revoke "):
                    group_id = text.split(" ", 1)[1].strip()
                    if group_id in active_groups:
                        name = active_groups[group_id].get("name", "Unknown")
                        del active_groups[group_id]
                        send_telegram(f"✅ Revoked: *{name}*", OWNER_CHAT_ID)
                        send_telegram("⛔ Your access has been revoked.", group_id)
                    else:
                        send_telegram("❌ Group ID not found.", OWNER_CHAT_ID)
                elif text == "/status":
                    try:
                        conn = get_db()
                        cur = conn.cursor()
                        cur.execute("SELECT COUNT(*) FROM alerts")
                        total_alerts = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM outcomes WHERE final_classification IN ('2x','5x','10x')")
                        total_wins = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM wallets WHERE runner_trades >= %s", (COHORT_MIN_HITS,))
                        smart_wallets = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM blacklists")
                        blacklisted = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM tracking WHERE alerted_at > %s", (int(time.time()) - 172800,))
                        tracking_count = cur.fetchone()[0]
                        cur.close()
                        conn.close()
                        wr = (total_wins / total_alerts * 100) if total_alerts > 0 else 0
                        send_telegram(
                            f"📊 *Bot Status — v15*\n\n"
                            f"👥 Active groups: {len(active_groups)}\n"
                            f"👀 Graduation watchlist: {len(graduation_watchlist)}\n"
                            f"📈 Tracking: {tracking_count} coins\n"
                            f"✅ Total alerted: {total_alerts}\n"
                            f"🎯 2x+ win rate: {wr:.1f}%\n"
                            f"🚫 Blacklisted: {blacklisted}\n"
                            f"🎯 Smart wallets: {smart_wallets}\n"
                            f"💾 PostgreSQL active\n\n"
                            f"*Filters active:*\n"
                            f"🚫 Mayhem mode tokens: blocked\n"
                            f"🚫 Based narrative tokens: blocked",
                            OWNER_CHAT_ID
                        )
                    except Exception as e:
                        send_telegram(f"Status error: {e}", OWNER_CHAT_ID)
                elif text == "/report":
                    send_daily_report()
                elif text == "/help":
                    send_telegram(
                        "🤖 *Owner Commands:*\n\n"
                        "/gencode — Generate access code\n"
                        "/listgroups — See active groups\n"
                        "/revoke GROUP\\_ID — Remove group\n"
                        "/status — Bot stats + win rate\n"
                        "/report — Learning report\n"
                        "/help — This message",
                        OWNER_CHAT_ID
                    )

            if chat_type in ["group", "supergroup"]:
                group_name = message.get("chat", {}).get("title", "Unknown Group")
                if text.startswith("/activate "):
                    code = text.split(" ", 1)[1].strip().upper()
                    if chat_id in active_groups:
                        send_telegram("✅ Already activated!", chat_id)
                    elif code in access_codes and not access_codes[code]["used"]:
                        access_codes[code]["used"] = True
                        active_groups[chat_id] = {"name": group_name, "activated_at": time.time(), "activated_by": username}
                        send_telegram("✅ *Meme Radar Signal activated!*\n\nAlerts incoming. 🚀", chat_id)
                        send_telegram(f"✅ *New group:*\n{group_name}\nID: `{chat_id}`\nBy: @{username}", OWNER_CHAT_ID)
                    else:
                        send_telegram("❌ Invalid or used code.", chat_id)
                elif text in ["/start", "/help"]:
                    if chat_id in active_groups:
                        send_telegram("🤖 *Meme Radar Signal v15 active!*\n\nEarly detection + smart money alerts. DYOR.", chat_id)
                    else:
                        send_telegram("👋 *Meme Radar Signal*\n\nNot activated. Get access code then:\n`/activate YOURCODE`", chat_id)
    except Exception as e:
        print(f"Command handler error: {e}")

# ── DAILY REPORT ──────────────────────────────────────────────

def send_daily_report():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT COUNT(*) as total FROM alerts")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as wins FROM outcomes WHERE final_classification IN ('2x','5x','10x')")
        wins_2x = cur.fetchone()["wins"]
        cur.execute("SELECT COUNT(*) as wins FROM outcomes WHERE final_classification IN ('5x','10x')")
        wins_5x = cur.fetchone()["wins"]
        cur.execute("SELECT COUNT(*) as rugs FROM outcomes WHERE final_classification = 'rug'")
        rugs = cur.fetchone()["rugs"]
        cur.execute("SELECT AVG(time_to_2x) as avg FROM outcomes WHERE time_to_2x IS NOT NULL")
        avg_2x = cur.fetchone()["avg"]
        cur.execute("SELECT signal_name, weight, success_rate FROM signal_weights ORDER BY weight DESC LIMIT 5")
        top_signals = cur.fetchall()
        cur.execute("SELECT COUNT(*) as cnt FROM wallets WHERE runner_trades >= %s", (COHORT_MIN_HITS,))
        smart_count = cur.fetchone()["cnt"]
        cur.execute("SELECT alert_tier, COUNT(*) as cnt FROM alerts GROUP BY alert_tier")
        tiers = cur.fetchall()
        cur.close()
        conn.close()

        wr_2x = (wins_2x / total * 100) if total > 0 else 0
        wr_5x = (wins_5x / total * 100) if total > 0 else 0
        rug_rate = (rugs / total * 100) if total > 0 else 0
        avg_2x_str = f"{avg_2x:.0f}min" if avg_2x else "N/A"
        tier_lines = "\n".join([f"• {t['alert_tier']}: {t['cnt']} alerts" for t in tiers])
        signal_lines = "\n".join([f"• {s['signal_name']}: {s['weight']:.2f} ({s['success_rate']*100:.0f}%)" for s in top_signals])

        msg = (
            f"📊 *DAILY REPORT — v15*\n"
            f"🗓 {time.strftime('%Y-%m-%d')}\n\n"
            f"*Performance:*\n"
            f"📈 Total: {total} alerts\n"
            f"✅ 2x+ rate: {wr_2x:.1f}%\n"
            f"🚀 5x+ rate: {wr_5x:.1f}%\n"
            f"💀 Rug rate: {rug_rate:.1f}%\n"
            f"⏱ Avg time to 2x: {avg_2x_str}\n\n"
            f"*By Tier:*\n{tier_lines if tier_lines else 'No data'}\n\n"
            f"*Top Signals:*\n{signal_lines if signal_lines else 'Building...'}\n\n"
            f"*Wallet Intelligence:*\n"
            f"🎯 Smart wallets: {smart_count}\n\n"
            f"*Filters:*\n"
            f"🚫 Mayhem mode: active\n"
            f"🚫 Based narrative: active\n\n"
            f"💾 All data in PostgreSQL"
        )
        send_telegram(msg, OWNER_CHAT_ID)
        print("Daily report sent")
    except Exception as e:
        print(f"Daily report error: {e}")
        send_telegram(f"Report error: {e}", OWNER_CHAT_ID)

# ── MAIN LOOP ─────────────────────────────────────────────────

def main():
    global last_fast_scan, last_medium_scan, last_slow_scan, last_daily_report

    print("Starting Meme Radar Signal v15...")
    init_db()
    load_state_from_db()
    print("State restored from database")

    send_telegram(
        "🤖 *Meme Radar Signal v15 is LIVE!*\n\n"
        "💾 *Persistent Memory:*\n"
        "✅ PostgreSQL active\n"
        "✅ Signal weights restored\n"
        "✅ Wallet intelligence restored\n\n"
        "🔵 *Early Detection Engine:*\n"
        "✅ $5k–$80k early tier\n"
        "✅ Bonding curve fast-fill\n"
        "✅ Wallet burst + velocity spike\n"
        "✅ Smart wallet early entry\n\n"
        "🚫 *New Filters:*\n"
        "✅ Mayhem mode tokens: blocked\n"
        "✅ Based narrative tokens: blocked\n\n"
        "📡 All 6 sources active\n"
        "⚡ Scan: 30s / 60s / 3min\n\n"
        "Let's catch runners early! 🎯"
    )

    while True:
        now = time.time()
        handle_commands()

        if now - last_fast_scan >= FAST_INTERVAL:
            last_fast_scan = now
            print("Fast scan")
            fetch_pumpfun_graduated()
            monitor_graduation_watchlist()
            check_milestones()

        if now - last_medium_scan >= MEDIUM_INTERVAL:
            last_medium_scan = now
            print("Medium scan")
            pairs, early_pairs = fetch_pumpfun_active()
            pairs.extend(fetch_dexscreener_new_pairs())
            pairs.extend(fetch_dexscreener_trending())
            if early_pairs:
                analyze_early(early_pairs)
            if pairs:
                analyze_and_alert(pairs)

        if now - last_slow_scan >= SLOW_INTERVAL:
            last_slow_scan = now
            print("Slow scan")
            pairs = []
            pairs.extend(fetch_dexscreener_gainers())
            pairs.extend(fetch_birdeye_trending())
            if pairs:
                analyze_and_alert(pairs)

        if now - last_daily_report >= DAILY_REPORT_INTERVAL:
            last_daily_report = now
            send_daily_report()

        time.sleep(5)

if __name__ == "__main__":
    main()
