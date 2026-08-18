import json
from types import SimpleNamespace

import scripts.bench_runner as pixverse_runner

from scripts.bench_runner import (
    TARGET_DURATION_S,
    build_prompt_inputs,
    find_archived_job_output,
    find_existing_skip_marker,
    find_job_content_policy_rejection,
    load_completed_job_ids,
    mark_job_completed,
    new_bench_job,
    order_new_bench_files,
    prepare_attempt,
    select_files,
    unmark_job_completed,
    write_content_policy_skip_marker,
    write_failed_manifest,
)
from src.promptchoreo.adapters.pixverse import (
    PixVerseAdapter,
    PixVerseContentPolicyRejection,
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


def test_content_policy_rejection_writes_failure_and_permanent_skip(tmp_path):
    error = PixVerseContentPolicyRejection(
        PixVerseAdapter.CONTENT_POLICY_MESSAGE,
        initial_prompt_time_s=2.4,
    )
    job = {
        "case_id": "P-0044",
        "split": "P-180",
        "phase": "progressive",
        "duration_s": 180,
    }

    write_failed_manifest(
        str(tmp_path),
        "P-0044:P-180",
        job,
        "attempt_001",
        "2026-08-17T01:02:03Z",
        error,
    )
    marker_path = write_content_policy_skip_marker(
        str(tmp_path),
        "P-0044:P-180",
        error,
        recorded_at_utc="2026-08-17T01:02:04Z",
    )

    manifest = json.loads(
        (tmp_path / "run_manifest.json").read_text(encoding="utf-8")
    )
    marker = json.loads(
        (tmp_path / "skip_job.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert manifest["final_video"] is None
    assert manifest["failure_reason"] == error.failure_reason
    assert manifest["timing"]["initial_prompt_time_s"] == 2.4
    assert marker_path == str(tmp_path / "skip_job.json")
    assert marker == {
        "job_id": "P-0044:P-180",
        "model_id": "pixverse_r1",
        "status": "failed",
        "skip_future_runs": True,
        "retryable": False,
        "failure_reason": error.failure_reason,
        "evidence": PixVerseAdapter.CONTENT_POLICY_MESSAGE,
        "recorded_at_utc": "2026-08-17T01:02:04Z",
    }


def test_completed_registry_does_not_depend_on_job_output_folder(tmp_path):
    registry = tmp_path / "state" / "completed_jobs.json"
    job_id = "I-0115:I-180"

    mark_job_completed(
        job_id,
        path=str(registry),
        completed_at_utc="2026-08-17T02:03:04Z",
    )

    assert load_completed_job_ids(str(registry)) == {job_id}
    assert not (tmp_path / "interactive" / "I-0115_I-180").exists()
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["completed_jobs"][job_id] == {
        "status": "success",
        "completed_at_utc": "2026-08-17T02:03:04Z",
    }


def test_completed_registry_preserves_previous_successes(tmp_path):
    registry = tmp_path / "completed_jobs.json"

    mark_job_completed("I-0001:I-180", path=str(registry))
    mark_job_completed("P-0001:P-180", path=str(registry))

    assert load_completed_job_ids(str(registry)) == {
        "I-0001:I-180",
        "P-0001:P-180",
    }


def test_explicit_rerun_can_remove_one_previous_success(tmp_path):
    registry = tmp_path / "completed_jobs.json"
    mark_job_completed("I-0089:I-180", path=str(registry))
    mark_job_completed("I-0119:I-180", path=str(registry))

    assert unmark_job_completed(
        "I-0089:I-180", path=str(registry)
    ) is True
    assert load_completed_job_ids(str(registry)) == {"I-0119:I-180"}
    assert unmark_job_completed(
        "I-0089:I-180", path=str(registry)
    ) is False


def test_multiple_jobs_are_selected_exactly_without_requiring_i_p_pairs():
    requested = [
        "I-0089_I-180",
        "I-0119_I-180",
        "I-0134_I-180",
        "P-0103_P-180",
    ]
    args = SimpleNamespace(
        phase="new",
        job=None,
        jobs=requested,
        shuffle_seed=20260816,
    )

    assert select_files(args) == [f"{name}.yaml" for name in requested]


def test_prepare_attempt_archives_skip_marker_for_explicit_rerun(tmp_path):
    job_dir = tmp_path / "I-0134_I-180"
    job_dir.mkdir()
    (job_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "attempt_id": "attempt_001",
                "status": "failed",
                "failure_reason": "content_policy_rejection: rejected",
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "skip_job.json").write_text("{}", encoding="utf-8")

    assert prepare_attempt(str(job_dir)) == "attempt_002"
    assert (job_dir / "attempt_001" / "run_manifest.json").exists()
    assert (job_dir / "attempt_001" / "skip_job.json").exists()
    assert not (job_dir / "skip_job.json").exists()


def test_skip_marker_is_found_in_manually_archived_run_folder(
    tmp_path, monkeypatch
):
    archived_root = tmp_path / "跑过"
    monkeypatch.setattr(
        pixverse_runner, "ARCHIVED_RUN_ROOT", str(archived_root)
    )
    archived_job = archived_root / "i" / "I-0112_I-180"
    archived_job.mkdir(parents=True)
    marker = archived_job / "skip_job.json"
    marker.write_text("{}", encoding="utf-8")

    active_job = tmp_path / "interactive" / "I-0112_I-180"
    assert find_existing_skip_marker(
        str(active_job), "I-0112:I-180"
    ) == str(marker)


def test_archived_job_folder_alone_marks_pixverse_job_as_run(
    tmp_path, monkeypatch
):
    archived_root = tmp_path / "跑过"
    monkeypatch.setattr(
        pixverse_runner, "ARCHIVED_RUN_ROOT", str(archived_root)
    )
    archived_job = archived_root / "i" / "I-0126_I-180"
    archived_job.mkdir(parents=True)

    assert find_archived_job_output("I-0126:I-180") == str(archived_job)


def test_content_policy_rejection_is_found_in_archived_progressive_job(
    tmp_path, monkeypatch
):
    archived_root = tmp_path / "跑过"
    monkeypatch.setattr(
        pixverse_runner, "ARCHIVED_RUN_ROOT", str(archived_root)
    )
    archived_job = archived_root / "p" / "P-0043_P-180"
    archived_job.mkdir(parents=True)
    (archived_job / "run_manifest.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "failure_reason": "content_policy_rejection: rejected",
            }
        ),
        encoding="utf-8",
    )

    found_dir, reason = find_job_content_policy_rejection(
        str(tmp_path / "progressive" / "P-0043_P-180"),
        "P-0043:P-180",
    )
    assert found_dir == str(archived_job)
    assert reason == "content_policy_rejection: rejected"
