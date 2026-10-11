import os
import json
import time
import random
import requests
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()  # reads your .env file

# ============================================================
# CONFIG — put your real keys in a .env file, never in this script
# ============================================================

GEMINI_KEYS = [
    os.getenv("GEMINI_KEY_1"),
    os.getenv("GEMINI_KEY_2"),
    os.getenv("GEMINI_KEY_3"),
    os.getenv("GEMINI_KEY_4"),
]
GEMINI_KEYS = [k for k in GEMINI_KEYS if k]  # drop empty ones

BINANCE_SQUARE_KEY = os.getenv("BINANCE_SQUARE_KEY")

MAX_POSTS_PER_DAY = 40
POST_INTERVAL_MINUTES = 30
MIN_MINUTES_BETWEEN_POSTS = 28  # skip a run if the last post was more recent than this

MEMORY_FILE = "posting_memory.json"
LOG_FILE = "post_log.csv"

BLACKLIST = {
    "USDT", "USDC", "FDUSD", "USD1", "USDE", "USDS",
    "DAI", "TUSD", "BUSD", "USDP", "EUR", "EURC"
}


# ============================================================
# MEMORY — tracks today's posts so we don't repeat / go over the cap
# ============================================================

def load_memory():
    if not os.path.exists(MEMORY_FILE):
        return {"date": str(date.today()), "posted_symbols": [], "count": 0}

    with open(MEMORY_FILE, "r") as f:
        mem = json.load(f)

    # Reset automatically when the day changes
    if mem.get("date") != str(date.today()):
        mem = {
            "date": str(date.today()),
            "posted_symbols": [],
            "count": 0,
            "last_post_ts": mem.get("last_post_ts", 0),
        }

    return mem


def save_memory(mem):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f)


def log_result(row):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a") as f:
        if is_new:
            f.write("date,time,symbol,direction,status,error\n")
        f.write(",".join(str(x) for x in row) + "\n")


# ============================================================
# STEP 1 — MARKET DATA
# ============================================================

def fetch_candidates():
    hosts = [
        "https://data-api.binance.vision",
        "https://api.binance.com",
    ]
    headers = {"User-Agent": "Mozilla/5.0"}

    resp = None
    last_error = None
    for host in hosts:
        for attempt in range(1, 3):
            try:
                resp = requests.get(f"{host}/api/v3/ticker/24hr", headers=headers, timeout=15)
                break
            except requests.exceptions.ConnectionError as e:
                last_error = e
                print(f"⚠️ Connection problem with {host} (try {attempt}/2): {e}")
                time.sleep(3)
        if resp is not None:
            break

    if resp is None:
        raise last_error

    resp.raise_for_status()
    tickers = resp.json()

    candidates = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        base = symbol[:-4]
        if base in BLACKLIST:
            continue

        try:
            change = float(t["priceChangePercent"])
            volume = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        if abs(change) < 3 or volume < 10_000_000:
            continue

        candidates.append({
            "symbol": symbol,
            "base": base,
            "lastPrice": t["lastPrice"],
            "priceChangePercent": t["priceChangePercent"],
            "quoteVolume": t["quoteVolume"],
            "absChange": abs(change),
        })

    candidates.sort(key=lambda c: c["absChange"], reverse=True)
    return candidates[:30]


def pick_eligible_coin(candidates, mem):
    for c in candidates:
        if c["base"] not in mem["posted_symbols"]:
            return c
    return None


# ============================================================
# STEP 2 — ASK GEMINI (with failover across your keys)
# ============================================================

def build_prompt(coin):
    return f"""You are a professional crypto swing trader. Using ONLY the Binance market data below, produce ONE high-quality trade setup.

Market data:
Symbol: {coin['base']}
Current price: {coin['lastPrice']}
24h change: {coin['priceChangePercent']}%
24h quote volume: {coin['quoteVolume']}

Rules:
- Entry must be close to the current price. Derive stop loss and take-profits from the current price. Never invent prices.
- Choose direction from the data (bearish => SHORT, bullish => LONG). Do not always pick LONG.
- Keep technical_thesis to 2-3 sentences: professional, no filler.
- Vary wording so posts never look copied. Keep the whole post about 100-180 words.
Return ONLY valid JSON with keys: symbol, direction, entry, stop_loss, tp1, tp2, tp3, trigger, technical_thesis, risk_note, hashtags (array)."""


def ask_gemini(coin):
    prompt = build_prompt(coin)

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "responseMimeType": "application/json",
        },
    }

    last_error = None

    for i, key in enumerate(GEMINI_KEYS, start=1):
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={key}"
        )
        try:
            resp = requests.post(url, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)

            required = ["symbol", "direction", "entry", "stop_loss", "tp1"]
            if all(parsed.get(k) for k in required):
                parsed["_gemini_key_used"] = i
                return parsed

            last_error = "Gemini response missing required fields"

        except Exception as e:
            last_error = str(e)
            continue  # try the next key

    raise RuntimeError(f"All Gemini keys failed: {last_error}")


# ============================================================
# STEP 3 — FORMAT THE POST
# ============================================================

def calculate_risk_reward(entry, stop_loss, tp1):
    """Computed directly from the numbers, never from the AI, so it's
    always mathematically accurate."""
    try:
        entry_f = float(entry)
        sl_f = float(stop_loss)
        tp1_f = float(tp1)
        risk = abs(entry_f - sl_f)
        reward = abs(tp1_f - entry_f)
        if risk == 0:
            return None
        return round(reward / risk, 2)
    except (ValueError, TypeError):
        return None


CLOSING_LINES = [
    "Where do you see this heading? 👇",
    "Patience pays better than FOMO.",
    "Trade the plan, not your emotions.",
    "Let the market confirm before you act.",
    "Which target are you eyeing first? 👇",
]


def format_post(coin, signal):
    is_short = "SHORT" in signal["direction"].upper()
    sym = coin["base"]
    tags = f"#{sym} #{sym.lower()}"  # Binance Square only allows a couple of hashtags

    header_emoji = "🔻" if is_short else "🚀"
    dot = "🔴" if is_short else "🟢"
    label = "SHORT SETUP" if is_short else "LONG SETUP"
    hook = (
        "Sellers are stepping in — here's the setup. ⚡"
        if is_short else
        "Strong momentum is building — here's the setup. ⚡"
    )

    lines = [f"{header_emoji} ${sym} {label} {dot}", hook, ""]

    if signal.get("technical_thesis"):
        lines += [signal["technical_thesis"], ""]

    # Market context — real numbers, not AI opinion.
    try:
        change = float(coin["priceChangePercent"])
        lines += [f"📊 24h: {change:+.2f}% | Vol: ${float(coin['quoteVolume']):,.0f}", ""]
    except (ValueError, TypeError, KeyError):
        pass

    lines += ["Trade Plan", f"Entry: {signal['entry']}", f"Stop Loss: {signal['stop_loss']}", f"TP1: {signal['tp1']}"]
    if signal.get("tp2"):
        lines.append(f"TP2: {signal['tp2']}")
    if signal.get("tp3"):
        lines.append(f"TP3: {signal['tp3']}")

    rr = calculate_risk_reward(signal["entry"], signal["stop_loss"], signal["tp1"])
    if rr:
        lines += ["", f"⚖️ Risk:Reward (TP1) — 1:{rr}"]

    if signal.get("trigger"):
        lines += ["", "Trigger:", signal["trigger"]]

    if signal.get("risk_note"):
        lines += ["", "Risk Note:", signal["risk_note"]]

    lines += ["", random.choice(CLOSING_LINES), "", tags]

    return "\n".join(lines)


# ============================================================
# STEP 4 — PUBLISH TO BINANCE SQUARE
# ============================================================

def publish_post(coin, signal, post_text):
    url = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
    headers = {
        "X-Square-OpenAPI-Key": BINANCE_SQUARE_KEY,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
    }

    # Officially documented behavior: Binance's backend auto-parses $SYMBOL
    # and #topic mentions found in plain bodyTextOnly — no custom JSON needed.
    payload = {"bodyTextOnly": post_text}

    # Retry a few times if the connection is cut (e.g. "Connection reset by peer")
    resp = None
    last_conn_error = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            break
        except requests.exceptions.ConnectionError as e:
            last_conn_error = e
            print(f"⚠️ Connection problem posting (try {attempt}/3): {e}")
            time.sleep(5)

    if resp is None:
        return False, "", f"Connection failed after 3 tries: {last_conn_error}"

    data = resp.json() if resp.content else {}

    success = data.get("success") is True and str(data.get("code")) == "000000"
    post_id = ""
    if success:
        post_id = str((data.get("data") or {}).get("id", ""))

    return success, post_id, data.get("message", "")


# ============================================================
# ONE FULL CYCLE
# ============================================================

def run_once():
    mem = load_memory()

    if mem["count"] >= MAX_POSTS_PER_DAY:
        print(f"⏸ Daily cap reached ({MAX_POSTS_PER_DAY}). Skipping this run.")
        return

    minutes_since_last = (time.time() - mem.get("last_post_ts", 0)) / 60
    if minutes_since_last < MIN_MINUTES_BETWEEN_POSTS:
        print(f"⏸ Last post was {minutes_since_last:.0f} min ago. Waiting for the 30-minute gap.")
        return

    try:
        candidates = fetch_candidates()
    except Exception as e:
        print("⚠️ Market data step failed:", e)
        return

    coin = pick_eligible_coin(candidates, mem)

    if not coin:
        print("⏸ No eligible coin right now. Skipping this run.")
        return

    try:
        signal = ask_gemini(coin)
    except Exception as e:
        print("⚠️ Gemini failed:", e)
        log_result([date.today(), datetime.now().strftime("%H:%M"), coin["base"], "", "GEMINI_FAILED", str(e)[:200]])
        return

    post_text = format_post(coin, signal)
    success, post_id, error_msg = publish_post(coin, signal, post_text)

    if success:
        mem["posted_symbols"].append(coin["base"])
        mem["count"] += 1
        mem["last_post_ts"] = time.time()
        save_memory(mem)
        print(f"✅ Posted {coin['base']} ({signal['direction']}) — post id {post_id}")
        log_result([date.today(), datetime.now().strftime("%H:%M"), coin["base"], signal["direction"], "SUCCESS", ""])
    else:
        print(f"❌ Publish failed for {coin['base']}: {error_msg}")
        log_result([date.today(), datetime.now().strftime("%H:%M"), coin["base"], signal["direction"], "FAILED", str(error_msg)[:200]])


# ============================================================
# MAIN LOOP — runs every 30 minutes while this script is open
# ============================================================

def main():
    print("🤖 Binance Square bot started.")
    print(f"Posting up to {MAX_POSTS_PER_DAY} times/day, checking every {POST_INTERVAL_MINUTES} minutes.")
    print("Keep this terminal window open and your Mac awake for it to keep running.")
    print("Press Ctrl+C to stop.\n")

    while True:
        try:
            run_once()
        except Exception as e:
            print("⚠️ Unexpected error this cycle:", e)

        time.sleep(POST_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        # Single run, then exit — used by GitHub Actions scheduling.
        run_once()
    else:
        main()
