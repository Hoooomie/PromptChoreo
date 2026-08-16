#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载当前 PixVerse World 账号在 Mine 中生成的视频。

脚本直接读取 PixVerse 自己加载的作品列表响应，再下载响应中的媒体 URL；
不依赖卡片坐标、按钮文案或 Download 弹窗。

用法::

    python scripts/download_pv_videos.py
    python scripts/download_pv_videos.py --date 2026.07.19
    python scripts/download_pv_videos.py --all-sessions
    python scripts/download_pv_videos.py --dry-run

首次使用前请运行 ``python scripts/login_pixverse.py`` 保存登录态。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USER_DATA = Path.home() / ".workbuddy" / "browser_data_pixverse"
MINE_URL = "https://world.pixverse.video/discover/history/worlds"
GALLERY_URL = "https://world.pixverse.video/discover/gallery"
DEFAULT_DOWNLOADED_FILE = PROJECT_ROOT / ".downloaded_pv.json"
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "outputs" / "downloads" / "pixverse"

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DATE_PATTERN = re.compile(r"(?P<year>\d{4})[-./](?P<month>\d{1,2})[-./](?P<day>\d{1,2})")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class VideoRecord:
    """一个可下载的 PixVerse 视频。"""

    key: str
    url: str
    title: str
    date: str
    preset_id: str
    session_id: str | None = None

    @property
    def filename(self) -> str:
        suffix = Path(unquote(urlsplit(self.url).path)).suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            suffix = ".mp4"
        parts = [self.date or "unknown-date", self.title or "untitled", f"p{self.preset_id}"]
        if self.session_id:
            parts.append(f"s{self.session_id}")
        return sanitize_filename("_".join(parts), max_length=180 - len(suffix)) + suffix


@dataclass(frozen=True)
class ApiAccess:
    """复用 PixVerse 页面请求的 API 入口和认证头，不把凭据写入磁盘。"""

    base_url: str
    authorization: str | None = field(default=None, repr=False, compare=False)


def sanitize_filename(value: str, *, max_length: int = 180) -> str:
    """生成 Windows/macOS/Linux 都可接受的文件名主体。"""

    value = INVALID_FILENAME.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "untitled"
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or "untitled"


def normalize_date(value: Any) -> str:
    """将 PixVerse 时间或命令行日期统一为 YYYY.MM.DD。"""

    match = DATE_PATTERN.search(str(value or ""))
    if not match:
        return ""
    return (
        f"{int(match.group('year')):04d}."
        f"{int(match.group('month')):02d}."
        f"{int(match.group('day')):02d}"
    )


def unwrap_api_data(payload: Any) -> Any:
    """兼容 PixVerse 原始 ``{code, data}`` 和直接 data 两种响应。"""

    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        if "items" in current or "total" in current:
            break
        nested = next(
            (
                current[key]
                for key in ("data", "Data", "resp", "Resp", "result")
                if isinstance(current.get(key), (dict, list))
            ),
            None,
        )
        if nested is None:
            break
        current = nested
    return current


def collection_from_payload(payload: Any) -> tuple[list[dict[str, Any]], int]:
    data = unwrap_api_data(payload)
    if isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
        return items, len(items)
    if not isinstance(data, dict):
        return [], 0
    items = data.get("items") or data.get("list") or []
    normalized = [item for item in items if isinstance(item, dict)]
    try:
        total = int(data.get("total", len(normalized)))
    except (TypeError, ValueError):
        total = len(normalized)
    return normalized, total


def _stable_url_id(url: str) -> str:
    """忽略会轮换的签名 query，以媒体路径作为稳定 ID。"""

    parsed = urlsplit(url)
    stable = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]


def _video_url(item: dict[str, Any]) -> str:
    for key in ("final_video_url", "video_uri", "video_url", "url"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return ""


def video_records_for_preset(
    preset: dict[str, Any],
    sessions: list[dict[str, Any]] | None = None,
    source_session: dict[str, Any] | None = None,
    *,
    include_all_sessions: bool = False,
) -> list[VideoRecord]:
    """按官方前端的字段优先级提取一个 World 的下载链接。"""

    preset_id = str(preset.get("id") or preset.get("preset_id") or "unknown")
    title = str(preset.get("title") or preset.get("name") or "untitled").strip()
    date = normalize_date(
        preset.get("created_at")
        or preset.get("create_time")
        or preset.get("createdAt")
    )
    examples = preset.get("example_sessions")
    example_sessions = examples if isinstance(examples, list) else []

    candidates: list[tuple[str, str | None]] = []
    # 2026-08 起，新建 World 的顶层 video_uri 可能为空；真正成片只挂在
    # source_session_id 对应的 /rtg/db-session/{id}.final_video_url 上。
    primary = _video_url(preset) or _video_url(source_session or {})
    if primary:
        candidates.append((primary, None))

    session_items = [item for item in (sessions or []) if isinstance(item, dict)]
    if include_all_sessions:
        for item in [*example_sessions, *session_items]:
            url = _video_url(item)
            if url:
                session_id = item.get("session_id") or item.get("id")
                candidates.append((url, str(session_id) if session_id is not None else None))
    elif not candidates:
        for item in [*example_sessions, *session_items]:
            url = _video_url(item)
            if url:
                # 官方 World 下载也把第一条 example session 当作主视频。
                candidates.append((url, None))
                break

    records: list[VideoRecord] = []
    seen_urls: set[str] = set()
    for url, session_id in candidates:
        stable_id = _stable_url_id(url)
        if stable_id in seen_urls:
            continue
        seen_urls.add(stable_id)
        kind = f"session:{session_id}" if session_id else "primary"
        records.append(
            VideoRecord(
                key=f"pixverse:{preset_id}:{kind}:{stable_id}",
                url=url,
                title=title,
                date=date,
                preset_id=preset_id,
                session_id=session_id,
            )
        )
    return records


def load_downloaded(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[警告] 无法读取下载记录 {path}: {exc}", file=sys.stderr)
        return set()
    if isinstance(payload, list):
        return {str(item) for item in payload}
    if isinstance(payload, dict):
        values = payload.get("downloaded", payload.keys())
        return {str(item) for item in values}
    return set()


def save_downloaded(path: Path, downloaded: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(sorted(downloaded), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def reconcile_download_state(
    record: VideoRecord,
    downloaded: set[str],
    destination: Path,
) -> tuple[bool, bool]:
    """返回（是否跳过，是否移除了指向缺失文件的过期记录）。"""
    recorded_keys = {record.key}
    if not record.session_id:
        recorded_keys.add(f"{record.date}_{record.title}")

    matching_keys = recorded_keys & downloaded
    if not matching_keys:
        return False, False
    if destination.exists():
        return True, False

    downloaded.difference_update(matching_keys)
    return False, True


def _response_page_number(response: Response) -> int | None:
    parsed = urlsplit(response.url)
    if parsed.path.rstrip("/") != "/api/my-presets":
        return None
    values = parse_qs(parsed.query).get("page", ["1"])
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


async def _json_response(response: Response) -> Any:
    if not response.ok:
        raise RuntimeError(f"PixVerse API 返回 HTTP {response.status}: {response.url}")
    try:
        return await response.json()
    except Exception as exc:
        raise RuntimeError(f"PixVerse API 未返回 JSON: {response.url}") from exc


def _mine_error(page: Page) -> RuntimeError:
    if "/not-available" in page.url:
        return RuntimeError(
            "PixVerse World 当前网络区域不可用；请切换到 PixVerse 支持的网络区域后重试。"
        )
    return RuntimeError(
        "没有捕获到 PixVerse Mine 列表。请先运行 scripts/login_pixverse.py 完成登录，"
        "并确认浏览器中可以打开 Mine > Worlds。"
    )


async def _api_access_from_response(response: Response) -> ApiAccess:
    parsed = urlsplit(response.url)
    headers = await response.request.all_headers()
    return ApiAccess(
        base_url=f"{parsed.scheme}://{parsed.netloc}",
        authorization=headers.get("authorization"),
    )


async def collect_presets(
    page: Page,
    *,
    timeout_ms: int,
) -> tuple[list[dict[str, Any]], ApiAccess]:
    """通过站点自身的无限滚动，收集 ``/api/my-presets`` 的全部分页。"""

    try:
        async with page.expect_response(
            lambda response: _response_page_number(response) == 1,
            timeout=timeout_ms,
        ) as response_info:
            await page.goto(MINE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        first_response = await response_info.value
    except PlaywrightTimeout as exc:
        raise _mine_error(page) from exc
    except PlaywrightError as exc:
        raise _mine_error(page) from exc

    first_items, total = collection_from_payload(await _json_response(first_response))
    presets = list(first_items)
    seen_ids = {str(item.get("id") or item.get("preset_id")) for item in presets}
    page_size = max(1, len(first_items) or 20)
    expected_page = 2

    while len(presets) < total:
        try:
            async with page.expect_response(
                lambda response, wanted=expected_page: _response_page_number(response)
                == wanted,
                timeout=timeout_ms,
            ) as response_info:
                await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            response = await response_info.value
        except PlaywrightTimeout as exc:
            raise RuntimeError(
                f"PixVerse Mine 加载到第 {expected_page - 1} 页后停止响应；"
                f"已取得 {len(presets)}/{total} 个 World。"
            ) from exc

        items, response_total = collection_from_payload(await _json_response(response))
        if response_total:
            total = response_total
        added = 0
        for item in items:
            item_id = str(item.get("id") or item.get("preset_id"))
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            presets.append(item)
            added += 1
        if not items or not added:
            break
        expected_page += 1
        # 等 React 把新卡片插入 DOM，使下一次滚到底部能触发下一页。
        await page.wait_for_timeout(300)
        if expected_page > (total + page_size - 1) // page_size + 2:
            break

    return presets, await _api_access_from_response(first_response)


async def collect_source_session(
    context: BrowserContext,
    api_access: ApiAccess,
    source_session_id: str,
    *,
    timeout_ms: int,
) -> dict[str, Any] | None:
    """读取新 World 所关联的源 session，取得真正的 final_video_url。"""

    endpoint = f"/rtg/db-session/{quote(source_session_id, safe='')}"
    headers = (
        {"Authorization": api_access.authorization}
        if api_access.authorization
        else {}
    )
    try:
        response = await context.request.get(
            f"{api_access.base_url}{endpoint}",
            headers=headers,
            timeout=timeout_ms,
        )
    except PlaywrightError as exc:
        print(f"  [警告] source session {source_session_id} 请求失败：{exc}")
        return None
    if not response.ok:
        print(
            f"  [警告] source session {source_session_id} 返回 HTTP {response.status}"
        )
        return None
    try:
        data = unwrap_api_data(await response.json())
    except Exception as exc:
        print(f"  [警告] source session {source_session_id} 响应无法解析：{exc}")
        return None
    return data if isinstance(data, dict) else None


async def collect_sessions(
    page: Page,
    preset_id: str,
    *,
    timeout_ms: int,
) -> list[dict[str, Any]]:
    """让官方详情页自行带认证头请求一个 World 的全部 session。"""

    endpoint = f"/api/presets/{preset_id}/sessions"
    query = urlencode({"presetId": preset_id, "from": "history_downloader"})
    detail_url = f"{GALLERY_URL}?{query}"
    try:
        async with page.expect_response(
            lambda response: urlsplit(response.url).path.rstrip("/")
            == endpoint.rstrip("/"),
            timeout=timeout_ms,
        ) as response_info:
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=timeout_ms)
        response = await response_info.value
    except PlaywrightTimeout:
        print(f"  [警告] World {preset_id} 的 session 列表加载超时，仅保留主视频。")
        return []
    try:
        items, _ = collection_from_payload(await _json_response(response))
    except RuntimeError as exc:
        print(f"  [警告] World {preset_id} 的 session 列表读取失败：{exc}")
        return []
    return items


def download_stream(url: str, destination: Path, *, timeout_s: int) -> None:
    """按官方前端相同的媒体 URL 下载，同时避免把整段视频读进内存。"""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"拒绝非 HTTP(S) 视频 URL: {url!r}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 PixVerseMineDownloader/1.0",
            "Referer": "https://world.pixverse.video/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


async def _launch_context(
    playwright: Any,
    *,
    user_data_dir: Path,
    headless: bool,
    browser_channel: str | None,
) -> BrowserContext:
    options: dict[str, Any] = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "viewport": {"width": 1920, "height": 1080},
        "accept_downloads": False,
        "args": [
            "--disable-features=TrackingProtection3pcd,ThirdPartyStoragePartitioning",
            "--disable-popup-blocking",
        ],
    }
    if browser_channel:
        options["channel"] = browser_channel
    try:
        return await playwright.chromium.launch_persistent_context(**options)
    except Exception as exc:
        raise RuntimeError(
            f"无法打开 PixVerse 浏览器资料目录 {user_data_dir}。"
            "请关闭正在使用该资料目录的 Chrome 后重试。"
        ) from exc


async def run(args: argparse.Namespace) -> int:
    target_date = normalize_date(args.date) if args.date else ""
    if args.date and not target_date:
        raise ValueError("--date 必须是 YYYY.MM.DD 或 YYYY-MM-DD")

    downloaded_file = Path(args.downloaded_file).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    user_data_dir = Path(args.user_data_dir).expanduser().resolve()
    downloaded = load_downloaded(downloaded_file)

    async with async_playwright() as playwright:
        context = await _launch_context(
            playwright,
            user_data_dir=user_data_dir,
            headless=args.headless,
            browser_channel=args.browser_channel,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            presets, api_access = await collect_presets(
                page,
                timeout_ms=args.timeout * 1000,
            )
            if target_date:
                presets = [
                    preset
                    for preset in presets
                    if normalize_date(
                        preset.get("created_at")
                        or preset.get("create_time")
                        or preset.get("createdAt")
                    )
                    == target_date
                ]
            if args.limit is not None:
                presets = presets[: args.limit]

            print(
                f"找到 {len(presets)} 个 PixVerse World"
                + (f"（{target_date}）" if target_date else "")
            )
            records: list[VideoRecord] = []
            for index, preset in enumerate(presets, 1):
                preset_id = str(preset.get("id") or preset.get("preset_id") or "unknown")
                source_session: dict[str, Any] | None = None
                source_session_id = preset.get("source_session_id")
                if not _video_url(preset) and source_session_id is not None:
                    print(
                        f"  [{index}/{len(presets)}] 定位 World {preset_id} 的"
                        f" source session {source_session_id}..."
                    )
                    source_session = await collect_source_session(
                        context,
                        api_access,
                        str(source_session_id),
                        timeout_ms=args.timeout * 1000,
                    )
                sessions: list[dict[str, Any]] = []
                if args.all_sessions and preset_id != "unknown":
                    print(f"  [{index}/{len(presets)}] 读取 World {preset_id} 的 sessions...")
                    sessions = await collect_sessions(
                        page,
                        preset_id,
                        timeout_ms=args.timeout * 1000,
                    )
                preset_records = video_records_for_preset(
                    preset,
                    sessions,
                    source_session,
                    include_all_sessions=args.all_sessions,
                )
                if not preset_records:
                    print(f"  [警告] World {preset_id} 没有找到可下载的成片 URL")
                records.extend(preset_records)
        finally:
            await context.close()

    print(f"提取到 {len(records)} 个可下载视频")
    succeeded = 0
    failed = 0
    for index, record in enumerate(records, 1):
        destination = output_dir / record.filename
        if not args.all:
            should_skip, removed_stale_record = reconcile_download_state(
                record,
                downloaded,
                destination,
            )
            if removed_stale_record:
                print(
                    f"  [{index}/{len(records)}] 下载记录已过期（文件不存在），重新下载 "
                    f"{destination.name}"
                )
                if not args.dry_run:
                    save_downloaded(downloaded_file, downloaded)
            if should_skip:
                print(f"  [{index}/{len(records)}] 跳过（已下载） {destination.name}")
                continue
        if not args.all and destination.exists():
            print(f"  [{index}/{len(records)}] 跳过（文件已存在） {destination.name}")
            downloaded.add(record.key)
            if not args.dry_run:
                save_downloaded(downloaded_file, downloaded)
            continue
        if args.dry_run:
            print(f"  [{index}/{len(records)}] [DRY-RUN] {destination.name}")
            continue

        print(f"  [{index}/{len(records)}] 下载 {destination.name}")
        try:
            await asyncio.to_thread(
                download_stream,
                record.url,
                destination,
                timeout_s=args.download_timeout,
            )
        except Exception as exc:
            failed += 1
            print(f"    [失败] {exc}", file=sys.stderr)
            continue
        succeeded += 1
        downloaded.add(record.key)
        save_downloaded(downloaded_file, downloaded)
        print("    [完成]")

    print(f"完成：新下载 {succeeded} 个，失败 {failed} 个 -> {output_dir}")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="忽略下载记录，重新下载并覆盖匹配文件",
    )
    parser.add_argument(
        "--all-sessions",
        action="store_true",
        help="除每个 World 的主视频外，也抓取其全部 exploration/session 视频",
    )
    parser.add_argument("--date", help="只下载指定日期，如 2026.07.19")
    parser.add_argument("--limit", type=int, help="最多处理多少个 World（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只列出文件，不下载")
    parser.add_argument("--headless", action="store_true", help="无界面运行浏览器")
    parser.add_argument(
        "--browser-channel",
        choices=("chrome", "msedge"),
        help="改用系统 Chrome/Edge；默认使用 Playwright Chromium",
    )
    parser.add_argument(
        "--user-data-dir",
        default=str(DEFAULT_USER_DATA),
        help=f"PixVerse 浏览器资料目录（默认 {DEFAULT_USER_DATA}）",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_DOWNLOAD_DIR),
        help=f"视频输出目录（默认 {DEFAULT_DOWNLOAD_DIR}）",
    )
    parser.add_argument(
        "--downloaded-file",
        default=str(DEFAULT_DOWNLOADED_FILE),
        help=f"下载记录文件（默认 {DEFAULT_DOWNLOADED_FILE}）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="页面/API 等待秒数（默认 60）",
    )
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=180,
        help="单个视频下载连接超时秒数（默认 180）",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit 必须大于 0")
    if args.timeout < 1 or args.download_timeout < 1:
        raise SystemExit("超时时间必须大于 0")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (RuntimeError, ValueError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
