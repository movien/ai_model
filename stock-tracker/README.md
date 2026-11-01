# Stock & Price Tracker

Watches any product at any retailer and alerts you when it comes back in stock or its price
drops. It was set up for the **NVIDIA RTX 5090**, but it works for any product.

- **Any product, any number of retailers.** Each product has one or more *listings* (where to buy it).
- **Add your own retailers.** Most shop pages work with no setup. When one doesn't, you add a few CSS
  selectors or keywords, or point the tracker at a JSON API.
- **Adjustable refresh rate** for all products, one product, or one listing (`30s`, `2m`, `1h`, ...).
- **Price tracking.** Every check is saved in a local SQLite database. You get alerts for a target
  price, a max price (to skip scalper listings) and price drops, plus history and CSV export.
- **Alerts** in the terminal, as phone push notifications (ntfy), or via Discord, Telegram, email,
  a desktop popup, or any webhook.
- **Polite to sites.** It waits between requests to the same site, backs off after errors, and
  respects `Retry-After`. It **doesn't try to get past bot protection.** A site that blocks
  automated checks shows up as `BLOCKED` (see [Bot protection](#bot-protection)).

## Setup

Python 3.10 or newer.

```bash
cd stock-tracker
pip install -r requirements.txt
python tracker.py init        # creates config.yaml with an RTX 5090 starter entry
python tracker.py list
```

`config.yaml` is your file. CLI commands edit it, you can edit it by hand, and a running tracker
reloads it automatically. All options are documented in [`config.example.yaml`](config.example.yaml).

## Quick start: RTX 5090

1. **Best Buy** (most reliable, uses Best Buy's official API). Get a free API key at
   <https://developer.bestbuy.com> (approval can take a while), then set it:

   ```bash
   export BESTBUY_API_KEY=your-key            # macOS / Linux
   setx BESTBUY_API_KEY your-key              # Windows (then open a new terminal)
   ```

   The starter config tracks the Founders Edition by its SKU, the `skuId=` number in the Best Buy URL.
   Pasting any Best Buy product URL also works, since the SKU is read from the URL:

   ```bash
   python tracker.py add-listing "NVIDIA RTX 5090" "https://www.bestbuy.com/site/...6614151.p?skuId=6614151"
   ```

2. **Micro Center.** The starter config already tracks four RTX 5090 models there. Micro Center
   stock is **per store**. The starter config uses the **Phoenix** store (ID 205). To change it,
   set another store by name or 3-digit ID:

   ```bash
   python tracker.py stores                                  # list store names and IDs
   python tracker.py add-retailer microcenter --set store=tustin
   python tracker.py test-url "https://www.microcenter.com/product/690450/..."   # check it reads the page
   ```

   To watch a second store, add the same URL again with `--store`:

   ```bash
   python tracker.py add-listing rtx "https://www.microcenter.com/product/690450/..." --store "St. Davids"
   ```

3. **Other stores.** Paste product page URLs. Each one is test-fetched right away, so you see
   whether it works:

   ```bash
   python tracker.py add-listing "NVIDIA RTX 5090" "https://www.example-store.com/rtx-5090-product-page" --label "ASUS Astral"
   ```

4. **Phone alerts (free, no account).** Install the **ntfy** app, subscribe to a hard-to-guess topic
   name, and put the same name in `config.yaml`:

   ```yaml
   notifications:
     ntfy:
       topic: rtx5090-alerts-k93hf
   ```

   Then run `python tracker.py test-notify`.

5. **Run it.**

   ```bash
   python tracker.py check      # check everything once, print a table
   python tracker.py run        # keep watching; Ctrl+C to stop
   ```

## Tracking other products

```bash
# Product + listings in one go. Prices accept "$1,999" style input.
python tracker.py add-product "PS5 Pro" https://store-a.com/ps5-pro https://store-b.com/p/123 \
    --target-price 649 --interval 5m

# More listings later
python tracker.py add-listing "PS5 Pro" https://store-c.com/products/ps5-pro --retailer shopify --variant "Disc Edition"

# Change prices / refresh rate / name, or pause a product
python tracker.py edit-product "PS5 Pro" --target-price 599 --max-price 750
python tracker.py edit-product "PS5 Pro" --disable          # --enable to resume
python tracker.py edit-product "PS5 Pro" --max-price none   # clear a value

python tracker.py remove-listing "PS5 Pro" 2                # numbers come from `list`
python tracker.py remove-product "PS5 Pro"                  # its history stays in the database
```

Product names can be shortened when unambiguous (`rtx` matches "NVIDIA RTX 5090").

## Retailers

`python tracker.py retailers` lists them. Built in:

| Key       | Type       | Use for |
|-----------|------------|---------|
| `generic` | `html`     | Any product page (the default when no retailer matches the URL) |
| `shopify` | `shopify`  | Any Shopify store; reads the store's public product data, including variants |
| `bestbuy` | `json_api` | Best Buy, through the official Products API (needs `BESTBUY_API_KEY`) |
| `microcenter` | `html` | Micro Center product pages, with per-store stock (`store:`; see `python tracker.py stores`) |

The retailer for a listing comes from its `retailer:` field. If that isn't set, the URL's domain is
matched against each retailer's `domains`. If nothing matches, `generic` is used.

### How pages are read (`html` type)

The tracker tries these in order and uses the first one that answers:

1. **Your selectors**, if the retailer has them: `price_selector`, `price_regex`, `stock_selector`.
2. **schema.org structured data** (JSON-LD). Most shops publish it for Google Shopping, so most pages
   work without any setup.
3. **Microdata and meta tags** (`itemprop="price"`, `product:price:amount`, ...).
4. **Keywords** in the page text: `out_of_stock_text` is checked first, then `in_stock_text`.

### Adding a retailer whose pages don't work out of the box

Use `test-url` to try options on a real product page until both price and stock are found, then save
them:

```bash
python tracker.py test-url https://www.shop.com/item/123
#   Price: -   not found
#   Stock: IN STOCK   from page text contains 'add to cart'

python tracker.py test-url https://www.shop.com/item/123 --price-selector ".product-price .now"
#   Price: $1,999.99   from selector '.product-price .now'

python tracker.py add-retailer shopcom --name "Shop.com" --domain shop.com \
    --price-selector ".product-price .now" --out-of-stock-text "Sold out" --in-stock-text "Add to cart"
```

To find a selector, right-click the price in your browser, choose **Inspect**, and use the element's
class or id (`.class-name` or `#id`). `--stock-selector` limits the keyword search to one element
(such as the buy button), which avoids false matches from "related products" sections.

### Retailers with per-store stock (Micro Center)

A retailer can turn a listing's `store:` into URL parameters. The built-in Micro Center does this
with `?storeid=`:

```yaml
retailers:
  microcenter:
    store: tustin            # default for every Micro Center listing (name or "101")
products:
  - name: NVIDIA RTX 5090
    listings:
      - url: https://www.microcenter.com/product/690450/...
      - url: https://www.microcenter.com/product/690450/...
        store: st-davids     # the same card at a second store (tracked separately)
```

Write numeric IDs in quotes (`"025"`). Unquoted, YAML reads `025` as an octal number. The store list
comes from the open-source [streetmerchant](https://github.com/jef/streetmerchant) project, plus
Phoenix (205). Other newer stores may be missing; use their 3-digit ID directly. Your own retailers can do the same with
`url_params: {storeid: "{store}"}` and an optional `stores:` name-to-ID map.

The Micro Center page reading (price in `span#pricing`, stock text in `#pnlInventory`) also follows
streetmerchant. Micro Center blocked every request from the cloud server this was built on, so it
hasn't been checked against a live page. Run `test-url` once from your own connection. If price or
stock comes out wrong, adjust it with
`python tracker.py add-retailer microcenter --price-selector ... --stock-selector ...`.

### Any JSON API (`json_api` type)

```yaml
retailers:
  mystore:
    name: My Store
    type: json_api
    url: "https://api.mystore.com/v1/items/{sku}?key={api_key}"   # {fields} come from the retailer or listing
    api_key: "${MYSTORE_API_KEY}"
    price_path: data.price            # dotted path into the JSON; list items by number: items.0.price
    in_stock_path: data.available     # true/false, a quantity, or text like "InStock"
    title_path: data.name
    link_path: data.url
    sku_pattern: '/item/(\d+)'        # optional: pull the SKU out of a pasted URL
```

### Bot protection

Big retailers (Amazon, Newegg, Micro Center, B&H, Walmart, and stock-alert sites such as
Trackalacker) often put product pages behind Cloudflare or similar bot checks. When that happens the
listing shows **`BLOCKED`**, and the tracker waits longer before trying again. It deliberately doesn't
try to get around these checks: no headless browsers, CAPTCHA solving or proxy rotation. That breaks
those sites' rules and tends to get your IP banned.

Whether a site blocks you depends a lot on your network: home connections are blocked far less than
cloud servers. If a site keeps blocking you:

- use the retailer's **official API** if it has one (that's why Best Buy is built in),
- track the same product at **other retailers**, especially smaller shops and Shopify stores, or
- if a site only rejects the default User-Agent text, you can change `user_agent` (globally or per
  retailer). Keep the refresh rate modest and check the site's terms of use for automated access.

## Refresh rate

| Scope           | Command                                                  | Config field |
|-----------------|----------------------------------------------------------|--------------|
| Default         | `python tracker.py interval 5m`                          | `settings.default_interval` |
| One product     | `python tracker.py interval 2m --product "RTX 5090"`     | product `interval:` |
| One listing     | `python tracker.py interval 90s --product rtx --listing 1` | listing `interval:` |

- Nothing is checked more often than `settings.min_interval` (default `1m`, hard floor `30s`).
- `per_host_delay` (default `5s`) spaces out requests to the same website. A retailer's `min_delay`
  overrides it.
- After errors or blocks, the wait doubles each time, up to `max_backoff` (default `1h`).
- Each wait varies by ±10% (`jitter`) so checks don't land at the same moment.

## Price tracking and alerts

Per product:

| Field          | Meaning |
|----------------|---------|
| `target_price` | Alert when a listing's price is at or below this. |
| `max_price`    | In-stock offers above this don't count, so you're not alerted about $5,000 scalper listings. You *are* alerted when a listing drops to or below it. |

Alerts fire on **changes** compared with the previous check, so you get one message per event
instead of one per check:

- **IN STOCK**: became buyable (in stock and within `max_price`). The very first check counts too.
- **Target price**: reached `target_price` (and not out of stock).
- **Price drop**: fell by at least `price_drop_min_percent` (default 1%).

History:

```bash
python tracker.py history rtx          # current, lowest, highest, last time in stock, recent changes
python tracker.py export prices.csv    # every check, for Excel / Google Sheets
```

## Notifications

A channel is on as soon as its fields are filled in. Use environment variables to keep secrets out of
the file, e.g. `bot_token: "${TELEGRAM_BOT_TOKEN}"`. Test with `python tracker.py test-notify`.

| Channel   | Config |
|-----------|--------|
| Terminal  | `console: true` (on by default), `bell: true` |
| Phone push | `ntfy.topic` (install the ntfy app; free) |
| Discord   | `discord.webhook_url` (Server settings, Integrations, Webhooks) |
| Telegram  | `telegram.bot_token` (from @BotFather) and `telegram.chat_id` |
| Email     | `email.smtp_host`, `port`, `username`, `password`, `from`, `to` (Gmail needs an app password) |
| Webhook   | `webhook.url`: JSON POST with title, message, url, price, ... |
| Desktop   | `desktop: true` (best with `pip install plyer`) |

## Running it all the time

- Simplest: leave `python tracker.py run -q` open in a terminal (`-q` prints only alerts and problems).
- **Windows:** Task Scheduler, then *Create Basic Task*, *When I log on*, *Start a program*:
  `pythonw.exe` with arguments `C:\path\to\stock-tracker\tracker.py run -q`.
- **macOS / Linux:** `nohup python3 tracker.py run -q >> tracker.log 2>&1 &`, or a systemd user service:

  ```ini
  # ~/.config/systemd/user/stock-tracker.service
  [Service]
  WorkingDirectory=%h/stock-tracker
  ExecStart=/usr/bin/python3 tracker.py run -q
  Restart=on-failure
  [Install]
  WantedBy=default.target
  ```

  Then run `systemctl --user enable --now stock-tracker`.

While it runs you can add products or change settings from another terminal. The change is picked
up within a few seconds.

## Command reference

| Command | What it does |
|---------|--------------|
| `init` | Create `config.yaml` from the example |
| `list` | Products, listings, refresh rates, last status |
| `check [--product P] [--no-alerts]` | Check everything once |
| `run [--product P] [-q]` | Keep checking and alert on changes |
| `add-product NAME [URL...] [--target-price] [--max-price] [--interval]` | Track a product |
| `add-listing PRODUCT [URL] [--retailer] [--sku] [--variant] [--store] [--label] [--interval]` | Add where to buy |
| `edit-product NAME [--rename] [--target-price] [--max-price] [--interval] [--disable/--enable]` | Change a product |
| `remove-product NAME`, `remove-listing PRODUCT N` | Stop tracking |
| `interval EVERY [--product P [--listing N]]` | Set the refresh rate |
| `retailers`, `add-retailer KEY ...`, `remove-retailer KEY` | Manage retailers |
| `test-url URL [--store S] [selector options]` | Show what the tracker can read from a page |
| `stores [RETAILER]` | Store names and IDs for retailers with per-store stock |
| `history [PRODUCT]`, `export FILE.csv` | Price history |
| `settings [KEY [VALUE]]` | Show or change global settings |
| `test-notify` | Send a test alert |

`--config PATH` (or the `STOCK_TRACKER_CONFIG` env var) uses a different config file.
`-v` turns on debug logging.

## Troubleshooting

- **`no price or stock info found`**: the page has no structured data. Use `test-url` with
  selectors, then `add-retailer` (see above).
- **`BLOCKED`**: the site rejects automated checks. See [Bot protection](#bot-protection).
- **`Best Buy needs api_key`**: set `BESTBUY_API_KEY` (and open a new terminal on Windows).
- **Wrong stock status from keywords**: add a `stock_selector` so only the buy button's text is
  checked.
- **Config errors** list every problem with its product and listing number. Fix them and the
  tracker reloads. If a CLI edit went wrong, the previous file is in `config.yaml.bak`.

## Development

```bash
python -m unittest discover -s tests     # runs offline against a local test server
```

Code: `stocktracker/config.py` (config and validation), `fetch.py` (HTTP, bot-check detection),
`extract.py` (page/API parsing), `storage.py` (SQLite history), `alerts.py`, `notify.py`,
`engine.py` (checks and scheduler), `cli.py`.
