#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁剪 Odyssey 原始录屏 -> 成片。

输入: outputs/video/od  (Playwright 直接录制的原始 webm，未裁剪)
输出: outputs/tvideo/od (按 Odyssey 内容区裁剪后的 mp4)

用法:
    python scripts/trim_odyssey.py
    python scripts/trim_odyssey.py --input outputs/video/od --output outputs/tvideo/od
    python scripts/trim_odyssey.py --crop W:H:X:Y
    python scripts/trim_odyssey.py --ss 1 --to 120
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys

CONFIG = {
    "input": "outputs/video/od",
    "output": "outputs/tvideo/od",
    "limit": 24,
    "round": 2,
    "samples": [3, 10, 20],
    "site": "Odyssey",
}

# ── 裁剪参数（两步法） ──
# 第 1 步：削掉顶部浏览器栏（白色 Chrome 地址栏/标签页），
#         然后视频在剩余区域里是真正居中的。
# 第 2 步：居中裁剪视频。
CHROME_TOP_RATIO = 0.13   # 浏览器栏高度 / 屏幕高（先削掉）
OD_VIDEO_W_RATIO = 0.31   # 视频宽 / 屏幕宽
OD_VIDEO_H_RATIO = 0.32   # 视频高 / 屏幕高（削掉栏之后自动居中，无需 X/Y 偏移）

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


def fixed_crop(width: int, height: int) -> tuple[int, int, int, int] | str:
    """两步裁：先削浏览器栏，再居中裁视频。返回裁剪参数供 process 使用。"""
    # 第 1 步：削顶部浏览器栏
    top = even(int(height * CHROME_TOP_RATIO))
    # 第 2 步：剩余区域内，视频居中
    vw = even(int(width * OD_VIDEO_W_RATIO))
    vh = even(int(height * OD_VIDEO_H_RATIO))
    rem_h = height - top          # 削掉栏之后的高度
    vx = even((width - vw) // 2)  # 水平居中
    vy = even((rem_h - vh) // 2)  # 垂直居中（相对剩余区域）

    # 返回两步 ffmpeg crop 链：用逗号连接
    strip = f"crop={width}:{rem_h}:0:{top}"          # 削栏
    zoom = f"crop={vw}:{vh}:{vw + 2 if vx < 0 else vx}:{vy}"  # 居中裁视频
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
            vf = crop  # 直接使用滤镜链（两步裁等）
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
                vw = even(int(size[0] * OD_VIDEO_W_RATIO))
                vh = even(int(size[1] * OD_VIDEO_H_RATIO))
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
