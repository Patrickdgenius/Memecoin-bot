import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MCAP_MIN = 80_000
MCAP_MAX = 5_000_000
VOLUME_MIN = 50_000
MIN_LIQUIDITY = 30_000
MIN_PRICE_CHANGE = 30
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

def fetch_solana_tokens():
    url = "https://api.dexscreener.com/latest/dex/tokens/solana"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        return data.get("pairs", [])
    except Exception as e:
        print(f"Fetch error: {e}")
        return []

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
            txns = pair.get("txns", {})
            buys_1h = txns.get("h1", {}).get("buys", 0)
            sells_1h = txns.get("h1", {}).get("sells", 0)
            age_hours = None
            pair_created = pair.get("pairCreatedAt")
            if pair_created:
                age_hours = (time.time() - pair_created / 1000) / 3600

            if token_address in alerted_tokens:
                last_alerted = alerted_tokens[token_address]
                if time.time() - last_alerted < 86400:
                    continue

            if not (MCAP_MIN <= mcap <= MCAP_MAX):
                continue
            if volume_24h < VOLUME_MIN:
                continue
            if liquidity < MIN_LIQUIDITY:
                continue
            if price_change_1h < MIN_PRICE_CHANGE:
                continue
            if age_hours and age_hours > 48:
                continue
            if sells_1h > buys_1h * 2:
                continue

            score = 0
            reasons = []

            if price_change_1h >= 100:
                score += 3
                reasons.append("🔥 100%+ pump in 1hr")
            elif price_change_1h >= 50:
                score += 2
                reasons.append("⚡ 50%+ pump in 1hr")
            elif price_change_1h >= 30:
                score += 1
                reasons.append("📈 30%+ pump in 1hr")

            if volume_24h > mcap * 0.5:
                score += 2
                reasons.append("📊 High volume vs mcap")

            if buys_1h > sells_1h * 2:
                score += 2
                reasons.append("💚 Heavy buy pressure")

            if liquidity > 100_000:
                score += 1
                reasons.append("💧 Strong liquidity")

            if age_hours and age_hours < 6:
                score += 2
                reasons.append("🆕 Very fresh token (<6hrs)")
            elif age_hours and age_hours < 24:
                score += 1
                reasons.append("🕐 Token under 24hrs old")

            if mcap < 500_000:
                score += 2
                reasons.append("🎯 Very low mcap — very early entry")
            elif mcap < 800_000:
                score += 1
                reasons.append("🎯 Low mcap — early entry")

            if score < 3:
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

            alerted_tokens[token_address] = time.time()

            message = (
                f"{alert_emoji} *MEMECOIN ALERT — {alert_level}* {alert_emoji}\n\n"
                f"*{token_name}* (${token_symbol})\n\n"
                f"💰 Market Cap: ${mcap:,.0f}\n"
                f"📈 Price Change 1hr: {price_change_1h:.1f}%\n"
                f"📈 Price Change 24hr: {price_change_24h:.1f}%\n"
                f"💵 Price: ${price_usd}\n"
                f"📊 Volume 24hr: ${volume_24h:,.0f}\n"
                f"💧 Liquidity: ${liquidity:,.0f}\n"
                f"🛒 Buys/Sells (1hr): {buys_1h}/{sells_1h}\n"
                f"⏰ Token Age: {age_hours:.1f}hrs\n\n"
                f"{whale_text}"
                f"*Why it's flagged:*\n" + "\n".join(reasons) + f"\n\n"
                f"*Rug Risk: {rug_label}*\n" + "\n".join(rug_flags) + f"\n\n"
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
    print("🤖 Memecoin Scanner Bot v2 started...")
    send_telegram("🤖 *Memecoin Scanner Bot v2 is now LIVE!*\n\n✅ Rug pull detection\n✅ Alert levels\n✅ Whale detector\n✅ Direct buy links\n\nScanning Solana every 5 minutes...")
    while True:
        print("🔍 Scanning Dexscreener...")
        pairs = fetch_solana_tokens()
        if pairs:
            analyze_and_alert(pairs)
        time.sleep(ALERT_INTERVAL)

if __name__ == "__main__":
    main()
