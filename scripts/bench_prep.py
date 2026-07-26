"""StreamAVBench job → PromptChoreo YAML timeline 转换器。"""
import argparse, json, os, sys
import yaml

BENCH_DIR = "StreamAVBench_closed_source_web_package/StreamAVBench_closed_source_web_package"
OUTPUT_DIR = "bench_yamls"

def load_jobs(phase="pilot"):
    path = os.path.join(BENCH_DIR, f"{phase}_jobs.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["jobs"]

def job_to_dict(job):
    schedule = job["prompt_schedule"]
    initial = next((p for p in schedule if p["role"] == "initial"), schedule[0])
    updates = [p for p in schedule if p["role"] == "update"]

    d = {"recorder": {"enabled": True, "start_hotkey": "ctrl+f1", "stop_hotkey": "ctrl+f2"}}

    if updates:
        d["initial_prompt"] = initial["text"]
        d["end_delay"] = 10
        d["events"] = []
        for u in updates:
            d["events"].append({"time": u["activation_media_time_s"], "prompt": u["text"], "label": f"t={u['activation_media_time_s']}s"})
    else:
        d["initial_prompt"] = initial["text"]
        d["end_delay"] = job["duration_s"]
        d["events"] = [{"time": 0, "prompt": initial["text"], "label": "initial"}]

    return d

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="pilot", choices=["pilot", "remain"])
    parser.add_argument("--track", default=None, choices=["A", "B"])
    parser.add_argument("--split", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    jobs = load_jobs(args.phase)
    if args.track:
        jobs = [j for j in jobs if j["track"] == args.track]
    if args.split:
        jobs = [j for j in jobs if j["split"] == args.split]

    print(f"Phase: {args.phase}, Jobs: {len(jobs)}")
    if args.dry_run:
        for j in jobs[:2]:
            print(yaml.dump(job_to_dict(j), allow_unicode=True, default_flow_style=False))
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for job in jobs:
        data = job_to_dict(job)
        fname = f"{job['job_id'].replace(':', '_')}.yaml"
        path = os.path.join(OUTPUT_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {job['job_id']} | track={job['track']} split={job['split']}\n")
            f.write(f"# output: {job.get('output_relpath', '')}\n")
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"  {fname}")

    print(f"\nDone → {OUTPUT_DIR}/ ({len(jobs)} files)")

if __name__ == "__main__":
    main()
