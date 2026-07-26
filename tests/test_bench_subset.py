import json
import os
from pathlib import Path

import yaml

from scripts.bench_subset import (
    filter_work_items_by_duration,
    load_subset_jobs,
    prepare_subset_work_items,
    subset_output_dir,
)


def _job(job_id, track, schedule):
    split = f"{track}-120"
    return {
        "case_id": job_id.split(":", 1)[0],
        "duration_s": 120,
        "job_id": job_id,
        "output_relpath": f"outputs/<model_id>/42/{job_id}",
        "prompt_schedule": schedule,
        "seed": 42,
        "source_stage4_sha256": "0" * 64,
        "split": split,
        "track": track,
    }


def test_subset_jobs_materialize_yaml_and_output_paths(tmp_path):
    jobs = [
        _job(
            "TEST-A:A-120",
            "A",
            [
                {
                    "activation_media_time_s": 0,
                    "prompt_id": "TEST-A:prompt:00",
                    "role": "initial",
                    "segment_index": 0,
                    "text": "Initial A",
                }
            ],
        ),
        _job(
            "TEST-B:B-120",
            "B",
            [
                {
                    "activation_media_time_s": 0,
                    "prompt_id": "TEST-B:prompt:00",
                    "role": "initial",
                    "segment_index": 0,
                    "text": "Initial B",
                },
                {
                    "activation_media_time_s": 110,
                    "prompt_id": "TEST-B:prompt:11",
                    "role": "update",
                    "segment_index": 11,
                    "text": "Update B",
                },
            ],
        ),
    ]
    source = tmp_path / "jobs.jsonl"
    source.write_text(
        "\n".join(json.dumps(job) for job in jobs) + "\n",
        encoding="utf-8",
    )

    loaded = load_subset_jobs(source)
    assert [job["phase"] for job in loaded] == ["remain", "remain"]

    yaml_dir = tmp_path / "yaml"
    items = prepare_subset_work_items(source, yaml_dir, "TEST-B")
    assert [filename for filename, _ in items] == ["TEST-B_B-120.yaml"]
    timeline = yaml.safe_load(
        (yaml_dir / "TEST-B_B-120.yaml").read_text(encoding="utf-8")
    )
    assert timeline["initial_prompt"] == "Initial B"
    assert timeline["end_delay"] == 10.0
    assert timeline["events"][0]["time"] == 110

    output = Path(
        subset_output_dir(loaded[1], "example_model", project_root=tmp_path)
    )
    expected_name = "TEST-B_B-120" if os.name == "nt" else "TEST-B:B-120"
    assert output == tmp_path / "outputs" / "example_model" / "42" / expected_name


def test_filter_work_items_by_duration():
    work_items = [
        ("A_A-30.yaml", {"duration_s": 30}),
        ("A_A-60.yaml", {"duration_s": 60}),
        ("A_A-120.yaml", {"duration_s": 120}),
        ("missing.yaml", None),
    ]

    assert filter_work_items_by_duration(work_items, None) is work_items
    assert filter_work_items_by_duration(work_items, 30) == [
        ("A_A-30.yaml", {"duration_s": 30})
    ]
    assert filter_work_items_by_duration(work_items, 60) == [
        ("A_A-60.yaml", {"duration_s": 60})
    ]
    assert filter_work_items_by_duration(work_items, 120) == [
        ("A_A-120.yaml", {"duration_s": 120})
    ]
