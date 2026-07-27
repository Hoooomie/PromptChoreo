"""Odyssey 交互模拟适配器。

URL: https://experience.odyssey.ml/

流程：
1. setup: 导航 → 填 initial_prompt → 提交 → 等排队 → 等模拟就绪 → 关闭音频
2. submit_prompt: 注入交互指令（Enter 发送）
3. teardown: 点右上角 X 关闭 → 记录停止时间戳
4. 异常信号：会话超时弹出「Your SessionEnded」→ 停录 → Try Again 复位

结果通过 Playwright 屏幕录制获得。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .base import RetryCurrentJob, SiteAdapter


class SessionEndedError(Exception):
    """Odyssey 会话时长耗尽，弹出了「Your Session Ended」。"""
    pass


class ContentBlockedError(Exception):
    """Odyssey 拒绝了 prompt，并显示 Content Blocked。"""

    def __init__(
        self,
        message: str,
        *,
        prompt_event: dict | None = None,
        prompt_events: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.prompt_event = prompt_event
        self.prompt_events = prompt_events or (
            [prompt_event] if prompt_event is not None else []
        )


class OdysseyAdapter(SiteAdapter):
    """Odyssey AI 交互模拟适配器。"""

    name = "odyssey"
    resets_clock = True

    URL = "https://experience.odyssey.ml/"

    SELECTORS = {
        "landing_textarea": "textarea[placeholder*='Describe']",
        "submit_button": "button:has-text('Start Simulating')",
        "stream_textarea": "textarea.header-xs",
        "close_button": "button[class*='left-[calc(100%+9px)]']",
        "video_overlay": "div.cursor-text",
        # 会话超时弹窗
        "session_ended_dialog": "text=Your Session Ended",
        "try_again_button": "button:has-text('Try Again')",
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._session_started = False
        self._injections_done = False
        self.generation_start_monotonic: float | None = None
        self.recording_start_monotonic: float | None = None
        self.stop_monotonic: float | None = None
        self.crop_region: str | None = None
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
        self._latest_prompt_event: dict | None = None
        self._content_blocked_events: list[dict] = []
        self._content_blocked_dialog_active = False
        self._retry_reason: str | None = None
        self._video_wait_retry_count = 0
        # Odyssey 在 Try Again 后的第一轮生成经常卡在 loading。
        # 该标记用于在真正的 job 重试前先消耗一次占位生成作为预热。
        self._warmup_needed_after_retry = False
        self._max_queue_wait = self.config.get("max_queue_wait", 300)
        self._post_inject_delay = self.config.get("post_inject_delay", 0.5)
        # 站点加载不稳定时，可在 yaml 里调大（单位秒），默认 8s
        self._load_wait = float(self.config.get("load_wait", 8))

    @property
    def is_done(self) -> bool:
        return self._injections_done

    async def setup(self, page: Page) -> None:
        self._begin_job_attempt()
        # 若标签页已停在输入框界面（上一次 run 的 teardown 已点 X 复位，
        # 或 launch 脚本直接开在这），跳过整页重载，直接复用——省一次网络请求，
        # 也避免重置用户手动开的全屏。
        already_at_input = False
        try:
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=3000
            )
            already_at_input = True
        except Exception:
            already_at_input = False

        if not already_at_input:
            await page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
            # Odyssey 站点加载不稳定，固定多等一会儿让前端 settle
            await asyncio.sleep(self._load_wait)
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=60000
            )

        # 全屏由用户手动控制（CDP 模式自动跳过），其余模式在此处理
        await self._enter_fullscreen(page)

        initial_prompt = self.config.get("initial_prompt")
        while True:
            # 在填写 initial prompt 之前确认当前会话剩余时间足够完成整个 job。
            # 剩余时间不足时，先等待会话自然耗尽，再点 Try Again 获取新会话。
            await self._ensure_session_budget(page)
            if self._warmup_needed_after_retry:
                await self._run_retry_warmup(page)
                self._warmup_needed_after_retry = False
            try:
                if initial_prompt:
                    print(
                        f"[DEBUG] setup 阶段启动模拟: initial_prompt={initial_prompt!r}",
                        file=sys.stderr,
                    )
                    await self._start_session(page, str(initial_prompt))
                return
            except RetryCurrentJob as exc:
                self._video_wait_retry_count += 1
                self._retry_reason = str(exc)
                self._session_started = False
                self._injections_done = False
                self.generation_start_monotonic = None
                self.recording_start_monotonic = None
                self.stop_monotonic = None
                self.crop_region = None
                self._injection_log = []
                self._latest_prompt_event = None
                self._content_blocked_events = []
                self._content_blocked_dialog_active = False
                self.initial_prompt_time_s = None
                self.first_video_chunk_time_s = None
                self.generation_complete_time_s = None
                self._warmup_needed_after_retry = True
                self._begin_job_attempt(force=True)
                print(
                    "[SessionBudget] 已点击 Try Again；"
                    f"同一 job 开始第 {self._video_wait_retry_count + 1} 次尝试",
                    file=sys.stderr,
                )

    async def _run_retry_warmup(self, page: Page) -> None:
        """Use and discard the first generation after ``Try Again``.

        Odyssey can leave the first post-reset generation in a loading state.
        Submit a harmless placeholder without starting the recorder, wait until
        the generation view is entered (or a short safety deadline expires),
        then close it so the real benchmark job starts from a clean input page.
        """
        warmup_prompt = "warmup"
        print(
            "[SessionBudget] Try Again 后执行一次占位生成预热（不录制、不计入当前 job）",
            file=sys.stderr,
        )

        input_loc = page.locator(self.SELECTORS["landing_textarea"])
        await input_loc.wait_for(state="visible", timeout=15000)
        await input_loc.click()
        await input_loc.fill(warmup_prompt)
        await asyncio.sleep(0.2)

        submit_btn = page.locator(self.SELECTORS["submit_button"])
        await submit_btn.wait_for(state="visible", timeout=10000)
        await submit_btn.click()

        # 只确认占位请求已经离开输入页/进入 simulating，不等待视频渲染。
        # 若首轮确实卡在 loading，安全期限到后仍执行关闭和复位。
        deadline = time.monotonic() + min(float(self._max_queue_wait), 30.0)
        while time.monotonic() < deadline:
            if await self._check_session_ended(page, raise_if_ended=False):
                await self._dismiss_session_ended(page)
                return

            try:
                body_text = await page.evaluate("() => document.body.innerText || ''")
                if re.search(r"simulating", body_text, re.I):
                    break
            except Exception:
                pass

            try:
                if not await input_loc.is_visible(timeout=500):
                    break
            except Exception:
                break
            await asyncio.sleep(0.5)

        await asyncio.sleep(0.5)
        await self._reset_to_input(page)
        await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
            state="visible", timeout=15000
        )
        print("[SessionBudget] 占位生成已跳过，开始当前 job", file=sys.stderr)

    async def submit_prompt(self, page: Page, prompt: str, target_time: float | None = None) -> None:
        if not self._session_started:
            await self._start_session(page, prompt)
        else:
            await self._inject_command(page, prompt)

    async def _get_top_timer_seconds(self, page: Page) -> float | None:
        """读取页面顶部居中的 M:SS 倒计时，返回剩余秒数。"""
        try:
            return await page.evaluate(
                """() => {
                    const candidates = [];
                    const els = document.querySelectorAll('*');
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0 || r.top < -2 || r.top > 120)
                            continue;
                        if (r.width > 180 || r.height > 80)
                            continue;
                        const text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                        const m = text.match(/^(\\d{1,3}):(\\d{2})$/);
                        if (!m) continue;
                        const seconds = Number(m[1]) * 60 + Number(m[2]);
                        candidates.push({
                            seconds,
                            text,
                            top: r.top,
                            centerDistance: Math.abs((r.left + r.width / 2) - innerWidth / 2),
                            area: r.width * r.height,
                        });
                    }
                    candidates.sort((a, b) =>
                        a.centerDistance - b.centerDistance ||
                        a.top - b.top || a.area - b.area
                    );
                    return candidates.length ? candidates[0].seconds : null;
                }"""
            )
        except Exception as exc:
            print(f"[SessionBudget] 读取顶部倒计时失败: {exc}", file=sys.stderr)
            return None

    async def _ensure_session_budget(self, page: Page) -> None:
        """initial prompt 前确保剩余会话时间足够完成当前 job。"""
        required = float(self.config.get("_required_duration_s", 0) or 0)
        guard = float(self.config.get("_session_guard_s", 15) or 15)
        if required <= 0:
            return

        # 页面刚加载时倒计时可能还未挂载，短暂轮询后再决定。
        remaining: float | None = None
        for _ in range(40):
            remaining = await self._get_top_timer_seconds(page)
            if remaining is not None:
                break
            await asyncio.sleep(0.25)

        if remaining is None:
            print(
                "[SessionBudget] 未检测到顶部倒计时，继续执行 job（不自动等待超时）",
                file=sys.stderr,
            )
            return

        threshold = required + guard
        print(
            f"[SessionBudget] 当前剩余 {remaining:.0f}s；"
            f"job 需要 {required:.0f}s + 安全余量 {guard:.0f}s = {threshold:.0f}s",
            file=sys.stderr,
        )
        if remaining >= threshold:
            print("[SessionBudget] 剩余时间足够，直接开始 job", file=sys.stderr)
            return

        print(
            "[SessionBudget] 剩余时间不足，等待会话耗尽后点击 Try Again",
            file=sys.stderr,
        )
        deadline = time.monotonic() + max(remaining + 30, 60)
        while time.monotonic() < deadline:
            if await self._check_session_ended(page, raise_if_ended=False):
                await self._dismiss_session_ended(page)
                print("[SessionBudget] 已获取新会话，继续当前 job", file=sys.stderr)
                return
            await asyncio.sleep(0.5)

        raise RuntimeError(
            "等待会话超时：未检测到 Your Session Ended 弹窗，"
            "为避免在剩余时间不足时提交 initial prompt，已停止当前 job"
        )

    async def _raise_if_content_blocked(
        self, page: Page, prompt_event: dict | None = None
    ) -> None:
        """Raise a structured error when Odyssey shows its moderation dialog."""
        if not await self._content_blocked_is_visible(page):
            return

        if self.recording_start_monotonic is not None:
            await self._handle_playback_content_blocked(
                page, prompt_event=prompt_event, detected=True
            )
            return

        error = (
            "content_blocked: Your request was flagged for inappropriate content."
        )
        failed_event = None
        if prompt_event is not None:
            failed_event = dict(prompt_event)
            failed_event.update({"status": "failed", "error": error})
            self._injection_log = [*self._injection_log, failed_event]

        print(f"[ContentBlocked] {error}", file=sys.stderr)
        await self._dismiss_content_blocked(page)
        raise ContentBlockedError(
            error,
            prompt_event=failed_event,
            prompt_events=list(self._injection_log),
        )

    async def _content_blocked_is_visible(self, page: Page) -> bool:
        """Return whether Odyssey's Content Blocked dialog is visible."""
        try:
            body_text = await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            return False

        return bool(
            re.search(
                r"content\s+blocked|flagged\s+for\s+inappropriate\s+content",
                body_text,
                re.I,
            )
        )

    async def _handle_playback_content_blocked(
        self,
        page: Page,
        prompt_event: dict | None = None,
        *,
        detected: bool = False,
    ) -> bool:
        """Close a playback-time moderation dialog without stopping recording."""
        if not detected and not await self._content_blocked_is_visible(page):
            if self._content_blocked_dialog_active:
                self._content_blocked_events[-1]["dialog_closed"] = True
            self._content_blocked_dialog_active = False
            return False

        error = (
            "content_blocked: Your request was flagged for inappropriate content."
        )
        associated_event = prompt_event or self._latest_prompt_event
        if not self._content_blocked_dialog_active:
            if associated_event is not None:
                associated_event["status"] = "failed"
                associated_event["error"] = error

            media_time_s = (
                round(time.monotonic() - self.recording_start_monotonic, 1)
                if self.recording_start_monotonic is not None
                else None
            )
            incident = {
                "media_time_s": media_time_s,
                "job_time_s": round(
                    time.monotonic() - self._job_start_monotonic, 1
                ),
                "prompt_id": (
                    associated_event.get("prompt_id", "")
                    if associated_event is not None
                    else ""
                ),
                "role": (
                    associated_event.get("role", "")
                    if associated_event is not None
                    else ""
                ),
                "dialog_closed": False,
            }
            self._content_blocked_events.append(incident)
            self._content_blocked_dialog_active = True
            print(
                "[ContentBlocked] 播放过程中检测到拦截弹窗；"
                "保持录屏并自动点击右上角 X",
                file=sys.stderr,
            )

        clicked = await self._close_content_blocked_dialog(page)
        if clicked:
            self._content_blocked_events[-1]["dialog_closed"] = True
        return True

    async def _sleep_while_monitoring_content_blocked(
        self, page: Page, duration_s: float, poll_interval: float = 0.25
    ) -> None:
        """Sleep against the recording clock while dismissing moderation dialogs."""
        deadline = time.monotonic() + max(float(duration_s), 0.0)
        while True:
            await self._handle_playback_content_blocked(page)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(poll_interval, remaining))

    async def _close_content_blocked_dialog(self, page: Page) -> bool:
        """Click only the moderation dialog X, leaving playback and recording active."""
        clicked = False
        try:
            # Locate the closest dialog container from its exact title, then
            # choose the visible button nearest the container's top-right corner.
            clicked = bool(
                await page.evaluate(
                    """() => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const s = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 &&
                                s.visibility !== 'hidden' && s.display !== 'none';
                        };
                        const title = [...document.querySelectorAll('*')].find(
                            (el) => visible(el) &&
                                (el.textContent || '').trim() === 'Content Blocked'
                        );
                        if (!title) return false;

                        let container = title;
                        for (let depth = 0; container && depth < 7; depth++) {
                            const buttons = [
                                ...container.querySelectorAll(
                                    'button, [role="button"]'
                                )
                            ].filter(visible);
                            if (buttons.length) {
                                const cr = container.getBoundingClientRect();
                                buttons.sort((a, b) => {
                                    const ar = a.getBoundingClientRect();
                                    const br = b.getBoundingClientRect();
                                    const as = Math.abs(cr.right - ar.right) +
                                        Math.abs(cr.top - ar.top);
                                    const bs = Math.abs(cr.right - br.right) +
                                        Math.abs(cr.top - br.top);
                                    return as - bs;
                                });
                                buttons[0].click();
                                return true;
                            }
                            container = container.parentElement;
                        }
                        return false;
                    }"""
                )
            )
        except Exception as exc:
            print(f"[ContentBlocked] 点击弹窗 X 失败: {exc}", file=sys.stderr)

        if clicked:
            print("[ContentBlocked] 已点击弹窗右上角 X，继续播放", file=sys.stderr)
        else:
            print(
                "[ContentBlocked] 暂未定位到弹窗 X，将在下一轮继续尝试",
                file=sys.stderr,
            )
        return clicked

    async def _dismiss_content_blocked(self, page: Page) -> None:
        """Stop recording, close a fatal pre-playback dialog, and reset."""
        if self._session_started or self.recording_start_monotonic is not None:
            print("[ContentBlocked] 先停止当前录屏", file=sys.stderr)
            await self._recorder_stop(page)

        await self._close_content_blocked_dialog(page)
        await asyncio.sleep(0.5)
        await self._reset_to_input(page)

    async def _start_session(self, page: Page, prompt: str) -> None:
        self._begin_job_attempt()
        input_loc = page.locator(self.SELECTORS["landing_textarea"])
        await input_loc.wait_for(state="visible", timeout=60000)
        await input_loc.click()
        await input_loc.fill(prompt)
        await asyncio.sleep(0.5)

        submit_btn = page.locator(self.SELECTORS["submit_button"])
        await submit_btn.wait_for(state="visible", timeout=10000)
        await submit_btn.click()
        self.initial_prompt_time_s = round(
            time.monotonic() - self._job_start_monotonic, 1
        )
        prompt_schedule = self.config.get("_prompt_schedule", [])
        initial = prompt_schedule[0] if prompt_schedule else {}
        initial_event = {
            "prompt_id": initial.get("prompt_id", ""),
            "role": "initial",
            "scheduled_media_time_s": 0.0,
            "actual_media_time_s": 0.0,
            "actual_injection_time_s": self.initial_prompt_time_s,
            "status": "accepted",
            "error": None,
        }
        self._latest_prompt_event = initial_event
        await self._raise_if_content_blocked(page, initial_event)

        print("[DEBUG] 等待模拟就绪...", file=sys.stderr)
        deadline = time.monotonic() + self._max_queue_wait
        while time.monotonic() < deadline:
            body_text = await page.evaluate("() => document.body.innerText || ''")
            await self._raise_if_content_blocked(page, initial_event)
            if re.search(r"simulating", body_text, re.I):
                print("[DEBUG] 检测到 'simulating' 文本", file=sys.stderr)
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError(f"排队超时（{self._max_queue_wait}s），模拟未就绪")

        # 生成已开始 → 立即启动外部录屏（尽量早，不遗漏开头）。
        # 之后的 sleep/等交互框/静音都在录制范围内，无妨。
        self.generation_start_monotonic = time.monotonic()
        self._session_started = True
        await self._recorder_start(page)
        # 外部录屏热键在 _recorder_start() 内发送；从这里开始，时间轴和录屏
        # 使用同一个起点。不能使用 generation_start_monotonic，因为它早于
        # 等待视频就绪和发送录屏热键，通常会造成首条 prompt 提前数秒。
        self.recording_start_monotonic = time.monotonic()
        print(
            "[DEBUG] 录屏计时起点已建立："
            f"generation_start={self.generation_start_monotonic:.3f}, "
            f"recording_start={self.recording_start_monotonic:.3f}",
            file=sys.stderr,
        )

        await self._sleep_while_monitoring_content_blocked(page, 5)

        stream_ta = await self._find_stream_textarea(page, timeout=120)
        if stream_ta is None:
            raise RuntimeError("找不到交互 textarea")
        await stream_ta.wait_for(state="visible", timeout=30000)
        print("[DEBUG] 交互 textarea 已出现", file=sys.stderr)

        # 关闭配乐（点生成界面 🎵），保留视频原声（不做浏览器层静音）
        await self._toggle_bgm_off(page)

        # 测出真正的视频/画布内容包围盒，作为裁剪区（去掉周边 UI）
        # canvas 可能晚几秒才撑满尺寸，重试几次
        for _ in range(10):
            await self._handle_playback_content_blocked(page)
            await self._detect_content_region(page)
            if self.crop_region:
                break
            await self._sleep_while_monitoring_content_blocked(page, 1)

        # 记录 initial prompt（spec 格式）
        self._injection_log = []
        self._injection_log.append(initial_event)

        # 注入循环：消费后续 update 事件
        events = self.config.get("_inject_events", [])
        end_delay = self.config.get("_end_delay", 0.0)
        await self._run_injection_loop(page, events, end_delay)
        self._injections_done = True

    async def _wait_video_ready(self, page: Page, timeout: float = 6) -> None:
        """无限等待首个视频画面，同时守住完成当前 job 的会话时间预算。

        ``timeout`` 仅为兼容基类签名；Odyssey 不再按固定秒数放弃等待。只要
        剩余会话时间仍大于 ``job 时长 + 3s`` 就继续等待。如果触及底线，
        本次生成永久放弃，即使视频随后出现也不会启动录屏。
        """
        required = float(self.config.get("_required_duration_s", 0) or 0)
        guard = float(self.config.get("_render_guard_s", 3) or 3)
        threshold = required + guard
        print(
            "[DEBUG] 等待 Odyssey 视频元素开始渲染（不设固定超时）；"
            f"会话时间底线={required:.0f}s+{guard:.0f}s={threshold:.0f}s",
            file=sys.stderr,
        )

        while True:
            if await self._check_session_ended(page, raise_if_ended=False):
                await self._wait_for_session_end_and_reset(page)
                raise RetryCurrentJob("session_ended_while_waiting_for_video")

            remaining = await self._get_top_timer_seconds(page)
            if required > 0 and remaining is not None and remaining <= threshold:
                print(
                    f"[SessionBudget] 等待视频时剩余 {remaining:.0f}s，"
                    f"已达到 job 时长 + {guard:.0f}s 的放弃线；"
                    "本次结果不录制，等待会话耗尽",
                    file=sys.stderr,
                )
                await self._wait_for_session_end_and_reset(page)
                raise RetryCurrentJob(
                    "insufficient_session_time_while_waiting_for_video"
                )

            ready = await page.evaluate(
                """() => {
                    const els = document.querySelectorAll('video, canvas');
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 50 || r.height < 50) continue;
                        if (el.tagName === 'VIDEO') {
                            if (el.readyState >= 2 && el.videoWidth > 0) return true;
                        } else {
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if ready:
                self.first_video_chunk_time_s = round(
                    time.monotonic() - self._job_start_monotonic, 1
                )
                print(
                    "[DEBUG] Odyssey 视频元素已开始渲染，可以开始录制",
                    file=sys.stderr,
                )
                return

            await asyncio.sleep(0.3)

    async def _wait_for_session_end_and_reset(self, page: Page) -> None:
        """Once an attempt is abandoned, ignore late video and wait for reset."""
        while True:
            if await self._check_session_ended(page, raise_if_ended=False):
                await self._dismiss_session_ended(page)
                await page.locator(
                    self.SELECTORS["landing_textarea"]
                ).wait_for(state="visible", timeout=15000)
                return
            await asyncio.sleep(0.5)

    async def _check_session_ended(self, page: Page, *, raise_if_ended: bool = True) -> bool:
        """检测 Odyssey 会话超时弹窗「Your Session Ended」。

        Parameters
        ----------
        raise_if_ended : True（默认）
            检测到弹窗时抛出 SessionEndedError；设为 False 仅返回 bool。

        Returns
        -------
        True  → 弹窗出现（会话已结束）
        False → 弹窗未出现（正常）
        """
        try:
            dialog = page.locator(self.SELECTORS["session_ended_dialog"])
            if await dialog.is_visible(timeout=500):
                print("[SessionEnded] 检测到会话超时弹窗", file=sys.stderr)
                if raise_if_ended:
                    raise SessionEndedError("Odyssey 会话已耗尽：Your Session Ended")
                return True
        except PlaywrightTimeout:
            pass
        return False

    async def _dismiss_session_ended(self, page: Page) -> None:
        """点「Try Again」关闭会话超时弹窗，让站点回到输入框界面。"""
        try:
            btn = page.locator(self.SELECTORS["try_again_button"])
            if await btn.is_visible(timeout=3000):
                await btn.click(timeout=5000)
                print("[SessionEnded] 已点击 Try Again", file=sys.stderr)
                # 等 landing 输入框重新出现
                await page.locator(
                    self.SELECTORS["landing_textarea"]
                ).wait_for(state="visible", timeout=15000)
                print("[SessionEnded] 已回到输入框界面", file=sys.stderr)
            else:
                print("[SessionEnded] Try Again 按钮不可见", file=sys.stderr)
        except Exception as e:
            print(f"[SessionEnded] Try Again 点击失败: {e}", file=sys.stderr)

    async def _find_stream_textarea(self, page: Page, timeout: float = 60) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self._handle_playback_content_blocked(page)
            for sel in ["textarea", "textarea[enterkeyhint]", self.SELECTORS["stream_textarea"]]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1000):
                        return loc
                except Exception:
                    pass
            await self._sleep_while_monitoring_content_blocked(page, 3)
        return None

    async def _inject_command(
        self,
        page: Page,
        command: str,
        prompt_event: dict | None = None,
    ) -> None:
        # 注入前检查会话是否已耗尽——若弹出「Your Session Ended」，立即中断
        await self._check_session_ended(page)
        await self._handle_playback_content_blocked(page)
        self._latest_prompt_event = prompt_event

        try:
            click_area = page.locator(self.SELECTORS["video_overlay"]).first
            if await click_area.is_visible(timeout=2000):
                await click_area.click(position={"x": 100, "y": 50})
                await asyncio.sleep(0.3)
        except Exception:
            pass

        # 注入过程中交互框可能因超时消失，这里也做一次检测
        try:
            stream_ta = page.locator(self.SELECTORS["stream_textarea"]).first
            await stream_ta.wait_for(state="visible", timeout=10000)
            await stream_ta.click()
            await stream_ta.fill("")
            await stream_ta.fill(command)
            await asyncio.sleep(0.2)
            await stream_ta.press("Enter")
            print(f"[DEBUG] 已注入: {command[:50]}", file=sys.stderr)
        except PlaywrightTimeout:
            # 交互框不可见 → 可能是会话超时弹窗挡住了，再确认一次
            await self._check_session_ended(page)
            raise  # 不是超时弹窗 → 原样抛出 timeout

        if prompt_event is not None:
            prompt_event["actual_injection_time_s"] = round(
                time.monotonic() - self._job_start_monotonic, 1
            )
        await self._raise_if_content_blocked(page, prompt_event)

        if self._post_inject_delay > 0:
            await self._sleep_while_monitoring_content_blocked(
                page, self._post_inject_delay
            )

    async def _run_injection_loop(
        self, page: Page, events: list[dict], end_delay: float
    ) -> None:
        """按墙钟在精准时刻注入后续 prompt（Odyssey 无 video.currentTime）。"""
        clock_start = self.recording_start_monotonic or self.generation_start_monotonic
        if clock_start is None:
            raise RuntimeError("录屏计时起点尚未建立")

        poll_interval = 0.25
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
            target_duration = end_delay
            remaining = target_duration - (time.monotonic() - clock_start)
            if remaining > 0:
                print(f"[DEBUG] 等待 {remaining:.0f}s 至录制时长 {target_duration:.0f}s", file=sys.stderr)
                await self._sleep_while_monitoring_content_blocked(
                    page, remaining, poll_interval
                )
            await self._handle_playback_content_blocked(page)
            self.generation_complete_time_s = round(
                time.monotonic() - self._job_start_monotonic, 1
            )
            print("[DEBUG] 停止录屏 → 立刻点 X 结束生成", file=sys.stderr)
            await self._recorder_stop(page)
            await self._reset_to_input(page)
            return
        print(
            f"[DEBUG] 注入循环启动，共 {len(targets)} 个目标: "
            f"{[f'{t:.0f}s' for t, _, _, _ in targets]}（轮询 {poll_interval*1000:.0f}ms，按录屏时间到点注入）",
            file=sys.stderr,
        )

        idx = 0
        last_inject_at_target: float | None = None
        last_activity = time.monotonic()
        while idx < len(targets):
            await self._handle_playback_content_blocked(page)
            elapsed = time.monotonic() - clock_start
            target_time, prompt, prompt_id, role = targets[idx]
            # 不提前触发：prompt 时间严格相对于外部录屏开始时刻。
            # 轮询间隔最多带来约 250ms 的晚到，不再允许提前 0.8s 注入。
            if elapsed >= target_time:
                if last_inject_at_target == target_time:
                    await asyncio.sleep(poll_interval)
                    continue

                drift = elapsed - target_time
                print(
                    f"[DEBUG] 注入 t={target_time:.0f}s → 墙钟已过 {elapsed:.1f}s "
                    f"(偏差 {drift:+.1f}s)",
                    file=sys.stderr,
                )
                prompt_event = {
                    "prompt_id": prompt_id,
                    "role": role,
                    "scheduled_media_time_s": target_time,
                    "actual_media_time_s": round(elapsed, 1),
                    "actual_injection_time_s": None,
                    "status": "accepted",
                    "error": None,
                }
                await self._inject_command(page, prompt, prompt_event)
                self._injection_log.append(prompt_event)
                last_inject_at_target = target_time
                idx += 1
                last_activity = time.monotonic()
                await asyncio.sleep(0.3)
                continue

            # 超时保护：120s 无注入则跳出
            if time.monotonic() - last_activity > 120:
                print("[WARN] 注入循环超时（120s无注入），跳出", file=sys.stderr)
                break

            await asyncio.sleep(poll_interval)

        # 收尾
        last_target_time = max(t[0] for t in targets)
        if end_delay:
            extra = end_delay
        elif len(targets) >= 2:
            extra = targets[1][0] - targets[0][0]
        else:
            extra = 10
        target_duration = last_target_time + extra
        remaining = target_duration - (time.monotonic() - clock_start)
        if remaining > 0:
            print(
                f"[DEBUG] 注入完成，等待 {remaining:.0f}s 至录制时长 {target_duration:.0f}s 后停录屏",
                file=sys.stderr,
            )
            await self._sleep_while_monitoring_content_blocked(
                page, remaining, poll_interval
            )

        await self._handle_playback_content_blocked(page)
        self.generation_complete_time_s = round(
            time.monotonic() - self._job_start_monotonic, 1
        )
        print("[DEBUG] 停止录屏 → 立刻点 X 结束生成", file=sys.stderr)
        await self._recorder_stop(page)
        await self._reset_to_input(page)

    async def wait_for_ready(self, page: Page, timeout: float = 300) -> None:
        if not self._session_started:
            return
        try:
            loc = page.locator(self.SELECTORS["stream_textarea"]).first
            await loc.wait_for(state="visible", timeout=timeout * 1000)
        except PlaywrightTimeout:
            pass

    async def teardown(self, page: Page) -> None:
        """生成完成：先停录屏 → 立刻点 X 关闭当前会话（背靠背，不留空隙）。"""
        await self._recorder_stop(page)
        await self._reset_to_input(page)
        self.stop_monotonic = time.monotonic()

    async def _reset_to_input(self, page: Page) -> None:
        """生成完成后复位到输入框界面（不关浏览器、不整页重载）。

        支持两种复位场景：
        1. 正常结束 → 点右上角 X 关闭当前画面
        2. 会话超时 → 点「Try Again」关闭弹窗回到输入框
        """
        # 已在输入框界面（上一次已复位 / 本次尚未开始）→ 无需点击
        try:
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=2000
            )
            print("[DEBUG] 已在输入框界面，跳过复位点击", file=sys.stderr)
            self._session_started = False
            self.generation_start_monotonic = None
            self.recording_start_monotonic = None
            self.crop_region = None
            return
        except Exception:
            pass

        # 分支 A：会话超时弹窗 → Try Again
        if await self._check_session_ended(page, raise_if_ended=False):
            await self._dismiss_session_ended(page)
            self._session_started = False
            self.generation_start_monotonic = None
            self.recording_start_monotonic = None
            self.crop_region = None
            return

        # 分支 B：正常结束 → 右上角 X 关闭按钮
        clicked = False
        for sel in [
            self.SELECTORS["close_button"],
            "button[aria-label*='close' i]",
            "button[title*='close' i]",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(timeout=5000)
                    clicked = True
                    print(f"[DEBUG] 已点击复位按钮: {sel}", file=sys.stderr)
                    break
            except Exception:
                continue

        if not clicked:
            print("[DEBUG] 未找到右上角 X 按钮（可能已在输入框界面）", file=sys.stderr)

        # 等输入框重新出现，确认复位成功
        try:
            await page.locator(self.SELECTORS["landing_textarea"]).wait_for(
                state="visible", timeout=15000
            )
            print("[DEBUG] 已复位到输入框界面", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] 复位后未检测到输入框（需手动复位）: {e}", file=sys.stderr)

        # 清空内部状态，下一次 run 视为全新会话
        self._session_started = False
        self.generation_start_monotonic = None
        self.recording_start_monotonic = None
        self.crop_region = None
