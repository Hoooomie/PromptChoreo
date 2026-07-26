"""Playwright 浏览器自动化后端。

通过 Playwright 控制浏览器，委托 SiteAdapter 完成具体的页面操作。
支持 persistent context（复用登录态）和自动视频录制。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from rich.console import Console

from ..adapters.base import SiteAdapter
from ..core.timeline import PromptEvent
from .base import Backend


def _get_screen_size() -> dict | None:
    """检测主屏分辨率（CSS 像素）。

    用于 kiosk 全屏时让 viewport 匹配屏幕，使 EV 录到原生分辨率而不是被 viewport 限制。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        h = user32.GetSystemMetrics(1)  # SM_CYSCREEN
        if w and h:
            return {"width": int(w), "height": int(h)}
    except Exception:
        pass
    return None


class BrowserBackend(Backend):
    """浏览器自动化后端。"""

    def __init__(
        self,
        adapter: SiteAdapter,
        *,
        headless: bool = False,
        slow_mo: int = 0,
        user_data_dir: str | None = None,
        record_video_dir: str | None = None,
        console: Console | None = None,
        channel: str | None = None,
        viewport: dict | None = None,
        cdp_url: str | None = None,
        mute: bool = False,
    ) -> None:
        self.adapter = adapter
        self.headless = headless
        self.slow_mo = slow_mo
        self.user_data_dir = user_data_dir or getattr(adapter, "user_data_dir", None)
        self.record_video_dir = record_video_dir
        self.console = console or Console()
        self.channel = channel
        self.viewport = viewport or {"width": 2560, "height": 1440}
        # mute=False 默认：用户希望听到视频原声（仅配乐通过界面 🎵 关闭）。
        # 仅当显式 mute=True 时才在浏览器层 --mute-audio 彻底静音。
        self.mute = mute
        # 连接模式：连上用户手动打开的 Chrome for Testing，工具不再自己启动浏览器
        self._cdp_url = cdp_url
        self.resets_clock = getattr(adapter, "resets_clock", False)
        self._video_start_monotonic: float | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def recording_start(self) -> float | None:
        """适配器发送录屏开始热键后的时间（monotonic）。"""
        return (
            getattr(self.adapter, "recording_start_monotonic", None)
            or getattr(self.adapter, "generation_start_monotonic", None)
        )

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        # —— 连接模式：连上用户手动打开的 Chrome for Testing ——
        # 全屏 / 窗口大小由用户手动控制，录屏只走外部 EV（不依赖 Playwright 录像）。
        if self._cdp_url:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self._cdp_url
                )
            except Exception as exc:
                raise RuntimeError(
                    f"无法连接 CDP 地址 {self._cdp_url!r}：{exc}\n"
                    f"请确认：\n"
                    f"  1) 已用 scripts/launch_chrome_for_testing.py 打开 Chrome for Testing；\n"
                    f"  2) --cdp 地址/端口正确（启动脚本默认 http://127.0.0.1:9222）；\n"
                    f"  3) 端口没写少（常见笔误 922 → 应为 9222）。"
                ) from exc
            # 复用已存在的默认 context（它挂着 user-data-dir 的登录态），
            # 绝不新建独立 context（那样会丢掉 cookie，重新变成未登录）。
            if self._browser.contexts:
                self._context = self._browser.contexts[0]
            else:
                self._context = await self._browser.new_context(no_viewport=True)
            # 复用已存在的标签页（launch 脚本已开好并停在输入框界面），
            # 不新建——这样 teardown 的「点 X 复位到输入框」能跨 run 保留，
            # 下一个 run 直接在同一标签页上重开会话，无需整页重载、也不会丢全屏。
            self._page = await self._reuse_or_new_page()

            # 连接模式站点一致性保护：用户手动开的浏览器只登录了一个站点，
            # 不能跨站 goto 抢走标签页。若当前标签页不在本任务站点，明确报错而不是静默跳转。
            from urllib.parse import urlparse as _urlparse
            _expected_host = _urlparse(getattr(self.adapter, "URL", "") or "").netloc
            _current_host = _urlparse(self._page.url or "").netloc
            if _expected_host and _current_host and _expected_host != _current_host:
                _site_name = getattr(self.adapter, "name", "?")
                raise RuntimeError(
                    f"连接模式站点不匹配：你手动开的 Chrome 当前停在 {self._page.url}，"
                    f"但本次任务要跑的是 {_site_name}（应位于 {_expected_host}）。\n"
                    f"连接模式不会跨站跳转，以免抢走你的标签页。请先关闭当前浏览器，"
                    f"用 scripts/launch_chrome_for_testing.py --site {_site_name} "
                    f"重新打开正确站点后再连。"
                )

            self.console.print(
                f"[dim]已连接外部浏览器 (CDP {self._cdp_url})[/]"
            )
            self._video_start_monotonic = time.monotonic()
            await self.adapter.setup(self._page)
            self.console.print("[dim]站点适配器初始化完成[/]")
            return

        # 不覆盖——用 CLI 传入的 viewport 或默认 2560x1440

        video_kwargs = {}
        if self.record_video_dir:
            os.makedirs(self.record_video_dir, exist_ok=True)
            video_kwargs = {
                "record_video_dir": self.record_video_dir,
                "record_video_size": self.viewport,
            }

        if self.user_data_dir:
            self._context = await self._playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                headless=self.headless,
                slow_mo=self.slow_mo,
                viewport=self.viewport,
                channel=self.channel,
                args=["--no-restore", "--use-fake-ui-for-media-stream", "--start-maximized", *(["--mute-audio"] if self.mute else [])],
                **video_kwargs,
            )
            # 显式创建新页面，避免复用浏览器恢复的旧标签
            self._page = await self._context.new_page()
            self.console.print(
                f"[dim]浏览器已启动 (persistent, headless={self.headless})[/]"
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                channel=self.channel,
                args=["--use-fake-ui-for-media-stream", "--start-maximized", *(["--mute-audio"] if self.mute else [])],
            )
            self._context = await self._browser.new_context(
                viewport=self.viewport, **video_kwargs
            )
            self._page = await self._context.new_page()
            self.console.print(
                f"[dim]浏览器已启动 (headless={self.headless})[/]"
            )

        self._video_start_monotonic = time.monotonic()

        if self.record_video_dir:
            self.console.print(f"[dim]视频录制中 → {self.record_video_dir}[/]")

        await self.adapter.setup(self._page)
        self.console.print("[dim]站点适配器初始化完成[/]")

    async def _reuse_or_new_page(self) -> Any:
        """CDP 模式下挑一个已存在的标签页复用：优先 URL 匹配当前站点，否则用最旧的。

        返回已存在的 Page；若没有任何标签页则新建一个。
        """
        from urllib.parse import urlparse

        if not self._context or not self._context.pages:
            return await self._context.new_page()
        host = urlparse(getattr(self.adapter, "URL", "") or "").netloc
        for p in self._context.pages:
            try:
                if host and host in (p.url or ""):
                    return p
            except Exception:
                continue
        # 没匹配到就退而求其次用第一个（通常是 launch 开的那一个）
        return self._context.pages[0]

    async def execute(self, event: PromptEvent) -> None:
        if self._page is None:
            raise RuntimeError("后端未启动，请先调用 start()")

        await self.adapter.wait_for_ready(self._page)
        await self.adapter.submit_prompt(
            self._page, event.prompt, target_time=event.time
        )

    @property
    def is_done(self) -> bool:
        return bool(getattr(self.adapter, "is_done", False))

    async def stop(self) -> None:
        video_path = None
        if not self._cdp_url and self._page and self._page.video:
            try:
                video_path = await self._page.video.path()
            except Exception:
                pass

        if self._page and self.adapter:
            try:
                await self.adapter.teardown(self._page)
            except Exception:
                pass

        if self._cdp_url:
            # 连接模式：保留用户的标签页（teardown 已点 X 复位到输入框，
            # 留给下一个 run 复用），绝不关 page / context / 浏览器——
            # 只断开本地 Playwright 驱动，远程 Chrome for Testing 继续运行。
            pass
        else:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

        # 原始录屏已由 Playwright 直接写入 record_video_dir。
        # 空间裁剪 / 时间裁剪交给独立的 scripts/trim_*.py 处理，按站点分开。
        if video_path:
            self.console.print(f"[bold green]原始录屏已保存: {video_path}[/]")
        if self._cdp_url:
            # 连接模式：仅断开本地 Playwright 驱动，远程 Chrome for Testing 继续运行，
            # 标签页留给下一个 run 复用（teardown 已复位到输入框）。
            self.console.print("[dim]CDP 连接已断开（浏览器保持运行，标签页已复位）[/]")
        else:
            self.console.print("[dim]浏览器已关闭[/]")
