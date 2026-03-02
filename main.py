import os
import time
import requests
import re
from collections import defaultdict

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MCAP_MIN = 40_000
MCAP_MAX = 300_000
VOLUME_MIN = 20_000
MIN_LIQUIDITY = 10_000
MIN_PRICE_CHANGE = 10
ALERT_INTERVAL = 60
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

alerted_tokens = {}
tracking_list = {}
graduation_watchlist = {}
rug_blacklist = set()

MILESTONES = [50, 100, 200, 300, 500, 1000]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ── DATA FETCHING ──────────────────────────────────────────────

def fetch_dexscreener_new_pairs():
    pairs = []
    try:
        url = "https://api.dexscreener.com/latest/dex/search?q=solana"
        r = requests.get(url, timeout=10)
        data = r.json()
        pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
        print(f"Dexscreener new pairs: {len(pairs)} fetched")
    except Exception as e:
        print(f"Dexscreener new pairs error: {e}")
    return pairs

def fetch_dexscreener_trending():
    pairs = []
    try:
        url = "https://api.dexscreener.com/token-boosts/top/v1"
        r = requests.get(url, timeout=10)
        data = r.json()
        solana_tokens = [t for t in (data if isinstance(data, list) else data.get("pairs", [])) if t.get("chainId") == "solana"]
        for addr in [t.get("tokenAddress") for t in solana_tokens if t.get("tokenAddress")][:20]:
            try:
                r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=10)
                d2 = r2.json()
                if isinstance(d2, dict):
                    pairs.extend(d2.get("pairs", []))
                time.sleep(0.2)
            except:
                continue
        print(f"Dexscreener trending: {len(pairs)} fetched")
    except Exception as e:
        print(f"Dexscreener trending error: {e}")
    return pairs

def fetch_dexscreener_gainers():
    pairs = []
    try:
        for query in ["pump", "sol", "meme", "cat", "dog", "pepe", "ai", "based"]:
            try:
                r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={query}", timeout=10)
                data = r.json()
                pairs.extend([p for p in data.get("pairs", []) if p.get("chainId") == "solana"])
                time.sleep(0.2)
            except:
                continue
        print(f"Dexscreener gainers: {len(pairs)} fetched")
    except Exception as e:
        print(f"Dexscreener gainers error: {e}")
    return pairs

def fetch_pumpfun_graduated():
    """Fetch recently graduated Pump.fun tokens — highest quality signal"""
    pairs = []
    try:
        url = "https://frontend-api-v3.pump.fun/coins?limit=50&sort=usd_market_cap&order=desc&includeNsfw=false"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        coins = r.json()
        coin_list = coins if isinstance(coins, list) else coins.get("coins", [])
        graduated = [c for c in coin_list if c.get("complete") == True]
        print(f"Pump.fun graduated: {len(graduated)} found")
        for coin in graduated:
            try:
                token_address = coin.get("mint", "")
                if not token_address or token_address in rug_blacklist:
                    continue
                mcap = coin.get("usd_market_cap", 0) or 0
                name = coin.get("name", "Unknown")
                symbol = coin.get("symbol", "?")
                created_timestamp = coin.get("created_timestamp", 0)
                reply_count = coin.get("reply_count", 0) or 0
                description = coin.get("description", "") or ""
                twitter = coin.get("twitter", "") or ""
                telegram = coin.get("telegram", "") or ""
                website = coin.get("website", "") or ""

                if mcap > 300_000:
                    continue
                if reply_count < 3:
                    continue

                # Add to graduation watchlist if not already there
                if token_address not in graduation_watchlist and token_address not in alerted_tokens:
                    graduation_watchlist[token_address] = {
                        "name": name,
                        "symbol": symbol,
                        "description": description,
                        "twitter": twitter,
                        "telegram": telegram,
                        "website": website,
                        "reply_count": reply_count,
                        "created_timestamp": created_timestamp,
                        "added_at": time.time(),
                        "graduation_mcap": mcap,
                        "price_history": [],
                        "buy_history": [],
                        "sell_history": [],
                        "consecutive_dumps": 0,
                        "dip_detected": False,
                        "dip_low_mcap": None,
                        "alerted": False,
                        "url": f"https://dexscreener.com/solana/{token_address}"
                    }
                    print(f"Added to graduation watchlist: {name} ({symbol}) — ${mcap:,.0f}")
            except:
                continue
    except Exception as e:
        print(f"Pump.fun graduated error: {e}")
    return pairs

def fetch_pumpfun_active():
    """Fetch active non-graduated Pump.fun tokens"""
    pairs = []
    try:
        url = "https://frontend-api-v3.pump.fun/coins?limit=50&sort=usd_market_cap&order=desc&includeNsfw=false"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        coins = r.json()
        coin_list = coins if isinstance(coins, list) else coins.get("coins", [])
        active = [c for c in coin_list if not c.get("complete")]
        print(f"Pump.fun active: {len(active)} found")
        for coin in active:
            try:
                mcap = coin.get("usd_market_cap", 0) or 0
                token_address = coin.get("mint", "")
                name = coin.get("name", "Unknown")
                symbol = coin.get("symbol", "?")
                created_timestamp = coin.get("created_timestamp", 0)
                age_hours = (time.time() - created_timestamp / 1000) / 3600 if created_timestamp else None
                reply_count = coin.get("reply_count", 0) or 0

                if mcap < MCAP_MIN or mcap > MCAP_MAX:
                    continue
                if age_hours and age_hours < 0.5:
                    continue
                if age_hours and age_hours > 24:
                    continue
                if reply_count < 5:
                    continue

                pairs.append({
                    "baseToken": {"address": token_address, "name": name, "symbol": symbol},
                    "marketCap": mcap,
                    "volume": {"h24": coin.get("volume", 0) or 0},
                    "liquidity": {"usd": (coin.get("virtual_sol_reserves", 0) or 0) * 150},
                    "priceChange": {"h1": 0, "h24": 0, "m5": 0},
                    "priceUsd": str(coin.get("price", 0)),
                    "txns": {"h1": {"buys": 0, "sells": 0}, "h24": {"buys": 0, "sells": 0}, "m5": {"buys": 0, "sells": 0}},
                    "pairCreatedAt": created_timestamp,
                    "url": f"https://pump.fun/{token_address}",
                    "source": "pumpfun",
                    "graduated": False,
                    "description": coin.get("description", "") or "",
                    "reply_count": reply_count
                })
            except:
                continue
    except Exception as e:
        print(f"Pump.fun active error: {e}")
    return pairs

def fetch_birdeye_trending():
    pairs = []
    try:
        url = "https://public-api.birdeye.so/defi/token_trending?sort_by=v24hUSD&sort_type=desc&offset=0&limit=20"
        headers = {"X-API-KEY": "public", "x-chain": "solana"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        tokens = data.get("data", {}).get("tokens", [])
        print(f"Birdeye: {len(tokens)} tokens fetched")
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

# ── WALLET CONCENTRATION CHECK ────────────────────────────────

def check_wallet_concentration(token_address):
    """Check top holder concentration via Solana RPC"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [token_address]
        }
        r = requests.post(SOLANA_RPC, json=payload, timeout=10)
        data = r.json()
        accounts = data.get("result", {}).get("value", [])
        if not accounts:
            return None, "Unknown"

        total = sum(float(a.get("uiAmount", 0) or 0) for a in accounts)
        if total == 0:
            return None, "Unknown"

        top10 = sum(float(a.get("uiAmount", 0) or 0) for a in accounts[:10])
        concentration = (top10 / total) * 100

        if concentration > 50:
            return concentration, "🔴 HIGH concentration — top 10 wallets hold {:.0f}%".format(concentration)
        elif concentration > 30:
            return concentration, "🟡 MEDIUM concentration — top 10 wallets hold {:.0f}%".format(concentration)
        else:
            return concentration, "🟢 LOW concentration — top 10 wallets hold {:.0f}%".format(concentration)
    except Exception as e:
        print(f"Wallet concentration error: {e}")
        return None, "Unknown"

# ── NARRATIVE DETECTION ───────────────────────────────────────

def get_narrative(description, name, symbol):
    """Analyze token description and name for bullish narrative signals"""
    if not description:
        return "No description found", 0

    description_lower = description.lower()
    name_lower = name.lower()
    symbol_lower = symbol.lower()
    combined = f"{description_lower} {name_lower} {symbol_lower}"

    narrative_score = 0
    signals = []

    # Trending narrative keywords
    bullish_narratives = {
        "ai": ("🤖 AI narrative", 3),
        "artificial intelligence": ("🤖 AI narrative", 3),
        "agent": ("🤖 AI agent narrative", 3),
        "meme": ("😂 Meme narrative", 2),
        "dog": ("🐕 Dog coin narrative", 2),
        "cat": ("🐈 Cat coin narrative", 2),
        "pepe": ("🐸 Pepe narrative", 2),
        "elon": ("⚡ Elon narrative", 3),
        "trump": ("🇺🇸 Political narrative", 2),
        "based": ("🔵 Based narrative", 2),
        "gas": ("⛽ Utility narrative", 2),
        "solana": ("☀️ Solana ecosystem", 1),
        "moon": ("🌙 Moon narrative", 1),
        "community": ("👥 Community driven", 2),
        "viral": ("📱 Viral narrative", 2),
        "trending": ("📈 Trending narrative", 2),
        "fair launch": ("✅ Fair launch", 3),
        "no team": ("✅ No team tokens", 3),
        "renounced": ("✅ Renounced", 3),
        "burned": ("🔥 Liquidity burned", 3),
    }

    for keyword, (label, points) in bullish_narratives.items():
        if keyword in combined:
            signals.append(label)
            narrative_score += points

    # Bearish signals in description
    bearish_keywords = ["rug", "scam", "fake", "honeypot", "drain"]
    for keyword in bearish_keywords:
        if keyword in combined:
            narrative_score -= 5
            signals.append(f"⚠️ Warning: '{keyword}' found in description")

    if narrative_score >= 6:
        strength = "🔥 Very bullish narrative"
    elif narrative_score >= 3:
        strength = "📈 Bullish narrative"
    elif narrative_score > 0:
        strength = "🟡 Neutral narrative"
    else:
        strength = "🔴 Weak/bearish narrative"

    narrative_summary = f"{strength}\n" + "\n".join(signals[:5]) if signals else strength
    return narrative_summary, narrative_score

# ── GRADUATION WATCHLIST MONITOR ─────────────────────────────

def monitor_graduation_watchlist():
    """Constantly monitor graduated tokens — detect runners, dips, and rugs"""
    to_remove = []

    for token_address, info in list(graduation_watchlist.items()):
        try:
            if info.get("alerted"):
                to_remove.append(token_address)
                continue

            # Stop watching after 6 hours
            hours_watching = (time.time() - info["added_at"]) / 3600
            if hours_watching > 6:
                print(f"Graduation watch expired: {info['name']}")
                to_remove.append(token_address)
                continue

            # Fetch current data
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
            volume_5m = pair.get("volume", {}).get("m5", 0) or 0
            price_change_5m = pair.get("priceChange", {}).get("m5", 0) or 0
            dex_url = pair.get("url", info["url"])
            liquidity = pair.get("liquidity", {}).get("usd", 0) or 0

            if current_mcap <= 0:
                continue

            graduation_mcap = info["graduation_mcap"]

            # Track price history
            info["price_history"].append(current_mcap)
            info["buy_history"].append(buys_5m)
            info["sell_history"].append(sells_5m)

            # Keep last 20 data points
            if len(info["price_history"]) > 20:
                info["price_history"] = info["price_history"][-20:]
                info["buy_history"] = info["buy_history"][-20:]
                info["sell_history"] = info["sell_history"][-20:]

            change_from_graduation = ((current_mcap - graduation_mcap) / graduation_mcap) * 100

            # ── RUG DETECTION ──
            # Consecutive dumps with no recovery = rug
            if price_change_5m < -15 and sells_5m > buys_5m * 3:
                info["consecutive_dumps"] += 1
            else:
                info["consecutive_dumps"] = 0

            if info["consecutive_dumps"] >= 3:
                print(f"RUG detected: {info['name']} — blacklisting")
                rug_blacklist.add(token_address)
                to_remove.append(token_address)
                continue

            # ── STRAIGHT RUNNER DETECTION ──
            # Keeps pumping after graduation with strong buys
            if (change_from_graduation > 30 and
                buys_5m > sells_5m * 1.5 and
                buys_1h > sells_1h and
                current_mcap <= MCAP_MAX):

                alert_type = "🚀 GRADUATION RUNNER"
                _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url)
                info["alerted"] = True
                continue

            # ── DIP DETECTION ──
            if not info["dip_detected"]:
                if change_from_graduation < -10:
                    info["dip_detected"] = True
                    info["dip_low_mcap"] = current_mcap
                    print(f"Dip detected: {info['name']} — watching for recovery")
            else:
                # Update dip low
                if current_mcap < info["dip_low_mcap"]:
                    info["dip_low_mcap"] = current_mcap

                # ── DIP ENTRY DETECTION ──
                # Price recovering from dip + buys coming in strong
                dip_low = info["dip_low_mcap"]
                recovery_pct = ((current_mcap - dip_low) / dip_low) * 100 if dip_low > 0 else 0

                if (recovery_pct > 10 and
                    buys_5m >= 3 and
                    buys_5m > sells_5m and
                    liquidity > MIN_LIQUIDITY):

                    dip_depth = ((graduation_mcap - dip_low) / graduation_mcap) * 100 if graduation_mcap > 0 else 0
                    alert_type = f"💎 DIP ENTRY — dipped {dip_depth:.0f}% then recovering"
                    _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url)
                    info["alerted"] = True
                    continue

        except Exception as e:
            print(f"Graduation monitor error for {token_address}: {e}")
            continue

    for addr in to_remove:
        if addr in graduation_watchlist:
            del graduation_watchlist[addr]

def _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url):
    """Send graduation alert with full analysis"""
    name = info["name"]
    symbol = info["symbol"]
    graduation_mcap = info["graduation_mcap"]
    description = info.get("description", "")
    reply_count = info.get("reply_count", 0)

    # Narrative analysis
    narrative_summary, narrative_score = get_narrative(description, name, symbol)

    # Wallet concentration
    concentration, concentration_label = check_wallet_concentration(token_address)

    txns = pair.get("txns", {})
    buys_1h = txns.get("h1", {}).get("buys", 0)
    sells_1h = txns.get("h1", {}).get("sells", 0)
    buys_5m = txns.get("m5", {}).get("buys", 0)
    volume_24h = pair.get("volume", {}).get("h24", 0) or 0
    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
    price_usd = pair.get("priceUsd", "0")

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
        "name": name,
        "symbol": symbol,
        "alert_mcap": current_mcap,
        "alerted_at": time.time(),
        "milestones_hit": [],
        "dex_url": dex_url
    }

    message = (
        f"🎓🚨 *PUMP.FUN GRADUATION ALERT* 🚨🎓\n\n"
        f"*{name}* (${symbol})\n"
        f"📡 {alert_type}\n\n"
        f"📋 CA: `{token_address}`\n\n"
        f"💰 Graduation Mcap: ${graduation_mcap:,.0f}\n"
        f"💰 Current Mcap: ${current_mcap:,.0f}\n"
        f"📈 Change since graduation: {change_from_graduation:+.1f}%\n"
        f"💵 Price: ${price_usd}\n"
        f"📊 Volume 24hr: ${volume_24h:,.0f}\n"
        f"💧 Liquidity: ${liquidity:,.0f}\n"
        f"🛒 Buys/Sells (1hr): {buys_1h}/{sells_1h}\n"
        f"⚡ Buys last 5min: {buys_5m}\n"
        f"💬 Community replies: {reply_count}\n\n"
        f"*🧠 Narrative Analysis:*\n{narrative_summary}\n\n"
        f"*👛 Wallet Concentration:*\n{concentration_label}\n\n"
        f"{f'*🔗 Socials:* {social_links}' if social_links else ''}\n\n"
        f"📊 _Tracking milestones: +50% +100% +200% +300% +500% +1000%_\n\n"
        f"🔗 [DexScreener]({dex_url})\n"
        f"⚡ [Snipe on Trojan]({trojan_link})\n"
        f"🔄 [Buy on Raydium]({raydium_link})\n\n"
        f"⚠️ _DYOR. Not financial advice._"
    )
    send_telegram(message)
    print(f"Graduation alert: {name} ({symbol}) — {alert_type}")

# ── MILESTONE TRACKING ────────────────────────────────────────

def get_current_mcap(token_address):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        data = r.json()
        pairs = data.get("pairs", [])
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
            milestones_hit = info["milestones_hit"]

            for milestone in MILESTONES:
                if milestone in milestones_hit:
                    continue
                if change_pct >= milestone:
                    emoji = "🤯" if milestone >= 500 else "🚀🚀🚀" if milestone >= 300 else "🚀🚀" if milestone >= 100 else "🚀"
                    send_telegram(
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
                    milestones_hit.append(milestone)
                    print(f"Milestone: {info['name']} hit +{milestone}%!")

            if all(m in milestones_hit for m in MILESTONES):
                to_remove.append(token_address)

        except Exception as e:
            print(f"Milestone error: {e}")

    for addr in to_remove:
        if addr in tracking_list:
            del tracking_list[addr]

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
        flags.append("🟢 No major rug flags detected")
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
            if not token_address or token_address in seen or token_address in rug_blacklist:
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
            description = pair.get("description", "") or ""
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

            score = 0
            reasons = []

            if source == "pumpfun":
                score += 1
                reasons.append("🚀 Active on Pump.fun")

            if price_change_5m >= 20:
                score += 3
                reasons.append(f"⚡ {price_change_5m:.0f}% pump in 5 mins!")
            elif price_change_5m >= 10:
                score += 2
                reasons.append(f"📈 {price_change_5m:.0f}% pump in 5 mins")

            if price_change_1h >= 100:
                score += 3
                reasons.append("🔥 100%+ pump in 1hr")
            elif price_change_1h >= 50:
                score += 2
                reasons.append("⚡ 50%+ pump in 1hr")
            elif price_change_1h >= 10:
                score += 1
                reasons.append("📈 10%+ pump in 1hr")

            if volume_24h > mcap * 0.5 and mcap > 0:
                score += 2
                reasons.append("📊 High volume vs mcap")

            if buys_1h > sells_1h * 2 and buys_1h > 0:
                score += 2
                reasons.append("💚 Heavy buy pressure")
            if buys_5m >= 10:
                score += 2
                reasons.append(f"🔥 {buys_5m} buys in 5 mins")
            elif buys_5m >= 5:
                score += 1
                reasons.append(f"👥 {buys_5m} buys in 5 mins")

            if liquidity > 100_000:
                score += 1
                reasons.append("💧 Strong liquidity")

            if age_hours and age_hours < 1:
                score += 2
                reasons.append("🆕 Very fresh (<1hr)")
            elif age_hours and age_hours < 6:
                score += 1
                reasons.append("🆕 Fresh token (<6hrs)")

            if mcap < 100_000:
                score += 3
                reasons.append("🎯 Ultra micro mcap")
            elif mcap < 200_000:
                score += 2
                reasons.append("🎯 Micro mcap — extremely early")
            elif mcap < 300_000:
                score += 1
                reasons.append("🎯 Low mcap — early entry")

            # Narrative check
            narrative_summary, narrative_score = get_narrative(description, token_name, token_symbol)
            if narrative_score >= 6:
                score += 2
                reasons.append("🧠 Very bullish narrative")
            elif narrative_score >= 3:
                score += 1
                reasons.append("🧠 Bullish narrative detected")

            if score < 2:
                continue

            rug_label, rug_flags, rug_score = get_rug_risk(pair, source)
            if source == "pumpfun" and rug_score >= 2:
                continue
            if source != "pumpfun" and rug_score >= 4:
                continue

            # Wallet concentration check
            concentration, concentration_label = check_wallet_concentration(token_address)
            if concentration and concentration > 50:
                continue  # skip heavily concentrated tokens

            alert_level, alert_emoji = get_alert_level(score)
            trojan_link = f"https://t.me/paris_trojanbot?start=snipe_{token_address}"
            raydium_link = f"https://raydium.io/swap/?inputCurrency=SOL&outputCurrency={token_address}"
            source_label = "🌊 Pump.fun" if source == "pumpfun" else "📊 Dexscreener"

            alerted_tokens[token_address] = time.time()
            tracking_list[token_address] = {
                "name": token_name,
                "symbol": token_symbol,
                "alert_mcap": mcap,
                "alerted_at": time.time(),
                "milestones_hit": [],
                "dex_url": dex_url
            }

            message = (
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
                f"*Why flagged:*\n" + "\n".join(reasons) + f"\n\n"
                f"*Rug Risk: {rug_label}*\n" + "\n".join(rug_flags) + f"\n\n"
                f"📊 _Tracking: +50% +100% +200% +300% +500% +1000%_\n\n"
                f"🔗 [DexScreener]({dex_url})\n"
                f"⚡ [Snipe on Trojan]({trojan_link})\n"
                f"🔄 [Buy on Raydium]({raydium_link})\n\n"
                f"⚠️ _DYOR. Not financial advice._"
            )
            send_telegram(message)
            print(f"Alerted: {token_name} ({token_symbol}) — {alert_level} — Score: {score}")

        except Exception as e:
            print(f"Analysis error: {e}")
            continue

# ── MAIN LOOP ─────────────────────────────────────────────────

def main():
    print("🤖 Memecoin Scanner Bot v10 started...")
    send_telegram(
        "🤖 *Memecoin Scanner Bot v10 is now LIVE!*\n\n"
        "📡 *Sources:*\n"
        "✅ Dexscreener New Pairs\n"
        "✅ Dexscreener Trending\n"
        "✅ Dexscreener Gainers\n"
        "✅ Pump.fun Graduated (with watch mode)\n"
        "✅ Pump.fun Active\n"
        "✅ Birdeye Trending\n\n"
        "🆕 *New Intelligence:*\n"
        "🎓 Graduation watch — runner vs dip vs rug detection\n"
        "💎 Dip entry alerts with recovery confirmation\n"
        "🧠 Narrative analysis on every coin\n"
        "👛 Wallet concentration filter\n"
        "🚀 Milestone tracking: +50% to +1000%\n"
        "⚡ Scanning every 60 seconds\n\n"
        "Let's catch some runners! 🎯"
    )
    while True:
        print("🔍 Scanning all sources...")
        all_pairs = []
        all_pairs.extend(fetch_dexscreener_new_pairs())
        all_pairs.extend(fetch_dexscreener_trending())
        all_pairs.extend(fetch_dexscreener_gainers())
        fetch_pumpfun_graduated()  # adds to watchlist, doesn't return pairs
        all_pairs.extend(fetch_pumpfun_active())
        all_pairs.extend(fetch_birdeye_trending())
        print(f"Total pairs to analyze: {len(all_pairs)}")
        print(f"Graduation watchlist: {len(graduation_watchlist)} tokens being monitored")
        if all_pairs:
            analyze_and_alert(all_pairs)
        monitor_graduation_watchlist()
        check_milestones()
        time.sleep(ALERT_INTERVAL)

if __name__ == "__main__":
    main()
