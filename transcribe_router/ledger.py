"""额度账本：记录每家已用时长、剩余额度、重置/过期规则与连续失败熔断。

持久化为 ``<state_dir>/ledger.json``，进程重启后不丢。

并发安全
--------
- 所有写入都在 ``ledger.lock`` 的**跨进程排他锁**内完成，且先重新读盘再改再写，
  多进程同时跑不会互相覆盖（读-改-写是一个原子区间）
- 落盘用「临时文件 + os.replace」保证原子性，中途被杀不会留下半截 JSON
- 读取不加锁，依赖原子替换：读到的要么是旧版本，要么是新版本

额度类型（对应 config 的 providers.<name>.quota.mode）：

- ``monthly``  每月 1 日 0 点重置
- ``daily``    每天 0 点重置
- ``fixed``    一次性总额度，用完即止

到期规则（两条互相独立，可都留空表示不过期）：

- ``quota.expires_at``         绝对日期（``2027-02-15`` 或 ISO 时间），优先使用
- ``quota.expires_after_days`` 从**首次成功转写**起算 N 天；首次用量的时间戳会写入账本的
  ``first_used_at``，之后每次读账本都按 ``first_used_at + N 天`` 推算到期时间
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import AppConfig, ProviderConfig
from .utils import file_lock, fmt_hours, local_iso, now_ts

ENTRY_DEFAULTS = {
    "quota_mode": "monthly",
    "quota_hours": 0.0,
    "used_hours_period": 0.0,
    "period_key": "",
    "used_hours_total": 0.0,
    "expires_at": None,
    "expires_after_days": None,
    "first_used_at": None,
    "manual_exhausted": False,
    "manual_reason": None,
    "auth_failed_until": 0.0,
    "auth_error": None,
    "consecutive_failures": 0,
    "down_until": 0.0,
    "last_error": None,
    "last_used_at": None,
    "updated_at": None,
}


def _parse_expiry(value: str | None) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime.combine(day, time(23, 59, 59)).timestamp()
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _entry_expiry_ts(entry: dict) -> float | None:
    """到期时间：绝对日期优先，其次按「首次使用 + N 天」推算。"""
    absolute = _parse_expiry(entry.get("expires_at"))
    if absolute is not None:
        return absolute
    days = entry.get("expires_after_days")
    first_used = _parse_expiry(entry.get("first_used_at"))
    if not days or first_used is None:
        return None
    return first_used + float(days) * 86400


def _period_key(mode: str, ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    if mode == "monthly":
        return dt.strftime("%Y-%m")
    if mode == "daily":
        return dt.strftime("%Y-%m-%d")
    return "fixed"


def _next_reset_ts(mode: str, ts: float) -> float | None:
    dt = datetime.fromtimestamp(ts)
    if mode == "daily":
        tomorrow = (dt + timedelta(days=1)).date()
        return datetime.combine(tomorrow, time(0, 0, 0)).timestamp()
    if mode == "monthly":
        year, month = dt.year, dt.month + 1
        if month > 12:
            year, month = year + 1, 1
        return datetime(year, month, 1).timestamp()
    return None


class Ledger:
    def __init__(self, path: Path, configs: dict[str, ProviderConfig], auth_retry_minutes: float = 30.0):
        self.path = Path(path)
        self.lock_path = self.path.with_name("ledger.lock")
        self.configs = configs
        self.auth_retry_minutes = auth_retry_minutes
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._stamp: tuple[int, int] | None = None
        self._read()
        self.sync_configs()

    # ------------------------------------------------------------------ #
    # 读写基础
    # ------------------------------------------------------------------ #
    def _disk_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _read(self) -> None:
        stamp = self._disk_stamp()
        if stamp is None:
            self._entries = {}
            self._stamp = None
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 文件损坏（极少见）：保留现有内存副本，不覆盖磁盘，避免静默丢账
            return
        entries = data.get("providers") or {}
        if isinstance(entries, dict):
            self._entries = {k: dict(v) for k, v in entries.items() if isinstance(v, dict)}
            self._stamp = stamp

    def _refresh(self, *, force: bool = False) -> None:
        """磁盘被其它进程改过就重新加载。"""
        if force or self._disk_stamp() != self._stamp:
            self._read()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": local_iso(), "providers": self._entries}
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self._stamp = self._disk_stamp()

    def _entry(self, name: str) -> dict:
        entry = self._entries.get(name)
        if entry is None:
            entry = dict(ENTRY_DEFAULTS)
            self._entries[name] = entry
        for key, value in ENTRY_DEFAULTS.items():
            entry.setdefault(key, value)
        return entry

    def _normalize_periods(self) -> None:
        ts = now_ts()
        for entry in self._entries.values():
            key = _period_key(entry["quota_mode"], ts)
            if entry.get("period_key") != key:
                entry["period_key"] = key
                entry["used_hours_period"] = 0.0
                if entry.get("manual_exhausted"):
                    entry["manual_exhausted"] = False
                    entry["manual_reason"] = None
            # 旧版本账本没有 first_used_at：用过就按最后一次用量补记，避免试用期永不开始
            if (
                entry.get("expires_after_days")
                and not entry.get("first_used_at")
                and float(entry.get("used_hours_total") or 0) > 0
            ):
                entry["first_used_at"] = entry.get("last_used_at") or entry.get("updated_at") or local_iso()

    def _mutate(self, name: str, apply) -> None:
        """在跨进程排他锁内完成 读盘 -> 修改 -> 原子写。"""
        with self._lock:
            with file_lock(self.lock_path, exclusive=True):
                self._refresh(force=True)
                entry = self._entry(name)
                self._normalize_periods()
                apply(entry)
                entry["updated_at"] = local_iso()
                self._write()

    # ------------------------------------------------------------------ #
    # 配置同步
    # ------------------------------------------------------------------ #
    def sync_configs(self) -> None:
        with self._lock:
            with file_lock(self.lock_path, exclusive=True):
                self._refresh(force=True)
                for name, cfg in self.configs.items():
                    entry = self._entry(name)
                    entry["quota_mode"] = cfg.quota.mode
                    entry["quota_hours"] = float(cfg.quota.hours)
                    entry["expires_at"] = cfg.quota.expires_at
                    entry["expires_after_days"] = cfg.quota.expires_after_days
                self._normalize_periods()
                self._write()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def expires_ts(self, name: str) -> float | None:
        with self._lock:
            self._refresh()
            return _entry_expiry_ts(self._entry(name))

    def is_expired(self, name: str) -> bool:
        expiry = self.expires_ts(name)
        return bool(expiry and now_ts() >= expiry)

    def auth_failed(self, name: str) -> bool:
        with self._lock:
            self._refresh()
            return float(self._entry(name).get("auth_failed_until") or 0) > now_ts()

    def is_down(self, name: str) -> bool:
        with self._lock:
            self._refresh()
            return float(self._entry(name).get("down_until") or 0) > now_ts()

    def manual_exhausted(self, name: str) -> bool:
        with self._lock:
            self._refresh()
            return bool(self._entry(name).get("manual_exhausted"))

    def _remaining_locked(self, name: str) -> float:
        entry = self._entry(name)
        if entry.get("manual_exhausted") or self.auth_failed(name) or self.is_expired(name):
            return 0.0
        total = float(entry.get("quota_hours") or 0)
        used = float(entry.get("used_hours_period") or 0)
        return max(0.0, total - used)

    def remaining_hours(self, name: str) -> float:
        with self._lock:
            self._refresh()
            self._normalize_periods()
            return self._remaining_locked(name)

    def status(self, name: str) -> dict:
        with self._lock:
            self._refresh()
            self._normalize_periods()
            entry = self._entry(name)
            cfg = self.configs[name]
            remaining = self._remaining_locked(name)
            expiry = _entry_expiry_ts(entry)
            next_reset = _next_reset_ts(entry["quota_mode"], now_ts())

            if entry.get("manual_exhausted"):
                state = "手动标记用完"
            elif self.auth_failed(name):
                state = "鉴权失败（冷却中）"
            elif self.is_down(name):
                state = "连续失败熔断中"
            elif self.is_expired(name):
                state = "额度已过期"
            elif remaining <= 0:
                state = "额度耗尽"
            else:
                state = "可用"

            return {
                "name": name,
                "display": cfg.display,
                "enabled": cfg.enabled,
                "priority": cfg.priority,
                "mode": entry["quota_mode"],
                "quota_hours": float(entry.get("quota_hours") or 0),
                "used_hours_period": float(entry.get("used_hours_period") or 0),
                "used_hours_total": float(entry.get("used_hours_total") or 0),
                "remaining_hours": remaining,
                "remaining_text": fmt_hours(remaining),
                "expires_at": entry.get("expires_at"),
                "expires_after_days": entry.get("expires_after_days"),
                "first_used_at": entry.get("first_used_at"),
                "expires_ts": expiry,
                "next_reset_ts": next_reset,
                "manual_exhausted": bool(entry.get("manual_exhausted")),
                "manual_reason": entry.get("manual_reason"),
                "auth_failed_until": float(entry.get("auth_failed_until") or 0),
                "consecutive_failures": int(entry.get("consecutive_failures") or 0),
                "down_until": float(entry.get("down_until") or 0),
                "last_error": entry.get("last_error"),
                "state": state,
            }

    def all_status(self) -> list[dict]:
        return [self.status(name) for name in self.configs]

    # ------------------------------------------------------------------ #
    # 变更
    # ------------------------------------------------------------------ #
    def record_usage(self, name: str, hours: float) -> None:
        if hours <= 0:
            return

        def apply(entry: dict) -> None:
            if not entry.get("first_used_at"):
                entry["first_used_at"] = local_iso()
            entry["used_hours_period"] = float(entry.get("used_hours_period") or 0) + hours
            entry["used_hours_total"] = float(entry.get("used_hours_total") or 0) + hours
            entry["last_used_at"] = local_iso()
            entry["auth_failed_until"] = 0.0
            entry["auth_error"] = None
            entry["consecutive_failures"] = 0
            entry["down_until"] = 0.0
            entry["last_error"] = None

        self._mutate(name, apply)

    def mark_exhausted(self, name: str, reason: str) -> None:
        def apply(entry: dict) -> None:
            entry["manual_exhausted"] = True
            entry["manual_reason"] = reason
            entry["last_error"] = reason

        self._mutate(name, apply)

    def mark_auth_failed(self, name: str, reason: str) -> None:
        def apply(entry: dict) -> None:
            entry["auth_failed_until"] = now_ts() + self.auth_retry_minutes * 60
            entry["auth_error"] = reason
            entry["last_error"] = reason

        self._mutate(name, apply)

    def record_failure(self, name: str, reason: str, threshold: int, cooldown_minutes: float) -> None:
        """连续失败计数；达到阈值触发熔断，冷却期内不再选这家。"""

        def apply(entry: dict) -> None:
            count = int(entry.get("consecutive_failures") or 0) + 1
            entry["last_error"] = reason
            if threshold > 0 and count >= threshold:
                entry["down_until"] = now_ts() + max(1.0, cooldown_minutes) * 60
                entry["consecutive_failures"] = 0
                entry["last_error"] = f"{reason}（连续失败 {count} 次，熔断 {cooldown_minutes:g} 分钟）"
            else:
                entry["consecutive_failures"] = count

        self._mutate(name, apply)

    def mark_error(self, name: str, reason: str) -> None:
        def apply(entry: dict) -> None:
            entry["last_error"] = reason

        self._mutate(name, apply)

    def reset(self, name: str, *, keep_total: bool = True) -> None:
        def apply(entry: dict) -> None:
            entry["manual_exhausted"] = False
            entry["manual_reason"] = None
            entry["auth_failed_until"] = 0.0
            entry["auth_error"] = None
            entry["consecutive_failures"] = 0
            entry["down_until"] = 0.0
            entry["last_error"] = None
            entry["used_hours_period"] = 0.0
            entry["period_key"] = _period_key(entry["quota_mode"], now_ts())
            if not keep_total:
                entry["used_hours_total"] = 0.0

        self._mutate(name, apply)


def open_ledger(cfg: AppConfig) -> Ledger:
    return Ledger(cfg.state_dir / "ledger.json", cfg.providers, cfg.routing.auth_retry_minutes)
