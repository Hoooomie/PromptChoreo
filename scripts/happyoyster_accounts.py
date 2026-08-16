"""One-video email account pool for Happy Oyster automation."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EMAIL_KEYS = ("email", "mail", "username", "account", "邮箱", "邮箱号")
PASSWORD_KEYS = ("password", "passwd", "pwd", "密码")
ACCOUNT_LIST_KEYS = ("accounts", "users", "data")
STATE_VERSION = 3
VIDEOS_PER_ACCOUNT = 1


class AccountPoolExhausted(RuntimeError):
    """Raised when every account has exhausted its successful-video quota."""


@dataclass(frozen=True)
class HappyOysterAccount:
    email: str = field(repr=False)
    password: str = field(repr=False)
    fingerprint: str
    ordinal: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_string(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _account_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = None
        for key in ACCOUNT_LIST_KEYS:
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        if records is None and payload and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload.items()
        ):
            records = [
                {"email": email, "password": password}
                for email, password in payload.items()
            ]
        if records is None:
            raise ValueError(
                "账号 JSON 必须是账号数组，或包含 accounts/users/data 数组"
            )
    else:
        raise ValueError("账号 JSON 根节点必须是数组或对象")

    if not records:
        raise ValueError("账号 JSON 中没有账号")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("账号 JSON 中的每一项都必须是对象")
    return records


def load_accounts(path: str | os.PathLike[str]) -> list[HappyOysterAccount]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"账号 JSON 不存在: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"账号 JSON 格式无效: {source}: {exc}") from exc

    accounts: list[HappyOysterAccount] = []
    seen: set[str] = set()
    for index, record in enumerate(_account_records(payload), start=1):
        email = _first_string(record, EMAIL_KEYS)
        password = _first_string(record, PASSWORD_KEYS)
        if not email or not password:
            raise ValueError(
                f"账号 JSON 第 {index} 项缺少 email/password"
            )
        normalized = email.casefold()
        if normalized in seen:
            raise ValueError(f"账号 JSON 第 {index} 项邮箱重复")
        seen.add(normalized)
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        accounts.append(
            HappyOysterAccount(
                email=email,
                password=password,
                fingerprint=fingerprint,
                ordinal=index,
            )
        )
    return accounts


class AccountPool:
    """Persistently allow one successful video per account."""

    def __init__(
        self,
        accounts_path: str | os.PathLike[str],
        state_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.accounts_path = Path(accounts_path)
        self.state_path = (
            Path(state_path)
            if state_path
            else self.accounts_path.with_name(
                ".happyoyster_account_usage.json"
            )
        )
        if self.accounts_path.resolve() == self.state_path.resolve():
            raise ValueError("账号使用状态文件不能与账号源 JSON 是同一个文件")
        self.accounts = load_accounts(self.accounts_path)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": STATE_VERSION, "accounts": {}}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"账号使用状态文件格式无效: {self.state_path}: {exc}"
            ) from exc
        if not isinstance(state, dict) or not isinstance(
            state.get("accounts"), dict
        ):
            raise ValueError(f"账号使用状态文件结构无效: {self.state_path}")
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        state["version"] = STATE_VERSION
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    @staticmethod
    def _success_count(entry: dict[str, Any]) -> int:
        value = entry.get("success_count")
        if isinstance(value, int) and not isinstance(value, bool):
            return max(value, 0)
        # Version-1 state marked an account completed after its first video.
        # Treat that as one success so existing state gains exactly one slot.
        if entry.get("status") == "completed":
            return 1
        return 0

    @property
    def available_count(self) -> int:
        used = self._load_state()["accounts"]
        return sum(
            used.get(account.fingerprint, {}).get("status") != "claimed"
            and self._success_count(used.get(account.fingerprint, {}))
            < VIDEOS_PER_ACCOUNT
            for account in self.accounts
        )

    @property
    def remaining_video_slots(self) -> int:
        used = self._load_state()["accounts"]
        return sum(
            max(
                VIDEOS_PER_ACCOUNT
                - self._success_count(used.get(account.fingerprint, {})),
                0,
            )
            for account in self.accounts
            if used.get(account.fingerprint, {}).get("status") != "claimed"
        )

    def claim_next(self, job_id: str) -> HappyOysterAccount:
        state = self._load_state()
        used = state["accounts"]
        candidates = []
        for account in self.accounts:
            entry = used.get(account.fingerprint, {})
            success_count = self._success_count(entry)
            if (
                entry.get("status") == "claimed"
                or success_count >= VIDEOS_PER_ACCOUNT
            ):
                continue
            if entry.get("status") == "available" and success_count > 0:
                priority = 0
            elif not entry:
                priority = 1
            else:
                priority = 2
            candidates.append((priority, account.ordinal, account))

        for _, _, account in sorted(candidates):
            entry = used.get(account.fingerprint, {})
            success_count = self._success_count(entry)
            used[account.fingerprint] = {
                **entry,
                "status": "claimed",
                "job_id": job_id,
                "claimed_at_utc": _utc_now(),
                "success_count": success_count,
            }
            self._save_state(state)
            return account
        raise AccountPoolExhausted(
            "Happy Oyster 账号的单次成功视频额度均已用完"
        )

    def mark_finished(
        self,
        account: HappyOysterAccount,
        job_id: str,
        *,
        success: bool,
    ) -> None:
        state = self._load_state()
        entry = state["accounts"].setdefault(account.fingerprint, {})
        success_count = self._success_count(entry)
        if success:
            success_count += 1
        entry.update(
            {
                "status": (
                    "completed"
                    if success and success_count >= VIDEOS_PER_ACCOUNT
                    else ("available" if success else "failed")
                ),
                "job_id": job_id,
                "finished_at_utc": _utc_now(),
                "success_count": success_count,
            }
        )
        self._save_state(state)
