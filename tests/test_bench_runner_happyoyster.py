import asyncio
import json
from types import SimpleNamespace

import scripts.bench_runner_happyoyster as happyoyster_runner

from scripts.bench_runner_happyoyster import (
    ADAPTER_CLASS,
    DEFAULT_ACCOUNTS_JSON,
    MODEL_ID,
    TARGET_DURATION_S,
    TRACK_B_INJECTION_INTERVAL_S,
    build_run_plan,
    clear_browser_identity,
    find_archived_job_output,
    find_existing_skip_marker,
    find_nonretryable_failure_reason,
    finalize_error_recording,
    load_completed_job_ids,
    manifest_matches_current_policy,
    mark_job_completed,
    is_acceptable_recording_resolution,
    new_bench_job,
    order_new_bench_files,
    prepare_attempt,
    select_files,
    select_work_items,
    unmark_job_completed,
    write_failed_manifest,
)
from src.promptchoreo.adapters.happy_oyster_global import (
    HappyOysterGlobalAdapter,
)


def test_runner_defaults_to_international_site():
    assert ADAPTER_CLASS is HappyOysterGlobalAdapter
    assert MODEL_ID == "happyoyster_global"
    assert DEFAULT_ACCOUNTS_JSON == "happyoyster_accounts.json"


def test_completed_registry_does_not_depend_on_job_output_folder(tmp_path):
    registry = tmp_path / "state" / "completed_jobs.json"
    job_id = "I-0115:I-180"

    mark_job_completed(
        job_id,
        path=str(registry),
        completed_at_utc="2026-08-17T03:04:05Z",
    )

    assert load_completed_job_ids(str(registry)) == {job_id}
    assert not (tmp_path / "interactive" / "I-0115_I-180").exists()
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["completed_jobs"][job_id] == {
        "status": "success",
        "completed_at_utc": "2026-08-17T03:04:05Z",
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
                "failure_reason": "site_generation_nonretryable: rejected",
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "skip_job.json").write_text("{}", encoding="utf-8")

    attempt_id, _ = prepare_attempt(str(job_dir))

    assert attempt_id == "attempt_002"
    assert (job_dir / "attempt_001" / "run_manifest.json").exists()
    assert (job_dir / "attempt_001" / "skip_job.json").exists()
    assert not (job_dir / "skip_job.json").exists()


def test_skip_marker_is_found_in_manually_archived_run_folder(
    tmp_path, monkeypatch
):
    archived_root = tmp_path / "跑过"
    monkeypatch.setattr(
        happyoyster_runner, "ARCHIVED_RUN_ROOT", str(archived_root)
    )
    archived_job = archived_root / "i" / "I-0112_I-180"
    archived_job.mkdir(parents=True)
    marker = archived_job / "skip_job.json"
    marker.write_text("{}", encoding="utf-8")

    active_job = tmp_path / "interactive" / "I-0112_I-180"
    assert find_existing_skip_marker(
        str(active_job), "I-0112:I-180"
    ) == str(marker)


def test_archived_job_folder_alone_marks_happyoyster_job_as_run(
    tmp_path, monkeypatch
):
    archived_root = tmp_path / "跑过"
    monkeypatch.setattr(
        happyoyster_runner, "ARCHIVED_RUN_ROOT", str(archived_root)
    )
    archived_job = archived_root / "i" / "I-0126_I-180"
    archived_job.mkdir(parents=True)

    assert find_archived_job_output("I-0126:I-180") == str(archived_job)


def test_progressive_archive_uses_lowercase_p_folder(tmp_path, monkeypatch):
    archived_root = tmp_path / "跑过"
    monkeypatch.setattr(
        happyoyster_runner, "ARCHIVED_RUN_ROOT", str(archived_root)
    )
    archived_job = archived_root / "p" / "P-0025_P-180"
    archived_job.mkdir(parents=True)
    marker = archived_job / "skip_job.json"
    marker.write_text("{}", encoding="utf-8")

    assert find_existing_skip_marker(
        str(tmp_path / "progressive" / "P-0025_P-180"),
        "P-0025:P-180",
    ) == str(marker)


def test_recording_resolution_accepts_browser_border_delta():
    assert is_acceptable_recording_resolution(
        {"width": 2560, "height": 1440}
    )
    assert is_acceptable_recording_resolution(
        {"width": 2544, "height": 1432}
    )
    assert not is_acceptable_recording_resolution(
        {"width": 1920, "height": 1080}
    )


def test_failure_recording_is_copied_into_job_directory(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "raw"
    source_dir.mkdir()
    source = source_dir / "capture.webm"
    source.write_bytes(b"raw-video")
    output_dir = tmp_path / "job"

    def fake_normalize(_ffmpeg, src, dst, duration_s):
        assert src == str(source)
        assert duration_s == 0
        with open(dst, "wb") as f:
            f.write(b"normalized-error-video")
        return True

    monkeypatch.setattr(happyoyster_runner, "VIDEO_SRC", str(source_dir))
    monkeypatch.setattr(
        happyoyster_runner, "normalize_full_frame", fake_normalize
    )

    result = finalize_error_recording(str(output_dir))

    assert result == str(output_dir / "error_recording.mp4")
    assert (output_dir / "error_recording.mp4").read_bytes() == (
        b"normalized-error-video"
    )


def test_new_bench_job_maps_new_track_metadata():
    progressive = new_bench_job("P-0001_P-180.yaml")
    interactive = new_bench_job("I-0001_I-180.yaml")

    assert progressive == {
        "job_id": "P-0001:P-180",
        "case_id": "P-0001",
        "track": "progressive",
        "split": "P-180",
        "phase": "progressive",
        "duration_s": 180,
    }
    assert interactive["track"] == "interactive"
    assert interactive["phase"] == "interactive"


def test_new_bench_order_is_shuffled_but_keeps_matching_i_p_pairs():
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


def test_single_new_track_uses_reproducible_shuffle():
    files = [f"P-{index:04d}_P-180.yaml" for index in range(1, 11)]

    first = order_new_bench_files(files, "progressive", seed=9)
    second = order_new_bench_files(files, "progressive", seed=9)

    assert first == second
    assert first != files


def test_clear_browser_identity_removes_cookies_then_opens_blank_page():
    calls = []

    class Context:
        async def clear_cookies(self):
            calls.append("clear_cookies")

    class Page:
        async def goto(self, url, **kwargs):
            calls.append(("goto", url, kwargs))

    asyncio.run(clear_browser_identity(Context(), Page()))

    assert calls == [
        "clear_cookies",
        ("goto", "about:blank", {"wait_until": "domcontentloaded"}),
    ]


def test_track_a_runs_for_three_minutes_without_injections():
    job = {
        "track": "A",
        "prompt_schedule": [
            {"role": "initial", "text": "Initial A", "prompt_id": "a0"}
        ],
    }

    plan = build_run_plan(job, {}, "TEST-A:A-120")

    assert plan["target_duration_s"] == TARGET_DURATION_S == 180.0
    assert plan["end_delay_s"] == 180.0
    assert plan["inject_events"] == []


def test_track_b_uses_five_30_second_injections_in_three_minutes():
    schedule = [
        {"role": "initial", "text": "Initial B", "prompt_id": "b0"}
    ] + [
        {
            "role": "update",
            "text": f"Update {index}",
            "prompt_id": f"b{index}",
            "activation_media_time_s": index * 10,
        }
        for index in range(1, 12)
    ]
    job = {"track": "B", "prompt_schedule": schedule}

    plan = build_run_plan(job, {}, "TEST-B:B-120")

    assert TRACK_B_INJECTION_INTERVAL_S == 30.0
    assert [event["time"] for event in plan["inject_events"]] == [
        30.0,
        60.0,
        90.0,
        120.0,
        150.0,
    ]
    assert [event["prompt_id"] for event in plan["inject_events"]] == [
        "b1",
        "b2",
        "b3",
        "b4",
        "b5",
    ]
    assert plan["end_delay_s"] == 30.0
    assert plan["target_duration_s"] == 180.0


def test_only_current_180_second_results_are_skipped():
    assert manifest_matches_current_policy(
        {"status": "success", "target_duration_s": 180}
    )
    assert not manifest_matches_current_policy(
        {"status": "success", "target_duration_s": 120}
    )
    assert not manifest_matches_current_policy(
        {"status": "failed", "target_duration_s": 180}
    )


def test_test_phase_uses_three_pilot_jobs_as_180_second_tasks():
    args = SimpleNamespace(
        phase="test",
        subset=None,
        job=None,
        duration_filter=180,
    )

    items = select_work_items(args)

    assert len(items) == 3
    assert [job["track"] for _, job in items] == ["B", "A", "A"]
    assert [job["duration_s"] for _, job in items] == [180, 180, 180]
    assert [job["phase"] for _, job in items] == ["test", "test", "test"]
    assert [job["job_id"] for _, job in items] == [
        "B-0003:B-180",
        "A-0001:A-180",
        "A-0002:A-180",
    ]


def test_prepare_attempt_archives_site_failure_evidence(tmp_path):
    job_dir = tmp_path / "TEST-CASE_A-120"
    job_dir.mkdir()
    (job_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "attempt_id": "attempt_001",
                "status": "failed",
                "failure_reason": "site_generation_failed: test",
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "error_recording.mp4").write_bytes(b"error")
    (job_dir / "error_screenshot.png").write_bytes(b"shot")

    attempt_id, retry_reason = prepare_attempt(str(job_dir))

    assert attempt_id == "attempt_002"
    assert retry_reason == "retry_after_site_generation_error"
    assert (job_dir / "attempt_001" / "run_manifest.json").exists()
    assert (job_dir / "attempt_001" / "error_recording.mp4").exists()
    assert (job_dir / "attempt_001" / "error_screenshot.png").exists()


def test_site_generation_failure_manifest_has_no_final_video(
    tmp_path, monkeypatch
):
    empty_raw = tmp_path / "raw"
    empty_raw.mkdir()
    monkeypatch.setattr(happyoyster_runner, "VIDEO_SRC", str(empty_raw))
    error = RuntimeError(
        "site_generation_failed: HappyOyster displayed an error"
    )
    error.prompt_events = [
        {
            "prompt_id": "TEST-CASE:prompt:00",
            "role": "initial",
            "scheduled_media_time_s": 0.0,
            "actual_media_time_s": 0.0,
            "actual_injection_time_s": 10.2,
            "status": "accepted",
            "error": None,
        }
    ]
    error.timing = {
        "initial_prompt_time_s": 10.2,
        "first_video_chunk_time_s": None,
        "generation_complete_time_s": None,
    }

    write_failed_manifest(
        str(tmp_path),
        "TEST-CASE:A-120",
        {
            "case_id": "TEST-CASE",
            "split": "A-120",
            "phase": "pilot",
            "duration_s": 120,
        },
        "attempt_002",
        "2026-07-25T08:00:00Z",
        "retry_after_site_generation_error",
        error,
    )

    manifest = json.loads(
        (tmp_path / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert manifest["final_video"] is None
    assert manifest["actual_duration_s"] is None
    assert manifest["settings"]["resolution"] is None
    assert manifest["failure_reason"].startswith("site_generation_failed:")
    assert manifest["retry_reason"] == "retry_after_site_generation_error"
    assert manifest["target_duration_s"] == 180.0


def test_playback_unavailable_writes_nonretryable_skip_marker(
    tmp_path, monkeypatch
):
    empty_raw = tmp_path / "raw"
    empty_raw.mkdir()
    monkeypatch.setattr(happyoyster_runner, "VIDEO_SRC", str(empty_raw))
    error = RuntimeError(
        "site_playback_unavailable: HappyOyster displayed \"This scene "
        "can't be played right now\"; only the first-frame preview was "
        "available"
    )
    error.prompt_events = []
    error.timing = {}

    write_failed_manifest(
        str(tmp_path),
        "I-0146:I-180",
        {
            "case_id": "I-0146",
            "split": "I-180",
            "phase": "interactive",
            "duration_s": 180,
        },
        "attempt_002",
        "2026-08-16T03:02:33Z",
        "retry_after_previous_failed_attempt",
        error,
    )

    manifest = json.loads(
        (tmp_path / "run_manifest.json").read_text(encoding="utf-8")
    )
    skip = json.loads(
        (tmp_path / "skip_job.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert manifest["final_video"] is None
    assert manifest["failure_reason"].startswith(
        "site_playback_unavailable:"
    )
    assert skip["skip_future_runs"] is True
    assert skip["retryable"] is False


def test_oops_writes_nonretryable_skip_marker(tmp_path, monkeypatch):
    empty_raw = tmp_path / "raw"
    empty_raw.mkdir()
    monkeypatch.setattr(happyoyster_runner, "VIDEO_SRC", str(empty_raw))
    error = RuntimeError(
        "site_generation_nonretryable: HappyOyster displayed "
        "'Oops / Something went wrong' and produced no valid video"
    )
    error.prompt_events = []
    error.timing = {}

    write_failed_manifest(
        str(tmp_path),
        "P-0040:P-180",
        {
            "case_id": "P-0040",
            "split": "P-180",
            "phase": "progressive",
            "duration_s": 180,
        },
        "attempt_001",
        "2026-08-16T08:15:06Z",
        None,
        error,
    )

    skip = json.loads(
        (tmp_path / "skip_job.json").read_text(encoding="utf-8")
    )
    assert skip["skip_future_runs"] is True
    assert skip["retryable"] is False
    assert skip["evidence"] == "Oops: Something went wrong"


def test_legacy_oops_in_archived_attempt_is_still_skipped(tmp_path):
    attempt = tmp_path / "attempt_001"
    attempt.mkdir()
    (attempt / "run_manifest.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "failure_reason": (
                    "site_generation_failed: HappyOyster displayed "
                    "'Oops / Something went wrong' and produced no valid "
                    "video"
                ),
            }
        ),
        encoding="utf-8",
    )

    reason = find_nonretryable_failure_reason(str(tmp_path))

    assert reason is not None
    assert "Oops / Something went wrong" in reason
