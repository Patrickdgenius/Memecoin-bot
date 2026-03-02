import os
import time
import random
import string
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OWNER_CHAT_ID = TELEGRAM_CHAT_ID

MCAP_MIN = 40_000
MCAP_MAX = 300_000
VOLUME_MIN = 20_000
MIN_LIQUIDITY = 5_000
MIN_PRICE_CHANGE = 10
ALERT_INTERVAL = 60
GRADUATION_MCAP_MIN = 25_000
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

alerted_tokens = {}
tracking_list = {}
graduation_watchlist = {}
rug_blacklist = set()
active_groups = {}
access_codes = {}
last_update_id = 0

MILESTONES = [50, 100, 200, 300, 500, 1000]

# ── TELEGRAM MESSAGING ────────────────────────────────────────

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
    """Send alert to owner + all active groups"""
    send_telegram(message, TELEGRAM_CHAT_ID)
    for group_id in list(active_groups.keys()):
        try:
            send_telegram(message, group_id)
        except Exception as e:
            print(f"Broadcast error to {group_id}: {e}")

# ── ACCESS CODE SYSTEM ────────────────────────────────────────

def generate_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def handle_commands():
    """Listen for commands from owner and groups"""
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

            # ── OWNER COMMANDS ──
            if chat_id == str(OWNER_CHAT_ID):

                if text == "/gencode":
                    code = generate_code()
                    access_codes[code] = {"created_at": time.time(), "used": False}
                    send_telegram(
                        f"🔑 *New Access Code Generated*\n\n"
                        f"`{code}`\n\n"
                        f"Share this with whoever you want to give access.\n"
                        f"They type `/activate {code}` in their group.",
                        OWNER_CHAT_ID
                    )

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
                        send_telegram(f"✅ Access revoked for *{name}*", OWNER_CHAT_ID)
                        send_telegram("⛔ Your access to this bot has been revoked.", group_id)
                    else:
                        send_telegram("❌ Group ID not found.", OWNER_CHAT_ID)

                elif text == "/status":
                    send_telegram(
                        f"📊 *Bot Status*\n\n"
                        f"👥 Active groups: {len(active_groups)}\n"
                        f"🔑 Unused codes: {sum(1 for c in access_codes.values() if not c['used'])}\n"
                        f"👀 Graduation watchlist: {len(graduation_watchlist)}\n"
                        f"🚫 Rug blacklist: {len(rug_blacklist)}\n"
                        f"📈 Tracking: {len(tracking_list)} coins\n"
                        f"✅ Alerted today: {len(alerted_tokens)} coins",
                        OWNER_CHAT_ID
                    )

                elif text == "/help":
                    send_telegram(
                        "🤖 *Owner Commands:*\n\n"
                        "/gencode — Generate new access code\n"
                        "/listgroups — See all active groups\n"
                        "/revoke GROUP\\_ID — Remove group access\n"
                        "/status — Bot stats\n"
                        "/help — This message",
                        OWNER_CHAT_ID
                    )

            # ── GROUP COMMANDS ──
            if chat_type in ["group", "supergroup"]:
                group_name = message.get("chat", {}).get("title", "Unknown Group")

                if text.startswith("/activate "):
                    code = text.split(" ", 1)[1].strip().upper()
                    if chat_id in active_groups:
                        send_telegram("✅ This group is already activated!", chat_id)
                    elif code in access_codes and not access_codes[code]["used"]:
                        access_codes[code]["used"] = True
                        active_groups[chat_id] = {
                            "name": group_name,
                            "activated_at": time.time(),
                            "activated_by": username
                        }
                        send_telegram(
                            f"✅ *Meme Radar Signal activated!*\n\n"
                            f"This group will now receive all memecoin alerts.\n"
                            f"🚀 Let's catch some runners!",
                            chat_id
                        )
                        send_telegram(
                            f"✅ *New group activated:*\n"
                            f"Group: {group_name}\n"
                            f"ID: `{chat_id}`\n"
                            f"By: @{username}",
                            OWNER_CHAT_ID
                        )
                    else:
                        send_telegram(
                            "❌ Invalid or already used access code.\n"
                            "Contact the admin for a valid code.",
                            chat_id
                        )

                elif text == "/start" or text == "/help":
                    if chat_id in active_groups:
                        send_telegram(
                            "🤖 *Meme Radar Signal is active in this group!*\n\n"
                            "You'll receive alerts for:\n"
                            "🎓 Pump.fun graduations\n"
                            "💎 Dip entries\n"
                            "🚨 Strong momentum plays\n"
                            "🚀 Milestone updates\n\n"
                            "DYOR. Not financial advice.",
                            chat_id
                        )
                    else:
                        send_telegram(
                            "👋 *Meme Radar Signal Bot*\n\n"
                            "This group is not yet activated.\n"
                            "Contact the admin for an access code, then type:\n"
                            "`/activate YOURCODE`",
                            chat_id
                        )

    except Exception as e:
        print(f"Command handler error: {e}")

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
                if not token_address or token_address in rug_blacklist or token_address in alerted_tokens:
                    continue
                mcap = coin.get("usd_market_cap", 0) or 0
                if mcap < GRADUATION_MCAP_MIN or mcap > MCAP_MAX:
                    continue
                if coin.get("reply_count", 0) < 3:
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
                        "created_timestamp": coin.get("created_timestamp", 0),
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
                        "url": f"https://dexscreener.com/solana/{token_address}"
                    }
                    print(f"Watching graduation: {coin.get('name')} — ${mcap:,.0f}")
            except:
                continue
    except Exception as e:
        print(f"Pump.fun graduated error: {e}")

def fetch_pumpfun_active():
    pairs = []
    try:
        url = "https://frontend-api-v3.pump.fun/coins?limit=50&sort=usd_market_cap&order=desc&includeNsfw=false"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        coins = r.json()
        coin_list = coins if isinstance(coins, list) else coins.get("coins", [])
        active = [c for c in coin_list if not c.get("complete")]
        for coin in active:
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
                    "volume_detail": {"m5": {"buyVolume": 0, "sellVolume": 0}},
                    "pairCreatedAt": created_timestamp,
                    "url": f"https://pump.fun/{token_address}",
                    "source": "pumpfun",
                    "graduated": False,
                    "description": coin.get("description", "") or "",
                    "reply_count": coin.get("reply_count", 0) or 0
                })
            except:
                continue
        print(f"Pump.fun active: {len(pairs)} fetched")
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

# ── WALLET CONCENTRATION ──────────────────────────────────────

def check_wallet_concentration(token_address):
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
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
            return concentration, f"🔴 HIGH — top 10 wallets hold {concentration:.0f}%"
        elif concentration > 30:
            return concentration, f"🟡 MEDIUM — top 10 wallets hold {concentration:.0f}%"
        else:
            return concentration, f"🟢 LOW — top 10 wallets hold {concentration:.0f}%"
    except Exception as e:
        print(f"Wallet concentration error: {e}")
        return None, "Unknown"

# ── NARRATIVE DETECTION ───────────────────────────────────────

def get_narrative(description, name, symbol):
    if not description and not name:
        return "No description found", 0
    combined = f"{(description or '').lower()} {name.lower()} {symbol.lower()}"
    score = 0
    signals = []
    bullish = {
        "ai": ("🤖 AI narrative", 3), "artificial intelligence": ("🤖 AI narrative", 3),
        "agent": ("🤖 AI agent", 3), "meme": ("😂 Meme narrative", 2),
        "dog": ("🐕 Dog coin", 2), "cat": ("🐈 Cat coin", 2),
        "pepe": ("🐸 Pepe narrative", 2), "elon": ("⚡ Elon narrative", 3),
        "trump": ("🇺🇸 Political narrative", 2), "based": ("🔵 Based narrative", 2),
        "community": ("👥 Community driven", 2), "viral": ("📱 Viral narrative", 2),
        "fair launch": ("✅ Fair launch", 3), "no team": ("✅ No team tokens", 3),
        "renounced": ("✅ Renounced", 3), "burned": ("🔥 LP burned", 3),
        "gas": ("⛽ Utility narrative", 2), "war": ("⚔️ War narrative", 2),
        "jelly": ("🟡 Fun narrative", 1), "sol": ("☀️ Solana native", 1),
    }
    for keyword, (label, points) in bullish.items():
        if keyword in combined:
            signals.append(label)
            score += points
    for bad in ["rug", "scam", "fake", "honeypot", "drain"]:
        if bad in combined:
            score -= 5
            signals.append(f"⚠️ Warning: '{bad}' in description")
    if score >= 6:
        strength = "🔥 Very bullish narrative"
    elif score >= 3:
        strength = "📈 Bullish narrative"
    elif score > 0:
        strength = "🟡 Neutral narrative"
    else:
        strength = "🔴 Weak narrative"
    return f"{strength}\n" + "\n".join(signals[:5]) if signals else strength, score

# ── SELL PRESSURE ANALYSIS ────────────────────────────────────

def is_real_pump(buy_vol, sell_vol, buys, sells, price_change_5m):
    """Determine if a pump is real or fake based on volume analysis"""
    if buy_vol <= 0 and sell_vol <= 0:
        return True, "No volume data — passing"

    total_vol = buy_vol + sell_vol
    if total_vol == 0:
        return True, "No volume data"

    buy_vol_ratio = buy_vol / total_vol

    # Fake pump — sell volume dominates
    if sell_vol > buy_vol * 2:
        return False, f"Sell volume {sell_vol:.0f} vs buy volume {buy_vol:.0f} — fake pump"

    # Fake pump — price pumping but barely any real buyers
    if price_change_5m > 20 and buys < 5:
        return False, "Price pumping but less than 5 real buyers — manipulation"

    # Real pump — buys dominating in both count and volume
    if buy_vol_ratio > 0.6 and buys > sells:
        return True, "Real pump — buy volume and count dominant"

    return True, "Passing volume check"

# ── GRADUATION WATCHLIST MONITOR ──────────────────────────────

def monitor_graduation_watchlist():
    to_remove = []
    for token_address, info in list(graduation_watchlist.items()):
        try:
            if info.get("alerted"):
                to_remove.append(token_address)
                continue

            hours_watching = (time.time() - info["added_at"]) / 3600
            if hours_watching > 12:
                print(f"Graduation watch expired: {info['name']}")
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
            price_change_5m = pair.get("priceChange", {}).get("m5", 0) or 0
            dex_url = pair.get("url", info["url"])

            # Get volume detail
            vol_data = pair.get("volume", {})
            buy_vol_5m = vol_data.get("m5", 0) or 0
            sell_vol_1h = vol_data.get("h1", 0) or 0

            if current_mcap <= 0:
                continue

            graduation_mcap = info["graduation_mcap"]
            change_from_graduation = ((current_mcap - graduation_mcap) / graduation_mcap) * 100

            # Track history
            info["price_history"].append(current_mcap)
            info["buy_count_history"].append(buys_5m)
            info["sell_count_history"].append(sells_5m)
            info["buy_vol_history"].append(buy_vol_5m)
            info["sell_vol_history"].append(sell_vol_1h)

            if len(info["price_history"]) > 30:
                info["price_history"] = info["price_history"][-30:]
                info["buy_count_history"] = info["buy_count_history"][-30:]
                info["sell_count_history"] = info["sell_count_history"][-30:]
                info["buy_vol_history"] = info["buy_vol_history"][-30:]
                info["sell_vol_history"] = info["sell_vol_history"][-30:]

            # ── RUG SIGNAL ──
            # Sells dominating in volume repeatedly = rug
            recent_buy_vol = sum(info["buy_vol_history"][-5:]) if len(info["buy_vol_history"]) >= 5 else buy_vol_5m
            recent_sell_vol = sum(info["sell_vol_history"][-5:]) if len(info["sell_vol_history"]) >= 5 else sell_vol_1h

            if recent_sell_vol > recent_buy_vol * 3 and sells_5m > buys_5m * 2:
                info["consecutive_rug_signals"] += 1
            else:
                info["consecutive_rug_signals"] = max(0, info["consecutive_rug_signals"] - 1)

            if info["consecutive_rug_signals"] >= 5:
                print(f"RUG confirmed: {info['name']} — blacklisting")
                rug_blacklist.add(token_address)
                to_remove.append(token_address)
                continue

            # ── DIP TRACKING ──
            if change_from_graduation < -10 and not info["dip_detected"]:
                info["dip_detected"] = True
                info["dip_low_mcap"] = current_mcap
                print(f"Dip detected: {info['name']} — monitoring recovery")

            if info["dip_detected"] and info["dip_low_mcap"]:
                if current_mcap < info["dip_low_mcap"]:
                    info["dip_low_mcap"] = current_mcap

                dip_low = info["dip_low_mcap"]
                recovery_pct = ((current_mcap - dip_low) / dip_low) * 100 if dip_low > 0 else 0
                dip_depth = ((graduation_mcap - dip_low) / graduation_mcap) * 100 if graduation_mcap > 0 else 0

                # Recovery confirmed — buys winning in both count AND volume
                if (recovery_pct > 10 and
                    buys_5m >= 3 and
                    buys_5m > sells_5m and
                    buy_vol_5m >= sell_vol_1h * 0.5 and
                    liquidity > MIN_LIQUIDITY):

                    alert_type = f"💎 DIP ENTRY — dipped {dip_depth:.0f}%, now recovering +{recovery_pct:.0f}%"
                    _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url)
                    info["alerted"] = True
                    continue

            # ── STRAIGHT RUNNER ──
            if (change_from_graduation > 30 and
                buys_1h > sells_1h and
                buy_vol_5m >= sell_vol_1h * 0.4 and
                current_mcap <= MCAP_MAX):
                alert_type = "🚀 GRADUATION RUNNER — pumping since migration"
                _send_graduation_alert(token_address, info, pair, current_mcap, change_from_graduation, alert_type, dex_url)
                info["alerted"] = True

        except Exception as e:
            print(f"Graduation monitor error {token_address}: {e}")
            continue

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

    narrative_summary, _ = get_narrative(info.get("description", ""), name, symbol)
    concentration, concentration_label = check_wallet_concentration(token_address)

    if concentration and concentration > 50:
        print(f"Skipping {name} — wallet concentration too high ({concentration:.0f}%)")
        return

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
        f"💰 Migration Mcap: ${graduation_mcap:,.0f}\n"
        f"💰 Current Mcap: ${current_mcap:,.0f}\n"
        f"📈 Change since migration: {change_from_graduation:+.1f}%\n"
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
    broadcast(message)
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
            if (time.time() - info["alerted_at"]) / 3600 > 48:
                to_remove.append(token_address)
                continue
            current_mcap = get_current_mcap(token_address)
            alert_mcap = info["alert_mcap"]
            if current_mcap <= 0 or alert_mcap <= 0:
                continue
            change_pct = ((current_mcap - alert_mcap) / alert_mcap) * 100
            hours_since = (time.time() - info["alerted_at"]) / 3600
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
                    print(f"Milestone: {info['name']} +{milestone}%!")
            if all(m in info["milestones_hit"] for m in MILESTONES):
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
            sells_5m = txns.get("m5", {}).get("sells", 0)
            description = pair.get("description", "") or ""

            # Volume detail for fake pump detection
            vol_data = pair.get("volume", {})
            buy_vol_5m = vol_data.get("m5", 0) or 0
            sell_vol_5m = buy_vol_5m * 0.4  # estimate if not available

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

            # Fake pump check
            is_real, pump_reason = is_real_pump(buy_vol_5m, sell_vol_5m, buys_5m, sells_5m, price_change_5m)
            if not is_real:
                print(f"Fake pump filtered: {token_name} — {pump_reason}")
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
                reasons.append(f"📈 {price_change_5m:.0f}% in 5 mins")

            if price_change_1h >= 100:
                score += 3
                reasons.append("🔥 100%+ in 1hr")
            elif price_change_1h >= 50:
                score += 2
                reasons.append("⚡ 50%+ in 1hr")
            elif price_change_1h >= 10:
                score += 1
                reasons.append("📈 10%+ in 1hr")

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
                reasons.append("🆕 Fresh (<6hrs)")

            if mcap < 100_000:
                score += 3
                reasons.append("🎯 Ultra micro mcap")
            elif mcap < 200_000:
                score += 2
                reasons.append("🎯 Micro mcap")
            elif mcap < 300_000:
                score += 1
                reasons.append("🎯 Low mcap")

            narrative_summary, narrative_score = get_narrative(description, token_name, token_symbol)
            if narrative_score >= 6:
                score += 2
                reasons.append("🧠 Very bullish narrative")
            elif narrative_score >= 3:
                score += 1
                reasons.append("🧠 Bullish narrative")

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

            alerted_tokens[token_address] = time.time()
            tracking_list[token_address] = {
                "name": token_name, "symbol": token_symbol,
                "alert_mcap": mcap, "alerted_at": time.time(),
                "milestones_hit": [], "dex_url": dex_url
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
            broadcast(message)
            print(f"Alerted: {token_name} ({token_symbol}) — {alert_level} — Score: {score}")

        except Exception as e:
            print(f"Analysis error: {e}")
            continue

# ── MAIN LOOP ─────────────────────────────────────────────────

def main():
    print("🤖 Memecoin Scanner Bot v11 started...")
    send_telegram(
        "🤖 *Memecoin Scanner Bot v11 is now LIVE!*\n\n"
        "📡 *Sources:*\n"
        "✅ Dexscreener New Pairs\n"
        "✅ Dexscreener Trending\n"
        "✅ Dexscreener Gainers\n"
        "✅ Pump.fun Graduated (watch mode)\n"
        "✅ Pump.fun Active\n"
        "✅ Birdeye Trending\n\n"
        "🆕 *New in v11:*\n"
        "🔍 Sell volume vs buy volume — fake pump filter\n"
        "🎓 Graduation mcap min $25k\n"
        "👥 Group access system — use /gencode\n"
        "🚫 Rug blacklist with volume confirmation\n"
        "💎 Dip + recovery monitoring\n\n"
        "📊 Milestones: +50% to +1000%\n"
        "⚡ Scanning every 60 seconds\n\n"
        "Owner commands: /gencode /listgroups /status /help\n\n"
        "Let's catch runners! 🎯"
    )
    while True:
        handle_commands()
        print("🔍 Scanning all sources...")
        all_pairs = []
        all_pairs.extend(fetch_dexscreener_new_pairs())
        all_pairs.extend(fetch_dexscreener_trending())
        all_pairs.extend(fetch_dexscreener_gainers())
        fetch_pumpfun_graduated()
        all_pairs.extend(fetch_pumpfun_active())
        all_pairs.extend(fetch_birdeye_trending())
        print(f"Total pairs: {len(all_pairs)} | Watchlist: {len(graduation_watchlist)} | Tracking: {len(tracking_list)}")
        if all_pairs:
            analyze_and_alert(all_pairs)
        monitor_graduation_watchlist()
        check_milestones()
        time.sleep(ALERT_INTERVAL)

if __name__ == "__main__":
    main()
