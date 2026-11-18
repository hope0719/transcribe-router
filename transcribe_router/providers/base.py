"""Provider 基类与公共数据结构。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import ProviderConfig
from ..errors import ConfigError, Unsupported
from ..hosting import Hosting
from ..utils import fmt_hours


@dataclass
class Segment:
    index: int
    path: Path
    start_ms: int
    end_ms: int
    boundary_ms: int
    url: str | None = None

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.end_ms - self.start_ms) / 1000)

    @property
    def size_mb(self) -> float:
        return self.path.stat().st_size / (1024 * 1024)

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower().lstrip(".") or "mp3"


@dataclass
class Utterance:
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None

    def to_dict(self) -> dict:
        data = {"start_ms": int(self.start_ms), "end_ms": int(self.end_ms), "text": self.text}
        if self.speaker not in (None, ""):
            data["speaker"] = str(self.speaker)
        return data


@dataclass
class ProviderContext:
    logger: object
    hosting: Hosting
    poll_interval: float = 5.0
    poll_timeout: float = 7200.0
    http_timeout: int = 30
    dry_run: bool = False
    limiter: "RateLimiter | None" = None
    sleep: Callable[[float], None] = time.sleep

    def log(self, level: str, message: str) -> None:
        getattr(self.logger, level)(message)


class RateLimiter:
    """按 provider 维度做最小间隔限流（不同线程共享）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, key: str, qps: float) -> float:
        qps = max(0.01, qps)
        with self._lock:
            now = time.monotonic()
            allowed = self._next.get(key, now)
            delay = max(0.0, allowed - now)
            self._next[key] = max(now, allowed) + 1.0 / qps
        if delay > 0:
            time.sleep(delay)
        return delay


class Provider:
    name = ""
    display = ""
    experimental = False
    requires_url = False
    CREDENTIAL_FIELDS: tuple[str, ...] = ()

    def __init__(self, cfg: ProviderConfig, ctx: ProviderContext):
        self.cfg = cfg
        self.ctx = ctx
        self.display = cfg.display or self.display or self.name

    # ------------------------------------------------------------------ #
    # 能力/限制
    # ------------------------------------------------------------------ #
    @property
    def max_file_seconds(self) -> float:
        return self.cfg.limits.max_file_seconds

    @property
    def max_file_mb(self) -> float:
        return self.cfg.limits.max_file_mb

    @property
    def qps(self) -> float:
        return self.cfg.limits.qps

    def option(self, key: str, default=None):
        return self.cfg.options.get(key, default)

    def credential(self, key: str) -> str:
        value = self.cfg.credentials.get(key)
        if value is None or str(value).strip() == "":
            raise ConfigError(
                f"{self.display} 缺少凭据 {key}，请在 config.yaml 的 providers.{self.name}.credentials 中填写"
                f"（建议写成 ${{环境变量名}} 并用 .env 提供值）"
            )
        return str(value).strip()

    def missing_credentials(self) -> list[str]:
        return [field for field in self.CREDENTIAL_FIELDS if not str(self.cfg.credentials.get(field) or "").strip()]

    def precheck(self, seg: Segment) -> str | None:
        """返回不适用原因；None 表示可以交给这家。"""
        if self.requires_url and not seg.url:
            return "只接受公网音频 URL，且未配置 hosting.url_prefix"
        return None

    def url_for(self, seg: Segment) -> str:
        if not seg.url:
            raise Unsupported(f"{self.display} 需要公网 URL，但当前片段没有可访问地址")
        return seg.url

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #
    def pace(self) -> None:
        if self.ctx.limiter:
            self.ctx.limiter.wait(self.name, self.qps)

    def transcribe(self, seg: Segment) -> list[Utterance]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe_quota(self, remaining_hours: float, expires_text: str) -> str:
        return f"{self.display}，剩余 {fmt_hours(remaining_hours)} / 额度到期 {expires_text}"


def clean_utterances(rows: list[Utterance]) -> list[Utterance]:
    """去掉空句子，修正时间区间，按开始时间排序。"""
    cleaned: list[Utterance] = []
    for row in rows:
        text = (row.text or "").strip()
        if not text:
            continue
        start = max(0, int(row.start_ms))
        end = max(start + 1, int(row.end_ms))
        cleaned.append(Utterance(start_ms=start, end_ms=end, text=text, speaker=row.speaker))
    cleaned.sort(key=lambda item: (item.start_ms, item.end_ms))
    return cleaned
