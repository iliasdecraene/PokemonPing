#!/usr/bin/env python3
"""
Headless-browser final confirm for wog.ch
=========================================

wog's final "Confirm Order" (cart.placeOrder) is behind Google reCAPTCHA
Enterprise, which a plain HTTP client can't satisfy. This drives a real
(headless) Chromium via Playwright for JUST that last click, reusing the cookies
from the already-logged-in requests session — so the fast part (login, clear
cart, add item) stays on requests and only the reCAPTCHA-gated confirm uses a
browser.

Playwright is an OPTIONAL dependency. If it isn't installed, place_order()
returns {"ok": False, "reason": "playwright-missing"} and the caller falls back
to the one-tap confirm link. Install on the VPS with:
    pip install playwright && playwright install --with-deps chromium

Everything here is import-safe; nothing runs on import.
"""

from __future__ import annotations

import re
from pathlib import Path

WOG_HOST = "https://www.wog.ch"
WOG_BASE = f"{WOG_HOST}/en/index.cfm"

# Markers used to classify the page after clicking Confirm Order. Tightened once
# we've seen a real success page in testing.
_SUCCESS = ("thank you", "thank you for your order", "order number", "bestellnummer",
            "order confirmation", "successfully", "ordercomplete", "orderconfirmation",
            "vielen dank")
_OUT_OF_STOCK = ("out of stock", "sold out", "no longer available", "nicht mehr verfügbar",
                 "nicht verfügbar", "not available", "ausverkauft")


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def _to_pw_cookies(cookiejar) -> list[dict]:
    """requests cookiejar -> Playwright cookie dicts."""
    out = []
    for c in cookiejar:
        out.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or "www.wog.ch",
            "path": c.path or "/",
            "secure": bool(getattr(c, "secure", True)),
            "httpOnly": False,
        })
    return out


def place_order(cookiejar, expect_name: str | None = None, confirm: bool = False,
                headless: bool = True, timeout_ms: int = 30000,
                debug_dir: str | None = None) -> dict:
    """Open cart.confirm in a browser (authenticated via the given cookies), and
    optionally click Confirm Order.

    Returns a dict:
      {"ok": bool, "ordered": bool, "reason": str, "url": str, "text": str}
    confirm=False => inspect only (verify cookies + summary), never clicks.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"ok": False, "ordered": False, "reason": "playwright-missing"}

    result = {"ok": False, "ordered": False, "reason": "", "url": "", "text": ""}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = browser.new_context(
                locale="en-US",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            )
            ctx.add_cookies(_to_pw_cookies(cookiejar))
            page = ctx.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(f"{WOG_BASE}/cart.confirm", wait_until="domcontentloaded")

            body = (page.inner_text("body") or "")
            low = body.lower()
            result["url"] = page.url

            # Cookie/login sanity: the confirm page must actually be the summary.
            if "cart.confirm" not in page.url or "authenticate" in page.url:
                result["reason"] = "not-authenticated (session cookies didn't carry over)"
                result["text"] = body[:300]
                browser.close()
                return result

            # Safety: the item we expect must be on the summary.
            if expect_name:
                key = expect_name.split(" — ")[0].split(" -EN-")[0].strip()[:18].lower()
                if key and key not in low:
                    result["reason"] = f"expected item not on summary ({key!r})"
                    result["text"] = body[:300]
                    browser.close()
                    return result

            if not confirm:
                result["ok"] = True
                result["reason"] = "verify-only (did not click Confirm Order)"
                result["text"] = body[:400]
                browser.close()
                return result

            # Make sure terms are accepted, then click Confirm Order.
            try:
                cb = page.query_selector("#agbAccepted")
                if cb and not cb.is_checked():
                    cb.check()
            except Exception:
                pass

            page.click("#submit-btn")
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
            # Give any post-submit redirect / async validation a moment.
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass

            after = (page.inner_text("body") or "")
            alow = after.lower()
            result["url"] = page.url
            result["text"] = after[:1500]
            # reCAPTCHA-specific signals worth flagging explicitly.
            if re.search(r"(recaptcha|not a robot|verify you are human|captcha)", alow):
                result["reason"] = "reCAPTCHA challenge/error on the page"

            if debug_dir:
                try:
                    d = Path(debug_dir)
                    d.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(d / "confirm_after.png"), full_page=True)
                    (d / "confirm_after.html").write_text(page.content(), "utf-8")
                    result["debug"] = str(d)
                except Exception:
                    pass

            if any(s in alow for s in _OUT_OF_STOCK):
                result["reason"] = "out-of-stock at confirm"
            elif any(s in alow for s in _SUCCESS) or "cart.confirm" not in page.url:
                result["ok"] = True
                result["ordered"] = True
                result["reason"] = "order placed"
            elif not result["reason"]:
                result["reason"] = "unknown result (see text/url)"
            browser.close()
            return result
    except Exception as e:
        result["reason"] = f"browser error: {e}"
        return result
