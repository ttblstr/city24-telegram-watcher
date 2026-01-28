#!/usr/bin/env python3
"""
check_city24.py
- Loads a City24 search URL
- Extracts listing URLs + title + price
- Sends Telegram messages for new listings
- Stores seen URLs in seen.json
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from playwright.sync_api import sync_playwright
from telegram import Bot

# Paths
BASE_DIR = Path(__file__).parent
SEEN_FILE = BASE_DIR / "seen.json"
CONFIG_FILE = BASE_DIR / "config.yaml"

# Helpers
def load_config():
    if not CONFIG_FILE.exists():
        raise SystemExit("Create config.yaml from config.example.yaml and fill in your values.")
    return yaml.safe_load(CONFIG_FILE.read_text())

def load_seen():
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text())
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()

def save_seen(seen_set):
    SEEN_FILE.write_text(json.dumps(sorted(list(seen_set)), indent=2, ensure_ascii=False))

def normalize_url(base, href):
    if href is None:
        return None
    return urljoin(base, href)

def extract_price(text):
    if not text:
        return None
    m = re.search(r'(\d[\d\s]*\d)\s*€', text)
    if m:
        try:
            return int(m.group(1).replace(" ", ""))
        except ValueError:
            return None
    return None

def send_telegram(bot_token, chat_id, text):
    bot = Bot(token=bot_token)
    bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)

def main():
    cfg = load_config()
    search_url = cfg.get("search_url")
    if not search_url:
        raise SystemExit("search_url missing in config.yaml")

    bot_token = cfg.get("telegram", {}).get("bot_token")
    chat_id = cfg.get("telegram", {}).get("chat_id")
    if not bot_token or not chat_id:
        raise SystemExit("Telegram bot_token/chat_id required in config.yaml")

    # load seen
    seen = load_seen()

    found = []   # list of tuples (url, title, price, snippet)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # set a reasonable user agent / language hint
        page.set_extra_http_headers({"Accept-Language": "lv,en;q=0.9"})

        print("Loading:", search_url)
        page.goto(search_url, timeout=60000)
        try:
            page.wait_for_selector("body", timeout=15000)
        except Exception:
            pass
        time.sleep(1)

        anchors = set()

        # heuristics: links with "Uzzināt vairāk"
        try:
            els = page.query_selector_all("a:has-text('Uzzināt vairāk')")
            for e in els:
                href = e.get_attribute("href")
                if href:
                    anchors.add(href)
        except Exception:
            pass

        # find anchors in likely card selectors
        for sel in ["article a", ".card a", ".offer a", ".announcement a", ".listing a", ".item a", "a.card-link"]:
            try:
                for e in page.query_selector_all(sel):
                    href = e.get_attribute("href")
                    if href:
                        anchors.add(href)
            except Exception:
                continue

        # fallback: all anchors with ad-like patterns
        try:
            for e in page.query_selector_all("a"):
                href = e.get_attribute("href")
                if not href:
                    continue
                if re.search(r"(sludinajum|sludinājums|announce|announcement|ad|offer|announcements)", href, re.I):
                    anchors.add(href)
        except Exception:
            pass

        anchors_full = [normalize_url(search_url, h) for h in anchors]
        anchors_full = [u for u in anchors_full if u and urlparse(u).netloc.endswith("city24.lv")]

        for u in sorted(set(anchors_full)):
            try:
                if len(found) >= 200:
                    break
                print("Checking", u)
                page.goto(u, timeout=60000)
                page.wait_for_selector("body", timeout=10000)
                title = ""
                try:
                    h1 = page.query_selector("h1")
                    if h1:
                        title = h1.inner_text().strip()
                except Exception:
                    pass
                if not title:
                    try:
                        title = page.title()
                    except Exception:
                        title = ""

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
                    m = re.search(r'(\d[\d\s]*\d)\s*€', txt)
                    if m:
                        price_text = m.group(0)

                snippet = ""
                try:
                    sn = page.query_selector(".short-description") or page.query_selector(".object-description") or page.query_selector("p")
                    if sn:
                        snippet = (sn.inner_text() or "").strip()[:300]
                except Exception:
                    snippet = ""

                price = extract_price(price_text) or None
                found.append((u, title or "", price, snippet))
            except Exception as e:
                print("Error visiting", u, e)
                continue

        browser.close()

    # filter new
    new = []
    for u, title, price, snippet in found:
        if u not in seen:
            minp = cfg.get("filters", {}).get("min_price")
            maxp = cfg.get("filters", {}).get("max_price")
            if price is not None:
                if minp is not None and price < minp:
                    continue
                if maxp is not None and price > maxp:
                    continue
            new.append((u, title, price, snippet))
            if len(new) >= cfg.get("message", {}).get("max_per_run", 10):
                break

    if not new:
        print("No new listings found.")
        return

    text_template = "<b>{title}</b>\nPrice: {price}\n{snippet}\n{url}"
    for u, title, price, snippet in new:
        price_str = f"{price} €" if price else "N/A"
        text = text_template.format(title=title or "Listing", price=price_str, snippet=snippet, url=u)
        try:
            send_telegram(bot_token, chat_id, text)
            print("Notified:", u)
            seen.add(u)
        except Exception as e:
            print("Telegram send failed:", e)

    save_seen(seen)


if __name__ == "__main__":
    main()
