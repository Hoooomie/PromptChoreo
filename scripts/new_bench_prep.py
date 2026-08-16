"""Convert new_bench progressive/interactive JSON into runner YAML files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


SOURCE_DIR = Path("new_bench")
OUTPUT_DIR = Path("bench_yamls")
TARGET_DURATION_S = 180
INJECTION_INTERVAL_S = 30


def _load_json(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{path} must contain a non-empty JSON array")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"{path} contains a non-object item")
    return payload


def _base_yaml(item: dict[str, Any], initial_prompt: str) -> dict[str, Any]:
    return {
        "metadata": {
            "scenario_id": item["scenario_id"],
            "track": item["track"],
            "selected_attempt": item.get("selected_attempt"),
            "duration_s": TARGET_DURATION_S,
        },
        "recorder": {
            "enabled": True,
            "start_hotkey": "ctrl+f1",
            "stop_hotkey": "ctrl+f2",
        },
        "initial_prompt": initial_prompt,
    }


def progressive_to_yaml(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("track") != "progressive":
        raise ValueError(f"{item.get('scenario_id')}: expected progressive track")
    prompt = item.get("prompts")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{item.get('scenario_id')}: invalid progressive prompt")
    data = _base_yaml(item, prompt.strip())
    data["end_delay"] = TARGET_DURATION_S
    data["events"] = []
    return data


def interactive_to_yaml(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("track") != "interactive":
        raise ValueError(f"{item.get('scenario_id')}: expected interactive track")
    prompts = item.get("prompts")
    if not isinstance(prompts, dict):
        raise ValueError(f"{item.get('scenario_id')}: invalid interactive prompts")
    initial = prompts.get("p0")
    updates = prompts.get("updates")
    if not isinstance(initial, str) or not initial.strip():
        raise ValueError(f"{item.get('scenario_id')}: invalid p0")
    if not isinstance(updates, list) or len(updates) != 5:
        raise ValueError(
            f"{item.get('scenario_id')}: expected exactly 5 updates"
        )

    events = []
    for index, update in enumerate(updates, start=1):
        if not isinstance(update, dict):
            raise ValueError(
                f"{item.get('scenario_id')}: update {index} is not an object"
            )
        prompt = update.get("prompt")
        prompt_id = update.get("prompt_id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                f"{item.get('scenario_id')}: update {index} has no prompt"
            )
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ValueError(
                f"{item.get('scenario_id')}: update {index} has no prompt_id"
            )
        activation_s = index * INJECTION_INTERVAL_S
        events.append(
            {
                "time": activation_s,
                "prompt": prompt.strip(),
                "prompt_id": prompt_id.strip(),
                "role": "update",
                "label": f"t={activation_s}s",
            }
        )

    data = _base_yaml(item, initial.strip())
    data["end_delay"] = TARGET_DURATION_S - events[-1]["time"]
    data["events"] = events
    return data


def materialize(source_dir: Path, output_dir: Path) -> list[Path]:
    progressive = _load_json(source_dir / "progressive.json")
    interactive = _load_json(source_dir / "interactive.json")
    items = [
        *((item, progressive_to_yaml(item), "P") for item in progressive),
        *((item, interactive_to_yaml(item), "I") for item in interactive),
    ]
    scenario_ids = [item[0].get("scenario_id") for item in items]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("new_bench contains duplicate scenario_id values")

    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for item, data, split_prefix in items:
        scenario_id = item["scenario_id"]
        filename = f"{scenario_id}_{split_prefix}-{TARGET_DURATION_S}.yaml"
        path = output_dir / filename
        path.write_text(
            yaml.safe_dump(
                data,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate PromptChoreo YAML for the new 180s benchmark"
    )
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    written = materialize(args.source_dir, args.output_dir)
    progressive_count = sum(path.name.startswith("P-") for path in written)
    interactive_count = sum(path.name.startswith("I-") for path in written)
    print(
        f"Generated {len(written)} YAML files: "
        f"progressive={progressive_count}, interactive={interactive_count}"
    )
    print(f"Output: {args.output_dir}")


if __name__ == "__main__":
    main()
