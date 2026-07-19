"""站点适配器抽象基类。

每个视频生成网站的 DOM 结构不同，适配器封装了
"定位输入框 → 输入 prompt → 触发提交" 的具体操作。
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from abc import ABC, abstractmethod

from playwright.async_api import Page


class SiteAdapter(ABC):
    """站点适配器接口。

    子类需要实现以下方法：

    - :meth:`setup` — 页面初始化（导航、登录等）
    - :meth:`submit_prompt` — 输入 prompt 并触发生成

    可选覆盖：

    - :meth:`wait_for_ready` — 等待输入框可用，默认立即返回
    """

    name: str = "base"

    # 流式模型设为 True：首个事件（启动会话）完成后重置计时起点，
    # 后续事件的 time 相对于"生成开始（00:00）"而非"脚本启动"。
    resets_clock: bool = False

    # 适配器内部已自行处理全部注入（如通过页面计时器轮询），
    # 设为 True 通知调度器跳过剩余事件，直接收尾。
    is_done: bool = False

    @abstractmethod
    async def setup(self, page: Page) -> None:
        """页面初始化：导航到目标 URL、处理登录弹窗等。

        在调度器启动时调用一次。
        """
        ...

    @abstractmethod
    async def submit_prompt(self, page: Page, prompt: str, target_time: float | None = None) -> None:
        """输入 prompt 并触发生成。

        实现应包含：
        1. 等待输入框可用
        2. 清空输入框（如需要）
        3. 输入 prompt 文本
        4. 触发提交（回车 / 点击按钮）

        Parameters
        ----------
        target_time : float, optional
            注入的目标时间（秒，相对于生成开始 00:00）。
            流式模型可用页面计时器对齐精准注入时机。
        """
        ...

    async def wait_for_ready(self, page: Page, timeout: float = 300) -> None:
        """等待网站准备好接受下一个 prompt。

        默认实现立即返回。子类可以覆盖此方法，
        通过检测页面元素（如输入框是否 enabled）来判断就绪状态。
        """
        return None

    async def teardown(self, page: Page) -> None:
        """清理资源，在调度结束时调用。默认空实现。"""
        return None

    async def _recorder_start(self, page: Page) -> None:
        """如有外部录屏配置，等视频真正开始渲染 → 再发开始热键。

        全屏（F11）已在 setup 阶段由 ``_enter_fullscreen`` 完成，这里不再按 F11，
        否则会与 setup 的全屏互相切换、退出全屏。EV 已设为"收到信号即录"，故必须
        等视频元素真正可见且在播后才发键，避免录到雪花屏。
        """
        cfg = self.config or {}
        if not cfg.get("_recorder_enabled"):
            print(
                "[Recorder] 未配置外部录屏，跳过开始 —— 在 timeline YAML 顶层加 "
                "`recorder: {enabled: true}` 即可启用（hotkey 默认 ctrl+f1/ctrl+f2）",
                file=sys.stderr,
            )
            return
        hk = cfg.get("_recorder_start_hotkey")
        if not hk:
            print("[Recorder] 未设置 _recorder_start_hotkey，跳过开始", file=sys.stderr)
            return

        # 等视频真正开始渲染（最多 6s），避免录到雪花屏
        try:
            await self._wait_video_ready(page, timeout=6)
        except Exception:
            pass

        # 发开始热键
        try:
            from ..recorder import ExternalRecorder

            ok = ExternalRecorder(start_hotkey=hk).start()
            print(
                f"[Recorder] 开始录制热键结果: {'成功' if ok else '失败（见上方 traceback）'}",
                file=sys.stderr,
            )
        except Exception:
            print(f"[Recorder] 开始录制异常:\n{traceback.format_exc()}", file=sys.stderr)

    async def _wait_video_ready(self, page: Page, timeout: float = 6) -> None:
        """轮询页面，直到出现可见且在播放的 <video> 或 <canvas> 元素。"""
        import time as _t

        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            ready = await page.evaluate(
                """() => {
                    const els = document.querySelectorAll('video, canvas');
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 50 || r.height < 50) continue;
                        if (el.tagName === 'VIDEO') {
                            if (el.readyState >= 2 && el.videoWidth > 0) return true;
                        } else {
                            // canvas 存在且可见即认为在渲染
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if ready:
                print("[DEBUG] 视频元素已开始渲染，可以开始录制", file=sys.stderr)
                return
            await asyncio.sleep(0.3)
        print("[DEBUG] 超时未检测到视频元素，仍继续录制（前几秒可能为加载画面）", file=sys.stderr)

    async def _enter_fullscreen(self, page: Page) -> None:
        """让浏览器窗口进入全屏，提高 EV 录屏分辨率。

        连接模式（用户手动打开 Chrome for Testing）下，全屏由用户自己控制，
        这里直接跳过，不碰用户的窗口。

        方案：**真实鼠标点击拿到「用户激活」权限 + 直接调用浏览器 Fullscreen API**
        （``document.documentElement.requestFullscreen()``，等效 F11，但由 JS 触发，
        **不依赖浏览器窗口是否有操作系统焦点**——这正是之前纯 F11 静默失败的根因）。

        为什么用 Fullscreen API 而不是点页面里的「全屏」按钮：
        - 对 ``documentElement`` 全屏会**保留**页面上的输入框 / Pause 等控件，
          后续按时间轴注入 prompt、点暂停仍然可用；
        - 点页面里的「全屏」按钮往往只把视频元素放大、隐藏控制栏，会破坏自动化。

        setup 阶段生成尚未开始，点画面中央不会暂停视频，且能可靠拿到用户激活。
        """
        # 连接模式（用户手动打开 Chrome for Testing）：全屏由用户自己控制，跳过
        if getattr(self, "config", {}).get("_connect_mode"):
            print("[DEBUG] 连接模式：全屏/窗口由用户手动控制，跳过自动全屏", file=sys.stderr)
            return
        try:
            await page.bring_to_front()
            # 若页面已填满整个屏幕（kiosk 启动或已全屏），无需再操作
            already = await page.evaluate(
                "() => window.innerWidth >= screen.width - 2 "
                "&& window.innerHeight >= screen.height - 2"
            )
            if already:
                print("[DEBUG] 页面已全屏（kiosk/全屏），跳过", file=sys.stderr)
                return
            # 1) 真实鼠标点击（CDP 受信任事件）→ 拿到 sticky user activation
            vp = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            await page.mouse.click(int(vp["w"] / 2), int(vp["h"] / 2))
            await asyncio.sleep(0.15)
            # 2) 用 Fullscreen API 全屏整个文档（不需要 OS 焦点）
            ok = await page.evaluate(
                "() => { try { if (!document.fullscreenElement) "
                "document.documentElement.requestFullscreen(); return true; } "
                "catch (e) { return false; } }"
            )
            if ok:
                print("[DEBUG] 已点击 + requestFullscreen，页面进入全屏", file=sys.stderr)
            else:
                # 极端回退：再试一次 F11
                await page.keyboard.press("F11")
                print("[DEBUG] requestFullscreen 失败，回退 F11", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] 全屏失败: {e}", file=sys.stderr)

    async def _exit_fullscreen(self, page: Page) -> None:
        """退出全屏（与 _enter_fullscreen 配对）。优先用 Fullscreen API，回退 F11。"""
        # 连接模式：不碰用户的窗口（全屏是用户手动开的，工具退出会破坏它）
        if getattr(self, "config", {}).get("_connect_mode"):
            print("[DEBUG] 连接模式：不退出用户全屏", file=sys.stderr)
            return
        try:
            still = await page.evaluate(
                "() => { if (document.fullscreenElement) { document.exitFullscreen(); "
                "return false; } return true; }"
            )
            # still=True 表示不是 JS 全屏（可能之前是 F11 全屏），补一次 F11 退出
            if still:
                await page.keyboard.press("F11")
        except Exception:
            pass

    async def _detect_content_region(self, page: Page) -> None:
        """检测页面上最大的可见 video/canvas 元素包围盒，作为裁剪区 W:H:X:Y。

        各站视频/画布元素不同（Happy Oyster 多半是 <video>，Odyssey 是 <canvas>，
        PixVerse 是 <video>），统一在此取面积最大的可见媒体元素，得到它在视口中的
        实际像素包围盒。相比 FFmpeg cropdetect 猜黑边，这种方式精准、且全屏无黑边时
        也照样能裁出内容区。结果存入 ``self.crop_region``，预留给独立的裁剪脚本
        （scripts/trim_*.py）使用。
        """
        try:
            box = await page.evaluate(
                """() => {
                    const els = Array.from(
                        document.querySelectorAll('video, canvas')
                    ).filter((el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 50 && r.height > 50;
                    });
                    if (!els.length) return null;
                    els.sort((a, b) => {
                        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                        return (rb.width * rb.height) - (ra.width * ra.height);
                    });
                    const r = els[0].getBoundingClientRect();
                    return {
                        w: Math.round(r.width), h: Math.round(r.height),
                        x: Math.round(r.x), y: Math.round(r.y),
                    };
                }"""
            )
            if box:
                self.crop_region = f"{box['w']}:{box['h']}:{box['x']}:{box['y']}"
                print(
                    f"[DEBUG] 裁剪区域(内容包围盒): {self.crop_region}", file=sys.stderr
                )
            else:
                print("[DEBUG] 未检测到 video/canvas，跳过内容裁剪", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] 内容区域检测失败: {e}", file=sys.stderr)

    async def _mute_all_media(self, page: Page) -> None:
        """静音页面所有媒体元素，作为浏览器级 --mute-audio 之外的双保险。

        做法：直接在 JS 层把所有 ``<video>``/``<audio>`` 设为 ``muted=true`` 且
        ``volume=0``。这与全屏状态、坐标、页面布局**完全无关**，比"按百分比坐标去猜
        按钮位置点击"可靠得多（后者在 kiosk 全屏 / 分辨率变化时必崩）。

        注意：浏览器启动参数 ``--mute-audio``（见 BrowserBackend）才是真正兜底，因为
        它连 WebAudio（无 <video> 元素）那种音频也能静掉；这里的 JS 静音只是额外一层。
        """
        try:
            count = await page.evaluate(
                """() => {
                    let n = 0;
                    document.querySelectorAll('video, audio').forEach((m) => {
                        m.muted = true;
                        try { m.volume = 0; } catch (e) {}
                        n++;
                    });
                    // WebAudio 兜底：让之后新建的 GainNode 默认增益为 0。
                    // （已存在的 AudioContext 靠浏览器 --mute-audio 兜底；这里只防新建。）
                    try {
                        const AC = window.AudioContext || window.webkitAudioContext;
                        if (AC && AC.prototype && !AC.prototype.__pc_muted) {
                            const orig = AC.prototype.createGain;
                            AC.prototype.createGain = function () {
                                const g = orig.call(this);
                                try { g.gain.value = 0; } catch (e) {}
                                return g;
                            };
                            AC.prototype.__pc_muted = true;
                        }
                    } catch (e) {}
                    return n;
                }"""
            )
            print(f"[DEBUG] 已静音 {count} 个媒体元素 (JS 层，含 WebAudio 拦截)", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG] JS 静音失败: {e}", file=sys.stderr)

    async def _toggle_bgm_off(self, page: Page) -> None:
        """关闭生成界面的背景音乐（配乐）：hover 视频 → 点击 🎵。

        核心原则：**hover 后不做任何会移走鼠标的中间操作**。
        控件栏是 hover-reveal 的，鼠标一离开视频区域就消失。
        因此用一次 JS evaluate 原子完成"找按钮 + 点击"，不拆步骤。

        默认行为（config ``bgm_off`` 未设或为 True）即关闭配乐；设 ``bgm_off: false`` 可跳过。
        """
        cfg = self.config or {}
        if cfg.get("bgm_off", True) is False:
            print("[DEBUG] 跳过关闭配乐（config bgm_off=false）", file=sys.stderr)
            return

        # ── 1. hover 视频中心，等控件栏浮出 ──
        video_box = None
        try:
            video = page.locator("video").first
            video_box = await video.bounding_box()
            if video_box:
                cx = video_box["x"] + video_box["width"] / 2
                cy = video_box["y"] + video_box["height"] / 2
                await page.mouse.move(cx, cy)
                await asyncio.sleep(2)
                print(f"[DEBUG-BGM] 已 hover 视频中心 ({cx:.0f},{cy:.0f})，等 2s", file=sys.stderr)
            else:
                print("[DEBUG-BGM] video bounding_box 为 None", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG-BGM] hover 视频失败: {e}", file=sys.stderr)

        if not video_box:
            print("[WARN] 无 video 边界，跳过关闭配乐", file=sys.stderr)
            return

        # ── 2. 原子操作：JS 在视频区域内找音乐按钮并点击 ──
        # 不做中间鼠标移动（会令 overlay 消失）。JS 里直接 .click() 目标按钮。
        vb = video_box
        max_retries = int(cfg.get("bgm_retries", 5))
        for attempt in range(max_retries):
            result = await page.evaluate(
                """(vb) => {
                    // 在视频包围盒内、底部 25% 区域找所有可见的按钮/icon 按钮
                    const v = document.querySelector('video');
                    if (!v) return {ok: false, reason: 'no video'};
                    const rects = [v.getBoundingClientRect()];
                    // 也查 video 的父容器（overlay 可能挂在父 div 上）
                    const parent = v.parentElement;
                    if (parent) rects.push(parent.getBoundingClientRect());

                    const buttons = Array.from(document.querySelectorAll(
                        'button, [role="button"], [aria-label], [title]'
                    ));
                    const hits = [];
                    for (const b of buttons) {
                        const r = b.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5) continue;
                        // 按钮中心是否在视频区域内
                        const bx = r.x + r.width / 2;
                        const by = r.y + r.height / 2;
                        const inside = rects.some(vr =>
                            bx >= vr.x && bx <= vr.x + vr.width &&
                            by >= vr.y && by <= vr.y + vr.height
                        );
                        if (!inside) continue;
                        // 只看底部 30% 区域（控件栏位置）
                        const bottomZone = vb.y + vb.height * 0.70;
                        if (by < bottomZone) continue;

                        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                        const title = (b.getAttribute('title') || '').toLowerCase();
                        const txt = (b.innerText || '').toLowerCase().trim();
                        const hasSvg = !!b.querySelector('svg');
                        const pressed = b.getAttribute('aria-pressed');
                        hits.push({
                            tag: b.tagName, cls: String(b.className||'').slice(0,80),
                            aria, title, txt: txt.slice(0,30), hasSvg, pressed,
                            x: Math.round(bx), y: Math.round(by),
                            w: Math.round(r.width), h: Math.round(r.height),
                            el: b,  // 保留引用供点击
                        });
                    }

                    if (hits.length === 0)
                        return {ok: false, reason: 'no_buttons_in_bottom_zone', hitCount: 0};

                    // 优先匹配 music/sound/audio/mute 关键词
                    const kw = ['music','sound','audio','mute','volume','bgm','配乐','音乐'];
                    let target = hits.find(h =>
                        kw.some(k => h.aria.includes(k) || h.title.includes(k) || h.txt.includes(k))
                    );
                    // 没有关键词匹配 → 取最右下角的带 svg 的按钮（音乐符号通常在最右）
                    if (!target) {
                        const svgHits = hits.filter(h => h.hasSvg);
                        if (svgHits.length > 0) {
                            target = svgHits.reduce((a, b) =>
                                (b.x + b.y) > (a.x + a.y) ? b : a
                            );
                        }
                    }
                    // 还没有 → 取最右下角的任意按钮
                    if (!target) {
                        target = hits.reduce((a, b) =>
                            (b.x + b.y) > (a.x + a.y) ? b : a
                        );
                    }

                    // 检查状态：如果 aria-pressed 已是 false（关闭态），跳过
                    if (target.pressed === 'false') {
                        return {ok: true, reason: 'already_off', target: target};
                    }

                    // 点击！
                    target.el.click();
                    return {ok: true, reason: 'clicked', target: target, hitCount: hits.length};
                }""",
                {"x": vb["x"], "y": vb["y"], "width": vb["width"], "height": vb["height"]},
            )

            # 清理 result 里无法序列化的 el 引用（JSON 序列化会失败）
            if isinstance(result, dict) and "target" in result:
                t = result["target"]
                if isinstance(t, dict):
                    t.pop("el", None)

            print(f"[DEBUG-BGM] 尝试 {attempt+1}/{max_retries}: {result}", file=sys.stderr)

            if isinstance(result, dict) and result.get("ok"):
                reason = result.get("reason", "")
                if reason == "already_off":
                    print("[DEBUG] 配乐已处于关闭状态，跳过", file=sys.stderr)
                    return
                elif reason == "clicked":
                    print("[DEBUG] 已点击音乐符号关闭配乐", file=sys.stderr)
                    return

            # 没找到按钮 → 重新 hover 视频中心（overlay 可能已消失），再试
            if attempt < max_retries - 1:
                cx = vb["x"] + vb["width"] / 2
                cy = vb["y"] + vb["height"] / 2
                await page.mouse.move(cx, cy)
                await asyncio.sleep(1.5)

        print(
            f"[WARN] {max_retries} 次尝试后仍未成功关闭配乐；"
            "可能控件栏未稳定出现或音乐按钮不在视频底部区域。",
            file=sys.stderr,
        )

    async def _recorder_stop(self, page: Page) -> None:
        """如有外部录屏配置，发送停止热键到 OS + 退出全屏。"""
        cfg = self.config or {}
        if not cfg.get("_recorder_enabled"):
            print(
                "[Recorder] 未配置外部录屏，跳过停止 —— 在 timeline YAML 顶层加 "
                "`recorder: {enabled: true}` 即可启用",
                file=sys.stderr,
            )
            return
        hk = cfg.get("_recorder_stop_hotkey")
        if not hk:
            print("[Recorder] 未设置 _recorder_stop_hotkey，跳过停止", file=sys.stderr)
        else:
            try:
                from ..recorder import ExternalRecorder

                ok = ExternalRecorder(stop_hotkey=hk).stop()
                print(
                    f"[Recorder] 停止录制热键结果: {'成功' if ok else '失败（见上方 traceback）'}",
                    file=sys.stderr,
                )
            except Exception:
                print(f"[Recorder] 停止录制异常:\n{traceback.format_exc()}", file=sys.stderr)
        await asyncio.sleep(0.5)
        await self._exit_fullscreen(page)
