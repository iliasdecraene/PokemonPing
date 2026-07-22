#!/usr/bin/env python3
"""
wog.ch auto-buyer
=================

Companion to notifier.py. When the notifier detects a *target* Pokémon product
on wog.ch (by default the "30th Celebration" English set), this logs into your
wog account, drops the item in your cart, and — in full-auto mode — places the
order on invoice ("Kauf auf Rechnung"), all in a couple of seconds.

WHY THIS IS POSSIBLE (recon findings, endpoints wog's own site uses):
  * Login:       POST /index.cfm/authenticate   (userName, password, csrfToken,
                 relocate=true) — NO reCAPTCHA on the login form.
  * Add to cart: POST /index.cfm/ajax.putIntoCart (productID, quantity,
                 csrfToken) — NO captcha; returns JSON {STATUS, MESSAGE, ...}.
  * The productID comes free from the detection feed; the csrfToken is printed
    in each product page's HTML.
  * The final checkout confirm (address -> Rechnung -> place order) sits behind
    your login and is mapped ON the VPS by dump_checkout() the first time, which
    walks up to — never through — the confirm button. Until that's wired,
    full-auto stops at "in your cart" and pings you to tap Pay.

SECRETS COME ONLY FROM ENV VARS — nothing here is committed:
  WOG_USERNAME, WOG_PASSWORD          your wog.ch login (set on the VPS only)
  WOG_BUY_ENABLED=1                   master switch. Unset/0 => DRY RUN (never
                                      touches the cart, only logs/pings).
  WOG_BUY_MODE=cart|auto              cart (default): add-to-cart + ping you to
                                      confirm. auto: also place the invoice order.
  WOG_BUY_KEYWORDS=30th,celebration   title must contain one (comma list, ANY).
  WOG_BUY_LANG_MARKER=-EN-            seriesName/name must contain this.
  WOG_BUY_MAX_PRICE=300               per-item CHF ceiling (hard safety stop).
  WOG_BUY_LEDGER=bought.json          one-order-per-product ledger file.

This file is import-safe: importing it does nothing. Run it directly for the
self-tests / recon (see the __main__ block).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

WOG_HOST = "https://www.wog.ch"
WOG_BASE = f"{WOG_HOST}/en/index.cfm"


# --------------------------------------------------------------------------- #
# Purchase guard — decides IF an item may be bought. Pure logic, fully testable.
# --------------------------------------------------------------------------- #

class BuyGuard:
    """Whitelist + price cap + one-order-per-product ledger.

    Deliberately conservative: an item is bought only if it matches EVERY rule.
    The ledger makes a repeat detection of the same product a no-op forever, so
    a flaky feed can never trigger a second order.
    """

    def __init__(self, keywords, lang_marker, max_price_chf, ledger_path):
        self.keywords = [k.strip().lower() for k in keywords if k.strip()]
        self.lang_marker = (lang_marker or "").lower()
        self.max_price_chf = float(max_price_chf)
        self.ledger_path = Path(ledger_path)

    # -- ledger ------------------------------------------------------------- #
    def _load(self) -> dict:
        try:
            return json.loads(self.ledger_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def already_bought(self, key: str) -> bool:
        return key in self._load()

    def record(self, key: str, info: dict) -> None:
        led = self._load()
        led[key] = info
        self.ledger_path.write_text(json.dumps(led, indent=2), "utf-8")

    # -- decision ----------------------------------------------------------- #
    def wants(self, item: dict) -> tuple[bool, str]:
        """Return (ok_to_buy, reason). reason explains a *rejection* or 'match'."""
        hay = f"{item.get('name', '')} {item.get('series', '')}".lower()

        if self.keywords and not any(k in hay for k in self.keywords):
            return False, f"no keyword {self.keywords} in title"
        if self.lang_marker and self.lang_marker not in hay:
            return False, f"lang marker {self.lang_marker!r} absent"
        if not item.get("in_stock", False):
            return False, "not in stock / not orderable yet"

        price = _parse_price(item.get("price", ""))
        if price is None:
            return False, "no parseable price (refusing to buy blind)"
        if price > self.max_price_chf:
            return False, f"price CHF {price:.2f} over cap CHF {self.max_price_chf:.2f}"

        if self.already_bought(item.get("key", "")):
            return False, "already ordered (ledger)"

        return True, f"match (CHF {price:.2f})"


def _parse_price(price: str):
    """'CHF 179.90' / '179,90' -> 179.9; unparseable -> None."""
    if not price:
        return None
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", str(price).replace("'", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# wog client — the actual HTTP flow against confirmed endpoints.
# --------------------------------------------------------------------------- #

class CheckoutNotConfigured(RuntimeError):
    """Raised until the logged-in checkout flow is mapped on the VPS."""


class WogClient:
    def __init__(self, username: str, password: str, session: requests.Session | None = None):
        self.username = username
        self.password = password
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        )
        self.logged_in = False

    # -- csrf --------------------------------------------------------------- #
    @staticmethod
    def _extract_csrf(html_text: str) -> str:
        # Product pages print it as inline JS: var csrfToken = "ABC123..."
        m = re.search(r'csrfToken\s*=\s*"([0-9A-Fa-f]{16,})"', html_text)
        if m:
            return m.group(1)
        # Forms carry it as a hidden input.
        m = re.search(r'name="csrfToken"\s+value="([0-9A-Fa-f]{16,})"', html_text)
        if m:
            return m.group(1)
        raise RuntimeError("csrfToken not found in page")

    # -- login -------------------------------------------------------------- #
    def login(self) -> bool:
        page = self.session.get(f"{WOG_BASE}/login", timeout=30)
        page.raise_for_status()
        csrf = self._extract_csrf(page.text)
        resp = self.session.post(
            f"{WOG_BASE}/authenticate",
            data={
                "userName": self.username,
                "password": self.password,
                "csrfToken": csrf,
                "relocate": "true",
                "rememberMe": "true",
            },
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()
        # A logged-in session no longer shows the login button; the account/logout
        # link appears instead. Confirm by hitting myAccount.
        acct = self.session.get(f"{WOG_BASE}/myAccount", timeout=30)
        self.logged_in = "authenticate" not in acct.url and (
            "logout" in acct.text.lower() or "myaccount" in acct.url.lower()
        )
        return self.logged_in

    # -- cart --------------------------------------------------------------- #
    def add_to_cart(self, product_id, quantity: int = 1, product_url: str | None = None) -> dict:
        """POST ajax.putIntoCart. Needs a fresh csrfToken from the product page."""
        if product_url and product_url.startswith("http"):
            url = product_url
        elif product_url and product_url.startswith("/"):
            url = WOG_HOST + product_url
        else:
            url = f"{WOG_BASE}/details/product/{product_id}"
        pg = self.session.get(url, timeout=30)
        pg.raise_for_status()
        csrf = self._extract_csrf(pg.text)

        resp = self.session.post(
            f"{WOG_HOST}/index.cfm/ajax.putIntoCart",
            data={"productID": str(product_id), "quantity": int(quantity), "csrfToken": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            data = {"STATUS": -1, "MESSAGE": "non-JSON response", "_raw": resp.text[:200]}
        return {
            "ok": int(data.get("STATUS", -1)) > 0,
            "message": data.get("MESSAGE", ""),
            "cart_count": data.get("SHOPPINGCARTITEMCOUNT"),
            "raw": data,
        }

    # -- checkout recon (safe: reads pages, never confirms) ----------------- #
    # Anchors worth following forward through the checkout wizard...
    _NEXT = re.compile(
        r"(checkout|zur[- ]?kasse|/kasse|weiter|proceed|continue|order\.process|"
        r"orderaddress|orderpayment|order-?overview|paymentmethod|payment-?method|"
        r"zahlungsart|zahlung)", re.I)
    # ...but NEVER follow anything that mutates the cart or commits the order.
    _DANGER = re.compile(
        r"(remove|delete|clear|cancel|logout|abmelden|confirm|bestellen|"
        r"placeorder|execute|submit-?order|pay\.|\.remove|\.buy)", re.I)

    def inspect_checkout(self, out_dir: str | None = None, max_pages: int = 8) -> list[str]:
        """SAFE recon: walk FORWARD from the cart through the checkout wizard,
        reporting each page's forms, inputs (esp. payment radios), selects, any
        'Rechnung' context, and the next-step links it follows. Follows only
        navigation links (never remove/cancel/confirm), so it maps the flow up to
        — but never through — the final place-order button.
        """
        report: list[str] = []
        out = Path(out_dir) if out_dir else None
        if out:
            out.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        queue = [f"{WOG_BASE}/cart"]
        page_no = 0
        while queue and len(seen) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                r = self.session.get(url, timeout=30)
            except requests.RequestException as e:
                report.append(f"\n=== {url} -> ERROR {e}")
                continue
            page_no += 1
            report.append(f"\n=== page {page_no}: {url}  (HTTP {r.status_code})")

            for fm in re.finditer(r"<form\b[^>]*>", r.text, re.I):
                action = (re.search(r'action="([^"]*)"', fm.group(0)) or [None, "?"])[1]
                method = (re.search(r'method="([^"]*)"', fm.group(0)) or [None, "?"])[1]
                report.append(f"   form: {method} -> {action}")

            for m in re.finditer(r"<input\b[^>]*>", r.text, re.I):
                tag = m.group(0)
                name = (re.search(r'\bname="([^"]*)"', tag) or [None, None])[1]
                if not name:
                    continue
                typ = (re.search(r'\btype="([^"]*)"', tag) or [None, ""])[1]
                val = (re.search(r'\bvalue="([^"]*)"', tag) or [None, ""])[1]
                if (typ.lower() in ("radio", "checkbox", "hidden")
                        or re.search(r"(payment|zahl|rechnung|invoice)", tag, re.I)):
                    report.append(f"   input {typ or '?'} name={name} value={val[:48]}")

            for sel in re.finditer(r'<select\b[^>]*\bname="([^"]*)"[^>]*>(.*?)</select>',
                                   r.text, re.I | re.S):
                opts = re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>',
                                  sel.group(2), re.I)
                report.append(f"   select name={sel.group(1)}: "
                              + "; ".join(f"{v}={t.strip()[:24]}" for v, t in opts[:12]))

            shown = 0
            for m in re.finditer(r"(kauf auf rechnung|rechnung|invoice)", r.text, re.I):
                ctx = re.sub(r"\s+", " ", r.text[max(0, m.start() - 70):m.start() + 70])
                report.append(f"   PAY-CTX: …{ctx}…")
                shown += 1
                if shown >= 3:
                    break

            for a in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.I | re.S):
                href, text = a.group(1), re.sub(r"<[^>]+>", "", a.group(2)).strip()
                blob = f"{href} {text}"
                if "index.cfm" in href and self._NEXT.search(blob) and not self._DANGER.search(blob):
                    full = href if href.startswith("http") else WOG_HOST + href
                    report.append(f"   NEXT-LINK: '{text[:32]}' -> {full}")
                    if full not in seen:
                        queue.append(full)

            # Call-to-action controls: button-styled links and <button>s. These
            # are usually the "proceed to checkout"/"order" triggers. Printed for
            # every page (so we can SEE them); auto-followed only if clearly safe
            # navigation. onclick/data-href reveal JS-driven navigation targets.
            for a in re.finditer(r'<a\b[^>]*class="[^"]*button[^"]*"[^>]*>(.*?)</a>',
                                 r.text, re.I | re.S):
                tag = a.group(0)
                href = (re.search(r'href="([^"]*)"', tag) or [None, ""])[1]
                label = re.sub(r"<[^>]+>", "", a.group(1)).strip()
                report.append(f"   CTA-LINK: '{label[:32]}' -> {href}")
                blob = f"{href} {label}"
                if href and "index.cfm" in href and self._NEXT.search(blob) and not self._DANGER.search(blob):
                    full = href if href.startswith("http") else WOG_HOST + href
                    if full not in seen:
                        queue.append(full)
            for b in re.finditer(r"<button\b[^>]*>(.*?)</button>", r.text, re.I | re.S):
                tag = b.group(0)
                label = re.sub(r"<[^>]+>", "", b.group(1)).strip()
                extras = []
                for attr in ("onclick", "data-href", "data-url", "formaction", "name"):
                    mv = re.search(rf'{attr}="([^"]*)"', tag)
                    if mv:
                        extras.append(f"{attr}={mv.group(1)[:70]}")
                if label or extras:
                    report.append(f"   BUTTON: '{label[:32]}'"
                                  + (("  " + " ".join(extras)) if extras else ""))

            if out and r.status_code == 200:
                (out / f"page_{page_no}.html").write_text(r.text, "utf-8")
        return report

    # Back-compat alias.
    def dump_checkout(self, out_dir: str) -> list[str]:
        return self.inspect_checkout(out_dir)

    def place_order_invoice(self, confirm: bool = False) -> dict:
        raise CheckoutNotConfigured(
            "Checkout confirm not yet mapped. Run `wog_buyer.py recon-checkout` "
            "on the VPS (logged in) and share the printed report so the invoice "
            "confirm step can be wired."
        )


# --------------------------------------------------------------------------- #
# Orchestration — called by the notifier when a matching alert fires.
# --------------------------------------------------------------------------- #

def buyer_config_from_env() -> dict:
    return {
        "username": os.environ.get("WOG_USERNAME", ""),
        "password": os.environ.get("WOG_PASSWORD", ""),
        "enabled": os.environ.get("WOG_BUY_ENABLED", "") in ("1", "true", "yes", "on"),
        "mode": (os.environ.get("WOG_BUY_MODE") or "cart").lower(),
        "keywords": (os.environ.get("WOG_BUY_KEYWORDS") or "30th,celebration").split(","),
        "lang_marker": os.environ.get("WOG_BUY_LANG_MARKER", "-EN-"),
        "max_price": os.environ.get("WOG_BUY_MAX_PRICE", "300"),
        "ledger": os.environ.get("WOG_BUY_LEDGER", "bought.json"),
    }


def buyer_policy_from_env() -> dict:
    """Global buy *policy* (no credentials). Per-person credentials + ledger are
    filled in by the caller from the matched buyer, so several people can each
    buy with their own wog account under the same price cap / language rules."""
    return {
        "enabled": os.environ.get("WOG_BUY_ENABLED", "") in ("1", "true", "yes", "on"),
        "mode": (os.environ.get("WOG_BUY_MODE") or "cart").lower(),
        "keywords": (os.environ.get("WOG_BUY_KEYWORDS") or "30th,celebration").split(","),
        "lang_marker": os.environ.get("WOG_BUY_LANG_MARKER", "-EN-"),
        "max_price": os.environ.get("WOG_BUY_MAX_PRICE", "300"),
    }


def consider_purchase(item: dict, cfg: dict, notify) -> str | None:
    """Decide + act on one detected wog item. `notify` is a callable(str)->None
    used for the Telegram receipt. Returns a short status string (or None if the
    item didn't match). NEVER raises out — a buy failure must not crash polling.
    """
    if not str(item.get("key", "")).startswith("wog:"):
        return None

    guard = BuyGuard(cfg["keywords"], cfg["lang_marker"], cfg["max_price"], cfg["ledger"])
    ok, reason = guard.wants(item)
    if not ok:
        return f"skip ({reason})"

    name = item.get("name", "?")
    price = item.get("price", "?")
    link = item.get("link", "")

    # DRY RUN — enabled switch is off. Prove the match, touch nothing.
    if not cfg["enabled"]:
        notify(f"🧪 WOULD BUY (dry run): {name}\n{price}\n{link}\n"
               f"Set WOG_BUY_ENABLED=1 to arm.")
        return "dry-run would-buy"

    if not cfg["username"] or not cfg["password"]:
        notify(f"⚠️ Target found but no WOG_USERNAME/PASSWORD set: {name}\n{link}")
        return "no credentials"

    try:
        client = WogClient(cfg["username"], cfg["password"])
        if not client.login():
            notify(f"⚠️ Target found but wog login FAILED: {name}\n{link}")
            return "login failed"

        pid = item["key"].split(":", 1)[1]
        res = client.add_to_cart(pid, quantity=1, product_url=link or None)
        if not res["ok"]:
            notify(f"⚠️ Add-to-cart failed for {name}: {res['message']}\n{link}")
            return f"add-to-cart failed: {res['message']}"

        # Item is now reserved in your cart.
        if cfg["mode"] != "auto":
            notify(f"🛒 IN YOUR CART at wog: {name}\n{price}\n"
                   f"Tap to pay now (invoice): {link}")
            guard.record(item["key"], {"name": name, "price": price, "action": "cart"})
            return "added to cart + pinged"

        # Full-auto: try to place the invoice order.
        try:
            order = client.place_order_invoice(confirm=True)
            notify(f"✅ ORDERED on invoice at wog: {name}\n{price}\n"
                   f"Order: {order.get('order_id', '?')}\n{link}")
            guard.record(item["key"], {"name": name, "price": price, "action": "ordered"})
            return "ordered"
        except CheckoutNotConfigured:
            # Interim: leave it in the cart and ping, so nothing is lost.
            notify(f"🛒 IN YOUR CART at wog (auto-checkout not yet wired): {name}\n"
                   f"{price}\nTap to pay now: {link}")
            guard.record(item["key"], {"name": name, "price": price, "action": "cart"})
            return "added to cart (checkout pending recon)"
    except Exception as e:  # never crash the poll loop
        notify(f"⚠️ Buy attempt errored for {name}: {e}\n{link}")
        return f"error: {e}"


def buy_target(target: dict, cfg: dict) -> str:
    """Handle a manual 'BUY' reply for ONE specific wog item.

    Unlike consider_purchase() (keyword auto-buy), this skips the keyword
    whitelist — the user explicitly chose this item — but still enforces:
    English-only (language marker), the price cap, and the one-order ledger.
    Returns a short human-readable result string (sent back as the Telegram
    reply). Never raises.
    """
    try:
        name = target.get("name", "?")
        link = target.get("link", "") or ""
        hay = f"{name} {target.get('series', '')}".lower()

        lang = (cfg.get("lang_marker") or "").lower()
        if lang and lang not in hay:
            return f"❌ Skipped — that isn't an English listing: {name}"

        price = _parse_price(target.get("price", ""))
        max_price = float(cfg["max_price"])
        if price is not None and price > max_price:
            return (f"❌ CHF {price:.2f} is over your safety cap of "
                    f"CHF {max_price:.0f}.\nRaise WOG_BUY_MAX_PRICE to allow it.")

        guard = BuyGuard(cfg["keywords"], cfg["lang_marker"], cfg["max_price"], cfg["ledger"])
        key = target.get("key", "")
        if guard.already_bought(key):
            return f"ℹ️ Already handled earlier: {name}"

        if not cfg["enabled"]:
            return (f"🧪 Dry run — would buy: {name} ({target.get('price', '?')}).\n"
                    f"Set WOG_BUY_ENABLED=1 on the server to arm real buying.")

        if not cfg["username"] or not cfg["password"]:
            return "⚠️ No WOG_USERNAME / WOG_PASSWORD set on the server."

        client = WogClient(cfg["username"], cfg["password"])
        if not client.login():
            return "⚠️ wog login failed — check the credentials."

        pid = key.split(":", 1)[1]
        res = client.add_to_cart(pid, quantity=1, product_url=link or None)
        if not res["ok"]:
            return f"❌ Couldn't add to cart (likely just sold out): {res['message']}"

        # A manual BUY always tries to place the real order — the reply *is* the
        # confirmation. Falls back to cart + pay-link only until checkout is wired.
        try:
            order = client.place_order_invoice(confirm=True)
            guard.record(key, {"name": name, "price": target.get("price"), "action": "ordered"})
            return f"✅ Ordered on invoice: {name}\nOrder {order.get('order_id', '?')}"
        except CheckoutNotConfigured:
            guard.record(key, {"name": name, "price": target.get("price"), "action": "cart"})
            return (f"🛒 Added to your cart: {name}\n"
                    f"(one-tap ordering not wired yet — tap to pay) {link}")
    except Exception as e:
        return f"⚠️ Buy failed: {e}"


# --------------------------------------------------------------------------- #
# CLI: self-tests (no creds) and live recon (needs creds, safe).
# --------------------------------------------------------------------------- #

def _self_test() -> None:
    """Guard logic — runs offline, no network, no creds."""
    tmp = Path(os.environ.get("TEMP", ".")) / "wog_ledger_test.json"
    tmp.unlink(missing_ok=True)
    g = BuyGuard(["30th", "celebration"], "-EN-", 300, tmp)

    def item(**kw):
        base = {"key": "wog:1", "name": "Pokemon 30th Celebration -EN- Booster",
                "in_stock": True, "price": "CHF 179.90"}
        base.update(kw)
        return base

    ok, why = g.wants(item()); assert ok, why
    assert not g.wants(item(name="Pokemon Scarlet -EN-"))[0], "keyword gate"
    assert not g.wants(item(name="Pokemon 30th Celebration -DE- Booster"))[0], "lang gate"
    assert not g.wants(item(in_stock=False))[0], "stock gate"
    assert not g.wants(item(price="CHF 999.00"))[0], "price cap"
    assert not g.wants(item(price=""))[0], "no price => refuse"
    assert _parse_price("CHF 1'299.90") == 1299.9, _parse_price("CHF 1'299.90")

    g.record("wog:1", {"name": "x"})
    assert g.already_bought("wog:1"), "ledger record"
    assert not g.wants(item())[0], "already bought"
    tmp.unlink(missing_ok=True)
    print("wog_buyer self-test: ALL PASS")


def _login_test() -> None:
    cfg = buyer_config_from_env()
    if not cfg["username"] or not cfg["password"]:
        sys.exit("Set WOG_USERNAME and WOG_PASSWORD to run the login test.")
    c = WogClient(cfg["username"], cfg["password"])
    print("login:", "OK" if c.login() else "FAILED")


def _find_orderable_product(client: "WogClient"):
    """Pick any in-stock wog product so recon has a non-empty cart to inspect."""
    in_stock = {"in stock normally", "in external stock"}
    try:
        r = client.session.post(
            f"{WOG_BASE}/ajax.productList",
            data={"platformID": "tc", "page": 1, "maxRows": 48, "orderBy": "bestseller"},
            timeout=40)
        for p in r.json().get("products", []):
            if (p.get("deliveryText") or "").lower() in in_stock:
                return str(p.get("productID")), p.get("title"), p.get("linkTo")
    except (requests.RequestException, ValueError):
        pass
    return None, None, None


def _recon_page() -> None:
    """GET one wog page (default cart.address) and dump its forms+fields, payment
    inputs, Rechnung context, and next-step controls. Read-only — never POSTs.
    Walk the wizard by passing the next path:  recon-page cart.address"""
    cfg = buyer_config_from_env()
    if not cfg["username"] or not cfg["password"]:
        sys.exit("Set WOG_USERNAME and WOG_PASSWORD first.")
    c = WogClient(cfg["username"], cfg["password"])
    if not c.login():
        sys.exit("login failed")
    path = sys.argv[2] if len(sys.argv) > 2 else "cart.address"
    url = path if path.startswith("http") else f"{WOG_BASE}/{path}"
    r = c.session.get(url, timeout=30)
    h = r.text
    print(f"GET {url} -> HTTP {r.status_code}, {len(h)} bytes\n")

    print("--- forms (action, method, field names) ---")
    for fm in re.finditer(r"<form\b[^>]*>(.*?)</form>", h, re.I | re.S):
        open_tag = fm.group(0)[:fm.group(0).find(">") + 1]
        action = (re.search(r'action="([^"]*)"', open_tag) or [None, "?"])[1]
        method = (re.search(r'method="([^"]*)"', open_tag) or [None, "?"])[1]
        fid = (re.search(r'id="([^"]*)"', open_tag) or [None, ""])[1]
        inner = fm.group(1)
        names = sorted(set(re.findall(
            r'<(?:input|select|textarea|button)[^>]*\bname="([^"]+)"', inner, re.I)))
        if action and "search" in action.lower():
            continue
        print(f"  FORM id={fid} {method} -> {action}")
        print(f"     fields: {names}")
        for m in re.finditer(r"<input\b[^>]*>", inner, re.I):
            if re.search(r"(payment|zahl|rechnung|invoice|vorkasse|twint|postfinance|paypal|kreditkarte)",
                         m.group(0), re.I):
                nm = (re.search(r'name="([^"]*)"', m.group(0)) or [None, ""])[1]
                vl = (re.search(r'value="([^"]*)"', m.group(0)) or [None, ""])[1]
                ty = (re.search(r'type="([^"]*)"', m.group(0)) or [None, ""])[1]
                print(f"     PAY-INPUT type={ty} name={nm} value={vl}")

    print("\n--- payment / Rechnung context ---")
    shown = 0
    for m in re.finditer(r"(kauf auf rechnung|rechnung|zahlungsart|payment method|vorkasse)", h, re.I):
        print("  …", re.sub(r"\s+", " ", h[max(0, m.start() - 90):m.start() + 130]), "…")
        shown += 1
        if shown >= 6:
            break

    print("\n--- next-step controls (buttons / id-bearing / order-flow anchors) ---")
    for a in re.finditer(r"<(a|button)\b[^>]*>(.*?)</\1>", h, re.I | re.S):
        tag = a.group(0)
        opentag = tag[:tag.find(">") + 1]
        label = re.sub(r"<[^>]+>", "", a.group(2)).strip()
        if re.search(r'id="(address|payment|confirm|order|checkout|submit)', opentag, re.I) \
                or re.search(r"(cart\.|order\.|checkout|weiter|confirm|bestell|zahl|proceed)", opentag, re.I) \
                or label.lower() in ("checkout", "continue", "next", "weiter", "confirm order",
                                     "order", "place order", "bestellen", "pay", "buy"):
            href = (re.search(r'href="([^"]*)"', opentag) or [None, ""])[1]
            aid = (re.search(r'id="([^"]*)"', opentag) or [None, ""])[1]
            print(f"  <{a.group(1)} id={aid} href={href}> '{label[:30]}'")

    print("\n--- radio/checkbox inputs (payment selection often lives here) ---")
    for m in re.finditer(r'<input\b[^>]*type="(radio|checkbox)"[^>]*>', h, re.I):
        tag = m.group(0)
        nm = (re.search(r'name="([^"]*)"', tag) or [None, ""])[1]
        vl = (re.search(r'value="([^"]*)"', tag) or [None, ""])[1]
        chk = "CHECKED" if re.search(r"\bchecked", tag, re.I) else ""
        print(f"  {m.group(1)} name={nm} value={vl} {chk}")

    print("\n--- raw around payment-method / rechnung / checked / cart.confirm ---")
    shown = 0
    for m in re.finditer(r"(payment-?method|paymentMethod|zahlart|kauf auf rechnung|"
                         r"rechnung|invoice|\bchecked\b|cart\.confirm|setPayment)", h, re.I):
        print("  …", re.sub(r"\s+", " ", h[max(0, m.start() - 110):m.start() + 160]), "…")
        shown += 1
        if shown >= 12:
            break


def _recon_trigger() -> None:
    """Find what the JS-driven 'Checkout' button does (its raw tag + the JS that
    wires it), so we can learn the real order-flow URL. Read-only."""
    cfg = buyer_config_from_env()
    if not cfg["username"] or not cfg["password"]:
        sys.exit("Set WOG_USERNAME and WOG_PASSWORD first.")
    c = WogClient(cfg["username"], cfg["password"])
    if not c.login():
        sys.exit("login failed")
    print("logged in.")
    cart = c.session.get(f"{WOG_BASE}/cart", timeout=30).text

    print("\n--- raw HTML around id=\"address\" (the Checkout control) ---")
    for m in re.finditer(r'id="address"', cart):
        chunk = cart[max(0, m.start() - 250):m.start() + 500]
        print(re.sub(r"\s+", " ", chunk))
        print("   ......")
    # Any anchor whose href points into an order/checkout flow.
    print("\n--- anchors with order/checkout hrefs ---")
    for a in re.finditer(r'<a\b[^>]*href="([^"]*index\.cfm/[^"]*)"[^>]*>', cart, re.I):
        if re.search(r"(order|checkout|kasse|bestell|address|payment|zahl)", a.group(1), re.I) \
                and not re.search(r"(myOrder|orderHistory|orderCancel)", a.group(1), re.I):
            print("  ", a.group(1))

    print("\n--- /cart inline JS: orderProcess config + #address handler ---")
    for s in re.finditer(r"<script[^>]*>(.*?)</script>", cart, re.I | re.S):
        js = s.group(1)
        # The orderProcess step map (…: '1_Warenkorb', …) tells us the wizard URLs.
        for k in re.finditer(r".{0,40}orderProcess.{0,400}", js, re.I):
            print("  [orderProcess]", re.sub(r"\s+", " ", k.group(0)).strip()[:460])
        # Anything binding the #address button / navigating on click.
        for k in re.finditer(r".{0,60}(getElementById\(['\"]address|#address|['\"]address['\"]|location\.href|window\.location|\.href\s*=).{0,140}", js, re.I):
            print("  [addr/nav]", re.sub(r"\s+", " ", k.group(0)).strip()[:240])

    print("\n--- full inline <script> blocks that mention #address or orderProcess ---")
    for s in re.finditer(r"<script[^>]*>(.*?)</script>", cart, re.I | re.S):
        js = s.group(1)
        if re.search(r"#address|orderProcess|window\.location", js, re.I):
            print("  ", re.sub(r"\s+", " ", js).strip()[:900])
            print("   ------")


def _recon_checkout() -> None:
    """Login, put ONE in-stock item in the cart, and print the checkout report.

    Never confirms an order. Optional arg: a specific productID to use.
    """
    cfg = buyer_config_from_env()
    if not cfg["username"] or not cfg["password"]:
        sys.exit("Set WOG_USERNAME and WOG_PASSWORD first.")
    c = WogClient(cfg["username"], cfg["password"])
    if not c.login():
        sys.exit("login failed")
    print("logged in.")

    if len(sys.argv) > 2:
        pid, title, link = sys.argv[2], "(given)", None
    else:
        pid, title, link = _find_orderable_product(c)
    if not pid:
        sys.exit("Couldn't find an in-stock product to add — pass a productID: "
                 "wog_buyer.py recon-checkout <productID>")
    print(f"using product {pid}  {title}")
    # add_to_cart normalizes absolute or relative links itself — pass as-is.
    print("add_to_cart:", c.add_to_cart(pid, product_url=link or None))

    print("\n----- CHECKOUT REPORT (paste all of this back) -----")
    for line in c.inspect_checkout(os.environ.get("RECON_OUT")):
        print(line)
    print("----- END REPORT -----")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "self-test"
    if cmd == "self-test":
        _self_test()
    elif cmd == "login-test":
        _login_test()
    elif cmd == "recon-checkout":
        _recon_checkout()
    elif cmd == "recon-trigger":
        _recon_trigger()
    elif cmd == "recon-page":
        _recon_page()
    else:
        sys.exit(f"unknown command {cmd!r}; use: self-test | login-test | "
                 f"recon-checkout [productID] | recon-trigger | recon-page [path]")
