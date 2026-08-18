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
from src.promptchoreo.adapters.pixverse import (
    PixVerseAdapter,
    PixVerseContentPolicyRejection,
)
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
COMPLETED_JOBS_PATH = os.path.join(
    OUTPUT_BASE, MODEL_ID, "completed_jobs.json"
)
ARCHIVED_RUN_ROOT = os.path.join(OUTPUT_BASE, MODEL_ID, "跑过")
TARGET_DURATION_S = 180
DEFAULT_NEW_BENCH_SHUFFLE_SEED = 20260816
NEW_BENCH_FILENAME_RE = re.compile(
    r"^(?P<track>[IP])-(?P<index>\d+)_(?P=track)-180\.yaml$"
)


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


def find_content_policy_rejection(out_dir):
    """Find a PixVerse content-policy rejection in current or prior attempts."""
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
        if manifest.get("status") == "failed" and reason.startswith(
            "content_policy_rejection:"
        ):
            return reason
    return None


def find_job_content_policy_rejection(out_dir, job_id):
    """Find policy rejection in active or manually archived job outputs."""
    for candidate in job_output_candidates(out_dir, job_id):
        reason = find_content_policy_rejection(candidate)
        if reason:
            return candidate, reason
    return None, None


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
            "skip_job.json",
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
    content_policy_rejection = isinstance(
        exc, PixVerseContentPolicyRejection
    )
    failure_reason = (
        str(exc)
        if content_policy_rejection
        else f"automation_error: {type(exc).__name__}: {exc}"
    )
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
            "initial_prompt_time_s": getattr(
                exc, "initial_prompt_time_s", None
            ),
            "first_video_chunk_time_s": None,
            "generation_complete_time_s": None,
        },
        "final_video": "final_video.mp4" if video_exists else None,
        "actual_duration_s": round(float(duration), 3) if duration is not None else None,
        "native_chunks_observable": False,
        "status": (
            "failed"
            if content_policy_rejection or not video_exists
            else "partial"
        ),
        "failure_reason": failure_reason,
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


def write_content_policy_skip_marker(
    out_dir, job_id, exc, recorded_at_utc=None
):
    """Permanently skip an initial prompt rejected by PixVerse."""
    if not isinstance(exc, PixVerseContentPolicyRejection):
        raise TypeError("content-policy skip marker requires policy rejection")
    os.makedirs(out_dir, exist_ok=True)
    recorded_at_utc = recorded_at_utc or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    marker = {
        "job_id": job_id,
        "model_id": MODEL_ID,
        "status": "failed",
        "skip_future_runs": True,
        "retryable": False,
        "failure_reason": str(exc),
        "evidence": exc.evidence,
        "recorded_at_utc": recorded_at_utc,
    }
    path = os.path.join(out_dir, "skip_job.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(marker, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


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
        filename
        for filename in os.listdir(YAML_DIR)
        if filename.endswith(".yaml")
    )
    requested_filenames = _requested_job_filenames(args)
    if requested_filenames:
        files = [
            filename for filename in files if filename in requested_filenames
        ]
    elif args.job:
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
    if (
        args.phase not in ("progressive", "interactive", "new")
        or explicit_job_selection(args)
    ):
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
    completed_job_ids = load_completed_job_ids()
    force_rerun = bool(getattr(args, "force_rerun", False))
    print(f"Jobs: {len(work_items)}")
    if run_list_path:
        print(f"Run list: {run_list_path}")
    print(f"Completed registry: {len(completed_job_ids)} successful jobs")
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
            _, policy_rejection = find_job_content_policy_rejection(
                out_dir, job_id
            )
            if skip_marker_path or policy_rejection:
                print(
                    f"  [DRY-SKIP] {job_id}: "
                    "已标记不重试或内容策略失败（含跑过目录）"
                )
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

            policy_dir, policy_rejection = find_job_content_policy_rejection(
                out_dir, job_id
            )
            if not force_rerun and policy_rejection:
                print(
                    f"\n=== {job_id} ===  "
                    "[SKIP] 已标记内容策略失败: "
                    f"{policy_dir}"
                )
                continue

            print(f"\n=== {job_id} ===")
            if force_rerun:
                print("  [RERUN] 已显式要求重跑，忽略旧成功/跳过记录")
                if unmark_job_completed(job_id):
                    completed_job_ids.discard(job_id)
                    print("  [RERUN] 已从成功账本移除旧记录")
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
            except Exception as e:
                write_failed_manifest(
                    out_dir, job_id, job, attempt_id, job_start_time_utc, e
                )
                if isinstance(e, PixVerseContentPolicyRejection):
                    marker_path = write_content_policy_skip_marker(
                        out_dir, job_id, e
                    )
                    print(f"  [CONTENT POLICY] {e.evidence}")
                    print(
                        "  FAIL: initial prompt rejected; "
                        f"marked non-retryable -> {marker_path}"
                    )
                else:
                    print(f"  FAIL: {e}")
            else:
                registry_path = mark_job_completed(job_id)
                completed_job_ids.add(job_id)
                ok_count += 1
                print(f"  [COMPLETED] 成功账本已更新: {registry_path}")
                print(f"  OK ({time.time() - t0:.0f}s)")

        print(f"\nDone: {ok_count}/{len(work_items)} OK")
        await context.close()

def main():
    parser = argparse.ArgumentParser(
        description="PixVerse StreamAVBench runner"
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
    if args.force_rerun and not explicit_job_selection(args):
        parser.error("--force-rerun 必须与 --job 或 --jobs 一起使用")
    if args.subset and args.jobs:
        parser.error("--jobs 不支持与 --subset 一起使用")
    asyncio.run(main_async(args))

if __name__ == "__main__":
    main()
