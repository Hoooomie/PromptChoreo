"""调度器：按时间轴到点触发 prompt 投喂。

使用 asyncio 实现精准定时调度。流式模式下 setup 完成后重置计时起点，
所有事件时间相对于重置点触发。
"""

from __future__ import annotations

import asyncio
import time

from rich.console import Console

from ..backends.base import Backend
from .timeline import PromptEvent, Timeline


class Scheduler:

    def __init__(
        self,
        timeline: Timeline,
        backend: Backend,
        console: Console | None = None,
    ) -> None:
        self.timeline = timeline
        self.backend = backend
        self.console = console or Console()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def run(self) -> None:
        events = self.timeline.sorted_events
        if not events:
            self.console.print("[yellow]时间轴为空，无可执行事件[/]")
            return

        self.console.print(
            f"[bold green]调度器启动[/] — 共 {len(events)} 个事件，"
            f"总时长 {self.timeline.duration:.1f}s"
        )

        await self.backend.start()

        try:
            # 如果适配器在 setup 期间已启动录屏，用录屏实际开始时刻作为调度基准；
            # 否则以 backend.start() 返回后的时刻为基准。
            start = self.backend.recording_start or time.monotonic()

            for i, event in enumerate(events):
                if self._cancelled:
                    self.console.print("[yellow]调度已取消[/]")
                    break

                if getattr(self.backend, "is_done", False):
                    self.console.print(
                        f"[dim]适配器已完成注入，跳过剩余 {len(events) - i} 个事件[/]"
                    )
                    break

                await self._wait_for_event(event, start, i + 1)
                if self._cancelled:
                    break

                await self._execute_event(event, i + 1)

            if not self._cancelled:
                # Some streaming adapters run the full injection loop internally and
                # stop the recorder themselves. In that case, do not wait end_delay
                # a second time before teardown/closing the page.
                if getattr(self.backend, "is_done", False):
                    self.console.print("[dim]适配器已完成收尾，立即关闭[/]")
                    return
                if self.timeline.end_delay > 0:
                    self.console.print(
                        f"[dim]等待 {self.timeline.end_delay:.1f}s 后停止...[/]"
                    )
                    await asyncio.sleep(self.timeline.end_delay)
                self.console.print("[bold green]所有事件执行完毕[/]")
        finally:
            await self.backend.stop()

    async def _wait_for_event(
        self, event: PromptEvent, start: float, index: int
    ) -> None:
        target = start + event.time
        now = time.monotonic()
        wait = target - now

        label_str = f" ({event.label})" if event.label else ""
        prompt_preview = event.prompt[:60]
        if len(event.prompt) > 60:
            prompt_preview += "..."

        if wait > 0:
            self.console.print(
                f"  [{event.time:>7.1f}s] T-{wait:.1f}s  "
                f"事件 #{index}{label_str}: {prompt_preview}"
            )
            await asyncio.sleep(wait)
        elif wait < -0.5:
            self.console.print(
                f"[yellow]  [{event.time:>7.1f}s] 事件 #{index} "
                f"延迟 {-wait:.1f}s[/]"
            )

    async def _execute_event(self, event: PromptEvent, index: int) -> None:
        prompt_preview = event.prompt[:60]
        if len(event.prompt) > 60:
            prompt_preview += "..."

        self.console.print(
            f"  [{event.time:>7.1f}s] [bold cyan]>>> 投喂[/] "
            f"#{index}: {prompt_preview}"
        )

        try:
            await self.backend.execute(event)
        except Exception as exc:
            self.console.print(
                f"[red]  事件 #{index} 执行失败: {exc}[/]"
            )
            raise
