import json

from scripts.bench_runner_happyoyster import (
    prepare_attempt,
    write_failed_manifest,
)


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


def test_site_generation_failure_manifest_has_no_final_video(tmp_path):
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
