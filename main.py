import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MCAP_MIN = 40_000
MCAP_MAX = 10_000_000
VOLUME_MIN = 20_000
MIN_LIQUIDITY = 10_000
MIN_PRICE_CHANGE = 10
ALERT_INTERVAL = 60
alerted_tokens = {}
tracking_list = {}

MILESTONES = [50, 100, 200, 300, 500, 1000]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch_dexscreener_new_pairs():
    pairs = []
    try:
        url = "https://api.dexscreener.com/latest/dex/search?q=solana"
        r = requests.get(url, timeout=10)
        data = r.json()
        all_pairs = data.get("pairs", [])
        pairs = [p for p in all_pairs if p.get("chainId") == "solana"]
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
        if isinstance(data, list):
            solana_tokens = [t for t in data if t.get("chainId") == "solana"]
        else:
            solana_tokens = [t for t in data.get("pairs", []) if t.get("chainId") == "solana"]
        addresses = [t.get("tokenAddress") for t in solana_tokens if t.get("tokenAddress")]
        for addr in addresses[:20]:
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
        for query in ["pump", "sol", "meme", "cat", "dog", "pepe"]:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
                r = requests.get(url, timeout=10)
                data = r.json()
                all_pairs = data.get("pairs", [])
                solana_pairs = [p for p in all_pairs if p.get("chainId") == "solana"]
                pairs.extend(solana_pairs)
                time.sleep(0.2)
            except:
                continue
        print(f"Dexscreener gainers: {len(pairs)} fetched")
    except Exception as e:
        print(f"Dexscreener gainers error: {e}")
    return pairs

def fetch_pumpfun_tokens():
    pairs = []
    try:
        url = "https://frontend-api-v3.pump.fun/coins?limit=50&sort=usd_market_cap&order=desc&includeNsfw=false"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        coins = r.json()
        coin_list = coins if isinstance(coins, list) else coins.get("coins", [])
        print(f"Pump.fun: {len(coin_list)} coins fetched")
        for coin in coin_list:
            try:
                mcap = coin.get("usd_market_cap", 0) or 0
                token_address = coin.get("mint", "")
                name = coin.get("name", "Unknown")
                symbol = coin.get("symbol", "?")
                created_timestamp = coin.get("created_timestamp", 0)
                age_hours = (time.time() - created_timestamp / 1000) / 3600 if created_timestamp else None
                reply_count = coin.get("reply_count", 0) or 0
                complete = coin.get("complete", False)
                if mcap < 50_000 or mcap > MCAP_MAX:
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
                    "graduated": complete
                })
            except:
                continue
    except Exception as e:
        print(f"Pump.fun fetch error: {e}")
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
                token_address = token.get("address", "")
                if not token_address:
                    continue
                r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
                d2 = r2.json()
                if isinstance(d2, dict):
                    pairs.extend(d2.get("pairs", []))
                time.sleep(0.2)
            except:
                continue
    except Exception as e:
        print(f"Birdeye fetch error: {e}")
    return pairs

def get_current_data(token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=10)
        data = r.json()
        pairs = data.get("pairs", [])
        if pairs:
            p = pairs[0]
            return {
                "mcap": p.get("marketCap", 0) or 0,
                "price": p.get("priceUsd", "0"),
                "volume": p.get("volume", {}).get("h24", 0) or 0
            }
    except:
        pass
    return None

def check_milestones():
    to_remove = []
    for token_address, info in list(tracking_list.items()):
        try:
            # Stop tracking after 48 hours
            hours_since = (time.time() - info["alerted_at"]) / 3600
            if hours_since > 48:
                to_remove.append(token_address)
                continue

            current_data = get_current_data(token_address)
            if not current_data:
                continue

            current_mcap = current_data["mcap"]
            alert_mcap = info["alert_mcap"]

            if current_mcap <= 0 or alert_mcap <= 0:
                continue

            change_pct = ((current_mcap - alert_mcap) / alert_mcap) * 100
            milestones_hit = info["milestones_hit"]

            for milestone in MILESTONES:
                if milestone in milestones_hit:
                    continue
                if change_pct >= milestone:
                    if milestone >= 500:
                        emoji = "🤯"
                    elif milestone >= 300:
                        emoji = "🚀🚀🚀"
                    elif milestone >= 100:
                        emoji = "🚀🚀"
                    else:
                        emoji = "🚀"

                    message = (
                        f"{emoji} *MILESTONE HIT — +{milestone}%!* {emoji}\n\n"
                        f"*{info['name']}* (${info['symbol']})\n\n"
                        f"💰 Mcap at alert: ${alert_mcap:,.0f}\n"
                        f"💰 Mcap now: ${current_mcap:,.0f}\n"
                        f"📈 Total gain: *+{change_pct:.0f}%*\n"
                        f"⏱ Time since alert: {hours_since:.1f}hrs\n\n"
                        f"📋 CA: `{token_address}`\n"
                        f"🔗 [View on DexScreener]({info['dex_url']})\n"
                        f"⚡ [Snipe on Trojan](https://t.me/paris_trojanbot?start=snipe_{token_address})"
                    )
                    send_telegram(message)
                    milestones_hit.append(milestone)
                    print(f"Milestone: {info['name']} hit +{milestone}%!")

            # If all milestones hit, stop tracking
            if all(m in milestones_hit for m in MILESTONES):
                to_remove.append(token_address)

        except Exception as e:
            print(f"Milestone check error: {e}")
            continue

    for addr in to_remove:
        if addr in tracking_list:
            del tracking_list[addr]

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
        flags.append("🟡 Very few buyers in last hour")

    if risk_score == 0:
        flags.append("🟢 No major rug flags detected")
    if risk_score >= 4:
        label = "🔴 HIGH RUG RISK"
    elif risk_score >= 2:
        label = "🟡 MEDIUM RISK"
    else:
        label = "🟢 LOW RISK"
    return label, flags, risk_score

def get_alert_level(score):
    if score >= 9:
        return "🔴 VERY STRONG", "🚨🚨🚨"
    elif score >= 6:
        return "🟠 STRONG", "🚨🚨"
    else:
        return "🟡 WATCH", "🚨"

def analyze_and_alert(pairs):
    seen = set()
    for pair in pairs:
        try:
            token_address = pair.get("baseToken", {}).get("address", "")
            if not token_address or token_address in seen:
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
            graduated = pair.get("graduated", False)
            txns = pair.get("txns", {})
            buys_1h = txns.get("h1", {}).get("buys", 0)
            sells_1h = txns.get("h1", {}).get("sells", 0)
            buys_5m = txns.get("m5", {}).get("buys", 0)
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
                if graduated:
                    score += 3
                    reasons.append("🎓 Graduated from Pump.fun")
                else:
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
                reasons.append(f"🔥 {buys_5m} buys in last 5 mins")
            elif buys_5m >= 5:
                score += 1
                reasons.append(f"👥 {buys_5m} buys in last 5 mins")

            if liquidity > 100_000:
                score += 1
                reasons.append("💧 Strong liquidity")

            if age_hours and age_hours < 1:
                score += 2
                reasons.append("🆕 Very fresh (<1hr)")
            elif age_hours and age_hours < 6:
                score += 1
                reasons.append("🆕 Fresh token (<6hrs)")

            if mcap < 200_000:
                score += 2
                reasons.append("🎯 Micro mcap — extremely early")
            elif mcap < 500_000:
                score += 1
                reasons.append("🎯 Low mcap — early entry")

            if score < 2:
                continue

            rug_label, rug_flags, rug_score = get_rug_risk(pair, source)
            if source == "pumpfun" and rug_score >= 2:
                continue
            if source != "pumpfun" and rug_score >= 4:
                continue

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
                f"📈 5min Change: {price_change_5m:.1f}%\n"
                f"📈 1hr Change: {price_change_1h:.1f}%\n"
                f"📈 24hr Change: {price_change_24h:.1f}%\n"
                f"💵 Price: ${price_usd}\n"
                f"📊 Volume 24hr: ${volume_24h:,.0f}\n"
                f"💧 Liquidity: ${liquidity:,.0f}\n"
                f"🛒 Buys/Sells (1hr): {buys_1h}/{sells_1h}\n"
                f"⚡ Buys last 5min: {buys_5m}\n"
                f"⏰ Token Age: {f'{age_hours:.1f}hrs' if age_hours else 'Unknown'}\n\n"
                f"*Why it's flagged:*\n" + "\n".join(reasons) + f"\n\n"
                f"*Rug Risk: {rug_label}*\n" + "\n".join(rug_flags) + f"\n\n"
                f"📊 _Tracking milestones: +50% +100% +200% +300% +500% +1000%_\n\n"
                f"🔗 [DexScreener]({dex_url})\n"
                f"⚡ [Snipe on Trojan]({trojan_link})\n"
                f"🔄 [Buy on Raydium]({raydium_link})\n\n"
                f"⚠️ _DYOR. Not financial advice._"
            )
            send_telegram(message)
            print(f"Alerted: {token_name} ({token_symbol}) — Level: {alert_level} — Score: {score}")

        except Exception as e:
            print(f"Analysis error: {e}")
            continue

def main():
    print("🤖 Memecoin Scanner Bot v9 started...")
    send_telegram(
        "🤖 *Memecoin Scanner Bot v9 is now LIVE!*\n\n"
        "📡 *Sources:*\n"
        "✅ Dexscreener New Pairs\n"
        "✅ Dexscreener Trending\n"
        "✅ Dexscreener Gainers\n"
        "✅ Pump.fun\n"
        "✅ Birdeye Trending\n\n"
        "🆕 *Milestone Tracking:*\n"
        "🚀 Alerts at +50% +100% +200% +300% +500% +1000%\n"
        "Tracked for 48hrs after each alert\n\n"
        "⚡ Scanning every 60 seconds\n"
        "📋 CA in every alert\n\n"
        "Scanning now..."
    )
    while True:
        print("🔍 Scanning all sources...")
        all_pairs = []
        all_pairs.extend(fetch_dexscreener_new_pairs())
        all_pairs.extend(fetch_dexscreener_trending())
        all_pairs.extend(fetch_dexscreener_gainers())
        all_pairs.extend(fetch_pumpfun_tokens())
        all_pairs.extend(fetch_birdeye_trending())
        print(f"Total pairs to analyze: {len(all_pairs)}")
        if all_pairs:
            analyze_and_alert(all_pairs)
        check_milestones()
        time.sleep(ALERT_INTERVAL)

if __name__ == "__main__":
    main()
