"""Scheduler 收尾行为测试。"""

import asyncio
import io
from unittest.mock import patch

from rich.console import Console

from promptchoreo.backends.base import Backend
from promptchoreo.core.scheduler import Scheduler
from promptchoreo.core.timeline import PromptEvent, Timeline


class DoneAfterExecuteBackend(Backend):
    def __init__(self) -> None:
        self.done = False
        self.stopped = False

    @property
    def is_done(self) -> bool:
        return self.done

    async def start(self) -> None:
        return None

    async def execute(self, event: PromptEvent) -> None:
        self.done = True

    async def stop(self) -> None:
        self.stopped = True


def test_scheduler_skips_outer_end_delay_when_backend_is_done():
    backend = DoneAfterExecuteBackend()
    timeline = Timeline(
        events=[PromptEvent(time=0, prompt="start")],
        end_delay=10,
    )
    sleeps = []

    async def _sleep_capture(seconds, *a, **k):
        sleeps.append(seconds)

    scheduler = Scheduler(
        timeline,
        backend,
        console=Console(file=io.StringIO()),
    )

    with patch("asyncio.sleep", _sleep_capture):
        asyncio.run(scheduler.run())

    assert 10 not in sleeps
    assert backend.stopped is True
