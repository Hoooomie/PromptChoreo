#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁剪 PixVerse 原始录屏 -> 成片。

输入: video/pv  (Playwright 直接录制的原始 webm，未裁剪)
输出: tvideo/pv (按 PixVerse 内容区裁剪后的 mp4)

PixVerse 播放器四周常有黑/暗边与工具栏，用 cropdetect 自动检测内容包围盒裁剪，
参数与 HappyOyster 类似（limit=30, round=16），但采样点不同。与另外两个脚本
相互独立，裁剪参数各自可调。

用法:
    python scripts/trim_pixverse.py
    python scripts/trim_pixverse.py --input video/pv --output tvideo/pv
    python scripts/trim_pixverse.py --crop 2560:1440:0:0
    python scripts/trim_pixverse.py --ss 2 --to 75
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

CONFIG = {
    "input": "outputs/video/pv",
    "output": "outputs/tvideo/pv",
    "limit": 30,          # cropdetect 亮度阈值（PixVerse 四周有暗边/工具栏）
    "round": 16,          # 裁剪尺寸对齐
    "samples": [5, 15, 25],   # 在视频这些秒处采样（跳过开场加载画面）
    "site": "PixVerse",
}

VIDEO_EXTS = (".webm", ".mp4", ".mkv", ".mov", ".avi")


def get_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def get_video_size(ffmpeg: str, video: str):
    """解析视频分辨率 (width, height)，失败返回 None。"""
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", video], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
        m = re.search(r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})", proc.stderr)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def detect_crop(ffmpeg: str, video: str, samples, limit, round_n):
    """在多个采样点跑 cropdetect，返回面积最小（最紧贴内容）的包围盒 W:H:X:Y。"""
    crops = []
    for sp in samples:
        cmd = [
            ffmpeg, "-y", "-ss", str(sp), "-i", video, "-t", "2",
            "-vf", f"cropdetect=limit={limit}:round={round_n}",
            "-f", "null", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
        except Exception:
            continue
        for line in proc.stderr.splitlines():
            if "crop=" in line:
                m = line.rsplit("crop=", 1)[-1].strip()
                parts = m.split(":")
                if len(parts) == 4:
                    try:
                        crops.append(tuple(int(p) for p in parts))
                    except ValueError:
                        pass
    if not crops:
        return None
    return min(crops, key=lambda c: c[0] * c[1])


def even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def _run(ffmpeg, cmd, timeout=600):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def process(ffmpeg, src, dst, crop, ss, to):
    vf = None
    if crop:
        w, h, x, y = [even(int(v)) for v in crop]
        vf = f"crop={w}:{h}:{x}:{y}"

    cmd = [ffmpeg, "-y"]
    if ss is not None:
        cmd += ["-ss", str(ss)]
    cmd += ["-i", src]
    if to is not None:
        cmd += ["-t", str(max(0.1, to - (ss or 0)))]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", dst]

    proc = _run(ffmpeg, cmd)
    if proc is None:
        print("    [失败] 超时")
        return False
    if proc.returncode != 0:
        if "-c:a" in cmd:
            cmd2 = [c for c in cmd if c not in ("-c:a", "aac")] + ["-an"]
            proc2 = _run(ffmpeg, cmd2)
            if proc2 and proc2.returncode == 0:
                return True
        print("    [失败] " + (proc.stderr[-300:] if proc.stderr else "未知错误"))
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description=f"裁剪 {CONFIG['site']} 录屏")
    ap.add_argument("--input", default=CONFIG["input"])
    ap.add_argument("--output", default=CONFIG["output"])
    ap.add_argument("--crop", default=None, help="手动裁剪区 W:H:X:Y，跳过自动检测")
    ap.add_argument("--ss", type=float, default=None, help="起始秒（时间裁剪）")
    ap.add_argument("--to", type=float, default=None, help="结束秒（时间裁剪）")
    args = ap.parse_args()

    if not os.path.isdir(args.input):
        print(f"[错误] 输入目录不存在: {args.input}")
        sys.exit(1)
    os.makedirs(args.output, exist_ok=True)

    ffmpeg = get_ffmpeg()
    files = sorted(f for f in os.listdir(args.input) if f.lower().endswith(VIDEO_EXTS))
    if not files:
        print(f"[提示] {args.input} 下没有视频文件")
        return

    print(f"=== {CONFIG['site']} 裁剪 ===  ffmpeg: {ffmpeg}")
    for f in files:
        src = os.path.join(args.input, f)
        name, _ = os.path.splitext(f)
        dst = os.path.join(args.output, name + ".mp4")
        print(f"\n--- {f} -> {os.path.basename(dst)}")
        if os.path.exists(dst):
            print("    已存在，跳过")
            continue

        crop = None
        if args.crop:
            crop = tuple(args.crop.split(":"))
            print(f"    使用手动裁剪区: {args.crop}")
        else:
            crop = detect_crop(ffmpeg, src, CONFIG["samples"], CONFIG["limit"], CONFIG["round"])
            if crop:
                w, h, x, y = crop
                size = get_video_size(ffmpeg, src)
                if size and w >= size[0] - 4 and h >= size[1] - 4:
                    print(f"    检测为全屏({w}x{h})，无需裁剪，直接转码")
                    crop = None
                else:
                    print(f"    自动裁剪区: {w}:{h}:{x}:{y}")
            else:
                print("    未检测到裁剪区，直接转码")

        ok = process(ffmpeg, src, dst, crop, args.ss, args.to)
        print("    [完成]" if ok else "    [失败]")

    print(f"\n全部完成 -> {args.output}")


if __name__ == "__main__":
    main()
