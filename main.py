import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MCAP_MIN = 80_000
MCAP_MAX = 5_000_000
VOLUME_MIN = 100_000
MIN_LIQUIDITY = 40_000
MIN_PRICE_CHANGE = 50
ALERT_INTERVAL = 300
alerted_tokens = set()

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

            if mcap < 800_000:
                score += 1
                reasons.append("🎯 Low mcap — early entry")

            if score < 4:
                continue

            alerted_tokens.add(token_address)

            message = (
                f"🚨 *MEMECOIN ALERT* 🚨\n\n"
                f"*{token_name}* (${token_symbol})\n\n"
                f"💰 Market Cap: ${mcap:,.0f}\n"
                f"📈 Price Change 1hr: {price_change_1h:.1f}%\n"
                f"📈 Price Change 24hr: {price_change_24h:.1f}%\n"
                f"💵 Price: ${price_usd}\n"
                f"📊 Volume 24hr: ${volume_24h:,.0f}\n"
                f"💧 Liquidity: ${liquidity:,.0f}\n"
                f"🛒 Buys/Sells (1hr): {buys_1h}/{sells_1h}\n"
                f"⏰ Token Age: {age_hours:.1f}hrs\n\n"
                f"*Why it's flagged:*\n" + "\n".join(reasons) + f"\n\n"
                f"🔗 [View on DexScreener]({dex_url})\n\n"
                f"⚠️ _DYOR. Not financial advice._"
            )
            send_telegram(message)
            print(f"Alerted: {token_name} ({token_symbol}) — Score: {score}")

        except Exception as e:
            print(f"Analysis error: {e}")
            continue

def main():
    print("🤖 Memecoin Scanner Bot started...")
    send_telegram("🤖 *Memecoin Scanner Bot is now LIVE!*\nScanning Solana for 200-1000% candidates every 5 minutes...")
    while True:
        print("🔍 Scanning Dexscreener...")
        pairs = fetch_solana_tokens()
        if pairs:
            analyze_and_alert(pairs)
        time.sleep(ALERT_INTERVAL)

if __name__ == "__main__":
    main()
