import os
import re
import sys
import time
import random
from urllib.parse import quote

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

FULL_OUTPUT_CSV = "airbnb_scraped.csv"
TEST_OUTPUT_CSV = "airbnb_test.csv"
PROPERTIES_PER_CITY = 50
TARGET_BEDROOMS = 2
STRICT_BEDROOMS = False
HEADLESS = False
MAX_SEARCH_PAGES = 10

CHECK_IN = "2027-01-10"
CHECK_OUT = "2027-01-15"
NIGHTS = 5

CITIES = [
    ("Amsterdam", "Netherlands"),
    ("Athens", "Greece"),
    ("Barcelona", "Spain"),
    ("Berlin", "Germany"),
    ("Budapest", "Hungary"),
    ("Lisbon", "Portugal"),
    ("London", "United Kingdom"),
    ("Paris", "France"),
    ("Rome", "Italy"),
    ("Vienna", "Austria"),
]


def pause(a=1.5, b=3.5):
    time.sleep(random.uniform(a, b))


def make_driver():
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    return webdriver.Edge(options=opts)


def build_search_url(city, country):
    place = city.replace(" ", "-") + "--" + country.replace(" ", "-")
    url = "https://www.airbnb.com/s/" + quote(place) + "/homes"
    url += "?room_types%5B%5D=Entire%20home%2Fapt"
    url += "&min_bedrooms=" + str(TARGET_BEDROOMS)
    url += "&currency=USD"
    url += "&checkin=" + CHECK_IN
    url += "&checkout=" + CHECK_OUT
    url += "&adults=2"
    return url


def listing_url(link):
    return link + "?check_in=" + CHECK_IN + "&check_out=" + CHECK_OUT + "&adults=2&currency=USD"


def dismiss_popups(driver):
    selectors = ['button[aria-label="Close"]', 'button[data-testid="accept-btn"]']
    for sel in selectors:
        try:
            driver.find_element(By.CSS_SELECTOR, sel).click()
            pause(0.5, 1)
        except Exception:
            pass


def bedrooms_ok(b):
    if b is None:
        return True
    if STRICT_BEDROOMS:
        return b == TARGET_BEDROOMS
    return b >= TARGET_BEDROOMS


PRICE_TOTAL_RE = re.compile(r"\$\s?([\d,]+)\s+for\s+(\d+)\s+nights?", re.I)
PRICE_NIGHT_RE = re.compile(r"\$\s?([\d,]+)\s*(?:USD)?\s*(?:/|per)?\s*night\b", re.I)
PRICE_ANY_RE = re.compile(r"\$\s?([\d,]+)")


def to_num(s):
    return float(s.replace(",", ""))


def parse_price(text, loose):
    m = PRICE_TOTAL_RE.search(text)
    if m:
        nights = int(m.group(2))
        if nights > 0:
            return round(to_num(m.group(1)) / nights, 2)
    m = PRICE_NIGHT_RE.search(text)
    if m:
        return to_num(m.group(1))
    if loose:
        m = PRICE_ANY_RE.search(text)
        if m:
            value = to_num(m.group(1))
            if "total" in text.lower():
                return round(value / NIGHTS, 2)
            return value
    return None


def element_text(el):
    t = el.text
    if not t or not t.strip():
        t = el.get_attribute("textContent") or ""
    return t


def collect_links(driver, city, country, needed):
    driver.get(build_search_url(city, country))
    pause(3, 5)
    dismiss_popups(driver)

    collected = {}
    target = int(needed * 1.6)

    for page in range(MAX_SEARCH_PAGES):
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/rooms/"]'))
            )
        except TimeoutException:
            print("   [!] no results on page", page + 1)
            break

        for _ in range(6):
            driver.execute_script("window.scrollBy(0, 900);")
            time.sleep(0.6)

        cards = driver.find_elements(By.CSS_SELECTOR, 'div[itemprop="itemListElement"]')
        if cards:
            for card in cards:
                try:
                    a = card.find_element(By.CSS_SELECTOR, 'a[href*="/rooms/"]')
                    link = a.get_attribute("href").split("?")[0]
                    price = parse_price(element_text(card), True)
                    old = collected.get(link)
                    if link not in collected or (old is None and price is not None):
                        collected[link] = price
                except NoSuchElementException:
                    continue
        else:
            for a in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/rooms/"]'):
                link = a.get_attribute("href").split("?")[0]
                collected.setdefault(link, None)

        print("   page", page + 1, "- links so far:", len(collected))
        if len(collected) >= target:
            break

        try:
            nxt = driver.find_element(By.CSS_SELECTOR, 'a[aria-label="Next"]')
            driver.execute_script("arguments[0].click();", nxt)
            pause(3, 5)
        except NoSuchElementException:
            break

    return collected


RATING_REVIEWS_RES = [
    re.compile(r"(\d\.\d{1,2})\s*[·•]\s*([\d,]+)\s*reviews?", re.I),
    re.compile(r"(\d\.\d{1,2})\s*\(\s*([\d,]+)\s*reviews?\s*\)", re.I),
    re.compile(r"Rated\s*(\d\.\d{1,2})\s*out of 5.*?([\d,]+)\s*reviews?", re.I | re.S),
]
BEDROOMS_RE = re.compile(r"(\d+)\s+bedrooms?", re.I)
LOCATION_RE = re.compile(r"(?:Entire|Private|Shared|Room)[^\n]*? in ([^\n]+)")


def scrape_listing(driver, link, card_price):
    driver.get(listing_url(link))
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "h1"))
        )
    except TimeoutException:
        print("   [!] page did not load:", link)
        return None
    pause(2, 3)
    dismiss_popups(driver)

    body = driver.find_element(By.TAG_NAME, "body").text

    try:
        title = driver.find_element(By.TAG_NAME, "h1").text.strip()
    except NoSuchElementException:
        title = None

    m = LOCATION_RE.search(body)
    location = m.group(1).strip() if m else None

    m = BEDROOMS_RE.search(body)
    bedrooms = int(m.group(1)) if m else None

    price = card_price
    if price is None:
        price = parse_price(body, False)

    rating = None
    reviews = None
    for rx in RATING_REVIEWS_RES:
        m = rx.search(body)
        if m:
            rating = float(m.group(1))
            reviews = int(m.group(2).replace(",", ""))
            break

    return {
        "name": title,
        "location": location,
        "price_per_night_usd": price,
        "rating": rating,
        "reviews_count": reviews,
        "is_superhost": "Superhost" in body,
        "guest_favorite": "Guest favorite" in body,
        "bedrooms": bedrooms,
        "url": link,
    }


def country_ok(location, country):
    if not location:
        return True
    return country.lower() in location.lower()


def parse_args():
    count = PROPERTIES_PER_CITY
    n_cities = len(CITIES)
    test = False
    if len(sys.argv) > 1:
        count = int(sys.argv[1])
        test = True
    if len(sys.argv) > 2:
        n_cities = int(sys.argv[2])
        test = True
    return count, n_cities, test


def load_previous(out_csv, count):
    rows = []
    done = set()
    if os.path.exists(out_csv):
        try:
            old = pd.read_csv(out_csv)
            counts = old["city"].value_counts()
            done = set(c for c, n in counts.items() if n >= count)
            rows = [r for r in old.to_dict("records") if r["city"] in done]
        except Exception:
            rows = []
            done = set()
    return rows, done


def main():
    count, n_cities, test = parse_args()
    out_csv = TEST_OUTPUT_CSV if test else FULL_OUTPUT_CSV
    cities = CITIES[:n_cities]

    all_rows = []
    done = set()
    if not test:
        all_rows, done = load_previous(out_csv, count)
        if done:
            print("Already done, skipping:", ", ".join(sorted(done)))

    print("Settings:", count, "per city |", len(cities), "cities | file:", out_csv)

    driver = make_driver()

    try:
        for city, country in cities:
            if city in done:
                continue
            print("\n=== " + city + " (" + country + ") ===")
            links = collect_links(driver, city, country, count)
            city_rows = []

            for link, card_price in links.items():
                if len(city_rows) >= count:
                    break
                try:
                    row = scrape_listing(driver, link, card_price)
                except Exception as e:
                    print("   [!] error:", e)
                    continue
                if not row:
                    continue
                if not bedrooms_ok(row["bedrooms"]):
                    continue
                if not country_ok(row["location"], country):
                    print("   [skip] wrong country:", row["location"])
                    continue
                row["city"] = city
                row["country"] = country
                city_rows.append(row)
                print("   (" + str(len(city_rows)) + "/" + str(count) + ") "
                      + str(row["location"]) + " | $" + str(row["price_per_night_usd"]))
                pause(1.5, 3.5)

            all_rows.extend(city_rows)
            pd.DataFrame(all_rows).to_csv(out_csv, index=False, encoding="utf-8-sig")
            print("   saved so far:", len(all_rows), "listings")

    finally:
        driver.quit()

    df = pd.DataFrame(all_rows)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print("\nDone. Saved", len(df), "listings to", out_csv)


if __name__ == "__main__":
    main()