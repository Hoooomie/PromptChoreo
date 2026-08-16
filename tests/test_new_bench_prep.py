import json

import yaml

from scripts.new_bench_prep import (
    interactive_to_yaml,
    materialize,
    progressive_to_yaml,
)


def test_progressive_yaml_has_no_injections():
    data = progressive_to_yaml(
        {
            "scenario_id": "P-0001",
            "track": "progressive",
            "selected_attempt": 1,
            "prompts": "initial progressive prompt",
        }
    )

    assert data["initial_prompt"] == "initial progressive prompt"
    assert data["end_delay"] == 180
    assert data["events"] == []


def test_interactive_yaml_injects_five_prompts_every_30_seconds():
    data = interactive_to_yaml(
        {
            "scenario_id": "I-0001",
            "track": "interactive",
            "selected_attempt": 2,
            "prompts": {
                "p0": "initial interactive prompt",
                "updates": [
                    {"prompt_id": f"u{index}", "prompt": f"update {index}"}
                    for index in range(1, 6)
                ],
            },
        }
    )

    assert data["initial_prompt"] == "initial interactive prompt"
    assert [event["time"] for event in data["events"]] == [
        30,
        60,
        90,
        120,
        150,
    ]
    assert [event["prompt_id"] for event in data["events"]] == [
        "u1",
        "u2",
        "u3",
        "u4",
        "u5",
    ]
    assert data["end_delay"] == 30


def test_materialize_writes_runner_compatible_yaml(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    (source / "progressive.json").write_text(
        json.dumps(
            [
                {
                    "scenario_id": "P-0001",
                    "track": "progressive",
                    "selected_attempt": 1,
                    "prompts": "progressive",
                }
            ]
        ),
        encoding="utf-8",
    )
    (source / "interactive.json").write_text(
        json.dumps(
            [
                {
                    "scenario_id": "I-0001",
                    "track": "interactive",
                    "selected_attempt": 1,
                    "prompts": {
                        "p0": "interactive",
                        "updates": [
                            {
                                "prompt_id": f"u{index}",
                                "prompt": f"update {index}",
                            }
                            for index in range(1, 6)
                        ],
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    written = materialize(source, output)

    assert [path.name for path in written] == [
        "P-0001_P-180.yaml",
        "I-0001_I-180.yaml",
    ]
    interactive = yaml.safe_load(
        (output / "I-0001_I-180.yaml").read_text(encoding="utf-8")
    )
    assert interactive["events"][-1]["time"] == 150
