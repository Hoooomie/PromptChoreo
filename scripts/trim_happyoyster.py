#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁剪 HappyOyster 原始录屏 -> 成片。

输入: outputs/video/ho  (EV 录屏原始文件)
输出: outputs/tvideo/ho (按 HappyOyster 内容区裁剪后的 mp4)

HappyOyster 视频 ~832x480 居中于页面。两步裁：先削顶部浏览器栏，再裁视频。

用法:
    python scripts/trim_happyoyster.py
    python scripts/trim_happyoyster.py --input outputs/video/ho --output outputs/tvideo/ho
    python scripts/trim_happyoyster.py --crop W:H:X:Y
    python scripts/trim_happyoyster.py --ss 1 --to 120
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys

CONFIG = {
    "input": "outputs/video/ho",
    "output": "outputs/tvideo/ho",
    "limit": 24,
    "round": 2,
    "samples": [10, 20, 30],
    "site": "HappyOyster",
}

# ── 裁剪参数（两步法） ──
# 第 1 步：削掉顶部浏览器栏（Chrome 地址栏/标签页），
#         然后视频在剩余区域里是真正居中的。
# 第 2 步：居中裁剪视频。
CHROME_TOP_RATIO = 0.11     # 浏览器栏高度 / 屏幕高（先削掉）
HO_VIDEO_W_RATIO = 0.85     # 视频宽 / 屏幕宽 (832/1920)
HO_VIDEO_H_RATIO = 1.0     # 视频高 / 屏幕高（1.0 = 不裁上下，保留全部高度）

VIDEO_EXTS = (".webm", ".mp4", ".mkv", ".mov", ".avi")


def get_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def get_video_size(ffmpeg: str, video: str):
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


def detect_crop(ffmpeg: str, video: str, samples, limit, round_n):
    """cropdetect 兜底：在多个采样点跑，返回面积最小的包围盒 W:H:X:Y。"""
    crops = []
    for sp in samples:
        cmd = [
            ffmpeg, "-y", "-ss", str(sp), "-i", video, "-t", "2",
            "-vf", f"cropdetect=limit={limit}:round={round_n}",
            "-f", "null", "-",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", timeout=60,
            )
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


def fixed_crop(width: int, height: int) -> str:
    """两步裁：先削浏览器栏，再按比例裁视频。返回 ffmpeg 滤镜链。"""
    # 第 1 步：削顶部浏览器栏
    top = even(int(height * CHROME_TOP_RATIO))
    rem_h = height - top
    # 第 2 步：裁视频
    vw = even(int(width * HO_VIDEO_W_RATIO))
    vx = even((width - vw) // 2)

    if HO_VIDEO_H_RATIO >= 1.0:
        # 不裁上下，保留全部高度
        vh = rem_h
        vy = 0
    else:
        vh = even(int(height * HO_VIDEO_H_RATIO))
        vy = even((rem_h - vh) // 2)

    strip = f"crop={width}:{rem_h}:0:{top}"          # 削栏
    zoom = f"crop={vw}:{vh}:{vx}:{vy}"               # 裁视频
    return f"{strip},{zoom}"


def _run(ffmpeg, cmd, timeout=600):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def process(ffmpeg, src, dst, crop, ss, to):
    vf = None
    if crop:
        if isinstance(crop, str):
            vf = crop
        else:
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
    cmd += [
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
        dst,
    ]

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
    ap.add_argument(
        "--crop", default=None,
        help="手动裁剪区 W:H:X:Y，跳过固定比例与自动检测",
    )
    ap.add_argument("--ss", type=float, default=None, help="起始秒（时间裁剪）")
    ap.add_argument("--to", type=float, default=None, help="结束秒（时间裁剪）")
    args = ap.parse_args()

    if not os.path.isdir(args.input):
        print(f"[错误] 输入目录不存在: {args.input}")
        sys.exit(1)
    os.makedirs(args.output, exist_ok=True)

    ffmpeg = get_ffmpeg()
    files = sorted(
        f for f in os.listdir(args.input) if f.lower().endswith(VIDEO_EXTS)
    )
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
            size = get_video_size(ffmpeg, src)
            if size:
                crop = fixed_crop(size[0], size[1])
                top = even(int(size[1] * CHROME_TOP_RATIO))
                vw = even(int(size[0] * HO_VIDEO_W_RATIO))
                vh = even(int(size[1] * HO_VIDEO_H_RATIO))
                print(
                    f"    两步裁剪: 削顶栏{top}px → 裁视频{vw}x{vh}居中  "
                    f"(源 {size[0]}x{size[1]})"
                )
            else:
                print("    无法解析分辨率，回退 cropdetect")
                crop = detect_crop(
                    ffmpeg, src, CONFIG["samples"],
                    CONFIG["limit"], CONFIG["round"],
                )
                if crop:
                    w, h, x, y = crop
                    print(f"    自动裁剪区: {w}x{h}@({x},{y})")
                else:
                    print("    未检测到裁剪区，直接转码")

        ok = process(ffmpeg, src, dst, crop, args.ss, args.to)
        print("    [完成]" if ok else "    [失败]")

    print(f"\n全部完成 -> {args.output}")


if __name__ == "__main__":
    main()
