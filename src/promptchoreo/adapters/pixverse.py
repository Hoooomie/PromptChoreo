"""PixVerse World 适配器."""

from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .base import SiteAdapter
from ..credentials import get_browser_data_dir, get_credentials


class PixVerseAdapter(SiteAdapter):

    name = "pixverse"
    resets_clock = True
    user_data_dir = get_browser_data_dir("pixverse")
    URL = "https://world.pixverse.video/generate/"

    SELECTORS = {
        "landing_textarea": "textarea[placeholder*='Describe']",
        "star_button": "button.rounded-full.size-9",
        "star_button_fallback": "button[class*='rounded-full'][class*='size-9']",
        "stream_textarea": "textarea[placeholder*='What would you']",
        "leave_button": "button:has-text('Leave')",
        "video_element": "video",
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._session_started = False
        self._injections_done = False
        self._video_offset: float = 0.0  # 录制起点视频时钟偏移
        self._last_pct: int | None = None
        self.generation_start_monotonic: float | None = None
        self.stop_monotonic: float | None = None
        self._job_start_monotonic = float(
            self.config.get("_job_start_monotonic", 0) or 0
        )
        self.job_start_time_utc: str | None = self.config.get(
            "_job_start_time_utc"
        )
        self.initial_prompt_time_s: float | None = None
        self.first_video_chunk_time_s: float | None = None
        self.generation_complete_time_s: float | None = None
        self._injection_log: list[dict] = []
        self._max_queue_wait = self.config.get("max_queue_wait", 30000)
        self._max_load_wait = self.config.get("max_load_wait", 600)
        self._post_inject_delay = self.config.get("post_inject_delay", 0.5)
        self._mode = (self.config.get("mode") or "story").lower()  # 默认 story 模式
        # Publish World Exploration 开关：默认开启，开启后可直接下载原视频。
        # 设为 false 才关闭（兼容旧行为）。
        self._publish = self.config.get("publish", True)
        self.crop_region: str | None = None

    async def setup(self, page: Page) -> None:
        self._begin_job_attempt()
        await page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await self._ensure_logged_in(page)
        input_loc = page.locator(self.SELECTORS["landing_textarea"])
        await input_loc.wait_for(state="visible", timeout=30000)
        initial_prompt = self.config.get("initial_prompt")
        if initial_prompt:
            print(f"[DEBUG] setup: {initial_prompt!r}", file=sys.stderr)
            await self._start_session(page, str(initial_prompt))

    @property
    def is_done(self) -> bool:
        return self._injections_done

    async def submit_prompt(self, page: Page, prompt: str, target_time: float | None = None) -> None:
        if self._injections_done:
            return  # 注入循环已处理全部事件，避免初始 prompt 被当指令二次注入
        if not self._session_started:
            await self._start_session(page, prompt)
        else:
            await self._inject_command(page, prompt)

    async def teardown(self, page: Page) -> None:
        await self._recorder_stop(page)
        try:
            leave = page.locator("button:has-text('Leave')").first
            await leave.click(force=True, timeout=5000)
            self.stop_monotonic = time.monotonic()
            print("[DEBUG] Leave 完成", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] Leave 失败: {e}", file=sys.stderr)
            self.stop_monotonic = time.monotonic()

    # ========== 登录 ==========

    async def _ensure_logged_in(self, page: Page) -> None:
        try:
            # 第一步：检查是否需要登录
            status = await page.evaluate("""() => {
                var email = document.querySelector('input[placeholder*="邮箱"], input[placeholder*="用户"]');
                if (!email || email.offsetParent === null) {
                    var ov = document.querySelector('button[aria-label*="Log in"]');
                    if (ov) { ov.click(); return 'overlay_clicked'; }
                    return 'no_login_needed';
                }
                return 'form_visible';
            }""")
            print(f"[DEBUG] 登录状态: {status}", file=sys.stderr)

            if status == 'overlay_clicked':
                await asyncio.sleep(6)

            if status in ('overlay_clicked', 'form_visible'):
                for attempt in range(10):
                    email_loc = page.locator("input[placeholder*='邮箱'], input[placeholder*='用户']").first
                    if not await email_loc.is_visible(timeout=2000):
                        print("[DEBUG] 登录页已消失", file=sys.stderr)
                        break
                    print(f"[DEBUG] 填表+登录 第{attempt+1}次", file=sys.stderr)
                    creds = get_credentials("pixverse")
                    await email_loc.fill(creds.get("email", ""))
                    await asyncio.sleep(0.5)
                    pwd_loc = page.locator("input[placeholder*='密码']").first
                    if await pwd_loc.is_visible(timeout=2000):
                        await pwd_loc.fill(creds.get("password", ""))
                    await asyncio.sleep(0.5)
                    # Enter 提交 + force click 双保险
                    await pwd_loc.press("Enter")
                    await asyncio.sleep(0.5)
                    try:
                        btn = page.locator("button[type='submit']").first
                        await btn.click(force=True, timeout=3000)
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                print("[DEBUG] 登录循环结束", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] 登录跳过: {e}", file=sys.stderr)

    # ========== 生成 ==========

    async def _ensure_mode(self, page: Page) -> None:
        """显式选择 PixVerse 生成模式（默认 story）。

        landing 页有一排控制：16:9 / Mode · Story / …。Mode 是下拉触发器。
        连接模式跨视频复用同一标签页，PixVerse 可能因上一次生成 / 离开操作把模式切走
        （实测第 2、3 个视频会变成 mini-game）；且「触发器文字」和「真实生成模式」
        可能错位（文字显示 Story、实际却以 mini-game 开跑）。

        因此**不依赖文字判断**：每个视频都显式打开下拉并点选目标选项。
        点已选中的选项在大多数下拉里是 no-op，安全；这样无论文字/真实模式是否错位，
        都能强制落回目标模式。
        """
        label = self._mode.capitalize()  # 'story' -> 'Story'
        try:
            # 先尽量等落地页输入框出现，确保我们是在 landing 页选模式，而非生成页
            try:
                await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                    state="visible", timeout=15000
                )
            except Exception:
                print(
                    "[WARN] 未检测到落地页输入框，模式选择可能在生成页进行"
                    "（请确认上一视频已复位到 landing）",
                    file=sys.stderr,
                )

            # 触发器：多候选，取第一个可见
            mode_btn = None
            for sel in [
                "button:has-text('Mode')",
                "button[class*='Mode']",
                "[role='button']:has-text('Mode')",
            ]:
                try:
                    b = page.locator(sel).first
                    if await b.is_visible(timeout=3000):
                        mode_btn = b
                        break
                except Exception:
                    continue
            if mode_btn is None:
                print(
                    "[WARN] 未找到 Mode 按钮，无法确保模式（将使用页面当前/默认模式，"
                    "若非目标模式请检查 Mode 选择器）",
                    file=sys.stderr,
                )
                return

            before = (await mode_btn.inner_text() or "").strip()
            print(f"[DEBUG] 模式选择前: {before!r}（目标 {label}）", file=sys.stderr)

            # 始终打开下拉并点选目标选项（不跳过，避免文字/真实模式错位）
            await mode_btn.click(timeout=5000)
            await asyncio.sleep(0.6)

            picked = False
            for sel in [
                f"div[role='option']:has-text('{label}')",
                f"li[role='option']:has-text('{label}')",
                f"[role='option']:has-text('{label}')",
                f"[role='menuitem']:has-text('{label}')",
                f"text={label}",
            ]:
                try:
                    opt = page.locator(sel).first
                    if await opt.is_visible(timeout=1500):
                        await opt.click(timeout=3000)
                        picked = True
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    continue

            # 关闭可能仍打开的下拉（点页面左上角空白）
            try:
                await page.mouse.click(5, 5)
            except Exception:
                pass
            await asyncio.sleep(0.3)

            after = (await mode_btn.inner_text() or "").strip()
            if label in (after or ""):
                print(f"[DEBUG] 模式已确认为 {label}", file=sys.stderr)
            elif picked:
                print(
                    f"[WARN] 已点选 {label} 但触发器仍显示 {after!r}，请检查 Mode 下拉实现",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[WARN] 未在下拉中找到 {label} 选项（当前 {after!r}），"
                    f"将使用页面默认模式",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[DEBUG] 模式选择异常（继续）: {e}", file=sys.stderr)

    async def _start_session(self, page: Page, prompt: str) -> None:
        """启动会话：选 Story 模式 → 填初始 prompt → 点星星 → 等生成页就绪
        → 等视频真正开始播放 → 开录 → 跑注入循环。

        与 Happy Oyster 一致：所有注入在内部循环处理，完成后置 is_done，
        调度器不再重复派发（避免初始 prompt 被当作指令二次注入）。
        """
        # 0. 显式选择模式（默认 story；PixVerse 可能记住上次模式，必须点选）
        self._begin_job_attempt()
        await self._ensure_mode(page)

        input_loc = page.locator(self.SELECTORS["landing_textarea"])
        await input_loc.fill(prompt)
        await asyncio.sleep(0.5)
        await self._click_star(page)
        self.initial_prompt_time_s = round(
            time.monotonic() - self._job_start_monotonic, 1
        )

        print("[DEBUG] 等待生成页就绪...", file=sys.stderr)
        deadline = time.monotonic() + self._max_queue_wait
        while time.monotonic() < deadline:
            body = await page.evaluate("() => document.body.innerText || ''")
            if "What would you like to happen" in body or "left in your session" in body or "Preparing" in body:
                print("[DEBUG] 生成页就绪", file=sys.stderr)
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError(f"等待生成页超时 {self._max_queue_wait}s")

        # 生成页刚就绪，给 UI 一点时间稳定
        await asyncio.sleep(2)
        # 关 BGM（一次点击）
        await self._toggle_bgm_off_pv(page)
        await asyncio.sleep(1)

        await self._detect_content_region(page)

        # 等视频真正开始播放，确认后立刻开录
        await self._wait_for_playback(page)
        self.first_video_chunk_time_s = round(
            time.monotonic() - self._job_start_monotonic, 1
        )
        self.generation_start_monotonic = time.monotonic()
        await self._recorder_start(page)
        # 录屏开始后的视频时钟作为偏移——后续注入在 offset + target_time 触发
        start_vt = await self._get_video_time(page) or 0.0
        # EV 热键延迟补偿（秒）：pyautogui 发键 → EV 实际开录的间隙
        hotkey_lag = float(self.config.get("recorder_lag_s", 0.8))
        self._video_offset = start_vt + hotkey_lag
        print(
            f"[DEBUG] 录屏开始：视频时钟={start_vt:.1f}s，注入偏移=+{start_vt:.1f}s",
            file=sys.stderr,
        )
        self._session_started = True

        # 注入循环：消费 config_extra["_inject_events"]，完成置 is_done
        events = self.config.get("_inject_events", [])
        end_delay = self.config.get("_end_delay", 0.0)
        self._injection_log = []  # 记录每次注入的实际时间（spec 格式）

        # 记录 initial prompt（Track A & B 都有）
        prompt_schedule = self.config.get("_prompt_schedule", [])
        initial = prompt_schedule[0] if prompt_schedule else {}
        self._injection_log.append({
            "prompt_id": initial.get("prompt_id", ""),
            "role": "initial",
            "scheduled_media_time_s": 0.0,
            "actual_media_time_s": 0.0,
            "actual_injection_time_s": self.initial_prompt_time_s,
            "status": "accepted",
            "error": None,
        })

        await self._run_injection_loop(page, events, end_delay, hotkey_lag)
        self._injections_done = True

    async def _click_star(self, page: Page) -> None:
        for sel in [self.SELECTORS["star_button"], self.SELECTORS["star_button_fallback"]]:
            try:
                btn = page.locator(sel).last
                if await btn.is_visible(timeout=3000):
                    await btn.click(timeout=5000)
                    print("[DEBUG] 星星已点击", file=sys.stderr)
                    return
            except Exception:
                pass
        print("[DEBUG] 星星失败，fallback Enter", file=sys.stderr)
        await page.locator(self.SELECTORS["landing_textarea"]).press("Enter")

    async def _ensure_publish_on(self, page: Page) -> None:
        """确保 Publish World Exploration 开关为开启状态。

        开启后 PixVerse 会保留可下载的原视频，无需再用外部录屏作为唯一素材来源。
        若 config["publish"] 为显式 False，则改为确保关闭（兼容旧行为）。
        """
        want_on = bool(self._publish)
        try:
            toggle = page.locator("[role='switch'], [aria-checked]").first
            if not await toggle.is_visible(timeout=5000):
                print("[DEBUG] 未找到 Publish 开关，跳过", file=sys.stderr)
                return
            checked = await toggle.get_attribute("aria-checked")
            if want_on:
                if checked != "true":
                    await toggle.click()
                    await asyncio.sleep(0.3)
                    print("[DEBUG] Publish 已开启（可直接下载原视频）", file=sys.stderr)
                else:
                    print("[DEBUG] Publish 已处于开启状态", file=sys.stderr)
            else:
                # 旧行为：确保关闭
                if checked == "true":
                    await toggle.click()
                    await asyncio.sleep(0.3)
                    print("[DEBUG] Publish 已关闭", file=sys.stderr)
                else:
                    print("[DEBUG] Publish 已处于关闭状态", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] Publish 开关操作失败: {e}", file=sys.stderr)

    # ========== 播放信号 / 注入循环（镜像 Happy Oyster） ==========

    async def _get_video_time(self, page: Page) -> float | None:
        """读取生成 <video> 的 currentTime（秒），作为注入循环的时钟。无 video 返回 None。"""
        try:
            return await page.evaluate("""() => {
                const v = document.querySelector('video');
                if (!v) return null;
                const t = v.currentTime;
                if (typeof t !== 'number' || !isFinite(t)) return null;
                return t;
            }""")
        except Exception:
            return None

    async def _wait_for_playback(self, page: Page, timeout: float | None = None) -> bool:
        """等视频真正开始播放：以 <video>.currentTime 递增为信号。

        返回 True 表示检测到播放；超时抛错（绝不盲录加载死屏）。
        加载百分比若卡住也只打日志、不作为失败信号（用户确认 PixVerse 加载慢但最终能生成）。
        """
        timeout = timeout or self._max_load_wait
        last: float | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = await self._get_video_time(page)
            if t is not None:
                if last is None:
                    # 第一次读到时钟：如果已经跑过 0.5s 以上，说明 BGM hover 期间
                    # 视频早已开始播放，无需再等递增，直接确认并开录。
                    if t > 0.5:
                        print(
                            f"[DEBUG] 视频已播放 {t:.1f}s（>0.5s），直接确认并开录",
                            file=sys.stderr,
                        )
                        return True
                    last = t
                    print(f"[DEBUG] 视频时钟出现: {t:.1f}s（等待递增确认播放）", file=sys.stderr)
                elif t > last + 0.05:
                    print(
                        f"[DEBUG] 视频时钟递增 {last:.1f}s → {t:.1f}s，确认开始播放",
                        file=sys.stderr,
                    )
                    return True
                else:
                    last = t
            else:
                try:
                    pct = await page.evaluate(
                        "() => { const m = document.body.innerText.match(/(\\d{1,3})\\s*%/);"
                        " return m ? parseInt(m[1], 10) : null; }"
                    )
                except Exception:
                    pct = None
                if pct is not None and pct != self._last_pct:
                    print(f"[DEBUG] 加载中... {pct}%（继续等待生成开始）", file=sys.stderr)
                    self._last_pct = pct
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"未检测到 PixVerse 视频开始播放（视频时钟未在 {timeout:.0f}s 内递增）。"
            "未启动录屏，避免录到加载死屏。请检查 PixVerse 播放信号或 _get_video_time 选择器。"
        )

    async def _run_injection_loop(
        self, page: Page, events: list[dict], end_delay: float, hotkey_lag: float = 0.8
    ) -> None:
        """按视频时钟在精准时刻注入后续 prompt。

        每 250ms 轮询 <video>.currentTime。注入目标时间 = _video_offset + target_time，
        其中 _video_offset 是录制开始瞬间的视频时钟——保证「第 t 秒注入」里的 t
        是相对录制起点的经过秒数，而不是视频的绝对播放时间。
        """
        tolerance = 0.8
        poll_interval = 0.25
        offset = getattr(self, "_video_offset", 0.0)
        # 过滤 time==0 的事件（已在 _start_session 中作为 initial prompt 注入并记录）
        targets = sorted(
            [
                (float(e["time"]), str(e["prompt"]),
                 e.get("prompt_id", ""), e.get("role", "update"))
                for e in events
                if float(e.get("time", 0)) > 0
            ],
            key=lambda x: x[0],
        )
        if not targets:
            print("[DEBUG] 无注入事件（仅 initial prompt），等待录制时长后停录屏", file=sys.stderr)
            target_duration = end_delay + hotkey_lag
            sleep_until = self.generation_start_monotonic + target_duration
            remaining = sleep_until - time.monotonic()
            if remaining > 0:
                print(
                    f"[DEBUG] 等待 {remaining:.0f}s 至录制时长 {target_duration:.0f}s 后停录屏",
                    file=sys.stderr,
                )
                await asyncio.sleep(remaining)
            self.generation_complete_time_s = round(
                time.monotonic() - self._job_start_monotonic, 1
            )
            print("[DEBUG] 停止录屏 → 立刻点 Leave 结束生成", file=sys.stderr)
            await self._recorder_stop(page)
            try:
                leave_btn = page.locator(self.SELECTORS["leave_button"]).first
                if await leave_btn.is_visible(timeout=3000):
                    await leave_btn.click(timeout=5000)
                    print("[DEBUG] 已点 Leave，生成结束", file=sys.stderr)
            except Exception as e:
                print(f"[DEBUG] Leave 点击失败: {e}", file=sys.stderr)
            return
        print(
            f"[DEBUG] 注入循环启动（偏移 +{offset:.1f}s），共 {len(targets)} 个目标: "
            f"{[f'{t:.0f}s' for t, _, _, _ in targets]}（轮询 {poll_interval*1000:.0f}ms，容差 ±{tolerance:.0f}s）",
            file=sys.stderr,
        )

        idx = 0
        last_inject_at_target: float | None = None
        last_activity = time.monotonic()
        while idx < len(targets):
            current = await self._get_video_time(page)
            if current is None:
                await asyncio.sleep(poll_interval)
                continue

            target_time, prompt, prompt_id, role = targets[idx]
            trigger_at = offset + target_time
            if current >= trigger_at - tolerance:
                if last_inject_at_target == target_time:
                    await asyncio.sleep(poll_interval)
                    continue

                drift = current - trigger_at
                print(
                    f"[DEBUG] 注入 t={target_time:.0f}s → 触发时钟={trigger_at:.1f}s "
                    f"(当前视频={current:.1f}s，偏差 {drift:+.1f}s)",
                    file=sys.stderr,
                )
                await self._inject_command(page, prompt)
                self._injection_log.append({
                    "prompt_id": prompt_id,
                    "role": role,
                    "scheduled_media_time_s": target_time,
                    "actual_media_time_s": round(current, 1),
                    "actual_injection_time_s": round(time.monotonic() - self._job_start_monotonic, 1),
                    "status": "accepted",
                    "error": None,
                })
                last_inject_at_target = target_time
                idx += 1
                last_activity = time.monotonic()
                await asyncio.sleep(0.3)
                continue

            # 超时保护：60s 无注入则跳出
            if time.monotonic() - last_activity > 60:
                print("[WARN] 注入循环超时（60s无注入），跳出", file=sys.stderr)
                break

            await asyncio.sleep(poll_interval)

        # 收尾：在录制起点 + 目标时长处停止录屏，然后点 Leave。
        # 停录屏在前、Leave 在后，确保录制时长精确。
        last_target_time = max(t[0] for t in targets)
        if end_delay:
            extra = end_delay
        elif len(targets) >= 2:
            extra = targets[1][0] - targets[0][0]
        else:
            extra = 10
        target_duration = last_target_time + extra
        # 加上 hotkey 延迟，使录屏时长精确
        target_duration += hotkey_lag
        sleep_until = self.generation_start_monotonic + target_duration
        remaining = sleep_until - time.monotonic()
        if remaining > 0:
            print(
                f"[DEBUG] 注入完成，等待 {remaining:.0f}s 至录制时长 {target_duration:.0f}s 后停录屏",
                file=sys.stderr,
            )
            await asyncio.sleep(remaining)

        self.generation_complete_time_s = round(
            time.monotonic() - self._job_start_monotonic, 1
        )
        # 先停录屏 → 立刻点 Leave 结束生成（背靠背，不 sleep）
        print("[DEBUG] 停止录屏 → 立刻点 Leave 结束生成", file=sys.stderr)
        await self._recorder_stop(page)
        try:
            leave_btn = page.locator(self.SELECTORS["leave_button"]).first
            if await leave_btn.is_visible(timeout=3000):
                await leave_btn.click(timeout=5000)
                print("[DEBUG] 已点 Leave，生成结束", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] Leave 点击失败: {e}", file=sys.stderr)
        # 录制窗口诊断：视频时钟起止 + 墙钟时长，便于核对"录制是否对齐"
        try:
            end_vt = await self._get_video_time(page)
            elapsed = (
                time.monotonic() - self.generation_start_monotonic
                if self.generation_start_monotonic
                else None
            )
            print(
                f"[DEBUG] 录屏结束：结束视频时钟={end_vt} "
                f"墙钟录制时长={elapsed:.1f}s"
                f"（若视频时钟明显小于墙钟，说明该时钟非真实生成时长，需改用其它时钟）",
                file=sys.stderr,
            )
        except Exception:
            pass

    async def _inject_command(self, page: Page, command: str) -> None:
        """注入：填 textarea → 点发送按钮（fallback Enter）。"""
        stream_ta = page.locator(self.SELECTORS["stream_textarea"]).first
        await stream_ta.wait_for(state="visible", timeout=10000)
        await stream_ta.click()
        await stream_ta.fill("")
        await stream_ta.fill(command)
        await asyncio.sleep(0.2)
        try:
            send = page.locator("button[type='submit']").first
            if await send.is_visible(timeout=2000):
                await send.click()
                print(f"[DEBUG] 注入(发送): {command[:50]}", file=sys.stderr)
                return
        except Exception:
            pass
        await stream_ta.press("Enter")
        print(f"[DEBUG] 注入(Enter): {command[:50]}", file=sys.stderr)
        if self._post_inject_delay > 0:
            await asyncio.sleep(self._post_inject_delay)

    async def _toggle_bgm_off_pv(self, page: Page) -> None:
        """PixVerse 新版 UI：右上角播放器图标 → Sound settings → 关 background music。"""
        try:
            # 1. dump 右上角所有按钮帮助 debug
            buttons_dump = await page.evaluate("""() => {
                var vw = window.innerWidth;
                return Array.from(document.querySelectorAll('button')).map(function(b, i) {
                    var r = b.getBoundingClientRect();
                    if (r.width === 0) return null;
                    if (r.x < vw * 0.8) return null;
                    return {i: i, text: (b.innerText || '').trim().slice(0, 30),
                            w: Math.round(r.width), x: Math.round(r.x), y: Math.round(r.y)};
                }).filter(Boolean);
            }""")
            print(f"[DEBUG-BGM] 右上角按钮: {buttons_dump}", file=sys.stderr)
            found = await page.evaluate("""() => {
                var vw = window.innerWidth;
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    var r = btns[i].getBoundingClientRect();
                    if (r.width > 10 && r.width < 60 && r.x > vw * 0.85 && r.y < 120) {
                        return i;
                    }
                }
                return -1;
            }""")
            if found < 0:
                print("[DEBUG-BGM] 未找到播放器图标", file=sys.stderr); return

            btn = page.locator("button").nth(found)
            await btn.click(timeout=3000)
            await asyncio.sleep(1.5)
            print("[DEBUG-BGM] 已点播放器图标", file=sys.stderr)

            # 2. Sound settings 面板：两个 switch，第一个=Sound Effects（不动），
            # 第二个=Background Music（关闭）
            await page.evaluate("""() => {
                var switches = document.querySelectorAll('[role="switch"]');
                // 遍历找 aria-checked='true' 的——它们是开着的
                var musicSwitches = [];
                for (var i = 0; i < switches.length; i++) {
                    if (switches[i].getAttribute('aria-checked') === 'true')
                        musicSwitches.push(switches[i]);
                }
                // 点最后一个（background music 在最下面）
                var result = 'no-match';
                if (musicSwitches.length >= 2) {
                    musicSwitches[musicSwitches.length - 1].click();
                    result = 'clicked-bgm';
                }
                return {result: result, totalSwitches: switches.length, checkedCount: musicSwitches.length};
            }""")

            await page.mouse.click(100, 100)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[DEBUG-BGM] BGM关闭失败: {e}", file=sys.stderr)

    async def wait_for_ready(self, page: Page, timeout: float = 300) -> None:
        if not self._session_started:
            return
        try:
            loc = page.locator(self.SELECTORS["stream_textarea"]).first
            await loc.wait_for(state="visible", timeout=timeout * 1000)
        except PlaywrightTimeout:
            pass

    async def _start_stream_capture(self, page: Page) -> None:
        """captureStream 测试：从 video 直接录制。"""
        try:
            ok = await page.evaluate("""() => {
                var v = document.querySelector('video');
                if (!v || !v.captureStream || v.videoWidth === 0 || v.paused) return false;
                window._pxChunks = [];
                var s = v.captureStream(0);
                var mime = MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'video/webm;codecs=vp9' : 'video/webm';
                var rec = new MediaRecorder(s, {mimeType: mime, videoBitsPerSecond: 10000000});
                rec.ondataavailable = function(e) { if (e.data.size > 0) window._pxChunks.push(e.data); };
                rec.start(5000);
                window._pxRec = rec;
                return true;
            }""")
            if ok:
                print("[DEBUG] 视频流录制已启动", file=sys.stderr)
            else:
                print("[DEBUG] 视频流录制未启动（video 未就绪）", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] 视频流启动失败: {e}", file=sys.stderr)

    async def _stop_stream_capture(self, page: Page) -> str | None:
        """停止录制并保存。"""
        try:
            await page.evaluate("() => { if (window._pxRec) window._pxRec.stop(); }")
            await asyncio.sleep(3)
            result = await page.evaluate("""() => {
                if (!window._pxChunks || !window._pxChunks.length) return null;
                var blob = new Blob(window._pxChunks, {type: 'video/webm'});
                return new Promise(function(resolve) {
                    var reader = new FileReader();
                    reader.onloadend = function() { resolve({size: blob.size, b64: reader.result}); };
                    reader.readAsDataURL(blob);
                });
            }""")
            if result and result.get("b64") and result["size"] > 1000:
                import os as _os, base64 as _b64, time as _time
                d = "outputs/videos_captured"
                _os.makedirs(d, exist_ok=True)
                p = _os.path.join(d, f"pixverse_{int(_time.time())}.webm")
                raw = _b64.b64decode(result["b64"].split(",", 1)[1])
                with open(p, "wb") as f:
                    f.write(raw)
                print(f"[DEBUG] 纯净视频: {len(raw)/1024:.0f}KB → {p}", file=sys.stderr)
                return p
        except Exception as e:
            print(f"[DEBUG] 视频流导出失败: {e}", file=sys.stderr)
        return None
