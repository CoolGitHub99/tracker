"""
Polymarket Top-200 Smart Money Tracker
---------------------------------------
Watches the top 200 most profitable traders on Polymarket and sends a phone
notification (via ntfy.sh) for two specific situations only:

1. MEGA TRADE  — one of them makes a single very large bet
   (could mean they have an edge or inside info).
2. CONSENSUS   — several of them independently buy the same side of the
   same market within a recent time window (the "smart money" is piling in).

Designed to be quiet most of the time — a few alerts a day, not a flood.

This script is meant to be run on a schedule by GitHub Actions, but you can
also run it on your own computer if you set the NTFY_TOPIC environment
variable first.
"""

import os
import json
import time
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---------------------------------------------------------------------------
# CONFIG — these can be overridden with environment variables, no code edits needed
# ---------------------------------------------------------------------------
NUM_TRADERS = int(os.environ.get("NUM_TRADERS", "200"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))

# Alert 1: Mega trade — a single trade at or above this dollar amount.
# Tune this up/down based on how often you actually see alerts (check the
# Actions log). Raise it if you're getting too many, lower it if too few.
BIG_TRADE_THRESHOLD = float(os.environ.get("BIG_TRADE_THRESHOLD", "50000"))

# Alert 2: Consensus — this many or more *different* top traders buying the
# same outcome of the same market within CONSENSUS_WINDOW_HOURS counts as
# "the smart money agrees."
CONSENSUS_MIN_TRADERS = int(os.environ.get("CONSENSUS_MIN_TRADERS", "5"))
CONSENSUS_WINDOW_HOURS = float(os.environ.get("CONSENSUS_WINDOW_HOURS", "6"))
# Once we've alerted about a market+outcome, don't alert again about the
# same one for this many hours (even if more traders keep piling in).
CONSENSUS_COOLDOWN_HOURS = float(os.environ.get("CONSENSUS_COOLDOWN_HOURS", "12"))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
TRADES_URL = "https://data-api.polymarket.com/trades"

STATE_DIR = Path(__file__).parent / "state"
LAST_SEEN_FILE = STATE_DIR / "last_seen.json"
LEADERBOARD_FILE = STATE_DIR / "leaderboard.json"
LAST_RUN_FILE = STATE_DIR / "last_run.json"
RECENT_TRADES_FILE = STATE_DIR / "recent_trades.json"
CONSENSUS_NOTIFIED_FILE = STATE_DIR / "consensus_notified.json"

HEADERS = {"User-Agent": "polymarket-smart-money-tracker/2.0"}


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
    if value >= 250_000:
        return "🐳 MASSIVE bet", 5, "whale"
    if value >= 100_000:
        return "🔥 Huge bet", 5, "fire"
    return "📈 Big bet", 4, "chart_with_upwards_trend"


def send_ntfy(title, message, priority=3, tags="", click_url=None):
    if not NTFY_TOPIC:
        print(f"[info] NTFY_TOPIC not set, would have sent: {title}")
        return
    # Using ntfy's JSON publish format (instead of plain HTTP headers) because
    # headers can't reliably carry emoji/unicode characters (titles with 🐳,
    # 👀, etc. would silently fail to send if put in a header).
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if click_url:
        payload["click"] = click_url
    try:
        requests.post("https://ntfy.sh/", json=payload, headers=HEADERS, timeout=10)
    except Exception as e:
        print(f"[warn] failed to send ntfy notification: {e}")


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    STATE_DIR.mkdir(exist_ok=True)

    last_seen = load_json(LAST_SEEN_FILE, {})
    recent_trades = load_json(RECENT_TRADES_FILE, [])  # sliding window for consensus detection
    consensus_notified = load_json(CONSENSUS_NOTIFIED_FILE, {})  # cooldowns

    first_run = len(last_seen) == 0

    print("Fetching top traders leaderboard...")
    leaderboard = fetch_leaderboard()
    print(f"Got {len(leaderboard)} traders")
    LEADERBOARD_FILE.write_text(json.dumps(leaderboard, indent=2))

    if first_run and leaderboard:
        send_ntfy(
            "✅ Polymarket Tracker is live!",
            f"Watching the top {len(leaderboard)} profitable traders.\n"
            f"You'll be alerted on a single bet of ${BIG_TRADE_THRESHOLD:,.0f}+, "
            f"or when {CONSENSUS_MIN_TRADERS}+ of them buy the same side of the "
            f"same market within {CONSENSUS_WINDOW_HOURS:.0f} hours.",
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
            # Brand new wallet to us — just remember where we are, don't alert
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
    mega_alerts = 0

    # --- Pass 1: update last_seen, send MEGA TRADE alerts, collect new trades ---
    for wallet, latest_ts, new_trades in results:
        if latest_ts is not None:
            new_last_seen[wallet] = latest_ts

        for entry, trade in new_trades:
            size = float(trade.get("size", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            value = size * price
            side = trade.get("side", "?")
            outcome = trade.get("outcome", "?")
            title_text = trade.get("title", "Unknown market")
            condition_id = trade.get("conditionId", "")
            slug = trade.get("eventSlug") or trade.get("slug")
            timestamp = trade.get("timestamp", int(time.time()))

            # Record every BUY into the consensus-tracking window (selling
            # doesn't show the same kind of "conviction" we're looking for)
            if side == "BUY" and condition_id:
                recent_trades.append({
                    "wallet": wallet,
                    "conditionId": condition_id,
                    "outcome": outcome,
                    "title": title_text,
                    "slug": slug,
                    "value": value,
                    "timestamp": timestamp,
                })

            # Mega trade alert
            if value >= BIG_TRADE_THRESHOLD:
                label, priority, tags = classify_value(value)
                click_url = f"https://polymarket.com/event/{slug}" if slug else None
                action_word = "Buying" if side == "BUY" else "Selling"
                send_ntfy(
                    title_text,
                    f"{action_word} {outcome}\n{label} (${value:,.0f})",
                    priority=priority,
                    tags=tags,
                    #click_url=click_url,
                )
                mega_alerts += 1
                time.sleep(0.3)

    # --- Pass 2: prune old trades out of the consensus window ---
    cutoff = time.time() - CONSENSUS_WINDOW_HOURS * 3600
    recent_trades = [t for t in recent_trades if t.get("timestamp", 0) >= cutoff]

    # --- Pass 3: check for consensus (many different traders, same bet) ---
    groups = defaultdict(set)  # (conditionId, outcome) -> set of wallets
    group_info = {}
    for t in recent_trades:
        key = (t["conditionId"], t["outcome"])
        groups[key].add(t["wallet"])
        group_info[key] = t  # keep last seen details (title/slug) for this market+outcome

    consensus_cooldown_cutoff = time.time() - CONSENSUS_COOLDOWN_HOURS * 3600
    consensus_alerts = 0

    for key, wallets in groups.items():
        if len(wallets) < CONSENSUS_MIN_TRADERS:
            continue
        key_str = f"{key[0]}::{key[1]}"
        last_notified = consensus_notified.get(key_str, 0)
        if last_notified >= consensus_cooldown_cutoff:
            continue  # already alerted about this one recently, stay quiet

        info = group_info[key]
        click_url = f"https://polymarket.com/event/{info['slug']}" if info.get("slug") else None
        send_ntfy(
            f"👀 {len(wallets)} top traders agree",
            f"{info['title']}\nBuying {info['outcome']} "
            f"(last {CONSENSUS_WINDOW_HOURS:.0f}h)",
            priority=4,
            tags="eyes",
            #click_url=click_url,
        )
        consensus_notified[key_str] = time.time()
        consensus_alerts += 1
        time.sleep(0.3)

    # Clean out old cooldown entries so the file doesn't grow forever
    consensus_notified = {
        k: v for k, v in consensus_notified.items() if v >= consensus_cooldown_cutoff
    }

    LAST_SEEN_FILE.write_text(json.dumps(new_last_seen, indent=2))
    RECENT_TRADES_FILE.write_text(json.dumps(recent_trades, indent=2))
    CONSENSUS_NOTIFIED_FILE.write_text(json.dumps(consensus_notified, indent=2))
    LAST_RUN_FILE.write_text(json.dumps({"last_run_unix": int(time.time())}, indent=2))
    print(f"Done. Sent {mega_alerts} mega-trade alert(s), {consensus_alerts} consensus alert(s).")


if __name__ == "__main__":
    main()
