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
# print emoji and would crash). Does not affect the WhatsApp message itself.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
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
        "search_term": "Pokemon",       # accent-insensitive on wog
        "platform_name": "Trading Cards",  # excludes Pokémon video games
        "name_filter": "-EN-",          # wog's English-language marker
        "max_pages": 5,                 # search is only reliable on early pages
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
# Adapter: wog.ch ajax.search
# --------------------------------------------------------------------------- #

def fetch_wog(site: dict, session: requests.Session) -> list[dict]:
    search_term = site.get("search_term", "Pokemon")
    platform_name = site.get("platform_name", "Trading Cards")
    name_filter = site.get("name_filter", "-EN-")
    max_pages = int(site.get("max_pages", 5))
    max_rows = int(site.get("max_rows", 48))  # >48 returns a degraded payload
    base = site.get("base_url", "https://www.wog.ch/en/index.cfm")

    seen: dict = {}
    for page in range(1, max_pages + 1):
        resp = session.post(
            f"{base}/ajax.search",
            data={"searchTerm": search_term, "page": page, "maxRows": max_rows},
            timeout=40,
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        for p in products:
            title = p.get("title") or ""
            if p.get("platformName") != platform_name:
                continue
            if name_filter and name_filter not in title:
                continue
            pid = p.get("productID")
            if pid in seen:
                continue
            delivery = p.get("deliveryText") or ""
            unit_price = p.get("unitPrice")
            seen[pid] = make_item(
                site, pid, title,
                in_stock=delivery in WOG_IN_STOCK,
                price=(f"CHF {unit_price}" if unit_price else ""),
                availability=delivery,
                link=p.get("linkTo"),
            )
        if len(products) < max_rows:
            break
    return list(seen.values())


ADAPTERS = {
    "woocommerce": fetch_woocommerce,
    "wog": fetch_wog,
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
    alerts: list[str] = []
    for key, now in curr.items():
        before = prev.get(key)
        if before is None:
            tag = "🆕 New" + (" (in stock)" if now["in_stock"] else " (not yet in stock)")
            alerts.append(render_message(tag, now))
        elif not before.get("in_stock") and now["in_stock"]:
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
        if resp.status_code == 200:
            print(f"  -> sent to {who}")
            return True
        body = _redact(resp.text[:200], recipient)
        print(f"  -> FAILED for {who}: HTTP {resp.status_code}: {body}")
        return False
    except requests.RequestException as e:
        print(f"  -> ERROR sending to {who}: {_redact(str(e), recipient)}")
        return False


def notify_all(recipients: list[dict], messages: list[str]) -> None:
    for msg in messages:
        print(f"ALERT:\n{msg}\n")
        for r in recipients:
            send_via_callmebot(r, msg)
            time.sleep(SEND_DELAY_SECONDS)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    sites = load_sites()
    recipients = load_recipients()
    if not recipients:
        print("WARNING: no recipients configured (CALLMEBOT_RECIPIENTS). "
              "Will detect changes but not send anything.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    curr: dict[str, dict] = {}
    for site in sites:
        try:
            items = fetch_site(site, session)
            curr.update({it["key"]: it for it in items})
            print(f"[{site['id']}] {len(items)} matching product(s).")
        except Exception as e:
            # One flaky shop shouldn't kill the whole run.
            print(f"[{site['id']}] ERROR: {e}")

    if not curr:
        print("No products fetched from any site; leaving state untouched.")
        return 1

    prev = load_state()
    if not prev:
        print("First run: seeding state silently (no alerts).")
        save_state(curr)
        return 0

    alerts = build_alerts(prev, curr)
    print(f"{len(alerts)} alert(s) to send.")
    if alerts:
        notify_all(recipients, alerts)

    # Carry forward items from sites that errored this run, so a transient
    # failure doesn't make everything look "new" next time. We only replace the
    # state for sites we actually fetched.
    fetched_sites = {it["key"].split(":", 1)[0] for it in curr.values()}
    merged = {k: v for k, v in prev.items() if k.split(":", 1)[0] not in fetched_sites}
    merged.update(curr)
    save_state(merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
