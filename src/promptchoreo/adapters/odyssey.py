"""Odyssey 交互模拟适配器。

URL: https://experience.odyssey.ml/

流程：
1. setup: 导航 → 填 initial_prompt → 提交 → 等排队 → 等模拟就绪 → 关闭音频
2. submit_prompt: 注入交互指令（Enter 发送）
3. teardown: 点右上角 X 关闭 → 记录停止时间戳
4. 异常信号：会话超时弹出「Your SessionEnded」→ 停录 → Try Again 复位

结果通过 Playwright 屏幕录制获得。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .base import SiteAdapter


class SessionEndedError(Exception):
    """Odyssey 会话时长耗尽，弹出了「Your Session Ended」。"""
    pass


class OdysseyAdapter(SiteAdapter):
    """Odyssey AI 交互模拟适配器。"""

    name = "odyssey"
    resets_clock = True

    URL = "https://experience.odyssey.ml/"

    SELECTORS = {
        "landing_textarea": "textarea[placeholder*='Describe']",
        "submit_button": "button:has-text('Start Simulating')",
        "stream_textarea": "textarea.header-xs",
        "close_button": "button[class*='left-[calc(100%+9px)]']",
        "video_overlay": "div.cursor-text",
        # 会话超时弹窗
        "session_ended_dialog": "text=Your Session Ended",
        "try_again_button": "button:has-text('Try Again')",
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._session_started = False
        self.generation_start_monotonic: float | None = None
        self.stop_monotonic: float | None = None
        self.crop_region: str | None = None
        self._max_queue_wait = self.config.get("max_queue_wait", 300)
        self._post_inject_delay = self.config.get("post_inject_delay", 0.5)
        # 站点加载不稳定时，可在 yaml 里调大（单位秒），默认 8s
        self._load_wait = float(self.config.get("load_wait", 8))

    async def setup(self, page: Page) -> None:
        # 若标签页已停在输入框界面（上一次 run 的 teardown 已点 X 复位，
        # 或 launch 脚本直接开在这），跳过整页重载，直接复用——省一次网络请求，
        # 也避免重置用户手动开的全屏。
        already_at_input = False
        try:
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=3000
            )
            already_at_input = True
        except Exception:
            already_at_input = False

        if not already_at_input:
            await page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
            # Odyssey 站点加载不稳定，固定多等一会儿让前端 settle
            await asyncio.sleep(self._load_wait)
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=60000
            )

        # 全屏由用户手动控制（CDP 模式自动跳过），其余模式在此处理
        await self._enter_fullscreen(page)

        initial_prompt = self.config.get("initial_prompt")
        if initial_prompt:
            print(f"[DEBUG] setup 阶段启动模拟: initial_prompt={initial_prompt!r}", file=sys.stderr)
            await self._start_session(page, str(initial_prompt))

    async def submit_prompt(self, page: Page, prompt: str, target_time: float | None = None) -> None:
        if not self._session_started:
            await self._start_session(page, prompt)
        else:
            await self._inject_command(page, prompt)

    async def _start_session(self, page: Page, prompt: str) -> None:
        input_loc = page.locator(self.SELECTORS["landing_textarea"])
        await input_loc.wait_for(state="visible", timeout=60000)
        await input_loc.click()
        await input_loc.fill(prompt)
        await asyncio.sleep(0.5)

        submit_btn = page.locator(self.SELECTORS["submit_button"])
        await submit_btn.wait_for(state="visible", timeout=10000)
        await submit_btn.click()

        print("[DEBUG] 等待模拟就绪...", file=sys.stderr)
        deadline = time.monotonic() + self._max_queue_wait
        while time.monotonic() < deadline:
            body_text = await page.evaluate("() => document.body.innerText || ''")
            if re.search(r"simulating", body_text, re.I):
                print("[DEBUG] 检测到 'simulating' 文本", file=sys.stderr)
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError(f"排队超时（{self._max_queue_wait}s），模拟未就绪")

        # 生成已开始 → 立即启动外部录屏（尽量早，不遗漏开头）。
        # 之后的 sleep/等交互框/静音都在录制范围内，无妨。
        self.generation_start_monotonic = time.monotonic()
        self._session_started = True
        await self._recorder_start(page)

        await asyncio.sleep(5)

        stream_ta = await self._find_stream_textarea(page, timeout=120)
        if stream_ta is None:
            raise RuntimeError("找不到交互 textarea")
        await stream_ta.wait_for(state="visible", timeout=30000)
        print("[DEBUG] 交互 textarea 已出现", file=sys.stderr)

        # 关闭配乐（点生成界面 🎵），保留视频原声（不做浏览器层静音）
        await self._toggle_bgm_off(page)

        # 测出真正的视频/画布内容包围盒，作为裁剪区（去掉周边 UI）
        # canvas 可能晚几秒才撑满尺寸，重试几次
        for _ in range(10):
            await self._detect_content_region(page)
            if self.crop_region:
                break
            await asyncio.sleep(1)

    async def _check_session_ended(self, page: Page, *, raise_if_ended: bool = True) -> bool:
        """检测 Odyssey 会话超时弹窗「Your Session Ended」。

        Parameters
        ----------
        raise_if_ended : True（默认）
            检测到弹窗时抛出 SessionEndedError；设为 False 仅返回 bool。

        Returns
        -------
        True  → 弹窗出现（会话已结束）
        False → 弹窗未出现（正常）
        """
        try:
            dialog = page.locator(self.SELECTORS["session_ended_dialog"])
            if await dialog.is_visible(timeout=500):
                print("[SessionEnded] 检测到会话超时弹窗", file=sys.stderr)
                if raise_if_ended:
                    raise SessionEndedError("Odyssey 会话已耗尽：Your Session Ended")
                return True
        except PlaywrightTimeout:
            pass
        return False

    async def _dismiss_session_ended(self, page: Page) -> None:
        """点「Try Again」关闭会话超时弹窗，让站点回到输入框界面。"""
        try:
            btn = page.locator(self.SELECTORS["try_again_button"])
            if await btn.is_visible(timeout=3000):
                await btn.click(timeout=5000)
                print("[SessionEnded] 已点击 Try Again", file=sys.stderr)
                # 等 landing 输入框重新出现
                await page.locator(
                    self.SELECTORS["landing_textarea"]
                ).wait_for(state="visible", timeout=15000)
                print("[SessionEnded] 已回到输入框界面", file=sys.stderr)
            else:
                print("[SessionEnded] Try Again 按钮不可见", file=sys.stderr)
        except Exception as e:
            print(f"[SessionEnded] Try Again 点击失败: {e}", file=sys.stderr)

    async def _find_stream_textarea(self, page: Page, timeout: float = 60) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for sel in ["textarea", "textarea[enterkeyhint]", self.SELECTORS["stream_textarea"]]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1000):
                        return loc
                except Exception:
                    pass
            await asyncio.sleep(3)
        return None

    async def _inject_command(self, page: Page, command: str) -> None:
        # 注入前检查会话是否已耗尽——若弹出「Your Session Ended」，立即中断
        await self._check_session_ended(page)

        try:
            click_area = page.locator(self.SELECTORS["video_overlay"]).first
            if await click_area.is_visible(timeout=2000):
                await click_area.click(position={"x": 100, "y": 50})
                await asyncio.sleep(0.3)
        except Exception:
            pass

        # 注入过程中交互框可能因超时消失，这里也做一次检测
        try:
            stream_ta = page.locator(self.SELECTORS["stream_textarea"]).first
            await stream_ta.wait_for(state="visible", timeout=10000)
            await stream_ta.click()
            await stream_ta.fill("")
            await stream_ta.fill(command)
            await asyncio.sleep(0.2)
            await stream_ta.press("Enter")
            print(f"[DEBUG] 已注入: {command[:50]}", file=sys.stderr)
        except PlaywrightTimeout:
            # 交互框不可见 → 可能是会话超时弹窗挡住了，再确认一次
            await self._check_session_ended(page)
            raise  # 不是超时弹窗 → 原样抛出 timeout

        if self._post_inject_delay > 0:
            await asyncio.sleep(self._post_inject_delay)

    async def wait_for_ready(self, page: Page, timeout: float = 300) -> None:
        if not self._session_started:
            return
        try:
            loc = page.locator(self.SELECTORS["stream_textarea"]).first
            await loc.wait_for(state="visible", timeout=timeout * 1000)
        except PlaywrightTimeout:
            pass

    async def teardown(self, page: Page) -> None:
        """生成完成：先停录屏 → 立刻点 X 关闭当前会话（背靠背，不留空隙）。"""
        await self._recorder_stop(page)
        await self._reset_to_input(page)
        self.stop_monotonic = time.monotonic()

    async def _reset_to_input(self, page: Page) -> None:
        """生成完成后复位到输入框界面（不关浏览器、不整页重载）。

        支持两种复位场景：
        1. 正常结束 → 点右上角 X 关闭当前画面
        2. 会话超时 → 点「Try Again」关闭弹窗回到输入框
        """
        # 已在输入框界面（上一次已复位 / 本次尚未开始）→ 无需点击
        try:
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=2000
            )
            print("[DEBUG] 已在输入框界面，跳过复位点击", file=sys.stderr)
            self._session_started = False
            self.generation_start_monotonic = None
            self.crop_region = None
            return
        except Exception:
            pass

        # 分支 A：会话超时弹窗 → Try Again
        if await self._check_session_ended(page, raise_if_ended=False):
            await self._dismiss_session_ended(page)
            self._session_started = False
            self.generation_start_monotonic = None
            self.crop_region = None
            return

        # 分支 B：正常结束 → 右上角 X 关闭按钮
        clicked = False
        for sel in [
            self.SELECTORS["close_button"],
            "button[aria-label*='close' i]",
            "button[title*='close' i]",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(timeout=5000)
                    clicked = True
                    print(f"[DEBUG] 已点击复位按钮: {sel}", file=sys.stderr)
                    break
            except Exception:
                continue

        if not clicked:
            print("[DEBUG] 未找到右上角 X 按钮（可能已在输入框界面）", file=sys.stderr)

        # 等输入框重新出现，确认复位成功
        try:
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=15000
            )
            print("[DEBUG] 已复位到输入框界面", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] 复位后未检测到输入框（需手动复位）: {e}", file=sys.stderr)

        # 清空内部状态，下一次 run 视为全新会话
        self._session_started = False
        self.generation_start_monotonic = None
        self.crop_region = None
