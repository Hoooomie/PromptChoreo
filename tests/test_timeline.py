"""Timeline 基础测试。"""

import pytest

from promptchoreo.core.timeline import PromptEvent, Timeline


def test_prompt_event_creation():
    event = PromptEvent(time=0, prompt="test prompt")
    assert event.time == 0
    assert event.prompt == "test prompt"
    assert event.label == ""


def test_prompt_event_negative_time():
    with pytest.raises(ValueError, match="不能为负"):
        PromptEvent(time=-1, prompt="test")


def test_prompt_event_empty_prompt():
    with pytest.raises(ValueError, match="不能为空"):
        PromptEvent(time=0, prompt="   ")


def test_timeline_from_dict():
    data = {
        "events": [
            {"time": 0, "prompt": "first", "label": "A"},
            {"time": 12, "prompt": "second"},
        ]
    }
    tl = Timeline.from_dict(data)
    assert len(tl) == 2
    assert tl.duration == 12


def test_timeline_sorted():
    data = {
        "events": [
            {"time": 24, "prompt": "c"},
            {"time": 0, "prompt": "a"},
            {"time": 12, "prompt": "b"},
        ]
    }
    tl = Timeline.from_dict(data)
    sorted_times = [e.time for e in tl.sorted_events]
    assert sorted_times == [0, 12, 24]


def test_timeline_empty_events():
    with pytest.raises(ValueError, match="没有 events"):
        Timeline.from_dict({"events": []})


def test_timeline_missing_time():
    with pytest.raises(ValueError, match="缺少 time"):
        Timeline.from_dict({"events": [{"prompt": "test"}]})
