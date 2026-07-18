"""时间轴数据结构与配置解析。

时间轴是一组 (时间点, prompt) 事件的有序序列，
调度器按时间点依次触发投喂。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PromptEvent:
    """单个 prompt 投喂事件。"""

    time: float          # 触发时间（秒），相对于调度器启动
    prompt: str          # prompt 文本
    label: str = ""      # 可选标签，用于日志标识
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.time < 0:
            raise ValueError(f"事件时间不能为负: time={self.time}")
        if not self.prompt.strip():
            raise ValueError("prompt 不能为空")


@dataclass
class Timeline:
    """时间轴：一组有序的 prompt 事件。"""

    events: list[PromptEvent]
    end_delay: float = 0.0  # 最后一个事件后等待多少秒再停止（如点 Pause）

    @classmethod
    def from_dict(cls, data: dict) -> Timeline:
        """从字典构建时间轴。

        预期格式::

            end_delay: 10  # 可选：最后一个事件后等待秒数
            events:
              - time: 0
                prompt: "一只猫在月光下跳舞"
                label: "开场"
              - time: 12
                prompt: "猫跳上屋顶"
        """
        raw_events = data.get("events", [])
        if not raw_events:
            raise ValueError("时间轴配置中没有 events")

        events: list[PromptEvent] = []
        for i, item in enumerate(raw_events):
            if "time" not in item:
                raise ValueError(f"事件 #{i+1} 缺少 time 字段")
            if "prompt" not in item:
                raise ValueError(f"事件 #{i+1} 缺少 prompt 字段")
            events.append(
                PromptEvent(
                    time=float(item["time"]),
                    prompt=str(item["prompt"]),
                    label=str(item.get("label", "")),
                    metadata=item.get("metadata", {}),
                )
            )
        end_delay = float(data.get("end_delay", 0.0))
        return cls(events=events, end_delay=end_delay)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Timeline:
        """从 YAML 文件加载时间轴。"""
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if data is None:
            raise ValueError(f"YAML 文件为空: {path}")
        return cls.from_dict(data)

    @classmethod
    def from_json(cls, path: str | Path) -> Timeline:
        """从 JSON 文件加载时间轴。"""
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
        return cls.from_dict(data)

    @classmethod
    def from_file(cls, path: str | Path) -> Timeline:
        """根据扩展名自动选择加载器。"""
        p = Path(path)
        if p.suffix in (".yaml", ".yml"):
            return cls.from_yaml(p)
        if p.suffix == ".json":
            return cls.from_json(p)
        raise ValueError(f"不支持的文件格式: {p.suffix}（支持 .yaml/.yml/.json）")

    @property
    def sorted_events(self) -> list[PromptEvent]:
        """按时间排序的事件列表。"""
        return sorted(self.events, key=lambda e: e.time)

    @property
    def duration(self) -> float:
        """时间轴总时长（最后一个事件的时间）。"""
        if not self.events:
            return 0.0
        return max(e.time for e in self.events)

    def __len__(self) -> int:
        return len(self.events)


@dataclass
class VideoSpec:
    """清单中的单个视频条目。"""

    name: str
    site: str | None
    timeline: Timeline
    # 注入到站点适配器 config 的额外字段（initial_prompt / recorder / load_wait 等）
    config_extra: dict = field(default_factory=dict)


@dataclass
class Manifest:
    """播放清单：一组视频，可一次性顺序执行。

    每「视频」自带 prompt 时间轴；站点可逐条指定（site 字段），
    否则回退到清单顶层 site。清单本身与 YAML/JSON 无关——
    同一结构两种格式都能读。
    """

    site: str | None
    videos: list[VideoSpec]

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        if not isinstance(data, dict) or "videos" not in data:
            raise ValueError("manifest 必须包含 videos 列表")
        raw_videos = data["videos"]
        if not isinstance(raw_videos, list) or not raw_videos:
            raise ValueError("videos 必须是非空列表")
        default_site = data.get("site")

        videos: list[VideoSpec] = []
        for i, v in enumerate(raw_videos):
            if not isinstance(v, dict):
                raise ValueError(f"videos[#{i}] 必须是对象")
            name = str(v.get("name") or f"video{i + 1}")
            site = v.get("site") or default_site
            if not site:
                raise ValueError(f"videos[{name}] 缺少 site（清单顶层也未指定）")

            tl = Timeline.from_dict(
                {
                    "events": v.get("events", []),
                    "end_delay": v.get("end_delay", 0.0),
                }
            )

            extra: dict = {}
            if "initial_prompt" in v:
                extra["initial_prompt"] = str(v["initial_prompt"])
                extra["_inject_events"] = v.get("events", [])
                extra["_end_delay"] = float(v.get("end_delay", 0.0))
            if "load_wait" in v:
                extra["load_wait"] = float(v["load_wait"])
            # 生成加载等待上限（Happy Oyster 加载很慢，需放宽；默认 600s，可逐条覆盖）
            if "max_load_wait" in v:
                extra["max_load_wait"] = float(v["max_load_wait"])
            elif data.get("max_load_wait") is not None:
                extra["max_load_wait"] = float(data["max_load_wait"])
            rec = v.get("recorder")
            if isinstance(rec, dict) and rec.get("enabled"):
                extra["_recorder_enabled"] = True
                extra["_recorder_start_hotkey"] = rec.get("start_hotkey", "ctrl+f1")
                extra["_recorder_stop_hotkey"] = rec.get("stop_hotkey", "ctrl+f2")
            else:
                extra.setdefault("_recorder_enabled", False)

            videos.append(
                VideoSpec(name=name, site=str(site), timeline=tl, config_extra=extra)
            )
        return cls(site=default_site, videos=videos)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Manifest":
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if data is None:
            raise ValueError(f"YAML 文件为空: {path}")
        return cls.from_dict(data)

    @classmethod
    def from_json(cls, path: str | Path) -> "Manifest":
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
        return cls.from_dict(data)

    @classmethod
    def from_file(cls, path: str | Path) -> "Manifest":
        """根据扩展名自动选择加载器（.json / .yaml / .yml）。"""
        p = Path(path)
        if p.suffix in (".yaml", ".yml"):
            return cls.from_yaml(p)
        if p.suffix == ".json":
            return cls.from_json(p)
        raise ValueError(f"不支持的文件格式: {p.suffix}（支持 .yaml/.yml/.json）")
