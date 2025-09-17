import requests
from bs4 import BeautifulSoup
import re
import time
from amazon_paapi import AmazonApi
import asyncio
import aiohttp
import random
import json
import os
from datetime import datetime, timedelta
import hashlib


class AmazonTelegramDealsBot:
    def __init__(self, telegram_bot_token, telegram_channel_id):
        # Initialize Amazon PA API
        self.amazon = AmazonApi(
            "AKPAORP8DX1757347889",
            "0zo+YXhJRPqkKO/YCfszbQ9Eo1Sk8hcRryMf22sa",
            "akki22784-21",
            "IN",
            throttling=3
        )

        # Telegram settings
        self.bot_token = telegram_bot_token
        self.channel_id = telegram_channel_id
        self.telegram_api_url = f"https://api.telegram.org/bot{telegram_bot_token}"

        # File to track sent products (prevents duplicates)
        self.sent_products_file = "sent_products.json"
        self.sent_products = self.load_sent_products()

        # Setup session with rotating user agents
        self.session = requests.Session()
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/121.0'
        ]
        
        # High commission rate categories (preference order)
        self.high_commission_categories = [
            'Fashion', 'Clothing', 'Shoes', 'Jewelry', 'Watches', 'Bags',
            'Home & Kitchen', 'Sports & Fitness', 'Beauty & Personal Care',
            'Toys & Games', 'Books', 'Music', 'Movies & TV',
            'Health & Household', 'Garden & Outdoor', 'Automotive',
            'Electronics', 'Computers', 'Mobile Phones'
        ]

        # Multiple deals URLs for different categories
        self.deals_urls = [
            "https://www.amazon.in/deals?&linkCode=ll2&tag=akki22784-21",
            "https://www.amazon.in/gp/goldbox?&linkCode=ll2&tag=akki22784-21",
            "https://www.amazon.in/deals?discountRanges=10-,&sortBy=BY_SCORE&tag=akki22784-21",
            # Category specific URLs
            "https://www.amazon.in/s?k=deals&rh=n%3A1571271031&tag=akki22784-21",  # Fashion
            "https://www.amazon.in/s?k=deals&rh=n%3A1380263031&tag=akki22784-21",  # Home & Kitchen
            "https://www.amazon.in/s?k=deals&rh=n%3A1355016031&tag=akki22784-21",  # Sports
            "https://www.amazon.in/s?k=deals&rh=n%3A1374618031&tag=akki22784-21",  # Beauty
            "https://www.amazon.in/s?k=deals&rh=n%3A1350380031&tag=akki22784-21",  # Toys
            "https://www.amazon.in/s?k=deals&rh=n%3A976442031&tag=akki22784-21",   # Books
        ]

        self.consecutive_failures = 0
        self.base_delay = 3

    def load_sent_products(self):
        """Load previously sent products from file"""
        try:
            if os.path.exists(self.sent_products_file):
                with open(self.sent_products_file, 'r') as f:
                    data = json.load(f)
                    # Clean old entries (older than 7 days)
                    cutoff_date = datetime.now() - timedelta(days=7)
                    cleaned_data = {}
                    for asin, timestamp in data.items():
                        if datetime.fromisoformat(timestamp) > cutoff_date:
                            cleaned_data[asin] = timestamp
                    return cleaned_data
            return {}
        except Exception as e:
            print(f"Error loading sent products: {e}")
            return {}

    def save_sent_products(self):
        """Save sent products to file"""
        try:
            with open(self.sent_products_file, 'w') as f:
                json.dump(self.sent_products, f, indent=2)
        except Exception as e:
            print(f"Error saving sent products: {e}")

    def is_product_already_sent(self, asin):
        """Check if product was already sent"""
        return asin in self.sent_products

    def mark_product_as_sent(self, asin):
        """Mark product as sent"""
        self.sent_products[asin] = datetime.now().isoformat()
        self.save_sent_products()

    def get_random_user_agent(self):
        """Get random user agent to avoid detection"""
        return random.choice(self.user_agents)

    def extract_asins_from_multiple_pages(self, max_products=1000):
        """Extract ASINs from multiple deals pages and sources"""
        print("🔍 Scraping deals from multiple sources...")
        all_asins = set()
        
        for i, deals_url in enumerate(self.deals_urls):
            if len(all_asins) >= max_products:
                break
                
            print(f"📄 Scraping source {i+1}/{len(self.deals_urls)}: {deals_url[:60]}...")
            
            try:
                # Use different user agent for each request
                self.session.headers.update({
                    'User-Agent': self.get_random_user_agent(),
                    'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Connection': 'keep-alive',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache'
                })

                response = self.session.get(deals_url, timeout=10)
                response.raise_for_status()

                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Enhanced ASIN extraction patterns
                asin_patterns = [
                    r'/dp/([A-Z0-9]{10})',
                    r'/gp/product/([A-Z0-9]{10})',
                    r'data-asin="([A-Z0-9]{10})"',
                    r'asin["\']?\s*[:=]\s*["\']([A-Z0-9]{10})["\']',
                    r'/([A-Z0-9]{10})/',
                    r'amazon\.in.*?/([A-Z0-9]{10})',
                    r'data-csa-c-asin="([A-Z0-9]{10})"',
                    r'data-testid=".*?([A-Z0-9]{10})"'
                ]

                page_content = response.text
                page_asins = set()
                
                for pattern in asin_patterns:
                    matches = re.findall(pattern, page_content)
                    for match in matches:
                        if len(match) == 10 and match.isalnum() and not match.isdigit():
                            page_asins.add(match)

                print(f"   ✅ Found {len(page_asins)} ASINs from this source")
                all_asins.update(page_asins)
                
                # Add delay between requests to avoid being blocked
                time.sleep(random.uniform(2, 4))

            except Exception as e:
                print(f"   ❌ Error scraping {deals_url}: {e}")
                continue

        # Remove already sent products
        new_asins = [asin for asin in all_asins if not self.is_product_already_sent(asin)]
        
        print(f"📊 Total unique ASINs found: {len(all_asins)}")
        print(f"🆕 New ASINs (not sent before): {len(new_asins)}")
        
        return list(new_asins)[:max_products]

    def get_category_priority_score(self, product_title, browse_node=None):
        """Assign priority score based on category (higher = better commission)"""
        title_lower = product_title.lower()
        
        # Check high commission categories first
        for i, category in enumerate(self.high_commission_categories):
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
            
            keywords = category_keywords.get(category, [category.lower()])
            if any(keyword in title_lower for keyword in keywords):
                return 100 - i  # Higher score for higher priority categories
        
        return 0  # Default score for uncategorized items

    def get_product_details_single(self, asin):
        """Get detailed product information with category prioritization"""
        max_retries = 2
        
        for attempt in range(max_retries):
            try:
                delay = self.base_delay + random.uniform(1, 3)
                time.sleep(delay)
                
                items = self.amazon.get_items([asin])
                
                if not items:
                    return None

                item = items[0]
                product_details = {
                    'asin': asin,
                    'title': 'N/A',
                    'primary_image': 'N/A',
                    'current_price': 'N/A',
                    'mrp': 'N/A',
                    'discount': 'N/A',
                    'discount_percentage': 0,
                    'availability': 'N/A',
                    'category_score': 0,
                    'affiliate_url': f"https://www.amazon.in/dp/{asin}?tag=akki22784-21&linkCode=ogi&th=1&psc=1"
                }

                # Extract basic info
                if item.item_info and item.item_info.title:
                    product_details['title'] = item.item_info.title.display_value
                    product_details['category_score'] = self.get_category_priority_score(product_details['title'])

                if item.images and item.images.primary:
                    product_details['primary_image'] = item.images.primary.large.url

                # Extract price and discount info
                if item.offers and item.offers.listings:
                    listing = item.offers.listings[0]

                    if listing.price:
                        product_details['current_price'] = f"₹{listing.price.amount}"

                    if listing.saving_basis:
                        product_details['mrp'] = f"₹{listing.saving_basis.amount}"
                        if listing.price:
                            discount_amount = listing.saving_basis.amount - listing.price.amount
                            discount_percentage = (discount_amount / listing.saving_basis.amount) * 100
                            product_details['discount'] = f"₹{discount_amount:.0f} ({discount_percentage:.0f}% off)"
                            product_details['discount_percentage'] = discount_percentage

                    if listing.availability:
                        product_details['availability'] = listing.availability.message

                self.consecutive_failures = 0

                # Only return products with good data and reasonable discount
                if (product_details['title'] != 'N/A' and 
                    product_details['current_price'] != 'N/A' and
                    product_details['discount_percentage'] >= 10):  # At least 10% discount
                    return product_details

                return None

            except Exception as e:
                if "rate limit" in str(e).lower() or "throttle" in str(e).lower():
                    self.consecutive_failures += 1
                    if attempt < max_retries - 1:
                        retry_delay = self.base_delay * (2 ** (attempt + 1)) + random.uniform(5, 10)
                        time.sleep(retry_delay)
                        continue
                print(f"❌ Error for ASIN {asin}: {e}")
                break
        
        return None

    def format_product_message(self, product):
        """Format product details for Telegram message"""
        title = product['title']
        if len(title) > 120:
            title = title[:117] + "..."

        # Add category indicator
        category_emoji = "🏷️"
        if product['category_score'] > 90:
            category_emoji = "👕"  # Fashion
        elif product['category_score'] > 85:
            category_emoji = "🏠"  # Home
        elif product['category_score'] > 80:
            category_emoji = "💄"  # Beauty
        elif product['category_score'] > 75:
            category_emoji = "🎮"  # Toys/Games

        message = f"""{category_emoji} **MEGA DEAL ALERT!** 🔥

📦 **{title}**

💰 **Price:** {product['current_price']}
🏷️ **MRP:** {product['mrp']}
🎯 **You Save:** {product['discount']}
✅ **Status:** {product['availability']}

🛒({product['affiliate_url']})

#AmazonDeals #MegaSavings #ShopNow"""
        return message

    def send_telegram_message(self, message, image_url=None):
        """Send message to Telegram channel"""
        try:
            if image_url and image_url != 'N/A':
                url = f"{self.telegram_api_url}/sendPhoto"
                data = {
                    'chat_id': self.channel_id,
                    'photo': image_url,
                    'caption': message,
                    'parse_mode': 'Markdown'
                }
            else:
                url = f"{self.telegram_api_url}/sendMessage"
                data = {
                    'chat_id': self.channel_id,
                    'text': message,
                    'parse_mode': 'Markdown',
                    'disable_web_page_preview': False
                }

            response = requests.post(url, data=data)
            return response.status_code == 200 and response.json().get('ok', False)

        except Exception as e:
            print(f"❌ Telegram error: {e}")
            return False

    def process_all_deals_to_telegram(self, max_products=1000, delay_between_messages=4):
        """Main processing function with enhanced features"""
        print("=" * 80)
        print("🚀 ENHANCED AMAZON DEALS TO TELEGRAM BOT")
        print("=" * 80)

        # Step 1: Extract ASINs from multiple sources
        asins = self.extract_asins_from_multiple_pages(max_products)
        if not asins:
            print("❌ No new ASINs found!")
            return

        print(f"🎯 Processing {len(asins)} new products...")

        successful_sends = 0
        processed_count = 0
        products_with_scores = []

        # Step 2: Get product details and score them
        print("\n📋 Fetching product details...")
        for i, asin in enumerate(asins[:max_products], 1):
            if processed_count >= max_products:
                break
                
            print(f"⏳ Processing {i}/{min(len(asins), max_products)}: {asin}")
            
            product = self.get_product_details_single(asin)
            processed_count += 1
            
            if product:
                products_with_scores.append(product)
                print(f"   ✅ Added - Score: {product['category_score']}, Discount: {product['discount_percentage']:.1f}%")
            else:
                print(f"   ❌ Skipped - No valid data")
            
            # Small delay between API calls
            time.sleep(1)

        # Step 3: Sort by priority (category score + discount percentage)
        products_with_scores.sort(
            key=lambda x: (x['category_score'] * 2 + x['discount_percentage']), 
            reverse=True
        )

        print(f"\n📤 Sending {len(products_with_scores)} products to Telegram...")

        # Step 4: Send products to Telegram
        for i, product in enumerate(products_with_scores, 1):
            print(f"📱 Sending {i}/{len(products_with_scores)}: {product['asin']}")
            
            message = self.format_product_message(product)
            if self.send_telegram_message(message, product['primary_image']):
                successful_sends += 1
                self.mark_product_as_sent(product['asin'])
                print(f"   ✅ Sent successfully")
            else:
                print(f"   ❌ Failed to send")
            
            time.sleep(delay_between_messages)

        # Final summary
        print("\n" + "=" * 80)
        print("📊 FINAL SUMMARY")
        print("=" * 80)
        print(f"✅ Successfully Sent: {successful_sends}")
        print(f"❌ Failed: {processed_count - successful_sends}")
        print(f"📈 Total Processed: {processed_count}")
        print(f"💾 Products in Database: {len(self.sent_products)}")
        print("=" * 80)

    def test_telegram_connection(self):
        """Test Telegram connection"""
        try:
            url = f"{self.telegram_api_url}/getMe"
            response = requests.get(url)
            
            if response.status_code == 200:
                result = response.json()
                if result['ok']:
                    bot_info = result['result']
                    print(f"✅ Telegram connected! Bot: @{bot_info.get('username', 'Unknown')}")
                    return True
            
            print("❌ Telegram connection failed")
            return False
                
        except Exception as e:
            print(f"❌ Telegram error: {e}")
            return False


def main():
    TELEGRAM_BOT_TOKEN = "7564060655:AAHdYhMzCjHXwpkBOKTiKfdheLv1VJ0Dl2o"
    TELEGRAM_CHANNEL_ID = "-1002364974498"

    bot = AmazonTelegramDealsBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID)

    if not bot.test_telegram_connection():
        print("❌ Please check your Telegram credentials")
        return

    # Run with enhanced settings
    bot.process_all_deals_to_telegram(max_products=200, delay_between_messages=5)


if __name__ == "__main__":
    main()