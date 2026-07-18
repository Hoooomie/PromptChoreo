"""执行后端抽象基类。

后端负责实际的 prompt 投喂执行。
目前有浏览器自动化后端，后续可扩展 API 后端。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.timeline import PromptEvent


class Backend(ABC):
    """执行后端接口。

    生命周期：start → execute(event) × N → stop
    """

    # 流式模型设为 True：首个事件完成后重置计时起点
    resets_clock: bool = False

    @abstractmethod
    async def start(self) -> None:
        """初始化后端资源（启动浏览器、建立连接等）。"""
        ...

    @abstractmethod
    async def execute(self, event: PromptEvent) -> None:
        """执行单个 prompt 事件的投喂。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """清理后端资源。"""
        ...
