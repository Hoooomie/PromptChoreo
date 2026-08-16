from scripts.bench_runner import (
    TARGET_DURATION_S,
    build_prompt_inputs,
    new_bench_job,
    order_new_bench_files,
)


def test_new_bench_job_maps_manifest_metadata():
    assert new_bench_job("P-0001_P-180.yaml") == {
        "job_id": "P-0001:P-180",
        "case_id": "P-0001",
        "track": "progressive",
        "split": "P-180",
        "phase": "progressive",
        "duration_s": TARGET_DURATION_S,
    }
    assert new_bench_job("I-0001_I-180.yaml")["track"] == "interactive"


def test_new_bench_order_is_reproducible_and_keeps_i_p_pairs():
    files = [
        f"{track}-{index:04d}_{track}-180.yaml"
        for index in range(1, 11)
        for track in ("I", "P")
    ]

    ordered = order_new_bench_files(files, "new", seed=20260816)

    assert ordered == order_new_bench_files(files, "new", seed=20260816)
    assert ordered != files
    assert all(
        ordered[offset].startswith("I-")
        and ordered[offset + 1].startswith("P-")
        and ordered[offset][2:6] == ordered[offset + 1][2:6]
        for offset in range(0, len(ordered), 2)
    )


def test_interactive_yaml_events_are_forwarded_to_pixverse_adapter():
    raw = {
        "initial_prompt": "initial",
        "events": [
            {
                "time": index * 30,
                "prompt": f"update {index}",
                "prompt_id": f"P{index}",
                "role": "update",
            }
            for index in range(1, 6)
        ],
    }

    initial, schedule, events = build_prompt_inputs(
        new_bench_job("I-0001_I-180.yaml"), raw
    )

    assert initial == "initial"
    assert [event["time"] for event in events] == [30, 60, 90, 120, 150]
    assert [event["prompt_id"] for event in events] == [
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
    ]
    assert [prompt["activation_media_time_s"] for prompt in schedule] == [
        0.0,
        30.0,
        60.0,
        90.0,
        120.0,
        150.0,
    ]


def test_legacy_job_schedule_still_takes_precedence_over_yaml_events():
    job = {
        "prompt_schedule": [
            {"role": "initial", "text": "legacy initial"},
            {
                "role": "update",
                "text": "legacy update",
                "activation_media_time_s": 12,
                "prompt_id": "legacy-1",
            },
        ]
    }
    raw = {
        "initial_prompt": "yaml initial",
        "events": [{"time": 30, "prompt": "yaml update"}],
    }

    initial, _, events = build_prompt_inputs(job, raw)

    assert initial == "legacy initial"
    assert events == [
        {
            "time": 12.0,
            "prompt": "legacy update",
            "prompt_id": "legacy-1",
            "role": "update",
        }
    ]
