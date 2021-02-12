import fileinput
import json
import os
import pickle
import platform
import random
import time
import typing
from contextlib import contextmanager

import psutil
import requests
from amazoncaptcha import AmazonCaptcha
from chromedriver_py import binary_path
from furl import furl
from lxml import html
from price_parser import parse_price, Price
from seleniumwire import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
    ElementNotInteractableException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC, wait
from selenium.webdriver.support.expected_conditions import staleness_of
from selenium.webdriver.support.ui import WebDriverWait

from common.amazon_support import (
    AmazonItemCondition,
    FGItem,
    SellerDetail,
    condition_check,
    price_check,
    solve_captcha,
    get_shipping_costs,
)
from notifications.notifications import NotificationHandler
from stores.basestore import BaseStoreHandler
from utils.logger import log
from utils.selenium_utils import enable_headless, options

# PDP_URL = "https://smile.amazon.com/gp/product/"
# AMAZON_DOMAIN = "www.amazon.com.au"
# AMAZON_DOMAIN = "www.amazon.com.br"
AMAZON_DOMAIN = "www.amazon.ca"
# NOT SUPPORTED AMAZON_DOMAIN = "www.amazon.cn"
# AMAZON_DOMAIN = "www.amazon.fr"
# AMAZON_DOMAIN = "www.amazon.de"
# NOT SUPPORTED AMAZON_DOMAIN = "www.amazon.in"
# AMAZON_DOMAIN = "www.amazon.it"
# AMAZON_DOMAIN = "www.amazon.co.jp"
# AMAZON_DOMAIN = "www.amazon.com.mx"
# AMAZON_DOMAIN = "www.amazon.nl"
# AMAZON_DOMAIN = "www.amazon.es"
# AMAZON_DOMAIN = "www.amazon.co.uk"
# AMAZON_DOMAIN = "www.amazon.com"
# AMAZON_DOMAIN = "www.amazon.se"

PDP_PATH = f"/dp/"
# REALTIME_INVENTORY_URL = f"{AMAZON_DOMAIN}gp/aod/ajax/ref=aod_f_new?asin="
# REALTIME_INVENTORY_PATH = f"/gp/aod/ajax/ref=aod_f_new?isonlyrenderofferlist=true&asin="
# REALTIME_INVENTORY_URL = "https://www.amazon.com/gp/aod/ajax/ref=dp_aod_NEW_mbc?asin="
REALTIME_INVENTORY_PATH = f"/gp/aod/ajax?asin="

CONFIG_FILE_PATH = "config/amazon_ajax_config.json"
STORE_NAME = "Amazon"
DEFAULT_MAX_TIMEOUT = 10
CART_PAGE_PATH = "/gp/cart/view.html"
CART_PAGE_SELECTOR = "//div[@id='sc-retail-cart-container']"
GOTO_CHECKOUT_BUTTON_SELECTOR = "//input[@name='proceedToRetailCheckout']"
# Step 1
SHIPPING_ADDRESS_SELECT_PATH = "/gp/buy/addressselect/handlers/display.html"
SHIPPING_ADDRESS_SELECT_SELECTOR = (
    "//div[@id='shipaddress']//input[@data-testid='Address_selectShipToThisAddress']"
)
USE_THIS_ADDRESS_BUTTON_SELECTOR = "//input[@aria-labelledby='orderSummaryPrimaryActionBtn-announce' and @type='submit']"
# Step 2
PAYMENT_METHOD_PATH = "/gp/buy/payselect/handlers/display.html"
PAYMENT_METHOD_SELECTOR = "//div[@id='payment']//div[@id='apx-content']"
USE_THIS_PAYMENT_METHOD_BUTTON_SELECTOR = ""

# Step 3
PAYMENT_PAGE_PATH = "/gp/buy/payselect/handlers/display.html"
PAYMENT_PAGE_SELECTOR = ""
PLACE_YOUR_ORDER_BUTTON_SELECTOR = "//input[@name='placeYourOrder1' and @type='submit']"


# Request
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, sdch, br",
    "Accept-Language": "en-US,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36",
}


def free_shipping_check(seller):
    if seller.shipping_cost.amount > 0:
        return False
    else:
        return True


class AmazonStoreHandler(BaseStoreHandler):
    http_client = False
    http_20_client = False
    http_session = True

    def __init__(
            self,
            notification_handler: NotificationHandler,
            headless=False,
            checkshipping=False,
            detailed=False,
            used=False,
            single_shot=False,
            no_screenshots=False,
            disable_presence=False,
            slow_mode=False,
            no_image=False,
            encryption_pass=None,
            log_stock_check=False,
            shipping_bypass=False,
    ) -> None:
        super().__init__()

        self.shuffle = True

        self.notification_handler = notification_handler
        self.check_shipping = checkshipping
        self.item_list: typing.List[FGItem] = []
        self.stock_checks = 0
        self.start_time = int(time.time())
        self.amazon_domain = "smile.amazon.com"
        self.webdriver_child_pids = []
        self.take_screenshots = not no_screenshots
        from cli.cli import global_config

        self.amazon_config = global_config.get_amazon_config(encryption_pass)

        # Load up our configuration
        self.parse_config()

        # Set up the Chrome options based on user flags
        if headless:
            enable_headless()

        prefs = get_prefs(no_image)
        set_options(prefs, slow_mode=slow_mode)
        modify_browser_profile()

        # Initialize the Session we'll use for this run
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # self.conn = http.client.HTTPSConnection(self.amazon_domain)
        # self.conn20 = HTTP20Connection(self.amazon_domain)

        # Spawn the web browser
        self.driver = create_driver(options)
        self.webdriver_child_pids = get_webdriver_pids(self.driver)

    def __del__(self):
        message = f"Shutting down {STORE_NAME} Store Handler."
        log.info(message)
        self.notification_handler.send_notification(message)
        self.delete_driver()

    def delete_driver(self):
        try:
            if platform.system() == "Windows" and self.driver:
                log.info("Cleaning up after web driver...")
                # brute force kill child Chrome pids with fire
                for pid in self.webdriver_child_pids:
                    try:
                        log.debug(f"Killing {pid}...")
                        process = psutil.Process(pid)
                        process.kill()
                    except psutil.NoSuchProcess:
                        log.debug(f"{pid} not found. Continuing...")
                        pass
            elif self.driver:
                self.driver.quit()

        except Exception as e:
            log.info(e)
            log.info(
                "Failed to clean up after web driver.  Please manually close browser."
            )
            return False
        return True

    def is_logged_in(self):
        try:
            text = self.driver.find_element_by_id("nav-link-accountList").text
            return not any(
                sign_in in text for sign_in in self.amazon_config["SIGN_IN_TEXT"]
            )
        except NoSuchElementException:

            return False

    def login(self):
        log.info(f"Logging in to {self.amazon_domain}...")

        email_field: WebElement
        remember_me: WebElement
        password_field: WebElement

        # Look for a sign in link
        try:
            skip_link: WebElement = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//a[contains(@href, '/ap/signin/')]")
                )
            )
            skip_link.click()
        except TimeoutException as e:
            log.error(
                "Timed out waiting for signin link.  Unable to find matching "
                "xpath for '//a[@data-nav-role='signin']'"
            )
            log.exception(e)
            exit(1)

        log.info("Inputting email...")
        try:

            email_field: WebElement = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="ap_email"]'))
            )
            with self.wait_for_page_change():
                email_field.clear()
                email_field.send_keys(self.amazon_config["username"] + Keys.RETURN)
            if self.driver.find_elements_by_xpath('//*[@id="auth-error-message-box"]'):
                log.error("Login failed, delete your credentials file")
                time.sleep(240)
                exit(1)
        except wait.TimeoutException as e:
            log.error("Timed out waiting for email login box.")
            log.exception(e)
            exit(1)

        log.info("Checking 'rememberMe' checkbox...")
        try:
            remember_me = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@name="rememberMe"]'))
            )
            remember_me.click()
        except NoSuchElementException:
            log.error("Remember me checkbox did not exist")

        log.info("Inputting Password")
        captcha_entry: WebElement = None
        try:
            password_field = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="ap_password"]'))
            )
            password_field.clear()
            password_field.send_keys(self.amazon_config["password"])
            # check for captcha
            try:
                captcha_entry = self.driver.find_element_by_xpath(
                    '//*[@id="auth-captcha-guess"]'
                )
            except NoSuchElementException:
                with self.wait_for_page_change(timeout=10):
                    password_field.send_keys(Keys.RETURN)

        except NoSuchElementException:
            log.error("Unable to find password input box.  Unable to log in.")
        except wait.TimeoutException:
            log.error("Timeout expired waiting for password input box.")

        if captcha_entry:
            try:
                log.info("Stuck on a captcha... Lets try to solve it.")
                captcha = AmazonCaptcha.fromdriver(self.driver)
                solution = captcha.solve()
                log.info(f"The solution is: {solution}")
                if solution == "Not solved":
                    log.info(
                        f"Failed to solve {captcha.image_link}, lets reload and get a new captcha."
                    )
                    self.send_notification(
                        "Unsolved Captcha", "unsolved_captcha", self.take_screenshots
                    )
                    self.driver.refresh()
                else:
                    self.send_notification(
                        "Solving catpcha", "captcha", self.take_screenshots
                    )
                    with self.wait_for_page_change(timeout=10):
                        captcha_entry.clear()
                        captcha_entry.send_keys(solution + Keys.RETURN)

            except Exception as e:
                log.debug(e)
                log.info("Error trying to solve captcha. Refresh and retry.")
                self.driver.refresh()
                time.sleep(5)

        # Deal with 2FA
        if self.driver.title in self.amazon_config["TWOFA_TITLES"]:
            log.info("enter in your two-step verification code in browser")
            while self.driver.title in self.amazon_config["TWOFA_TITLES"]:
                time.sleep(0.2)

        # Deal with Account Fix Up prompt
        if "accountfixup" in self.driver.current_url:
            # Click the skip link
            try:
                skip_link: WebElement = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//a[contains(@id, 'skip-link')]")
                    )
                )
                skip_link.click()
            except TimeoutException as e:
                log.error(
                    "Timed out waiting for the skip link.  Unable to find matching "
                    "xpath for '//a[contains(@id, 'skip-link')]'"
                )
                log.exception(e)

        log.info(f'Logged in as {self.amazon_config["username"]}')

    def run(self, delay=3):
        # Load up the homepage
        with self.wait_for_page_change():
            self.driver.get(f"https://{self.amazon_domain}")

        # Get a valid amazon session for our requests
        if not self.is_logged_in():
            self.login()

        # Verify the configuration file
        if not self.verify():
            # try one more time
            log.info("Failed to verify... trying more more time")
            self.verify()

        # To keep the user busy https://github.com/jakesgordon/javascript-tetris
        # ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
        # uri = pathlib.Path(f"{ROOT_DIR}/../tetris/index.html").as_uri()
        # log.debug(f"Tetris URL: {uri}")
        # self.driver.get(uri)

        message = f"Starting to hunt items at {STORE_NAME}"
        log.info(message)
        self.notification_handler.send_notification(message)

        while self.item_list:

            for item in self.item_list:
                qualified_seller = self.find_qualified_seller(item)
                if qualified_seller:
                    successful = self.attempt_purchase(item, qualified_seller)
                    if successful:
                        self.item_list.remove(item)
                time.sleep(delay + random.randint(0, 3))
            if self.shuffle:
                random.shuffle(self.item_list)

    @contextmanager
    def wait_for_page_change(self, timeout=30):
        """Utility to help manage selenium waiting for a page to load after an action, like a click"""
        old_page = self.driver.find_element_by_tag_name("html")
        yield
        WebDriverWait(self.driver, timeout).until(EC.staleness_of(old_page))
        WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located((By.XPATH, '//title')))

    def find_qualified_seller(self, item) -> SellerDetail or None:
        item_sellers = self.get_item_sellers(item, self.amazon_config["FREE_SHIPPING"])
        for seller in item_sellers:
            if not self.check_shipping and not free_shipping_check(seller):
                log.debug("Failed shipping hurdle.")
                return
            log.debug("Passed shipping hurdle.")
            if not condition_check(item, seller):
                log.debug("Failed item condition hurdle.")
                return
            log.debug("Passed item condition hurdle.")
            if not price_check(item, seller):
                log.debug("Failed price condition hurdle.")
                return
            log.debug("Pass price condition hurdle.")

            return seller

    def parse_config(self):
        log.info(f"Processing config file from {CONFIG_FILE_PATH}")
        # Parse the configuration file to get our hunt list
        try:
            with open(CONFIG_FILE_PATH) as json_file:
                config = json.load(json_file)
                self.amazon_domain = config.get("amazon_domain", "smile.amazon.com")

                json_items = config.get("items")
                self.parse_items(json_items)

        except FileNotFoundError:
            log.error(
                f"Configuration file not found at {CONFIG_FILE_PATH}.  Please see {CONFIG_FILE_PATH}_template."
            )
            exit(1)
        log.info(f"Found {len(self.item_list)} items to track at {STORE_NAME}.")

    def parse_items(self, json_items):
        for json_item in json_items:
            if (
                    "max-price" in json_item
                    and "asins" in json_item
                    and "min-price" in json_item
            ):
                max_price = json_item["max-price"]
                min_price = json_item["min-price"]
                if type(max_price) is str:
                    max_price = parse_price(max_price)
                else:
                    max_price = Price(max_price, currency=None, amount_text=None)
                if type(min_price) is str:
                    min_price = parse_price(min_price)
                else:
                    min_price = Price(min_price, currency=None, amount_text=None)

                if "condition" in json_item:
                    condition = parse_condition(json_item["condition"])
                else:
                    condition = AmazonItemCondition.New

                # Create new instances of an item for each asin specified
                asins_collection = json_item["asins"]
                if isinstance(asins_collection, str):
                    log.warning(
                        f"\"asins\" node needs be an list/array and included in braces (e.g., [])  Attempting to recover {json_item['asins']}"
                    )
                    # did the user forget to put us in an array?
                    asins_collection = asins_collection.split(",")
                for asin in asins_collection:
                    self.item_list.append(
                        FGItem(
                            asin,
                            min_price,
                            max_price,
                            condition=condition,
                        )
                    )
            else:
                log.error(
                    f"Item isn't fully qualified.  Please include asin, min-price and max-price. {json_item}"
                )

    def verify(self):
        log.info("Verifying item list...")
        items_to_purge = []
        verified = 0
        item_cache_file = os.path.join(
            os.path.dirname(os.path.abspath("__file__")),
            "stores",
            "store_data",
            "item_cache.p",
        )

        if os.path.exists(item_cache_file) and os.path.getsize(item_cache_file) > 0:
            item_cache = pickle.load(open(item_cache_file, "rb"))
        else:
            item_cache = {}

        for idx, item in enumerate(self.item_list):
            # Check the cache first to save the scraping...
            if item.id in item_cache.keys():
                cached_item = item_cache[item.id]
                log.debug(f"Verifying ASIN {cached_item.id} via cache  ...")
                # Update attributes that may have been changed in the config file
                cached_item.condition = item.condition
                cached_item.min_price = item.min_price
                cached_item.max_price = item.max_price
                self.item_list[idx] = cached_item
                log.info(
                    f"Verified ASIN {cached_item.id} as '{cached_item.short_name}'"
                )
                verified += 1
                continue

            # Verify that the ASIN hits and that we have a valid inventory URL
            pdp_url = f"https://{self.amazon_domain}{PDP_PATH}{item.id}"
            log.debug(f"Verifying at {pdp_url} ...")

            data, status = self.get_html(pdp_url)
            if status == 503:
                # Check for CAPTCHA
                tree = html.fromstring(data)
                captcha_form_element = tree.xpath(
                    "//form[contains(@action,'validateCaptcha')]"
                )
                if captcha_form_element:
                    # Solving captcha and resetting data
                    data, status = solve_captcha(
                        self.session, captcha_form_element[0], pdp_url
                    )

            if status == 200:
                item.furl = furl(
                    f"https://{self.amazon_domain}/{REALTIME_INVENTORY_PATH}{item.id}"
                )
                tree = html.fromstring(data)
                captcha_form_element = tree.xpath(
                    "//form[contains(@action,'validateCaptcha')]"
                )
                if captcha_form_element:
                    data, status = solve_captcha(self.session, captcha_form_element[0], pdp_url)
                    tree = html.fromstring(data)
                title = tree.xpath('//*[@id="productTitle"]')
                if len(title) > 0:
                    item.name = title[0].text.strip()
                    item.short_name = (
                        item.name[:40].strip() + "..."
                        if len(item.name) > 40
                        else item.name
                    )
                    log.info(f"Verified ASIN {item.id} as '{item.short_name}'")
                    item_cache[item.id] = item
                    verified += 1
                else:
                    # TODO: Evaluate if this happens with a 200 code
                    doggo = tree.xpath("//img[@alt='Dogs of Amazon']")
                    if doggo:
                        # Bad ASIN or URL... dump it
                        log.error(
                            f"Bad ASIN {item.id} for the domain or related failure.  Removing from hunt."
                        )
                        items_to_purge.append(item)
                    else:
                        log.info(
                            f"Unable to verify ASIN {item.id}.  Continuing without verification."
                        )
            else:
                log.error(
                    f"Unable to locate details for {item.id} at {pdp_url}.  Removing from hunt."
                )
                items_to_purge.append(item)

        # Purge any items we didn't find while verifying
        for item in items_to_purge:
            self.item_list.remove(item)

        log.info(
            f"Verified {verified} out of {len(self.item_list)} items on {STORE_NAME}"
        )
        pickle.dump(item_cache, open(item_cache_file, "wb"))

        return True

    def get_item_sellers(self, item, free_shipping_strings):
        """Parse out information to from the aod-offer nodes populate ItemDetail instances for each item """
        payload = self.get_real_time_data(item)
        sellers = []
        # This is where the parsing magic goes
        log.debug(f"payload is {len(payload)} bytes")
        if len(payload) == 0:
            log.error("Empty Response.  Skipping...")
            return sellers

        tree = html.fromstring(payload)

        # Get the pinned offer, if it exists, by checking for a pinned offer area and add to cart button
        pinned_offer = tree.xpath("//div[@id='aod-sticky-pinned-offer']")
        if not pinned_offer or not tree.xpath(
                "//div[@id='aod-sticky-pinned-offer']//input[@name='submit.addToCart']"
        ):
            log.debug(f"No pinned offer for {item.id} = {item.short_name}")
        else:
            for idx, offer in enumerate(pinned_offer):
                merchant_name = offer.xpath(
                    ".//span[@class='a-size-small a-color-base']"
                )[0].text.strip()
                price_text = offer.xpath(".//span[@class='a-price-whole']")[0].text
                price = parse_price(price_text)
                shipping_cost = get_shipping_costs(offer, free_shipping_strings)
                form_action = offer.xpath(".//form[contains(@action,'add-to-cart')]")[
                    0
                ].action
                condition_heading = offer.xpath(".//div[@id='aod-offer-heading']/h5")
                if condition_heading:
                    condition = AmazonItemCondition.from_str(
                        condition_heading[0].text.strip()
                    )
                else:
                    condition = AmazonItemCondition.Unknown
                offers = offer.xpath(f".//input[@name='offeringID.1']")
                offer_id = None
                if len(offers) > 0:
                    offer_id = offers[0].value
                else:
                    log.error("No offer ID found!")

                seller = SellerDetail(
                    merchant_name,
                    price,
                    shipping_cost,
                    condition,
                    form_action,
                    offer_id,
                )
                sellers.append(seller)

        offers = tree.xpath("//div[@id='aod-offer']")
        if not offers:
            log.debug(f"No offers found for {item.id} = {item.short_name}")
            return sellers
        for idx, offer in enumerate(offers):
            # This is preferred, but Amazon itself has unique URL parameters that I haven't identified yet
            # merchant_name = offer.xpath(
            #     ".//a[@target='_blank' and contains(@href, 'merch_name')]"
            # )[0].text.strip()
            merchant_name = offer.xpath(".//a[@target='_blank']")[0].text.strip()
            price_text = offer.xpath(
                ".//div[contains(@id, 'aod-price-')]//span[contains(@class,'a-offscreen')]"
            )[0].text
            price = parse_price(price_text)

            shipping_cost = get_shipping_costs(offer, free_shipping_strings)
            form_action = offer.xpath(".//form[contains(@action,'add-to-cart')]")[
                0
            ].action
            condition_heading = offer.xpath(".//div[@id='aod-offer-heading']/h5")
            if condition_heading:
                condition = AmazonItemCondition.from_str(
                    condition_heading[0].text.strip()
                )
            else:
                condition = AmazonItemCondition.Unknown
            offers = offer.xpath(f".//input[@name='offeringID.1']")
            offer_id = None
            if len(offers) > 0:
                offer_id = offers[0].value
            else:
                log.error("No offer ID found!")

            seller = SellerDetail(
                merchant_name,
                price,
                shipping_cost,
                condition,
                form_action,
                offer_id,
            )
            sellers.append(seller)
        return sellers

    def get_real_time_data(self, item):
        log.debug(f"Calling {STORE_NAME} for {item.short_name} using {item.furl.url}")
        data, status = self.get_html(item.furl.url)
        if item.status_code != status:
            # Track when we flip-flop between status codes.  200 -> 204 may be intelligent caching at Amazon.
            # We need to know if it ever goes back to a 200
            log.warning(
                f"{item.short_name} started responding with Status Code {status} instead of {item.status_code}"
            )
            item.status_code = status
        return data

    def attempt_purchase(self, item, qualified_seller):
        # Open the add.html URL in Selenium
        f = f"https://smile.amazon.com/gp/aws/cart/add.html?OfferListingId.1={qualified_seller.offering_id}&Quantity.1=1"

        with self.wait_for_page_change(timeout=5):
            self.driver.get(f)
            # log.info("Try to ATC and Buy from HERE!")
            # Click the continue button on the add.html page
            # TODO: implement logic to confirm there is quantity available

            xpath = "//input[@alt='Continue']"
            if wait_for_element_by_xpath(self.driver, xpath):
                try:
                    with self.wait_for_page_change():
                        self.driver.find_element_by_xpath(xpath).click()
                except NoSuchElementException:
                    log.error("Continue button not present on page")
            else:
                log.error("Continue button not present on page")

        # verify cart is non-zero
        if self.get_cart_count() != 0:

            CHECKOUT_TITLES = [
                "Amazon.com Checkout",
                "Amazon.co.uk Checkout",
                "Place Your Order - Amazon.ca Checkout",
                "Place Your Order - Amazon.co.uk Checkout",
                "Amazon.de Checkout",
                "Place Your Order - Amazon.de Checkout",
                "Amazon.de - Bezahlvorgang",
                "Bestellung aufgeben - Amazon.de-Bezahlvorgang",
                "Place Your Order - Amazon.com Checkout",
                "Place Your Order - Amazon.com",
                "Tramitar pedido en Amazon.es",
                "Processus de paiement Amazon.com",
                "Confirmar pedido - Compra Amazon.es",
                "Passez votre commande - Processus de paiement Amazon.fr",
                "Ordina - Cassa Amazon.it",
                "AmazonSmile Checkout",
                "Plaats je bestelling - Amazon.nl-kassa",
                "Place Your Order - AmazonSmile Checkout",
                "Preparing your order",
                "Ihre Bestellung wird vorbereitet",
                "Pagamento Amazon.it",
            ]

            while self.driver.title not in CHECKOUT_TITLES:
                # try to go directly to cart page:
                with self.wait_for_page_change(timeout=10):
                    self.driver.get(
                        f"https://{self.amazon_domain}/gp/buy/spc/handlers/display.html?hasWorkingJavascript=1"
                    )

        successful = self.handle_checkout()
        time.sleep(300)
        if successful:
            return True
        else:
            return False

    def navigate_purchase(self):
        pass

    def handle_checkout(self, test=True):
        previous_title = self.driver.title
        button = None
        timeout = get_timeout()

        button_xpaths = [
            '//*[@id="submitOrderButtonId"]/span/input',
            '//*[@id="bottomSubmitOrderButtonId"]/span/input',
            '//*[@id="placeYourOrder"]/span/input',
        ]

        while True:
            try:
                button = self.driver.find_element_by_xpath(button_xpaths[0])
            except NoSuchElementException:
                pass
            button_xpaths.append(button_xpaths.pop(0))
            if button:
                if button.is_enabled() and button.is_displayed():
                    break
            if time.time() > timeout:
                log.error("couldn't find buttons to proceed to checkout")
                # self.save_page_source("ptc-error")
                self.send_notification(
                    "Error in checkout.  Please check browser window.",
                    "ptc-error",
                    self.take_screenshots,
                )
                log.info("Refreshing page to try again")
                self.driver.refresh()
                time.sleep(3)
                # self.order_retry += 1
                return
        if test:
            log.info(f"Found button {button.text}, but this is a test")
            log.info("will not try to complete order")
            # log.info(f"test time took {time.time() - self.start_time_atc} to check out")
            # self.try_to_checkout = False
            # self.great_success = True
            # if self.single_shot:
            #     self.asin_list = []
        else:
            log.info(f"Clicking Button {button.text} to place order")
            with self.wait_for_page_change(timeout=10):
                button.click()

        ORDER_COMPLETE_TITLES = [
            "Amazon.com Thanks You",
            "Amazon.ca Thanks You",
            "AmazonSmile Thanks You",
            "Thank you",
            "Amazon.fr Merci",
            "Merci",
            "Amazon.es te da las gracias",
            "Amazon.fr vous remercie.",
            "Grazie da Amazon.it",
            "Hartelijk dank",
            "Thank You",
            "Amazon.de Vielen Dank",
        ]

        if self.driver.title in ORDER_COMPLETE_TITLES:
            return True
        else:
            return False

    def get_html(self, url):
        """Unified mechanism to get content to make changing connection clients easier"""
        f = furl(url)
        if not f.scheme:
            f.set(scheme="https")
        # response = self.session.get(f.url, headers=HEADERS)
        # return response.text, response.status_code

        # if self.http_client:
        #     # http.client method
        #     self.conn.request("GET", str(f.path), "", HEADERS)
        #     response = self.conn.getresponse()
        #     data = response.read()
        #     return data.decode("utf-8"), response.status
        # elif self.http_20_client:
        #     # hyper HTTP20Connection method
        #     self.conn20.request("GET", str(f.path), "", HEADERS)
        #     response = self.conn20.get_response()
        #     data = response.read()
        #     return data.decode("utf-8"), response.status
        # else:
        #     response = self.session.get(f.url, headers=HEADERS)
        #     return response.text, response.status_code
        # # else:
        # #
        # Selenium
        self.driver.get(f.url)
        response_code = 200  # Just assume it's fine... ;-)

        # # Access requests via the `requests` attribute
        for request in self.driver.requests:
            if request.url == url:
                response_code = request.response.status_code
                break
        data = self.driver.page_source

        return data, response_code

    # returns negative number if cart element does not exist, returns number if cart exists
    def get_cart_count(self):
        # check if cart number is on the page, if cart items = 0
        try:
            element = self.driver.find_element_by_xpath('//*[@id="nav-cart-count"]')
        except NoSuchElementException:
            return -1
        if element:
            try:
                return int(element.text)
            except Exception as e:
                log.debug("Error converting cart number to integer")
                log.debug(e)
                return -1


def parse_condition(condition: str) -> AmazonItemCondition:
    return AmazonItemCondition[condition]


def min_total_price(seller: SellerDetail):
    return seller.selling_price


def new_first(seller: SellerDetail):
    return seller.condition


def create_driver(options):
    try:
        return webdriver.Chrome(executable_path=binary_path, options=options)
    except Exception as e:
        log.error(e)
        log.error(
            "If you have a JSON warning above, try deleting your .profile-amz folder"
        )
        log.error(
            "If that's not it, you probably have a previous Chrome window open. You should close it."
        )
        exit(1)


def modify_browser_profile():
    # Delete crashed, so restore pop-up doesn't happen
    path_to_prefs = os.path.join(
        os.path.dirname(os.path.abspath("__file__")),
        ".profile-amz",
        "Default",
        "Preferences",
    )
    try:
        with fileinput.FileInput(path_to_prefs, inplace=True) as file:
            for line in file:
                print(line.replace("Crashed", "none"), end="")
    except FileNotFoundError:
        pass


def set_options(prefs, slow_mode):
    options.add_experimental_option("prefs", prefs)
    options.add_argument(f"user-data-dir=.profile-amz")
    if slow_mode:
        options.set_capability("pageLoadStrategy", "normal")
    else:
        options.set_capability("pageLoadStrategy", "eager")


def get_prefs(no_image):
    # For a list of Profile preferences,
    # see https://github.com/chromium/chromium/blob/master/chrome/common/pref_names.cc
    # and https://github.com/chromium/chromium/blob/master/chrome/browser/profiles/profile_impl.cc

    prefs = {
        "profile.password_manager_enabled": False,
        "credentials_enable_service": False,
    }
    if no_image:
        prefs["profile.managed_default_content_settings.images"] = 2
    else:
        prefs["profile.managed_default_content_settings.images"] = 0
    return prefs


def create_webdriver_wait(driver, wait_time=10):
    return WebDriverWait(driver, wait_time)


def get_webdriver_pids(driver):
    pid = driver.service.process.pid
    driver_process = psutil.Process(pid)
    children = driver_process.children(recursive=True)
    webdriver_child_pids = []
    for child in children:
        webdriver_child_pids.append(child.pid)
    return webdriver_child_pids


def get_timeout(timeout=DEFAULT_MAX_TIMEOUT):
    return time.time() + timeout


def wait_for_element_by_xpath(d, xpath, timeout=10):
    try:
        WebDriverWait(d, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
    except TimeoutException:
        log.error(f"failed to find {xpath}")
        return False

    return True
