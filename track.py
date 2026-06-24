"""
Polymarket Top-500 Whale Tracker
---------------------------------
- Fetches the top 500 most profitable traders on Polymarket (all-time PnL).
- Checks each one's most recent trades.
- Sends a phone notification (via ntfy.sh) for any NEW trade, with a rough
  "how big a deal is this" estimate based on dollars put down.

This script is designed to be run on a schedule by GitHub Actions, but you
can also run it on your own computer if you set the NTFY_TOPIC environment
variable first.
"""

import os
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---------------------------------------------------------------------------
# CONFIG — these can be overridden with environment variables, no code edits needed
# ---------------------------------------------------------------------------
NUM_TRADERS = int(os.environ.get("NUM_TRADERS", "500"))
MIN_NOTIFY_VALUE = float(os.environ.get("MIN_NOTIFY_VALUE", "100"))  # in USD
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
TRADES_URL = "https://data-api.polymarket.com/trades"

STATE_DIR = Path(__file__).parent / "state"
LAST_SEEN_FILE = STATE_DIR / "last_seen.json"
LEADERBOARD_FILE = STATE_DIR / "leaderboard.json"
LAST_RUN_FILE = STATE_DIR / "last_run.json"

HEADERS = {"User-Agent": "polymarket-whale-tracker/1.0"}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def get_json(url, params=None, retries=3):
    """GET a URL as JSON, with simple retries if Polymarket is slow/rate-limiting us."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt == retries - 1:
                print(f"[warn] failed to fetch {url} params={params}: {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_leaderboard():
    """Get the top NUM_TRADERS traders by all-time profit."""
    traders = []
    for offset in range(0, NUM_TRADERS, 50):
        data = get_json(
            LEADERBOARD_URL,
            {
                "category": "OVERALL",
                "timePeriod": "ALL",
                "orderBy": "PNL",
                "limit": 50,
                "offset": offset,
            },
        )
        if data:
            traders.extend(data)
        time.sleep(0.2)
    return traders[:NUM_TRADERS]


def fetch_recent_trades(wallet):
    data = get_json(TRADES_URL, {"user": wallet, "limit": 20})
    return data or []


def classify_value(value):
    """Rough 'how big a deal is this trade' label based on dollars put down."""
    if value >= 100_000:
        return "🐳 HUGE WHALE TRADE", 5, "whale"
    if value >= 25_000:
        return "🔥 Very large trade", 4, "fire"
    if value >= 5_000:
        return "📈 Large trade", 4, "chart_with_upwards_trend"
    if value >= 1_000:
        return "📊 Medium trade", 3, "bar_chart"
    return "Small trade", 2, "small_blue_diamond"


def send_ntfy(title, message, priority=3, tags="", click_url=None):
    if not NTFY_TOPIC:
        print(f"[info] NTFY_TOPIC not set, would have sent: {title}")
        return
    headers = {"Title": title, "Priority": str(priority)}
    if tags:
        headers["Tags"] = tags
    if click_url:
        headers["Click"] = click_url
    try:
        requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        print(f"[warn] failed to send ntfy notification: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    STATE_DIR.mkdir(exist_ok=True)

    last_seen = {}
    if LAST_SEEN_FILE.exists():
        try:
            last_seen = json.loads(LAST_SEEN_FILE.read_text())
        except Exception:
            last_seen = {}

    first_run = len(last_seen) == 0

    print("Fetching top traders leaderboard...")
    leaderboard = fetch_leaderboard()
    print(f"Got {len(leaderboard)} traders")
    LEADERBOARD_FILE.write_text(json.dumps(leaderboard, indent=2))

    if first_run and leaderboard:
        send_ntfy(
            "✅ Polymarket Tracker is live!",
            f"Now watching the top {len(leaderboard)} profitable traders. "
            f"You'll get a notification whenever one of them makes a trade "
            f"worth ${MIN_NOTIFY_VALUE:,.0f} or more.",
            priority=3,
        )

    def process_trader(entry):
        wallet = entry.get("proxyWallet")
        if not wallet:
            return None
        trades = fetch_recent_trades(wallet)
        if not trades:
            return wallet, last_seen.get(wallet), []

        trades.sort(key=lambda t: t.get("timestamp", 0))
        latest_ts = trades[-1].get("timestamp", 0)

        prev_ts = last_seen.get(wallet)
        if prev_ts is None:
            # Brand new wallet to us — just remember where we are, don't notify
            # (otherwise everyone's entire trade history would ping you on day 1)
            return wallet, latest_ts, []

        new_trades = [t for t in trades if t.get("timestamp", 0) > prev_ts]
        return wallet, latest_ts, [(entry, t) for t in new_trades]

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_trader, e) for e in leaderboard]
        for f in as_completed(futures):
            res = f.result()
            if res:
                results.append(res)

    new_last_seen = dict(last_seen)
    notify_count = 0

    for wallet, latest_ts, new_trades in results:
        if latest_ts is not None:
            new_last_seen[wallet] = latest_ts

        for entry, trade in new_trades:
            username = entry.get("userName") or wallet[:10]
            rank = entry.get("rank")
            size = float(trade.get("size", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            value = size * price
            if value < MIN_NOTIFY_VALUE:
                continue

            side = trade.get("side", "?")
            outcome = trade.get("outcome", "?")
            title_text = trade.get("title", "Unknown market")
            slug = trade.get("eventSlug") or trade.get("slug")
            label, priority, tags = classify_value(value)
            click_url = f"https://polymarket.com/event/{slug}" if slug else None

            notif_title = f"{username} (#{rank}) {side} {outcome}"
            notif_body = (
                f"{title_text}\n"
                f"${value:,.0f} put down ({size:,.0f} shares @ ${price:.3f})\n"
                f"{label}"
            )
            send_ntfy(notif_title, notif_body, priority=priority, tags=tags, click_url=click_url)
            notify_count += 1
            time.sleep(0.3)

    LAST_SEEN_FILE.write_text(json.dumps(new_last_seen, indent=2))
    LAST_RUN_FILE.write_text(json.dumps({"last_run_unix": int(time.time())}, indent=2))
    print(f"Done. Sent {notify_count} notifications.")


if __name__ == "__main__":
    main()
