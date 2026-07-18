"""外部录屏工具热键桥接。

通过模拟全局热键控制 EV录屏等外部工具的录制启停。

重要：优先使用 ``keyboard`` 库。原因——EV录屏之类的全局热键监听通常只认
``SendInput`` 产生的输入；``pyautogui.hotkey`` 底层走的是老式 ``keybd_event``，
对合成输入经常被全局热键钩子忽略，导致"按了但没反应"。``keyboard`` 库发送的是
``SendInput``，对全局热键最可靠（这也是单独测试脚本能按成功的原因）。

任何失败都会完整打印到 stderr，不再静默吞掉。
"""

from __future__ import annotations

import sys
import traceback


class ExternalRecorder:
    """通过模拟热键控制外部录屏工具。

    Parameters
    ----------
    start_hotkey : str
        开始录制热键，如 ``"ctrl+f1"``。
    stop_hotkey : str
        停止录制热键，如 ``"ctrl+f2"``。
    """

    def __init__(self, start_hotkey: str = "ctrl+f1", stop_hotkey: str = "ctrl+f2") -> None:
        self._start_hotkey = start_hotkey
        self._stop_hotkey = stop_hotkey
        self._backend = self._load_backend()

    @staticmethod
    def _load_backend() -> str | None:
        """选择可用的热键后端：优先 keyboard（SendInput），回退 pyautogui。"""
        try:
            import keyboard  # noqa: F401

            print("[Recorder] 热键后端: keyboard (SendInput，推荐)", file=sys.stderr)
            return "keyboard"
        except Exception as exc:  # pragma: no cover - 取决于环境
            print(f"[Recorder] keyboard 不可用，回退 pyautogui: {exc}", file=sys.stderr)
        try:
            import pyautogui  # noqa: F401

            print("[Recorder] 热键后端: pyautogui (keybd_event，可能不被全局热键识别)", file=sys.stderr)
            return "pyautogui"
        except Exception as exc:  # pragma: no cover - 取决于环境
            print(f"[Recorder] pyautogui 也不可用: {exc}", file=sys.stderr)
            return None

    def _send(self, combo: str) -> None:
        """发送组合键。combo 形如 'ctrl+f1'。"""
        if self._backend == "keyboard":
            import keyboard

            keyboard.send(combo)
        elif self._backend == "pyautogui":
            import pyautogui

            pyautogui.hotkey(*combo.split("+"))
        else:
            raise RuntimeError(
                "没有可用的热键后端：keyboard 与 pyautogui 均未安装。"
                "请 pip install keyboard"
            )

    def start(self) -> bool:
        """发送开始录制热键。返回是否发送成功（不代表 EV 一定开始录制）。"""
        print(
            f"[Recorder] 尝试开始录制: {self._start_hotkey!r} (backend={self._backend})",
            file=sys.stderr,
        )
        try:
            self._send(self._start_hotkey)
            print(f"[Recorder] 已开始热键已发送: {self._start_hotkey!r}", file=sys.stderr)
            return True
        except Exception:
            print(f"[Recorder] 开始录制失败:\n{traceback.format_exc()}", file=sys.stderr)
            return False

    def stop(self) -> bool:
        """发送停止录制热键。"""
        print(
            f"[Recorder] 尝试停止录制: {self._stop_hotkey!r} (backend={self._backend})",
            file=sys.stderr,
        )
        try:
            self._send(self._stop_hotkey)
            print(f"[Recorder] 已停止热键已发送: {self._stop_hotkey!r}", file=sys.stderr)
            return True
        except Exception:
            print(f"[Recorder] 停止录制失败:\n{traceback.format_exc()}", file=sys.stderr)
            return False
