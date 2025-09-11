import requests
from bs4 import BeautifulSoup
import re
import time
from amazon_paapi import AmazonApi
import random
import sys
import traceback


class AmazonTelegramDealsBot:
    def __init__(self, telegram_bot_token, telegram_channel_id):
        # Initialize Amazon PA API with throttling
        self.amazon = AmazonApi(
            "AKPAORP8DX1757347889",
            "0zo+YXhJRPqkKO/YCfszbQ9Eo1Sk8hcRryMf22sa",
            "akki22784-21",
            "IN",
            throttling=5  # 5 sec between API calls
        )

        self.bot_token = telegram_bot_token
        self.channel_id = telegram_channel_id
        self.telegram_api_url = f"https://api.telegram.org/bot{telegram_bot_token}"

        # Session for scraping
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
        })

        self.consecutive_failures = 0
        self.base_delay = 5

    def extract_asins_from_deals_page(self, deals_url):
        """Extract ASINs from deals page, handle 503 gracefully"""
        print(f"Scraping deals from: {deals_url}")
        try:
            response = self.session.get(deals_url, timeout=20)
            if response.status_code == 503:
                print("⚠️ Amazon returned 503 (blocked or throttled). Skipping this run.")
                return []

            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')

            asins = set()
            patterns = [
                r'/dp/([A-Z0-9]{10})',
                r'/gp/product/([A-Z0-9]{10})',
                r'data-asin="([A-Z0-9]{10})"',
                r'asin["\']?\s*[:=]\s*["\']([A-Z0-9]{10})["\']',
                r'/([A-Z0-9]{10})/'
            ]

            page_content = response.text
            for pattern in patterns:
                matches = re.findall(pattern, page_content)
                for m in matches:
                    if len(m) == 10 and m.isalnum():
                        asins.add(m)

            print(f"✅ Found {len(asins)} ASINs")
            return list(asins)

        except Exception as e:
            print(f"❌ Error scraping deals page: {e}")
            traceback.print_exc()
            return []

    def get_product_details_single(self, asin):
        """Fetch product details with retries and backoff"""
        max_retries = 4
        for attempt in range(max_retries):
            try:
                delay = self.base_delay * (2 ** attempt) + random.uniform(0, 2)
                print(f"⏳ Waiting {delay:.1f}s before PA API call...")
                time.sleep(delay)

                items = self.amazon.get_items([asin])
                if not items:
                    print(f"❌ No data for ASIN {asin}")
                    continue

                item = items[0]
                product = {
                    'asin': asin,
                    'title': getattr(item.item_info.title, "display_value", "N/A") if item.item_info and item.item_info.title else "N/A",
                    'primary_image': item.images.primary.large.url if item.images and item.images.primary else "N/A",
                    'current_price': "N/A",
                    'mrp': "N/A",
                    'discount': "N/A",
                    'availability': "N/A",
                    'affiliate_url': f"https://www.amazon.in/dp/{asin}?tag=akki22784-21&linkCode=ogi&th=1&psc=1"
                }

                if item.offers and item.offers.listings:
                    listing = item.offers.listings[0]
                    if listing.price:
                        product['current_price'] = f"₹{listing.price.amount}"
                    if listing.saving_basis:
                        product['mrp'] = f"₹{listing.saving_basis.amount}"
                        if listing.price:
                            discount = listing.saving_basis.amount - listing.price.amount
                            perc = (discount / listing.saving_basis.amount) * 100
                            product['discount'] = f"₹{discount:.2f} ({perc:.1f}% off)"
                    if listing.availability:
                        product['availability'] = listing.availability.message

                if product['title'] != "N/A" and product['current_price'] != "N/A":
                    print(f"✅ Got product {asin}")
                    return product
                else:
                    print(f"⚠️ Incomplete product data for {asin}")
                    return None

            except Exception as e:
                print(f"❌ Error fetching {asin}: {e}")
                traceback.print_exc()
                if attempt < max_retries - 1:
                    print("🔁 Retrying...")
                    continue
        return None

    # (send_telegram_message, format_product_message remain unchanged)

    def process_all_deals_to_telegram(self, deals_url, max_products=5, delay_between_messages=3):
        print("=" * 60)
        print("AMAZON DEALS TO TELEGRAM BOT")
        print("=" * 60)

        asins = self.extract_asins_from_deals_page(deals_url)
        if not asins:
            print("⚠️ No ASINs found this run.")
            return

        asins = asins[:max_products]
        print(f"Processing {len(asins)} ASINs...")

        for asin in asins:
            product = self.get_product_details_single(asin)
            if product:
                msg = self.format_product_message(product)
                self.send_telegram_message(msg, product['primary_image'])
                print("⏳ Delay before next...")
                time.sleep(delay_between_messages)
            else:
                print(f"Skipped {asin}")
