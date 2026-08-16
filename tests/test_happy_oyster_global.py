"""HappyOyster international-site adapter tests."""

import asyncio

import pytest

from promptchoreo.adapters.happy_oyster import HappyOysterAdapter
from promptchoreo.adapters.happy_oyster_global import (
    GoogleAccountIsolationRequired,
    HappyOysterGlobalAdapter,
)


class FakeButton:
    def __init__(self):
        self.clicked = False

    async def wait_for(self, *args, **kwargs):
        return None

    async def click(self, *args, **kwargs):
        self.clicked = True


class FakeField(FakeButton):
    def __init__(self):
        super().__init__()
        self.value = None

    async def fill(self, value):
        self.value = value


class FakeSubmitPage:
    def __init__(self):
        self.selector = None
        self.button = FakeButton()

    def locator(self, selector):
        self.selector = selector
        return self.button


class FakeGenerationErrorPage:
    async def evaluate(self, *args, **kwargs):
        return "Content could not be generated"


class EmptyLocator:
    async def all(self):
        return []


class TextOnlySignInPage:
    """Models the live non-semantic sign-in wrapper."""

    def __init__(self):
        self.clicked = False

    def locator(self, selector):
        return EmptyLocator()

    async def evaluate(self, script, needle):
        assert needle == HappyOysterGlobalAdapter.SIGN_IN_TEXT
        if "clickable.click()" in script:
            self.clicked = True
            return True
        return True


class CreditHeaderSignedInPage:
    """Models the live header variant with credits and an avatar button."""

    def locator(self, selector):
        return EmptyLocator()

    async def evaluate(self, script, needle):
        assert needle == HappyOysterGlobalAdapter.SIGN_IN_TEXT
        assert "Create a new world" in script
        return True


class FakeAuthLocator:
    def __init__(self, page, name):
        self.page = page
        self.name = name
        self.first = self

    async def wait_for(self, *args, **kwargs):
        if not await self.is_visible():
            raise RuntimeError("not visible")

    async def all(self):
        return [self]

    async def is_visible(self, *args, **kwargs):
        if self.name in ("campaign_close", "promo_login_button"):
            return False
        if self.name == "cookie_accept":
            return self.page.cookie_open
        if self.name == "user_menu":
            return self.page.logged_in
        if self.name == "login_button":
            return not self.page.logged_in and not self.page.auth_open
        if self.name == "google_signin_method":
            return self.page.auth_open and self.page.google_stage is None
        if self.name in ("google_email_input", "google_email_next"):
            return self.page.google_stage == "email"
        if self.name in ("google_password_input", "google_password_next"):
            return self.page.google_stage == "password"
        if self.name == "logout_button":
            return self.page.menu_open
        if self.name == "captcha":
            return False
        return False

    async def click(self, *args, **kwargs):
        self.page.actions.append(f"click:{self.name}")
        if self.name == "cookie_accept":
            self.page.cookie_open = False
        elif self.name == "login_button":
            self.page.auth_open = True
        elif self.name == "google_signin_method":
            self.page.url = "https://accounts.google.com/v3/signin/identifier"
            self.page.google_stage = "email"
        elif self.name == "google_email_next":
            self.page.google_stage = "password"
        elif self.name == "google_password_next":
            self.page.logged_in = True
            self.page.auth_open = False
            self.page.google_stage = None
            self.page.url = self.page.adapter.URL_HOME
        elif self.name == "user_menu":
            self.page.menu_open = True
        elif self.name == "logout_button":
            self.page.logged_in = False
            self.page.menu_open = False

    async def fill(self, value):
        self.page.actions.append(f"fill:{self.name}")
        self.page.fills[self.name] = value


class FakeAuthPage:
    def __init__(self, adapter):
        self.adapter = adapter
        self.logged_in = False
        self.auth_open = False
        self.cookie_open = True
        self.google_stage = None
        self.menu_open = False
        self.fills = {}
        self.urls = []
        self.actions = []
        self.url = ""
        self.selector_names = {
            value: key for key, value in adapter.SELECTORS.items()
        }

    async def goto(self, url, **kwargs):
        self.urls.append(url)
        self.url = url

    def locator(self, selector):
        return FakeAuthLocator(self, self.selector_names.get(selector, ""))

    async def evaluate(self, script, needle=None):
        if needle == "Sign in with Google":
            await FakeAuthLocator(self, "google_signin_method").click()
            return True
        raise AssertionError("unexpected evaluate call")


def test_global_navigation_retries_transient_connection_close():
    adapter = HappyOysterGlobalAdapter()
    adapter.NAVIGATION_RETRY_TIMEOUT_S = 10

    class FlakyPage:
        def __init__(self):
            self.attempts = 0

        async def goto(self, url, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("net::ERR_CONNECTION_CLOSED")

        def is_closed(self):
            return False

    page = FlakyPage()

    async def instant_sleep(*args, **kwargs):
        return None

    original_sleep = asyncio.sleep
    asyncio.sleep = instant_sleep
    try:
        asyncio.run(adapter._goto_with_retry(page, adapter.URL_HOME))
    finally:
        asyncio.sleep = original_sleep

    assert page.attempts == 3


def test_global_adapter_uses_distinct_site_and_profile():
    assert HappyOysterGlobalAdapter.URL_HOME == (
        "https://www.happyoyster.com/home"
    )
    assert HappyOysterGlobalAdapter.URL_DIRECTING == (
        "https://www.happyoyster.com/create/directing"
    )
    assert (
        HappyOysterGlobalAdapter.user_data_dir
        != HappyOysterAdapter.user_data_dir
    )


def test_global_adapter_uses_observed_english_controls():
    adapter = HappyOysterGlobalAdapter()
    assert adapter.CREDENTIAL_FILL_DELAY_S == 2.0
    assert "Describe your story" in adapter.SELECTORS["initial_input"]
    assert adapter.SELECTORS["initial_send"] == "button[aria-label='Send']"
    assert "direct" in adapter._stream_input_selector()
    assert adapter.SELECTORS["user_menu"] == (
        "button[aria-label='Open user navigation menu']"
    )
    assert "Log out" in adapter.SELECTORS["logout_button"]
    assert "Log in now" in adapter.SELECTORS["promo_login_button"]
    assert "Accept All" in adapter.SELECTORS["cookie_accept"]
    assert "free creative credits" in adapter.SELECTORS["login_button"]
    assert "Sign in with Google" in adapter.SELECTORS[
        "google_signin_method"
    ]


def test_global_initial_submit_uses_accessible_send_button():
    adapter = HappyOysterGlobalAdapter()
    page = FakeSubmitPage()

    asyncio.run(adapter._submit_initial(page))

    assert page.selector == "button[aria-label='Send']"
    assert page.button.clicked is True


def test_signed_out_marker_works_without_button_or_link_tag():
    adapter = HappyOysterGlobalAdapter()
    page = TextOnlySignInPage()

    assert asyncio.run(adapter._has_sign_in_marker(page)) is True
    asyncio.run(adapter._click_sign_in_marker(page))

    assert page.clicked is True


def test_signed_in_detection_accepts_credit_header_variant():
    adapter = HappyOysterGlobalAdapter()

    assert asyncio.run(adapter.is_logged_in(CreditHeaderSignedInPage())) is True


def test_google_image_challenge_waits_for_manual_completion():
    adapter = HappyOysterGlobalAdapter()
    adapter.CREDENTIAL_FILL_DELAY_S = 0
    page = type(
        "GooglePage",
        (),
        {"url": "https://accounts.google.com/v3/signin/identifier"},
    )()
    email_input = FakeField()
    email_next = FakeButton()
    stale_password_input = FakeField()
    fresh_password_input = FakeField()
    password_next = FakeButton()
    password_lookups = 0

    async def fake_first_visible(page_arg, selector, *, timeout_ms=0):
        nonlocal password_lookups
        assert page_arg is page
        if selector == adapter.SELECTORS["google_email_input"]:
            return email_input
        if selector == adapter.SELECTORS["google_email_next"]:
            return email_next
        if selector == adapter.SELECTORS["google_password_input"]:
            password_lookups += 1
            if password_lookups == 1:
                return None
            if password_lookups == 2:
                return stale_password_input
            return fresh_password_input
        if selector == adapter.SELECTORS["google_password_next"]:
            return password_next
        return None

    adapter._first_visible = fake_first_visible

    assert asyncio.run(
        adapter._complete_google_login(page, "user@example.com", "secret")
    ) is True
    assert password_lookups == 3
    assert email_input.value == "user@example.com"
    assert stale_password_input.value is None
    assert fresh_password_input.value == "secret"
    assert password_next.clicked is True


def test_google_login_finds_and_fills_oauth_popup_page():
    adapter = HappyOysterGlobalAdapter()
    adapter.CREDENTIAL_FILL_DELAY_S = 0
    origin = FakeAuthPage(adapter)
    origin.url = adapter.URL_HOME
    popup = FakeAuthPage(adapter)
    popup.url = "https://accounts.google.com/v3/signin/identifier"
    popup.google_stage = "email"
    context = type("Context", (), {"pages": [origin, popup]})()
    origin.context = context
    popup.context = context

    assert asyncio.run(
        adapter._complete_google_login(
            origin, "popup@example.com", "popup-password"
        )
    ) is True

    assert popup.fills == {
        "google_email_input": "popup@example.com",
        "google_password_input": "popup-password",
    }
    assert origin.fills == {}


def test_google_credentials_use_wait_fill_wait_next_sequence():
    adapter = HappyOysterGlobalAdapter()
    origin = FakeAuthPage(adapter)
    origin.url = adapter.URL_HOME
    popup = FakeAuthPage(adapter)
    popup.url = "https://accounts.google.com/v3/signin/identifier"
    popup.google_stage = "email"
    context = type("Context", (), {"pages": [origin, popup]})()
    origin.context = context
    popup.context = context

    async def record_sleep(delay):
        popup.actions.append(f"sleep:{delay}")

    original_sleep = asyncio.sleep
    asyncio.sleep = record_sleep
    try:
        assert asyncio.run(
            adapter._complete_google_login(
                origin, "timing@example.com", "timing-password"
            )
        ) is True
    finally:
        asyncio.sleep = original_sleep

    assert popup.actions == [
        "sleep:2.0",
        "fill:google_email_input",
        "sleep:2.0",
        "click:google_email_next",
        "sleep:2.0",
        "fill:google_password_input",
        "sleep:2.0",
        "click:google_password_next",
    ]


def test_google_page_detection_ignores_non_google_email_form():
    adapter = HappyOysterGlobalAdapter()
    page = FakeAuthPage(adapter)
    page.url = adapter.URL_HOME
    page.google_stage = "email"

    async def not_logged_in(page_arg):
        assert page_arg is page
        return False

    adapter.is_logged_in = not_logged_in

    result = asyncio.run(
        adapter._find_google_auth_page(page, timeout_s=0.01)
    )

    assert result is None
    assert page.fills == {}


def test_google_account_chooser_selects_use_another_account():
    adapter = HappyOysterGlobalAdapter()
    email_input = FakeField()
    page = type("AccountChooserPage", (), {})()
    page.chooser_clicked = False

    async def evaluate(script, labels):
        assert labels == ["使用其他账号", "Use another account"]
        page.chooser_clicked = True
        return True

    page.evaluate = evaluate

    async def fake_first_visible(page_arg, selector, *, timeout_ms=0):
        assert page_arg is page
        assert selector == adapter.SELECTORS["google_email_input"]
        return email_input if page.chooser_clicked else None

    adapter._first_visible = fake_first_visible

    result = asyncio.run(adapter._prepare_google_email_input(page))

    assert page.chooser_clicked is True
    assert result is email_input


def test_google_silent_previous_account_reuse_requires_isolation():
    adapter = HappyOysterGlobalAdapter()
    page = type(
        "SilentlyLoggedInPage",
        (),
        {"url": "https://www.happyoyster.com/home"},
    )()

    async def logged_in(page_arg):
        assert page_arg is page
        return True

    adapter.is_logged_in = logged_in

    with pytest.raises(GoogleAccountIsolationRequired):
        asyncio.run(adapter._prepare_google_email_input(page))


def test_global_generation_error_raises_stable_site_failure():
    adapter = HappyOysterGlobalAdapter()
    with pytest.raises(RuntimeError, match="site_generation_failed"):
        asyncio.run(
            adapter._raise_if_generation_failed(
                FakeGenerationErrorPage()
            )
        )


def test_supervised_global_login_only_handles_google_credentials():
    adapter = HappyOysterGlobalAdapter()
    adapter.CREDENTIAL_FILL_DELAY_S = 0
    page = FakeAuthPage(adapter)

    async def complete_google_login(
        page_arg, email, password, *, auth_page_timeout_s=None
    ):
        assert page_arg is page
        assert email == "account@example.com"
        assert password == "not-a-real-password"
        page.logged_in = True
        return True

    adapter._complete_google_login = complete_google_login

    asyncio.run(
        adapter.login_with_email(
            page, "account@example.com", "not-a-real-password"
        )
    )

    assert page.logged_in is True
    assert page.fills == {}
    assert page.actions == ["click:cookie_accept"]
    assert asyncio.run(adapter.logout(page)) is True
    assert page.logged_in is False


def test_global_logout_is_noop_when_already_logged_out():
    adapter = HappyOysterGlobalAdapter()
    page = FakeAuthPage(adapter)

    assert asyncio.run(adapter.logout(page)) is False
