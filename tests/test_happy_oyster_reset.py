"""Happy Oyster 复位逻辑测试（连接模式复用标签页 + 复位到输入框）。"""

import asyncio
from unittest.mock import patch

import pytest

from promptchoreo.adapters.happy_oyster import HappyOysterAdapter


class FakeLocator:
    def __init__(self, visible=True, raises=False):
        self._visible = visible
        self._raises = raises
        self.clicked = False

    async def wait_for(self, *a, **k):
        if self._raises:
            raise Exception("not found")
        return None

    async def is_visible(self, *a, **k):
        return self._visible

    async def click(self, *a, **k):
        self.clicked = True

    async def fill(self, *a, **k):
        pass


class FakePage:
    def __init__(self, url="https://www.happyoyster.cn/create/directing", input_visible=True):
        self.url = url
        self._input_visible = input_visible
        self._locators = {}
        self.goto_calls = 0

    def locator(self, sel):
        if sel not in self._locators:
            is_input = sel == HappyOysterAdapter.SELECTORS["initial_input"]
            vis = self._input_visible if is_input else True
            self._locators[sel] = FakeLocator(visible=vis, raises=not vis)
        return self._locators[sel]

    async def goto(self, url, **k):
        self.goto_calls += 1
        self.url = url
        self._input_visible = True
        self._locators = {}  # 重新导航后 locator 状态刷新


class FakeRecorder:
    def __init__(self):
        self.stop_calls = 0

    def start(self):
        return True

    def stop(self):
        self.stop_calls += 1
        return True


def _make_adapter():
    return HappyOysterAdapter(
        {
            "_recorder_enabled": True,
            "_recorder_start_hotkey": "ctrl+f1",
            "_recorder_stop_hotkey": "ctrl+f2",
            "initial_prompt": "x",
            "_inject_events": [],
        }
    )


def test_is_at_input_box_true():
    a = _make_adapter()
    page = FakePage(input_visible=True)
    assert asyncio.run(a._is_at_input_box(page)) is True


def test_is_at_input_box_false_on_explore_page():
    a = _make_adapter()
    page = FakePage(
        url="https://www.happyoyster.cn/explore/story/abc", input_visible=False
    )
    assert asyncio.run(a._is_at_input_box(page)) is False


def test_reset_skips_goto_when_already_at_input():
    a = _make_adapter()
    page = FakePage(input_visible=True)
    a._session_started = True
    a.generation_start_monotonic = 1.0
    asyncio.run(a._reset_to_input(page))
    # 已在输入框界面，不应重新导航
    assert page.goto_calls == 0
    # 内部状态应被清空
    assert a._session_started is False
    assert a.generation_start_monotonic is None
    assert a._recorder_stopped is False


def test_reset_goto_when_on_explore_page():
    a = _make_adapter()
    page = FakePage(
        url="https://www.happyoyster.cn/explore/story/abc", input_visible=False
    )
    asyncio.run(a._reset_to_input(page))
    assert page.goto_calls == 1
    assert a._session_started is False


def test_recorder_stop_idempotent():
    a = _make_adapter()
    a._ext_recorder = FakeRecorder()
    page = FakePage()
    asyncio.run(a._recorder_stop(page))
    asyncio.run(a._recorder_stop(page))
    assert a._ext_recorder.stop_calls == 1
    assert a._recorder_stopped is True


def test_teardown_stops_recorder_and_resets():
    a = _make_adapter()
    a._ext_recorder = FakeRecorder()
    a._session_started = True
    page = FakePage(input_visible=True)
    asyncio.run(a.teardown(page))
    assert a._ext_recorder.stop_calls == 1
    assert a._session_started is False


class FakePageTimer(FakePage):
    """evaluate 区分计时器查询与 body 文本查询，分别返回不同值。"""

    def __init__(self, seq):
        super().__init__()
        self._seq = list(seq)
        self._i = 0

    async def evaluate(self, *a, **k):
        # 计时器查询（_get_page_timer 的 JS 里包含 "REC" 关键字）→ 按序列返回
        arg = a[0] if a else ""
        if isinstance(arg, str) and "REC" in arg:
            if self._i < len(self._seq):
                v = self._seq[self._i]
                self._i += 1
                return v
            return self._seq[-1] if self._seq else None
        # body 文本查询 / 加载百分比查询 → 返回空字符串
        return ""


def test_wait_for_playback_returns_when_visible_rec_timer_increments():
    a = _make_adapter()
    page = FakePageTimer([None, 0, 0, 1])

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        assert asyncio.run(a._wait_for_playback(page, 5)) is True


def test_wait_for_playback_rejects_visible_static_zero_timer():
    a = _make_adapter()
    page = FakePageTimer([0, 0, 0])

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        assert asyncio.run(a._wait_for_playback(page, 0.2)) is False


def test_wait_for_playback_rejects_visible_static_nonzero_timer():
    a = _make_adapter()
    page = FakePageTimer([60, 60, 60])

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        assert asyncio.run(a._wait_for_playback(page, 0.2)) is False


def test_injection_schedule_uses_recording_clock_not_page_timer():
    a = _make_adapter()
    a.recording_start_monotonic = 100.0
    a._job_start_monotonic = 90.0
    a._post_inject_delay = 0
    clock = {"now": 100.0}
    sent_at = []

    async def fake_sleep(seconds):
        clock["now"] += seconds

    async def no_generation_error(page):
        return None

    async def prepare(page, prompt):
        assert prompt == "turn left"
        clock["now"] += 0.2

    async def send(page):
        sent_at.append(clock["now"])
        return clock["now"]

    async def no_notification(page, prompt):
        return None

    async def no_finish(page, duration):
        assert duration == 31.0

    async def page_timer_is_unavailable(page):
        return None

    a._raise_if_generation_failed = no_generation_error
    a._prepare_inject = prepare
    a._send_prepared_inject = send
    a._wait_for_notification = no_notification
    a._finish_recording = no_finish
    a._get_page_timer = page_timer_is_unavailable

    event = {
        "time": 30,
        "prompt": "turn left",
        "prompt_id": "i1",
        "role": "update",
    }
    with patch(
        "promptchoreo.adapters.happy_oyster.time.monotonic",
        side_effect=lambda: clock["now"],
    ), patch("asyncio.sleep", fake_sleep):
        asyncio.run(a._run_injection_loop(FakePage(), [event], 1))

    assert sent_at == [130.0]
    assert a._injection_log[-1]["scheduled_media_time_s"] == 30.0
    assert a._injection_log[-1]["actual_media_time_s"] == 30.0


class FakeGenerationErrorPage(FakePage):
    async def evaluate(self, *a, **k):
        return "内容无法生成，请换个描述试试"


class FakePlaybackUnavailablePage(FakePage):
    async def evaluate(self, *a, **k):
        return "This scene can't be played right now"


class FakeOopsPage(FakePage):
    async def evaluate(self, *a, **k):
        return "Oops / Something went wrong"


def test_generation_error_page_raises_stable_site_failure():
    a = _make_adapter()
    page = FakeGenerationErrorPage()
    with pytest.raises(RuntimeError, match="site_generation_failed"):
        asyncio.run(a._raise_if_generation_failed(page))


def test_playback_unavailable_stops_recorder_and_raises_skip_failure():
    a = _make_adapter()
    a._ext_recorder = FakeRecorder()
    a.recording_start_monotonic = 100.0
    page = FakePlaybackUnavailablePage()

    with pytest.raises(RuntimeError, match="site_playback_unavailable"):
        asyncio.run(a._raise_if_generation_failed(page))

    assert a._ext_recorder.stop_calls == 1
    assert a._recorder_stopped is True


def test_oops_stops_recorder_and_raises_nonretryable_failure():
    a = _make_adapter()
    a._ext_recorder = FakeRecorder()
    a.recording_start_monotonic = 100.0
    page = FakeOopsPage()

    with pytest.raises(RuntimeError, match="site_generation_nonretryable"):
        asyncio.run(a._raise_if_generation_failed(page))

    assert a._ext_recorder.stop_calls == 1
    assert a._recorder_stopped is True
