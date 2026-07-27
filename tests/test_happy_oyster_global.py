"""HappyOyster international-site adapter tests."""

import asyncio

import pytest

from promptchoreo.adapters.happy_oyster import HappyOysterAdapter
from promptchoreo.adapters.happy_oyster_global import (
    HappyOysterGlobalAdapter,
)


class FakeButton:
    def __init__(self):
        self.clicked = False

    async def wait_for(self, *args, **kwargs):
        return None

    async def click(self, *args, **kwargs):
        self.clicked = True


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
    assert "Describe your story" in adapter.SELECTORS["initial_input"]
    assert adapter.SELECTORS["initial_send"] == "button[aria-label='Send']"
    assert "direct" in adapter._stream_input_selector()


def test_global_initial_submit_uses_accessible_send_button():
    adapter = HappyOysterGlobalAdapter()
    page = FakeSubmitPage()

    asyncio.run(adapter._submit_initial(page))

    assert page.selector == "button[aria-label='Send']"
    assert page.button.clicked is True


def test_global_generation_error_raises_stable_site_failure():
    adapter = HappyOysterGlobalAdapter()
    with pytest.raises(RuntimeError, match="site_generation_failed"):
        asyncio.run(
            adapter._raise_if_generation_failed(
                FakeGenerationErrorPage()
            )
        )
