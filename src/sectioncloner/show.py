"""Open the rebuilt page in a visible phone-size browser window and scroll through the section.

    uv run python -m sectioncloner.show [--x 1010 --y 25] [--hold 6]
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

from .agent import PRODUCT_HANDLE
from .browser import HEIGHT, REBUILD_SELECTOR, UA, WIDTH

PHONE = {"viewport": {"width": WIDTH, "height": HEIGHT}, "user_agent": UA, "is_mobile": True, "has_touch": True}


def product_url() -> str:
    return f"https://{os.environ['SHOPIFY_STORE_DOMAIN']}/products/{PRODUCT_HANDLE}"


def tour(page: Page, url: str, password: str, hold: float = 6) -> None:
    """Log in to the storefront if needed, then a slow, readable scroll through the rebuilt sections."""
    page.goto(url, wait_until="domcontentloaded")
    if "/password" in page.url:
        page.fill('input[type="password"]', password)
        page.press('input[type="password"]', "Enter")
        page.wait_for_load_state("domcontentloaded")
        page.goto(url, wait_until="domcontentloaded")
    el = page.wait_for_selector(REBUILD_SELECTOR, timeout=30000)
    page.wait_for_timeout(int(hold * 1000))
    end = page.evaluate("e => e.getBoundingClientRect().bottom + scrollY", el)
    y = 0
    while y < end - HEIGHT:
        y += 6
        page.evaluate("v => scrollTo(0, v)", y)
        page.wait_for_timeout(16)
    page.wait_for_timeout(3000)
    page.evaluate("scrollTo({top: 0, behavior: 'smooth'})")
    page.wait_for_timeout(3000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--x", type=int, default=1010)
    ap.add_argument("--y", type=int, default=25)
    ap.add_argument("--hold", type=float, default=6, help="seconds to stay on the top before scrolling")
    args = ap.parse_args()
    load_dotenv()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=[f"--window-position={args.x},{args.y}",
                                                          f"--window-size={WIDTH + 40},{HEIGHT + 90}"])
        tour(browser.new_context(**PHONE).new_page(), product_url(), os.getenv("STOREFRONT_PASSWORD", ""), args.hold)
        browser.close()


if __name__ == "__main__":
    main()
