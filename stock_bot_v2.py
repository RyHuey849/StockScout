"""Discord stock-alert bot, v2 — multi-user.

Anyone in the server can track products via slash commands (/addproduct);
the bot checks them on a schedule and pings the person who added an item
when it comes back in stock. See PLAN.md for the full design.
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import discord
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
# Optional: syncing slash commands to one guild is instant; global sync can
# take up to an hour to propagate.
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "3"))
MAX_PRODUCTS_PER_USER = int(os.getenv("MAX_PRODUCTS_PER_USER", "10"))

DB_PATH = "stock_state.db"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

REQUEST_DELAY_SECONDS = 5  # politeness delay between product checks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("stock_bot")

# ---------------------------------------------------------------------------
# URL canonicalization — one product, one key, regardless of link variant
# ---------------------------------------------------------------------------

# Query params that never identify a product, only where the link came from.
TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "tag"}


def _canonical_target(host: str, path: str, query: list) -> str | None:
    # Target identifies products by TCIN; the slug before /-/A-<tcin> is
    # cosmetic and varies between links to the same item.
    match = re.search(r"(?i)/a-(\d+)", path)
    if match:
        return f"target.com/p/A-{match.group(1)}"
    return None


SITE_CANONICALIZERS = {
    "target.com": _canonical_target,
}


def make_url_key(url: str) -> str:
    """Canonical dedup key for a product URL: scheme/www/trailing-slash
    insensitive, tracking params stripped, remaining params sorted, plus
    site-specific rules (e.g. Target's TCIN)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    for domain, canonicalize in SITE_CANONICALIZERS.items():
        if host == domain or host.endswith("." + domain):
            key = canonicalize(host, path, query_pairs)
            if key:
                return key
    query = urlencode(sorted(query_pairs))
    return host + path + ("?" + query if query else "")


# ---------------------------------------------------------------------------
# Storage (SQLite) — products table is the live product list
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        # A products table from v1 (hardcoded-list era) lacks the owner
        # columns; move it aside rather than break on insert.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='products'"
        ).fetchone()
        if row:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if "added_by_id" not in columns:
                conn.execute("ALTER TABLE products RENAME TO products_v1_backup")
                log.info("Renamed old v1 products table to products_v1_backup")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                url TEXT PRIMARY KEY,
                url_key TEXT,
                name TEXT NOT NULL,
                added_by_id INTEGER NOT NULL,
                added_by_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_checked TEXT,
                added_at TEXT NOT NULL,
                image_url TEXT,
                price TEXT
            )
            """
        )
        # Migrate v2 tables created before newer columns existed, and backfill.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
        if "url_key" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN url_key TEXT")
        for column in ("image_url", "price"):
            if column not in columns:
                conn.execute(f"ALTER TABLE products ADD COLUMN {column} TEXT")
        for row in conn.execute(
            "SELECT url FROM products WHERE url_key IS NULL"
        ).fetchall():
            conn.execute(
                "UPDATE products SET url_key = ? WHERE url = ?",
                (make_url_key(row["url"]), row["url"]),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_url_key "
            "ON products(url_key)"
        )
        # Outbox for in-stock alerts raised outside the bot process (e.g. a
        # check triggered from the web dashboard); the bot drains it on a
        # short interval so Discord pings still go out.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                name TEXT NOT NULL,
                added_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def get_product(url: str) -> sqlite3.Row | None:
    """Look up a tracked product by URL, matching any variant of the link
    (www/no-www, trailing slash, tracking params, site-specific aliases)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE url_key = ?", (make_url_key(url),)
        ).fetchone()


def add_product(
    url: str,
    name: str,
    user_id: int,
    user_name: str,
    image_url: str | None = None,
    price: str | None = None,
) -> None:
    """Insert a product. Raises sqlite3.IntegrityError if any variant of
    this URL is already tracked."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO products
                (url, url_key, name, added_by_id, added_by_name, added_at,
                 image_url, price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, make_url_key(url), name, user_id, user_name, now, image_url, price),
        )


def remove_product(url: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM products WHERE url_key = ?", (make_url_key(url),)
        )


def update_status(url: str, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE products SET status = ?, last_checked = ? WHERE url = ?",
            (status, now, url),
        )


def products_for_user(user_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE added_by_id = ? ORDER BY added_at",
            (user_id,),
        ).fetchall()


def all_products() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM products ORDER BY added_at").fetchall()


def queue_alert(product: sqlite3.Row) -> None:
    """Queue an in-stock alert for the bot to deliver (used by processes
    other than the bot, e.g. the dashboard API)."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pending_alerts (url, name, added_by_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (product["url"], product["name"], product["added_by_id"], now),
        )


def drain_pending_alerts() -> list[sqlite3.Row]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM pending_alerts ORDER BY id").fetchall()
        if rows:
            conn.execute(
                "DELETE FROM pending_alerts WHERE id <= ?", (rows[-1]["id"],)
            )
        return rows


# ---------------------------------------------------------------------------
# Stock detection — site-specific parsers first, generic heuristic fallback
# ---------------------------------------------------------------------------

OUT_OF_STOCK_PHRASES = [
    "out of stock",
    "sold out",
    "currently unavailable",
    "temporarily unavailable",
    "notify me when",
    "back in stock",  # as in "email me when back in stock"
]

IN_STOCK_PHRASES = [
    "add to cart",
    "add to basket",
    "add to bag",
    "in stock",
    "buy now",
]


def _fetch_html(url: str) -> str | None:
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Fetch failed for %s: %s", url, exc)
        return None
    return resp.text


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(separator=" ").lower()


def check_generic(url: str) -> bool | None:
    """Phrase-based heuristic that works on many e-commerce sites with zero
    setup. Out-of-stock phrases win over in-stock ones because sold-out
    pages often still render a (disabled) add-to-cart button."""
    html = _fetch_html(url)
    if html is None:
        return None
    text = _visible_text(html)
    if any(phrase in text for phrase in OUT_OF_STOCK_PHRASES):
        return False
    if any(phrase in text for phrase in IN_STOCK_PHRASES):
        return True
    log.warning("No stock phrases found on %s — can't determine status", url)
    return None


def check_target(url: str) -> bool | None:
    """Target renders availability client-side and its API is bot-guarded,
    so render the page in headless Chromium and read the fulfillment
    section. Requires playwright (`playwright install chromium`)."""
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        log.error("playwright is not installed — can't check %s", url)
        return None

    selector = '[data-test="@web/AddToCart/FulfillmentSection"]'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 900},
            )
            try:
                page.goto(url, timeout=90_000, wait_until="domcontentloaded")
                page.wait_for_selector(selector, timeout=30_000)
                # The section appears before availability data populates it —
                # let the page settle before reading the final text.
                page.wait_for_timeout(8000)
                text = page.inner_text(selector).lower()
            finally:
                browser.close()
    except PlaywrightError as exc:
        log.warning("Browser check failed for %s: %s", url, exc)
        return None

    if not text.strip():
        log.warning("Empty fulfillment section on %s — layout may have changed", url)
        return None
    return not any(phrase in text for phrase in ("out of stock", "sold out"))


# Domain → parser. Checked before the generic fallback; add entries here for
# sites where the heuristic proves unreliable.
SITE_PARSERS = {
    "target.com": check_target,
}


def check_url(url: str) -> bool | None:
    """Return True/False for stock status, or None if undeterminable."""
    host = (urlparse(url).hostname or "").lower()
    for domain, parser in SITE_PARSERS.items():
        if host == domain or host.endswith("." + domain):
            return parser(url)
    return check_generic(url)


def _json_ld_price(node) -> tuple[str, str | None] | None:
    """Recursively find a schema.org offer price in parsed JSON-LD."""
    if isinstance(node, dict):
        price = node.get("price") or node.get("lowPrice")
        if price not in (None, ""):
            return str(price), node.get("priceCurrency")
        for value in node.values():
            found = _json_ld_price(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _json_ld_price(item)
            if found:
                return found
    return None


def _format_price(amount: str, currency: str | None) -> str:
    amount = str(amount).strip()
    if currency in (None, "", "USD"):
        return amount if amount.startswith("$") else f"${amount}"
    return f"{amount} {currency}"


def fetch_page_metadata(url: str) -> dict:
    """Best-effort product name, thumbnail, and price from page markup.
    Any of the values may be None — pages vary wildly."""
    meta = {"name": None, "image_url": None, "price": None}
    html = _fetch_html(url)
    if html is None:
        return meta
    soup = BeautifulSoup(html, "html.parser")

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        meta["name"] = " ".join(og_title["content"].split())[:150] or None
    elif soup.title and soup.title.string:
        meta["name"] = " ".join(soup.title.string.split())[:150] or None

    og_image = soup.find("meta", property="og:image") or soup.find(
        "meta", attrs={"name": "twitter:image"}
    )
    if og_image and og_image.get("content"):
        meta["image_url"] = urljoin(url, og_image["content"].strip())

    # Price: og/product meta tags, then JSON-LD offers, then itemprop.
    amount = currency = None
    for prop in ("product:price:amount", "og:price:amount"):
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            amount = tag["content"]
            cur_tag = soup.find("meta", property=prop.rsplit(":", 1)[0] + ":currency")
            currency = cur_tag.get("content") if cur_tag else None
            break
    if amount is None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            found = _json_ld_price(data)
            if found:
                amount, currency = found
                break
    if amount is None:
        tag = soup.find(attrs={"itemprop": "price"})
        if tag:
            amount = tag.get("content") or tag.get_text(strip=True)
    if amount and str(amount).strip():
        meta["price"] = _format_price(amount, currency)
    return meta


def check_all_products_sync() -> list[tuple[sqlite3.Row, str]]:
    """Check every tracked product; update stored state; return rows that
    just transitioned from out-of-stock to in-stock."""
    newly_in_stock = []
    for i, product in enumerate(all_products()):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            result = check_url(product["url"])
        except Exception:
            log.exception("Check crashed for %s", product["url"])
            continue
        if result is None:
            continue  # keep previous state when undeterminable
        status = "in_stock" if result else "out_of_stock"
        update_status(product["url"], status)
        log.info("%s: %s (was %s)", product["name"], status, product["status"])
        if status == "in_stock" and product["status"] == "out_of_stock":
            newly_in_stock.append((product, status))
    return newly_in_stock


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

# Slash commands don't need the privileged Message Content intent.
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler()


def _clean_url(url: str) -> str:
    # Discord users often paste URLs wrapped in <> to suppress the embed.
    url = url.strip().strip("<>")
    return url


def _is_admin(user: discord.User | discord.Member) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


def _alert_line(product: sqlite3.Row) -> str:
    return (
        f"🟢 <@{product['added_by_id']}> **{product['name']}** is back in stock!\n"
        f"{product['url']}"
    )


async def announce(lines: list[str], channel: discord.abc.Messageable) -> None:
    for line in lines:
        await channel.send(line)


async def run_checks_and_alert() -> None:
    newly_in_stock = await asyncio.to_thread(check_all_products_sync)
    if not newly_in_stock:
        return
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("Channel %s not found — check DISCORD_CHANNEL_ID", CHANNEL_ID)
        return
    await announce([_alert_line(p) for p, _ in newly_in_stock], channel)


@bot.event
async def setup_hook() -> None:
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", GUILD_ID)
    else:
        await bot.tree.sync()
        log.info("Slash commands synced globally (may take up to an hour to appear)")


async def deliver_queued_alerts() -> None:
    """Send alerts queued by other processes (e.g. the dashboard API)."""
    rows = await asyncio.to_thread(drain_pending_alerts)
    if not rows:
        return
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("Channel %s not found — check DISCORD_CHANNEL_ID", CHANNEL_ID)
        return
    await announce([_alert_line(row) for row in rows], channel)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s", bot.user)
    if not scheduler.running:
        scheduler.add_job(
            run_checks_and_alert,
            "interval",
            hours=CHECK_INTERVAL_HOURS,
            next_run_time=datetime.now(timezone.utc),  # run once at startup
        )
        scheduler.add_job(deliver_queued_alerts, "interval", seconds=60)
        scheduler.start()
        log.info("Scheduler started: checking every %s hours", CHECK_INTERVAL_HOURS)


@bot.tree.command(description="Start tracking a product")
@app_commands.describe(url="Link to the product page")
async def addproduct(interaction: discord.Interaction, url: str) -> None:
    url = _clean_url(url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        await interaction.response.send_message(
            "That doesn't look like a valid product URL.", ephemeral=True
        )
        return

    existing = get_product(url)
    if existing:
        if existing["added_by_id"] == interaction.user.id:
            await interaction.response.send_message(
                "You're already tracking that product.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"That product is already tracked by **{existing['added_by_name']}**.",
                ephemeral=True,
            )
        return

    if len(products_for_user(interaction.user.id)) >= MAX_PRODUCTS_PER_USER:
        await interaction.response.send_message(
            f"You're already tracking {MAX_PRODUCTS_PER_USER} products (the limit). "
            "Remove one with `/removeproduct` first.",
            ephemeral=True,
        )
        return

    # Fetching the page takes longer than the 3s interaction deadline.
    await interaction.response.defer()
    meta = await asyncio.to_thread(fetch_page_metadata, url)
    name = meta["name"] or (parsed.hostname + parsed.path)[:100]
    try:
        add_product(
            url,
            name,
            interaction.user.id,
            interaction.user.display_name,
            meta["image_url"],
            meta["price"],
        )
    except sqlite3.IntegrityError:
        await interaction.followup.send("That product is already being tracked.")
        return

    result = await asyncio.to_thread(check_url, url)
    if result is None:
        await interaction.followup.send(
            f"Now tracking **{name}** — but I couldn't determine its stock status "
            "yet. I'll keep trying on the schedule."
        )
        return
    status = "in_stock" if result else "out_of_stock"
    update_status(url, status)
    label = "in stock 🟢" if result else "out of stock 🔴"
    await interaction.followup.send(
        f"Now tracking **{name}** for {interaction.user.mention} — currently {label}. "
        "You'll get a ping here when it comes back."
        if not result
        else f"Now tracking **{name}** for {interaction.user.mention} — it's {label} right now!"
    )


@bot.tree.command(description="Stop tracking a product")
@app_commands.describe(url="Link to the product page you want to stop tracking")
async def removeproduct(interaction: discord.Interaction, url: str) -> None:
    url = _clean_url(url)
    product = get_product(url)
    if product is None:
        await interaction.response.send_message(
            "That URL isn't being tracked.", ephemeral=True
        )
        return
    if product["added_by_id"] != interaction.user.id and not _is_admin(interaction.user):
        await interaction.response.send_message(
            f"Only **{product['added_by_name']}** (who added it) or a server "
            "admin can remove that product.",
            ephemeral=True,
        )
        return
    remove_product(url)
    await interaction.response.send_message(
        f"Stopped tracking **{product['name']}**."
    )


STATUS_COLORS = {
    "in_stock": discord.Color.green(),
    "out_of_stock": discord.Color.red(),
}
STATUS_LABELS = {
    "in_stock": "🟢 In stock",
    "out_of_stock": "🔴 Out of stock",
}


def _product_embed(row: sqlite3.Row, show_owner: bool) -> discord.Embed:
    embed = discord.Embed(
        title=row["name"][:256],
        url=row["url"],
        color=STATUS_COLORS.get(row["status"], discord.Color.light_grey()),
    )
    if row["image_url"]:
        embed.set_thumbnail(url=row["image_url"])
    embed.add_field(name="Status", value=STATUS_LABELS.get(row["status"], "⚪ Unknown"))
    if row["price"]:
        embed.add_field(name="Price (when added)", value=row["price"])
    if row["last_checked"]:
        checked = datetime.fromisoformat(row["last_checked"])
        embed.add_field(name="Last checked", value=f"<t:{int(checked.timestamp())}:R>")
    if show_owner:
        embed.set_footer(text=f"Added by {row['added_by_name']}")
    return embed


async def _send_embeds(
    interaction: discord.Interaction,
    embeds: list[discord.Embed],
    ephemeral: bool = False,
) -> None:
    """Send embeds in batches of 10 (Discord's per-message limit): the
    first batch as the interaction response, the rest as follow-ups."""
    for i in range(0, len(embeds), 10):
        batch = embeds[i : i + 10]
        if interaction.response.is_done():
            await interaction.followup.send(embeds=batch, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embeds=batch, ephemeral=ephemeral)


@bot.tree.command(description="List the products you're tracking")
async def myproducts(interaction: discord.Interaction) -> None:
    rows = products_for_user(interaction.user.id)
    if not rows:
        await interaction.response.send_message(
            "You aren't tracking any products. Try `/addproduct`.", ephemeral=True
        )
        return
    embeds = [_product_embed(row, show_owner=False) for row in rows]
    await _send_embeds(interaction, embeds, ephemeral=True)


@bot.tree.command(name="list", description="List everything everyone is tracking")
async def list_all(interaction: discord.Interaction) -> None:
    rows = all_products()
    if not rows:
        await interaction.response.send_message(
            "Nothing is being tracked yet. Try `/addproduct`.", ephemeral=True
        )
        return
    await _send_embeds(interaction, [_product_embed(row, show_owner=True) for row in rows])


@bot.tree.command(description="Manually trigger a check of all tracked products")
async def checknow(interaction: discord.Interaction) -> None:
    count = len(all_products())
    if count == 0:
        await interaction.response.send_message(
            "Nothing is being tracked yet. Try `/addproduct`.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"Checking {count} product(s) now…")
    newly_in_stock = await asyncio.to_thread(check_all_products_sync)
    # A full check can outlive the interaction's 15-minute followup window,
    # so report results directly to the channel instead.
    channel = interaction.channel
    if newly_in_stock:
        await announce([_alert_line(p) for p, _ in newly_in_stock], channel)
    else:
        await channel.send("Done — no new stock changes.")


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and "
            "fill in your bot token and channel ID."
        )
    if not CHANNEL_ID:
        raise SystemExit("DISCORD_CHANNEL_ID is not set (see .env.example).")
    init_db()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
