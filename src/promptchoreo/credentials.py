"""凭据与路径加载工具。

从项目根目录的 ``.credentials.yaml`` 读取站点凭据（如邮箱/密码），
并提供各站点浏览器持久化目录（``browser_data_*``）的动态路径。
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load() -> dict:
    """加载 .credentials.yaml（若不存在则返回空）。"""
    path = _PROJECT_ROOT / ".credentials.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_credentials(site: str) -> dict:
    """获取指定站点的凭据字典。

    返回一个包含站点相关字段的字典（如 ``email`` / ``password``），
    未配置时返回空字典。仅当对应站点需要凭据时才使用该方法。
    """
    data = _load()
    return data.get(site, {})


def get_browser_data_dir(site: str) -> str:
    """返回指定站点持久化浏览器的 user-data-dir 路径。

    目录放在当前用户 ``~/.workbuddy/`` 下，与站点名对应：
    - happy_oyster / odyssey → ``~/.workbuddy/browser_data``
    - pixverse → ``~/.workbuddy/browser_data_pixverse``
    - 其他 → ``~/.workbuddy/browser_data_{site}``
    """
    base = Path.home() / ".workbuddy"
    if site in ("happy_oyster", "odyssey"):
        return str(base / "browser_data")
    if site == "pixverse":
        return str(base / "browser_data_pixverse")
    return str(base / f"browser_data_{site}")
