"""Happy Oyster 流式世界模型适配器（Directing Mode）。

URL: https://www.happyoyster.cn/create/directing

流程：
1. setup: 导航 → 登录检查 → 停在 Directing Mode 准备界面
2. 首次 submit_prompt: 输入初始 prompt → 点 ↑ 提交 → 等加载到 100% → 时间从 00:00 开始
3. 后续 submit_prompt: 在生成中输入新指令 → 点 story-send-btn 发送
4. teardown: 点大圆形 Pause 按钮暂停生成

结果通过屏幕录制获得（不通过网站下载）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from playwright.async_api import Page

from .base import SiteAdapter
from ..credentials import get_browser_data_dir


class HappyOysterAdapter(SiteAdapter):
    """Happy Oyster Directing Mode (实时导演) 适配器。

    Parameters
    ----------
    config : dict, optional
        配置项:
        - max_load_wait: int — 等待加载到 100% 的最大秒数（默认 180）
        - post_inject_delay: float — 注入指令后等待秒数（默认 0.5）
    """

    name = "happy_oyster"
    user_data_dir = get_browser_data_dir("happy_oyster")

    # 流式模型：首个事件（启动会话）完成后重置计时起点
    resets_clock = True

    URL_HOME = "https://www.happyoyster.cn/"
    URL_DIRECTING = "https://www.happyoyster.cn/create/directing"
    # 供连接模式（CDP）做站点一致性校验 / 标签页 host 匹配
    URL = URL_DIRECTING

    # 关键 CSS selector（基于 DOM 探索验证，2026-07-15 修正）
    SELECTORS = {
        # 准备界面 (/create/directing)
        "initial_input": "textarea.absolute.inset-0",
        # 准备界面的 ↑ 提交按钮：圆形 type=submit（注意不是 story-send-btn，
        # 那个类只存在于生成中界面）
        "initial_send": "button[type='submit']",
        # 准备界面 ↑ 回退 selector：form 内唯一的 rounded-full 按钮
        # （部分渲染状态下 type 不一定是 submit，但 rounded-full 是稳定特征）
        "initial_send_form": "form:has(textarea.absolute.inset-0) button.rounded-full",
        # 生成中界面 (/explore/story/...)
        "stream_input": "textarea[placeholder*='接下来']",
        "stream_send": "button.story-send-btn",
        "voice_button": "button.story-voice-btn",
        # Pause 按钮：视频左上方"⏸ 暂停 | 当前播放: ..."药丸条（rounded-[20px]，半透明黑底）
        # 生成前显示"开始"，生成中显示"暂停 | 当前播放..."。不是顶部 40px 药丸（相机/音量），
        # 也不是中央 120px 大圆（那是 Play/Resume）。
        "pause_button": "button.rounded-\\[20px\\]",
        # 登录按钮（用于检测未登录状态）
        "login_button": "button:has-text('登录')",
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._session_started = False
        self._injections_done = False
        # 视频裁剪时间戳（相对于 backend._video_start_monotonic）
        self.generation_start_monotonic: float | None = None
        self.pause_monotonic: float | None = None
        self.crop_region: str | None = None
        self._max_load_wait = self.config.get("max_load_wait", 600)
        self._post_inject_delay = self.config.get("post_inject_delay", 0.5)
        self._load_wait = self.config.get("load_wait", 3)
        self._recorder_stopped = False
        self.console = None
        # 外部录屏工具（EV录屏等）
        self._ext_recorder = _build_recorder(self.config)
        print(
            f"[Recorder] 构建结果: "
            f"{'已启用 (' + self._ext_recorder._backend + ')' if self._ext_recorder else '未启用（config 无 _recorder_enabled）'}",
            file=sys.stderr,
        )

    @property
    def is_done(self) -> bool:
        """适配器是否已在内部完成全部注入。"""
        return self._injections_done

    async def setup(self, page: Page) -> None:
        """导航到 Directing Mode 准备界面，如果有 initial_prompt 则直接启动生成。

        连接模式（复用标签页）下：若已在输入框界面就跳过整页重载，直接复用，
        这样连续跑多个 yaml 不会每次都重新渲染准备页。
        """
        import sys

        # 已在输入框界面（连接模式复用标签页 / 手动开的浏览器已停在准备页）→ 跳过重载
        already_at_input = await self._is_at_input_box(page)
        if not already_at_input:
            await page.goto(self.URL_DIRECTING, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(self._load_wait)

            # 检查登录状态
            login_btn = page.locator(self.SELECTORS["login_button"])
            if await login_btn.is_visible(timeout=3000):
                raise RuntimeError(
                    "未检测到登录状态。请先在浏览器中手动登录 Happy Oyster，"
                    "登录后重新运行（persistent context 会复用登录态）。"
                )

            # 等待准备界面的输入框出现
            input_loc = page.locator(self.SELECTORS["initial_input"])
            await input_loc.wait_for(state="visible", timeout=30000)

        # 打开即全屏，提升 EV 录屏分辨率（连接模式为 no-op 守卫）
        await self._enter_fullscreen(page)

        # 如果配置了 initial_prompt，在 setup 阶段直接启动生成
        # （填初始 prompt → 点 ↑ → 等 100%，不阻塞后续调度）
        initial_prompt = self.config.get("initial_prompt")
        if initial_prompt:
            print(
                f"[DEBUG] setup 阶段启动生成: initial_prompt={initial_prompt!r}",
                file=sys.stderr,
            )
            await self._start_session(page, initial_prompt)

    async def submit_prompt(self, page: Page, prompt: str, target_time: float | None = None) -> None:
        """根据会话状态分发：首次启动会话，后续注入指令。"""
        if self._injections_done:
            return  # 注入循环已处理全部
        if not self._session_started:
            await self._start_session(page, prompt)
        else:
            await self._inject_prompt(page, prompt, target_time)

    async def _start_session(self, page: Page, prompt: str) -> None:
        """启动会话：输入初始 prompt，提交，等视频真正开始播放才录屏。

        顺序要点（关键）：先确认「视频真正开始播放」——以页面顶部
        "REC mm:ss" 计时器从 00:00 开始**递增**为唯一信号——再启动外部录屏
        并记为裁剪起点。这一步**不依赖**生成中指令输入框。

        为什么不用 Pause 按钮文字：提交后有一段加载期（加载到 100%），期间
        Pause 按钮早已显示"暂停"，但画面尚未出帧。若以它为信号，录屏会把
        这一分多钟的加载死屏也录进去（用户明确要求：视频开始才录）。

        若计时器在超时内未递增：dump 计时器/页面文本/textarea 诊断并明确报错，
        绝不"假定已开始"盲录加载死屏。

        指令输入框只在「需要注入后续指令」时才必须存在，找不到时降级为
        跳过注入（而非中断整个生成 + 录屏）。
        """
        # 1. 输入初始 prompt（先聚焦再填入，确保 React 受控组件更新状态、启用提交按钮）
        input_loc = page.locator(self.SELECTORS["initial_input"])
        await input_loc.wait_for(state="visible", timeout=30000)
        await input_loc.click()
        await input_loc.fill(prompt)
        # 等待 React 状态更新（启用提交按钮）
        await asyncio.sleep(1.0)

        # 2. 提交（点准备界面的圆形 ↑ 按钮）
        await self._submit_initial(page)

        # 3. 等「视频真正开始播放」：以页面计时器开始递增为唯一可靠信号。
        #    提交后 Happy Oyster 有一段加载期（加载到 100%），期间 Pause 按钮可能
        #    已显示"暂停"、但画面尚未出帧——此时录屏会录到一分多钟的加载死屏。
        #    真正的播放信号是顶部 "REC mm:ss" 计时器从 00:00 开始跳动，因此必须
        #    等到计时器「递增」才启动录屏（用户明确要求：视频开始才录，不录加载屏）。
        playback_timeout = self._max_load_wait  # 秒（加载慢，默认 600，可配置）
        playback_ok = await self._wait_for_playback(page, playback_timeout)
        if not playback_ok:
            # 没等到计时器递增：绝不"假定已开始"盲录加载死屏，dump 诊断让用户精确修信号。
            timer = await self._get_page_timer(page)
            pct = await self._get_loading_pct(page)
            sample = await page.evaluate("() => document.body.innerText.slice(0, 400)")
            print(
                f"[ERROR] 未检测到视频开始播放（{playback_timeout:.0f}s 内计时器未递增，"
                f"当前计时器={timer!r}，加载进度={pct!r}）。未启动录屏，避免录到加载死屏。",
                file=sys.stderr,
            )
            print(f"[DEBUG-TIMER] page_text_sample={sample!r}", file=sys.stderr)
            await self._dump_textareas(page)
            raise RuntimeError(
                "未检测到 Happy Oyster 视频开始播放（计时器未在 "
                f"{playback_timeout:.0f}s 内递增）。请检查页面计时器格式或 _get_page_timer "
                "选择器；详细诊断见上方 [DEBUG-TIMER] / [DEBUG-TA]。"
            )

        # 视频已开始播放（计时器递增）→ 此刻才启动外部录屏。
        # 这帧同时是视频裁剪起点（不录加载死屏）。
        if self._ext_recorder:
            ok = self._ext_recorder.start()
            print(f"[Recorder] 开始录制热键结果: {'成功' if ok else '失败（见上方 traceback）'}", file=sys.stderr)
        else:
            print("[Recorder] 未配置外部录屏，跳过开始", file=sys.stderr)

        # 测出真正的视频内容包围盒，作为裁剪区（去掉顶部/侧边 UI）
        await self._detect_content_region(page)

        # 记录视频裁剪起点（即计时器开始递增的同一帧）
        self.generation_start_monotonic = time.monotonic()

        # 关闭配乐（点生成界面 🎵），保留视频原声（不做浏览器层静音）
        await self._toggle_bgm_off(page)

        self._session_started = True

        # 4. 定位生成中指令输入框（仅用于注入后续指令）。宽松选择器 + 失败不致命。
        stream_sel = "textarea.story-textarea, textarea[placeholder*='接下来']"
        try:
            await page.locator(stream_sel).first.wait_for(
                state="visible", timeout=60000
            )
        except Exception:
            print(
                "[WARN] 未找到生成中指令输入框（可能网站改版 / 选择器变化），"
                "后续注入指令将跳过；生成与录屏不受影响。",
                file=sys.stderr,
            )
            await self._dump_textareas(page)

        # 5. 启动注入循环：按页面计时器在精准时刻注入后续 prompt
        events = self.config.get("_inject_events", [])
        print(
            f"[DEBUG] config has _inject_events={'YES' if events else 'NO'} "
            f"({len(events)} events), config keys={sorted(self.config.keys())!r}",
            file=sys.stderr,
        )
        if events:
            end_delay = self.config.get("_end_delay", 0.0)
            try:
                await self._run_injection_loop(page, events, end_delay)
            except Exception as e:
                print(
                    f"[WARN] 注入循环异常（已跳过注入，生成与录屏保留）: {e}",
                    file=sys.stderr,
                )
            # 注入循环末尾已点 Pause，标记全部完成
            self._injections_done = True

    async def _submit_initial(self, page: Page) -> None:
        """提交初始 prompt：通过容器范围 + rounded-full 定位 ↑ 按钮。

        ↑ 按钮的稳定特征是：在 textarea 所在的 w-[680px] 输入卡片容器内，
        且是其中**唯一带 rounded-full 类的按钮**（其他工具栏按钮用 rounded-[8px]）。
        不依赖 type='submit'（该属性在不同渲染中可能变化）。
        """
        import sys

        # 路径 1：Playwright CSS-escaped 容器选择器
        try:
            btn = page.locator("div.w-\\[680px\\] button.rounded-full").first
            await btn.wait_for(state="visible", timeout=12000)
            await btn.click(timeout=5000)
            print("[DEBUG] 初始提交 via Playwright (container + rounded-full)", file=sys.stderr)
            return
        except Exception as e:
            print(f"[DEBUG] 初始提交 Playwright 路径失败，回退 JS: {e}", file=sys.stderr)

        # 路径 2：JS 从 textarea 向上找 680px 容器 → 取 rounded-full 按钮 → click
        clicked = await page.evaluate(
            """() => {
                const ta = document.querySelector("textarea.absolute.inset-0");
                if (!ta) return false;
                // 向上找到 w-[680px] 容器（输入卡片）
                let el = ta;
                while (el) {
                    if (el.classList.contains('w-[680px]')) break;
                    el = el.parentElement;
                }
                if (!el) return false;
                // 容器内唯一的 rounded-full 按钮就是 ↑
                const btn = el.querySelector('button.rounded-full');
                if (!btn || btn.disabled) return false;
                btn.click();
                return true;
            }"""
        )
        if not clicked:
            raise RuntimeError(
                "初始提交失败：在 w-[680px] 容器内找不到可点击的 rounded-full 按钮"
            )
        print("[DEBUG] 初始提交 via JS click (container + rounded-full)", file=sys.stderr)

    async def _get_page_timer(self, page: Page) -> float | None:
        """读取页面上的生成计时器（单位：秒），返回 None 如果读不到。

        Happy Oyster 生成中页面顶部显示 "REC mm:ss / total" 格式的计时。
        """
        return await page.evaluate(
            """() => {
                const text = document.body.innerText;
                const m = text.match(/REC\\s*(\\d+):(\\d+)/);
                if (m) return parseInt(m[1]) * 60 + parseInt(m[2]);
                // 回退：找任意 mm:ss 格式（排除可能的总时长）
                const all = text.match(/(\\d+):(\\d{2})/g);
                if (all) {
                    for (const t of all) {
                        const parts = t.split(':');
                        const secs = parseInt(parts[0]) * 60 + parseInt(parts[1]);
                        if (secs > 0) return secs;
                    }
                }
                return null;
            }"""
        )

    async def _wait_for_playback(self, page: Page, timeout: float) -> bool:
        """等待视频真正开始播放：以页面计时器递增为信号。

        返回 True 表示检测到计时器递增（视频在播）；超时返回 False。

        - 加载期页面停留在 /explore 并显示进度百分比（如「89%」），此时无计时器。
        - **关键：等待窗口只用 timeout（=max_load_wait）这一个边界。** 加载百分比
          可能长时间卡在某值（如 89%）但最终会动——用户已多次确认「加载慢、最终能
          生成、只是花时间长」。因此百分比卡住**绝不**作为失败信号，也**不**刷新
          deadline。我们只老实等满 timeout，期间百分比变化仅打印日志。
        - 递增检测还能过滤加载期静态显示的「总时长 mm:ss」，避免误判为播放。
        """
        last: float | None = None
        last_pct: int | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                t = await self._get_page_timer(page)
            except Exception:
                t = None
            if t is not None:
                if last is None:
                    last = t
                    print(f"[DEBUG] 计时器出现: {t}s（等待递增以确认播放）", file=sys.stderr)
                elif t > last:
                    print(
                        f"[DEBUG] 计时器递增 {last}s → {t}s，确认视频开始播放",
                        file=sys.stderr,
                    )
                    return True
                else:
                    last = t
            else:
                # 还在加载：读百分比仅用于日志，绝不以「卡住」判失败
                try:
                    pct = await self._get_loading_pct(page)
                except Exception:
                    pct = None
                if pct is not None and pct != last_pct:
                    print(f"[DEBUG] 加载中... {pct}%（继续等待生成开始）", file=sys.stderr)
                    last_pct = pct
            await asyncio.sleep(0.5)
        return False

    async def _get_loading_pct(self, page: Page) -> int | None:
        """读取页面加载百分比（如「89%」），返回 int 或 None。

        Happy Oyster 加载期停在 /explore，页面显示百分比进度（可能夹换行：
        "89\\n%"），用 \\s* 兼容。无加载百分比（已到生成页）返回 None。
        """
        return await page.evaluate(
            """() => {
                const m = document.body.innerText.match(/(\\d{1,3})\\s*%/);
                return m ? parseInt(m[1], 10) : null;
            }"""
        )

    async def _do_inject(self, page: Page, prompt: str) -> None:
        """注入一条指令：填框 + 点发送（不做计时器等待，由调用方控制时机）。"""
        stream_input = page.locator(
            "textarea.story-textarea, textarea[placeholder*='接下来']"
        )
        await stream_input.wait_for(state="visible", timeout=10000)
        await stream_input.fill("")
        await stream_input.fill(prompt)
        await asyncio.sleep(0.3)

        await self._click_send(
            page,
            [self.SELECTORS["stream_send"], self.SELECTORS["initial_send"]],
            label="指令注入",
        )
        await asyncio.sleep(self._post_inject_delay)

    async def _run_injection_loop(
        self, page: Page, events: list[dict], end_delay: float
    ) -> None:
        """按页面计时器在精准时刻注入后续 prompt。

        每 250ms 轮询页面计时器，当到达下一个目标时刻的 ±1s 内时注入。
        所有注入完成后等待通知 + t 秒 + Pause + 停留验证。
        """
        tolerance = 0.8
        poll_interval = 0.25

        targets = sorted(
            [(float(e["time"]), str(e["prompt"])) for e in events],
            key=lambda x: x[0],
        )
        if not targets:
            return
        print(
            f"[DEBUG] 注入循环启动，共 {len(targets)} 个目标: "
            f"{[f'{t:.0f}s' for t, _ in targets]}（轮询间隔 {poll_interval*1000:.0f}ms，容差 ±{tolerance:.0f}s）",
            file=sys.stderr,
        )

        idx = 0
        last_inject_at_target: float | None = None
        while idx < len(targets):
            current = await self._get_page_timer(page)
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
                    f"[DEBUG] 注入 t={target_time:.0f}s  "
                    f"(页面计时器 {current:.1f}s，偏差 {drift:+.1f}s)",
                    file=sys.stderr,
                )
                await self._do_inject(page, prompt)
                last_inject_at_target = target_time
                idx += 1
                await asyncio.sleep(0.3)
                continue

            await asyncio.sleep(poll_interval)

        # 收尾：等通知 → 等 t 秒 → Pause → 停留验证
        interval = targets[1][0] - targets[0][0] if len(targets) >= 2 else (end_delay or 10.0)

        last_prompt = targets[-1][1]
        await self._wait_for_notification(page, last_prompt)

        print(f"[DEBUG] 等待 {interval:.0f}s 后 Pause", file=sys.stderr)
        await asyncio.sleep(interval)

        print("[DEBUG] 点 Pause 结束生成", file=sys.stderr)
        try:
            pause_btn = page.locator(self.SELECTORS["pause_button"])
            await pause_btn.first.click(timeout=5000)
            self.pause_monotonic = time.monotonic()
        except Exception:
            pass
        # 停止外部录屏（EV录屏等），幂等
        await self._recorder_stop(page)
        await asyncio.sleep(1)

        print(f"[DEBUG] Pause 完成，停留 {interval:.0f}s 供验证", file=sys.stderr)
        await asyncio.sleep(interval)

    async def _wait_for_notification(
        self, page: Page, prompt: str,
        appear_timeout: float = 10.0, fade_timeout: float = 15.0,
    ) -> None:
        """等待最后一条 prompt 的通知弹窗出现并消失。

        在 DOM 中搜索 prompt 文本（排除 textarea 内的），用于检测 Happy Oyster
        视频顶部的指令通知弹窗：先出现 → 后消失，表示指令已被模型接收。
        """
        import sys
        import time as _time

        # —— 阶段 1：等待通知出现 ——
        print(f"[DEBUG] 等待通知弹窗出现: {prompt!r}", file=sys.stderr)
        deadline = _time.monotonic() + appear_timeout
        appeared = False
        while _time.monotonic() < deadline:
            found = await page.evaluate(
                """(prompt) => {
                    const ta = document.querySelector(
                        "textarea.story-textarea, textarea.absolute.inset-0"
                    );
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        if (!node.textContent.includes(prompt)) continue;
                        // 排除 textarea 内的文本
                        let el = node.parentElement;
                        while (el) {
                            if (el === ta) break;
                            el = el.parentElement;
                        }
                        if (el === ta) continue;
                        // 确认节点可见
                        el = node.parentElement;
                        if (el && el.offsetParent !== null) return true;
                    }
                    return false;
                }""",
                prompt,
            )
            if found:
                appeared = True
                break
            await asyncio.sleep(0.3)

        if not appeared:
            print("[DEBUG] 通知弹窗未检测到出现（可能已消失或页面无通知），继续", file=sys.stderr)
            return
        print("[DEBUG] 通知弹窗已出现", file=sys.stderr)

        # —— 阶段 2：等待通知消失 ——
        deadline = _time.monotonic() + fade_timeout
        while _time.monotonic() < deadline:
            found = await page.evaluate(
                """(prompt) => {
                    const ta = document.querySelector(
                        "textarea.story-textarea, textarea.absolute.inset-0"
                    );
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        if (!node.textContent.includes(prompt)) continue;
                        let el = node.parentElement;
                        while (el) {
                            if (el === ta) break;
                            el = el.parentElement;
                        }
                        if (el === ta) continue;
                        el = node.parentElement;
                        if (el && el.offsetParent !== null) return true;
                    }
                    return false;
                }""",
                prompt,
            )
            if not found:
                print("[DEBUG] 通知弹窗已消失", file=sys.stderr)
                return
            await asyncio.sleep(0.3)

        print("[DEBUG] 通知弹窗消失等待超时，继续", file=sys.stderr)

    async def _inject_prompt(self, page: Page, prompt: str, target_time: float | None = None) -> None:
        """（保留兼容）在生成中注入新指令。新流程中不会被调用。"""
        await self._do_inject(page, prompt)

    async def _click_send(self, page: Page, selectors, label: str = "发送") -> None:
        """点发送按钮（↑），支持多个候选 selector 容错。

        Parameters
        ----------
        selectors : str | list[str]
            候选按钮 CSS selector（按顺序尝试，命中第一个可见可点的）。
            初始提交建议 ["button[type='submit']", "button.story-send-btn"]；
            注入指令建议 ["button.story-send-btn", "button[type='submit']"]。
        label : str
            日志标签，便于排查。
        """
        import sys
        import time

        if isinstance(selectors, str):
            selectors = [selectors]
        print(
            f"[DEBUG] _click_send label={label!r} candidates={selectors!r}",
            file=sys.stderr,
        )
        last_exc = None
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=6000)
                await btn.click(timeout=5000, force=False)
                print(f"[DEBUG] 已点击「{label}」按钮 via {sel!r}", file=sys.stderr)
                return
            except Exception as exc:
                print(f"[DEBUG] 候选 {sel!r} 失败: {exc}", file=sys.stderr)
                last_exc = exc
                continue

        # 所有候选都失败：dump 诊断信息便于定位
        await self._dump_click_diagnostics(page, label, selectors, time.time())
        # 用 repr 且转义方括号，避免 Rich 标记把 [type='submit'] 吃掉
        msg = (
            f"点击「{label}」按钮失败，已尝试候选 {selectors!r}：{last_exc}"
        ).replace("[", "[[")
        raise RuntimeError(msg)

    async def _dump_click_diagnostics(self, page: Page, label: str, selectors, ts: float) -> None:
        """点击失败时 dump 截图 + 所有候选按钮状态，便于定位。"""
        import os
        import sys

        try:
            os.makedirs(".exploration", exist_ok=True)
            shot = f".exploration/click_fail_{label}_{int(ts)}.png"
            await page.screenshot(path=shot)
        except Exception as e:
            shot = f"(截图失败: {e})"

        try:
            url = page.url
            infos = await page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll("button").forEach((b, i) => {
                        const r = b.getBoundingClientRect();
                        if (!(r.width > 0 && r.height > 0)) return; // 只看可见的
                        out.push({
                            i: i,
                            type: b.type || "(none)",
                            cls: (b.className || '').slice(0, 80),
                            text: (b.innerText || '').slice(0, 20).replace(/\\n/g, ' '),
                            disabled: b.disabled,
                            x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.width), h: Math.round(r.height),
                        });
                    });
                    const ta = document.querySelector("textarea.absolute.inset-0, textarea.story-textarea");
                    // 同样 dump form 信息
                    const forms = document.querySelectorAll("form").length;
                    return {
                        url: location.href,
                        buttons: out,
                        form_count: forms,
                        textarea_value: ta ? ta.value : "(no textarea)",
                        textarea_value_len: ta ? ta.value.length : 0,
                    };
                }"""
            )
            print(f"[DEBUG-DIAG] label={label!r} url={infos['url']}", file=sys.stderr)
            print(f"[DEBUG-DIAG] textarea_value={infos['textarea_value']!r}", file=sys.stderr)
            print(f"[DEBUG-DIAG] buttons({len(infos['buttons'])})={infos['buttons']}", file=sys.stderr)
            print(f"[DEBUG-DIAG] form_count={infos.get('form_count', '?')}", file=sys.stderr)
            print(f"[DEBUG-DIAG] screenshot={shot}", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG-DIAG] 诊断 evaluate 失败: {e}", file=sys.stderr)

    async def _dump_textareas(self, page: Page) -> None:
        """生成中界面诊断：dump 当前页面所有 textarea 的 placeholder/class/尺寸。

        用于网站改版后定位「指令输入框」真实 CSS 选择器（旧的
        textarea[placeholder*='接下来'] 可能因 placeholder 文案变化而失效）。
        """
        try:
            info = await page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('textarea').forEach((t, i) => {
                        const r = t.getBoundingClientRect();
                        out.push({
                            i: i,
                            ph: (t.placeholder || '').slice(0, 60),
                            cls: (t.className || '').slice(0, 80),
                            w: Math.round(r.width), h: Math.round(r.height),
                            visible: r.width > 0 && r.height > 0,
                        });
                    });
                    return { url: location.href, textareas: out };
                }"""
            )
            print(f"[DEBUG-TA] url={info.get('url')}", file=sys.stderr)
            print(
                f"[DEBUG-TA] textareas({len(info.get('textareas', []))})={info.get('textareas')}",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"[DEBUG-TA] dump failed: {e}", file=sys.stderr)

    async def _is_at_input_box(self, page: Page) -> bool:
        """判断当前标签页是否已在 Directing 准备界面（输入框可见）。"""
        try:
            url = page.url or ""
            if self.URL_DIRECTING not in url:
                return False
            await page.locator(self.SELECTORS["initial_input"]).wait_for(
                state="visible", timeout=2000
            )
            return True
        except Exception:
            return False

    async def _recorder_stop(self, page: Page) -> None:
        """幂等停止外部录屏（EV录屏等），避免注入循环与 teardown 重复发停止键。"""
        if self._recorder_stopped:
            return
        if self._ext_recorder:
            ok = self._ext_recorder.stop()
            print(
                f"[Recorder] 停止录制热键结果: {'成功' if ok else '失败（见上方 traceback）'}",
                file=sys.stderr,
            )
        else:
            print("[Recorder] 未配置外部录屏，跳过停止", file=sys.stderr)
        self._recorder_stopped = True

    async def _reset_to_input(self, page: Page) -> None:
        """生成完成后复位到输入框界面（不关浏览器、不整页重载用户态）。

        Happy Oyster 生成发生在 /explore/story/... 路由，复位即回到
        /create/directing 准备界面。连接模式下复用同一标签页（goto 仍在同一标签），
        浏览器保持打开；非连接模式每次 run 各起各关，无影响。

        若当前已在输入框界面（上一次已复位 / 本次尚未开始），则跳过重载。
        """
        if await self._is_at_input_box(page):
            print("[DEBUG] 已在输入框界面，跳过复位导航", file=sys.stderr)
        else:
            try:
                await page.goto(self.URL_DIRECTING, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(self._load_wait)
                await page.locator(self.SELECTORS["initial_input"]).wait_for(
                    state="visible", timeout=30000
                )
                print("[DEBUG] 已复位到输入框界面", file=sys.stderr)
            except Exception as e:
                print(f"[DEBUG] 复位导航失败（需手动复位）: {e}", file=sys.stderr)

        # 清空内部状态，下一次 run 视为全新会话
        self._session_started = False
        self._injections_done = False
        self.generation_start_monotonic = None
        self.pause_monotonic = None
        self.crop_region = None
        self._recorder_stopped = False

    async def teardown(self, page: Page) -> None:
        """生成完成后复位到输入框界面（不关浏览器、不整页重载用户态）。

        连接模式下浏览器由用户手动打开，不能关；只需回到 /create/directing
        输入框界面，下一个 run 可直接在同一标签页上重开会话。
        """
        # 1) 停止外部录屏（若注入循环已停则幂等跳过）
        await self._recorder_stop(page)
        # 2) 复位到输入框界面（goto /create/directing），清空内部状态
        await self._reset_to_input(page)


def _build_recorder(config: dict[str, Any]) -> object | None:
    """根据 config 构建 ExternalRecorder，未配置则返回 None。"""
    if not config.get("_recorder_enabled"):
        return None
    try:
        from ..recorder import ExternalRecorder
        return ExternalRecorder(
            start_hotkey=config.get("_recorder_start_hotkey", "ctrl+f1"),
            stop_hotkey=config.get("_recorder_stop_hotkey", "ctrl+f2"),
        )
    except Exception:
        return None
