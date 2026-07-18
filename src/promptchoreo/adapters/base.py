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
        """关闭生成界面的背景音乐（配乐）：点击界面上的音乐符号（🎵）。

        与浏览器层 ``--mute-audio`` / JS 全静音不同，这里**只关配乐、保留视频原声**。
        默认行为（config ``bgm_off`` 未设或为 True）即关闭配乐；设 ``bgm_off: false`` 可跳过。

        由于各站音乐符号的精确选择器未知，采用「候选选择器 + 坐标兜底 + 诊断 dump」策略：

        - 找到**唯一**匹配的按钮 → 按 ``aria-pressed`` 状态决定是否点击（避免重复切换）；
        - 找不到按钮 → hover 视频后点击视频右下角的音乐符号位置（PixVerse 当前 UI）；
        - 找到多个 → **不盲点**，dump 生成界面所有按钮供精确定位，WARN 跳过。
        """
        cfg = self.config or {}
        if cfg.get("bgm_off", True) is False:
            print("[DEBUG] 跳过关闭配乐（config bgm_off=false）", file=sys.stderr)
            return

        # ── 先 hover 视频区域，让被隐藏的控件栏（含 🎵）显示 ──
        # PixVerse 等站点的音乐符号在生成界面默认不可见，
        # 只有鼠标移到视频画面区域时控件栏才浮出。
        hovered = False
        video_box = None
        try:
            video = page.locator("video").first
            video_box = await video.bounding_box()
            if video_box:
                # 移到视频中心（大多数站控件在中间浮出）
                await page.mouse.move(
                    video_box["x"] + video_box["width"] / 2,
                    video_box["y"] + video_box["height"] / 2,
                )
                await asyncio.sleep(1.5)
                hovered = True
                print("[DEBUG-BGM] 已 hover 视频中心，等待控件栏出现", file=sys.stderr)
            else:
                print("[DEBUG-BGM] video bounding_box 为 None，跳过 hover", file=sys.stderr)
        except Exception as e:
            print(f"[DEBUG-BGM] hover 视频失败（跳过）: {e}", file=sys.stderr)

        # 诊断：dump 生成界面所有 button（含 aria-label/title/text/svg/aria-pressed），
        # 便于在没有精确选择器时精确定位音乐符号。
        try:
            buttons = await page.evaluate(
                """() => Array.from(document.querySelectorAll('button')).map(b => {
                    const svg = b.querySelector('svg') ? 'svg' : '';
                    const lbl = b.getAttribute('aria-label') || '';
                    const title = b.getAttribute('title') || '';
                    const txt = (b.innerText || '').trim().slice(0, 30);
                    return {lbl, title, txt, svg, pressed: b.getAttribute('aria-pressed')};
                })"""
            )
            music_like = [
                b for b in buttons
                if any(
                    k in (b.get("lbl", "") + b.get("title", "") + b.get("txt", "")).lower()
                    for k in ["music", "sound", "audio", "mute", "volume", "🎵", "♪"]
                )
            ]
            print(
                f"[DEBUG-BGM] hover={'OK' if hovered else 'SKIP'}，所有按钮数={len(buttons)}，"
                f"疑似音乐/声音按钮={music_like}",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"[DEBUG-BGM] 按钮 dump 失败: {e}", file=sys.stderr)

        # 候选选择器：音乐 / 声音 / 音频 / mute 相关的按钮或带 title 的元素
        candidates = [
            "button[aria-label*='music' i]",
            "button[aria-label*='sound' i]",
            "button[aria-label*='audio' i]",
            "button[aria-label*='mute' i]",
            "[title*='music' i]",
            "[title*='sound' i]",
            "[title*='audio' i]",
        ]

        matched: list = []
        for sel in candidates:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible(timeout=1500):
                    matched.append((sel, loc.first))
            except Exception:
                continue

        if len(matched) == 1:
            sel, btn = matched[0]
            try:
                pressed = await btn.get_attribute("aria-pressed")
                if pressed == "true":
                    await btn.click(timeout=3000)
                    print(f"[DEBUG] 已点击音乐符号关闭配乐（{sel}）", file=sys.stderr)
                elif pressed == "false":
                    print(f"[DEBUG] 配乐已处于关闭状态，跳过（{sel}）", file=sys.stderr)
                else:
                    # 状态未知：默认点击一次（用户确认符号当前是开的）
                    await btn.click(timeout=3000)
                    print(
                        f"[DEBUG] 已点击音乐符号（状态未知，默认关闭一次，{sel}）",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(f"[WARN] 点击音乐符号失败（{sel}）: {e}", file=sys.stderr)
        elif len(matched) == 0:
            if video_box:
                # PixVerse 的音乐符号是 hover 后出现在视频右下角的圆形按钮。
                # 它目前没有稳定可读的 aria/title，因此用视频包围盒内的相对坐标兜底。
                # 默认偏移来自 16:9 生成页实测：距离右边/底边约 24px。
                offset_x = float(cfg.get("bgm_button_offset_x", 24))
                offset_y = float(cfg.get("bgm_button_offset_y", 24))
                x = video_box["x"] + video_box["width"] - offset_x
                y = video_box["y"] + video_box["height"] - offset_y
                try:
                    await page.mouse.move(x, y)
                    await asyncio.sleep(0.6)
                    target_info = await page.evaluate(
                        """({x, y}) => {
                            const el = document.elementFromPoint(x, y);
                            if (!el) return null;
                            const clickable = el.closest('button,[role="button"],[aria-label],[title]');
                            const node = clickable || el;
                            return {
                                tag: node.tagName,
                                cls: String(node.className || '').slice(0, 120),
                                aria: node.getAttribute('aria-label') || '',
                                title: node.getAttribute('title') || '',
                                text: (node.innerText || '').trim().slice(0, 60),
                            };
                        }""",
                        {"x": x, "y": y},
                    )
                    print(f"[DEBUG-BGM] 右下角点击目标={target_info}", file=sys.stderr)
                    await page.mouse.click(x, y)
                    print(
                        f"[DEBUG] 已通过视频右下角坐标点击音乐符号关闭配乐 "
                        f"(x={x:.0f}, y={y:.0f})",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(f"[WARN] 坐标兜底点击音乐符号失败: {e}", file=sys.stderr)
            else:
                print(
                    "[WARN] 未找到音乐符号按钮（候选选择器均不匹配），且没有 video 边界，"
                    "无法坐标兜底关闭配乐；请把上方 [DEBUG-BGM] 的疑似按钮信息发我以精确定位。",
                    file=sys.stderr,
                )
        else:
            print(
                f"[WARN] 匹配到多个音乐符号候选（{[m[0] for m in matched]}），"
                "为避免误点，暂未点击；请发 [DEBUG-BGM] 信息帮我锁定唯一选择器。",
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
