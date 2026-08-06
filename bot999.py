#!/usr/bin/env python3
"""
999.md apartment watcher -> Telegram alerts.

Watches 999.md for NEW monthly-rent apartment listings matching your filters
and pushes each new one to a Telegram chat. Dedupes via a local seen-ids file,
so you only ever get pinged once per listing.

Uses 999.md's own GraphQL endpoint (the one their website calls) - no HTML
scraping, no API key, no login required.

Python 3.8+, standard library only. No pip install needed.

Usage:
    python3 bot999.py --seed      # first run: remember what's already there, send nothing
    python3 bot999.py             # normal run: alert on anything new
    python3 bot999.py --dry-run   # print matches, send nothing, don't touch the store
    python3 bot999.py --chat-id   # helper: print the chat id of whoever messaged your bot
"""

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# CONFIG - edit here, or override with environment variables
# --------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")   # from @BotFather
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # your chat id

# Price band, in EUR per month
PRICE_MIN = float(os.environ.get("PRICE_MIN", 300))
PRICE_MAX = float(os.environ.get("PRICE_MAX", 400))

# Also match listings priced in MDL / USD, converted to EUR at these rates.
# Set MATCH_OTHER_CURRENCIES=0 to only ever match listings priced in EUR.
MATCH_OTHER_CURRENCIES = os.environ.get("MATCH_OTHER_CURRENCIES", "1") != "0"
MDL_PER_EUR = float(os.environ.get("MDL_PER_EUR", 19.4))
USD_PER_EUR = float(os.environ.get("USD_PER_EUR", 1.09))

# Optional: only these room counts, e.g. "1,2,3". Empty = any.
ROOMS_ALLOWED = [s.strip() for s in os.environ.get("ROOMS", "").split(",") if s.strip()]

# Two scan modes, so a frequent schedule stays cheap and polite:
#   QUICK - just page 1 (newest listings land at the top; 1 request)
#   FULL  - page through the entire filtered set (~5 requests), which also
#           catches flats whose price was edited down into your band
# A full sweep runs automatically if the last one was over FULL_SWEEP_EVERY_MIN
# minutes ago; every other run is quick.
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", 100))
MAX_PAGES = int(os.environ.get("MAX_PAGES", 20))
FULL_SWEEP_EVERY_MIN = int(os.environ.get("FULL_SWEEP_EVERY_MIN", 60))

# Never fire more than this many messages in one run (flood guard).
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", 15))

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("seen.json")))

# --------------------------------------------------------------------------
# 999.md filter ids (reverse-engineered from their own site requests)
# --------------------------------------------------------------------------

GRAPHQL_URL = "https://999.md/graphql"
AD_URL = "https://999.md/ro/{id}"
SUBCATEGORY_APARTMENTS = 1404

# Offer type (filterId 16 / featureId 1)
OFFER_MONTHLY_RENT = 912   # "De inchiriat lunar"

# Chisinau sectors / districts (filterId 32 / featureId 9)
REGIONS = {
    "Centru": 15664,        # note: several localities elsewhere are also named "Centru"
    "Botanica": 15665,
    "Buiucani": 15666,
    "Rascani": 15667,
    "Telecentru": 15668,
    "Ciocana": 15669,
    "Posta Veche": 15670,
    "Sculeni": 15671,
    "Aeroport": 15672,
    "BAM": 15675,
    "Paminteni": 15676,
    "6 cartier": 15677,
    "7 cartier": 15678,
    "8 cartier": 15679,
    "9 cartier": 15680,
}
WATCH_REGIONS = [s.strip() for s in
                 os.environ.get("REGIONS_WATCHED", "Rascani,Ciocana,Posta Veche").split(",")
                 if s.strip()]

# Building type (filterId 2307 / featureId 852)
BUILDING_NEW = 19108   # "Constructii noi"  (Secundar is the other option)

# Feature ids used to read values off each ad
F_PRICE, F_REGION, F_STREET, F_HOUSE_NO = 2, 9, 10, 11
F_PHONES, F_ROOMS, F_AREA, F_FLOOR, F_FLOORS, F_CONDITION, F_BUILDING = 16, 241, 244, 248, 249, 253, 852

QUERY = """
query SearchAds($input: Ads_SearchInput!, $locale: Common_Locale) {
  searchAds(input: $input) {
    count
    ads {
      id
      title
      price: feature(id: %d) { value }
      region: feature(id: %d) { value }
      street: feature(id: %d) { value }
      houseNo: feature(id: %d) { value }
      phones: feature(id: %d) { value }
      rooms: feature(id: %d) { value }
      area: feature(id: %d) { value }
      floor: feature(id: %d) { value }
      floors: feature(id: %d) { value }
      condition: feature(id: %d) { value }
      building: feature(id: %d) { value }
      posted: reseted(input: {format: "02.01.2006 15:04", locale: $locale, timezone: "Europe/Chisinau"})
    }
  }
}
""" % (F_PRICE, F_REGION, F_STREET, F_HOUSE_NO, F_PHONES, F_ROOMS,
       F_AREA, F_FLOOR, F_FLOORS, F_CONDITION, F_BUILDING)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_listings(quick=False):
    """Listings matching the server-side filters, newest first.

    quick=True fetches only the first page - enough to spot brand-new posts.
    quick=False pages through the whole result set.
    """
    region_ids = []
    for name in WATCH_REGIONS:
        if name not in REGIONS:
            print(f"[warn] unknown region '{name}' - known: {', '.join(REGIONS)}", file=sys.stderr)
            continue
        region_ids.append(REGIONS[name])

    filters = [{"filterId": 16, "features": [{"featureId": 1, "optionIds": [OFFER_MONTHLY_RENT]}]},
               {"filterId": 2307, "features": [{"featureId": 852, "optionIds": [BUILDING_NEW]}]}]
    if region_ids:
        filters.insert(1, {"filterId": 32, "features": [{"featureId": 9, "optionIds": region_ids}]})

    ads, skip, total = [], 0, None
    for _ in range(1 if quick else MAX_PAGES):
        payload = {
            "operationName": "SearchAds",
            "query": QUERY,
            "variables": {
                "locale": "ro_RO",
                "input": {
                    "source": "AD_SOURCE_DESKTOP_REDESIGN",
                    "sort": "SORT_ADS_DATE_DESC",
                    "pagination": {"limit": PAGE_SIZE, "skip": skip},
                    "subCategoryId": SUBCATEGORY_APARTMENTS,
                    "filters": filters,
                },
            },
        }
        res = post_json(GRAPHQL_URL, payload, headers={"lang": "ro", "source": "desktop_redesign"})
        if res.get("errors"):
            raise RuntimeError("999.md GraphQL error: " + json.dumps(res["errors"])[:400])
        page = res["data"]["searchAds"]["ads"]
        total = res["data"]["searchAds"]["count"]
        ads.extend(page)
        skip += PAGE_SIZE
        if quick or len(page) < PAGE_SIZE or skip >= (total or 0):
            break
        time.sleep(0.5)   # be polite to 999.md
    else:
        print(f"[warn] hit MAX_PAGES={MAX_PAGES}; {total} listings exist, only checked {len(ads)}",
              file=sys.stderr)

    # de-dupe defensively: paging a live, re-sorting list can repeat an ad
    unique, seen_ids = [], set()
    for ad in ads:
        if ad["id"] not in seen_ids:
            seen_ids.add(ad["id"])
            unique.append(ad)
    return unique


# --------------------------------------------------------------------------
# Parsing / matching
# --------------------------------------------------------------------------

def _val(feature):
    return feature.get("value") if feature else None


def price_in_eur(ad):
    """(amount_eur, 'raw label') or (None, label) if we can't compare it."""
    p = _val(ad.get("price"))
    if not p or p.get("value") in (None, 0):
        return None, "pret nespecificat"
    amount, unit = float(p["value"]), p.get("unit", "")
    if unit == "UNIT_EUR":
        return amount, f"{amount:,.0f} EUR".replace(",", " ")
    if not MATCH_OTHER_CURRENCIES:
        return None, f"{amount:,.0f} {unit.replace('UNIT_', '')}".replace(",", " ")
    if unit == "UNIT_MDL":
        return amount / MDL_PER_EUR, f"{amount:,.0f} MDL (~{amount / MDL_PER_EUR:,.0f} EUR)".replace(",", " ")
    if unit == "UNIT_USD":
        return amount / USD_PER_EUR, f"{amount:,.0f} USD (~{amount / USD_PER_EUR:,.0f} EUR)".replace(",", " ")
    return None, f"{amount:,.0f} {unit.replace('UNIT_', '')}".replace(",", " ")


def translated(feature):
    v = _val(feature)
    if isinstance(v, dict):
        return v.get("translated")
    return v if isinstance(v, str) else None


def matches(ad):
    eur, _ = price_in_eur(ad)
    if eur is None or not (PRICE_MIN <= eur <= PRICE_MAX):
        return False
    if ROOMS_ALLOWED:
        rooms = translated(ad.get("rooms")) or ""
        if not any(r in rooms for r in ROOMS_ALLOWED):
            return False
    return True


def describe(ad):
    _, price_label = price_in_eur(ad)
    rooms = translated(ad.get("rooms")) or "apartament"
    region = translated(ad.get("region")) or ""
    street = translated(ad.get("street")) or ""
    house = translated(ad.get("houseNo")) or ""
    address = " ".join(x for x in (street, house) if x)

    area = _val(ad.get("area"))
    area_txt = f"{area['value']:g} m2" if isinstance(area, dict) and area.get("value") else ""
    floor = translated(ad.get("floor"))
    floors = translated(ad.get("floors"))
    floor_txt = f"etaj {floor}/{floors}" if floor and floors else (f"etaj {floor}" if floor else "")
    condition = translated(ad.get("condition")) or ""

    phones = _val(ad.get("phones")) or {}
    phone_list = phones.get("phone_numbers", []) if isinstance(phones, dict) else []
    phone_txt = ", ".join("+" + p.lstrip("+") for p in phone_list[:2])

    e = html.escape
    lines = [f"<b>{e(rooms)} - {e(price_label)}/luna</b>"]
    meta = " | ".join(x for x in (region, address, area_txt, floor_txt, condition) if x)
    if meta:
        lines.append(e(meta))
    if phone_txt:
        lines.append(f"tel: {e(phone_txt)}")
    posted = ad.get("posted")
    if posted:
        lines.append(f"<i>publicat {e(str(posted))}</i>")
    lines.append(AD_URL.format(id=ad["id"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram(method, payload):
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is not set. See README.md.")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    for attempt in range(3):
        try:
            return post_json(url, payload)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429 and attempt < 2:
                retry = json.loads(body).get("parameters", {}).get("retry_after", 5)
                time.sleep(retry + 1)
                continue
            raise SystemExit(f"Telegram {method} failed ({exc.code}): {body[:300]}")


def send(text):
    telegram("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })


def print_chat_id():
    res = telegram("getUpdates", {})
    seen = {}
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
    if not seen:
        print("No messages yet. Open your bot in Telegram, press Start / send 'hi', then re-run.")
    for cid, who in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({who})")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {"seen": [], "last_full_sweep": 0}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"seen": [], "last_full_sweep": 0}


def save_state(ids, last_full_sweep):
    # keep the store bounded - 5000 ids is far more than we ever need
    STATE_FILE.write_text(json.dumps({
        "seen": sorted(ids)[-5000:],
        "last_full_sweep": last_full_sweep,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=0))


def due_for_full_sweep(state):
    return (time.time() - state.get("last_full_sweep", 0)) >= FULL_SWEEP_EVERY_MIN * 60


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="999.md apartment watcher -> Telegram")
    ap.add_argument("--seed", action="store_true",
                    help="record current listings as already-seen without notifying")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent; don't send, don't save state")
    ap.add_argument("--chat-id", action="store_true",
                    help="print the chat id of whoever has messaged your bot, then exit")
    ap.add_argument("--quick", action="store_true",
                    help="force a page-1-only scan")
    ap.add_argument("--full", action="store_true",
                    help="force a full sweep of every page")
    args = ap.parse_args()

    if args.chat_id:
        print_chat_id()
        return

    state = load_state()
    seen = set(state.get("seen", []))
    last_full = state.get("last_full_sweep", 0)

    # --seed and --dry-run always look at everything; otherwise a full sweep
    # happens on schedule and every other run is a cheap page-1 check.
    if args.full or args.seed or args.dry_run:
        quick = False
    elif args.quick:
        quick = True
    else:
        quick = not due_for_full_sweep(state)

    ads = fetch_listings(quick=quick)
    if not quick:
        last_full = time.time()
    hits = [a for a in ads if matches(a)]
    fresh = [a for a in hits if a["id"] not in seen]

    print(f"{'quick' if quick else 'full'} scan | fetched {len(ads)} listings | "
          f"{len(hits)} in {PRICE_MIN:.0f}-{PRICE_MAX:.0f} EUR | {len(fresh)} new")

    if args.dry_run:
        for ad in hits:
            print("-" * 60)
            print(describe(ad).replace("<b>", "").replace("</b>", "")
                  .replace("<i>", "").replace("</i>", ""))
        return

    if args.seed:
        save_state(seen | {a["id"] for a in hits}, last_full)
        print(f"seeded {len(hits)} matching ids - future runs will only alert on new matches")
        return

    if not TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_CHAT_ID is not set. Run: python3 bot999.py --chat-id")

    for ad in reversed(fresh[:MAX_ALERTS_PER_RUN]):   # oldest first
        send(describe(ad))
        time.sleep(1)
    if len(fresh) > MAX_ALERTS_PER_RUN:
        send(f"...si inca {len(fresh) - MAX_ALERTS_PER_RUN} anunturi noi "
             f"(limita per rulare). Ridica MAX_ALERTS_PER_RUN daca vrei toate.")

    # Only MATCHING listings are remembered. A flat currently priced above your
    # band stays "unseen", so if the landlord later drops it into range, that
    # counts as new and you get pinged. Price drops are the point.
    save_state(seen | {a["id"] for a in hits}, last_full)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        raise SystemExit(f"network error talking to 999.md: {exc}")
