"""Happy Oyster 复位逻辑测试（连接模式复用标签页 + 复位到输入框）。"""

import asyncio
from unittest.mock import patch

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
    assert a.crop_region is None


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
    """evaluate 按预设序列返回页面计时器值（None 表示无计时器）。"""

    def __init__(self, seq):
        super().__init__()
        self._seq = list(seq)
        self._i = 0

    async def evaluate(self, *a, **k):
        if self._i < len(self._seq):
            v = self._seq[self._i]
            self._i += 1
            return v
        return self._seq[-1] if self._seq else None


def test_wait_for_playback_true_on_increment():
    a = _make_adapter()
    page = FakePageTimer([0, 1])  # 计时器 0→1 递增 = 视频开始播放

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        assert asyncio.run(a._wait_for_playback(page, 5)) is True


def test_wait_for_playback_false_on_static_timer():
    a = _make_adapter()
    page = FakePageTimer([60, 60, 60])  # 加载期静态总时长，不递增

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        # 静态值不会递增 → 超时返回 False（绝不盲判为播放而开录）
        assert asyncio.run(a._wait_for_playback(page, 0.5)) is False
