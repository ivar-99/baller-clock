import requests
import json
import os
import re
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# ─── CONFIG ───────────────────────────────────────────────────────────────────
RESULTS_URL = "https://monopolybigballer.com/results/"
STATE_FILE  = "bonus_state.json"
IST         = timezone(timedelta(hours=5, minutes=30))  # Indian Standard Time
MAX_HISTORY = 200  # keep last 200 bonus events

# ─── SCRAPER ──────────────────────────────────────────────────────────────────

def scrape_bonus_rounds():
    """
    Use Playwright to scrape the JS-rendered results table.
    Returns only rows where bonus_game is '3 Rolls' or '5 Rolls'.
    Each row: { "utc_time", "ist_time", "bonus_type", "multiplier", "payout" }
    """
    bonus_rounds = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(RESULTS_URL, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(4000)  # wait for JS table to render

            rows = page.query_selector_all("table tr")
            print(f"  Found {len(rows)} table rows total")

            for row in rows[1:]:  # skip header
                cells = row.query_selector_all("td")
                if len(cells) < 4:
                    continue

                finished  = cells[0].inner_text().strip()
                bonus     = cells[2].inner_text().strip()
                payout    = cells[3].inner_text().strip()

                # Only care about 3 Rolls or 5 Rolls
                bonus_type = None
                if "5" in bonus and "roll" in bonus.lower():
                    bonus_type = "5 Rolls"
                elif "3" in bonus and "roll" in bonus.lower():
                    bonus_type = "3 Rolls"

                if not bonus_type or not finished:
                    continue

                # Extract multiplier from payout string e.g. "232x" or "45x"
                multiplier = None
                m = re.search(r'(\d+)x', payout, re.IGNORECASE)
                if m:
                    multiplier = int(m.group(1))

                # Parse the time — site shows times like "14:32" or "Jan 13 – 14:32"
                utc_time = parse_time_to_utc(finished)
                ist_time = utc_time.astimezone(IST) if utc_time else None

                bonus_rounds.append({
                    "raw_time":   finished,
                    "utc_time":   utc_time.isoformat() if utc_time else None,
                    "ist_time":   ist_time.strftime("%d %b %Y, %I:%M %p IST") if ist_time else finished,
                    "ist_iso":    ist_time.isoformat() if ist_time else None,
                    "bonus_type": bonus_type,
                    "multiplier": multiplier,
                    "payout":     payout,
                })

            browser.close()

    except Exception as e:
        print(f"⚠️  Scrape error: {e}")

    print(f"  Found {len(bonus_rounds)} bonus rounds (3/5 Rolls) on page")
    return bonus_rounds


def parse_time_to_utc(time_str: str):
    """
    Try to parse various time formats from the site into a UTC datetime.
    Site typically shows: "14:32", "13 Jan – 14:32", "Jan 13 – 01:58"
    """
    now = datetime.now(timezone.utc)
    try:
        # Format: "HH:MM" — assume today UTC
        if re.match(r'^\d{1,2}:\d{2}$', time_str):
            t = datetime.strptime(time_str, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day,
                tzinfo=timezone.utc
            )
            return t

        # Format: "13 Jan 2026 – 14:32" or "13 Jan – 14:32"
        m = re.search(r'(\d{1,2})\s+([A-Za-z]+)[\s–-]+(\d{1,2}):(\d{2})', time_str)
        if m:
            day, mon, hh, mm = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
            year = now.year
            dt = datetime.strptime(f"{day} {mon} {year} {hh}:{mm}", "%d %b %Y %H:%M")
            return dt.replace(tzinfo=timezone.utc)

        # Format: "Jan 13 – 01:58"
        m2 = re.search(r'([A-Za-z]+)\s+(\d{1,2})[\s–-]+(\d{1,2}):(\d{2})', time_str)
        if m2:
            mon, day, hh, mm = m2.group(1), m2.group(2), int(m2.group(3)), int(m2.group(4))
            year = now.year
            dt = datetime.strptime(f"{day} {mon} {year} {hh}:{mm}", "%d %b %Y %H:%M")
            return dt.replace(tzinfo=timezone.utc)

    except Exception as e:
        print(f"  ⚠️  Could not parse time '{time_str}': {e}")

    return None


# ─── ANALYSIS ENGINE ──────────────────────────────────────────────────────────

def analyse(history: list[dict]) -> dict:
    """
    Separate 3 Rolls and 5 Rolls histories.
    Calculate average interval, last seen time, and predicted next occurrence in IST.
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)

    three_rolls = [r for r in history if r["bonus_type"] == "3 Rolls"]
    five_rolls  = [r for r in history if r["bonus_type"] == "5 Rolls"]

    def compute_stats(rounds):
        if not rounds:
            return None

        # Get multipliers
        mults = [r["multiplier"] for r in rounds if r.get("multiplier")]
        avg_mult = round(sum(mults) / len(mults), 1) if mults else None
        max_mult = max(mults) if mults else None

        # Compute intervals between consecutive occurrences
        intervals_min = []
        times_with_iso = [r for r in rounds if r.get("utc_time")]
        times_with_iso.sort(key=lambda x: x["utc_time"])

        for i in range(1, len(times_with_iso)):
            t1 = datetime.fromisoformat(times_with_iso[i-1]["utc_time"])
            t2 = datetime.fromisoformat(times_with_iso[i]["utc_time"])
            diff = (t2 - t1).total_seconds() / 60
            if 0 < diff < 1440:  # only gaps < 24 hours (ignore day gaps)
                intervals_min.append(diff)

        avg_interval = round(sum(intervals_min) / len(intervals_min), 1) if intervals_min else None
        min_interval = round(min(intervals_min), 1) if intervals_min else None
        max_interval = round(max(intervals_min), 1) if intervals_min else None

        # Last occurrence
        last = times_with_iso[-1] if times_with_iso else rounds[-1]
        last_ist = last.get("ist_time", "Unknown")
        last_utc_str = last.get("utc_time")

        # Time since last occurrence
        minutes_since = None
        if last_utc_str:
            last_dt = datetime.fromisoformat(last_utc_str)
            minutes_since = round((now_utc - last_dt).total_seconds() / 60, 1)

        # Predicted next in IST
        predicted_next_ist = None
        predicted_in_minutes = None
        if last_utc_str and avg_interval:
            last_dt = datetime.fromisoformat(last_utc_str)
            predicted_utc = last_dt + timedelta(minutes=avg_interval)
            predicted_ist = predicted_utc.astimezone(IST)
            predicted_next_ist = predicted_ist.strftime("%d %b %Y, %I:%M %p IST")
            predicted_in_minutes = round((predicted_utc - now_utc).total_seconds() / 60, 1)

        return {
            "total_count":         len(rounds),
            "avg_multiplier":      avg_mult,
            "max_multiplier":      max_mult,
            "avg_interval_min":    avg_interval,
            "min_interval_min":    min_interval,
            "max_interval_min":    max_interval,
            "last_seen_ist":       last_ist,
            "minutes_since_last":  minutes_since,
            "predicted_next_ist":  predicted_next_ist,
            "predicted_in_minutes": predicted_in_minutes,
            "recent_10":           [
                {
                    "ist_time":   r.get("ist_time", r.get("raw_time")),
                    "multiplier": r.get("multiplier"),
                    "payout":     r.get("payout", "")
                }
                for r in rounds[-10:][::-1]  # newest first
            ]
        }

    return {
        "now_ist":     now_ist.strftime("%d %b %Y, %I:%M:%S %p IST"),
        "three_rolls": compute_stats(three_rolls),
        "five_rolls":  compute_stats(five_rolls),
    }


# ─── MAIN PIPELINE ────────────────────────────────────────────────────────────

def run_pipeline():
    print("🚀 BALLER CLOCK — BONUS TRACKER RUNNING...")
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    print(f"🕒 Current IST: {now_ist.strftime('%d %b %Y, %I:%M %p')}")

    # 1. Load existing history
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        history = state.get("history", [])
        print(f"📂 Loaded {len(history)} existing bonus events.")
    except FileNotFoundError:
        history = []
        print("📂 No state file. Starting fresh.")

    # 2. Scrape new data
    print("🌐 Scraping live results...")
    new_rounds = scrape_bonus_rounds()

    # 3. Deduplicate
    existing_keys = {(r.get("raw_time"), r.get("bonus_type")) for r in history}
    added = 0
    for r in new_rounds:
        key = (r.get("raw_time"), r.get("bonus_type"))
        if key not in existing_keys:
            history.append(r)
            existing_keys.add(key)
            added += 1
    print(f"➕ Added {added} new bonus events.")

    # 4. Trim
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    # 5. Analyse
    print("🧮 Analysing patterns...")
    analytics = analyse(history)

    # Print summary
    if analytics["three_rolls"]:
        t = analytics["three_rolls"]
        print(f"🎲 3 Rolls → Last: {t['last_seen_ist']} | Avg interval: {t['avg_interval_min']} min | Predicted next: {t['predicted_next_ist']}")
    if analytics["five_rolls"]:
        f = analytics["five_rolls"]
        print(f"🎰 5 Rolls → Last: {f['last_seen_ist']} | Avg interval: {f['avg_interval_min']} min | Predicted next: {f['predicted_next_ist']}")

    # 6. Save
    output = {
        "last_updated_ist": analytics["now_ist"],
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        "analytics":        analytics,
        "history":          history
    }
    with open(STATE_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"💾 Saved. Total bonus events: {len(history)}")
    print("✅ Done.")


if __name__ == "__main__":
    run_pipeline()
