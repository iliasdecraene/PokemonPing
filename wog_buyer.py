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
    _CART_URL_CANDIDATES = [
        "shoppingCart", "shoppingcart", "basket", "cart", "warenkorb",
        "checkout", "kasse", "order", "bestellung", "orderProcess",
        "orderOverview", "confirmOrder",
    ]

    def _discover_cart_urls(self) -> list[str]:
        """Find cart/checkout links on the site so recon doesn't rely on guesses."""
        urls: list[str] = []
        try:
            home = self.session.get(f"{WOG_BASE}/myAccount", timeout=30).text
            for m in re.finditer(r'href="([^"]*index\.cfm/[^"]*)"', home):
                u = m.group(1)
                if re.search(r"(cart|basket|warenkorb|checkout|kasse|order|bestell)",
                             u, re.I):
                    full = u if u.startswith("http") else WOG_HOST + u
                    if full not in urls:
                        urls.append(full)
        except requests.RequestException:
            pass
        for name in self._CART_URL_CANDIDATES:
            u = f"{WOG_BASE}/{name}"
            if u not in urls:
                urls.append(u)
        return urls

    def inspect_checkout(self, out_dir: str | None = None) -> list[str]:
        """SAFE recon: walk cart/checkout pages and report their forms, fields,
        and payment options as plain text. NEVER submits the final confirm.

        Run on the VPS (logged in, one item in the cart). Paste the output back so
        place_order_invoice() can be wired to the real invoice-confirm form.
        """
        report: list[str] = []
        out = Path(out_dir) if out_dir else None
        if out:
            out.mkdir(parents=True, exist_ok=True)
        for url in self._discover_cart_urls():
            try:
                r = self.session.get(url, timeout=30)
            except requests.RequestException as e:
                report.append(f"{url} -> ERROR {e}")
                continue
            name = url.rstrip("/").split("/")[-1]
            forms = re.findall(r"<form\b[^>]*>", r.text, re.I)
            pay = sorted(set(re.findall(
                r"(rechnung|invoice|vorkasse|twint|postfinance|paypal|kreditkarte|"
                r"zahlungsart|payment[\w-]*)", r.text, re.I)))
            report.append(f"\n=== {name}  (HTTP {r.status_code}, {len(forms)} form(s)) "
                          f"{url}")
            if pay:
                report.append("   payment hints: " + ", ".join(sorted({p.lower() for p in pay})))
            for f in forms:
                action = (re.search(r'action="([^"]*)"', f) or [None, "?"])[1]
                method = (re.search(r'method="([^"]*)"', f) or [None, "?"])[1]
                report.append(f"   <form action={action} method={method}>")
            # input / select field names within the page (checkout fields live in
            # these; names are what a POST needs).
            fields = sorted(set(re.findall(r'<(?:input|select|button)[^>]*\bname="([^"]+)"',
                                           r.text, re.I)))
            if fields:
                report.append("   fields: " + ", ".join(fields[:40]))
            # radio/checkbox values around payment (the invoice option value).
            for m in re.finditer(r'<input[^>]*name="([^"]*)"[^>]*value="([^"]*)"[^>]*>', r.text, re.I):
                blob = m.group(0).lower()
                if any(k in blob for k in ("rechnung", "invoice", "payment", "zahl")):
                    report.append(f"   payment-input: name={m.group(1)} value={m.group(2)}")
            if out and r.status_code == 200:
                (out / f"checkout_{name}.html").write_text(r.text, "utf-8")
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
    else:
        sys.exit(f"unknown command {cmd!r}; use: self-test | login-test | recon-checkout [productID]")
