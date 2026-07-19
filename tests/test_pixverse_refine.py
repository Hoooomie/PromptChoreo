"""PixVerse 精修测试（is_done 守卫 + 按视频时钟注入）。

背景：早期 PixVerse 适配器 _start_session 不跑注入循环、不置 is_done，
导致 setup 已用 initial_prompt 开会话后，调度器又把 time=0 的初始 prompt
当指令二次注入流式框。现照 Happy Oyster 改为内部循环 + is_done 守卫。
"""

import asyncio
from unittest.mock import patch

from promptchoreo.adapters.pixverse import PixVerseAdapter


class FakeLocatorPV:
    def __init__(self):
        self.first = self
        self.fills = []
        self.clicks = 0
        self.presses = []

    async def wait_for(self, *a, **k):
        return None

    async def is_visible(self, *a, **k):
        return True

    async def click(self, *a, **k):
        self.clicks += 1

    async def fill(self, v, *a, **k):
        self.fills.append(v)

    async def press(self, key, *a, **k):
        self.presses.append(key)


class FakePagePV:
    """evaluate 在含 currentTime 的 JS 时按预设序列返回视频时钟，其它返回 None。"""

    def __init__(self, clock_seq):
        self._clock = list(clock_seq)
        self._ci = 0
        self._locators = {}

    def locator(self, sel):
        if sel not in self._locators:
            self._locators[sel] = FakeLocatorPV()
        return self._locators[sel]

    async def evaluate(self, js, *a, **k):
        if "currentTime" in js:
            v = self._clock[self._ci] if self._ci < len(self._clock) else self._clock[-1]
            self._ci += 1
            return v
        return None


def _make_adapter_pv():
    return PixVerseAdapter({"initial_prompt": "x", "_inject_events": []})


def test_is_done_reflects_flag():
    a = _make_adapter_pv()
    assert a.is_done is False
    a._injections_done = True
    assert a.is_done is True


def test_submit_prompt_skips_when_injections_done():
    a = _make_adapter_pv()
    a._injections_done = True
    calls = []

    async def spy_start(page, prompt):
        calls.append(("start", prompt))

    async def spy_inject(page, prompt):
        calls.append(("inject", prompt))

    a._start_session = spy_start
    a._inject_command = spy_inject
    asyncio.run(a.submit_prompt(None, "anything"))
    # 守卫应直接返回，绝不调用 _start_session / _inject_command
    assert calls == []
    assert a.is_done is True


def test_run_injection_loop_injects_at_clock_times():
    a = _make_adapter_pv()
    events = [
        {"time": 10, "prompt": "cmd@10"},
        {"time": 20, "prompt": "cmd@20"},
        {"time": 30, "prompt": "cmd@30"},
    ]
    # 视频时钟缓慢递增 0..31，足以触发每个目标
    page = FakePagePV(list(range(0, 32)))
    inject_loc = page.locator(PixVerseAdapter.SELECTORS["stream_textarea"])

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        asyncio.run(a._run_injection_loop(page, events, end_delay=0.0))

    injected = [f for f in inject_loc.fills if f]
    assert injected == ["cmd@10", "cmd@20", "cmd@30"], injected


def test_run_injection_loop_tail_uses_end_delay():
    # 回归：录制尾巴必须由 end_delay 控制，不能被写死成事件间隔。
    a = _make_adapter_pv()
    events = [{"time": 10, "prompt": "c10"}, {"time": 20, "prompt": "c20"}]
    page = FakePagePV(list(range(0, 21)))
    sleeps = []

    async def _sleep_capture(s, *a, **k):
        sleeps.append(s)

    with patch("asyncio.sleep", _sleep_capture):
        asyncio.run(a._run_injection_loop(page, events, end_delay=15.0))
    assert max(sleeps) == 15.0, sleeps


# ========== 模式选择（Story） ==========

class _Loc:
    def __init__(self):
        self.first = self

    async def is_visible(self, *a, **k):
        return True


class FakeModeBtn(_Loc):
    def __init__(self, text):
        super().__init__()
        self._text = text
        self.clicks = 0

    async def inner_text(self, *a, **k):
        return self._text

    async def click(self, *a, **k):
        self.clicks += 1


class FakeOpt(_Loc):
    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self.clicks = 0

    async def click(self, *a, **k):
        self.clicks += 1
        self._owner._mode_text = "Mode · Story"


class FakeStub(_Loc):
    async def is_visible(self, *a, **k):
        return False


class FakePageMode:
    def __init__(self, start_text):
        self._mode_text = start_text
        self._btn = FakeModeBtn(start_text)
        self._opt = FakeOpt(self)
        self.mouse_clicks = 0

    def locator(self, sel):
        if "has-text('Mode')" in sel:
            return self._btn
        if "Story" in sel:
            return self._opt
        return FakeStub()

    class _Mouse:
        def __init__(self, o):
            self.o = o

        async def click(self, *a, **k):
            self.o.mouse_clicks += 1

    @property
    def mouse(self):
        return FakePageMode._Mouse(self)


def test_ensure_mode_switches_when_not_story():
    a = PixVerseAdapter({"mode": "story"})
    page = FakePageMode("Mode · World")
    asyncio.run(a._ensure_mode(page))
    assert page._btn.clicks == 1, "非 Story 应点击 Mode 打开下拉"
    assert page._opt.clicks == 1, "应点选 Story 选项"
    assert page._mode_text == "Mode · Story"


def test_ensure_mode_always_selects_target():
    # 即使触发器文字已是 "Mode · Story"，也要显式点选（PixVerse 文字/真实模式可能错位，
    # 且跨视频复用标签页后模式可能被切走，不点选就会以错模式开跑）。
    a = PixVerseAdapter({"mode": "story"})
    page = FakePageMode("Mode · Story")
    asyncio.run(a._ensure_mode(page))
    assert page._btn.clicks == 1, "应点击 Mode 打开下拉（即便文字已是 Story）"
    assert page._opt.clicks == 1, "应点选 Story 选项"
    assert page._mode_text == "Mode · Story"


# ========== 配乐关闭（hover 后点视频右下角 🎵） ==========

class FakeBgmVideo:
    def __init__(self, box):
        self.first = self
        self._box = box

    async def bounding_box(self):
        return self._box

    async def count(self):
        return 0

    async def is_visible(self, *a, **k):
        return True


class FakeBgmEmptyLocator:
    def __init__(self):
        self.first = self

    async def count(self):
        return 0

    async def is_visible(self, *a, **k):
        return False


class FakeBgmMouse:
    def __init__(self):
        self.moves = []
        self.clicks = []

    async def move(self, x, y):
        self.moves.append((x, y))

    async def click(self, x, y):
        self.clicks.append((x, y))


class FakeBgmPage:
    """模拟 hover + JS 原子点击流程。"""

    def __init__(self, evaluate_result=None):
        self.mouse = FakeBgmMouse()
        self._video = FakeBgmVideo({"x": 100, "y": 50, "width": 800, "height": 450})
        self._evaluate_result = evaluate_result or {
            "ok": True,
            "reason": "clicked",
            "target": {"tag": "BUTTON", "cls": "music-btn", "aria": "music"},
            "hitCount": 1,
        }
        self.evaluate_calls = 0

    def locator(self, sel):
        if sel == "video":
            return self._video
        return FakeBgmEmptyLocator()

    async def evaluate(self, *a, **k):
        self.evaluate_calls += 1
        return self._evaluate_result


def test_toggle_bgm_off_hovers_then_js_clicks():
    """hover 视频中心 → JS 原子找到音乐按钮并点击。"""
    a = PixVerseAdapter({})
    page = FakeBgmPage()

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        asyncio.run(a._toggle_bgm_off(page))

    # hover 视频中心 (100+400, 50+225) = (500, 275)
    assert page.mouse.moves[0] == (500, 275)
    # JS evaluate 被调用了（原子点击）
    assert page.evaluate_calls >= 1


def test_toggle_bgm_off_retries_when_no_button_found():
    """JS 没找到按钮 → 重新 hover 重试。"""
    a = PixVerseAdapter({})
    page = FakeBgmPage(evaluate_result={"ok": False, "reason": "no_buttons_in_bottom_zone", "hitCount": 0})

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        asyncio.run(a._toggle_bgm_off(page))

    # 应该重试多次（默认 5 次）
    assert page.evaluate_calls == 5


def test_toggle_bgm_off_skips_when_already_off():
    """aria-pressed=false → 配乐已关，跳过点击。"""
    a = PixVerseAdapter({})
    page = FakeBgmPage(evaluate_result={
        "ok": True, "reason": "already_off",
        "target": {"pressed": "false"},
    })

    async def _instant(*a, **k):
        return

    with patch("asyncio.sleep", _instant):
        asyncio.run(a._toggle_bgm_off(page))

    # 只调了一次 evaluate 就确认已关、退出
    assert page.evaluate_calls == 1
