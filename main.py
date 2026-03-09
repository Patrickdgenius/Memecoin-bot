import os
import time
import random
import string
import json
import requests
from collections import defaultdict

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OWNER_CHAT_ID = TELEGRAM_CHAT_ID

MCAP_MIN = 30_000
MCAP_MAX = 3_000_000
VOLUME_MIN = 20_000
MIN_LIQUIDITY = 5_000
MIN_PRICE_CHANGE = 10
GRADUATION_MCAP_MIN = 25_000
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

alerted_tokens = {}
tracking_list = {}
graduation_watchlist = {}
rug_blacklist = set()
honeypot_blacklist = set()
active_groups = {}
access_codes = {}
last_update_id = 0

last_fast_scan = 0
last_medium_scan = 0
last_slow_scan = 0
last_daily_report = 0
FAST_INTERVAL = 30
MEDIUM_INTERVAL = 60
SLOW_INTERVAL = 180
DAILY_REPORT_INTERVAL = 86400

MILESTONES = [50, 100, 200, 300, 500, 1000]

# ── LEARNING SYSTEM ───────────────────────────────────────────

learning_data = {
    "signal_weights": {
        "pumpfun_active": 1.0,
        "pumpfun_graduated": 3.0,
        "dexscreener": 1.0,
        "birdeye": 1.0,
        "price_change_5m_20": 3.0,
        "price_change_5m_10": 2.0,
        "price_change_1h_100": 3.0,
        "price_change_1h_50": 2.0,
        "price_change_1h_10": 1.0,
        "high_volume_ratio": 2.0,
        "heavy_buy_pressure": 2.0,
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
        # New v14 signals
        "wallet_swarm": 3.0,
        "buy_momentum_ladder": 3.0,
        "holder_dispersion": 2.0,
        "silent_accumulation": 2.0,
        "cohort_wallet_hit": 4.0,
        "wallet_network_hit": 5.0,
    },
    "source_performance": {
        "pumpfun": {"alerts": 0, "wins_2x": 0, "wins_5x": 0, "rugs": 0},
        "dexscreener": {"alerts": 0, "wins_2x": 0, "wins_5x": 0, "rugs": 0},
        "birdeye": {"alerts": 0, "wins_2x": 0, "wins_5x": 0, "rugs": 0},
    },
    "narrative_performance": defaultdict(lambda: {"alerts": 0, "wins_2x": 0, "wins_5x": 0, "rugs": 0}),
    "mcap_range_performance": {
        "under_100k": {"alerts": 0, "wins_2x": 0, "wins_5x": 0, "rugs": 0},
        "100k_200k": {"alerts": 0, "wins_2x": 0, "wins_5x": 0, "rugs": 0},
        "200k_300k": {"alerts": 0, "wins_2x": 0, "wins_5x": 0, "rugs": 0},
    },
    "total_alerts": 0,
    "total_wins_2x": 0,
    "total_wins_5x": 0,
    "total_rugs": 0,
    "last_updated": time.time()
}

alert_history = {}

# ── WALLET INTELLIGENCE SYSTEM ────────────────────────────────

# Stores buy history per token: {token_address: [{wallets: set, buys: int, timestamp: float}]}
token_buy_windows = defaultdict(list)

# Cohort system: wallets that appeared early in successful runners
cohort_wallets = {}  # {wallet: {"hits": int, "runner_tokens": [], "last_seen": float}}

# Network clusters: groups of wallets that appear together
wallet_networks = []  # [{"wallets": set, "hits": int, "last_active": float}]

# Holder count history per token for dispersion detection
holder_history = defaultdict(list)  # {token: [(timestamp, holder_count)]}

RUNNER_MCAP_THRESHOLD = 300_000  # mcap that defines a "runner"
COHORT_MIN_HITS = 3              # wallet must appear in 3+ runners to be "high confidence"
NETWORK_MIN_SIZE = 3             # min wallets to form a network cluster
NETWORK_MIN_HITS = 2             # min times cluster appeared together in runners

def get_pool_wallets(token_address, limit=20):
    """Get recent unique buyer wallets from token pool via free RPC"""
    wallets = set()
    try:
        # Step 1: get token accounts / pool address from dexscreener
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        pairs = r.json().get("pairs", [])
        if not pairs:
            return wallets
        pair_address = pairs[0].get("pairAddress", "")
        if not pair_address:
            return wallets

        # Step 2: get recent signatures for the pool (free RPC)
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [pair_address, {"limit": limit}]
        }
        r2 = requests.post(SOLANA_RPC, json=payload, timeout=10)
        sigs = r2.json().get("result", [])

        # Step 3: extract fee payers (buyers) from each transaction
        for sig_info in sigs[:10]:  # limit to 10 to avoid rate limiting
            try:
                sig = sig_info.get("signature", "")
                if not sig:
                    continue
                payload2 = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}]
                }
                r3 = requests.post(SOLANA_RPC, json=payload2, timeout=8)
                tx = r3.json().get("result", {})
                if tx:
                    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    if account_keys:
                        wallets.add(account_keys[0])  # fee payer = buyer
                time.sleep(0.15)
            except:
                continue
    except Exception as e:
        print(f"Wallet fetch error: {e}")
    return wallets

def record_window_activity(token_address, buys, wallets_set, timestamp=None):
    """Record a buy window snapshot for momentum/swarm analysis"""
    if timestamp is None:
        timestamp = time.time()
    windows = token_buy_windows[token_address]
    windows.append({"buys": buys, "wallets": wallets_set, "timestamp": timestamp})
    # Keep only last 10 windows
    if len(windows) > 10:
        token_buy_windows[token_address] = windows[-10:]

def detect_wallet_swarm(token_address, buys_5m, buys_1h):
    """Detect if many unique wallets are buying in a short window — organic distribution signal"""
    try:
        if buys_5m < 8:
            return False, 0, "Not enough buys"
        wallets = get_pool_wallets(token_address, limit=15)
        unique_count = len(wallets)
        record_window_activity(token_address, buys_5m, wallets)
        # Swarm = many unique wallets + average buy size is reasonable
        if unique_count >= 6 and buys_5m >= 8:
            return True, unique_count, f"🐝 {unique_count} unique wallets in 5min"
        return False, unique_count, "Normal activity"
    except Exception as e:
        print(f"Swarm detection error: {e}")
        return False, 0, "Error"

def detect_buy_momentum_ladder(token_address, buys_5m, buys_1h):
    """Detect consistently increasing buy counts across windows — momentum building"""
    windows = token_buy_windows.get(token_address, [])
    if len(windows) < 3:
        # Seed with current data
        record_window_activity(token_address, buys_5m, set())
        return False, "Not enough windows"
    recent = [w["buys"] for w in windows[-4:]]
    # Check if buys are consistently increasing
    increasing = all(recent[i] < recent[i+1] for i in range(len(recent)-1))
    if increasing and recent[-1] >= 5:
        return True, f"📈 Momentum ladder: {' → '.join(str(b) for b in recent)} buys"
    # Also check if last window is significantly higher than first
    if len(recent) >= 3 and recent[-1] > recent[0] * 1.5 and recent[-1] >= 5:
        return True, f"📈 Buy acceleration: {recent[0]} → {recent[-1]} buys"
    return False, "No ladder pattern"

def detect_holder_dispersion(token_address, pair_address=None):
    """Detect rapid holder count growth — strong early distribution signal"""
    try:
        history = holder_history[token_address]
        # Get current holder count via RPC largest accounts as proxy
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]}
        r = requests.post(SOLANA_RPC, json=payload, timeout=10)
        accounts = r.json().get("result", {}).get("value", [])
        holder_count = len([a for a in accounts if float(a.get("uiAmount", 0) or 0) > 0])

        now = time.time()
        history.append((now, holder_count))
        if len(history) > 20:
            holder_history[token_address] = history[-20:]

        if len(history) < 2:
            return False, "Not enough data"

        # Check growth over last 5 minutes
        recent = [(t, h) for t, h in history if now - t <= 300]
        if len(recent) < 2:
            return False, "Not enough recent data"

        oldest_h = recent[0][1]
        newest_h = recent[-1][1]
        if oldest_h > 0:
            growth_pct = ((newest_h - oldest_h) / oldest_h) * 100
            if growth_pct >= 30 and newest_h >= 10:
                return True, f"👥 Holders grew {growth_pct:.0f}% in 5min ({newest_h} holders)"
        return False, "Normal holder growth"
    except Exception as e:
        print(f"Holder dispersion error: {e}")
        return False, "Error"

def detect_silent_accumulation(token_address, buys_5m):
    """Detect slow steady buy increase across 3+ windows — early accumulation"""
    windows = token_buy_windows.get(token_address, [])
    record_window_activity(token_address, buys_5m, set())
    if len(windows) < 3:
        return False, "Not enough windows"
    recent = [w["buys"] for w in windows[-5:]]
    # All windows increasing AND starting from low base = silent accumulation
    if len(recent) >= 3:
        all_increasing = all(recent[i] <= recent[i+1] for i in range(len(recent)-1))
        started_low = recent[0] <= 3
        ended_meaningful = recent[-1] >= 5
        if all_increasing and started_low and ended_meaningful:
            return True, f"🤫 Silent accumulation: {' → '.join(str(b) for b in recent)} buys"
    return False, "No accumulation pattern"

def update_cohort_from_runner(token_address, early_wallets):
    """When a token becomes a runner, record its early wallets as cohort wallets"""
    if not early_wallets:
        return
    for wallet in early_wallets:
        if wallet not in cohort_wallets:
            cohort_wallets[wallet] = {"hits": 0, "runner_tokens": [], "last_seen": time.time()}
        cohort_wallets[wallet]["hits"] += 1
        cohort_wallets[wallet]["runner_tokens"].append(token_address)
        cohort_wallets[wallet]["last_seen"] = time.time()

    # Update wallet networks — find if these wallets cluster with existing networks
    _update_wallet_networks(early_wallets, token_address)
    print(f"Cohort updated: {len(early_wallets)} wallets from runner {token_address[:8]}...")

def _update_wallet_networks(new_wallets, token_address):
    """Update wallet network clusters based on new runner data"""
    if len(new_wallets) < NETWORK_MIN_SIZE:
        return
    wallet_set = set(new_wallets)
    matched = False
    for network in wallet_networks:
        overlap = wallet_set & network["wallets"]
        if len(overlap) >= NETWORK_MIN_SIZE:
            # Merge into existing network
            network["wallets"] |= wallet_set
            network["hits"] += 1
            network["last_active"] = time.time()
            matched = True
            break
    if not matched:
        wallet_networks.append({
            "wallets": wallet_set,
            "hits": 1,
            "last_active": time.time(),
            "tokens": [token_address]
        })

def check_cohort_wallets(token_address, current_wallets):
    """Check if known high-confidence wallets are early buyers of this token"""
    if not current_wallets or not cohort_wallets:
        return False, 0, [], "No cohort data yet"
    high_confidence = {w for w, d in cohort_wallets.items() if d["hits"] >= COHORT_MIN_HITS}
    if not high_confidence:
        return False, 0, [], "Building cohort data"
    matches = current_wallets & high_confidence
    match_count = len(matches)
    if match_count >= 2:
        top_wallets = [f"{w[:4]}...{w[-4:]}" for w in list(matches)[:3]]
        return True, match_count, top_wallets, f"🎯 {match_count} known smart wallets buying early"
    elif match_count == 1:
        return False, match_count, [], "1 cohort wallet (need 2+)"
    return False, 0, [], "No cohort wallets detected"

def check_wallet_networks(current_wallets):
    """Check if a known wallet network cluster is entering this token"""
    if not current_wallets or not wallet_networks:
        return False, 0, "No network data yet"
    strong_networks = [n for n in wallet_networks if n["hits"] >= NETWORK_HITS_MIN]
    for network in strong_networks:
        overlap = current_wallets & network["wallets"]
        if len(overlap) >= NETWORK_MIN_SIZE:
            return True, len(overlap), f"🕸️ {len(overlap)} wallets from known network entering"
    return False, 0, "No network match"

NETWORK_HITS_MIN = NETWORK_MIN_HITS

def check_runner_and_update_cohort(token_address, current_mcap, early_wallets_cache):
    """Called during milestone tracking — if token hits runner threshold, update cohort"""
    if current_mcap >= RUNNER_MCAP_THRESHOLD and token_address in early_wallets_cache:
        update_cohort_from_runner(token_address, early_wallets_cache[token_address])
        del early_wallets_cache[token_address]

# Cache for early wallets before we know if token is a runner
early_wallets_cache = {}

def get_mcap_range(mcap):
    if mcap < 100_000:
        return "under_100k"
    elif mcap < 200_000:
        return "100k_200k"
    else:
        return "200k_300k"

def record_alert(token_address, token_name, token_symbol, mcap, source, signals, narratives, alert_mcap):
    alert_history[token_address] = {
        "name": token_name,
        "symbol": token_symbol,
        "alert_mcap": alert_mcap,
        "source": source,
        "signals": signals,
        "narratives": narratives,
        "mcap_range": get_mcap_range(mcap),
        "alerted_at": time.time(),
        "outcomes_checked": [],
        "peak_multiplier": 1.0,
        "outcome": None
    }
    learning_data["total_alerts"] += 1
    mcap_range = get_mcap_range(mcap)
    if mcap_range in learning_data["mcap_range_performance"]:
        learning_data["mcap_range_performance"][mcap_range]["alerts"] += 1
    source_key = "pumpfun" if "pumpfun" in source else "birdeye" if "birdeye" in source else "dexscreener"
    if source_key in learning_data["source_performance"]:
        learning_data["source_performance"][source_key]["alerts"] += 1
    for narrative in narratives:
        learning_data["narrative_performance"][narrative]["alerts"] += 1

def update_learning(token_address, current_mcap, hours_since):
    if token_address not in alert_history:
        return
    info = alert_history[token_address]
    alert_mcap = info["alert_mcap"]
    if alert_mcap <= 0 or current_mcap <= 0:
        return
    multiplier = current_mcap / alert_mcap
    info["peak_multiplier"] = max(info["peak_multiplier"], multiplier)

    # Update cohort if this token became a runner
    check_runner_and_update_cohort(token_address, current_mcap, early_wallets_cache)

    if hours_since >= 24 and info["outcome"] is None:
        peak = info["peak_multiplier"]
        if peak >= 10:
            outcome = "10x"
        elif peak >= 5:
            outcome = "5x"
        elif peak >= 2:
            outcome = "2x"
        elif peak <= 0.3:
            outcome = "rug"
        elif peak >= 1.3:
            outcome = "small_win"
        else:
            outcome = "flat"
        info["outcome"] = outcome
        source = info["source"]
        source_key = "pumpfun" if "pumpfun" in source else "birdeye" if "birdeye" in source else "dexscreener"
        mcap_range = info["mcap_range"]
        if source_key in learning_data["source_performance"]:
            if outcome in ["2x", "5x", "10x"]:
                learning_data["source_performance"][source_key]["wins_2x"] += 1
                learning_data["total_wins_2x"] += 1
            if outcome in ["5x", "10x"]:
                learning_data["source_performance"][source_key]["wins_5x"] += 1
                learning_data["total_wins_5x"] += 1
            if outcome == "rug":
                learning_data["source_performance"][source_key]["rugs"] += 1
                learning_data["total_rugs"] += 1
        if mcap_range in learning_data["mcap_range_performance"]:
            if outcome in ["2x", "5x", "10x"]:
                learning_data["mcap_range_performance"][mcap_range]["wins_2x"] += 1
            if outcome == "rug":
                learning_data["mcap_range_performance"][mcap_range]["rugs"] += 1
        for narrative in info["narratives"]:
            learning_data["narrative_performance"][narrative]["alerts"] = max(1, learning_data["narrative_performance"][narrative]["alerts"])
            if outcome in ["2x", "5x", "10x"]:
                learning_data["narrative_performance"][narrative]["wins_2x"] += 1
            if outcome == "rug":
                learning_data["narrative_performance"][narrative]["rugs"] += 1
        adjust_signal_weights(info["signals"], outcome)
        learning_data["last_updated"] = time.time()
        print(f"Learning updated: {info['name']} — outcome: {outcome} — peak: {peak:.1f}x")

def adjust_signal_weights(signals, outcome):
    for signal in signals:
        if signal not in learning_data["signal_weights"]:
            continue
        current_weight = learning_data["signal_weights"][signal]
        if outcome in ["5x", "10x"]:
            learning_data["signal_weights"][signal] = min(5.0, current_weight * 1.05)
        elif outcome == "2x":
            learning_data["signal_weights"][signal] = min(5.0, current_weight * 1.02)
        elif outcome == "rug":
            learning_data["signal_weights"][signal] = max(0.1, current_weight * 0.90)
        elif outcome == "flat":
            learning_data["signal_weights"][signal] = max(0.1, current_weight * 0.98)

def get_learned_score(signals, source, mcap, narratives):
    score = 0.0
    source_key = "pumpfun" if "pumpfun" in source else "birdeye" if "birdeye" in source else "dexscreener"
    src_perf = learning_data["source_performance"].get(source_key, {})
    src_alerts = src_perf.get("alerts", 0)
    if src_alerts >= 10:
        src_win_rate = src_perf.get("wins_2x", 0) / src_alerts
        if src_win_rate > 0.5:
            score += 2.0
        elif src_win_rate > 0.3:
            score += 1.0
        elif src_win_rate < 0.1:
            score -= 1.0
    for signal in signals:
        weight = learning_data["signal_weights"].get(signal, 1.0)
        score += weight
    for narrative in narratives:
        narr_perf = learning_data["narrative_performance"].get(narrative, {})
        narr_alerts = narr_perf.get("alerts", 0)
        if narr_alerts >= 5:
            narr_win_rate = narr_perf.get("wins_2x", 0) / narr_alerts
            if narr_win_rate > 0.5:
                score += 1.5
            elif narr_win_rate > 0.3:
                score += 0.5
    mcap_range = get_mcap_range(mcap)
    mcap_perf = learning_data["mcap_range_performance"].get(mcap_range, {})
    mcap_alerts = mcap_perf.get("alerts", 0)
    if mcap_alerts >= 10:
        mcap_win_rate = mcap_perf.get("wins_2x", 0) / mcap_alerts
        if mcap_win_rate > 0.5:
            score += 1.0
        elif mcap_win_rate < 0.2:
            score -= 1.0
    return score

def send_daily_report():
    total = learning_data["total_alerts"]
    wins_2x = learning_data["total_wins_2x"]
    wins_5x = learning_data["total_wins_5x"]
    rugs = learning_data["total_rugs"]
    win_rate_2x = (wins_2x / total * 100) if total > 0 else 0
    win_rate_5x = (wins_5x / total * 100) if total > 0 else 0
    rug_rate = (rugs / total * 100) if total > 0 else 0
    best_narratives = []
    for narr, perf in learning_data["narrative_performance"].items():
        if perf["alerts"] >= 3:
            wr = perf["wins_2x"] / perf["alerts"] * 100
            best_narratives.append((narr, wr, perf["alerts"]))
    best_narratives.sort(key=lambda x: x[1], reverse=True)
    source_lines = ""
    for src, perf in learning_data["source_performance"].items():
        if perf["alerts"] > 0:
            wr = perf["wins_2x"] / perf["alerts"] * 100
            source_lines += f"• {src}: {wr:.0f}% win rate ({perf['alerts']} alerts)\n"
    top_signals = sorted(learning_data["signal_weights"].items(), key=lambda x: x[1], reverse=True)[:5]
    signal_lines = "\n".join([f"• {s}: {w:.2f}" for s, w in top_signals])
    narrative_lines = ""
    for narr, wr, alerts in best_narratives[:5]:
        narrative_lines += f"• {narr}: {wr:.0f}% win rate ({alerts} alerts)\n"

    high_conf_wallets = len([w for w, d in cohort_wallets.items() if d["hits"] >= COHORT_MIN_HITS])
    network_count = len([n for n in wallet_networks if n["hits"] >= NETWORK_MIN_HITS])

    msg = (
        f"📊 *DAILY LEARNING REPORT*\n\n"
        f"🗓 {time.strftime('%Y-%m-%d')}\n\n"
        f"*Overall Performance:*\n"
        f"📈 Total alerts: {total}\n"
        f"✅ 2x+ win rate: {win_rate_2x:.1f}%\n"
        f"🚀 5x+ win rate: {win_rate_5x:.1f}%\n"
        f"💀 Rug rate: {rug_rate:.1f}%\n\n"
        f"*Source Performance:*\n{source_lines}\n"
        f"*Top Narratives:*\n{narrative_lines if narrative_lines else 'Not enough data yet'}\n"
        f"*Strongest Signals:*\n{signal_lines}\n\n"
        f"*🧠 Wallet Intelligence:*\n"
        f"🎯 High-confidence wallets: {high_conf_wallets}\n"
        f"🕸️ Known wallet networks: {network_count}\n"
        f"📦 Tokens in cohort tracking: {len(cohort_wallets)}\n\n"
        f"🧠 Bot is continuously learning and adjusting weights based on outcomes."
    )
    send_telegram(msg, OWNER_CHAT_ID)
    print("Daily report sent")

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

# ── HONEYPOT CHECK ────────────────────────────────────────────

def is_honeypot(token_address):
    if token_address in honeypot_blacklist:
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
            honeypot_blacklist.add(token_address)
            return True, f"Honeypot: {reason}"
        if sell_tax > 15:
            honeypot_blacklist.add(token_address)
            return True, f"High sell tax: {sell_tax:.0f}%"
        if buy_tax > 15:
            return True, f"High buy tax: {buy_tax:.0f}%"
        return False, f"Clean — Buy: {buy_tax:.1f}% Sell: {sell_tax:.1f}%"
    except Exception as e:
        print(f"Honeypot error: {e}")
        return False, "Check unavailable"

# ── ACCESS CODE SYSTEM ────────────────────────────────────────

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
                    send_telegram(f"🔑 *New Access Code:*\n\n`{code}`\n\nShare this — they type `/activate {code}` in their group.", OWNER_CHAT_ID)
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
                    total = learning_data["total_alerts"]
                    wr = (learning_data["total_wins_2x"] / total * 100) if total > 0 else 0
                    high_conf = len([w for w, d in cohort_wallets.items() if d["hits"] >= COHORT_MIN_HITS])
                    networks = len([n for n in wallet_networks if n["hits"] >= NETWORK_MIN_HITS])
                    send_telegram(
                        f"📊 *Bot Status*\n\n"
                        f"👥 Active groups: {len(active_groups)}\n"
                        f"👀 Graduation watchlist: {len(graduation_watchlist)}\n"
                        f"📈 Tracking: {len(tracking_list)} coins\n"
                        f"✅ Total alerted: {total}\n"
                        f"🎯 2x+ win rate: {wr:.1f}%\n"
                        f"🚫 Rug blacklist: {len(rug_blacklist)}\n"
                        f"🍯 Honeypot blacklist: {len(honeypot_blacklist)}\n"
                        f"🧠 Learning active — {len(alert_history)} coins tracked\n\n"
                        f"*🕵️ Wallet Intelligence:*\n"
                        f"🎯 High-confidence wallets: {high_conf}\n"
                        f"🕸️ Known wallet networks: {networks}\n"
                        f"📦 Cohort wallet pool: {len(cohort_wallets)}",
                        OWNER_CHAT_ID
                    )
                elif text == "/report":
                    send_daily_report()
                elif text == "/help":
                    send_telegram(
                        "🤖 *Owner Commands:*\n\n"
                        "/gencode — Generate access code\n"
                        "/listgroups — See active groups\n"
                        "/revoke GROUP\\_ID — Remove group\n"
                        "/status — Bot stats + win rate\n"
                        "/report — Get learning report now\n"
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
                        send_telegram("✅ *Meme Radar Signal activated!*\n\nThis group will now receive all memecoin alerts. 🚀", chat_id)
                        send_telegram(f"✅ *New group activated:*\n{group_name}\nID: `{chat_id}`\nBy: @{username}", OWNER_CHAT_ID)
                    else:
                        send_telegram("❌ Invalid or used code. Contact admin.", chat_id)
                elif text in ["/start", "/help"]:
                    if chat_id in active_groups:
                        send_telegram("🤖 *Meme Radar Signal is active!*\n\nReceiving alerts for graduation runners, dip entries, and strong momentum plays.\n\nDYOR. Not financial advice.", chat_id)
                    else:
                        send_telegram("👋 *Meme Radar Signal*\n\nNot activated. Contact admin for access code then type:\n`/activate YOURCODE`", chat_id)
    except Exception as e:
        print(f"Command handler error: {e}")

# ── DATA FETCHING ──────────────────────────────────────────────

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
        for query in ["pump", "sol", "meme", "cat", "dog", "pepe", "ai", "based"]:
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
                if not token_address or token_address in rug_blacklist or token_address in honeypot_blacklist or token_address in alerted_tokens:
                    continue
                mcap = coin.get("usd_market_cap", 0) or 0
                if mcap < GRADUATION_MCAP_MIN or mcap > MCAP_MAX:
                    continue
                if (coin.get("reply_count", 0) or 0) < 3:
                    continue
                if token_address not in graduation_watchlist:
                    graduation_watchlist[token_address] = {
                        "name": coin.get("name", "Unknown"),
                        "symbol": coin.get("symbol", "?"),
                        "description": coin.get("description", "") or "",
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
                        "url": f"https://dexscreener.com/solana/{token_address}"
                    }
                    print(f"Watching: {coin.get('name')} — ${mcap:,.0f}")
            except:
                continue
    except Exception as e:
        print(f"Pump.fun graduated error: {e}")

def fetch_pumpfun_active():
    pairs = []
    try:
        url = "https://frontend-api-v3.pump.fun/coins?limit=50&sort=usd_market_cap&order=desc&includeNsfw=false"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        coins = r.json()
        coin_list = coins if isinstance(coins, list) else coins.get("coins", [])
        for coin in [c for c in coin_list if not c.get("complete")]:
            try:
                mcap = coin.get("usd_market_cap", 0) or 0
                token_address = coin.get("mint", "")
                created_timestamp = coin.get("created_timestamp", 0)
                age_hours = (time.time() - created_timestamp / 1000) / 3600 if created_timestamp else None
                if mcap < MCAP_MIN or mcap > MCAP_MAX:
                    continue
                if age_hours and (age_hours < 0.5 or age_hours > 24):
                    continue
                if (coin.get("reply_count", 0) or 0) < 5:
                    continue
                pairs.append({
                    "baseToken": {"address": token_address, "name": coin.get("name", "Unknown"), "symbol": coin.get("symbol", "?")},
                    "marketCap": mcap,
                    "volume": {"h24": coin.get("volume", 0) or 0},
                    "liquidity": {"usd": (coin.get("virtual_sol_reserves", 0) or 0) * 150},
                    "priceChange": {"h1": 0, "h24": 0, "m5": 0},
                    "priceUsd": str(coin.get("price", 0)),
                    "txns": {"h1": {"buys": 0, "sells": 0}, "h24": {"buys": 0, "sells": 0}, "m5": {"buys": 0, "sells": 0}},
                    "pairCreatedAt": created_timestamp,
                    "url": f"https://pump.fun/{token_address}",
                    "source": "pumpfun",
                    "description": coin.get("description", "") or "",
                    "reply_count": coin.get("reply_count", 0) or 0
                })
            except:
                continue
        print(f"Pump.fun active: {len(pairs)}")
    except Exception as e:
        print(f"Pump.fun active error: {e}")
    return pairs

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
            return concentration, f"🔴 HIGH — top 10 hold {concentration:.0f}%"
        elif concentration > 30:
            return concentration, f"🟡 MEDIUM — top 10 hold {concentration:.0f}%"
        else:
            return concentration, f"🟢 LOW — top 10 hold {concentration:.0f}%"
    except:
        return None, "Unknown"

# ── NARRATIVE DETECTION ───────────────────────────────────────

def get_narrative(description, name, symbol):
    combined = f"{(description or '').lower()} {name.lower()} {symbol.lower()}"
    score = 0
    signals = []
    detected_narratives = []
    bullish = {
        "ai": ("🤖 AI narrative", 3, "ai_narrative"),
        "agent": ("🤖 AI agent", 3, "ai_narrative"),
        "meme": ("😂 Meme narrative", 2, "meme_narrative"),
        "dog": ("🐕 Dog coin", 2, "dog_narrative"),
        "cat": ("🐈 Cat coin", 2, "cat_narrative"),
        "pepe": ("🐸 Pepe", 2, "pepe_narrative"),
        "elon": ("⚡ Elon narrative", 3, "elon_narrative"),
        "trump": ("🇺🇸 Political", 2, "political_narrative"),
        "based": ("🔵 Based", 2, "based_narrative"),
        "community": ("👥 Community", 2, "community_narrative"),
        "viral": ("📱 Viral", 2, "viral_narrative"),
        "fair launch": ("✅ Fair launch", 3, "fair_launch"),
        "renounced": ("✅ Renounced", 3, "fair_launch"),
        "burned": ("🔥 LP burned", 3, "fair_launch"),
        "war": ("⚔️ War narrative", 2, "war_narrative"),
        "gas": ("⛽ Utility", 2, "utility_narrative"),
        "jelly": ("🟡 Fun narrative", 1, "fun_narrative"),
    }
    for keyword, (label, points, narrative_key) in bullish.items():
        if keyword in combined:
            signals.append(label)
            score += points
            if narrative_key not in detected_narratives:
                detected_narratives.append(narrative_key)
    for bad in ["rug", "scam", "fake", "honeypot", "drain"]:
        if bad in combined:
            score -= 5
            signals.append(f"⚠️ '{bad}' in description")
    if score >= 6:
        strength = "🔥 Very bullish narrative"
    elif score >= 3:
        strength = "📈 Bullish narrative"
    elif score > 0:
        strength = "🟡 Neutral narrative"
    else:
        strength = "🔴 Weak narrative"
    return (f"{strength}\n" + "\n".join(signals[:5])) if signals else strength, score, detected_narratives

# ── FAKE PUMP CHECK ───────────────────────────────────────────

def is_real_pump(buy_vol, sell_vol, buys, sells, price_change_5m):
    if buy_vol <= 0 and sell_vol <= 0:
        return True, "No volume data"
    if sell_vol > buy_vol * 2:
        return False, "Sell vol dominates"
    if price_change_5m > 20 and buys < 5:
        return False, "Pump with <5 buyers"
    return True, "Real pump"

# ── MILESTONE + LEARNING TRACKER ─────────────────────────────

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
    to_remove = []
    for token_address, info in list(tracking_list.items()):
        try:
            hours_since = (time.time() - info["alerted_at"]) / 3600
            if hours_since > 48:
                to_remove.append(token_address)
                continue
            current_mcap = get_current_mcap(token_address)
            alert_mcap = info["alert_mcap"]
            if current_mcap <= 0 or alert_mcap <= 0:
                continue
            change_pct = ((current_mcap - alert_mcap) / alert_mcap) * 100
            for checkpoint in [1, 4, 24]:
                if hours_since >= checkpoint and checkpoint not in info.get("learning_checkpoints", []):
                    update_learning(token_address, current_mcap, hours_since)
                    if "learning_checkpoints" not in info:
                        info["learning_checkpoints"] = []
                    info["learning_checkpoints"].append(checkpoint)
            for milestone in MILESTONES:
                if milestone in info["milestones_hit"]:
                    continue
                if change_pct >= milestone:
                    emoji = "🤯" if milestone >= 500 else "🚀🚀🚀" if milestone >= 300 else "🚀🚀" if milestone >= 100 else "🚀"
                    broadcast(
                        f"{emoji} *MILESTONE HIT — +{milestone}%!* {emoji}\n\n"
                        f"*{info['name']}* (${info['symbol']})\n\n"
                        f"💰 Mcap at alert: ${alert_mcap:,.0f}\n"
                        f"💰 Mcap now: ${current_mcap:,.0f}\n"
                        f"📈 Total gain: *+{change_pct:.0f}%*\n"
                        f"⏱ Time since alert: {hours_since:.1f}hrs\n\n"
                        f"📋 CA: `{token_address}`\n"
                        f"🔗 [DexScreener]({info['dex_url']})\n"
                        f"⚡ [Snipe on Trojan](https://t.me/paris_trojanbot?start=snipe_{token_address})"
                    )
                    info["milestones_hit"].append(milestone)
            if all(m in info["milestones_hit"] for m in MILESTONES):
                to_remove.append(token_address)
        except Exception as e:
            print(f"Milestone error: {e}")
    for addr in to_remove:
        if addr in tracking_list:
            del tracking_list[addr]

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
                    honeypot_blacklist.add(token_address)
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
                rug_blacklist.add(token_address)
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
    price_usd = pair.get("priceUsd", "0")
    narrative_summary, _, detected_narratives = get_narrative(info.get("description", ""), name, symbol)
    concentration, concentration_label = check_wallet_concentration(token_address)
    if concentration and concentration > 50:
        return
    alert_signals = ["pumpfun_graduated"]
    if "dip" in alert_type.lower():
        alert_signals.append("dip_entry")
    else:
        alert_signals.append("graduation_runner")
    trojan_link = f"https://t.me/paris_trojanbot?start=snipe_{token_address}"
    raydium_link = f"https://raydium.io/swap/?inputCurrency=SOL&outputCurrency={token_address}"
    social_links = ""
    if info.get("twitter"):
        social_links += f"🐦 [Twitter]({info['twitter']}) "
    if info.get("telegram"):
        social_links += f"✈️ [Telegram]({info['telegram']}) "
    if info.get("website"):
        social_links += f"🌐 [Website]({info['website']})"
    alerted_tokens[token_address] = time.time()
    tracking_list[token_address] = {
        "name": name, "symbol": symbol,
        "alert_mcap": current_mcap, "alerted_at": time.time(),
        "milestones_hit": [], "learning_checkpoints": [], "dex_url": dex_url
    }
    record_alert(token_address, name, symbol, current_mcap, "pumpfun_graduated", alert_signals, detected_narratives, current_mcap)
    broadcast(
        f"🎓🚨 *PUMP.FUN GRADUATION ALERT* 🚨🎓\n\n"
        f"*{name}* (${symbol})\n"
        f"📡 {alert_type}\n\n"
        f"📋 CA: `{token_address}`\n\n"
        f"💰 Migration Mcap: ${graduation_mcap:,.0f}\n"
        f"💰 Current Mcap: ${current_mcap:,.0f}\n"
        f"📈 Since migration: {change_from_graduation:+.1f}%\n"
        f"💵 Price: ${price_usd}\n"
        f"📊 Volume 24hr: ${volume_24h:,.0f}\n"
        f"💧 Liquidity: ${liquidity:,.0f}\n"
        f"🛒 Buys/Sells (1hr): {buys_1h}/{sells_1h}\n"
        f"⚡ Buys last 5min: {buys_5m}\n"
        f"💬 Community replies: {info.get('reply_count', 0)}\n\n"
        f"*🧠 Narrative:*\n{narrative_summary}\n\n"
        f"*👛 Wallet Concentration:*\n{concentration_label}\n\n"
        f"{f'*🔗 Socials:* {social_links}' if social_links else ''}\n\n"
        f"📊 _Tracking: +50% +100% +200% +300% +500% +1000%_\n\n"
        f"🔗 [DexScreener]({dex_url})\n"
        f"⚡ [Snipe on Trojan]({trojan_link})\n"
        f"🔄 [Buy on Raydium]({raydium_link})\n\n"
        f"⚠️ _DYOR. Not financial advice._"
    )
    print(f"Graduation alert: {name} — {alert_type}")

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
        flags.append("🔴 Very low liquidity vs mcap")
    if sells_24h > buys_24h * 1.5 and buys_24h > 0:
        risk_score += 2
        flags.append("🔴 Heavy sell pressure")
    if price_change_24h > 500:
        risk_score += 1
        flags.append("🟡 Extreme 24hr pump")
    if liquidity < 20_000:
        risk_score += 1
        flags.append("🟡 Low liquidity")
    if volume_24h > mcap * 3 and mcap > 0:
        risk_score += 1
        flags.append("🟡 Suspicious volume")
    if buys_1h < 5 and source != "pumpfun":
        risk_score += 1
        flags.append("🟡 Very few buyers")
    if risk_score == 0:
        flags.append("🟢 No major rug flags")
    label = "🔴 HIGH RUG RISK" if risk_score >= 4 else "🟡 MEDIUM RISK" if risk_score >= 2 else "🟢 LOW RISK"
    return label, flags, risk_score

def get_alert_level(score):
    if score >= 9:
        return "🔴 VERY STRONG", "🚨🚨🚨"
    elif score >= 6:
        return "🟠 STRONG", "🚨🚨"
    else:
        return "🟡 WATCH", "🚨"

# ── MAIN SCANNER ──────────────────────────────────────────────

def analyze_and_alert(pairs):
    seen = set()
    for pair in pairs:
        try:
            token_address = pair.get("baseToken", {}).get("address", "")
            if not token_address or token_address in seen or token_address in rug_blacklist or token_address in honeypot_blacklist:
                continue
            seen.add(token_address)
            token_name = pair.get("baseToken", {}).get("name", "Unknown")
            token_symbol = pair.get("baseToken", {}).get("symbol", "?")
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
            description = pair.get("description", "") or ""
            buy_vol_5m = pair.get("volume", {}).get("m5", 0) or 0
            sell_vol_5m = buy_vol_5m * 0.4
            age_hours = None
            pair_created = pair.get("pairCreatedAt")
            if pair_created:
                age_hours = (time.time() - pair_created / 1000) / 3600
            if token_address in alerted_tokens:
                if time.time() - alerted_tokens[token_address] < 86400:
                    continue
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
            is_real, pump_reason = is_real_pump(buy_vol_5m, sell_vol_5m, buys_5m, sells_5m, price_change_5m)
            if not is_real:
                print(f"Fake pump: {token_name} — {pump_reason}")
                continue
            hp, hp_reason = is_honeypot(token_address)
            if hp:
                print(f"Honeypot: {token_name} — {hp_reason}")
                continue

            active_signals = []
            reasons = []

            # ── EXISTING SIGNALS ──
            if source == "pumpfun":
                active_signals.append("pumpfun_active")
                reasons.append("🚀 Active on Pump.fun")
            elif "birdeye" in source:
                active_signals.append("birdeye")
            else:
                active_signals.append("dexscreener")
            if price_change_5m >= 20:
                active_signals.append("price_change_5m_20")
                reasons.append(f"⚡ {price_change_5m:.0f}% in 5 mins!")
            elif price_change_5m >= 10:
                active_signals.append("price_change_5m_10")
                reasons.append(f"📈 {price_change_5m:.0f}% in 5 mins")
            if price_change_1h >= 100:
                active_signals.append("price_change_1h_100")
                reasons.append("🔥 100%+ in 1hr")
            elif price_change_1h >= 50:
                active_signals.append("price_change_1h_50")
                reasons.append("⚡ 50%+ in 1hr")
            elif price_change_1h >= 10:
                active_signals.append("price_change_1h_10")
                reasons.append("📈 10%+ in 1hr")
            if volume_24h > mcap * 0.5 and mcap > 0:
                active_signals.append("high_volume_ratio")
                reasons.append("📊 High volume vs mcap")
            if buys_1h > sells_1h * 2 and buys_1h > 0:
                active_signals.append("heavy_buy_pressure")
                reasons.append("💚 Heavy buy pressure")
            if buys_5m >= 10:
                active_signals.append("buys_5m_10")
                reasons.append(f"🔥 {buys_5m} buys in 5 mins")
            elif buys_5m >= 5:
                active_signals.append("buys_5m_5")
                reasons.append(f"👥 {buys_5m} buys in 5 mins")
            if liquidity > 100_000:
                active_signals.append("strong_liquidity")
                reasons.append("💧 Strong liquidity")
            if age_hours and age_hours < 1:
                active_signals.append("very_fresh")
                reasons.append("🆕 Very fresh (<1hr)")
            elif age_hours and age_hours < 6:
                active_signals.append("fresh")
                reasons.append("🆕 Fresh (<6hrs)")
            if mcap < 100_000:
                active_signals.append("ultra_micro_mcap")
                reasons.append("🎯 Ultra micro mcap")
            elif mcap < 200_000:
                active_signals.append("micro_mcap")
                reasons.append("🎯 Micro mcap")
            elif mcap < 300_000:
                active_signals.append("low_mcap")
                reasons.append("🎯 Low mcap")
            narrative_summary, narrative_score, detected_narratives = get_narrative(description, token_name, token_symbol)
            if narrative_score >= 6:
                active_signals.append("bullish_narrative_strong")
                reasons.append("🧠 Very bullish narrative")
            elif narrative_score >= 3:
                active_signals.append("bullish_narrative")
                reasons.append("🧠 Bullish narrative")

            # ── NEW v14 SIGNALS ──

            # 1. Wallet swarm detection
            swarm_detected, unique_wallets, swarm_msg = detect_wallet_swarm(token_address, buys_5m, buys_1h)
            if swarm_detected:
                active_signals.append("wallet_swarm")
                reasons.append(swarm_msg)

            # 2. Buy momentum ladder
            ladder_detected, ladder_msg = detect_buy_momentum_ladder(token_address, buys_5m, buys_1h)
            if ladder_detected:
                active_signals.append("buy_momentum_ladder")
                reasons.append(ladder_msg)

            # 3. Holder dispersion
            dispersion_detected, dispersion_msg = detect_holder_dispersion(token_address)
            if dispersion_detected:
                active_signals.append("holder_dispersion")
                reasons.append(dispersion_msg)

            # 4. Silent accumulation
            accum_detected, accum_msg = detect_silent_accumulation(token_address, buys_5m)
            if accum_detected:
                active_signals.append("silent_accumulation")
                reasons.append(accum_msg)

            # 5 & 6. Cohort wallet detection
            current_wallets = get_pool_wallets(token_address, limit=15) if swarm_detected or buys_5m >= 5 else set()
            if current_wallets:
                # Cache early wallets for cohort tracking
                if token_address not in early_wallets_cache:
                    early_wallets_cache[token_address] = current_wallets
                cohort_hit, cohort_count, cohort_wallets_list, cohort_msg = check_cohort_wallets(token_address, current_wallets)
                if cohort_hit:
                    active_signals.append("cohort_wallet_hit")
                    reasons.append(cohort_msg)

                # 7. Wallet network detection
                network_hit, network_size, network_msg = check_wallet_networks(current_wallets)
                if network_hit:
                    active_signals.append("wallet_network_hit")
                    reasons.append(network_msg)

            score = get_learned_score(active_signals, source, mcap, detected_narratives)
            if score < 2:
                continue

            rug_label, rug_flags, rug_score = get_rug_risk(pair, source)
            if source == "pumpfun" and rug_score >= 2:
                continue
            if source != "pumpfun" and rug_score >= 4:
                continue
            concentration, concentration_label = check_wallet_concentration(token_address)
            if concentration and concentration > 50:
                continue

            alert_level, alert_emoji = get_alert_level(score)
            trojan_link = f"https://t.me/paris_trojanbot?start=snipe_{token_address}"
            raydium_link = f"https://raydium.io/swap/?inputCurrency=SOL&outputCurrency={token_address}"
            source_label = "🌊 Pump.fun" if source == "pumpfun" else "📊 Dexscreener"
            total = learning_data["total_alerts"]
            wr = (learning_data["total_wins_2x"] / total * 100) if total >= 10 else None
            win_rate_line = f"🎯 Bot win rate: {wr:.0f}% ({total} alerts)\n" if wr else ""

            # Wallet intelligence summary line for alert
            wallet_intel_line = ""
            if "wallet_network_hit" in active_signals:
                wallet_intel_line = "🕸️ *Known wallet network entering!*\n"
            elif "cohort_wallet_hit" in active_signals:
                wallet_intel_line = "🎯 *Smart money wallets detected!*\n"
            elif swarm_detected:
                wallet_intel_line = f"🐝 *Wallet swarm: {unique_wallets} unique buyers*\n"

            alerted_tokens[token_address] = time.time()
            tracking_list[token_address] = {
                "name": token_name, "symbol": token_symbol,
                "alert_mcap": mcap, "alerted_at": time.time(),
                "milestones_hit": [], "learning_checkpoints": [], "dex_url": dex_url
            }
            record_alert(token_address, token_name, token_symbol, mcap, source, active_signals, detected_narratives, mcap)

            broadcast(
                f"{alert_emoji} *MEMECOIN ALERT — {alert_level}* {alert_emoji}\n\n"
                f"*{token_name}* (${token_symbol})\n"
                f"📡 Source: {source_label}\n\n"
                f"📋 CA: `{token_address}`\n\n"
                f"💰 Market Cap: ${mcap:,.0f}\n"
                f"📈 5min: {price_change_5m:.1f}% | 1hr: {price_change_1h:.1f}% | 24hr: {price_change_24h:.1f}%\n"
                f"💵 Price: ${price_usd}\n"
                f"📊 Volume 24hr: ${volume_24h:,.0f}\n"
                f"💧 Liquidity: ${liquidity:,.0f}\n"
                f"🛒 Buys/Sells (1hr): {buys_1h}/{sells_1h}\n"
                f"⚡ Buys last 5min: {buys_5m}\n"
                f"⏰ Age: {f'{age_hours:.1f}hrs' if age_hours else 'Unknown'}\n\n"
                f"*🧠 Narrative:*\n{narrative_summary}\n\n"
                f"*👛 Wallet Concentration:*\n{concentration_label}\n\n"
                f"{wallet_intel_line}"
                f"*Why flagged:*\n" + "\n".join(reasons) + f"\n\n"
                f"*Rug Risk: {rug_label}*\n" + "\n".join(rug_flags) + f"\n\n"
                f"{win_rate_line}"
                f"📊 _Tracking: +50% +100% +200% +300% +500% +1000%_\n\n"
                f"🔗 [DexScreener]({dex_url})\n"
                f"⚡ [Snipe on Trojan]({trojan_link})\n"
                f"🔄 [Buy on Raydium]({raydium_link})\n\n"
                f"⚠️ _DYOR. Not financial advice._"
            )
            print(f"Alerted: {token_name} — {alert_level} — Score: {score:.1f}")
        except Exception as e:
            print(f"Analysis error: {e}")

# ── MAIN LOOP ─────────────────────────────────────────────────

def main():
    global last_fast_scan, last_medium_scan, last_slow_scan, last_daily_report
    print("🤖 Memecoin Scanner Bot v14 started...")
    send_telegram(
        "🤖 *Memecoin Scanner Bot v14 is now LIVE!*\n\n"
        "📡 *Sources:*\n"
        "✅ Dexscreener New Pairs\n"
        "✅ Dexscreener Trending\n"
        "✅ Dexscreener Gainers\n"
        "✅ Pump.fun Graduated (watch mode)\n"
        "✅ Pump.fun Active\n"
        "✅ Birdeye Trending\n\n"
        "🧠 *Learning System Active:*\n"
        "✅ Tracks every alert outcome\n"
        "✅ Adjusts signal weights from results\n"
        "✅ Tracks win rate per source + narrative\n"
        "✅ Daily report — use /report anytime\n\n"
        "🕵️ *New Runner Detection (v14):*\n"
        "✅ Micro wallet swarm detection\n"
        "✅ Buy momentum ladder detection\n"
        "✅ Early holder dispersion tracking\n"
        "✅ Silent accumulation detection\n"
        "✅ Cohort wallet tracking (builds over time)\n"
        "✅ Wallet network cluster detection\n\n"
        "⚡ Scan timing: 30s / 60s / 3min\n"
        "Owner commands: /gencode /listgroups /status /report /help\n\n"
        "Let's catch runners! 🎯"
    )
    while True:
        now = time.time()
        handle_commands()
        if now - last_fast_scan >= FAST_INTERVAL:
            last_fast_scan = now
            print("⚡ Fast scan")
            fetch_pumpfun_graduated()
            monitor_graduation_watchlist()
            check_milestones()
        if now - last_medium_scan >= MEDIUM_INTERVAL:
            last_medium_scan = now
            print("🔍 Medium scan")
            pairs = []
            pairs.extend(fetch_dexscreener_new_pairs())
            pairs.extend(fetch_dexscreener_trending())
            pairs.extend(fetch_pumpfun_active())
            if pairs:
                analyze_and_alert(pairs)
        if now - last_slow_scan >= SLOW_INTERVAL:
            last_slow_scan = now
            print("🌐 Slow scan")
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
