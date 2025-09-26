#!/usr/bin/env python3
"""
main.py - Amazon deals -> Telegram bot
Features:
 - Loads config from env (and .env for local testing)
 - Concurrent scraping (aiohttp) with semaphore
 - ASIN extraction via regex
 - Batched Amazon API calls (with exponential backoff)
 - Telegram messaging (photo with caption or text)
 - Persistent sent_products.json (rotates >7 days)
 - Safe defaults suited for hourly runs via GitHub Actions
"""

import os
import sys
import re
import time
import json
import random
import logging
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import List

# Async HTTP
import asyncio
import aiohttp

# Sync HTTP
import requests
from bs4 import BeautifulSoup

# Amazon API (make sure the package name matches what you installed)
from amazon_paapi import AmazonApi

# dotenv for local testing (the workflow uses GitHub secrets)
from dotenv import load_dotenv

# === Load .env for local dev (silent if not present) ===
load_dotenv()

# === Config from env ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
AMAZON_ACCESS_KEY = os.getenv("AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY = os.getenv("AMAZON_SECRET_KEY")
AMAZON_PARTNER_TAG = os.getenv("AMAZON_PARTNER_TAG", "yourtag-21")
AMAZON_REGION = os.getenv("AMAZON_REGION", "IN")

DATA_DIR = os.path.expanduser(os.getenv("DEALS_DATA_DIR", "~/.amazon_deals"))
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
SENT_PRODUCTS_FILE = os.path.join(DATA_DIR, "sent_products.json")

CONCURRENCY = int(os.getenv("DEALS_CONCURRENCY", "3"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))  # batch size to query Amazon API per call
LOG_FILE = os.getenv("LOG_FILE", os.path.join(DATA_DIR, "amazon_deals_bot.log"))
MAX_PRODUCTS_PER_RUN = int(os.getenv("MAX_PRODUCTS_PER_RUN", "200"))
DELAY_BETWEEN_MESSAGES = int(os.getenv("DELAY_BETWEEN_MESSAGES", "5"))

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("amazon_deals_bot")

# === Helper: exponential backoff decorator ===
def backoff(max_tries=5, base_delay=1.0, factor=2.0, jitter=0.5, on_exception=(Exception,)):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            tries = 0
            while tries < max_tries:
                try:
                    return fn(*args, **kwargs)
                except on_exception as e:
                    tries += 1
                    if tries >= max_tries:
                        logger.error("Backoff: max tries reached for %s: %s", fn.__name__, e)
                        raise
                    sleep_for = base_delay * (factor ** (tries - 1)) + random.uniform(0, jitter)
                    logger.warning("Backoff: %s failed (try %d/%d): %s. Sleeping %.2fs",
                                   fn.__name__, tries, max_tries, e, sleep_for)
                    time.sleep(sleep_for)
        return wrapper
    return decorator

# === Bot class ===
class AmazonTelegramDealsBot:
    def __init__(self, telegram_bot_token: str, telegram_channel_id: str):
        if not (telegram_bot_token and telegram_channel_id and AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY):
            logger.error("Missing essential environment variables. Exiting.")
            raise SystemExit("Missing configuration")

        # Amazon PAAPI client (throttling param left as default, tune if needed)
        self.amazon = AmazonApi(
            AMAZON_ACCESS_KEY,
            AMAZON_SECRET_KEY,
            AMAZON_PARTNER_TAG,
            AMAZON_REGION,
            throttling=3
        )

        # Telegram
        self.bot_token = telegram_bot_token
        self.channel_id = telegram_channel_id
        self.telegram_api_url = f"https://api.telegram.org/bot{telegram_bot_token}"

        # local state
        self.sent_products_file = SENT_PRODUCTS_FILE
        self.sent_products = self.load_sent_products()

        # user agents
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
        ]

        # high commission categories (priority order)
        self.high_commission_categories = [
            'Fashion', 'Clothing', 'Shoes', 'Jewelry', 'Watches', 'Bags',
            'Home & Kitchen', 'Sports & Fitness', 'Beauty & Personal Care',
            'Toys & Games', 'Books', 'Health & Household', 'Electronics', 'Computers', 'Mobile Phones'
        ]

        # initial deals URLs (can extend)
        self.deals_urls = [
            "https://www.amazon.in/deals?&linkCode=ll2",
            "https://www.amazon.in/gp/goldbox?&linkCode=ll2",
            "https://www.amazon.in/deals?discountRanges=10-,&sortBy=BY_SCORE",
            # category nodes (examples) - extend these if needed
            "https://www.amazon.in/s?k=deals&rh=n%3A1571271031",  # Fashion
            "https://www.amazon.in/s?k=deals&rh=n%3A1380263031",  # Home & Kitchen
            "https://www.amazon.in/s?k=deals&rh=n%3A1355016031",  # Sports
            "https://www.amazon.in/s?k=deals&rh=n%3A1374618031",  # Beauty
            "https://www.amazon.in/s?k=deals&rh=n%3A1350380031",  # Toys
            "https://www.amazon.in/s?k=deals&rh=n%3A976442031",   # Books
        ]

        # ASIN patterns
        self.asin_patterns = [
            r'/dp/([A-Z0-9]{10})',
            r'/gp/product/([A-Z0-9]{10})',
            r'data-asin=["\']?([A-Z0-9]{10})["\']?',
            r'asin["\']?\s*[:=]\s*["\']([A-Z0-9]{10})["\']',
            r'data-csa-c-asin=["\']([A-Z0-9]{10})["\']',
            r'data-testid=".*?([A-Z0-9]{10})"'
        ]

        self.consecutive_failures = 0
        self.base_delay = 2

    # ---------- persistence ----------
    def load_sent_products(self):
        try:
            if os.path.exists(self.sent_products_file):
                with open(self.sent_products_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # keep entries not older than 7 days
                cutoff = datetime.now() - timedelta(days=7)
                cleaned = {k: v for k, v in data.items() if datetime.fromisoformat(v) > cutoff}
                return cleaned
        except Exception as e:
            logger.exception("Error loading sent products: %s", e)
        return {}

    def save_sent_products(self):
        try:
            with open(self.sent_products_file, "w", encoding="utf-8") as f:
                json.dump(self.sent_products, f, indent=2)
        except Exception as e:
            logger.exception("Error saving sent products: %s", e)

    def is_product_already_sent(self, asin: str) -> bool:
        return asin in self.sent_products

    def mark_product_as_sent(self, asin: str):
        self.sent_products[asin] = datetime.now().isoformat()
        self.save_sent_products()

    def get_random_user_agent(self) -> str:
        return random.choice(self.user_agents)

    # ---------- scraping (async) ----------
    async def _fetch_page(self, session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore, timeout: int = 20):
        headers = {
            "User-Agent": self.get_random_user_agent(),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        async with sem:
            try:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    resp.raise_for_status()
                    return await resp.text()
            except Exception as e:
                logger.warning("Failed to fetch %s : %s", url, e)
                return None

    def extract_asins_from_multiple_pages(self, max_products=1000) -> List[str]:
        logger.info("🔍 Scraping %d sources with concurrency=%d", len(self.deals_urls), CONCURRENCY)

        async def _main():
            sem = asyncio.Semaphore(CONCURRENCY)
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                tasks = [self._fetch_page(session, url, sem) for url in self.deals_urls]
                pages = await asyncio.gather(*tasks)
                return pages

        pages = asyncio.run(_main())
        all_asins = set()
        for p in pages:
            if not p:
                continue
            for pattern in self.asin_patterns:
                matches = re.findall(pattern, p)
                for m in matches:
                    if len(m) == 10 and m.isalnum():
                        all_asins.add(m)

        logger.info("Found %d unique ASINs across pages", len(all_asins))
        new_asins = [a for a in all_asins if not self.is_product_already_sent(a)]
        logger.info("%d ASINs are new (not sent before)", len(new_asins))
        return new_asins[:max_products]

    # ---------- Amazon API calls ----------
    @backoff(max_tries=5, base_delay=2.0, factor=2.0, jitter=1.0, on_exception=(Exception,))
    def fetch_items_batch(self, asins: List[str]):
        """
        Call amazon.get_items on a batch of ASINs. If Amazon API raises throttle errors,
        the backoff decorator will retry.
        """
        try:
            items = self.amazon.get_items(asins)
            return items or []
        except Exception as e:
            # Let backoff handle retry; raise to trigger it
            logger.exception("Amazon API batch error: %s", e)
            raise

    def get_product_details_single(self, item_obj):
        """
        Convert an Amazon API returned item object to our product dict.
        Accepts already-retrieved item object (not raw ASIN).
        """
        try:
            asin = getattr(item_obj, "asin", None) or getattr(item_obj, "ASIN", None)
            product_details = {
                "asin": asin,
                "title": "N/A",
                "primary_image": "N/A",
                "current_price": "N/A",
                "mrp": "N/A",
                "discount": "N/A",
                "discount_percentage": 0,
                "availability": "N/A",
                "category_score": 0,
                "affiliate_url": f"https://www.amazon.in/dp/{asin}?tag={AMAZON_PARTNER_TAG}&linkCode=ogi&th=1&psc=1"
            }

            # item_info.title
            if getattr(item_obj, "item_info", None) and getattr(item_obj.item_info, "title", None):
                title = item_obj.item_info.title.display_value
                product_details["title"] = title
                product_details["category_score"] = self.get_category_priority_score(title)

            if getattr(item_obj, "images", None) and getattr(item_obj.images, "primary", None):
                product_details["primary_image"] = item_obj.images.primary.large.url

            # offers/listings
            if getattr(item_obj, "offers", None) and getattr(item_obj.offers, "listings", None):
                listing = item_obj.offers.listings[0]
                if getattr(listing, "price", None):
                    product_details["current_price"] = f"₹{listing.price.amount}"
                if getattr(listing, "saving_basis", None):
                    product_details["mrp"] = f"₹{listing.saving_basis.amount}"
                    if getattr(listing, "price", None):
                        try:
                            discount_amount = listing.saving_basis.amount - listing.price.amount
                            discount_percentage = (discount_amount / listing.saving_basis.amount) * 100
                            product_details["discount"] = f"₹{discount_amount:.0f} ({discount_percentage:.0f}% off)"
                            product_details["discount_percentage"] = discount_percentage
                        except Exception:
                            pass
                if getattr(listing, "availability", None):
                    product_details["availability"] = listing.availability.message

            # Validate minimal fields
            if (product_details["title"] != "N/A" and
                    product_details["current_price"] != "N/A" and
                    product_details["discount_percentage"] >= 10):
                return product_details
        except Exception as e:
            logger.exception("Error parsing item: %s", e)
        return None

    # ---------- category scoring ----------
    def get_category_priority_score(self, product_title: str, browse_node=None) -> int:
        title_lower = (product_title or "").lower()
        category_keywords = {
            'Fashion': ['dress', 'shirt', 'trouser', 'fashion', 'clothing', 'apparel'],
            'Clothing': ['clothing', 'wear', 'fabric', 'cotton', 'silk', 'denim'],
            'Shoes': ['shoes', 'sneakers', 'boots', 'sandals', 'footwear', 'heel'],
            'Jewelry': ['jewelry', 'ring', 'necklace', 'earring', 'bracelet', 'chain'],
            'Watches': ['watch', 'smartwatch', 'timepiece', 'wrist'],
            'Bags': ['bag', 'backpack', 'handbag', 'purse', 'wallet', 'luggage'],
            'Home & Kitchen': ['kitchen', 'home', 'cookware', 'utensil', 'furniture', 'decor'],
            'Sports & Fitness': ['sports', 'fitness', 'gym', 'exercise', 'yoga', 'cricket'],
            'Beauty & Personal Care': ['beauty', 'cosmetic', 'skincare', 'makeup', 'perfume'],
            'Toys & Games': ['toy', 'game', 'kids', 'children', 'puzzle', 'doll'],
            'Books': ['book', 'novel', 'guide', 'textbook', 'story'],
            'Health & Household': ['health', 'wellness', 'medicine', 'supplement', 'vitamin']
        }
        for i, category in enumerate(self.high_commission_categories):
            keywords = category_keywords.get(category, [category.lower()])
            if any(k in title_lower for k in keywords):
                return 100 - i
        return 0

    # ---------- messaging ----------
    def format_product_message(self, product: dict) -> str:
        title = product.get("title", "Unknown product")
        if len(title) > 120:
            title = title[:117] + "..."

        category_emoji = "🏷️"
        score = product.get("category_score", 0)
        if score > 90:
            category_emoji = "👕"
        elif score > 85:
            category_emoji = "🏠"
        elif score > 80:
            category_emoji = "💄"
        elif score > 75:
            category_emoji = "🎮"

        message = (
            f"{category_emoji} *MEGA DEAL ALERT!* 🔥\n\n"
            f"📦 *{title}*\n\n"
            f"💰 *Price:* {product.get('current_price')}\n"
            f"🏷️ *MRP:* {product.get('mrp')}\n"
            f"🎯 *You Save:* {product.get('discount')}\n"
            f"✅ *Status:* {product.get('availability')}\n\n"
            f"🛒 {product.get('affiliate_url')}\n\n"
            f"#AmazonDeals #MegaSavings #ShopNow"
        )
        return message

    def send_telegram_message(self, message: str, image_url: str = None) -> bool:
        try:
            if image_url and image_url != "N/A":
                url = f"{self.telegram_api_url}/sendPhoto"
                data = {
                    "chat_id": self.channel_id,
                    "photo": image_url,
                    "caption": message,
                    "parse_mode": "Markdown"
                }
            else:
                url = f"{self.telegram_api_url}/sendMessage"
                data = {
                    "chat_id": self.channel_id,
                    "text": message,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False
                }
            resp = requests.post(url, data=data, timeout=15)
            if resp.status_code == 200 and resp.json().get("ok", False):
                return True
            logger.warning("Telegram API returned non-ok: %s %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.exception("Telegram send error: %s", e)
            return False

    # ---------- main pipeline ----------
    def process_all_deals_to_telegram(self, max_products=MAX_PRODUCTS_PER_RUN, delay_between_messages=DELAY_BETWEEN_MESSAGES):
        logger.info("=" * 40)
        logger.info("START RUN - Processing deals")
        logger.info("=" * 40)

        # Step 1: extract ASINs
        asins = self.extract_asins_from_multiple_pages(max_products=max_products)
        if not asins:
            logger.info("No new ASINs found. Exiting run.")
            return

        logger.info("Processing %d ASINs...", len(asins))

        # Step 2: batch Amazon API calls
        products_with_scores = []
        for i in range(0, len(asins), BATCH_SIZE):
            batch = asins[i:i + BATCH_SIZE]
            logger.info("Querying Amazon API for batch %d - size %d", i // BATCH_SIZE + 1, len(batch))
            try:
                items = self.fetch_items_batch(batch)
            except Exception as e:
                logger.exception("Failed to fetch batch from Amazon: %s", e)
                continue

            # items may be list of item objects
            for item_obj in items:
                product = self.get_product_details_single(item_obj)
                if product:
                    products_with_scores.append(product)
                    logger.info("   Added product %s (score %.1f, discount %.1f%%)",
                                product.get("asin"), product.get("category_score", 0), product.get("discount_percentage", 0))

            # sleep between batches to be gentle on the API
            time.sleep(random.uniform(1.5, 3.5))

        if not products_with_scores:
            logger.info("No product passed filtering (discount/fields). Exiting run.")
            return

        # Step 3: sort by priority: category_score * 2 + discount_percentage
        products_with_scores.sort(key=lambda x: (x.get("category_score", 0) * 2 + x.get("discount_percentage", 0)), reverse=True)

        # Step 4: send to Telegram (with small delay)
        successful_sends = 0
        for idx, product in enumerate(products_with_scores, 1):
            logger.info("Sending %d/%d : %s", idx, len(products_with_scores), product["asin"])
            msg = self.format_product_message(product)
            if self.send_telegram_message(msg, product.get("primary_image")):
                successful_sends += 1
                self.mark_product_as_sent(product["asin"])
                logger.info("Sent successfully: %s", product["asin"])
            else:
                logger.warning("Failed to send: %s", product["asin"])
            time.sleep(delay_between_messages + random.uniform(0, 2))

        logger.info("Run finished. Sent %d products", successful_sends)
        logger.info("Database size: %d", len(self.sent_products))
        logger.info("=" * 40)

    # ---------- utility ----------
    def test_telegram_connection(self) -> bool:
        try:
            url = f"{self.telegram_api_url}/getMe"
            r = requests.get(url, timeout=10)
            if r.status_code == 200 and r.json().get("ok", False):
                bot_user = r.json()["result"].get("username", "unknown")
                logger.info("Telegram connected! Bot username: @%s", bot_user)
                return True
            logger.error("Telegram connection failed: %s", r.text)
            return False
        except Exception as e:
            logger.exception("Telegram connection error: %s", e)
            return False


# ---------- CLI execution ----------
def main():
    bot = AmazonTelegramDealsBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID)
    if not bot.test_telegram_connection():
        logger.error("Telegram connection test failed - check TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID")
        return
    bot.process_all_deals_to_telegram(max_products=MAX_PRODUCTS_PER_RUN, delay_between_messages=DELAY_BETWEEN_MESSAGES)


if __name__ == "__main__":
    main()
