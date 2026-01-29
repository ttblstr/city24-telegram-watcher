#!/usr/bin/env python3
import json
import re
import time
import traceback
import asyncio
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from playwright.sync_api import sync_playwright

# python-telegram-bot v20+ is async
from telegram import Bot
from telegram.constants import ParseMode


BASE_DIR = Path(__file__).parent
SEEN_FILE = BASE_DIR / "seen.json"
CONFIG_FILE = BASE_DIR / "config.yaml"


def load_config():
    if not CONFIG_FILE.exists():
        raise SystemExit("config.yaml missing (workflow should generate it).")
    return yaml.safe_load(CONFIG_FILE.read_text())


def load_seen():
    """
    seen.json format:
      {
        "<search_url_1>": ["listing_url_a", "listing_url_b", ...],
        "<search_url_2>": [...],
        ...
      }
    """
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_seen(seen_map):
    SEEN_FILE.write_text(json.dumps(seen_map, indent=2, ensure_ascii=False))


def normalize_url(base, href):
    if not href:
        return None
    return urljoin(base, href.split("#")[0])


def extract_price(text):
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*\d)\s*€", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(" ", ""))
    except ValueError:
        return None


def send_telegram(bot_token, chat_id, text):
    """
    Correctly send messages using python-telegram-bot v20+ (async API).
    """
    bot = Bot(token=bot_token)

    async def _send():
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

    # Run the coroutine in an event loop safely
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # unlikely in our script, but safe
        return asyncio.run_coroutine_threadsafe(_send(), loop).result()
    else:
        return asyncio.run(_send())


def looks_like_listing(url: str) -> bool:
    """
    Filter out homepage and obvious non-listing pages.
    This is heuristic; it prevents notifying https://www.city24.lv/
    """
    if not url:
        return False
    u = urlparse(url)

    if not u.netloc.endswith("city24.lv"):
        return False

    # Not homepage
    if u.path in ["", "/"]:
        return False

    # Reject obvious non-listing pages
    bad_prefixes = [
        "/lietosanas-noteikumi",
        "/privatuma-politika",
        "/kontakti",
        "/login",
        "/register",
        "/meklet",
        "/help",
    ]
    if any(u.path.startswith(p) for p in bad_prefixes):
        return False

    # Require some depth in path to reduce menus
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        return False

    return True


def scrape_listings(page, search_url, max_visit=40):
    """
    Scrape a City24 search results page and return listing detail pages:
      [(url, title, price, snippet), ...]
    """
    page.goto(search_url, timeout=60000)
    page.wait_for_selector("body", timeout=15000)
    time.sleep(2)

    anchors = set()

    # Heuristic selectors
    selectors = [
        "a:has-text('Uzzināt vairāk')",
        "article a",
        ".card a",
        ".offer a",
        ".announcement a",
        ".listing a",
        ".item a",
        "a.card-link",
        "a",
    ]

    for sel in selectors:
        try:
            for el in page.query_selector_all(sel):
                href = el.get_attribute("href")
                if not href:
                    continue

                # only internal or city24 links
                if urlparse(href).netloc == "" or "city24.lv" in href:
                    anchors.add(href)
        except Exception:
            continue

    anchors_full = [normalize_url(search_url, h) for h in anchors]
    anchors_full = [
        u for u in anchors_full
        if u and urlparse(u).netloc.endswith("city24.lv")
    ]

    # Important: filter out junk (homepage etc.)
    anchors_full = sorted({u for u in anchors_full if looks_like_listing(u)})

    print("Anchors found on search page:", len(anchors_full))
    print("First 10 anchors:", anchors_full[:10])

    found = []
    visited = 0

    for u in anchors_full:
        if visited >= max_visit:
            break
        visited += 1
        try:
            page.goto(u, timeout=60000)
            page.wait_for_selector("body", timeout=10000)

            # Title
            title = ""
            try:
                h1 = page.query_selector("h1")
                if h1:
                    title = h1.inner_text().strip()
            except Exception:
                pass
            if not title:
                try:
                    title = page.title() or ""
                except Exception:
                    title = ""

            # Price
            price_text = ""
            for sel in [".price", ".object-price", ".offer-price", ".ad-price", ".price-block", ".price--value"]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        price_text = el.inner_text().strip()
                        break
                except Exception:
                    pass

            if not price_text:
                txt = page.inner_text("body")[:5000]
                m = re.search(r"(\d[\d\s]*\d)\s*€", txt)
                if m:
                    price_text = m.group(0)

            # Snippet
            snippet = ""
            try:
                sn = (
                    page.query_selector(".short-description")
                    or page.query_selector(".object-description")
                    or page.query_selector("p")
                )
                if sn:
                    snippet = (sn.inner_text() or "").strip()[:250]
            except Exception:
                snippet = ""

            price = extract_price(price_text)
            found.append((u, title, price, snippet))

        except Exception:
            print("Error visiting listing:", u)
            traceback.print_exc()
            continue

    return found


def main():
    cfg = load_config()

    bot_token = cfg.get("telegram", {}).get("bot_token")
    chat_id = cfg.get("telegram", {}).get("chat_id")
    if not bot_token or not chat_id:
        raise SystemExit("Missing telegram.bot_token or telegram.chat_id in config.yaml")

    search_urls = cfg.get("search_urls")
    if not search_urls:
        single = cfg.get("search_url")
        if single:
            search_urls = [single]
    if not search_urls or not isinstance(search_urls, list):
        raise SystemExit("Missing search_urls (list) or search_url in config.yaml")

    minp = cfg.get("filters", {}).get("min_price")
    maxp = cfg.get("filters", {}).get("max_price")
    max_per_run = cfg.get("message", {}).get("max_per_run", 10)

    seen_map = load_seen()
    for su in search_urls:
        seen_map.setdefault(su, [])

    notifications = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_extra_http_headers({"Accept-Language": "lv,en;q=0.9"})

        for su in search_urls:
            print("Loading search:", su)
            listings = scrape_listings(page, su, max_visit=40)
            seen_set = set(seen_map.get(su, []))

            for u, title, price, snippet in listings:
                if u in seen_set:
                    continue

                if price is not None:
                    if minp is not None and price < minp:
                        continue
                    if maxp is not None and price > maxp:
                        continue

                notifications.append((u, title, price, snippet))
                seen_set.add(u)

                if len(notifications) >= max_per_run:
                    break

            seen_map[su] = sorted(list(seen_set))

            if len(notifications) >= max_per_run:
                break

        browser.close()

    if not notifications:
        print("No new listings found.")
        save_seen(seen_map)
        return

    for u, title, price, snippet in notifications:
        price_str = f"{price} €" if price else "N/A"
        text = f"<b>{title or 'Listing'}</b>\nPrice: {price_str}\n{snippet}\n{u}"
        send_telegram(bot_token, chat_id, text)
        print("Notified:", u)

    save_seen(seen_map)
    print("Done.")


if __name__ == "__main__":
    main()
