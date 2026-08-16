#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载当前 Happy Oyster 账号作品库中的实时导演视频。

脚本读取作品页自身的 ``exploration-records`` 分页响应，以稳定作品 ID
定位详情页和 CDN 成片，不依赖卡片坐标或固定滚动次数。

用法::

    python scripts/download_ho_videos.py --dry-run
    python scripts/download_ho_videos.py --date 2026.07.26
    python scripts/download_ho_videos.py
    python scripts/download_ho_videos.py --all

首次使用前请运行 ``python scripts/login_happyoyster.py`` 保存登录态。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import struct
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

from playwright.async_api import (
    APIResponse,
    BrowserContext,
    Download,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "outputs" / "downloads" / "happyoyster"
DEFAULT_DOWNLOADED_FILE = PROJECT_ROOT / ".downloaded_ho.json"
EXPLORATION_API_PATH = "/api/v1/profile/exploration-records"

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DATE_PATTERN = re.compile(
    r"(?P<year>\d{4})[-./](?P<month>\d{1,2})[-./](?P<day>\d{1,2})"
)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

COLLECTION_KEYS = ("records", "items", "list", "rows", "content")
ENVELOPE_KEYS = ("data", "result", "response", "resp", "payload")
TOTAL_KEYS = (
    "total",
    "totalCount",
    "total_count",
    "recordCount",
    "record_count",
)
ID_KEYS = (
    "explorationId",
    "exploration_id",
    "recordId",
    "record_id",
    "id",
    "uuid",
)
TITLE_KEYS = ("title", "name", "worldName", "world_name", "prompt", "description")
DATE_KEYS = (
    "createdAt",
    "created_at",
    "createTime",
    "create_time",
    "createdTime",
    "created_time",
    "date",
)
VIDEO_LIST_KEYS = {
    "videos",
    "videolist",
    "branchvideos",
    "branchlist",
    "medialist",
    "branches",
}


@dataclass(frozen=True)
class SiteConfig:
    name: str
    base_url: str
    user_data_dir: Path
    output_dir: Path
    downloaded_file: Path

    @property
    def profile_url(self) -> str:
        return f"{self.base_url}/profile"


SITE_CONFIGS = {
    "cn": SiteConfig(
        name="cn",
        base_url="https://www.happyoyster.cn",
        user_data_dir=Path.home() / ".workbuddy" / "browser_data",
        output_dir=DEFAULT_DOWNLOAD_DIR,
        downloaded_file=DEFAULT_DOWNLOADED_FILE,
    ),
    "global": SiteConfig(
        name="global",
        base_url="https://www.happyoyster.com",
        user_data_dir=Path.home()
        / ".workbuddy"
        / "browser_data_happy_oyster_global",
        output_dir=PROJECT_ROOT / "outputs" / "downloads" / "happyoyster_global",
        downloaded_file=PROJECT_ROOT / ".downloaded_ho_global.json",
    ),
}


@dataclass(frozen=True)
class ApiAccess:
    """复用页面请求的 API 地址和认证头，凭据只保留在当前进程内。"""

    endpoint_url: str
    headers: dict[str, str] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Exploration:
    exploration_id: str
    title: str
    date: str
    video_urls: tuple[str, ...]


@dataclass(frozen=True)
class VideoRecord:
    site: str
    exploration_id: str
    title: str
    date: str
    index: int
    total: int
    url: str | None

    @property
    def stable_media_id(self) -> str:
        if self.url:
            return stable_url_id(self.url)
        return "official"

    @property
    def key(self) -> str:
        return (
            f"happyoyster:{self.site}:{self.exploration_id}:"
            f"video:{self.stable_media_id}"
        )

    @property
    def filename(self) -> str:
        suffix = ".mp4"
        if self.url:
            candidate = Path(unquote(urlsplit(self.url).path)).suffix.lower()
            if candidate in VIDEO_SUFFIXES:
                suffix = candidate
        short_id = sanitize_filename(self.exploration_id, max_length=12)
        body = "_".join(
            (
                self.date or "unknown-date",
                self.title or "untitled",
                f"e{short_id}",
                f"v{self.index:02d}",
            )
        )
        return sanitize_filename(body, max_length=190 - len(suffix)) + suffix

    @property
    def detail_path(self) -> str:
        return f"/profile/exploration/{quote(self.exploration_id, safe='-_')}"


def sanitize_filename(value: str, *, max_length: int = 180) -> str:
    """生成 Windows/macOS/Linux 都可接受的文件名主体。"""

    value = INVALID_FILENAME.sub("_", str(value))
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "untitled"
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or "untitled"


def normalize_date(value: Any) -> str:
    """将站点日期、ISO 时间或时间戳统一为 ``YYYY.MM.DD``。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y.%m.%d")
        except (OSError, OverflowError, ValueError):
            return ""
    match = DATE_PATTERN.search(str(value or ""))
    if not match:
        return ""
    return (
        f"{int(match.group('year')):04d}."
        f"{int(match.group('month')):02d}."
        f"{int(match.group('day')):02d}"
    )


def _integer_from(mapping: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def collection_from_payload(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    """兼容常见 API envelope，返回作品列表和可选总数。"""

    def visit(
        node: Any,
        inherited_total: int | None,
        depth: int,
    ) -> tuple[list[dict[str, Any]], int | None] | None:
        if depth > 6:
            return None
        if isinstance(node, list):
            items = [item for item in node if isinstance(item, dict)]
            if items or not node:
                return items, inherited_total
            return None
        if not isinstance(node, dict):
            return None

        total = _integer_from(node, TOTAL_KEYS)
        if total is None:
            total = inherited_total

        for key in COLLECTION_KEYS:
            if key in node:
                result = visit(node[key], total, depth + 1)
                if result is not None:
                    return result
        for key in ENVELOPE_KEYS:
            if key in node:
                result = visit(node[key], total, depth + 1)
                if result is not None:
                    return result
        return None

    result = visit(payload, None, 0)
    return result if result is not None else ([], None)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _first_field(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    wanted = {_normalized_key(key) for key in keys}
    queue: list[tuple[dict[str, Any], int]] = [(item, 0)]
    while queue:
        node, depth = queue.pop(0)
        for key, value in node.items():
            if _normalized_key(str(key)) in wanted and value not in (None, ""):
                return value
        if depth >= 3:
            continue
        for key in ENVELOPE_KEYS:
            nested = node.get(key)
            if isinstance(nested, dict):
                queue.append((nested, depth + 1))
    return None


def stable_url_id(url: str) -> str:
    """忽略会轮换的 query，以媒体路径生成稳定 ID。"""

    parsed = urlsplit(url)
    stable = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]


def _iter_video_candidates(
    node: Any,
    path: tuple[str, ...] = (),
) -> Iterator[tuple[str, int]]:
    if isinstance(node, str):
        parsed = urlsplit(node)
        if parsed.scheme not in {"http", "https"}:
            return
        suffix = Path(unquote(parsed.path)).suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            return

        path_text = " ".join(_normalized_key(part) for part in path)
        url_text = parsed.path.lower()
        score = 10 if suffix == ".mp4" else 0
        if any(word in path_text for word in ("poster", "thumbnail", "cover")):
            score -= 200
        if "preview" in path_text:
            score -= 60
        if any(
            word in path_text
            for word in ("download", "original", "master", "final", "compose")
        ):
            score += 120
        elif any(word in path_text for word in ("video", "media", "play", "source")):
            score += 40
        if "compose_branch_video" in url_text:
            score += 80
        yield node, score
        return

    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_video_candidates(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_video_candidates(value, (*path, str(index)))


def _best_video_urls(node: Any) -> list[str]:
    candidates = list(_iter_video_candidates(node))
    if not candidates:
        return []
    best_score = max(score for _, score in candidates)
    result: list[str] = []
    seen: set[str] = set()
    for url, score in candidates:
        stable = stable_url_id(url)
        if score != best_score or stable in seen:
            continue
        seen.add(stable)
        result.append(url)
    return result


def extract_video_urls(item: dict[str, Any]) -> list[str]:
    """优先每个视频/分支的原始或合成 URL，丢弃低分预览。"""

    list_groups: list[list[Any]] = []
    for key, value in item.items():
        if _normalized_key(str(key)) in VIDEO_LIST_KEYS and isinstance(value, list):
            list_groups.append(value)

    if list_groups:
        result: list[str] = []
        seen: set[str] = set()
        for group in list_groups:
            for video_item in group:
                for url in _best_video_urls(video_item):
                    stable = stable_url_id(url)
                    if stable in seen:
                        continue
                    seen.add(stable)
                    result.append(url)
        if result:
            return result
    return _best_video_urls(item)


def exploration_from_item(item: dict[str, Any]) -> Exploration:
    raw_id = _first_field(item, ID_KEYS)
    if raw_id in (None, ""):
        canonical = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        raw_id = "unknown-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    raw_title = _first_field(item, TITLE_KEYS)
    raw_date = _first_field(item, DATE_KEYS)
    return Exploration(
        exploration_id=str(raw_id),
        title=str(raw_title or "untitled").strip() or "untitled",
        date=normalize_date(raw_date),
        video_urls=tuple(extract_video_urls(item)),
    )


def records_for_explorations(
    explorations: list[Exploration],
    *,
    site: str,
) -> list[VideoRecord]:
    records: list[VideoRecord] = []
    for exploration in explorations:
        urls: tuple[str | None, ...] = exploration.video_urls or (None,)
        total = len(urls)
        for index, url in enumerate(urls, 1):
            records.append(
                VideoRecord(
                    site=site,
                    exploration_id=exploration.exploration_id,
                    title=exploration.title,
                    date=exploration.date,
                    index=index,
                    total=total,
                    url=url,
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


def _response_page_number(response: Response) -> int | None:
    parsed = urlsplit(response.url)
    if parsed.path.rstrip("/") != EXPLORATION_API_PATH:
        return None
    query = parse_qs(parsed.query)
    value = (query.get("page") or query.get("pageNo") or ["1"])[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _page_size_from_url(url: str, fallback: int) -> int:
    query = parse_qs(urlsplit(url).query)
    value = (query.get("pageSize") or query.get("page_size") or [fallback])[0]
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, fallback)


def _url_for_page(url: str, page_number: int, page_size: int) -> str:
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    page_key = "pageNo" if any(key == "pageNo" for key, _ in pairs) else "page"
    size_key = (
        "page_size" if any(key == "page_size" for key, _ in pairs) else "pageSize"
    )
    filtered = [
        (key, value)
        for key, value in pairs
        if key not in {"page", "pageNo", "pageSize", "page_size"}
    ]
    filtered.extend(((page_key, str(page_number)), (size_key, str(page_size))))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(filtered), ""))


async def _json_from_page_response(response: Response) -> Any:
    if not response.ok:
        if response.status in {401, 403}:
            raise RuntimeError(
                "Happy Oyster 下载器登录态已失效。请运行 "
                "scripts/login_happyoyster.py（国际站使用 "
                "scripts/login_happyoyster_global.py）重新登录后再试。"
            )
        raise RuntimeError(
            f"Happy Oyster 作品列表返回 HTTP {response.status}: {response.url}"
        )
    try:
        return await response.json()
    except Exception as exc:
        raise RuntimeError("Happy Oyster 作品列表没有返回 JSON") from exc


async def _json_from_api_response(response: APIResponse) -> Any:
    if not response.ok:
        if response.status in {401, 403}:
            raise RuntimeError(
                "Happy Oyster 下载器登录态在分页过程中失效，请重新登录后再试。"
            )
        raise RuntimeError(
            f"Happy Oyster 作品列表返回 HTTP {response.status}: {response.url}"
        )
    try:
        return await response.json()
    except Exception as exc:
        raise RuntimeError("Happy Oyster 后续分页没有返回 JSON") from exc


async def _api_access_from_response(response: Response) -> ApiAccess:
    all_headers = await response.request.all_headers()
    exact_headers = {
        "accept",
        "accept-language",
        "authorization",
        "device-id",
        "deviceid",
        "origin",
        "referer",
        "token",
        "user-agent",
    }
    headers = {
        key: value
        for key, value in all_headers.items()
        if key.lower() in exact_headers or key.lower().startswith("x-")
    }
    return ApiAccess(endpoint_url=response.url, headers=headers)


async def collect_exploration_items(
    page: Page,
    context: BrowserContext,
    *,
    profile_url: str,
    timeout_ms: int,
) -> list[dict[str, Any]]:
    """捕获第一页后，复用其内存认证头读取其余分页。"""

    try:
        async with page.expect_response(
            lambda response: _response_page_number(response) == 1,
            timeout=timeout_ms,
        ) as response_info:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_ms)
        first_response = await response_info.value
    except (PlaywrightTimeout, PlaywrightError) as exc:
        raise RuntimeError(
            "没有捕获到 Happy Oyster 作品列表。请先运行登录脚本，并确认作品页"
            "能看到“我的视频”。"
        ) from exc

    first_items, total = collection_from_payload(
        await _json_from_page_response(first_response)
    )
    if not first_items and total not in (0, None):
        raise RuntimeError("作品列表响应存在总数，但没有解析到作品记录")

    api_access = await _api_access_from_response(first_response)
    page_size = _page_size_from_url(first_response.url, len(first_items) or 12)
    items = list(first_items)
    seen = {
        exploration_from_item(item).exploration_id
        for item in first_items
    }

    page_number = 2
    while True:
        if total is not None and len(items) >= total:
            break
        if total is None and len(first_items) < page_size:
            break
        if page_number > 1000:
            raise RuntimeError("作品分页超过 1000 页，已停止以避免无限循环")

        next_url = _url_for_page(api_access.endpoint_url, page_number, page_size)
        try:
            response = await context.request.get(
                next_url,
                headers=api_access.headers,
                timeout=timeout_ms,
            )
        except PlaywrightError as exc:
            raise RuntimeError(f"读取作品列表第 {page_number} 页失败：{exc}") from exc
        page_items, response_total = collection_from_payload(
            await _json_from_api_response(response)
        )
        if response_total is not None:
            total = response_total

        added = 0
        for item in page_items:
            exploration_id = exploration_from_item(item).exploration_id
            if exploration_id in seen:
                continue
            seen.add(exploration_id)
            items.append(item)
            added += 1
        if len(page_items) < page_size or not added:
            break
        page_number += 1

    return items


def _mp4_top_level_boxes(path: Path) -> set[bytes]:
    boxes: set[bytes] = set()
    size_total = path.stat().st_size
    with path.open("rb") as media:
        offset = 0
        while offset + 8 <= size_total:
            media.seek(offset)
            header = media.read(16)
            if len(header) < 8:
                break
            size = struct.unpack(">I", header[:4])[0]
            box_type = header[4:8]
            header_size = 8
            if size == 1:
                if len(header) < 16:
                    break
                size = struct.unpack(">Q", header[8:16])[0]
                header_size = 16
            elif size == 0:
                size = size_total - offset
            if size < header_size or offset + size > size_total:
                break
            boxes.add(box_type)
            offset += size
    return boxes


def validate_media_file(path: Path) -> str:
    """拒绝登录页/错误 JSON 冒充的视频，并返回实际容器类型。"""

    if not path.exists() or path.stat().st_size < 1024:
        raise ValueError("下载结果过小，不是有效视频")
    with path.open("rb") as media:
        magic = media.read(12)
    if len(magic) >= 8 and magic[4:8] == b"ftyp":
        boxes = _mp4_top_level_boxes(path)
        if b"moov" not in boxes or b"mdat" not in boxes:
            raise ValueError("MP4 缺少 moov/mdat 数据")
        return "mp4"
    if magic.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    raise ValueError("下载结果不是 MP4/WebM，可能是登录页或错误响应")


def _destination_for_container(destination: Path, container: str) -> Path:
    expected = ".webm" if container == "webm" else ".mp4"
    return destination if destination.suffix.lower() == expected else destination.with_suffix(expected)


def download_stream(
    url: str,
    destination: Path,
    *,
    timeout_s: int,
    referer: str,
) -> Path:
    """流式保存 CDN 成片；验证成功后再原子替换目标文件。"""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"拒绝非 HTTP(S) 视频 URL: {url!r}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 HappyOysterDownloader/1.0",
            "Referer": referer,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type and not (
                content_type.startswith("video/")
                or "octet-stream" in content_type
            ):
                raise ValueError(f"媒体 URL 返回了异常 Content-Type: {content_type}")
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        container = validate_media_file(temporary)
        final_destination = _destination_for_container(destination, container)
        os.replace(temporary, final_destination)
        return final_destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


async def _save_browser_download(download: Download, destination: Path) -> Path:
    suffix = Path(download.suggested_filename).suffix.lower()
    if suffix in VIDEO_SUFFIXES and suffix != destination.suffix.lower():
        destination = destination.with_suffix(suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        await download.save_as(str(temporary))
        container = validate_media_file(temporary)
        final_destination = _destination_for_container(destination, container)
        os.replace(temporary, final_destination)
        return final_destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


async def download_via_official_button(
    page: Page,
    record: VideoRecord,
    destination: Path,
    *,
    base_url: str,
    navigation_timeout_ms: int,
    prepare_timeout_ms: int,
) -> Path:
    """进入稳定详情路由并等待站点官方 Download 事件。"""

    detail_url = f"{base_url}{record.detail_path}"
    await page.goto(
        detail_url,
        wait_until="domcontentloaded",
        timeout=navigation_timeout_ms,
    )
    button = page.locator("button[aria-label='下载'], button[title='下载']").first
    await button.wait_for(state="visible", timeout=navigation_timeout_ms)

    download_task = asyncio.create_task(
        page.wait_for_event("download", timeout=prepare_timeout_ms)
    )
    try:
        await button.click()
        download = await download_task
        return await _save_browser_download(download, destination)
    finally:
        if not download_task.done():
            download_task.cancel()
            await asyncio.gather(download_task, return_exceptions=True)


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
        "accept_downloads": True,
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
            f"无法打开 Happy Oyster 浏览器资料目录 {user_data_dir}。"
            "请关闭正在使用该资料目录的 Chrome 后重试。"
        ) from exc


async def run(args: argparse.Namespace) -> int:
    site_config = SITE_CONFIGS[args.site]
    target_date = normalize_date(args.date) if args.date else ""
    if args.date and not target_date:
        raise ValueError("--date 必须是 YYYY.MM.DD 或 YYYY-MM-DD")

    user_data_dir = Path(args.user_data_dir or site_config.user_data_dir).expanduser().resolve()
    output_dir = Path(args.output_dir or site_config.output_dir).expanduser().resolve()
    downloaded_file = Path(
        args.downloaded_file or site_config.downloaded_file
    ).expanduser().resolve()
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
            items = await collect_exploration_items(
                page,
                context,
                profile_url=site_config.profile_url,
                timeout_ms=args.timeout * 1000,
            )
            explorations = [exploration_from_item(item) for item in items]
            if target_date:
                explorations = [
                    exploration
                    for exploration in explorations
                    if exploration.date == target_date
                ]
            if args.limit is not None:
                explorations = explorations[: args.limit]
            records = records_for_explorations(explorations, site=args.site)

            print(
                f"找到 {len(explorations)} 个 Happy Oyster 作品，"
                f"提取到 {len(records)} 个视频"
                + (f"（{target_date}）" if target_date else "")
            )

            succeeded = 0
            failed = 0
            for position, record in enumerate(records, 1):
                destination = output_dir / record.filename
                if not args.all and (
                    record.key in downloaded or record.title in downloaded
                ):
                    print(
                        f"  [{position}/{len(records)}] 跳过（已下载） "
                        f"{destination.name}"
                    )
                    continue
                if not args.all and destination.exists():
                    print(
                        f"  [{position}/{len(records)}] 跳过（文件已存在） "
                        f"{destination.name}"
                    )
                    downloaded.add(record.key)
                    if not args.dry_run:
                        save_downloaded(downloaded_file, downloaded)
                    continue
                if args.dry_run:
                    source = (
                        "官方下载→CDN 回退"
                        if args.source == "auto"
                        else ("官方下载" if args.source == "official" else "CDN 成片")
                    )
                    print(
                        f"  [{position}/{len(records)}] [DRY-RUN] "
                        f"{destination.name} [{source}]"
                    )
                    continue

                print(f"  [{position}/{len(records)}] 下载 {destination.name}")
                saved_path: Path | None = None
                official_error: Exception | None = None
                try_official = args.source in {"auto", "official"} and (
                    record.total == 1 or record.url is None
                )
                if try_official:
                    try:
                        saved_path = await download_via_official_button(
                            page,
                            record,
                            destination,
                            base_url=site_config.base_url,
                            navigation_timeout_ms=args.timeout * 1000,
                            prepare_timeout_ms=args.prepare_timeout * 1000,
                        )
                    except Exception as exc:
                        official_error = exc
                        if args.source == "auto" and record.url:
                            print(f"    [官方下载失败，改用 CDN] {exc}")

                if saved_path is None and args.source in {"auto", "direct"}:
                    if not record.url:
                        official_error = official_error or RuntimeError(
                            "作品列表没有提供 CDN 视频 URL"
                        )
                    else:
                        try:
                            saved_path = await asyncio.to_thread(
                                download_stream,
                                record.url,
                                destination,
                                timeout_s=args.download_timeout,
                                referer=site_config.profile_url,
                            )
                        except Exception as exc:
                            official_error = exc

                if saved_path is None:
                    failed += 1
                    print(f"    [失败] {official_error}", file=sys.stderr)
                    continue

                succeeded += 1
                downloaded.add(record.key)
                save_downloaded(downloaded_file, downloaded)
                print(f"    [完成] {saved_path.name}")

            print(f"完成：新下载 {succeeded} 个，失败 {failed} 个 -> {output_dir}")
            return 1 if failed else 0
        finally:
            await context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site",
        choices=tuple(SITE_CONFIGS),
        default="cn",
        help="站点：cn（默认）或 global",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "official", "direct"),
        default="auto",
        help="auto 优先官方下载并回退 CDN；official/direct 强制指定来源",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="忽略下载记录，重新下载并覆盖匹配文件",
    )
    parser.add_argument("--date", help="只下载指定日期，如 2026.07.26")
    parser.add_argument("--limit", type=int, help="最多处理多少个作品（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只列出文件，不下载")
    parser.add_argument("--headless", action="store_true", help="无界面运行浏览器")
    parser.add_argument(
        "--browser-channel",
        choices=("chrome", "msedge"),
        help="改用系统 Chrome/Edge；默认使用 Playwright Chromium",
    )
    parser.add_argument("--user-data-dir", help="覆盖站点默认浏览器资料目录")
    parser.add_argument("--output-dir", help="覆盖站点默认视频输出目录")
    parser.add_argument("--downloaded-file", help="覆盖站点默认下载记录文件")
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="页面/API 等待秒数（默认 60）",
    )
    parser.add_argument(
        "--prepare-timeout",
        type=int,
        default=180,
        help="官方下载后台准备秒数（默认 180）",
    )
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=180,
        help="单个 CDN 视频连接超时秒数（默认 180）",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit 必须大于 0")
    if min(args.timeout, args.prepare_timeout, args.download_timeout) < 1:
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
