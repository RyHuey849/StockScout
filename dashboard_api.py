"""Web dashboard API for the stock-alert bot.

Runs as a separate process from the bot, sharing the same SQLite DB and
reusing the bot module's scraping/DB helpers. Serves the built React app
from dashboard/dist at / and a JSON API under /api.

Run: python dashboard_api.py   (default http://127.0.0.1:8000)

No authentication — binds to localhost by default. If you expose it on a
LAN/VPN, anyone who can reach it can add/remove products.
"""

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import stock_bot_v2 as core

HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
# Products added via the dashboard are attributed to this Discord user ID
# (set it to yours so in-stock pings mention you); 0 shows as "Dashboard".
DASHBOARD_USER_ID = int(os.getenv("DASHBOARD_USER_ID", "0"))
DASHBOARD_USER_NAME = os.getenv("DASHBOARD_USER_NAME", "Dashboard")

DIST_DIR = Path(__file__).parent / "dashboard" / "dist"

app = FastAPI(title="StockScout dashboard")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class AddProductRequest(BaseModel):
    url: str


@app.get("/api/products")
def list_products() -> list[dict]:
    return [_row_to_dict(row) for row in core.all_products()]


@app.post("/api/products")
def add_product(body: AddProductRequest) -> dict:
    url = body.url.strip().strip("<>")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(422, "That doesn't look like a valid product URL.")
    existing = core.get_product(url)
    if existing:
        raise HTTPException(
            409, f"Already tracked (added by {existing['added_by_name']})."
        )
    meta = core.fetch_page_metadata(url)
    name = meta["name"] or (parsed.hostname + parsed.path)[:100]
    try:
        core.add_product(
            url, name, DASHBOARD_USER_ID, DASHBOARD_USER_NAME,
            meta["image_url"], meta["price"],
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Already tracked.")
    result = core.check_url(url)
    if result is not None:
        core.update_status(url, "in_stock" if result else "out_of_stock")
    return _row_to_dict(core.get_product(url))


@app.delete("/api/products")
def delete_product(url: str) -> dict:
    product = core.get_product(url)
    if product is None:
        raise HTTPException(404, "That URL isn't being tracked.")
    core.remove_product(url)
    return {"removed": product["name"]}


@app.post("/api/check")
def check_now() -> dict:
    """Check every tracked product now. Newly-in-stock items are queued so
    the bot still delivers the Discord pings (within ~a minute)."""
    newly_in_stock = core.check_all_products_sync()
    for product, _status in newly_in_stock:
        core.queue_alert(product)
    return {
        "checked": len(core.all_products()),
        "newly_in_stock": [_row_to_dict(p) for p, _ in newly_in_stock],
    }


if DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="dashboard")


def main() -> None:
    core.init_db()
    if not DIST_DIR.is_dir():
        core.log.warning(
            "dashboard/dist not found — API only. Build the frontend with "
            "`npm run build` in dashboard/."
        )
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
