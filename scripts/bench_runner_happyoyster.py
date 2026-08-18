"""StreamAVBench batch runner for Happy Oyster international Directing Mode.

The browser process is reused across jobs. Each account may complete one
video; every job still logs out before the next login. Each successful job:

1. runs through ``HappyOysterGlobalAdapter``;
2. records the full 2560x1440 browser frame with the YAML hotkeys;
3. normalizes the full frame to MP4 without spatial cropping;
4. writes a spec-compatible manifest and event files.

Happy Oyster runs use a single 180-second policy. Track B keeps the first five
updates and injects them every 30 seconds, leaving a final 30-second tail.
"""

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.promptchoreo.adapters.happy_oyster_global import (
    GoogleAccountIsolationRequired,
    HappyOysterGlobalAdapter,
)
from src.promptchoreo.core.media import get_mp4_media_info
from scripts.bench_subset import (
    DEFAULT_SUBSET_JOBS,
    filter_work_items_by_duration,
    prepare_subset_work_items,
    subset_output_dir,
)
from scripts.happyoyster_accounts import (
    AccountPool,
    AccountPoolExhausted,
    VIDEOS_PER_ACCOUNT,
)


BENCH_DIR = (
    "StreamAVBench_closed_source_web_package/"
    "StreamAVBench_closed_source_web_package"
)
YAML_DIR = "bench_yamls"
SUBSET_YAML_DIR = os.path.join(YAML_DIR, "formal_120s_subset_60cases")
OUTPUT_BASE = "outputs"
MODEL_ID = "happyoyster_global"
COMPLETED_JOBS_PATH = os.path.join(
    OUTPUT_BASE, MODEL_ID, "completed_jobs.json"
)
ARCHIVED_RUN_ROOT = os.path.join(OUTPUT_BASE, MODEL_ID, "跑过")
MODEL_NAME = "HappyOyster Global"
ADAPTER_CLASS = HappyOysterGlobalAdapter
VIDEO_SRC = "outputs/video/ho"
BROWSER_SIZE = {"width": 2560, "height": 1440}
MAX_RECORDING_EDGE_DELTA_PX = 32
VIDEO_EXTS = (".webm", ".mp4", ".mkv", ".mov", ".avi")
TARGET_DURATION_S = 180.0
TRACK_B_INJECTION_INTERVAL_S = 30.0
DEFAULT_ACCOUNTS_JSON = "happyoyster_accounts.json"
DEFAULT_NEW_BENCH_SHUFFLE_SEED = 20260816
NEW_BENCH_FILENAME_RE = re.compile(
    r"^(?P<track>[IP])-(?P<index>\d+)_(?P=track)-180\.yaml$"
)
TEST_SOURCE_JOB_IDS = (
    "B-0003:B-120",
    "A-0001:A-120",
    "A-0002:A-120",
)
PLAYBACK_UNAVAILABLE_PREFIX = "site_playback_unavailable:"
NONRETRYABLE_GENERATION_PREFIX = "site_generation_nonretryable:"


def _load_completed_jobs_payload(path):
    if not os.path.exists(path):
        return {
            "version": 1,
            "model_id": MODEL_ID,
            "completed_jobs": {},
        }
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"cannot read completed-jobs registry: {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("completed_jobs"), dict
    ):
        raise RuntimeError(
            f"invalid completed-jobs registry (expected completed_jobs object): {path}"
        )
    return payload


def load_completed_job_ids(path=COMPLETED_JOBS_PATH):
    """Load successful job IDs without consulting per-job output folders."""
    payload = _load_completed_jobs_payload(path)
    return {
        job_id
        for job_id, record in payload["completed_jobs"].items()
        if isinstance(record, dict) and record.get("status") == "success"
    }


def mark_job_completed(job_id, path=COMPLETED_JOBS_PATH, completed_at_utc=None):
    """Atomically persist a successful run independently of its output files."""
    payload = _load_completed_jobs_payload(path)
    completed_at_utc = completed_at_utc or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    payload["version"] = 1
    payload["model_id"] = MODEL_ID
    payload["completed_jobs"][job_id] = {
        "status": "success",
        "completed_at_utc": completed_at_utc,
    }

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary_path = f"{path}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return path


def unmark_job_completed(job_id, path=COMPLETED_JOBS_PATH):
    """Remove one success entry when an explicit rerun begins."""
    payload = _load_completed_jobs_payload(path)
    if job_id not in payload["completed_jobs"]:
        return False
    del payload["completed_jobs"][job_id]

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary_path = f"{path}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return True


def is_nonretryable_site_failure(exc_or_text):
    text = str(exc_or_text)
    return text.startswith(
        (PLAYBACK_UNAVAILABLE_PREFIX, NONRETRYABLE_GENERATION_PREFIX)
    ) or any(
        marker in text
        for marker in (
            "Oops / Something went wrong",
            "Oops: Something went wrong",
            "Oops / 出了点问题",
        )
    )


def find_nonretryable_failure_reason(out_dir):
    """Find known skip failures in current or archived attempt manifests."""
    candidates = [os.path.join(out_dir, "run_manifest.json")]
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir), reverse=True):
            if name.startswith("attempt_"):
                candidates.append(
                    os.path.join(out_dir, name, "run_manifest.json")
                )
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        reason = str(manifest.get("failure_reason") or "")
        if manifest.get("status") == "failed" and is_nonretryable_site_failure(
            reason
        ):
            return reason
    return None


def job_output_candidates(out_dir, job_id):
    """Return active and manually archived output locations for a job."""
    candidates = [out_dir]
    archived = archived_job_output_dir(job_id)
    if archived:
        if os.path.normcase(os.path.normpath(archived)) != os.path.normcase(
            os.path.normpath(out_dir)
        ):
            candidates.append(archived)
    return candidates


def archived_job_output_dir(job_id):
    """Map an Interactive/Progressive job to its manual archive folder."""
    track = str(job_id)[:1].lower()
    if track not in ("i", "p"):
        return None
    return os.path.join(
        ARCHIVED_RUN_ROOT, track, str(job_id).replace(":", "_")
    )


def find_archived_job_output(job_id):
    """Return the archive folder when the job was manually marked as run."""
    archived = archived_job_output_dir(job_id)
    return archived if archived and os.path.isdir(archived) else None


def find_existing_skip_marker(out_dir, job_id):
    """Find a permanent skip marker in active or archived job outputs."""
    for candidate in job_output_candidates(out_dir, job_id):
        marker_path = os.path.join(candidate, "skip_job.json")
        if os.path.exists(marker_path):
            return marker_path
    return None


def find_job_nonretryable_failure(out_dir, job_id):
    """Find a known non-retryable failure in active or archived outputs."""
    for candidate in job_output_candidates(out_dir, job_id):
        reason = find_nonretryable_failure_reason(candidate)
        if reason:
            return candidate, reason
    return None, None


def write_skip_marker(out_dir, job_id, failure_reason):
    """Persist a non-retryable marker shared by live and legacy failures."""
    os.makedirs(out_dir, exist_ok=True)
    playback_unavailable = str(failure_reason).startswith(
        PLAYBACK_UNAVAILABLE_PREFIX
    )
    evidence = (
        "This scene can't be played right now. Showing the first frame "
        "preview for now."
        if playback_unavailable
        else "Oops: Something went wrong"
    )
    marker = {
        "job_id": job_id,
        "model_id": MODEL_ID,
        "status": "failed",
        "skip_future_runs": True,
        "retryable": False,
        "failure_reason": str(failure_reason),
        "evidence": evidence,
        "recorded_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    skip_path = os.path.join(out_dir, "skip_job.json")
    with open(skip_path, "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=2, ensure_ascii=False)
    print(f"  [SKIP] 不可重试跳过标记已写入: {skip_path}")
    return skip_path


def is_acceptable_recording_resolution(media_info):
    """Allow the small client-area delta introduced by browser chrome."""
    width = media_info.get("width")
    height = media_info.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    return (
        abs(width - BROWSER_SIZE["width"]) <= MAX_RECORDING_EDGE_DELTA_PX
        and abs(height - BROWSER_SIZE["height"])
        <= MAX_RECORDING_EDGE_DELTA_PX
    )


def get_ffmpeg():
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def normalize_full_frame(ffmpeg, src, dst, duration_s):
    """Convert the entire source frame to MP4; spatial filters are forbidden."""
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        src,
    ]
    if duration_s > 0:
        cmd += ["-t", str(float(duration_s))]
    cmd += [
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        dst,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("    [失败] 全画面 MP4 转换超时")
        return False
    if proc.returncode != 0:
        print(
            "    [失败] "
            + (proc.stderr[-300:] if proc.stderr else "未知 ffmpeg 错误")
        )
        return False
    return True


def load_job_source(phase, job_id):
    path = os.path.join(BENCH_DIR, f"{phase}_jobs.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for job in data["jobs"]:
        if job["job_id"] == job_id:
            return job
    return None


def yaml_to_job_id(filename):
    return filename.replace(".yaml", "").replace("_", ":", 1)


def _job_track(job, job_id):
    if job and job.get("track"):
        return str(job["track"]).upper()
    split = str(job.get("split", "") if job else "")
    if split:
        return split.split("-", 1)[0].upper()
    if ":" in job_id:
        return job_id.split(":", 1)[1].split("-", 1)[0].upper()
    return ""


def build_run_plan(job, raw, job_id):
    """Return the fixed 180s schedule used by Happy Oyster international.

    A 180-second Track B run has five useful 30-second update slots
    (30..150); any events beyond the recording boundary are omitted.
    """
    source_schedule = list(job.get("prompt_schedule", []) if job else [])
    if source_schedule:
        initial = next(
            (
                dict(prompt)
                for prompt in source_schedule
                if prompt.get("role") == "initial"
            ),
            {},
        )
        updates = [
            dict(prompt)
            for prompt in source_schedule
            if prompt.get("role") != "initial"
        ]
    else:
        initial = {
            "activation_media_time_s": 0.0,
            "text": raw.get("initial_prompt", ""),
            "prompt_id": "",
            "role": "initial",
        }
        updates = [
            {
                "activation_media_time_s": event.get("time", 0),
                "text": event.get("prompt", ""),
                "prompt_id": event.get("prompt_id", ""),
                "role": event.get("role", "update"),
            }
            for event in (raw.get("events") or [])
            if float(event.get("time", 0) or 0) > 0
        ]

    track = _job_track(job, job_id)
    if track == "B":
        max_updates = max(
            int(TARGET_DURATION_S // TRACK_B_INJECTION_INTERVAL_S) - 1,
            0,
        )
        updates = updates[:max_updates]
        for index, prompt in enumerate(updates, start=1):
            prompt["activation_media_time_s"] = (
                index * TRACK_B_INJECTION_INTERVAL_S
            )

    prompt_schedule = ([initial] if initial else []) + updates
    inject_events = [
        {
            "time": float(prompt["activation_media_time_s"]),
            "prompt": prompt.get("text", prompt.get("prompt", "")),
            "prompt_id": prompt.get("prompt_id", ""),
            "role": prompt.get("role", "update"),
        }
        for prompt in updates
    ]
    last_injection_s = (
        inject_events[-1]["time"] if inject_events else 0.0
    )
    return {
        "target_duration_s": TARGET_DURATION_S,
        "end_delay_s": TARGET_DURATION_S - last_injection_s,
        "initial_prompt": initial.get(
            "text", initial.get("prompt", raw.get("initial_prompt", ""))
        ),
        "prompt_schedule": prompt_schedule,
        "inject_events": inject_events,
    }


def manifest_matches_current_policy(manifest):
    """Only reuse results already recorded with the current 180s policy."""
    try:
        target_duration_s = float(manifest.get("target_duration_s"))
    except (TypeError, ValueError):
        return False
    return (
        manifest.get("status") == "success"
        and target_duration_s == TARGET_DURATION_S
    )


def prepare_attempt(out_dir):
    """Archive a previous attempt and return ``(attempt_id, retry_reason)``."""
    attempt_numbers = []
    retry_reason = None
    if os.path.isdir(out_dir):
        for name in os.listdir(out_dir):
            if name.startswith("attempt_") and name[8:].isdigit():
                attempt_numbers.append(int(name[8:]))

    manifest_path = os.path.join(out_dir, "run_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            previous = json.load(f)
        if previous.get("status") != "success":
            previous_failure = str(previous.get("failure_reason") or "")
            retry_reason = (
                "retry_after_site_generation_error"
                if previous_failure.startswith("site_generation_failed:")
                else "retry_after_previous_failed_attempt"
            )
        previous_id = previous.get("attempt_id", "attempt_001")
        previous_number = (
            int(previous_id[8:])
            if previous_id.startswith("attempt_")
            and previous_id[8:].isdigit()
            else 1
        )
        attempt_numbers.append(previous_number)
        archive_dir = os.path.join(out_dir, f"attempt_{previous_number:03d}")
        os.makedirs(archive_dir, exist_ok=True)
        for name in (
            "run_manifest.json",
            "prompt_events.jsonl",
            "chunk_events.jsonl",
            "final_video.mp4",
            "error_recording.mp4",
            "error_screenshot.png",
            "skip_job.json",
            "chunks",
        ):
            source = os.path.join(out_dir, name)
            target = os.path.join(archive_dir, name)
            if os.path.exists(source) and not os.path.exists(target):
                shutil.move(source, target)
        for name in os.listdir(out_dir):
            if not name.startswith("error_recording_source."):
                continue
            source = os.path.join(out_dir, name)
            target = os.path.join(archive_dir, name)
            if os.path.isfile(source) and not os.path.exists(target):
                shutil.move(source, target)

    return (
        f"attempt_{max(attempt_numbers, default=0) + 1:03d}",
        retry_reason,
    )


def _remove_video_files(directory):
    if not os.path.isdir(directory):
        return
    for name in os.listdir(directory):
        if not name.lower().endswith(VIDEO_EXTS):
            continue
        path = os.path.join(directory, name)
        for attempt in range(30):
            try:
                os.remove(path)
                break
            except OSError:
                if attempt == 29:
                    raise
                time.sleep(1)


def finalize_recording(out_dir, target_duration_s):
    """Write a full-frame MP4 without applying any spatial crop."""
    files = sorted(
        name
        for name in os.listdir(VIDEO_SRC)
        if name.lower().endswith(VIDEO_EXTS)
    )
    if not files:
        print("  [WARN] Happy Oyster 录屏目录中没有视频")
        return None

    ffmpeg = get_ffmpeg()
    src = os.path.join(VIDEO_SRC, files[0])
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "final_video.mp4")
    print("  [VIDEO] 保留完整录屏画面，不执行空间裁剪")

    ok = normalize_full_frame(ffmpeg, src, dst, target_duration_s)
    if not ok:
        return None
    print(f"  [VIDEO] 原始录屏已整理并命名: {src} -> {dst}")
    return dst


def finalize_error_recording(out_dir):
    """Preserve the current raw recorder file as failure evidence."""
    if not os.path.isdir(VIDEO_SRC):
        return None
    files = [
        name
        for name in os.listdir(VIDEO_SRC)
        if name.lower().endswith(VIDEO_EXTS)
        and os.path.getsize(os.path.join(VIDEO_SRC, name)) > 0
    ]
    if not files:
        print("  [WARN] 未找到可保留的失败录屏文件")
        return None

    src = max(
        (os.path.join(VIDEO_SRC, name) for name in files),
        key=os.path.getmtime,
    )
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "error_recording.mp4")
    print("  [VIDEO] 正在保留失败录屏证据")
    try:
        normalized = normalize_full_frame(get_ffmpeg(), src, dst, 0)
    except Exception as exc:
        normalized = False
        print(f"  [WARN] 失败录屏 MP4 转换异常: {exc}")
    if normalized:
        print(f"  [VIDEO] 失败录屏已保存: {src} -> {dst}")
        return dst

    extension = os.path.splitext(src)[1].lower() or ".video"
    fallback = os.path.join(out_dir, f"error_recording_source{extension}")
    shutil.copy2(src, fallback)
    print(
        "  [VIDEO] MP4 转换失败，已保留原始失败录屏: "
        f"{fallback}"
    )
    return fallback


def write_failed_manifest(
    out_dir,
    job_id,
    job,
    attempt_id,
    job_start_time_utc,
    retry_reason,
    exc,
):
    """Write a required failure record, including any events captured so far."""
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "chunks"), exist_ok=True)

    error_recording_path = finalize_error_recording(out_dir)
    error_recording_info = (
        get_mp4_media_info(error_recording_path)
        if error_recording_path
        else {}
    )
    video_path = os.path.join(out_dir, "final_video.mp4")
    video_exists = os.path.exists(video_path)
    media_info = get_mp4_media_info(video_path) if video_exists else {}
    duration = media_info.get("duration_s")
    prompt_events = getattr(exc, "prompt_events", []) or []
    timing = getattr(exc, "timing", {}) or {}
    error_text = str(exc)
    playback_unavailable = error_text.startswith(
        PLAYBACK_UNAVAILABLE_PREFIX
    )
    nonretryable_failure = is_nonretryable_site_failure(error_text)
    site_generation_failed = error_text.startswith(
        ("site_generation_failed:", NONRETRYABLE_GENERATION_PREFIX)
    )
    site_failure = site_generation_failed or playback_unavailable

    manifest = {
        "job_id": job_id,
        "case_id": job["case_id"] if job else "",
        "split": job["split"] if job else "",
        "phase": job["phase"] if job else "pilot",
        "attempt_id": attempt_id,
        "model_id": MODEL_ID,
        "model_name": MODEL_NAME,
        "model_version": None,
        "run_time_utc": getattr(
            exc, "run_time_utc", None
        ) or job_start_time_utc,
        "target_duration_s": TARGET_DURATION_S,
        "settings": {
            "resolution": (
                None if site_failure else media_info.get("resolution")
            ),
            "fps": None if site_failure else media_info.get("fps"),
            "audio_enabled": (
                None if site_failure
                else media_info.get("audio_enabled")
            ),
            "seed": None,
        },
        "timing": {
            "job_start_time": getattr(
                exc, "run_time_utc", None
            ) or job_start_time_utc,
            "initial_prompt_time_s": timing.get("initial_prompt_time_s"),
            "first_video_chunk_time_s": timing.get(
                "first_video_chunk_time_s"
            ),
            "generation_complete_time_s": timing.get(
                "generation_complete_time_s"
            ),
        },
        "final_video": (
            None
            if site_failure
            else ("final_video.mp4" if video_exists else None)
        ),
        "actual_duration_s": (
            None
            if site_failure
            else (
                round(float(duration), 3)
                if duration is not None
                else None
            )
        ),
        "error_recording": (
            os.path.basename(error_recording_path)
            if error_recording_path
            else None
        ),
        "error_recording_duration_s": (
            round(float(error_recording_info["duration_s"]), 3)
            if error_recording_info.get("duration_s") is not None
            else None
        ),
        "native_chunks_observable": False,
        "status": (
            "failed"
            if site_failure
            else ("partial" if video_exists else "failed")
        ),
        "failure_reason": (
            error_text
            if site_failure
            else f"automation_error: {type(exc).__name__}: {exc}"
        ),
        "retry_reason": retry_reason,
        "notes": (
            (
                "HappyOyster displayed 'This scene can't be played right "
                "now' and only exposed a first-frame preview. Recording was "
                "stopped immediately and this job is non-retryable."
                if playback_unavailable
                else (
                    "HappyOyster displayed a generation error. The benchmark "
                    "prompt was submitted verbatim and was not modified; no "
                    "valid model video was produced."
                )
            )
            if site_failure
            else (
                "Final video, when present, is a full-frame external screen "
                "recording normalized to MP4 without spatial cropping."
            )
        ),
    }
    manifest_path = os.path.join(out_dir, "run_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"  [MANIFEST] 失败记录已写入: {manifest_path}")

    prompt_path = os.path.join(out_dir, "prompt_events.jsonl")
    with open(prompt_path, "w", encoding="utf-8") as f:
        for event in prompt_events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(
        f"  [EVENTS] 提交事件已写入: {prompt_path} "
        f"({len(prompt_events)} 条)"
    )
    with open(
        os.path.join(out_dir, "chunk_events.jsonl"), "w", encoding="utf-8"
    ):
        pass
    if nonretryable_failure:
        write_skip_marker(out_dir, job_id, error_text)


def _adapter_timing(adapter):
    return {
        "initial_prompt_time_s": getattr(
            adapter, "initial_prompt_time_s", None
        ),
        "first_video_chunk_time_s": getattr(
            adapter, "first_video_chunk_time_s", None
        ),
        "generation_complete_time_s": getattr(
            adapter, "generation_complete_time_s", None
        ),
    }


def _attach_failure_context(exc, adapter, fallback_run_time):
    """Attach adapter state so the outer failure writer can preserve it."""
    for name, value in (
        ("prompt_events", list(getattr(adapter, "_injection_log", []) or [])),
        ("timing", _adapter_timing(adapter)),
        (
            "run_time_utc",
            getattr(adapter, "job_start_time_utc", None)
            or fallback_run_time,
        ),
    ):
        try:
            setattr(exc, name, value)
        except Exception:
            pass


async def _safe_teardown(adapter, page):
    try:
        await adapter.teardown(page)
    except Exception as exc:
        print(f"  [WARN] Happy Oyster 页面复位失败: {exc}")


async def run_one(
    page,
    yaml_path,
    job_id,
    job,
    out_dir,
    attempt_id,
    job_start_time_utc,
    job_start_monotonic,
    retry_reason,
    args,
):
    """Run one job without closing the persistent browser."""
    _remove_video_files(VIDEO_SRC)

    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    run_plan = build_run_plan(job, raw, job_id)
    prompt_schedule = run_plan["prompt_schedule"]
    inject_events = run_plan["inject_events"]
    target_duration_s = run_plan["target_duration_s"]
    if _job_track(job, job_id) in ("B", "INTERACTIVE"):
        print(
            "  [PLAN] Interactive injections: "
            + ", ".join(f"{event['time']:.0f}s" for event in inject_events)
        )
    print(f"  [PLAN] Target duration: {target_duration_s:.0f}s")
    config = {
        "initial_prompt": run_plan["initial_prompt"],
        "_inject_events": inject_events,
        "_prompt_schedule": prompt_schedule,
        "_end_delay": run_plan["end_delay_s"],
        "_required_duration_s": target_duration_s,
        "_job_start_time_utc": job_start_time_utc,
        "_job_start_monotonic": job_start_monotonic,
        "max_load_wait": float(args.max_load_wait),
        # 保留 HappyOyster 的全部原始声音，不点击配乐/静音按钮。
        "bgm_off": False,
    }
    recorder = raw.get("recorder") or {}
    if recorder.get("enabled"):
        config["_recorder_enabled"] = True
        config["_recorder_start_hotkey"] = recorder.get(
            "start_hotkey", "ctrl+f1"
        )
        config["_recorder_stop_hotkey"] = recorder.get(
            "stop_hotkey", "ctrl+f2"
        )

    adapter = ADAPTER_CLASS(config)
    try:
        await adapter.setup(page)

        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(os.path.join(out_dir, "chunks"), exist_ok=True)

        final_video = finalize_recording(out_dir, target_duration_s)
        if not final_video:
            raise RuntimeError("video_finalize_failed")

        media_info = get_mp4_media_info(final_video)
        actual_duration_s = media_info["duration_s"]
        if actual_duration_s is None:
            raise RuntimeError("final_video_duration_unavailable")
        if not is_acceptable_recording_resolution(media_info):
            raise RuntimeError(
                "unexpected_final_resolution: "
                f"{media_info.get('resolution')!r}; expected "
                f"within {MAX_RECORDING_EDGE_DELTA_PX}px per edge of "
                f"{BROWSER_SIZE['width']}x{BROWSER_SIZE['height']}"
            )
        if (
            media_info.get("width") != BROWSER_SIZE["width"]
            or media_info.get("height") != BROWSER_SIZE["height"]
        ):
            print(
                "  [VIDEO] 接受窗口边框导致的近似全屏分辨率: "
                f"{media_info.get('resolution')}"
            )
        actual_duration_s = round(float(actual_duration_s), 3)

        run_time = adapter.job_start_time_utc or job_start_time_utc
        manifest = {
            "job_id": job_id,
            "case_id": job["case_id"] if job else "",
            "split": job["split"] if job else "",
            "phase": job["phase"] if job else "pilot",
            "attempt_id": attempt_id,
            "model_id": MODEL_ID,
            "model_name": MODEL_NAME,
            "model_version": None,
            "run_time_utc": run_time,
            "target_duration_s": target_duration_s,
            "settings": {
                "resolution": media_info["resolution"],
                "fps": media_info["fps"],
                "audio_enabled": media_info["audio_enabled"],
                "seed": None,
            },
            "timing": {
                "job_start_time": run_time,
                **_adapter_timing(adapter),
            },
            "final_video": "final_video.mp4",
            "actual_duration_s": actual_duration_s,
            "native_chunks_observable": False,
            "status": "success",
            "failure_reason": None,
            "retry_reason": retry_reason,
            "notes": (
                "Final video is a full-frame 2560x1440 external screen "
                "recording normalized to MP4 without spatial cropping."
            ),
        }
        manifest_path = os.path.join(out_dir, "run_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        timing = manifest["timing"]
        print(f"  [MANIFEST] 运行清单已写入: {manifest_path}")
        print(
            "  [TIMING] 已记录提交清单时间: "
            f"job_start_utc={timing['job_start_time']}, "
            f"initial_prompt=+{timing['initial_prompt_time_s']}s, "
            f"recording_start=+{timing['first_video_chunk_time_s']}s, "
            f"generation_complete=+{timing['generation_complete_time_s']}s"
        )

        chunk_events_path = os.path.join(out_dir, "chunk_events.jsonl")
        with open(chunk_events_path, "w", encoding="utf-8"):
            pass
        prompt_events_path = os.path.join(out_dir, "prompt_events.jsonl")
        with open(prompt_events_path, "w", encoding="utf-8") as f:
            for event in adapter._injection_log:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(
            f"  [EVENTS] Prompt 提交记录已写入: {prompt_events_path} "
            f"({len(adapter._injection_log)} 条)"
        )
        print(f"  [EVENTS] Chunk 记录已写入: {chunk_events_path}")
    except Exception as exc:
        _attach_failure_context(exc, adapter, job_start_time_utc)
        await _safe_teardown(adapter, page)
        raise
    else:
        await _safe_teardown(adapter, page)


def explicit_job_selection(args):
    """Return whether this invocation explicitly selects one or more jobs."""
    return bool(getattr(args, "job", None) or getattr(args, "jobs", None))


def _requested_job_filenames(args):
    requested = set()
    for value in getattr(args, "jobs", None) or []:
        filename = os.path.basename(str(value)).replace(":", "_")
        if not filename.endswith(".yaml"):
            filename += ".yaml"
        requested.add(filename)
    return requested


def select_files(args):
    files = sorted(
        name for name in os.listdir(YAML_DIR) if name.endswith(".yaml")
    )
    requested_filenames = _requested_job_filenames(args)
    if requested_filenames:
        files = [name for name in files if name in requested_filenames]
    elif args.job:
        files = [name for name in files if args.job in name]

    if args.phase in ("pilot", "remain"):
        source = os.path.join(BENCH_DIR, f"{args.phase}_jobs.json")
        with open(source, encoding="utf-8") as f:
            phase_ids = {job["job_id"] for job in json.load(f)["jobs"]}
        files = [
            name for name in files if yaml_to_job_id(name) in phase_ids
        ]
    elif args.phase == "progressive":
        files = [name for name in files if name.startswith("P-")]
    elif args.phase == "interactive":
        files = [name for name in files if name.startswith("I-")]
    elif args.phase == "new":
        files = [
            name
            for name in files
            if name.startswith("P-") or name.startswith("I-")
        ]
    if requested_filenames:
        selected = set(files)
        missing = sorted(requested_filenames - selected)
        if missing:
            raise ValueError(
                "requested YAML is missing or incompatible with --phase: "
                + ", ".join(missing)
            )

    if (
        args.phase in ("progressive", "interactive", "new")
        and not explicit_job_selection(args)
    ):
        files = order_new_bench_files(
            files,
            args.phase,
            getattr(
                args,
                "shuffle_seed",
                DEFAULT_NEW_BENCH_SHUFFLE_SEED,
            ),
        )
    return files


def order_new_bench_files(files, phase, seed):
    """Return a deterministic shuffled order for the new benchmark.

    The combined phase shuffles scenario numbers, then emits the matching
    Interactive and Progressive YAML consecutively: I-n, P-n.
    """
    files = sorted(files)
    rng = random.Random(seed)
    if phase in ("progressive", "interactive"):
        rng.shuffle(files)
        return files
    if phase != "new":
        return files

    pairs = {}
    for filename in files:
        match = NEW_BENCH_FILENAME_RE.fullmatch(filename)
        if match is None:
            raise ValueError(f"invalid new benchmark YAML filename: {filename}")
        index = match.group("index")
        track = match.group("track")
        if track in pairs.setdefault(index, {}):
            raise ValueError(f"duplicate {track} YAML for scenario {index}")
        pairs[index][track] = filename

    incomplete = [
        index for index, pair in pairs.items() if set(pair) != {"I", "P"}
    ]
    if incomplete:
        raise ValueError(
            "new benchmark I/P pair is incomplete: "
            + ", ".join(sorted(incomplete, key=int))
        )

    indices = sorted(pairs, key=int)
    rng.shuffle(indices)
    return [
        pairs[index][track]
        for index in indices
        for track in ("I", "P")
    ]


def new_bench_job(filename):
    job_id = yaml_to_job_id(filename)
    case_id, _, split = job_id.partition(":")
    if case_id.startswith("P-") and split == "P-180":
        track = "progressive"
    elif case_id.startswith("I-") and split == "I-180":
        track = "interactive"
    else:
        return None
    return {
        "job_id": job_id,
        "case_id": case_id,
        "track": track,
        "split": split,
        "phase": track,
        "duration_s": int(TARGET_DURATION_S),
    }


def select_work_items(args):
    if args.phase == "test":
        work_items = []
        for source_job_id in TEST_SOURCE_JOB_IDS:
            source_job = load_job_source("pilot", source_job_id)
            if source_job is None:
                raise RuntimeError(
                    f"test source job not found: {source_job_id}"
                )
            job = dict(source_job)
            track = str(job["track"])
            job["duration_s"] = int(TARGET_DURATION_S)
            job["split"] = f"{track}-{int(TARGET_DURATION_S)}"
            job["job_id"] = (
                f"{job['case_id']}:{job['split']}"
            )
            job["phase"] = "test"
            work_items.append(
                (
                    source_job_id.replace(":", "_") + ".yaml",
                    job,
                )
            )
    elif args.subset:
        work_items = prepare_subset_work_items(
            args.subset, SUBSET_YAML_DIR, args.job
        )
    else:
        work_items = [
            (
                filename,
                load_job_source("pilot", yaml_to_job_id(filename))
                or load_job_source("remain", yaml_to_job_id(filename))
                or new_bench_job(filename),
            )
            for filename in select_files(args)
        ]

    return filter_work_items_by_duration(work_items, args.duration_filter)


def write_new_bench_run_list(work_items, args):
    """Persist the exact shuffled order so a run can be audited or resumed."""
    if (
        args.phase not in ("progressive", "interactive", "new")
        or explicit_job_selection(args)
    ):
        return None
    seed = getattr(
        args, "shuffle_seed", DEFAULT_NEW_BENCH_SHUFFLE_SEED
    )
    run_list_dir = os.path.join(OUTPUT_BASE, MODEL_ID, "run_lists")
    os.makedirs(run_list_dir, exist_ok=True)
    path = os.path.join(run_list_dir, f"{args.phase}_seed_{seed}.json")
    payload = {
        "phase": args.phase,
        "shuffle_seed": seed,
        "count": len(work_items),
        "ordering": (
            "shuffle scenario numbers; for each number run Interactive then "
            "Progressive"
            if args.phase == "new"
            else "shuffle YAML files"
        ),
        "items": [
            {
                "position": position,
                "yaml_file": filename,
                "job_id": job["job_id"] if job else yaml_to_job_id(filename),
                "track": job.get("track") if job else None,
            }
            for position, (filename, job) in enumerate(work_items, start=1)
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


async def clear_browser_identity(context, page):
    """Remove cross-account web identity before starting a new OAuth flow."""
    await context.clear_cookies()
    await page.goto("about:blank", wait_until="domcontentloaded")


async def launch_runner_context(chromium, user_data_dir):
    context = await chromium.launch_persistent_context(
        user_data_dir,
        headless=False,
        # EV 锁定的是 Chrome 创建时的整个窗口尺寸。不要设置 Playwright
        # viewport（会额外叠加浏览器栏），也不要最大化（会扣掉任务栏 48px）。
        no_viewport=True,
        args=[
            "--no-restore",
            "--use-fake-ui-for-media-stream",
            "--window-position=0,0",
            "--window-size=2560,1440",
        ],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    session = await context.new_cdp_session(page)
    try:
        window = await session.send("Browser.getWindowForTarget")
        bounds = window.get("bounds") or {}
    finally:
        await session.detach()
    actual_window_size = (
        int(bounds.get("width") or 0),
        int(bounds.get("height") or 0),
    )
    expected_window_size = (
        BROWSER_SIZE["width"],
        BROWSER_SIZE["height"],
    )
    print(f"Browser window bounds: {bounds}")
    if actual_window_size != expected_window_size:
        await context.close()
        raise RuntimeError(
            "unexpected_browser_window_size_before_jobs: "
            f"{actual_window_size[0]}x{actual_window_size[1]}; "
            f"expected {expected_window_size[0]}x{expected_window_size[1]}. "
            "No prompt was submitted."
        )
    return context, page


async def main_async(args):
    work_items = select_work_items(args)
    run_list_path = write_new_bench_run_list(work_items, args)
    completed_job_ids = load_completed_job_ids()
    force_rerun = bool(getattr(args, "force_rerun", False))
    account_pool = getattr(args, "account_pool", None)
    if account_pool is None:
        account_pool = AccountPool(args.accounts_json, args.account_state)
    print(f"Jobs: {len(work_items)}")
    if run_list_path:
        print(f"Run list: {run_list_path}")
    print(f"Completed registry: {len(completed_job_ids)} successful jobs")
    print(
        "Accounts: "
        f"{account_pool.available_count}/{len(account_pool.accounts)} available; "
        f"{account_pool.remaining_video_slots} video slots remaining"
    )
    if args.dry_run:
        for filename, job in work_items:
            job_id = job["job_id"] if job else yaml_to_job_id(filename)
            out_dir = (
                subset_output_dir(job, MODEL_ID)
                if args.subset
                else os.path.join(
                    OUTPUT_BASE,
                    MODEL_ID,
                    job["phase"] if job else "pilot",
                    job_id.replace(":", "_"),
                )
            )
            if force_rerun:
                print(f"  [DRY-RERUN] {job_id} -> {out_dir}")
                continue
            if job_id in completed_job_ids:
                print(f"  [DRY-SKIP] {job_id}: 已成功完成（成功账本）")
                continue
            archived_job_dir = find_archived_job_output(job_id)
            if archived_job_dir:
                print(
                    f"  [DRY-SKIP] {job_id}: 跑过目录已存在: "
                    f"{archived_job_dir}"
                )
                continue
            skip_marker_path = find_existing_skip_marker(out_dir, job_id)
            _, legacy_skip_reason = find_job_nonretryable_failure(
                out_dir, job_id
            )
            if skip_marker_path or legacy_skip_reason:
                print(
                    f"  [DRY-SKIP] {job_id}: "
                    "已标记不重试或历史 Oops 失败（含跑过目录）"
                )
                continue
            print(f"  [DRY] {job_id} -> {out_dir}")
        return

    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        context, page = await launch_runner_context(
            playwright.chromium, ADAPTER_CLASS.user_data_dir
        )
        temporary_profiles = []
        ok_count = 0
        auth_adapter = ADAPTER_CLASS()
        for filename, job in work_items:
            job_id = job["job_id"] if job else yaml_to_job_id(filename)
            if not force_rerun and job_id in completed_job_ids:
                print(
                    f"\n=== {job_id} ===  "
                    "[SKIP] 已成功完成（成功账本）"
                )
                continue
            archived_job_dir = find_archived_job_output(job_id)
            if not force_rerun and archived_job_dir:
                print(
                    f"\n=== {job_id} ===  "
                    "[SKIP] 跑过目录已存在: "
                    f"{archived_job_dir}"
                )
                continue
            out_dir = (
                subset_output_dir(job, MODEL_ID)
                if args.subset
                else os.path.join(
                    OUTPUT_BASE,
                    MODEL_ID,
                    job["phase"] if job else "pilot",
                    job_id.replace(":", "_"),
                )
            )
            skip_marker_path = find_existing_skip_marker(out_dir, job_id)
            if not force_rerun and skip_marker_path:
                print(
                    f"\n=== {job_id} ===  [SKIP] 已标记不重试: "
                    f"{skip_marker_path}"
                )
                continue
            legacy_dir, legacy_skip_reason = find_job_nonretryable_failure(
                out_dir, job_id
            )
            if not force_rerun and legacy_skip_reason:
                marker_path = write_skip_marker(
                    legacy_dir, job_id, legacy_skip_reason
                )
                print(
                    f"\n=== {job_id} ===  [SKIP] "
                    "检测到历史 Oops/不可重试失败: "
                    f"{marker_path}"
                )
                continue
            print(f"\n=== {job_id} ===")
            if account_pool.available_count <= 0:
                print(
                    "  STOP: Happy Oyster 账号的单次成功视频"
                    "额度均已用完"
                )
                break
            try:
                # Establish a known logged-out state before consuming a new
                # account from the persistent one-video quota pool.
                await auth_adapter.logout(page)
                await clear_browser_identity(context, page)
                print(
                    "  [ACCOUNT] Cleared browser cookies before account login"
                )
            except Exception as exc:
                print(
                    "  STOP: 无法在领用新账号前确认退出状态："
                    f"{exc}"
                )
                break
            try:
                account = account_pool.claim_next(job_id)
            except AccountPoolExhausted as exc:
                print(f"  STOP: {exc}")
                break
            print(
                "  [ACCOUNT] Claimed account "
                f"{account.ordinal}/{len(account_pool.accounts)} "
                f"(max {VIDEOS_PER_ACCOUNT} successful videos)"
            )
            if force_rerun:
                print("  [RERUN] 已显式要求重跑，忽略旧成功/跳过记录")
                if unmark_job_completed(job_id):
                    completed_job_ids.discard(job_id)
                    print("  [RERUN] 已从成功账本移除旧记录")
            attempt_id, retry_reason = prepare_attempt(out_dir)
            job_start_time_utc = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            job_start_monotonic = time.monotonic()
            started = time.time()
            success = False
            failed = False
            nonretryable_failure = False
            cleanup_errors = []
            try:
                try:
                    await auth_adapter.login_with_email(
                        page, account.email, account.password
                    )
                except GoogleAccountIsolationRequired:
                    print(
                        "  [ACCOUNT] Detected previous Google account reuse; "
                        "restarting with an isolated browser profile"
                    )
                    await context.close()
                    isolated_profile = tempfile.TemporaryDirectory(
                        prefix="promptchoreo_happyoyster_isolated_",
                        ignore_cleanup_errors=True,
                    )
                    temporary_profiles.append(isolated_profile)
                    context, page = await launch_runner_context(
                        playwright.chromium, isolated_profile.name
                    )
                    await clear_browser_identity(context, page)
                    print(
                        "  [ACCOUNT] Retrying the same account in a clean profile"
                    )
                    await auth_adapter.login_with_email(
                        page, account.email, account.password
                    )
                await run_one(
                    page,
                    os.path.join(
                        SUBSET_YAML_DIR if args.subset else YAML_DIR,
                        filename,
                    ),
                    job_id,
                    job,
                    out_dir,
                    attempt_id,
                    job_start_time_utc,
                    job_start_monotonic,
                    retry_reason,
                    args,
                )
                success = True
                ok_count += 1
                try:
                    registry_path = mark_job_completed(job_id)
                    completed_job_ids.add(job_id)
                    print(
                        "  [COMPLETED] 成功账本已更新: "
                        f"{registry_path}"
                    )
                except Exception as exc:
                    cleanup_errors.append(f"成功账本写入失败: {exc}")
                print(f"  OK ({time.time() - started:.0f}s)")
            except Exception as exc:
                failed = True
                nonretryable_failure = is_nonretryable_site_failure(exc)
                write_failed_manifest(
                    out_dir,
                    job_id,
                    job,
                    attempt_id,
                    job_start_time_utc,
                    retry_reason,
                    exc,
                )
                print(f"  FAIL: {exc}")
            finally:
                try:
                    account_pool.mark_finished(
                        account, job_id, success=success
                    )
                    if success:
                        print(
                            "  [ACCOUNT] 账号状态已落盘: "
                            f"#{account.ordinal} completed，额度 1/1 已使用"
                        )
                    else:
                        print(
                            "  [ACCOUNT] 账号状态已落盘: "
                            f"#{account.ordinal} failed，成功额度未消耗"
                        )
                except Exception as exc:
                    cleanup_errors.append(f"账号状态写入失败: {exc}")
                try:
                    await auth_adapter.logout(page)
                except Exception as exc:
                    cleanup_errors.append(f"退出登录失败: {exc}")
            if cleanup_errors:
                print(
                    "  STOP: 为避免账号串用已终止批处理："
                    + "；".join(cleanup_errors)
                )
                break
            if failed:
                if nonretryable_failure:
                    print(
                        "  [SKIP] 检测到不可重试的网站错误；"
                        "已停止录屏并标记为后续跳过，继续下一个任务"
                    )
                    continue
                print(
                    "  STOP: 当前任务失败；账号保留为可重试状态，"
                    "已终止批处理以便诊断"
                )
                break

        print(f"\nDone: {ok_count}/{len(work_items)} OK")
        await context.close()
        for temporary_profile in temporary_profiles:
            temporary_profile.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="Happy Oyster international 180s StreamAVBench runner"
    )
    parser.add_argument("--job", default=None)
    parser.add_argument(
        "--jobs",
        nargs="+",
        default=None,
        metavar="YAML",
        help="精确选择多个 YAML 任务（可省略 .yaml）",
    )
    parser.add_argument(
        "--force-rerun",
        action="store_true",
        help=(
            "强制重跑显式选中的任务；旧结果归档到 attempt_*"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--accounts-json",
        default=DEFAULT_ACCOUNTS_JSON,
        metavar="PATH",
        help=(
            "邮箱账号池 JSON；默认 happyoyster_accounts.json；"
            "每个账号最多成功生成一个视频"
        ),
    )
    parser.add_argument(
        "--account-state",
        default=None,
        metavar="PATH",
        help=(
            "账号使用状态文件；默认与账号 JSON 同目录的 "
            ".happyoyster_account_usage.json"
        ),
    )
    parser.add_argument(
        "--phase",
        choices=[
            "pilot",
            "remain",
            "test",
            "progressive",
            "interactive",
            "new",
            "all",
        ],
        default="pilot",
        help=(
            "pilot（默认）| remain | test（3 条 pilot 样本）| "
            "progressive | interactive | new（全部新数据）| all"
        ),
    )
    parser.add_argument(
        "--180",
        dest="duration_filter",
        action="store_const",
        const=180,
        help="只选择 duration_s=180 的任务",
    )
    parser.add_argument(
        "--shuffle-seed",
        type=int,
        default=DEFAULT_NEW_BENCH_SHUFFLE_SEED,
        help=(
            "新数据集的固定乱序种子；相同种子会得到相同运行顺序"
        ),
    )
    parser.set_defaults(duration_filter=None)
    parser.add_argument(
        "--subset",
        nargs="?",
        const=str(DEFAULT_SUBSET_JOBS),
        default=None,
        metavar="JOBS_JSONL",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-load-wait",
        type=float,
        default=600,
        help="等待 Happy Oyster 视频开始播放的最大秒数",
    )
    args = parser.parse_args()
    if args.force_rerun and not explicit_job_selection(args):
        parser.error("--force-rerun 必须与 --job 或 --jobs 一起使用")
    if args.subset and args.jobs:
        parser.error("--jobs 不支持与 --subset 一起使用")
    try:
        args.account_pool = AccountPool(
            args.accounts_json, args.account_state
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
