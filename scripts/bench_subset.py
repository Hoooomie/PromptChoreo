"""Helpers for running the private formal 120-second benchmark subset."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUBSET_JOBS = (
    PROJECT_ROOT
    / "formal_120s_subset_60cases"
    / "formal_120s_subset_60cases"
    / "generation_jobs.jsonl"
)
SUBSET_PHASE = "remain"


def load_subset_jobs(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load and validate the private 120-second JSONL job list."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"subset job file not found: {source}")

    jobs: list[dict[str, Any]] = []
    seen_job_ids: set[str] = set()
    with source.open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                source_job = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid subset JSON on line {line_number}: {exc}"
                ) from exc

            required = {
                "case_id",
                "duration_s",
                "job_id",
                "output_relpath",
                "prompt_schedule",
                "seed",
                "split",
                "track",
            }
            missing = sorted(required - source_job.keys())
            if missing:
                raise ValueError(
                    f"subset job on line {line_number} is missing: "
                    + ", ".join(missing)
                )

            job_id = str(source_job["job_id"])
            if job_id in seen_job_ids:
                raise ValueError(f"duplicate subset job_id: {job_id}")
            if float(source_job["duration_s"]) != 120.0:
                raise ValueError(
                    f"subset job {job_id} has non-120s duration: "
                    f"{source_job['duration_s']!r}"
                )

            prompt_schedule = source_job["prompt_schedule"]
            if not isinstance(prompt_schedule, list) or not prompt_schedule:
                raise ValueError(f"subset job {job_id} has no prompt schedule")
            if not any(
                event.get("role") == "initial" for event in prompt_schedule
            ):
                raise ValueError(f"subset job {job_id} has no initial prompt")

            job = dict(source_job)
            # The recording spec only allows pilot/remain. This formal subset
            # was selected entirely from the remaining queue.
            job["phase"] = SUBSET_PHASE
            jobs.append(job)
            seen_job_ids.add(job_id)

    if not jobs:
        raise ValueError(f"subset job file is empty: {source}")
    return jobs


def job_id_to_yaml_filename(job_id: str) -> str:
    return job_id.replace(":", "_") + ".yaml"


def _job_to_yaml(job: dict[str, Any]) -> dict[str, Any]:
    schedule = job["prompt_schedule"]
    initial = next(event for event in schedule if event.get("role") == "initial")
    updates = [event for event in schedule if event.get("role") == "update"]
    duration_s = float(job["duration_s"])
    last_update_s = max(
        (float(event["activation_media_time_s"]) for event in updates),
        default=0.0,
    )

    data: dict[str, Any] = {
        "recorder": {
            "enabled": True,
            "start_hotkey": "ctrl+f1",
            "stop_hotkey": "ctrl+f2",
        },
        "initial_prompt": initial["text"],
        "end_delay": duration_s - last_update_s if updates else duration_s,
    }
    if updates:
        data["events"] = [
            {
                "time": event["activation_media_time_s"],
                "prompt": event["text"],
                "label": f"t={event['activation_media_time_s']}s",
            }
            for event in updates
        ]
    return data


def prepare_subset_work_items(
    subset_path: str | os.PathLike[str],
    yaml_dir: str | os.PathLike[str],
    job_filter: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Materialize ignored YAML timelines and return selected work items."""
    jobs = load_subset_jobs(subset_path)
    if job_filter:
        jobs = [
            job
            for job in jobs
            if job_filter in job["job_id"]
            or job_filter in job_id_to_yaml_filename(job["job_id"])
        ]

    destination = Path(yaml_dir)
    destination.mkdir(parents=True, exist_ok=True)
    work_items: list[tuple[str, dict[str, Any]]] = []
    for job in jobs:
        filename = job_id_to_yaml_filename(job["job_id"])
        yaml_path = destination / filename
        yaml_path.write_text(
            yaml.safe_dump(
                _job_to_yaml(job),
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        work_items.append((filename, job))
    return work_items


def filter_work_items_by_duration(
    work_items: list[tuple[str, dict[str, Any] | None]],
    duration_s: float | tuple[float, ...] | None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Keep work items whose source job has one of the requested durations."""
    if duration_s is None:
        return work_items

    requested = duration_s if isinstance(duration_s, tuple) else (duration_s,)
    expected = {float(value) for value in requested}
    return [
        (filename, job)
        for filename, job in work_items
        if job is not None
        and float(job.get("duration_s", -1)) in expected
    ]


def subset_output_dir(
    job: dict[str, Any],
    model_id: str,
    project_root: str | os.PathLike[str] = PROJECT_ROOT,
) -> str:
    """Resolve a subset ``output_relpath`` safely inside the repository."""
    raw = str(job["output_relpath"]).replace("<model_id>", model_id)
    relative = PurePosixPath(raw.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts:
        raise ValueError(f"invalid subset output_relpath: {raw!r}")
    if relative.parts[0] != "outputs" or ".." in relative.parts:
        raise ValueError(f"unsafe subset output_relpath: {raw!r}")

    parts = list(relative.parts)
    if os.name == "nt":
        parts = [part.replace(":", "_") for part in parts]
    return str(Path(project_root).joinpath(*parts))
