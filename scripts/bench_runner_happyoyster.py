"""StreamAVBench batch runner for Happy Oyster Directing Mode.

The browser and login profile are reused across jobs. Each successful job:

1. runs through ``HappyOysterAdapter``;
2. records the full 2560x1440 browser frame with the YAML hotkeys;
3. normalizes the full frame to MP4 without spatial cropping;
4. writes a spec-compatible manifest and event files.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.promptchoreo.adapters.happy_oyster import HappyOysterAdapter
from src.promptchoreo.core.media import get_mp4_media_info
from scripts.bench_subset import (
    DEFAULT_SUBSET_JOBS,
    filter_work_items_by_duration,
    prepare_subset_work_items,
    subset_output_dir,
)


BENCH_DIR = (
    "StreamAVBench_closed_source_web_package/"
    "StreamAVBench_closed_source_web_package"
)
YAML_DIR = "bench_yamls"
SUBSET_YAML_DIR = os.path.join(YAML_DIR, "formal_120s_subset_60cases")
OUTPUT_BASE = "outputs"
MODEL_ID = "happyoyster"
MODEL_NAME = "HappyOyster"
ADAPTER_CLASS = HappyOysterAdapter
VIDEO_SRC = "outputs/video/ho"
BROWSER_SIZE = {"width": 2560, "height": 1440}
VIDEO_EXTS = (".webm", ".mp4", ".mkv", ".mov", ".avi")


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
            "chunks",
        ):
            source = os.path.join(out_dir, name)
            target = os.path.join(archive_dir, name)
            if os.path.exists(source) and not os.path.exists(target):
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
    print(f"  [VIDEO] {dst}")
    return dst


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

    video_path = os.path.join(out_dir, "final_video.mp4")
    video_exists = os.path.exists(video_path)
    media_info = get_mp4_media_info(video_path) if video_exists else {}
    duration = media_info.get("duration_s")
    prompt_events = getattr(exc, "prompt_events", []) or []
    timing = getattr(exc, "timing", {}) or {}
    error_text = str(exc)
    site_generation_failed = error_text.startswith("site_generation_failed:")

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
        "target_duration_s": job["duration_s"] if job else 0,
        "settings": {
            "resolution": (
                None if site_generation_failed else media_info.get("resolution")
            ),
            "fps": None if site_generation_failed else media_info.get("fps"),
            "audio_enabled": (
                None if site_generation_failed
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
            if site_generation_failed
            else ("final_video.mp4" if video_exists else None)
        ),
        "actual_duration_s": (
            None
            if site_generation_failed
            else (
                round(float(duration), 3)
                if duration is not None
                else None
            )
        ),
        "native_chunks_observable": False,
        "status": (
            "failed"
            if site_generation_failed
            else ("partial" if video_exists else "failed")
        ),
        "failure_reason": (
            error_text
            if site_generation_failed
            else f"automation_error: {type(exc).__name__}: {exc}"
        ),
        "retry_reason": retry_reason,
        "notes": (
            "HappyOyster displayed a generation error. The benchmark prompt "
            "was submitted verbatim and was not modified; no valid model "
            "video was produced."
            if site_generation_failed
            else (
                "Final video, when present, is a full-frame external screen "
                "recording normalized to MP4 without spatial cropping."
            )
        ),
    }
    with open(
        os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    prompt_path = os.path.join(out_dir, "prompt_events.jsonl")
    with open(prompt_path, "w", encoding="utf-8") as f:
        for event in prompt_events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    with open(
        os.path.join(out_dir, "chunk_events.jsonl"), "w", encoding="utf-8"
    ):
        pass


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

    prompt_schedule = job.get("prompt_schedule", []) if job else []
    inject_events = []
    for prompt in prompt_schedule:
        if prompt.get("role") == "initial":
            continue
        inject_events.append({
            "time": prompt["activation_media_time_s"],
            "prompt": prompt["text"],
            "prompt_id": prompt.get("prompt_id", ""),
            "role": prompt.get("role", "update"),
        })

    target_duration_s = float(
        job.get("duration_s", raw.get("end_delay", 0))
        if job
        else raw.get("end_delay", 0)
    )
    config = {
        "initial_prompt": raw.get("initial_prompt", ""),
        "_inject_events": inject_events,
        "_prompt_schedule": prompt_schedule,
        "_end_delay": float(raw.get("end_delay", 0)),
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
        if (
            media_info.get("width") != BROWSER_SIZE["width"]
            or media_info.get("height") != BROWSER_SIZE["height"]
        ):
            raise RuntimeError(
                "unexpected_final_resolution: "
                f"{media_info.get('resolution')!r}; expected "
                f"{BROWSER_SIZE['width']}x{BROWSER_SIZE['height']}"
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
            "target_duration_s": (
                job["duration_s"] if job else target_duration_s
            ),
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
        with open(
            os.path.join(out_dir, "run_manifest.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        with open(
            os.path.join(out_dir, "chunk_events.jsonl"),
            "w",
            encoding="utf-8",
        ):
            pass
        with open(
            os.path.join(out_dir, "prompt_events.jsonl"),
            "w",
            encoding="utf-8",
        ) as f:
            for event in adapter._injection_log:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        _attach_failure_context(exc, adapter, job_start_time_utc)
        await _safe_teardown(adapter, page)
        raise
    else:
        await _safe_teardown(adapter, page)


def select_files(args):
    files = sorted(
        name for name in os.listdir(YAML_DIR) if name.endswith(".yaml")
    )
    if args.job:
        files = [name for name in files if args.job in name]

    if args.phase in ("pilot", "remain"):
        source = os.path.join(BENCH_DIR, f"{args.phase}_jobs.json")
        with open(source, encoding="utf-8") as f:
            phase_ids = {job["job_id"] for job in json.load(f)["jobs"]}
        files = [
            name for name in files if yaml_to_job_id(name) in phase_ids
        ]
    return files


def select_work_items(args):
    if args.subset:
        work_items = prepare_subset_work_items(
            args.subset, SUBSET_YAML_DIR, args.job
        )
    else:
        work_items = [
            (
                filename,
                load_job_source("pilot", yaml_to_job_id(filename))
                or load_job_source("remain", yaml_to_job_id(filename)),
            )
            for filename in select_files(args)
        ]

    return filter_work_items_by_duration(work_items, args.duration_filter)


async def main_async(args):
    work_items = select_work_items(args)
    print(f"Jobs: {len(work_items)}")
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
            print(f"  [DRY] {job_id} -> {out_dir}")
        return

    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            ADAPTER_CLASS.user_data_dir,
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
            raise RuntimeError(
                "unexpected_browser_window_size_before_jobs: "
                f"{actual_window_size[0]}x{actual_window_size[1]}; "
                f"expected {expected_window_size[0]}x{expected_window_size[1]}. "
                "No prompt was submitted."
            )

        ok_count = 0
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
            manifest_path = os.path.join(out_dir, "run_manifest.json")
            final_video = os.path.join(out_dir, "final_video.mp4")
            if os.path.exists(manifest_path) and os.path.exists(final_video):
                with open(manifest_path, encoding="utf-8") as f:
                    previous = json.load(f)
                if previous.get("status") == "success":
                    print(f"\n=== {job_id} ===  [SKIP] 已完成")
                    continue

            print(f"\n=== {job_id} ===")
            attempt_id, retry_reason = prepare_attempt(out_dir)
            job_start_time_utc = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            job_start_monotonic = time.monotonic()
            started = time.time()
            try:
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
                ok_count += 1
                print(f"  OK ({time.time() - started:.0f}s)")
            except Exception as exc:
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

        print(f"\nDone: {ok_count}/{len(work_items)} OK")
        await context.close()


def main():
    parser = argparse.ArgumentParser(
        description="Happy Oyster StreamAVBench batch runner"
    )
    parser.add_argument("--job", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--phase",
        choices=["pilot", "remain", "all"],
        default="pilot",
        help="pilot（默认）| remain | all",
    )
    duration_group = parser.add_mutually_exclusive_group()
    for duration_s in (30, 60, 120):
        duration_group.add_argument(
            f"--{duration_s}",
            dest="duration_filter",
            action="store_const",
            const=duration_s,
            help=f"只运行当前 phase 中 duration_s={duration_s} 的任务",
        )
    parser.set_defaults(duration_filter=None)
    parser.add_argument(
        "--subset",
        nargs="?",
        const=str(DEFAULT_SUBSET_JOBS),
        default=None,
        metavar="JOBS_JSONL",
        help="运行正式 60-case 120s 子集；不传路径时使用默认私有 JSONL",
    )
    parser.add_argument(
        "--max-load-wait",
        type=float,
        default=600,
        help="等待 Happy Oyster 视频开始播放的最大秒数",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
