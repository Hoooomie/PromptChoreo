"""Manifest（多视频清单）解析测试。"""

import json

from promptchoreo.core.timeline import Manifest


def _sample() -> dict:
    return {
        "site": "odyssey",
        "videos": [
            {
                "name": "a",
                "site": "happy_oyster",
                "initial_prompt": "x",
                "end_delay": 5,
                "recorder": {"enabled": True, "start_hotkey": "ctrl+f1", "stop_hotkey": "ctrl+f2"},
                "events": [{"time": 10, "prompt": "p1"}, {"time": 20, "prompt": "p2"}],
            },
            {
                "name": "b",
                "initial_prompt": "y",
                "events": [{"time": 0, "prompt": "q"}],
            },
        ],
    }


def test_from_dict_basic():
    m = Manifest.from_dict(_sample())
    assert m.site == "odyssey"
    assert len(m.videos) == 2
    # 第二条未指定 site，回退到清单顶层 site
    assert m.videos[0].site == "happy_oyster"
    assert m.videos[1].site == "odyssey"
    assert len(m.videos[0].timeline) == 2
    assert m.videos[0].timeline.end_delay == 5


def test_config_extra_processed():
    m = Manifest.from_dict(_sample())
    extra = m.videos[0].config_extra
    assert extra["initial_prompt"] == "x"
    assert extra["_recorder_enabled"] is True
    assert extra["_recorder_start_hotkey"] == "ctrl+f1"
    # 有 recorder 块 -> 注入事件一并带出
    assert extra["_inject_events"] == [{"time": 10, "prompt": "p1"}, {"time": 20, "prompt": "p2"}]


def test_missing_videos_raises():
    import pytest

    with pytest.raises(ValueError):
        Manifest.from_dict({"site": "odyssey"})


def test_missing_site_raises():
    import pytest

    with pytest.raises(ValueError):
        Manifest.from_dict({"videos": [{"name": "a", "events": []}]})


def test_from_json_roundtrip(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(_sample()), encoding="utf-8")
    m = Manifest.from_file(p)
    assert len(m.videos) == 2
    assert m.videos[1].site == "odyssey"
