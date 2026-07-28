from contextlib import contextmanager

from playwright.sync_api import Browser, sync_playwright


@contextmanager
def get_browser():
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()
