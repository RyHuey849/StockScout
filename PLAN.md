# Discord Stock Alert Bot — Project Plan

## Goal
A Discord bot, usable by the whole server, that monitors product pages on
retailer websites and sends an alert when an item transitions from
**out of stock** to **in stock**. Anyone can add or remove their own
tracked products via Discord commands — no code editing required. Checks
run on a schedule (every few hours), not continuously.

## Architecture

1. **Discord bot** — `discord.py`. Posts alerts to a channel, pinging the
   specific person who added the product. Exposes self-service commands
   for managing tracked products.
2. **Stock checker** — two-layer detection:
   - **Site-specific parsers** (`SITE_PARSERS` dict, keyed by domain) for
     retailers you've added custom logic for — most accurate.
   - **Generic fallback** — scans page text for common phrases ("out of
     stock", "sold out", "add to cart", etc.) so *any* URL can be tracked
     without a friend needing to find a CSS selector themselves. Less
     precise, but zero setup.
   Uses `requests` + `BeautifulSoup`. For JS-rendered sites (stock status
   not present in raw HTML), swap in the Playwright-based example instead.
3. **Scheduler** — `APScheduler` (`AsyncIOScheduler`) running inside the
   same process as the bot, checking every tracked product on an interval
   (default: every 3 hours, via `CHECK_INTERVAL_HOURS`).
4. **State & product storage** — SQLite (`stock_state.db`), single
   `products` table. Each row stores: URL, who added it (Discord user ID
   + display name), last known status, and last checked time. This
   replaces the old hardcoded `PRODUCTS` list — products are now added
   and removed live, by anyone in the server.
5. **Hosting** — designed to run as a small always-on process (VPS, home
   server, Raspberry Pi). Needs a persistent Discord connection, so not
   suited to serverless/one-shot execution.

## Commands (usable by anyone in the server)

| Command | Description |
|---|---|
| `!addproduct <url>` | Start tracking a product |
| `!removeproduct <url>` | Stop tracking a product (only the person who added it, or a server admin, can remove it) |
| `!myproducts` | List products you're tracking |
| `!list` | List everything everyone is tracking |
| `!checknow` | Manually trigger a check of all products right now |

## Guardrails for shared/multi-user use

- **Per-user cap** — `MAX_PRODUCTS_PER_USER` (default 10) prevents any
  one person from overloading the schedule.
- **Ownership on removal** — only the adder (or a server admin) can
  remove a tracked product.
- **Targeted pings** — alerts `@mention` the person who added the item,
  not the whole channel, so people aren't pinged for products they don't
  care about.
- **Politeness/ToS considerations** — checking every few hours per
  product is reasonably light, but confirm target sites' `robots.txt` /
  ToS don't prohibit automated stock checks. A per-request delay and
  randomized user-agent are in place to avoid looking like abusive
  traffic; the per-user cap also limits total request volume as more
  people start using the bot.

## Files

- `stock_bot_v2.py` — main bot: config, DB helpers (products table),
  two-layer stock-check logic, Discord event handlers and self-service
  commands, scheduler setup.
- `requirements.txt` — Python dependencies (includes `python-dotenv`).
- `.env.example` — template for `DISCORD_BOT_TOKEN` and
  `DISCORD_CHANNEL_ID`; copy to `.env` and fill in real values (never
  commit `.env` itself).
- `stock_state.db` — created automatically on first run (SQLite,
  gitignored). Now stores the live product list, not just status.

## Setup Steps

1. Create a Discord bot application at
   https://discord.com/developers/applications → add a Bot → copy the
   token.
2. Invite the bot to your server with "Send Messages" permission. Enable
   the "Message Content Intent" in the Bot settings (required for reading
   command text).
3. Get your target channel's ID (enable Developer Mode in Discord →
   right-click channel → Copy ID).
4. Create a virtual environment and install dependencies:
   ```
   python -m venv venv
   source venv/bin/activate   # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```
5. Copy `.env.example` to `.env` and fill in your real
   `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`.
6. Run it: `python stock_bot_v2.py`
7. In Discord, try `!addproduct <url>` with a real product link to test.

## Known Gaps / Next Steps

- **Generic detection is a heuristic.** The phrase-matching fallback
  works on many e-commerce sites but can be fooled by unusual page
  layouts, or by pages that mention "out of stock" in an unrelated
  context (e.g. related-products section). Add a `SITE_PARSERS` entry
  for any site that proves unreliable.
- **JS-rendered sites.** If a target site's stock status isn't in the
  raw HTML, switch that check to the commented-out Playwright-based
  example in `stock_bot_v2.py`.
- **Resilience not yet built:**
  - No retry/backoff on failed requests.
  - No auto-restart if the process crashes (consider a systemd service
    file, `pm2`, or Docker + restart policy).
  - No alerting if detection silently fails for a URL — currently just
    logs a warning; could be upgraded to also notify the adder.
- **No per-guild scoping.** If the bot is ever added to more than one
  Discord server, all servers currently share the same product list and
  alert channel. Would need a `guild_id` column and per-guild filtering
  to properly separate them.

## Possible Future Enhancements

- Slash commands (`/addproduct`) instead of prefix commands, for a more
  modern Discord UX.
- Price-drop tracking in addition to stock status.
- Richer `!list` / `!myproducts` output (embeds with thumbnails, last
  checked time, price).
- Configurable per-product check interval (some items matter more than
  others).
- Web dashboard for managing tracked products outside of Discord.
