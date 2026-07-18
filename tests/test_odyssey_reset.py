"""Odyssey 复位逻辑测试：点 X → 回到输入框界面，并清空内部会话状态。"""

import asyncio

from promptchoreo.adapters.odyssey import OdysseyAdapter


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
