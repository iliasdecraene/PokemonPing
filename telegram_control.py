#!/usr/bin/env python3
"""
Telegram controller — two-way channel for the notifier.
=======================================================

On top of just *sending* alerts, this lets you **reply `BUY`** to a wog.ch alert
in Telegram and have the bot attempt to buy that exact product, then reply with
the result.

How the reply mapping works: when we send a wog alert we remember its Telegram
`message_id` alongside the product. When a message arrives that is a *reply* to
one of those alerts and its text is "BUY", we look up the product and hand it to
a buy handler. Telegram's long-poll `getUpdates` (offset-tracked) is how we read
replies — no webhook/public URL needed, which suits a small VPS.

State (last-seen update offset + the message_id -> product map) lives in a small
JSON file so a restart doesn't lose in-flight buy targets.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

# Accept a reply as a buy command only if its whole text is one of these
# (case-insensitive). Strict on purpose — no accidental buys from chatter.
BUY_WORDS = {"buy", "buy it", "🟢", "yes buy"}


class TelegramController:
    def __init__(self, token: str, chat_ids, authorized=None,
                 state_path="telegram_state.json", target_ttl_seconds: int = 86400):
        self.token = str(token)
        self.chat_ids = [str(c) for c in chat_ids]
        # {str(telegram_user_id): buyer_dict}. Only these people may trigger a
        # BUY, and each buys with THEIR own credentials (buyer_dict). Authorizing
        # by *sender* — not by chat — is what makes group chats safe.
        self.authorized = {str(k): v for k, v in (authorized or {}).items()}
        self.state_path = Path(state_path)
        self.target_ttl = int(target_ttl_seconds)
        self.api = f"https://api.telegram.org/bot{self.token}"

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            d = {}
        d.setdefault("offset", 0)
        d.setdefault("targets", {})
        return d

    def _save(self, d: dict) -> None:
        self.state_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")

    def _redact(self, s) -> str:
        return str(s).replace(self.token, "***")

    # -- send --------------------------------------------------------------- #
    def send(self, text: str, buy_target: dict | None = None) -> None:
        """Send `text` to every configured chat. If buy_target is given, remember
        each resulting message so a later BUY reply to it maps back to the item."""
        st = self._load()
        now = _now()
        for chat_id in self.chat_ids:
            try:
                r = requests.post(f"{self.api}/sendMessage",
                                  data={"chat_id": chat_id, "text": text}, timeout=30)
                ok = r.status_code == 200
                verdict = "sent OK" if ok else "FAILED"
                print(f"  -> telegram {chat_id}: HTTP {r.status_code} "
                      f"[{verdict}] {self._redact(r.text)[:200]}")
                if ok and buy_target:
                    mid = (r.json().get("result") or {}).get("message_id")
                    if mid is not None:
                        st["targets"][f"{chat_id}:{mid}"] = {
                            "ts": now,
                            "key": buy_target.get("key"),
                            "name": buy_target.get("name"),
                            "link": buy_target.get("link"),
                            "price": buy_target.get("price"),
                            "series": buy_target.get("series", ""),
                        }
            except requests.RequestException as e:
                print(f"  -> telegram ERROR {chat_id}: {self._redact(e)}")
            time.sleep(1)
        # Forget stale targets so the file can't grow without bound.
        st["targets"] = {k: v for k, v in st["targets"].items()
                         if now - v.get("ts", 0) < self.target_ttl}
        self._save(st)

    def reply(self, chat_id, reply_to_message_id, text: str) -> None:
        try:
            requests.post(f"{self.api}/sendMessage",
                          data={"chat_id": chat_id, "text": text,
                                "reply_to_message_id": reply_to_message_id},
                          timeout=30)
        except requests.RequestException as e:
            print(f"  -> telegram reply ERROR: {self._redact(e)}")

    # -- receive ------------------------------------------------------------ #
    def poll_replies(self, handler) -> int:
        """Fetch new updates; for each BUY reply to a remembered wog alert, call
        handler(target)->str and send the returned text back as a reply.

        Returns how many buy commands were handled. Never raises.
        """
        st = self._load()
        try:
            r = requests.get(f"{self.api}/getUpdates",
                             params={"offset": st["offset"], "timeout": 0}, timeout=30)
            if r.status_code != 200:
                print(f"  getUpdates HTTP {r.status_code}: {self._redact(r.text)[:200]}")
                return 0
            updates = r.json().get("result", [])
        except (requests.RequestException, ValueError) as e:
            print(f"  getUpdates ERROR: {self._redact(e)}")
            return 0

        handled = 0
        max_offset = st["offset"]
        for u in updates:
            uid = u.get("update_id", 0)
            if uid >= max_offset:
                max_offset = uid + 1
            msg = u.get("message") or u.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id not in self.chat_ids:
                continue  # ignore anything not from a configured chat
            if (msg.get("text") or "").strip().lower() not in BUY_WORDS:
                continue
            # Authorize by the *sender*, not the chat: in a group everyone shares
            # the chat id, so only a whitelisted user id may buy.
            from_id = str((msg.get("from") or {}).get("id", ""))
            buyer = self.authorized.get(from_id)
            if buyer is None:
                self.reply(chat_id, msg.get("message_id"),
                           "🔒 You're not set up to buy. Ask the owner to add your "
                           f"Telegram ID ({from_id}) to the buyers list.")
                continue
            rid = (msg.get("reply_to_message") or {}).get("message_id")
            target = st["targets"].get(f"{chat_id}:{rid}") if rid is not None else None
            if not target:
                self.reply(chat_id, msg.get("message_id"),
                           "↩️ To buy, reply BUY *to a wog.ch alert message*.")
                continue
            print(f"[buy] BUY from {buyer.get('name', from_id)} for "
                  f"{target.get('key')} ({target.get('name')})")
            try:
                result = handler(target, buyer) or "(no result)"
            except Exception as e:  # a buy must never crash the loop
                result = f"⚠️ Buy handler error: {e}"
            self.reply(chat_id, msg.get("message_id"), result)
            handled += 1

        st["offset"] = max_offset
        self._save(st)
        return handled


def _now() -> float:
    # Wrapped so tests can monkeypatch a fixed clock.
    return time.time()
