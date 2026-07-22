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
| [wellplayed.ch](https://www.wellplayed.ch/collections/pokemon) | Pokémon TCG products with `(EN)` in the title | Shopify `products.json` collection feed |
| [laschocards.ch](https://laschocards.ch/en/collections/pre-order) | **New** English pre-orders only (language is a variant) | Shopify feed, per-variant tracking, `new`-only alerts |
| [detsuki.ch](https://detsuki.ch/collections/pokemon) | English variants of Pokémon products (language is a variant) | Shopify feed, per-variant tracking |
| [theuncommonshop.ch](https://theuncommonshop.ch) | Sealed English Pokémon TCG (displays, ETBs, boxes, tins…) | WooCommerce Store API, category + `Sprache` attribute filters, polled every ~2 min |

---

## How it works

```
GitHub Actions job (a ~6h "shift", restarted by cron)
        │  loops every ~30 s
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
- **`shopify`** — fetches a Shopify collection's public `products.json` feed
  (e.g. `…/collections/pokemon/products.json`). Clean JSON with per-variant
  `available` + `price`; stock = any variant available. Filtered by a title
  substring.

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
- `exclude_category_terms` *(optional)* — skip products whose category names
  contain any of these substrings (e.g. `["japanisch"]` drops everything in a
  "Booster Displays (Japanisch)" category).
- `require_attribute` *(optional)* — e.g. `{"name": "Sprache", "value":
  "Englisch"}`: skip products that HAVE the attribute but lack the value.
  Products without the attribute are kept.
- `min_poll_seconds` *(optional, any site type)* — in loop mode, poll this site
  at most every N seconds (others keep the fast cadence). Use for shops with
  big catalogs or touchy rate limiting.

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

**Shopify shop:**
```json
{
  "id": "wellplayed",
  "type": "shopify",
  "label": "WellPlayed",
  "collection_url": "https://www.wellplayed.ch/collections/pokemon",
  "name_filter": "(EN)"
}
```
- `collection_url` is the shop's collection page; the adapter reads
  `<collection_url>/products.json`. Works on any Shopify shop — point it at the
  collection you want and set `name_filter` to a title substring.
- `variant_filter` *(optional)* — when language/edition is a Shopify **variant**
  rather than part of the title (e.g. a `Language: English/German/French`
  option), set this to the variant name (e.g. `"English"`). Each matching
  variant is then tracked on its own — its own stock, its own price, and a deep
  `?variant=…` link straight to that language.
- `alert_on` *(optional, any site type)* — which changes notify you. Defaults to
  `["new", "restock"]`. Use `["new"]` for a **pre-order shop** that re-lists the
  same item at ever-rising prices (you only want the first, retail-priced drop);
  use `["restock"]` if you only care about back-in-stock events.

`id` must be unique per shop (it namespaces the saved state). `label` is what
shows up in the WhatsApp message.

---

## Auto-buyer (wog.ch) — optional, VPS only

`wog_buyer.py` can log into your wog.ch account and **automatically grab a target
product** (default: the "30th Celebration" English set) the instant the notifier
spots it — beating the sold-out window. It uses the same endpoints wog's own site
uses: `POST /authenticate` to log in and `POST /ajax.putIntoCart` to add to cart
(both captcha-free); checkout is on invoice ("Kauf auf Rechnung").

**This runs on a private box, never on GitHub Actions.** Your login must not go
near a public repo, and the buyer is *inert unless a `WOG_BUY_*` env var is set* —
so the public runner keeps doing detection-only, and a small VPS (~5 CHF/month)
does the buying with fast polling.

⚠️ **Honest caveats:** auto-purchasing is against wog's ToS (same as the scalper
bots you're racing). Worst realistic case is a cancelled order or account ban —
it's your account on the line. The buyer has hard safety rails: a keyword
whitelist, a `WOG_BUY_MAX_PRICE` ceiling, and a one-order-per-product ledger
(`bought.json`) so it can never double-buy. Every action sends you a Telegram
receipt.

**Reply BUY (the normal control).** Every wog.ch alert on Telegram ends with
"↩️ Reply BUY to grab this one". Reply **BUY** to that message and the bot buys
*that exact product* — re-checked English-only and under your price cap — then
replies with the result (`✅ Ordered`, `🛒 In your cart — tap to pay`, or
`❌ likely sold out`). You stay in control and approve each purchase, but it's
one tap. Active automatically whenever Telegram is set and buying is armed
(`WOG_BUY_ENABLED=1`) or in dry-run (`WOG_BUY_DRYRUN=1`, where BUY just replies
"would buy"). It reads replies via Telegram long-polling — no public URL needed.

**Keyword full-auto (optional).** Set `WOG_AUTOBUY_KEYWORDS=1` to *also* buy any
fresh wog item matching `WOG_BUY_KEYWORDS` with no reply needed — unattended.
Off by default; reply-BUY is the recommended control.

**Modes** (`WOG_BUY_MODE`):
- `cart` *(default, works today)* — adds the item to your cart and pings you a
  link to tap **Pay**. The item is reserved in seconds; you confirm on your phone.
- `auto` — also places the invoice order with no interaction. Enabled after a
  **one-time checkout recon on the VPS**: with a target item in the cart, run
  `python wog_buyer.py recon-checkout <productID>` (it walks up to — never
  through — the confirm button and dumps `checkout_*.html`), then the final
  confirm step gets wired to your account's real payment form.

**Setup on the VPS:**
```bash
git clone https://github.com/<you>/PokemonPing.git && cd PokemonPing
pip install -r requirements.txt
cp .env.example .env      # fill in WOG_USERNAME/PASSWORD, Telegram, keep DRYRUN=1 at first
python wog_buyer.py self-test          # offline sanity check
set -a; . ./.env; set +a
python wog_buyer.py login-test         # confirm the login works
python notifier.py                     # runs detection + (dry-run) buyer in a loop
```
Once a dry run has pinged you a correct "🧪 WOULD BUY" for a real target, set
`WOG_BUY_ENABLED=1` (and `WOG_BUY_MODE=auto` after the checkout recon) to arm it.

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
- **Speed:** the notifier runs in **loop mode** — one Actions job polls every
  ~30 s (set the `POLL_SECONDS` repo variable to tune) for up to ~5h45m, then
  exits and the cron starts the next shift. Typical alert latency is **under a
  minute**. There's a small gap (~1–2 min) at each shift handover every ~6 h,
  and this is only free because **public repos get unlimited Actions minutes**
  — a private repo would burn ~43,000 min/month, far past any paid plan's
  included minutes. Unset `POLL_SECONDS` (local runs) = one single pass.
- **Resilience:** if one shop's request fails, the run logs it and continues
  with the others; that shop's previous state is preserved (no false "new"
  alerts next run).
- **No repeat spam:** items are never forgotten once seen — a flaky feed (CDN
  cache) that briefly drops a product can't make its comeback look "new" —
  and the same alert is never re-sent within 30 min
  (`ALERT_COOLDOWN_SECONDS` to tune).
- **CallMeBot** is a free third-party relay (it only messages people who opted
  in). Messages are spaced out to respect its rate limits.
- **Be polite to the shops:** ~30 s polling of lightweight JSON endpoints is
  acceptable, but don't push it much lower — if a shop starts erroring or
  blocking, raise `POLL_SECONDS` (e.g. to 60 or 120).
