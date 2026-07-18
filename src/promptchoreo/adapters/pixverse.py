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
    URL = "https://world.pixverse.ai/generate/"

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
        self._last_pct: int | None = None
        self.generation_start_monotonic: float | None = None
        self.stop_monotonic: float | None = None
        self._max_queue_wait = self.config.get("max_queue_wait", 300)
        self._max_load_wait = self.config.get("max_load_wait", 600)
        self._post_inject_delay = self.config.get("post_inject_delay", 0.5)
        self._mode = (self.config.get("mode") or "story").lower()  # 默认 story 模式
        # Publish World Exploration 开关：默认开启，开启后可直接下载原视频。
        # 设为 false 才关闭（兼容旧行为）。
        self._publish = self.config.get("publish", True)
        self.crop_region: str | None = None

    async def setup(self, page: Page) -> None:
        await page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await self._ensure_logged_in(page)
        input_loc = page.locator(self.SELECTORS["landing_textarea"])
        await input_loc.wait_for(state="visible", timeout=30000)
        # 打开即全屏，提升 EV 录屏分辨率
        await self._enter_fullscreen(page)
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
        await self._ensure_mode(page)

        input_loc = page.locator(self.SELECTORS["landing_textarea"])
        await input_loc.fill(prompt)
        await asyncio.sleep(0.5)
        await self._click_star(page)

        print("[DEBUG] 等待生成页就绪...", file=sys.stderr)
        deadline = time.monotonic() + self._max_queue_wait
        while time.monotonic() < deadline:
            body = await page.evaluate("() => document.body.innerText || ''")
            if "What would you like to happen" in body or "left in your session" in body:
                print("[DEBUG] 生成页就绪", file=sys.stderr)
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError(f"等待生成页超时 {self._max_queue_wait}s")

        await asyncio.sleep(3)
        # 测出真正的视频内容包围盒，作为裁剪区（去掉周边 UI）
        await self._detect_content_region(page)
        # 确保 Publish World Exploration 开启（开启后可直接下载原视频）
        await self._ensure_publish_on(page)

        # 等视频真正开始播放（<video>.currentTime 递增）才开录，绝不录加载死屏
        await self._wait_for_playback(page)
        # 播放开始后 hover 画面并点击 🎵 关闭配乐；此时浮层按钮才稳定出现。
        await self._toggle_bgm_off(page)
        self.generation_start_monotonic = time.monotonic()
        start_vt = await self._get_video_time(page)
        print(f"[DEBUG] 录屏开始：视频时钟={start_vt}（以此刻为录制起点）", file=sys.stderr)
        self._session_started = True
        await self._recorder_start(page)

        # 注入循环：消费 config_extra["_inject_events"]，完成置 is_done
        events = self.config.get("_inject_events", [])
        end_delay = self.config.get("_end_delay", 0.0)
        await self._run_injection_loop(page, events, end_delay)
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
        self, page: Page, events: list[dict], end_delay: float
    ) -> None:
        """按视频时钟在精准时刻注入后续 prompt（镜像 Happy Oyster）。

        每 250ms 轮询 <video>.currentTime，当到达下一个目标时刻的 ±容差内时注入。
        全部注入完成后等待 interval 秒（供内容沉淀），再停止外部录屏。
        """
        tolerance = 0.8
        poll_interval = 0.25
        targets = sorted(
            [(float(e["time"]), str(e["prompt"])) for e in events],
            key=lambda x: x[0],
        )
        if not targets:
            print("[DEBUG] 无注入事件，跳过注入循环", file=sys.stderr)
            return
        print(
            f"[DEBUG] 注入循环启动，共 {len(targets)} 个目标: "
            f"{[f'{t:.0f}s' for t, _ in targets]}（轮询 {poll_interval*1000:.0f}ms，容差 ±{tolerance:.0f}s）",
            file=sys.stderr,
        )

        idx = 0
        last_inject_at_target: float | None = None
        while idx < len(targets):
            current = await self._get_video_time(page)
            if current is None:
                await asyncio.sleep(poll_interval)
                continue

            target_time, prompt = targets[idx]
            if current >= target_time - tolerance:
                if last_inject_at_target == target_time:
                    await asyncio.sleep(poll_interval)
                    continue

                drift = current - target_time
                print(
                    f"[DEBUG] 注入 t={target_time:.0f}s (视频时钟 {current:.1f}s，偏差 {drift:+.1f}s)",
                    file=sys.stderr,
                )
                await self._inject_command(page, prompt)
                last_inject_at_target = target_time
                idx += 1
                await asyncio.sleep(0.3)
                continue

            await asyncio.sleep(poll_interval)

        # 收尾：等一段（供最后一条指令的内容沉淀）后停止外部录屏。
        # 尾巴长度优先用 end_delay（用户在清单里设定 = 最后一条注入后继续录制的秒数），
        # 这样录制结束点由用户控制、与清单对齐；end_delay 为 0 才回退到事件间隔/默认。
        if end_delay and end_delay > 0:
            tail = float(end_delay)
        elif len(targets) >= 2:
            tail = targets[1][0] - targets[0][0]
        else:
            tail = 10.0
        print(
            f"[DEBUG] 注入完成，最后一条在视频时钟 {targets[-1][0]:.0f}s；"
            f"按 end_delay={end_delay} 继续录制 {tail:.0f}s 后停止",
            file=sys.stderr,
        )
        await asyncio.sleep(tail)
        await self._recorder_stop(page)
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
        stream_ta = page.locator(self.SELECTORS["stream_textarea"]).first
        await stream_ta.wait_for(state="visible", timeout=10000)
        await stream_ta.click()
        await stream_ta.fill("")
        await stream_ta.fill(command)
        await asyncio.sleep(0.2)
        await stream_ta.press("Enter")
        print(f"[DEBUG] 注入: {command[:50]}", file=sys.stderr)
        if self._post_inject_delay > 0:
            await asyncio.sleep(self._post_inject_delay)

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
