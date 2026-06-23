# Pokémon Drop Notifier 🔔

Watches one or more card shops and sends you a **Telegram** (and/or WhatsApp via
CallMeBot) message the moment a Pokémon product:

- **comes back in stock** (out-of-stock → in-stock), or
- **appears as a brand-new listing**.

Delivery channels (configure either or both — alerts go to all configured):
- **Telegram** — reliable, free, recommended.
- **CallMeBot** (WhatsApp) — free but third-party and sometimes flaky delivery.

Runs entirely on **GitHub Actions' free tier** — no server, no NAS, no paid
services. State lives in the Actions cache, so it only ever alerts on *changes*.

**Shops watched out of the box:**

| Shop | What's matched | How |
|------|----------------|-----|
| [cardcollectors.ch](https://cardcollectors.ch) | Pokémon products with `(EN)` in the title | WooCommerce Store API (clean JSON) |
| [wog.ch](https://www.wog.ch) | Pokémon **Trading Cards** with `-EN-` in the title | wog's `ajax.search` JSON endpoint |

---

## How it works

```
GitHub Actions cron (every ~5 min)
        │
        ▼
  notifier.py ──► for each configured shop, an "adapter" fetches products
        │          → normalize to {name, in_stock, price, link}
        │          → diff against last run (state.json, from Actions cache)
        ▼
  Telegram / CallMeBot  ──► message to each configured chat / recipient
```

Each shop is handled by an **adapter** chosen by the site's `type`:

- **`woocommerce`** — polls a WooCommerce shop's Store API
  (`/wp-json/wc/store/v1/products`). Reliable structured JSON with
  `is_in_stock`, `prices`, `permalink`. Filtered by a brand id + a title
  substring.
- **`wog`** — posts to wog.ch's internal `ajax.productList` endpoint (which
  returns JSON), browsing a platform + genre tag (Trading Cards + the
  "Pokémon TCG" tag) to get the *complete* catalog, then filters by language.
  wog strips the language suffix from the display `title`, so the filter matches
  against `seriesName` (which keeps the `-EN-` marker). (wog's site is
  JavaScript-rendered, but this is the same endpoint its own grid calls.)

Adding a shop that uses an **existing** adapter is pure config — no code.

---

## Setup

### 1. Set up Telegram (recommended, ~2 minutes)

1. In Telegram, message **[@BotFather](https://t.me/BotFather)** → `/newbot` →
   follow the prompts → it gives you a **bot token** like
   `123456789:AAE...`.
2. **Open a chat with your new bot and send it any message** (e.g. `hi`).
   *This step is required* — a bot cannot message you until you've messaged it
   first.
3. Get your **chat id**: message **[@userinfobot](https://t.me/userinfobot)** and
   it replies with your numeric id (e.g. `123456789`). That's your
   `TELEGRAM_CHAT_IDS`.

> To send to a **group** later: add your bot to the group, then use the group's
> chat id (a negative number). For just yourself, the personal id above is fine.

### 2. (Optional) CallMeBot WhatsApp

Skip unless you also want WhatsApp. Each recipient does this once: save
**+34 644 51 95 23** as a contact, WhatsApp it `I allow callmebot to send me
messages`, and note the **API key** it replies with + their **phone**. (Current
activation number: <https://www.callmebot.com/blog/free-api-whatsapp-messages/>.
CallMeBot delivery can be unreliable — Telegram is the dependable channel.)

### 3. Push to GitHub

```bash
git init
git add .
git commit -m "Pokemon drop notifier"
git branch -M main
git remote add origin https://github.com/<you>/pokemon-drop-notifier.git
git push -u origin main
```

### 4. Add your secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_BOT_TOKEN` → the token from BotFather
- `TELEGRAM_CHAT_IDS` → your chat id (or several, comma-separated: `111,222`)
- *(optional)* `CALLMEBOT_RECIPIENTS` → JSON array:
  `[{"name": "Me", "phone": "+41791234567", "apikey": "123456"}]`

### 5. Turn it on

- **Actions** tab → enable workflows → open **Pokemon Drop Notifier** → **Run workflow**.
- The **first run seeds state silently** (no spam for everything already listed).
  After that you only get alerts for *changes*.

Done. 🎉

---

## Configuring which shops to watch

The default shops are defined in `notifier.py` (`DEFAULT_SITES`). To change them
without editing code, set a repo **variable** named `SITES` (Settings → Secrets
and variables → Actions → *Variables*) to a JSON array — see
[`sites.local.json.example`](sites.local.json.example). Each entry:

**WooCommerce shop:**
```json
{
  "id": "cardcollectors",
  "type": "woocommerce",
  "label": "CardCollectors",
  "api_url": "https://SHOP/wp-json/wc/store/v1/products?brand=3038&orderby=date&order=desc",
  "brand_id": "3038",
  "name_filter": "(EN)"
}
```
- Find a brand id: open `https://SHOP/wp-json/wc/store/v1/products/brands?per_page=100`
  and read the `id` for the brand you want. (Or use `category=<id>` in `api_url`
  and look at `…/products/categories`.)

**wog.ch:**
```json
{
  "id": "wog",
  "type": "wog",
  "label": "WOG.ch",
  "platform_id": "tc",
  "tag": "392",
  "order_by": "releasedate",
  "name_filter": "-EN-",
  "match_field": "seriesName",
  "platform_name": "Trading Cards",
  "max_pages": 8
}
```
- `platform_id` `"tc"` = Trading Cards; `tag` `"392"` = the "Pokémon TCG" genre.
  To watch a different genre/brand, open the platform's filter sidebar on wog and
  read the tag id from the `Pokémon TCG`-style checkbox (`value="392"`).
- `name_filter` is matched against `match_field` (default `seriesName`, which
  keeps the `-EN-` language marker — the display title does not).

`id` must be unique per shop (it namespaces the saved state). `label` is what
shows up in the WhatsApp message.

---

## Testing locally (optional)

```bash
pip install -r requirements.txt
cp config.local.json.example config.local.json   # add your recipients
# optional: cp sites.local.json.example sites.local.json  # to tweak shops
python notifier.py
```

First local run seeds `state.json`; later runs alert on changes. `state.json`,
`config.local.json` and `sites.local.json` are git-ignored.

---

## Notes, limits & honest expectations

- **wog.ch reality:** at the time of writing, wog tracks **14 English Pokémon
  TCG products, all currently out of stock** ("no longer available"); what's in
  stock is mostly accessories and German (`-DE-`) items. That's fine: the
  notifier is exactly what tells you when one of those English products
  **restocks** or a **new English product appears**.
- **wog.ch language detection:** wog strips the `-EN-` suffix from the display
  title, so matching the title alone misses most English products (which have
  English *names* but no token). The adapter matches `seriesName` instead, which
  keeps the marker — this is why it finds 14, not 5.
- **Speed:** GitHub's scheduled jobs are throttled and often run **5–15 min
  late**. Great for restocks / new listings; **not** fast enough to win drops
  that sell out in seconds (that needs sub-minute polling + auto-checkout).
- **Near-real-time upgrade:** to poll every ~60s, turn `main()` into a loop
  (poll → sleep 60s → repeat) up to the 6-hour job limit, with the cron just
  restarting it. Ask and I'll wire it up.
- **Resilience:** if one shop's request fails, the run logs it and continues
  with the others; that shop's previous state is preserved (no false "new"
  alerts next run).
- **CallMeBot** is a free third-party relay (it only messages people who opted
  in). Messages are spaced out to respect its rate limits.
- **Be polite to the shops:** the default ~5-min cadence is light. Don't crank
  the cron to every few seconds.
