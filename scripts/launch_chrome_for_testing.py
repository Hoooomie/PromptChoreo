"""一键启动 Playwright 自带的 Chrome for Testing，供 PromptChoreo 以 CDP 连接模式使用。

用法::

    python scripts/launch_chrome_for_testing.py --site happy_oyster
    python scripts/launch_chrome_for_testing.py --user-data-dir ~/.workbuddy/browser_data --port 9222

启动后：
  1. 浏览器会保持打开（别关）。
  2. 手动按 F11 全屏（提高 EV 录屏分辨率）。
  3. 在 EV 录屏里把录制目标设为「Google Chrome for Testing」窗口。
  4. 运行：promptchoreo run examples/timeline_xxx.yaml --site xxx --cdp http://127.0.0.1:9222
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# 各站点对应的持久化 user-data-dir（与适配器保持一致，登录态才能带过来）
_BASE = Path.home() / ".workbuddy"
SITE_USER_DATA = {
    "happy_oyster": str(_BASE / "browser_data"),
    "pixverse": str(_BASE / "browser_data_pixverse"),
}

# 各站点的入口 URL（启动浏览器后自动打开，省去手动导航）
SITE_URLS = {
    "happy_oyster": "https://www.happyoyster.cn/create/directing",
    "odyssey": "https://experience.odyssey.ml/",
    "pixverse": "https://world.pixverse.ai/generate/",
}


def _find_chrome_exe() -> str:
    """返回 Playwright 自带的 Chrome for Testing 可执行文件路径。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return p.chromium.executable_path
    except Exception as e:
        raise SystemExit(
            f"找不到 Playwright 的 Chrome for Testing：{e}\n"
            "请先执行: playwright install chromium"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="启动 Chrome for Testing（供 PromptChoreo 以 CDP 连接）"
    )
    parser.add_argument(
        "--site", default="happy_oyster",
        choices=list(SITE_USER_DATA.keys()) + ["odyssey"],
        help="站点名（决定默认 user-data-dir；odyssey 不需要登录态，用默认目录）",
    )
    parser.add_argument(
        "--user-data-dir", default=None,
        help="自定义 user-data-dir（覆盖 --site 默认值）",
    )
    parser.add_argument(
        "--port", default=9222, type=int,
        help="远程调试端口（默认 9222）",
    )
    parser.add_argument(
        "--url", default=None,
        help="启动后打开的 URL（覆盖 --site 默认入口）",
    )
    parser.add_argument(
        "--mute", action="store_true",
        help="浏览器级静音（连同 WebAudio 一起静掉）。默认不加此旗标，"
             "以便能听到视频原声；仅在你想彻底静音时才加 --mute。",
    )
    args = parser.parse_args()

    user_data_dir = args.user_data_dir or SITE_USER_DATA.get(args.site)
    # odyssey 没有固定 user-data-dir：用一个专用目录，避免和系统 Chrome 冲突
    if user_data_dir is None:
        user_data_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "browser_data_odyssey"
        )

    exe = _find_chrome_exe()
    cdp_url = f"http://127.0.0.1:{args.port}"

    # 启动后自动打开对应站点入口；可用 --url 覆盖
    start_url = args.url or SITE_URLS.get(args.site, "")
    if not start_url:
        raise SystemExit(f"未知站点「{args.site}」，请用 --url 指定要打开的地址")

    cmd = [
        exe,
        f"--remote-debugging-port={args.port}",
        # 现代 Chrome 要求显式放行 CDP 来源，否则 connect_over_cdp 会被拒绝
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        # 默认不加 --mute-audio：用户希望听到视频原声。仅当显式 --mute 时才静音。
        *(["--mute-audio"] if args.mute else []),
        start_url,  # 作为位置参数传入，Chrome 会直接打开该 URL
    ]

    print(f"[launch] Chrome for Testing: {exe}")
    print(f"[launch] user-data-dir:     {user_data_dir}")
    print(f"[launch] 启动 URL:          {start_url}")
    print(f"[launch] CDP 地址:          {cdp_url}")
    print("[launch] 浏览器启动中（请保持窗口打开）...")
    # 分离进程：脚本退出后浏览器继续独立运行
    subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS)
    print("\n下一步：")
    print("  1) 手动按 F11 把窗口全屏（EV 录屏分辨率更高）")
    print("  2) 在 EV 录屏中把录制目标设为「Google Chrome for Testing」窗口")
    print(
        f"  3) 运行：promptchoreo run examples/timeline_{args.site}.yaml "
        f"--site {args.site} --cdp {cdp_url}"
    )


if __name__ == "__main__":
    main()
