"""Odyssey 复位逻辑测试：点 X → 回到输入框界面，并清空内部会话状态。"""

import asyncio
import time

import pytest

from promptchoreo.adapters.base import RetryCurrentJob
from promptchoreo.adapters.odyssey import ContentBlockedError, OdysseyAdapter


class FakeLocator:
    def __init__(self, visible: bool = True, raises: bool = False) -> None:
        self._visible = visible
        self._raises = raises
        self.clicked = False

    async def wait_for(self, *a, **k):
        if self._raises:
            raise Exception("not found")
        return None

    @property
    def first(self):
        return self

    async def is_visible(self, timeout: int = 2000) -> bool:
        return self._visible

    async def click(self, timeout: int = 5000) -> None:
        self.clicked = True


class FakePage:
    def __init__(
        self,
        landing_visible: bool = True,
        x_visible: bool = True,
        session_ended_visible: bool = False,
        try_again_visible: bool = False,
    ) -> None:
        self._landing = landing_visible
        self._x = x_visible
        self._session_ended = session_ended_visible
        self._try_again = try_again_visible
        self._locators: dict = {}

    def locator(self, sel: str) -> FakeLocator:
        if sel not in self._locators:
            if sel == OdysseyAdapter.SELECTORS["landing_textarea"]:
                self._locators[sel] = FakeLocator(visible=self._landing, raises=not self._landing)
            elif sel == OdysseyAdapter.SELECTORS["session_ended_dialog"]:
                self._locators[sel] = FakeLocator(visible=self._session_ended, raises=False)
            elif sel == OdysseyAdapter.SELECTORS["try_again_button"]:
                self._locators[sel] = FakeLocator(visible=self._try_again, raises=False)
            else:
                self._locators[sel] = FakeLocator(visible=self._x, raises=not self._x)
        return self._locators[sel]


def _make_adapter() -> OdysseyAdapter:
    a = OdysseyAdapter({"_connect_mode": True})
    a._session_started = True
    a.generation_start_monotonic = 1.0
    a.crop_region = "1:2:3:4"
    return a


def test_reset_when_already_at_input_skips_click():
    a = _make_adapter()
    page = FakePage(landing_visible=True)
    asyncio.run(a._reset_to_input(page))
    assert a._session_started is False
    assert a.generation_start_monotonic is None
    assert a.crop_region is None


def test_reset_clicks_x_then_clears_state():
    a = _make_adapter()
    page = FakePage(landing_visible=False, x_visible=True)
    asyncio.run(a._reset_to_input(page))
    x_loc = page._locators[OdysseyAdapter.SELECTORS["close_button"]]
    assert x_loc.clicked is True
    assert a._session_started is False
    assert a.crop_region is None


def test_reset_x_not_found_still_clears_state():
    a = _make_adapter()
    page = FakePage(landing_visible=False, x_visible=False)
    asyncio.run(a._reset_to_input(page))
    assert a._session_started is False
    assert a.generation_start_monotonic is None
    assert a.crop_region is None


def test_reset_session_ended_clicks_try_again():
    """会话超时弹窗出现时，复位应走 Try Again 分支而非 X 按钮。

    点 Try Again 后站点回到输入框界面（landing_visible 变为 True）。
    """
    a = _make_adapter()
    page = FakePage(
        landing_visible=False,  # 初始不在输入框（弹窗挡住了）
        x_visible=True,
        session_ended_visible=True,
        try_again_visible=True,
    )

    # 让 landing 在 Try Again 之后变成可见（模拟弹窗消失、输入框重现）
    original_locator = page.locator

    def smart_locator(sel: str):
        loc = original_locator(sel)
        if sel == OdysseyAdapter.SELECTORS["landing_textarea"]:
            # 第一次调用时 landing 不可见（弹窗状态），之后变为可见
            call_count = getattr(page, "_landing_call_count", 0)
            page._landing_call_count = call_count + 1
            if call_count > 2:  # Try Again 之后再次查询 → 可见
                loc._visible = True
                loc._raises = False
        return loc

    page.locator = smart_locator

    asyncio.run(a._reset_to_input(page))
    # 应该点了 Try Again 而不是 X
    try_again_loc = page._locators.get(OdysseyAdapter.SELECTORS["try_again_button"])
    x_loc = page._locators.get(OdysseyAdapter.SELECTORS["close_button"])
    assert try_again_loc is not None and try_again_loc.clicked is True
    assert x_loc is None or not x_loc.clicked
    assert a._session_started is False
    assert a.crop_region is None


def test_video_wait_ignores_timeout_and_waits_until_ready():
    """Odyssey 的首帧等待不应再受基类传入的固定 timeout 限制。"""
    adapter = OdysseyAdapter(
        {"_required_duration_s": 60, "_render_guard_s": 3}
    )
    checks = 0

    async def session_not_ended(page, *, raise_if_ended=True):
        return False

    async def plenty_of_time(page):
        return 120.0

    class VideoPage:
        async def evaluate(self, script):
            nonlocal checks
            checks += 1
            return checks >= 2

    adapter._check_session_ended = session_not_ended
    adapter._get_top_timer_seconds = plenty_of_time

    asyncio.run(adapter._wait_video_ready(VideoPage(), timeout=0))
    assert checks == 2


def test_video_wait_abandons_before_accepting_late_video():
    """触及预算底线后不得再查询/接受随后出现的视频。"""
    adapter = OdysseyAdapter(
        {"_required_duration_s": 60, "_render_guard_s": 3}
    )
    reset_waited = False

    async def session_not_ended(page, *, raise_if_ended=True):
        return False

    async def at_budget_floor(page):
        return 63.0

    async def wait_for_reset(page):
        nonlocal reset_waited
        reset_waited = True

    class LateVideoPage:
        async def evaluate(self, script):
            raise AssertionError("预算不足后不应再检查晚到的视频")

    adapter._check_session_ended = session_not_ended
    adapter._get_top_timer_seconds = at_budget_floor
    adapter._wait_for_session_end_and_reset = wait_for_reset

    with pytest.raises(
        RetryCurrentJob,
        match="insufficient_session_time_while_waiting_for_video",
    ):
        asyncio.run(adapter._wait_video_ready(LateVideoPage()))
    assert reset_waited is True


def test_setup_retries_same_job_after_video_wait_abandonment():
    """Try Again 复位后，setup 应重新提交同一个 initial prompt。"""
    adapter = OdysseyAdapter({"initial_prompt": "same prompt"})
    page = FakePage(landing_visible=True)
    starts = 0
    warmups = 0

    async def no_op(page):
        return None

    async def run_warmup(page):
        nonlocal warmups
        warmups += 1

    async def start_then_retry(page, prompt):
        nonlocal starts
        starts += 1
        assert prompt == "same prompt"
        if starts == 1:
            adapter._session_started = True
            adapter.generation_start_monotonic = 123.0
            raise RetryCurrentJob(
                "insufficient_session_time_while_waiting_for_video"
            )

    adapter._enter_fullscreen = no_op
    adapter._ensure_session_budget = no_op
    adapter._run_retry_warmup = run_warmup
    adapter._start_session = start_then_retry

    asyncio.run(adapter.setup(page))
    assert starts == 2
    assert warmups == 1
    assert adapter._video_wait_retry_count == 1
    assert (
        adapter._retry_reason
        == "insufficient_session_time_while_waiting_for_video"
    )


def test_playback_content_blocked_is_closed_and_recording_continues():
    adapter = OdysseyAdapter({"_connect_mode": True})
    adapter._session_started = True
    adapter._job_start_monotonic = time.monotonic() - 15
    adapter.recording_start_monotonic = time.monotonic() - 12
    prompt_event = {
        "prompt_id": "B-0027:prompt:04",
        "role": "update",
        "scheduled_media_time_s": 40.0,
        "actual_media_time_s": 40.1,
        "actual_injection_time_s": 45.0,
        "status": "accepted",
        "error": None,
    }
    adapter._latest_prompt_event = prompt_event
    adapter._injection_log = [prompt_event]

    class ContentBlockedPage:
        def __init__(self):
            self.body_text = (
                "Content Blocked\n"
                "Your request was flagged for inappropriate content."
            )
            self.clicks = 0

        async def evaluate(self, script):
            if "document.body.innerText" in script:
                return self.body_text
            self.clicks += 1
            self.body_text = ""
            return True

    page = ContentBlockedPage()

    async def recorder_must_not_stop(page):
        raise AssertionError("playback-time Content Blocked must not stop recording")

    adapter._recorder_stop = recorder_must_not_stop

    assert asyncio.run(adapter._handle_playback_content_blocked(page)) is True
    assert page.clicks == 1
    assert adapter._session_started is True
    assert prompt_event["status"] == "failed"
    assert prompt_event["error"].startswith("content_blocked:")
    assert len(adapter._content_blocked_events) == 1
    assert adapter._content_blocked_events[0]["prompt_id"] == "B-0027:prompt:04"
    assert adapter._content_blocked_events[0]["dialog_closed"] is True

    assert asyncio.run(adapter._handle_playback_content_blocked(page)) is False
    assert len(adapter._content_blocked_events) == 1


def test_pre_playback_content_blocked_remains_fatal():
    adapter = OdysseyAdapter({"_connect_mode": True})
    prompt_event = {
        "prompt_id": "B-0027:prompt:00",
        "role": "initial",
        "status": "accepted",
        "error": None,
    }
    dismissed = False

    class ContentBlockedPage:
        async def evaluate(self, script):
            return (
                "Content Blocked\n"
                "Your request was flagged for inappropriate content."
            )

    async def dismiss(page):
        nonlocal dismissed
        dismissed = True

    adapter._dismiss_content_blocked = dismiss

    with pytest.raises(ContentBlockedError) as exc_info:
        asyncio.run(
            adapter._raise_if_content_blocked(
                ContentBlockedPage(), prompt_event
            )
        )

    assert dismissed is True
    assert exc_info.value.prompt_event["status"] == "failed"
    assert exc_info.value.prompt_event["error"].startswith("content_blocked:")
