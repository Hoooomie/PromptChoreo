"""PixVerse benchmark 批量运行器（进程内循环，浏览器不关）。

新 benchmark 的 Progressive / Interactive 输入由
``scripts/new_bench_prep.py`` 生成；PixVerse 继续复用同一个登录 profile，
不做账号轮换。
"""
import argparse, asyncio, json, os, random, re, shutil, sys, time
from datetime import datetime, timezone

import yaml
from playwright.async_api import async_playwright

# 项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.promptchoreo.core.scheduler import Scheduler
from src.promptchoreo.core.timeline import Timeline
from src.promptchoreo.core.media import get_mp4_media_info
from src.promptchoreo.adapters.pixverse import PixVerseAdapter
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
MODEL_ID = "pixverse_r1"
VIDEO_SRC = "outputs/video/pv"
TARGET_DURATION_S = 180
DEFAULT_NEW_BENCH_SHUFFLE_SEED = 20260816
NEW_BENCH_FILENAME_RE = re.compile(
    r"^(?P<track>[IP])-(?P<index>\d+)_(?P=track)-180\.yaml$"
)

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


def build_prompt_inputs(job, raw):
    """Build adapter inputs from either legacy job JSON or new-bench YAML."""
    prompt_schedule = list(job.get("prompt_schedule", []) if job else [])
    if prompt_schedule:
        initial_prompt = next(
            (
                prompt.get("text", "")
                for prompt in prompt_schedule
                if prompt.get("role") == "initial"
            ),
            raw.get("initial_prompt", ""),
        )
        inject_events = [
            {
                "time": float(prompt["activation_media_time_s"]),
                "prompt": prompt.get("text", ""),
                "prompt_id": prompt.get("prompt_id", ""),
                "role": prompt.get("role", "update"),
            }
            for prompt in prompt_schedule
            if prompt.get("role") != "initial"
        ]
        return initial_prompt, prompt_schedule, inject_events

    initial_prompt = raw.get("initial_prompt", "")
    inject_events = [
        {
            "time": float(event.get("time", 0)),
            "prompt": event.get("prompt", ""),
            "prompt_id": event.get("prompt_id", ""),
            "role": event.get("role", "update"),
        }
        for event in (raw.get("events") or [])
        if float(event.get("time", 0) or 0) > 0
    ]
    prompt_schedule = [
        {
            "activation_media_time_s": 0.0,
            "text": initial_prompt,
            "prompt_id": "",
            "role": "initial",
        }
    ] + [
        {
            "activation_media_time_s": event["time"],
            "text": event["prompt"],
            "prompt_id": event["prompt_id"],
            "role": event["role"],
        }
        for event in inject_events
    ]
    return initial_prompt, prompt_schedule, inject_events


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

    initial_prompt, prompt_schedule, inject_events = build_prompt_inputs(
        job, raw
    )
    if inject_events:
        print(
            "  [PLAN] Injections: "
            + ", ".join(f"{event['time']:.0f}s" for event in inject_events)
        )

    config = {
        "initial_prompt": initial_prompt,
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

def order_new_bench_files(files, phase, seed):
    """Return a reproducible new-benchmark order.

    Combined runs keep matching Interactive / Progressive scenarios adjacent,
    in I-then-P order, while shuffling scenario numbers.
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
    """Derive manifest metadata for a generated new-benchmark YAML file."""
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
        "duration_s": TARGET_DURATION_S,
    }


def select_files(args):
    files = sorted(
        filename
        for filename in os.listdir(YAML_DIR)
        if filename.endswith(".yaml")
    )
    if args.job:
        files = [filename for filename in files if args.job in filename]

    if args.phase in ("pilot", "remain"):
        source = os.path.join(BENCH_DIR, f"{args.phase}_jobs.json")
        with open(source, encoding="utf-8") as f:
            phase_ids = {job["job_id"] for job in json.load(f)["jobs"]}
        files = [
            filename
            for filename in files
            if yaml_to_job_id(filename) in phase_ids
        ]
    elif args.phase == "progressive":
        files = [filename for filename in files if filename.startswith("P-")]
    elif args.phase == "interactive":
        files = [filename for filename in files if filename.startswith("I-")]
    elif args.phase == "new":
        files = [
            filename
            for filename in files
            if filename.startswith(("I-", "P-"))
        ]

    if args.phase in ("progressive", "interactive", "new") and not args.job:
        files = order_new_bench_files(
            files,
            args.phase,
            getattr(args, "shuffle_seed", DEFAULT_NEW_BENCH_SHUFFLE_SEED),
        )
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
                or load_job_source("remain", yaml_to_job_id(filename))
                or new_bench_job(filename),
            )
            for filename in select_files(args)
        ]

    return filter_work_items_by_duration(work_items, args.duration_filter)


def write_new_bench_run_list(work_items, args):
    """Persist the exact shuffled order for auditing and resume checks."""
    if args.phase not in ("progressive", "interactive", "new") or args.job:
        return None
    seed = getattr(args, "shuffle_seed", DEFAULT_NEW_BENCH_SHUFFLE_SEED)
    run_list_dir = os.path.join(OUTPUT_BASE, MODEL_ID, "run_lists")
    os.makedirs(run_list_dir, exist_ok=True)
    path = os.path.join(run_list_dir, f"{args.phase}_seed_{seed}.json")
    payload = {
        "phase": args.phase,
        "shuffle_seed": seed,
        "count": len(work_items),
        "ordering": (
            "shuffle scenario numbers; for each number run Interactive then Progressive"
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


async def main_async(args):
    work_items = select_work_items(args)
    run_list_path = write_new_bench_run_list(work_items, args)
    print(f"Jobs: {len(work_items)}")
    if run_list_path:
        print(f"Run list: {run_list_path}")
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
            if os.path.exists(os.path.join(out_dir, "skip_job.json")):
                print(f"  [DRY-SKIP] {job_id}: 已标记不重试")
                continue
            print(f"  [DRY] {job_id} -> {out_dir}")
        return

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            PixVerseAdapter.user_data_dir,
            headless=False,
            viewport={"width": 2560, "height": 1440},
            args=["--no-restore", "--use-fake-ui-for-media-stream", "--start-maximized"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

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

            video_exists = any(
                f.startswith("final_video.") for f in (os.listdir(out_dir) if os.path.isdir(out_dir) else [])
            )
            if os.path.exists(manifest_path):
                with open(manifest_path, encoding="utf-8") as mf:
                    m = json.load(mf)
                content_policy_rejection = (
                    m.get("status") == "failed"
                    and str(m.get("failure_reason") or "").startswith(
                        "content_policy_rejection:"
                    )
                )
                if (
                    m.get("status") == "success" and video_exists
                ) or content_policy_rejection:
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
        await context.close()

def main():
    parser = argparse.ArgumentParser(
        description="PixVerse StreamAVBench runner"
    )
    parser.add_argument("--job", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--phase",
        choices=[
            "pilot",
            "remain",
            "progressive",
            "interactive",
            "new",
            "all",
        ],
        default="pilot",
        help=(
            "pilot（默认）| remain | progressive | interactive | "
            "new（全部新数据）| all"
        ),
    )
    duration_group = parser.add_mutually_exclusive_group()
    for duration_s in (30, 60, 120, 180):
        duration_group.add_argument(
            f"--{duration_s}",
            dest="duration_filter",
            action="store_const",
            const=duration_s,
            help=f"只运行当前 phase 中 duration_s={duration_s} 的任务",
        )
    parser.set_defaults(duration_filter=None)
    parser.add_argument(
        "--shuffle-seed",
        type=int,
        default=DEFAULT_NEW_BENCH_SHUFFLE_SEED,
        help="新数据集的固定乱序种子；相同种子会得到相同运行顺序",
    )
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
