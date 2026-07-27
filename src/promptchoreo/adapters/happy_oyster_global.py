"""HappyOyster international-site adapter for Directing Mode."""

from __future__ import annotations

import sys

from playwright.async_api import Page

from .happy_oyster import HappyOysterAdapter
from ..credentials import get_browser_data_dir


class HappyOysterGlobalAdapter(HappyOysterAdapter):
    """HappyOyster Directing Mode adapter for ``happyoyster.com``."""

    name = "happy_oyster_global"
    user_data_dir = get_browser_data_dir("happy_oyster_global")

    URL_HOME = "https://www.happyoyster.com/home"
    URL_DIRECTING = "https://www.happyoyster.com/create/directing"
    URL = URL_DIRECTING

    SELECTORS = {
        **HappyOysterAdapter.SELECTORS,
        "initial_input": (
            "textarea[placeholder*='Describe your story'], "
            "textarea.absolute.inset-0"
        ),
        "initial_send": "button[aria-label='Send']",
        "initial_send_form": "button[aria-label='Send']",
        "stream_input": (
            "textarea[placeholder*='direct'], "
            "textarea[placeholder*='Direct']"
        ),
        "stream_send": (
            "button.story-send-btn, button[aria-label='Send']"
        ),
        "login_button": (
            "a:has-text('Sign in'), button:has-text('Sign in'), "
            "a:has-text('Log in'), button:has-text('Log in')"
        ),
    }

    async def _submit_initial(self, page: Page) -> None:
        """Submit through the international site's accessible Send button."""
        try:
            button = page.locator(self.SELECTORS["initial_send"])
            await button.wait_for(state="visible", timeout=12000)
            await button.click(timeout=5000)
            print(
                "[DEBUG] Initial submit via international Send button",
                file=sys.stderr,
            )
            return
        except Exception as exc:
            print(
                "[DEBUG] International Send button failed; "
                f"falling back to shared selector: {exc}",
                file=sys.stderr,
            )
        await super()._submit_initial(page)
