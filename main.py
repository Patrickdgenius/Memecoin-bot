import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MCAP_MIN = 40_000
MCAP_MAX = 10_000_000
VOLUME_MIN = 20_000
MIN_LIQUIDITY = 12_000
MIN_PRICE_CHANGE = 10
ALERT_INTERVAL = 300
alerted_tokens = {}

KNOWN_WHALE_WALLETS = [
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "5tzFkiKscXHK5ZXCGbCzNzHkHa7Fy8bN6hJdEMFvQPqJ",
]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch_dexscreener_tokens():
    url = "https://api.dexscreener.com/latest/dex/tokens/solana"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        pairs = data.get("pairs", [])
        print(f"Dexscreener: {len(pairs)} pairs fetched")
        return pairs
    except Exception as e:
        print(f"Dexscreener fetch error: {e}")
        return []

def fetch_dexscreener_trending():
    url = "https://api.dexscreener.com/token-boosts/top/v1"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        solana_tokens = [t for t in data if t.get("chainId") == "solana"]
        addresses = [t.get("tokenAddress") for t in solana_tokens if t.get("tokenAddress")]
        all_pairs = []
        for addr in addresses[:10]:
            try:
                r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=10)
                d2 = r2.json()
                all_pairs.extend(d2.get("pairs", []))
                time.sleep(0.3)
            except:
                continue
        print(f"Dexscreener trending: {len(all_pairs)} pairs fetched")
        return all_pairs
    except Exception as e:
        print(f"Trending fetch error: {e}")
        return []

def fetch_pumpfun_tokens():
    pairs = []
    try:
        url = "https://frontend-api.pump.fun/coins/latest?limit=50&includeNsfw=false"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        coins = r.json()
        print(f"Pump.fun: {len(coins)} coins fetched")
        for coin in coins:
            try:
                mcap = coin.get("usd_market_cap", 0) or 0
                token_address = coin.get("mint", "")
                name = coin.get("name", "Unknown")
                symbol = coin.get("symbol", "?")
                created_timestamp = coin.get("created_timestamp", 0)
                age_hours = (time.time() - created_timestamp / 1000) / 3600 if created_timestamp else None

                if mcap < MCAP_MIN or mcap > MCAP_MAX:
                    continue
                if age_hours and age_hours > 24:
                    continue

                pair = {
                    "baseToken": {"address": token_address, "name": name, "symbol": symbol},
                    "marketCap": mcap,
                    "volume": {"h24": coin.get("volume", 0) or 0},
                    "liquidity": {"usd": coin.get("virtual_sol_reserves", 0) * 150 or 0},
                    "priceChange": {"h1": 0, "h24": 0},
                    "priceUsd": str(coin.get("price", 0)),
                    "txns": {"h1": {"buys": 0, "sells": 0}},
                    "pairCreatedAt": created_timestamp,
                    "url": f"https://pump.fun/{token_address}",
                    "source": "pumpfun"
                }
                pairs.append(pair)
            except:
                continue
    except Exception as e:
        print(f"Pump.fun fetch error: {e}")
    return pairs

def get_rug_risk(pair):
    risk_score = 0
    flags = []
    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
    mcap = pair.get("marketCap", 0) or 0
    volume_24h = pair.get("volume", {}).get("h24", 0) or 0
    txns = pair.get("txns", {})
    buys_24h = txns.get("h24", {}).get("buys", 0)
    sells_24h = txns.get("h24", {}).get("sells", 0)
    price_change_24h = pair.get("priceChange", {}).get("h24", 0) or 0

    if mcap > 0 and liquidity < mcap * 0.05:
        risk_score += 2
        flags.append("🔴 Very low liquidity vs mcap")
    if sells_24h > buys_24h * 1.5:
        risk_score += 2
        flags.append("🔴 Heavy sell pressure")
    if price_change_24h > 500:
        risk_score += 1
        flags.append("🟡 Extreme 24hr pump — watch for dump")
    if liquidity < 40_000:
        risk_score += 1
        flags.append("🟡 Low liquidity — easy to manipulate")
    if volume_24h > mcap * 3:
        risk_score += 1
        flags.append("🟡 Suspicious volume — possible wash trading")
    if risk_score == 0:
        flags.append("🟢 No major rug flags detected")

    if risk_score >= 4:
        label = "🔴 HIGH RUG RISK"
    elif risk_score >= 2:
        label = "🟡 MEDIUM RISK"
    else:
        label = "🟢 LOW RISK"
    return label, flags, risk_score

def check_whale_activity(pair):
    token_address = pair.get("baseToken", {}).get("address", "")
    whales_found = []
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=10)
        data = r.json()
        pairs = data.get("pairs", [])
        if pairs:
            for wallet in KNOWN_WHALE_WALLETS:
                if any(wallet.lower() in str(p).lower() for p in pairs):
                    whales_found.append(wallet[:8] + "...")
    except:
        pass
    return whales_found

def get_alert_level(score):
    if score >= 9:
        return "🔴 VERY STRONG", "🚨🚨🚨"
    elif score >= 6:
        return "🟠 STRONG", "🚨🚨"
    else:
        return "🟡 WATCH", "🚨"

def analyze_and_alert(pairs):
    for pair in pairs:
        try:
            token_address = pair.get("baseToken", {}).get("address", "")
            token_name = pair.get("baseToken", {}).get("name", "Unknown")
            token_symbol = pair.get("baseToken", {}).get("symbol", "?")
            mcap = pair.get("marketCap", 0) or 0
            volume_24h = pair.get("volume", {}).get("h24", 0) or 0
            liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
            price_change_1h = pair.get("priceChange", {}).get("h1", 0) or 0
            price_change_24h = pair.get("priceChange", {}).get("h24", 0) or 0
            price_usd = pair.get("priceUsd", "0")
            dex_url = pair.get("url", "")
            source = pair.get("source", "dexscreener")
            txns = pair.get("txns", {})
            buys_1h = txns.get("h1", {}).get("buys", 0)
            sells_1h = txns.get("h1", {}).get("sells", 0)
            age_hours = None
            pair_created = pair.get("pairCreatedAt")
            if pair_created:
                age_hours = (time.time() - pair_created / 1000) / 3600

            if token_address in alerted_tokens:
                if time.time() - alerted_tokens[token_address] < 86400:
                    continue

            if not (MCAP_MIN <= mcap <= MCAP_MAX):
                continue
            if volume_24h < VOLUME_MIN:
                continue
            if liquidity < MIN_LIQUIDITY:
                continue
            if source != "pumpfun" and price_change_1h < MIN_PRICE_CHANGE:
                continue
            if age_hours and age_hours > 48:
                continue
            if sells_1h > buys_1h * 2 and buys_1h > 0:
                continue

            score = 0
            reasons = []

            if source == "pumpfun":
                score += 2
                reasons.append("🚀 Listed on Pump.fun — very early")

            if price_change_1h >= 100:
                score += 3
                reasons.append("🔥 100%+ pump in 1hr")
            elif price_change_1h >= 50:
                score += 2
                reasons.append("⚡ 50%+ pump in 1hr")
            elif price_change_1h >= 20:
                score += 1
                reasons.append("📈 20%+ pump in 1hr")

            if volume_24h > mcap * 0.5:
                score += 2
                reasons.append("📊 High volume vs mcap")

            if buys_1h > sells_1h * 2 and buys_1h > 0:
                score += 2
                reasons.append("💚 Heavy buy pressure")

            if liquidity > 100_000:
                score += 1
                reasons.append("💧 Strong liquidity")

            if age_hours and age_hours < 1:
                score += 3
                reasons.append("🆕 Brand new token (<1hr)")
            elif age_hours and age_hours < 6:
                score += 2
                reasons.append("🆕 Very fresh token (<6hrs)")
            elif age_hours and age_hours < 24:
                score += 1
                reasons.append("🕐 Token under 24hrs old")

            if mcap < 200_000:
                score += 2
                reasons.append("🎯 Micro mcap — extremely early")
            elif mcap < 500_000:
                score += 1
                reasons.append("🎯 Low mcap — early entry")

            if score < 2:
                continue

            rug_label, rug_flags, rug_score = get_rug_risk(pair)
            if rug_score >= 4:
                continue

            whales = check_whale_activity(pair)
            whale_text = ""
            if whales:
                score += 2
                whale_text = f"🐋 *Known whales in:* {', '.join(whales)}\n"
                reasons.append("🐋 Whale wallet detected!")

            alert_level, alert_emoji = get_alert_level(score)
            trojan_link = f"https://t.me/paris_trojanbot?start=snipe_{token_address}"
            raydium_link = f"https://raydium.io/swap/?inputCurrency=SOL&outputCurrency={token_address}"
            source_label = "🌊 Pump.fun" if source == "pumpfun" else "📊 Dexscreener"

            alerted_tokens[token_address] = time.time()

            message = (
                f"{alert_emoji} *MEMECOIN ALERT — {alert_level}* {alert_emoji}\n\n"
                f"*{token_name}* (${token_symbol})\n"
                f"📡 Source: {source_label}\n\n"
                f"💰 Market Cap: ${mcap:,.0f}\n"
                f"📈 Price Change 1hr: {price_change_1h:.1f}%\n"
                f"📈 Price Change 24hr: {price_change_24h:.1f}%\n"
                f"💵 Price: ${price_usd}\n"
                f"📊 Volume 24hr: ${volume_24h:,.0f}\n"
                f"💧 Liquidity: ${liquidity:,.0f}\n"
                f"🛒 Buys/Sells (1hr): {buys_1h}/{sells_1h}\n"
                f"⏰ Token Age: {f'{age_hours:.1f}hrs' if age_hours else 'Unknown'}\n\n"
                f"{whale_text}"
                f"*Why it's flagged:*\n" + "\n".join(reasons) + f"\n\n"
                f"*Rug Risk: {rug_label}*\n" + "\n".join(rug_flags) + f"\n\n"
                f"🔗 [DexScreener]({dex_url})\n"
                f"⚡ [Snipe on Trojan]({trojan_link})\n"
                f"🔄 [Buy on Raydium]({raydium_link})\n\n"
                f"⚠️ _DYOR. Not financial advice._"
            )
            send_telegram(message)
            print(f"Alerted: {token_name} ({token_symbol}) — Level: {alert_level} — Score: {score} — Source: {source}")

        except Exception as e:
            print(f"Analysis error: {e}")
            continue

def main():
    print("🤖 Memecoin Scanner Bot v3 started...")
    send_telegram(
        "🤖 *Memecoin Scanner Bot v3 is now LIVE!*\n\n"
        "📡 *Sources:*\n"
        "✅ Dexscreener\n"
        "✅ Dexscreener Trending\n"
        "✅ Pump.fun\n\n"
        "🛡 Rug detection | Alert levels | Whale detector | Trojan snipe link\n\n"
        "Scanning every 5 minutes..."
    )
    while True:
        print("🔍 Scanning all sources...")
        all_pairs = []
        all_pairs.extend(fetch_dexscreener_tokens())
        all_pairs.extend(fetch_dexscreener_trending())
        all_pairs.extend(fetch_pumpfun_tokens())
        print(f"Total pairs to analyze: {len(all_pairs)}")
        if all_pairs:
            analyze_and_alert(all_pairs)
        time.sleep(ALERT_INTERVAL)

if __name__ == "__main__":
    main()
