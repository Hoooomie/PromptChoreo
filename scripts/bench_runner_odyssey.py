"""Benchmark 批量运行器 — Odyssey 版本（进程内循环，浏览器不关）。

用法:
    python scripts/bench_runner_odyssey.py                  # 默认 pilot（50 个）
    python scripts/bench_runner_odyssey.py --phase remain    # remain（490 个）
    python scripts/bench_runner_odyssey.py --phase all       # 全部（540 个）
"""
import argparse, asyncio, json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone

import yaml
from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.promptchoreo.adapters.odyssey import ContentBlockedError, OdysseyAdapter
from src.promptchoreo.core.media import get_mp4_media_info
from scripts.bench_subset import (
    DEFAULT_SUBSET_JOBS,
    filter_work_items_by_duration,
    prepare_subset_work_items,
    subset_output_dir,
)

BENCH_DIR = "StreamAVBench_closed_source_web_package/StreamAVBench_closed_source_web_package"
YAML_DIR = "bench_yamls"
SUBSET_YAML_DIR = os.path.join(YAML_DIR, "formal_120s_subset_60cases")
OUTPUT_BASE = "outputs"
MODEL_ID = "odyssey"
VIDEO_SRC = "outputs/video/od"
TRIM_OUTPUT = "outputs/tvideo/od"
DEFAULT_VIDEO_SIZE = (2560, 1440)
BROWSER_SIZE = {"width": 2560, "height": 1440}

# ── 裁剪参数（F11 全屏，无浏览器栏） ──
# 与 scripts/trim_odyssey.py 保持一致：视频区域约为 816x444，左上角约为 (872, 510)
#（源分辨率 2560x1440）。视频不在屏幕正中央，因此 x/y 也使用固定比例。
VIDEO_X_RATIO = 0.3408
VIDEO_Y_RATIO = 0.3545
VIDEO_W_RATIO = 0.3190
VIDEO_H_RATIO = 0.3086
VIDEO_EXTS = (".webm", ".mp4", ".mkv", ".mov", ".avi")


def get_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def get_video_size(ffmpeg, video):
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", video], capture_output=True, text=True,
            encoding="utf-8", timeout=30
        )
        m = re.search(
            r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})", proc.stderr
        )
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def even(n):
    return n if n % 2 == 0 else n - 1


def compute_crop(width, height, crop_region=None):
    """计算 Odyssey 裁剪参数。

    crop_region: 显式传入时可覆盖固定裁剪区。
    自动流程使用与 trim_odyssey.py 一致的固定比例、非居中裁剪。
    """
    if crop_region:
        parts = crop_region.split(":")
        if len(parts) == 4:
            return tuple(even(int(p)) for p in parts)

    vw = even(int(width * VIDEO_W_RATIO))
    vh = even(int(height * VIDEO_H_RATIO))
    vx = even(int(width * VIDEO_X_RATIO))
    vy = even(int(height * VIDEO_Y_RATIO))
    return (vw, vh, vx, vy)


def trim_video(crop_region=None):
    """裁剪当前录屏。返回裁剪后 mp4 路径，失败返回 None。"""
    os.makedirs(TRIM_OUTPUT, exist_ok=True)
    ffmpeg = get_ffmpeg()

    files = sorted(f for f in os.listdir(VIDEO_SRC) if f.lower().endswith(VIDEO_EXTS))
    if not files:
        print("  [WARN] 无录屏文件可裁剪")
        return None

    src = os.path.join(VIDEO_SRC, files[0])
    name, _ = os.path.splitext(files[0])
    dst = os.path.join(TRIM_OUTPUT, name + ".mp4")

    if os.path.exists(dst):
        print(f"  [TRIM] 已存在，跳过: {dst}")
        return dst

    crop = compute_crop(0, 0, crop_region)
    if not crop_region:
        size = get_video_size(ffmpeg, src)
        if size:
            crop = compute_crop(size[0], size[1])
            print(f"  裁剪: {crop[0]}x{crop[1]}@({crop[2]},{crop[3]}) (源 {size[0]}x{size[1]})")
        else:
            crop = compute_crop(*DEFAULT_VIDEO_SIZE)
            print(
                f"  无法解析分辨率，使用默认 {DEFAULT_VIDEO_SIZE[0]}x"
                f"{DEFAULT_VIDEO_SIZE[1]} 的固定裁剪: {crop[0]}x{crop[1]}@"
                f"({crop[2]},{crop[3]})"
            )

    w, h, x, y = [even(int(v)) for v in crop]
    vf = f"crop={w}:{h}:{x}:{y}"
    print(f"  实际滤镜: {vf}")

    cmd = [
        ffmpeg, "-y", "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
        dst,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600)
    except subprocess.TimeoutExpired:
        print("  [TRIM] 超时")
        return None

    if proc.returncode != 0:
        # 回退：无音频
        cmd2 = [c for c in cmd if c not in ("-c:a", "aac")] + ["-an"]
        try:
            proc2 = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", timeout=600)
            if proc2.returncode == 0:
                print(f"  [TRIM] (无音频) {dst}")
                return dst
        except Exception:
            pass
        print(f"  [TRIM-FAIL] {proc.stderr[-200:] if proc.stderr else '未知错误'}")
        return None

    print(f"  [TRIM] {dst}")
    return dst


def clear_video_dir():
    if not os.path.isdir(VIDEO_SRC):
        return
    for f in os.listdir(VIDEO_SRC):
        if f.lower().endswith(VIDEO_EXTS):
            path = os.path.join(VIDEO_SRC, f)
            for attempt in range(10):
                try:
                    os.remove(path)
                    break
                except OSError:
                    if attempt == 9:
                        raise
                    time.sleep(1)


def move_trimmed(out_dir):
    files = sorted(f for f in os.listdir(TRIM_OUTPUT) if f.endswith(".mp4"))
    if not files:
        print("  [WARN] 无裁剪后视频")
        return
    src = os.path.join(TRIM_OUTPUT, files[0])
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "final_video.mp4")
    for attempt in range(10):
        try:
            os.rename(src, dst)
            print(f"  [VIDEO] {dst}")
            return dst
        except OSError:
            if attempt == 9:
                raise
            time.sleep(1)


def load_job_source(phase, job_id):
    path = os.path.join(BENCH_DIR, f"{phase}_jobs.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for j in data["jobs"]:
        if j["job_id"] == job_id:
            return j
    return None


def build_success_notes(adapter):
    """Describe non-fatal playback incidents while keeping a valid run successful."""
    notes = (
        "Final video is an external screen recording that was spatially "
        "cropped and re-encoded."
    )
    incidents = getattr(adapter, "_content_blocked_events", []) or []
    if not incidents:
        return notes

    details = []
    for incident in incidents:
        media_time_s = incident.get("media_time_s")
        time_label = (
            f"{float(media_time_s):.1f}s"
            if media_time_s is not None
            else "unknown media time"
        )
        prompt_id = incident.get("prompt_id") or "unknown prompt"
        details.append(f"{time_label} ({prompt_id})")

    close_status = (
        "each dialog was closed automatically"
        if all(incident.get("dialog_closed") for incident in incidents)
        else "automatic closing was attempted but could not be confirmed for every dialog"
    )
    return (
        f"{notes} During playback, Odyssey displayed Content Blocked "
        f"{len(incidents)} time(s) at {', '.join(details)}; {close_status}, "
        "recording continued, and the final video remained usable."
    )


def yaml_to_job_id(filename):
    return filename.replace(".yaml", "").replace("_", ":", 1)


def prepare_attempt(out_dir):
    """Archive a previous non-success attempt and return the next attempt ID."""
    attempt_numbers = []
    if os.path.isdir(out_dir):
        for name in os.listdir(out_dir):
            if name.startswith("attempt_") and name[8:].isdigit():
                attempt_numbers.append(int(name[8:]))

    manifest_path = os.path.join(out_dir, "run_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            previous = json.load(f)
        previous_id = previous.get("attempt_id", "attempt_001")
        previous_number = (
            int(previous_id[8:])
            if previous_id.startswith("attempt_") and previous_id[8:].isdigit()
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
            "chunks",
        ):
            source = os.path.join(out_dir, name)
            target = os.path.join(archive_dir, name)
            if os.path.exists(source) and not os.path.exists(target):
                shutil.move(source, target)

    next_number = max(attempt_numbers, default=0) + 1
    return f"attempt_{next_number:03d}"


def write_failed_manifest(
    out_dir, job_id, job, attempt_id, job_start_time_utc, exc
):
    """Write the required failure record even when no video was produced."""
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "chunks"), exist_ok=True)
    video_path = os.path.join(out_dir, "final_video.mp4")
    video_exists = os.path.exists(video_path)
    media_info = get_mp4_media_info(video_path) if video_exists else {}
    duration = media_info.get("duration_s")
    is_content_blocked = isinstance(exc, ContentBlockedError)
    prompt_events = getattr(exc, "prompt_events", []) or []
    initial_prompt_event = next(
        (event for event in prompt_events if event.get("role") == "initial"),
        None,
    )
    initial_prompt_time_s = (
        initial_prompt_event.get("actual_injection_time_s")
        if initial_prompt_event
        else None
    )
    if is_content_blocked:
        status = "blocked"
        failure_reason = "content_blocked"
        notes = (
            "Odyssey displayed: Content Blocked. Your request was flagged for "
            "inappropriate content; no usable video was produced."
        )
    else:
        status = "partial" if video_exists else "failed"
        failure_reason = f"automation_error: {type(exc).__name__}: {exc}"
        notes = (
            "Final video, when present, is an external screen recording that "
            "was spatially cropped and re-encoded."
        )
    manifest = {
        "job_id": job_id,
        "case_id": job["case_id"] if job else "",
        "split": job["split"] if job else "",
        "phase": job["phase"] if job else "pilot",
        "attempt_id": attempt_id,
        "model_id": MODEL_ID,
        "model_name": "Odyssey",
        "model_version": None,
        "run_time_utc": job_start_time_utc,
        "target_duration_s": job["duration_s"] if job else 0,
        "settings": {
            "resolution": media_info.get("resolution"),
            "fps": media_info.get("fps"),
            "audio_enabled": media_info.get("audio_enabled"),
            "seed": None,
        },
        "timing": {
            "job_start_time": job_start_time_utc,
            "initial_prompt_time_s": initial_prompt_time_s,
            "first_video_chunk_time_s": None,
            "generation_complete_time_s": None,
        },
        "final_video": "final_video.mp4" if video_exists else None,
        "actual_duration_s": round(float(duration), 3) if duration is not None else None,
        "native_chunks_observable": False,
        "status": status,
        "failure_reason": failure_reason,
        "retry_reason": None,
        "notes": notes,
    }
    with open(os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    prompt_path = os.path.join(out_dir, "prompt_events.jsonl")
    if prompt_events:
        with open(prompt_path, "w", encoding="utf-8") as f:
            for event in prompt_events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    elif not os.path.exists(prompt_path):
        with open(prompt_path, "w", encoding="utf-8"):
            pass

    chunk_path = os.path.join(out_dir, "chunk_events.jsonl")
    if not os.path.exists(chunk_path):
        with open(chunk_path, "w", encoding="utf-8"):
            pass


async def run_one(
    page,
    yaml_path,
    job_id,
    job,
    out_dir,
    attempt_id,
    job_start_time_utc,
    job_start_monotonic,
):
    """跑单个 job，不关浏览器。"""
    clear_video_dir()
    # 也清空裁剪输出目录，避免上一 job 残留
    if os.path.isdir(TRIM_OUTPUT):
        for f in os.listdir(TRIM_OUTPUT):
            if f.endswith(".mp4"):
                os.remove(os.path.join(TRIM_OUTPUT, f))

    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # 从 job source 的 prompt_schedule 构建注入事件
    prompt_schedule = job.get("prompt_schedule", []) if job else []
    inject_events = []
    for ps in prompt_schedule:
        if ps.get("role") == "initial":
            continue
        inject_events.append({
            "time": ps["activation_media_time_s"],
            "prompt": ps["text"],
            "prompt_id": ps.get("prompt_id", ""),
            "role": ps.get("role", "update"),
        })

    config = {
        "initial_prompt": raw.get("initial_prompt", ""),
        "_inject_events": inject_events,
        "_prompt_schedule": prompt_schedule,
        "_end_delay": float(raw.get("end_delay", 0)),
        "_required_duration_s": float(
            job.get("duration_s", raw.get("end_delay", 0)) if job else raw.get("end_delay", 0)
        ),
        "_session_guard_s": 15.0,
        "_render_guard_s": 3.0,
        "_job_start_time_utc": job_start_time_utc,
        "_job_start_monotonic": job_start_monotonic,
        # 保留 Odyssey 生成画面的原始配乐/声音，不执行 🎵 关闭动作。
        "bgm_off": False,
    }
    rec = raw.get("recorder") or {}
    if rec.get("enabled"):
        config["_recorder_enabled"] = True
        config["_recorder_start_hotkey"] = rec.get("start_hotkey", "ctrl+f1")
        config["_recorder_stop_hotkey"] = rec.get("stop_hotkey", "ctrl+f2")

    adapter = OdysseyAdapter(config)
    adapter._session_started = False
    await adapter.setup(page)

    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "chunks"), exist_ok=True)

    # 裁剪录屏
    # 与 scripts/trim_odyssey.py 保持同一套截图测得的固定裁剪比例。
    # 不传 adapter.crop_region，避免自动检测结果绕过固定裁剪框。
    if not trim_video():
        raise RuntimeError("video_trim_failed")
    final_video = move_trimmed(out_dir)
    if not final_video:
        raise RuntimeError("final_video_missing")
    media_info = get_mp4_media_info(final_video)
    actual_duration_s = media_info["duration_s"]
    if actual_duration_s is None:
        raise RuntimeError("final_video_duration_unavailable")
    actual_duration_s = round(float(actual_duration_s), 3)

    # 从 injection_log 提取 timing
    log = getattr(adapter, "_injection_log", []) or []

    run_time = adapter.job_start_time_utc or job_start_time_utc
    manifest = {
        "job_id": job_id,
        "case_id": job["case_id"] if job else "",
        "split": job["split"] if job else "",
        "phase": job["phase"] if job else "pilot",
        "attempt_id": (
            f"attempt_{int(attempt_id[8:]) + adapter._video_wait_retry_count:03d}"
        ),
        "model_id": MODEL_ID,
        "model_name": "Odyssey",
        "model_version": None,
        "run_time_utc": run_time,
        "target_duration_s": job["duration_s"] if job else 0,
        "settings": {
            "resolution": media_info["resolution"],
            "fps": media_info["fps"],
            "audio_enabled": media_info["audio_enabled"],
            "seed": None,
        },
        "timing": {
            "job_start_time": run_time,
            "initial_prompt_time_s": adapter.initial_prompt_time_s,
            "first_video_chunk_time_s": adapter.first_video_chunk_time_s,
            "generation_complete_time_s": adapter.generation_complete_time_s,
        },
        "final_video": "final_video.mp4",
        "actual_duration_s": actual_duration_s,
        "native_chunks_observable": False,
        "status": "success",
        "failure_reason": None,
        "retry_reason": getattr(adapter, "_retry_reason", None),
        "notes": build_success_notes(adapter),
    }
    with open(os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    # chunk_events: Odyssey 也拿不到原生 chunks → 空文件
    with open(os.path.join(out_dir, "chunk_events.jsonl"), "w", encoding="utf-8") as f:
        pass

    # prompt_events（spec 格式）
    if log:
        with open(os.path.join(out_dir, "prompt_events.jsonl"), "w", encoding="utf-8") as f:
            for entry in log:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def select_work_items(args):
    if args.subset:
        work_items = prepare_subset_work_items(
            args.subset, SUBSET_YAML_DIR, args.job
        )
    else:
        files = sorted(
            filename
            for filename in os.listdir(YAML_DIR)
            if filename.endswith(".yaml")
        )
        if args.job:
            files = [f for f in files if args.job in f]

        if args.phase == "pilot":
            phase_ids = {j["job_id"] for j in json.load(open(os.path.join(BENCH_DIR, "pilot_jobs.json")))["jobs"]}
            files = [f for f in files if yaml_to_job_id(f) in phase_ids]
        elif args.phase == "remain":
            phase_ids = {j["job_id"] for j in json.load(open(os.path.join(BENCH_DIR, "remain_jobs.json")))["jobs"]}
            files = [f for f in files if yaml_to_job_id(f) in phase_ids]

        work_items = [
            (
                filename,
                load_job_source("pilot", yaml_to_job_id(filename))
                or load_job_source("remain", yaml_to_job_id(filename)),
            )
            for filename in files
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-restore",
                "--use-fake-ui-for-media-stream",
                "--start-maximized",
                "--start-fullscreen",
                "--window-size=2560,1440",
            ],
        )
        context = await browser.new_context(
            viewport=BROWSER_SIZE,
            screen=BROWSER_SIZE,
            device_scale_factor=1,
        )
        page = await context.new_page()
        actual_viewport = await page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight, "
            "screenWidth: screen.width, screenHeight: screen.height})"
        )
        print(f"Browser display: {actual_viewport}")

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
            skip_marker_path = os.path.join(out_dir, "skip_job.json")
            if os.path.exists(skip_marker_path):
                print(f"\n=== {job_id} ===  [SKIP] 已标记不重试")
                continue

            video_exists = os.path.exists(os.path.join(out_dir, "final_video.mp4"))
            if os.path.exists(manifest_path) and video_exists:
                with open(manifest_path, encoding="utf-8") as mf:
                    m = json.load(mf)
                if m.get("status") == "success":
                    print(f"\n=== {job_id} ===  [SKIP] 已完成")
                    continue

            print(f"\n=== {job_id} ===")
            attempt_id = prepare_attempt(out_dir)
            job_start_time_utc = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            job_start_monotonic = time.monotonic()
            t0 = time.time()
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
                )
                ok_count += 1
                print(f"  OK ({time.time() - t0:.0f}s)")
            except Exception as e:
                write_failed_manifest(
                    out_dir, job_id, job, attempt_id, job_start_time_utc, e
                )
                print(f"  FAIL: {e}")

        print(f"\nDone: {ok_count}/{len(work_items)} OK")
        await browser.close()


def main():
    parser = argparse.ArgumentParser(description="Odyssey benchmark 批量运行器")
    parser.add_argument("--job", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--phase", choices=["pilot", "remain", "all"], default="pilot",
                        help="pilot（50 个，默认）| remain（490 个）| all（540 个）")
    duration_group = parser.add_mutually_exclusive_group()
    for duration_s in (30, 60, 120):
        duration_group.add_argument(
            f"--{duration_s}",
            dest="duration_filter",
            action="store_const",
            const=duration_s,
            help=f"只运行当前 phase 中 duration_s={duration_s} 的任务",
        )
    duration_group.add_argument(
        "--30+60",
        dest="duration_filter",
        action="store_const",
        const=(30, 60),
        help="只运行当前 phase 中 duration_s=30 或 60 的任务",
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
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
