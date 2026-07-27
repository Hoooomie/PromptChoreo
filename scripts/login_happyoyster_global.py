"""Open HappyOyster international in a dedicated persistent browser profile.

Usage:
    python scripts/login_happyoyster_global.py
"""

import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.promptchoreo.adapters.happy_oyster_global import (
    HappyOysterGlobalAdapter,
)


USER_DATA_DIR = HappyOysterGlobalAdapter.user_data_dir
LOGIN_URL = HappyOysterGlobalAdapter.URL_HOME


def main() -> None:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        print("=" * 60)
        print("HappyOyster international is open. Please sign in manually.")
        print(f"Login profile: {USER_DATA_DIR}")
        print("After sign-in, return here and press Enter to save and close.")
        print("=" * 60)
        input("Press Enter after sign-in...")
        context.close()
        print("Browser closed. International-site login state was saved.")


if __name__ == "__main__":
    main()
