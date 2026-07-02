#!/usr/bin/env python3
"""
Pokemon Drop Notifier
=====================

Watches one or more shops for Pokémon products and sends a WhatsApp message via
CallMeBot whenever a matching product:

  * appears as a NEW listing, or
  * goes from out-of-stock -> in-stock (a restock).

Each shop is handled by a small "adapter". Adding a site = adding one entry to
the SITES config (env var SITES / sites.local.json), no new code needed for a
site that uses an adapter we already have.

Adapters currently supported (the "type" field):
  * "woocommerce" - any WooCommerce shop's Store API (clean JSON).
                    e.g. cardcollectors.ch
  * "wog"         - wog.ch's ajax.search endpoint (JSON).
  * "shopify"     - any Shopify shop's <collection>/products.json feed.
                    e.g. wellplayed.ch

State (what we saw last run) is kept in a small JSON file so we only alert on
*changes*. On GitHub Actions that file is persisted via the Actions cache.
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

import requests

# Make console output UTF-8 safe (Windows defaults to cp1252, which can't
# print emoji and would crash). line_buffering matters in loop mode: without
# it, GitHub Actions buffers stdout and the log looks dead for hours.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) PokemonDropNotifier/2.0"
)

# CallMeBot asks you to space messages out; this avoids being rate-limited.
SEND_DELAY_SECONDS = 4

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# Treated as "in stock" for the wog.ch adapter (green availability states).
WOG_IN_STOCK = {"in stock normally", "in external stock"}


# --------------------------------------------------------------------------- #
# Site configuration
# --------------------------------------------------------------------------- #
# Default sites. Override the whole list via the SITES env var (a JSON array)
# or a local sites.local.json file — handy for testing or adding shops without
# touching this file.

DEFAULT_SITES = [
    {
        "id": "cardcollectors",
        "type": "woocommerce",
        "label": "CardCollectors",
        # brand=3038 is the Pokémon brand on cardcollectors.ch.
        "api_url": "https://cardcollectors.ch/wp-json/wc/store/v1/products"
                   "?brand=3038&orderby=date&order=desc",
        "brand_id": "3038",      # defence-in-depth client-side check ("" disables)
        "name_filter": "(EN)",   # case-insensitive substring the title must contain
    },
    {
        "id": "wog",
        "type": "wog",
        "label": "WOG.ch",
        "platform_id": "tc",            # "tc" = Trading Cards
        "tag": "392",                   # "Pokémon TCG" genre tag (full catalog)
        "order_by": "releasedate",
        "name_filter": "-EN-",          # matched against seriesName (keeps the token)
        "match_field": "seriesName",
        "platform_name": "Trading Cards",
        "max_pages": 8,
    },
    {
        "id": "wellplayed",
        "type": "shopify",
        "label": "WellPlayed",
        "collection_url": "https://www.wellplayed.ch/collections/pokemon",
        "name_filter": "(EN)",   # English Pokémon TCG products carry "(EN)"
    },
    {
        "id": "laschocards",
        "type": "shopify",
        "label": "Laschocards (pre-order)",
        "collection_url": "https://laschocards.ch/en/collections/pre-order",
        # Language is a Shopify variant here, not in the title. Track the
        # English variant specifically (its own stock + ?variant= link).
        "variant_filter": "English",
        # Pre-order shop: it "restocks" the same item at rising prices. Only the
        # FIRST appearance is at retail, so alert on new listings ONLY.
        "alert_on": ["new"],
    },
]


def _local_json(path: str) -> dict | list | None:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def load_sites() -> list[dict]:
    raw = os.environ.get("SITES")
    if raw:
        return json.loads(raw)
    local = _local_json("sites.local.json")
    if isinstance(local, list):
        return local
    return DEFAULT_SITES


def load_recipients() -> list[dict]:
    """
    CALLMEBOT_RECIPIENTS is a JSON array, e.g.:
        [{"name": "Me", "phone": "+41791234567", "apikey": "123456"},
         {"name": "Alex", "phone": "+41799999999", "apikey": "789012"}]
    """
    raw = os.environ.get("CALLMEBOT_RECIPIENTS")
    if not raw:
        local = _local_json("config.local.json")
        if isinstance(local, dict):
            raw = local.get("CALLMEBOT_RECIPIENTS")
    if not raw:
        return []
    recipients = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(recipients, list):
        raise ValueError("CALLMEBOT_RECIPIENTS must be a JSON array")
    return recipients


def load_telegram() -> dict | None:
    """
    Telegram config:
        TELEGRAM_BOT_TOKEN  - the token from @BotFather
        TELEGRAM_CHAT_IDS   - who to message: a JSON array OR a comma-separated
                              list of chat ids. A personal chat id is a positive
                              number; a group/channel id is negative.
    Returns None if not configured.
    """
    def _val(key):
        v = os.environ.get(key)
        if not v:
            local = _local_json("config.local.json")
            if isinstance(local, dict):
                v = local.get(key)
        return v

    token = _val("TELEGRAM_BOT_TOKEN")
    raw_ids = _val("TELEGRAM_CHAT_IDS")
    if not token or not raw_ids:
        return None

    ids: list = []
    if isinstance(raw_ids, list):
        ids = raw_ids
    else:
        raw_ids = str(raw_ids).strip()
        try:
            parsed = json.loads(raw_ids)
            ids = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            ids = [part for part in raw_ids.split(",")]
    chat_ids = [str(i).strip() for i in ids if str(i).strip()]
    if not chat_ids:
        return None
    return {"token": str(token), "chat_ids": chat_ids}


# --------------------------------------------------------------------------- #
# Normalized item shape
# --------------------------------------------------------------------------- #
# Every adapter returns a list of dicts with this shape. The "key" is namespaced
# by site id so products from different shops never collide in the state file.
#
#   {key, label, name, in_stock, price, availability, link}


def make_item(site, pid, name, in_stock, price, availability, link) -> dict:
    return {
        "key": f"{site['id']}:{pid}",
        "label": site["label"],
        "name": html.unescape((name or "").strip()),
        "in_stock": bool(in_stock),
        "price": price or "",
        "availability": availability or "",
        "link": link or "",
        # Which transitions should alert for this site:
        #   "new"     -> a listing we've never seen before
        #   "restock" -> a known listing going out-of-stock -> in-stock
        # Defaults to both. Some sites (e.g. pre-order shops that "restock" the
        # same product at ever-higher prices) only want "new".
        "alert_on": list(site.get("alert_on") or ["new", "restock"]),
    }


# --------------------------------------------------------------------------- #
# Adapter: WooCommerce Store API
# --------------------------------------------------------------------------- #

def fetch_woocommerce(site: dict, session: requests.Session) -> list[dict]:
    api_url = site["api_url"]
    name_filter = (site.get("name_filter") or "").lower()
    brand_id = str(site.get("brand_id") or "")
    per_page = int(site.get("per_page", 100))

    sep = "&" if "?" in api_url else "?"
    items: list[dict] = []
    page = 1
    while True:
        url = f"{api_url}{sep}per_page={per_page}&page={page}"
        resp = session.get(url, timeout=30, headers={"Accept": "application/json"})
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        for p in batch:
            name = p.get("name") or ""
            if name_filter and name_filter not in name.lower():
                continue
            if brand_id:
                brand_ids = {str(b.get("id")) for b in (p.get("brands") or [])}
                if brand_id not in brand_ids:
                    continue
            prices = p.get("prices") or {}
            items.append(make_item(
                site, p.get("id"), name,
                in_stock=p.get("is_in_stock"),
                price=_woo_price(prices),
                availability=(p.get("stock_availability") or {}).get("text", ""),
                link=p.get("permalink"),
            ))
        if len(batch) < per_page:
            break
        page += 1
        if page > 50:  # safety stop
            break
    return items


def _woo_price(prices: dict) -> str:
    amount = prices.get("price")
    if amount is None:
        return ""
    try:
        minor = int(prices.get("currency_minor_unit", 2))
        symbol = (prices.get("currency_symbol") or "").strip()
        return f"{symbol} {int(amount) / (10 ** minor):.2f}".strip()
    except (ValueError, TypeError):
        return ""


# --------------------------------------------------------------------------- #
# Adapter: wog.ch ajax.productList
# --------------------------------------------------------------------------- #
# wog.ch is JavaScript-rendered, but its grid is filled by POSTing to the same
# `ajax.productList` endpoint the site itself uses. We browse a platform + a
# genre tag (e.g. Trading Cards + "Pokémon TCG" tag 392) to get the *complete*
# catalog with stable pagination — far more reliable than the search endpoint.
#
# Language note: the display `title` has the language suffix (e.g. "-EN-")
# STRIPPED, but `seriesName` keeps it. So we match the language filter against
# `seriesName`, not `title`. (English products also carry areaCodeText
# "US-Version"/"UK-Version"; German ones have none.)

def fetch_wog(site: dict, session: requests.Session) -> list[dict]:
    platform_id = site.get("platform_id", "tc")        # "tc" = Trading Cards
    tag = str(site.get("tag", "392"))                  # 392 = Pokémon TCG genre
    order_by = site.get("order_by", "releasedate")
    name_filter = site.get("name_filter", "-EN-")      # matched against seriesName
    match_field = site.get("match_field", "seriesName")
    platform_name = site.get("platform_name", "Trading Cards")  # defence-in-depth
    max_pages = int(site.get("max_pages", 8))
    max_rows = int(site.get("max_rows", 48))           # >48 returns a degraded payload
    base = site.get("base_url", "https://www.wog.ch/en/index.cfm")

    seen: dict = {}
    for page in range(1, max_pages + 1):
        data = {"platformID": platform_id, "page": page,
                "maxRows": max_rows, "orderBy": order_by}
        if tag:
            data["tag"] = tag
        resp = session.post(f"{base}/ajax.productList", data=data, timeout=40)
        resp.raise_for_status()
        products = resp.json().get("products", [])
        for p in products:
            match_value = p.get(match_field) or p.get("title") or ""
            if name_filter and name_filter not in match_value:
                continue
            if platform_name and p.get("platformName") != platform_name:
                continue
            pid = p.get("productID")
            if pid in seen:
                continue
            delivery = p.get("deliveryText") or ""
            unit_price = p.get("unitPrice")
            seen[pid] = make_item(
                site, pid, p.get("title"),
                in_stock=delivery in WOG_IN_STOCK,
                price=(f"CHF {unit_price}" if unit_price else ""),
                availability=delivery,
                link=p.get("linkTo"),
            )
        if len(products) < max_rows:
            break
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Adapter: Shopify storefront JSON
# --------------------------------------------------------------------------- #
# Any Shopify shop exposes a public, clean JSON feed of a collection at
# `<collection_url>/products.json`. Each product has `variants` with an
# `available` boolean and a `price`; the product page is `<base>/products/<handle>`.
# We page through it (250/page is Shopify's max) and filter by a title substring.

def fetch_shopify(site: dict, session: requests.Session) -> list[dict]:
    collection_url = site["collection_url"].rstrip("/")
    name_filter = (site.get("name_filter") or "").lower()
    # When set, language/variant is a Shopify *variant* (not in the title). We
    # then track each matching variant separately (key = variant id) and check
    # that variant's own stock. e.g. variant_filter "English" on a shop whose
    # products have a Language option.
    variant_filter = (site.get("variant_filter") or "").lower()
    per_page = int(site.get("per_page", 250))           # 250 = Shopify's max

    # Product links: keep any locale prefix (e.g. ".../en/") that precedes
    # "/collections", so links land on the right localized product page.
    product_base = collection_url.split("/collections", 1)[0]

    items: list[dict] = []
    page = 1
    while True:
        url = f"{collection_url}/products.json?limit={per_page}&page={page}"
        resp = session.get(url, timeout=30, headers={"Accept": "application/json"})
        resp.raise_for_status()
        products = resp.json().get("products", [])
        if not products:
            break
        for p in products:
            title = p.get("title") or ""
            if name_filter and name_filter not in title.lower():
                continue
            handle = p.get("handle")
            variants = p.get("variants") or []

            if variant_filter:
                # One item per matching variant (e.g. the English one).
                for v in variants:
                    vtitle = (v.get("title") or "")
                    if variant_filter not in vtitle.lower():
                        continue
                    avail = bool(v.get("available"))
                    price = v.get("price")
                    items.append(make_item(
                        site, v.get("id"), f"{title} — {vtitle}",
                        in_stock=avail,
                        price=(f"CHF {price}" if price else ""),
                        availability=("In stock" if avail else "Sold out"),
                        link=f"{product_base}/products/{handle}?variant={v.get('id')}",
                    ))
                continue

            # Default: one item per product, in stock if any variant is.
            in_stock = any(v.get("available") for v in variants)
            price = variants[0].get("price") if variants else None
            items.append(make_item(
                site, p.get("id"), title,
                in_stock=in_stock,
                price=(f"CHF {price}" if price else ""),
                availability=("In stock" if in_stock else "Sold out"),
                link=f"{product_base}/products/{handle}",
            ))
        if len(products) < per_page:
            break
        page += 1
        if page > 50:  # safety stop
            break
    return items


ADAPTERS = {
    "woocommerce": fetch_woocommerce,
    "wog": fetch_wog,
    "shopify": fetch_shopify,
}


def fetch_site(site: dict, session: requests.Session) -> list[dict]:
    adapter = ADAPTERS.get(site["type"])
    if adapter is None:
        raise ValueError(f"Unknown site type: {site['type']!r} (site {site.get('id')})")
    return adapter(site, session)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Diffing -> alerts
# --------------------------------------------------------------------------- #

def build_alerts(prev: dict[str, dict], curr: dict[str, dict]) -> list[str]:
    # Sites we have *any* prior state for. A site we've never recorded (newly
    # added, or one whose catalog suddenly expanded) is seeded silently rather
    # than blasting its whole catalog as "new".
    known_sites = {k.split(":", 1)[0] for k in prev}
    alerts: list[str] = []
    for key, now in curr.items():
        site_id = key.split(":", 1)[0]
        alert_on = now.get("alert_on") or ["new", "restock"]
        before = prev.get(key)
        if before is None:
            if site_id not in known_sites:
                continue  # silent seed for a brand-new site
            if "new" not in alert_on:
                continue
            tag = "🆕 New" + (" (in stock)" if now["in_stock"] else " (not yet in stock)")
            alerts.append(render_message(tag, now))
        elif not before.get("in_stock") and now["in_stock"]:
            if "restock" not in alert_on:
                continue
            alerts.append(render_message("📦 Back in stock", now))
    return alerts


def render_message(headline: str, item: dict) -> str:
    lines = [f"{headline} at {item['label']}!", item["name"]]
    detail = " · ".join(x for x in (item.get("price"), item.get("availability")) if x)
    if detail:
        lines.append(detail)
    if item.get("link"):
        lines.append(item["link"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CallMeBot
# --------------------------------------------------------------------------- #

def _mask_phone(phone: str) -> str:
    """Show only the last 4 digits, e.g. +417xxxxx4567 -> '…4567'."""
    p = str(phone)
    return "…" + p[-4:] if len(p) > 4 else "…"


def _redact(text: str, recipient: dict) -> str:
    """Strip the phone + apikey (raw and URL-encoded) from any string we log.

    Critical for PUBLIC repos: Actions logs are world-readable, and a network
    error from requests includes the full failing URL (phone + apikey in the
    query string). GitHub's secret masking can't be relied on for values parsed
    out of a JSON secret, so we redact them ourselves.
    """
    out = str(text)
    for v in (str(recipient.get("phone", "")), str(recipient.get("apikey", ""))):
        if v:
            out = out.replace(v, "***").replace(urllib.parse.quote(v), "***")
    return out


def send_via_callmebot(recipient: dict, text: str) -> bool:
    who = recipient.get("name") or _mask_phone(recipient.get("phone", ""))
    params = {
        "phone": str(recipient["phone"]),
        "text": text,
        "apikey": str(recipient["apikey"]),
    }
    try:
        resp = requests.get(
            "https://api.callmebot.com/whatsapp.php", params=params, timeout=30
        )
        # CallMeBot returns HTTP 200 even on errors — the real outcome is in the
        # body (e.g. "Message queued..." vs "APIKEY not valid"). Always log it.
        body = _redact(" ".join(resp.text.split())[:300], recipient)
        ok = resp.status_code == 200 and "queued" in resp.text.lower()
        verdict = "queued OK" if ok else "NOT delivered"
        print(f"  -> {who}: HTTP {resp.status_code} [{verdict}] CallMeBot: {body}")
        return ok
    except requests.RequestException as e:
        print(f"  -> ERROR sending to {who}: {_redact(str(e), recipient)}")
        return False


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

TELEGRAM_DELAY_SECONDS = 1  # Telegram is fast; a small gap avoids 429s.


def send_via_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url, data={"chat_id": chat_id, "text": text}, timeout=30
        )
        # Never let the bot token leak into public logs.
        body = str(resp.text)[:300].replace(token, "***")
        ok = resp.status_code == 200
        verdict = "sent OK" if ok else "FAILED"
        print(f"  -> telegram {chat_id}: HTTP {resp.status_code} [{verdict}] {body}")
        return ok
    except requests.RequestException as e:
        print(f"  -> telegram ERROR for {chat_id}: {str(e).replace(token, '***')}")
        return False


def notify_all(messages: list[str], recipients: list[dict], telegram: dict | None) -> None:
    for msg in messages:
        print(f"ALERT:\n{msg}\n")
        for r in recipients:
            send_via_callmebot(r, msg)
            time.sleep(SEND_DELAY_SECONDS)
        if telegram:
            for chat_id in telegram["chat_ids"]:
                send_via_telegram(telegram["token"], chat_id, msg)
                time.sleep(TELEGRAM_DELAY_SECONDS)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run_cycle(sites, recipients, telegram, session, verbose: bool) -> int | None:
    """One poll of every site: fetch, diff against state, send alerts, save.

    Returns the number of alerts sent, or None if every site failed to fetch
    (state is left untouched in that case).
    """
    curr: dict[str, dict] = {}
    for site in sites:
        try:
            items = fetch_site(site, session)
            curr.update({it["key"]: it for it in items})
            if verbose:
                print(f"[{site['id']}] {len(items)} matching product(s).")
        except Exception as e:
            # One flaky shop shouldn't kill the whole cycle.
            print(f"[{site['id']}] ERROR: {e}")

    if not curr:
        print("No products fetched from any site; leaving state untouched.")
        return None

    prev = load_state()
    if not prev:
        print("First run: seeding state silently (no alerts).")
        save_state(curr)
        return 0

    alerts = build_alerts(prev, curr)
    if verbose or alerts:
        print(f"{len(alerts)} alert(s) to send.")
    if alerts:
        notify_all(alerts, recipients, telegram)

    # Carry forward items from sites that errored this cycle, so a transient
    # failure doesn't make everything look "new" next time. We only replace the
    # state for sites we actually fetched.
    fetched_sites = {it["key"].split(":", 1)[0] for it in curr.values()}
    merged = {k: v for k, v in prev.items() if k.split(":", 1)[0] not in fetched_sites}
    merged.update(curr)
    save_state(merged)
    return len(alerts)


def main() -> int:
    sites = load_sites()
    recipients = load_recipients()
    telegram = load_telegram()
    channels = []
    if recipients:
        channels.append(f"CallMeBot×{len(recipients)}")
    if telegram:
        channels.append(f"Telegram×{len(telegram['chat_ids'])}")
    if channels:
        print(f"Channels: {', '.join(channels)}")
    else:
        print("WARNING: no delivery channels configured "
              "(CALLMEBOT_RECIPIENTS / TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_IDS). "
              "Will detect changes but not send anything.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # POLL_SECONDS > 0 turns on loop mode: keep polling every POLL_SECONDS for
    # up to MAX_RUNTIME_MINUTES, then exit cleanly so the workflow's cron can
    # start a fresh job (GitHub kills jobs at 6h; we stop before that so the
    # state cache still gets saved). POLL_SECONDS unset/0 = single pass, which
    # is what you want for local testing.
    poll_seconds = int(os.environ.get("POLL_SECONDS", "0") or 0)
    if poll_seconds <= 0:
        return 0 if run_cycle(sites, recipients, telegram, session, verbose=True) is not None else 1

    max_minutes = float(os.environ.get("MAX_RUNTIME_MINUTES", "345") or 345)
    deadline = time.monotonic() + max_minutes * 60
    print(f"Loop mode: polling every {poll_seconds}s for up to {max_minutes:.0f} min.")

    cycle = 0
    while True:
        cycle += 1
        started = time.monotonic()
        try:
            sent = run_cycle(sites, recipients, telegram, session,
                             verbose=(cycle == 1))
            # Log the first cycle, every alert, and a heartbeat every ~10 min
            # so the Actions log shows the loop is alive without being spammy.
            if cycle == 1 or sent or cycle % 20 == 0:
                print(f"{time.strftime('%H:%M:%S')} cycle {cycle}: "
                      f"{'fetch failed' if sent is None else f'{sent} alert(s)'}")
        except Exception as e:
            # Never let one bad cycle kill the whole shift.
            print(f"{time.strftime('%H:%M:%S')} cycle {cycle} ERROR: {e}")

        wait = max(0.0, poll_seconds - (time.monotonic() - started))
        if time.monotonic() + wait >= deadline:
            break
        if wait:
            time.sleep(wait)

    print(f"Shift over after {cycle} cycle(s); exiting so the next scheduled "
          f"run takes over.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
