"""Benchmark 批量运行器（进程内循环，浏览器不关）。"""
import argparse, asyncio, json, os, shutil, sys, time
from datetime import datetime, timezone

import yaml
from playwright.async_api import async_playwright

# 项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.promptchoreo.core.scheduler import Scheduler
from src.promptchoreo.core.timeline import Timeline
from src.promptchoreo.core.media import get_mp4_media_info
from src.promptchoreo.adapters.pixverse import PixVerseAdapter

BENCH_DIR = "StreamAVBench_closed_source_web_package/StreamAVBench_closed_source_web_package"
YAML_DIR = "bench_yamls"
OUTPUT_BASE = "outputs"
MODEL_ID = "pixverse_r1"
VIDEO_SRC = "outputs/video/pv"

def load_job_source(phase, job_id):
    path = os.path.join(BENCH_DIR, f"{phase}_jobs.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for j in data["jobs"]:
        if j["job_id"] == job_id:
            return j
    return None

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
    manifest = {
        "job_id": job_id,
        "case_id": job["case_id"] if job else "",
        "split": job["split"] if job else "",
        "phase": job["phase"] if job else "pilot",
        "attempt_id": attempt_id,
        "model_id": MODEL_ID,
        "model_name": "PixVerse R1",
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
            "initial_prompt_time_s": None,
            "first_video_chunk_time_s": None,
            "generation_complete_time_s": None,
        },
        "final_video": "final_video.mp4" if video_exists else None,
        "actual_duration_s": round(float(duration), 3) if duration is not None else None,
        "native_chunks_observable": False,
        "status": "partial" if video_exists else "failed",
        "failure_reason": f"automation_error: {type(exc).__name__}: {exc}",
        "retry_reason": None,
        "notes": "Final video, when present, is an external screen recording.",
    }
    with open(os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    for filename in ("prompt_events.jsonl", "chunk_events.jsonl"):
        path = os.path.join(out_dir, filename)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8"):
                pass


def clear_video_dir():
    if not os.path.isdir(VIDEO_SRC):
        return
    for f in os.listdir(VIDEO_SRC):
        if f.endswith((".mp4", ".webm", ".mkv", ".mov")):
            path = os.path.join(VIDEO_SRC, f)
            for attempt in range(10):
                try:
                    os.remove(path)
                    break
                except OSError:
                    if attempt == 9:
                        raise
                    time.sleep(1)

def move_recording(job_id, out_dir):
    if not os.path.isdir(VIDEO_SRC):
        return None
    files = [f for f in os.listdir(VIDEO_SRC) if f.endswith((".mp4", ".webm", ".mkv", ".mov"))]
    if not files:
        print("  [WARN] video/ 下无录屏文件")
        return
    src = os.path.join(VIDEO_SRC, files[0])
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "final_video.mp4")
    # EV 停止后需要时间释放文件句柄，重试最多 30 秒
    for attempt in range(30):
        try:
            os.rename(src, dst)
            print(f"  [VIDEO] {dst}")
            return dst
        except OSError:
            if attempt == 29:
                raise
            time.sleep(1)

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
    # 清空录屏目录
    clear_video_dir()

    # 加载 YAML
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # 从 job source 的 prompt_schedule 构建注入事件（带 prompt_id/role）
    prompt_schedule = job.get("prompt_schedule", []) if job else []
    inject_events = []
    for ps in prompt_schedule:
        if ps.get("role") == "initial":
            continue  # initial prompt 由 adapter 在 _start_session 处理
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
        "_job_start_time_utc": job_start_time_utc,
        "_job_start_monotonic": job_start_monotonic,
        "bgm_off": True,
    }
    rec = raw.get("recorder") or {}
    if rec.get("enabled"):
        config["_recorder_enabled"] = True
        config["_recorder_start_hotkey"] = rec.get("start_hotkey", "ctrl+f1")
        config["_recorder_stop_hotkey"] = rec.get("stop_hotkey", "ctrl+f2")

    adapter = PixVerseAdapter(config)
    adapter._session_started = False
    await adapter.setup(page)

    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "chunks"), exist_ok=True)

    # 从 injection_log 提取 timing
    log = getattr(adapter, "_injection_log", []) or []

    final_video = move_recording(job_id, out_dir)
    if not final_video:
        raise RuntimeError("final_video_missing")
    media_info = get_mp4_media_info(final_video)
    actual_duration_s = media_info["duration_s"]
    if actual_duration_s is None:
        raise RuntimeError("final_video_duration_unavailable")
    actual_duration_s = round(float(actual_duration_s), 3)

    run_time = adapter.job_start_time_utc or job_start_time_utc
    manifest = {
        "job_id": job_id,
        "case_id": job["case_id"] if job else "",
        "split": job["split"] if job else "",
        "phase": job["phase"] if job else "pilot",
        "attempt_id": attempt_id,
        "model_id": MODEL_ID,
        "model_name": "PixVerse R1",
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
        "retry_reason": None,
        "notes": "Final video is an external screen recording.",
    }
    with open(os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    # chunk_events: PixVerse 拿不到原生 chunks → 空文件
    with open(os.path.join(out_dir, "chunk_events.jsonl"), "w", encoding="utf-8") as f:
        pass

    # prompt_events（实录注入时间，spec 格式）
    if log:
        with open(os.path.join(out_dir, "prompt_events.jsonl"), "w", encoding="utf-8") as f:
            for entry in log:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

async def main_async(args):
    files = sorted(os.listdir(YAML_DIR))
    if args.job:
        files = [f for f in files if args.job in f]

    if args.phase == "pilot":
        phase_ids = {j["job_id"] for j in json.load(open(os.path.join(BENCH_DIR, "pilot_jobs.json")))["jobs"]}
        files = [f for f in files if yaml_to_job_id(f) in phase_ids]
    elif args.phase == "remain":
        phase_ids = {j["job_id"] for j in json.load(open(os.path.join(BENCH_DIR, "remain_jobs.json")))["jobs"]}
        files = [f for f in files if yaml_to_job_id(f) in phase_ids]

    print(f"Jobs: {len(files)}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            PixVerseAdapter.user_data_dir,
            headless=False,
            viewport={"width": 2560, "height": 1440},
            args=["--no-restore", "--use-fake-ui-for-media-stream", "--start-maximized"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        ok_count = 0
        for f in files:
            job_id = yaml_to_job_id(f)
            job = load_job_source("pilot", job_id) or load_job_source("remain", job_id)
            if args.dry_run:
                print(f"  [DRY] {job_id}")
                continue

            out_dir = os.path.join(OUTPUT_BASE, MODEL_ID, job["phase"] if job else "pilot", job_id.replace(":", "_"))
            manifest_path = os.path.join(out_dir, "run_manifest.json")
            video_exists = any(
                f.startswith("final_video.") for f in (os.listdir(out_dir) if os.path.isdir(out_dir) else [])
            )
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
                    os.path.join(YAML_DIR, f),
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

        print(f"\nDone: {ok_count}/{len(files)} OK")
        await context.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--phase", choices=["pilot", "remain", "all"], default="pilot",
                        help="pilot（50 个，默认）| remain（490 个）| all（540 个）")
    args = parser.parse_args()
    asyncio.run(main_async(args))

if __name__ == "__main__":
    main()
