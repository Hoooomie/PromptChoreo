"""CLI 入口。

用法::

    promptchoreo run timeline.yaml
    promptchoreo run timeline.yaml --headless
    promptchoreo dry-run timeline.yaml
"""

from __future__ import annotations

import asyncio
import sys

import click
import yaml
from rich.console import Console
from rich.table import Table

from .adapters.odyssey import SessionEndedError
from .backends.browser_backend import BrowserBackend
from .core.scheduler import Scheduler
from .core.timeline import Manifest, Timeline

console = Console()

# 每个站点的 Playwright 原始录屏目录
_SITE_VIDEO_DIR = {
    "happy_oyster": "videos/happyoyster",
    "odyssey": "videos/odyssey",
    "pixverse": "videos/pixverse",
}


def _load_site_config(path: str | None) -> dict:
    if path is None:
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_adapter(site: str, config: dict):
    if site == "odyssey":
        from .adapters.odyssey import OdysseyAdapter
        return OdysseyAdapter(config)
    if site == "happy_oyster":
        from .adapters.happy_oyster import HappyOysterAdapter
        return HappyOysterAdapter(config)
    if site == "pixverse":
        from .adapters.pixverse import PixVerseAdapter
        return PixVerseAdapter(config)
    raise click.BadParameter(f"未知站点适配器: {site}")


@click.group()
@click.version_option(package_name="promptchoreo")
def main() -> None:
    """PromptChoreo — 按时序自动投喂 prompt 到流式视频生成网站。"""


@main.command("dry-run")
@click.argument("timeline", type=click.Path(exists=True))
def dry_run(timeline: str) -> None:
    """预览时间轴：只打印不执行。"""
    tl = Timeline.from_file(timeline)

    table = Table(title="时间轴预览", show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("时间", style="cyan", width=10)
    table.add_column("标签", style="yellow", width=12)
    table.add_column("Prompt", style="white")

    for i, event in enumerate(tl.sorted_events, 1):
        table.add_row(
            str(i),
            f"{event.time:.1f}s",
            event.label or "-",
            event.prompt[:80],
        )

    console.print(table)
    console.print(f"\n共 {len(tl)} 个事件，总时长 {tl.duration:.1f}s")


@main.command("run")
@click.argument("timeline", type=click.Path(exists=True))
@click.option(
    "--site", "-s", default="odyssey",
    help="站点适配器名称（默认 odyssey）",
)
@click.option(
    "--config", "-c", type=click.Path(exists=True),
    help="站点配置文件（YAML）",
)
@click.option(
    "--headless/--headed", default=False,
    help="无头模式 / 有头模式（默认有头）",
)
@click.option(
    "--slow-mo", type=int, default=0,
    help="操作间延迟毫秒数，便于观察",
)
@click.option(
    "--user-data-dir", type=click.Path(),
    default=None,
    help="浏览器用户数据目录（持久化登录态）。需要登录的站点（如 happy_oyster）必须提供",
)
@click.option(
    "--record-dir", type=click.Path(),
    default=None,
    help="视频录制保存目录（默认按站点：outputs/video/ho|od|pv）",
)
@click.option(
    "--no-record", is_flag=True, default=False,
    help="禁用视频录制",
)
@click.option(
    "--chrome", "use_system_chrome", is_flag=True, default=False,
    help="使用系统安装的 Chrome（而非 Playwright 自带的 Chromium）",
)
@click.option(
    "--resolution", type=str, default="1920x1080",
    help="视口分辨率 WxH（默认 1920x1080）",
)
@click.option(
    "--cdp", "cdp_url", default=None,
    help="连接已手动打开的 Chrome for Testing（CDP 地址，如 http://127.0.0.1:9222）。"
         "启用后工具不自己启动浏览器：全屏/窗口由你手动控制，录屏只走外部 EV，"
         "不再写 Playwright 原始视频。需先用 scripts/launch_chrome_for_testing.py 开浏览器。",
)
@click.option(
    "--max-load-wait", type=float, default=None,
    help="生成加载等待上限（秒）。Happy Oyster 加载很慢，默认 600；超过仍无计时器则报错。",
)
@click.option(
    "--mute", is_flag=True, default=False,
    help="浏览器级彻底静音（连同 WebAudio）。默认不加：保留视频原声，"
         "仅通过界面 🎵 关闭配乐。仅当你想彻底静音时加 --mute。",
)
def run(timeline: str, site: str, config: str | None, headless: bool, slow_mo: int,
        user_data_dir: str, record_dir: str, no_record: bool,
        use_system_chrome: bool, resolution: str, cdp_url: str | None,
        max_load_wait: float | None, mute: bool) -> None:
    """执行单个时间轴：自动投喂 prompt。"""
    tl = Timeline.from_file(timeline)

    # 解析分辨率
    try:
        w_str, h_str = resolution.split("x")
        viewport_w, viewport_h = int(w_str), int(h_str)
    except (ValueError, AttributeError):
        raise click.BadParameter(f"分辨率格式错误: {resolution}（应为 WxH，如 1920x1080）")

    # 从时间轴文件提取 initial_prompt + 注入事件 + 外部录屏配置
    with open(timeline, encoding="utf-8") as f:
        raw_timeline = yaml.safe_load(f) or {}
    site_config = _build_site_config(
        site=site, config_path=config, raw_timeline=raw_timeline,
        cdp_url=cdp_url, user_data_dir=user_data_dir,
        no_record=no_record, record_dir=record_dir,
    )
    if max_load_wait is not None:
        site_config["max_load_wait"] = float(max_load_wait)

    _execute_timeline(
        tl, site, site_config,
        headless=headless, slow_mo=slow_mo,
        use_system_chrome=use_system_chrome,
        viewport_w=viewport_w, viewport_h=viewport_h,
        cdp_url=cdp_url, mute=mute,
    )


def _build_site_config(
    site: str, config_path: str | None, raw_timeline: dict,
    cdp_url: str | None, user_data_dir: str | None,
    no_record: bool, record_dir: str | None,
) -> dict:
    """组装站点适配器 config：合并 --config、时间轴里的 initial_prompt/recorder 等。"""
    connect_mode = bool(cdp_url)
    site_config = _load_site_config(config_path)

    if connect_mode:
        site_config["_connect_mode"] = True
        # 连接模式下外部浏览器已持有 user-data-dir；Playwright 录像不可用
        user_data_dir = None
    else:
        # 非连接模式下 record_video_dir 仅占位；当前实现录屏走外部 EV
        record_video_dir = None if no_record else (record_dir or _SITE_VIDEO_DIR.get(site, "videos"))

    if "initial_prompt" in raw_timeline:
        site_config["initial_prompt"] = str(raw_timeline["initial_prompt"])
        site_config["_inject_events"] = raw_timeline.get("events", [])
        site_config["_end_delay"] = float(raw_timeline.get("end_delay", 0))

    if "max_load_wait" in raw_timeline:
        site_config["max_load_wait"] = float(raw_timeline["max_load_wait"])

    rec = raw_timeline.get("recorder") or site_config.get("recorder") or {}
    if isinstance(rec, dict) and rec.get("enabled"):
        site_config["_recorder_enabled"] = True
        site_config["_recorder_start_hotkey"] = rec.get("start_hotkey", "ctrl+f1")
        site_config["_recorder_stop_hotkey"] = rec.get("stop_hotkey", "ctrl+f2")
    else:
        site_config.setdefault("_recorder_enabled", False)

    # 把 user_data_dir 透传给适配器（非连接模式、且需要登录的站点用）
    if user_data_dir:
        site_config["user_data_dir"] = user_data_dir

    return site_config


def _execute_timeline(
    tl: Timeline, site: str, site_config: dict,
    headless: bool, slow_mo: int, use_system_chrome: bool,
    viewport_w: int, viewport_h: int, cdp_url: str | None,
    mute: bool = False,
    label: str = "",
) -> None:
    """执行单个时间轴（run 与 batch 共用）。

    注意：SessionEndedError（会话超时）会被重新抛出而不是 sys.exit，
    以便 batch 模式可以逐视频捕获并继续。其余异常仍 sys.exit(1)。
    """
    # 延迟导入避免循环依赖（odyssey 模块才定义此异常）
    from .adapters.odyssey import SessionEndedError

    adapter = _build_adapter(site, site_config)
    backend = BrowserBackend(
        adapter, headless=headless, slow_mo=slow_mo,
        user_data_dir=site_config.get("user_data_dir"),
        record_video_dir=None,
        console=console,
        channel="chrome" if use_system_chrome else None,
        viewport={"width": viewport_w, "height": viewport_h},
        cdp_url=cdp_url,
        mute=mute,
    )
    scheduler = Scheduler(tl, backend, console=console)

    try:
        asyncio.run(scheduler.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]用户中断[/]")
        sys.exit(1)
    except SessionEndedError as exc:
        console.print(f"\n[yellow]会话超时（{label or site}）: {exc}[/]")
        raise  # batch 循环捕获后继续下一个
    except Exception as exc:
        # 错误信息常含 CSS 选择器（如 textarea[placeholder*='接下来']），
        # Rich 会把 [...] 当标记语法吃掉，导致看不清真实错误。转义后输出。
        safe = str(exc).replace("[", "[[")
        console.print(f"\n[red]执行出错:[/] {safe}")
        sys.exit(1)


@main.command("batch")
@click.argument("manifest", type=click.Path(exists=True))
@click.option(
    "--site", "-s", default=None,
    help="默认站点（清单内每条 video 未指定 site 时使用）",
)
@click.option(
    "--config", "-c", type=click.Path(exists=True),
    help="站点配置文件（YAML），合并进每个 video 的 config",
)
@click.option("--headless/--headed", default=False, help="无头模式 / 有头模式")
@click.option("--slow-mo", type=int, default=0, help="操作间延迟毫秒数")
@click.option("--user-data-dir", type=click.Path(), default=None,
              help="浏览器用户数据目录（连接模式忽略）")
@click.option("--no-record", is_flag=True, default=False, help="禁用视频录制")
@click.option("--chrome", "use_system_chrome", is_flag=True, default=False,
              help="使用系统安装的 Chrome")
@click.option("--resolution", type=str, default="1920x1080", help="视口分辨率 WxH")
@click.option(
    "--cdp", "cdp_url", default=None,
    help="连接已手动打开的 Chrome for Testing（CDP 地址）。推荐：清单多站点时只开一个浏览器，"
         "工具自动复用同一标签页、跑完自动复位，依次生成每个视频。",
)
@click.option(
    "--max-load-wait", type=float, default=None,
    help="生成加载等待上限（秒），覆盖清单内所有视频的 max_load_wait；Happy Oyster 默认 600。",
)
@click.option(
    "--mute", is_flag=True, default=False,
    help="浏览器级彻底静音（连同 WebAudio）。默认不加：保留视频原声，"
         "仅通过界面 🎵 关闭配乐。仅当你想彻底静音时加 --mute。",
)
def batch(manifest: str, site: str | None, config: str | None, headless: bool,
          slow_mo: int, user_data_dir: str, no_record: bool,
          use_system_chrome: bool, resolution: str, cdp_url: str | None,
          max_load_wait: float | None, mute: bool) -> None:
    """按清单顺序执行多个时间轴（一个文件 = 多个视频）。

    清单格式（.json / .yaml 均可）：:

        {
          "site": "odyssey",
          "videos": [
            {"name": "片段1", "site": "odyssey",
             "initial_prompt": "...", "end_delay": 10,
             "recorder": {"enabled": true, "start_hotkey": "ctrl+f1", "stop_hotkey": "ctrl+f2"},
             "events": [{"time": 0, "prompt": "..."}]},
            {"name": "片段2", "site": "happy_oyster", "initial_prompt": "...", "events": [...]}
          ]
        }

    连接模式（--cdp）下：所有视频共用一个已开的 Chrome 标签页，
    跑完自动复位，无需重启浏览器。
    """
    mf = Manifest.from_file(manifest)

    try:
        w_str, h_str = resolution.split("x")
        viewport_w, viewport_h = int(w_str), int(h_str)
    except (ValueError, AttributeError):
        raise click.BadParameter(f"分辨率格式错误: {resolution}（应为 WxH，如 1920x1080）")

    default_site = site or mf.site

    # 连接模式（--cdp）下：复用用户手动开的单个浏览器，只能服务同一站点。
    # 清单若混合多站点，这里直接报错，避免后面在 CDP 里反复跨站 goto 抢走标签页。
    if cdp_url:
        _sites = {v.site or default_site for v in mf.videos if (v.site or default_site)}
        if len(_sites) > 1:
            raise click.BadParameter(
                "连接模式（--cdp）下清单不能混合多站点"
                f"（检测到：{', '.join(sorted(_sites))}）。\n"
                "连接模式复用你手动开的单个浏览器，只能服务同一站点。"
                "请按站点拆分清单（每个站点一个清单），或去掉 --cdp 走工具自带浏览器。"
            )

    total = len(mf.videos)
    console.print(f"[bold]清单载入[/]：共 {total} 个视频")

    for idx, vspec in enumerate(mf.videos, 1):
        v_site = vspec.site or default_site
        if not v_site:
            console.print(f"[red]视频 {vspec.name} 缺少 site，跳过[/]")
            continue

        console.print(
            f"\n[bold cyan]── 视频 {idx}/{total}：{vspec.name} "
            f"（站点 {v_site}）──[/]"
        )
        # 站点配置 = --config 基础 + 连接模式标志 + 该 video 自带 config_extra
        # （config_extra 已是处理好的形式：initial_prompt / _inject_events /
        #  _end_delay / _recorder_enabled 等，无需再按原始文件解析）
        site_config = _load_site_config(config)
        if cdp_url:
            site_config["_connect_mode"] = True
            user_data_dir = None
        site_config.update(vspec.config_extra)
        if max_load_wait is not None:
            site_config["max_load_wait"] = float(max_load_wait)
        if user_data_dir:
            site_config.setdefault("user_data_dir", user_data_dir)

        try:
            _execute_timeline(
                vspec.timeline, v_site, site_config,
                headless=headless, slow_mo=slow_mo,
                use_system_chrome=use_system_chrome,
                viewport_w=viewport_w, viewport_h=viewport_h,
                cdp_url=cdp_url, mute=mute,
                label=vspec.name,
            )
        except SessionEndedError as err:
            # 会话超时：适配器已点 Try Again 复位，batch 继续下一个视频
            console.print(
                f"\n[yellow]视频 {vspec.name} 会话超时（{err}），已复位，继续下一个...[/]"
            )
        except SystemExit:
            # _execute_timeline 内部对非 KeyboardInterrupt/SessionEnded 的异常会 sys.exit(1)。
            # batch 模式下拦截 sys.exit，不退出整个进程，继续下一个视频。
            console.print(
                f"\n[yellow]视频 {vspec.name} 异常结束（见上方错误信息），"
                f"继续下一个...[/]"
            )
        except Exception as exc:
            console.print(
                f"\n[yellow]视频 {vspec.name} 异常: {exc}，继续下一个...[/]"
            )

    console.print(f"\n[bold green]清单全部完成[/]：{total} 个视频")


if __name__ == "__main__":
    main()
