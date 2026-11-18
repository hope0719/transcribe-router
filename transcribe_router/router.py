"""自动选路 + 降级。

排序规则
--------
1. 先看 ``priority``（大者先用）：阿里云三家 / 腾讯 / 火山为 10，讯飞 3，网易见外 1，
   这样小额或高价的 provider 天然排在最后；
2. 同优先级内按「剩余额度从多到少」排序。

降级规则
--------
额度耗尽 / 鉴权失败 / 超出单文件上限 / 接口报错 -> 记录原因并换下一家；
网络抖动、5xx、限流 -> 先在同一家重试 N 次，仍失败再换下一家。
每次切换都会打印「当前使用 X，剩余 Y / 额度到期 Z」。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .config import AppConfig
from .errors import (
    AllProvidersFailed,
    AuthError,
    FileTooLarge,
    ProviderError,
    QuotaExhausted,
    TransientError,
    Unsupported,
)
from .ledger import Ledger
from .providers import Provider, Segment, Utterance
from .utils import fmt_hours, fmt_ts


@dataclass
class Candidate:
    provider: Provider
    remaining: float


class Router:
    def __init__(
        self,
        providers: list[Provider],
        ledger: Ledger,
        cfg: AppConfig,
        logger,
        *,
        forced: str | None = None,
        dry_run: bool = False,
    ):
        self.providers = providers
        self.ledger = ledger
        self.cfg = cfg
        self.log = logger
        self.forced = forced
        self.dry_run = dry_run
        self._active: str | None = None
        self._last_reason: str | None = None
        self._lock = threading.Lock()
        self._skip_reasons: dict[str, str] = {}
        self._semaphores = {p.name: threading.Semaphore(cfg.concurrency.per_provider) for p in providers}

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #
    def expiry_text(self, name: str) -> str:
        expires_ts = self.ledger.expires_ts(name)
        if not expires_ts:
            return "长期（无固定到期日）"
        suffix = "（已过期）" if self.ledger.is_expired(name) else ""
        return f"{fmt_ts(expires_ts)}{suffix}"

    def quota_line(self, name: str) -> str:
        remaining = self.ledger.remaining_hours(name)
        return f"当前使用 {self._display(name)}，剩余 {fmt_hours(remaining)} / 额度到期 {self.expiry_text(name)}"

    def _display(self, name: str) -> str:
        for provider in self.providers:
            if provider.name == name:
                return provider.display
        return self.ledger.status(name)["display"]

    def startup_report(self) -> None:
        lines = ["", "=" * 78, "额度轮换已就绪（按优先级 / 剩余额度排序）"]
        ranked = sorted(self.providers, key=lambda p: (-p.cfg.priority, -self.ledger.remaining_hours(p.name)))
        for index, provider in enumerate(ranked, start=1):
            status = self.ledger.status(provider.name)
            mode_text = {"monthly": "每月重置", "daily": "每日重置", "fixed": "一次性"}[status["mode"]]
            flag = " [实验性]" if provider.experimental else ""
            lines.append(
                f"  {index}. {provider.display:<28}{flag} 剩余 {fmt_hours(status['remaining_hours']):>10}"
                f"  | {mode_text} | 到期 {self.expiry_text(provider.name)}"
            )
            if status["manual_exhausted"]:
                lines.append(f"     ! 已手动标记用完：{status['manual_reason'] or '无原因'}")
            elif status["auth_failed_until"] > time.time():
                lines.append(f"     ! 鉴权失败冷却中：{status['last_error'] or ''}")
            elif status["down_until"] > time.time():
                lines.append(f"     ! 连续失败熔断中：{status['last_error'] or ''}")
        if self.forced:
            lines.append(f"  * --provider 已强制指定：{self.forced}（不会自动降级到其它家）")
        lines.append("=" * 78)
        for line in lines:
            self.log.info(line)

        if ranked:
            best = ranked[0]
            reason = ""
            if self.ledger.remaining_hours(best.name) <= 0:
                reason = "（注意：额度账本显示该家已用完，首次调用可能直接失败）"
            self.log.info("当前使用 %s，剩余 %s / 额度到期 %s%s",
                          best.display,
                          fmt_hours(self.ledger.remaining_hours(best.name)),
                          self.expiry_text(best.name),
                          reason)

    def _announce_switch(self, chosen: Provider, prev: str | None, reason: str | None) -> None:
        if prev == chosen.name:
            return
        if prev is None:
            self.log.info("当前使用 %s，剩余 %s / 额度到期 %s",
                          chosen.display,
                          fmt_hours(self.ledger.remaining_hours(chosen.name)),
                          self.expiry_text(chosen.name))
        else:
            self.log.warning(
                "[切换] %s 不可用（%s）→ %s",
                self._display(prev),
                reason or "未知原因",
                self.quota_line(chosen.name),
            )

    # ------------------------------------------------------------------ #
    # 选路
    # ------------------------------------------------------------------ #
    def rank(self, seg: Segment | None = None) -> list[Candidate]:
        rows: list[Candidate] = []
        self._skip_reasons = {}
        for provider in self.providers:
            remaining = self.ledger.remaining_hours(provider.name)
            reason = self._ineligible_reason(provider, seg, remaining)
            if reason:
                self._skip_reasons[provider.name] = reason
                self.log.debug("跳过 %s：%s", provider.display, reason)
                continue
            rows.append(Candidate(provider=provider, remaining=remaining))

        rows.sort(key=lambda item: (-item.provider.cfg.priority, -item.remaining, item.provider.name))
        return rows

    def _ineligible_reason(self, provider: Provider, seg: Segment | None, remaining: float) -> str | None:
        forced = self.forced == provider.name
        if not forced:
            if self.ledger.manual_exhausted(provider.name):
                return "额度已被手动标记用完"
            if self.ledger.auth_failed(provider.name):
                return "鉴权失败冷却中"
            if self.ledger.is_down(provider.name):
                return "连续失败熔断中"
            if self.ledger.is_expired(provider.name):
                return "免费额度已过期"
            if remaining * 60 < self.cfg.routing.min_remaining_minutes:
                return "额度耗尽"
        if seg is None:
            return None
        if seg.duration_s > provider.max_file_seconds + 0.5:
            return f"片段时长 {seg.duration_s / 60:.1f} 分钟超过单文件上限 {provider.max_file_seconds / 60:.0f} 分钟"
        if seg.size_mb > provider.max_file_mb:
            return f"片段 {seg.size_mb:.1f}MB 超过单文件大小上限 {provider.max_file_mb:.0f}MB"
        return provider.precheck(seg)

    def describe_plan(self, seg: Segment) -> str:
        rows = self.rank(seg)
        if not rows:
            detail = "；".join(f"{self._display(name)}: {reason}" for name, reason in self._skip_reasons.items())
            return f"没有可用 provider（{detail}）"
        return " -> ".join(f"{item.provider.display}({fmt_hours(item.remaining)})" for item in rows)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> tuple[str, list[Utterance]]:
        candidates = self.rank(seg)
        if not candidates:
            detail = "；".join(f"{self._display(name)}: {reason}" for name, reason in self._skip_reasons.items())
            raise AllProvidersFailed(f"片段 {seg.index} 没有可用 provider。{detail}")

        reasons: list[str] = []
        for candidate in candidates:
            provider = candidate.provider
            if self.forced and candidate.remaining <= 0:
                self.log.warning("--provider 指定了 %s，但账本显示额度已用完，仍按你的要求尝试", provider.display)

            for attempt in range(self.cfg.routing.retries_per_provider + 1):
                try:
                    with self._semaphores[provider.name]:
                        utterances = provider.transcribe(seg)
                except AuthError as exc:
                    self.ledger.mark_auth_failed(provider.name, str(exc))
                    reasons.append(f"{provider.display}: 鉴权失败")
                    self._on_failure(provider, "鉴权失败", str(exc))
                    break
                except QuotaExhausted as exc:
                    self.ledger.mark_exhausted(provider.name, str(exc))
                    reasons.append(f"{provider.display}: 额度耗尽")
                    self._on_failure(provider, "额度耗尽", str(exc))
                    break
                except (FileTooLarge, Unsupported) as exc:
                    self.ledger.mark_error(provider.name, str(exc))
                    reasons.append(f"{provider.display}: {exc}")
                    self._on_failure(provider, "不适用", str(exc))
                    break
                except TransientError as exc:
                    self.ledger.mark_error(provider.name, str(exc))
                    if attempt < self.cfg.routing.retries_per_provider:
                        backoff = self.cfg.routing.retry_backoff_seconds * (attempt + 1)
                        self.log.warning("%s 瞬时错误，%.0fs 后重试（%d/%d）：%s",
                                         provider.display, backoff, attempt + 1,
                                         self.cfg.routing.retries_per_provider, exc)
                        time.sleep(backoff)
                        continue
                    reasons.append(f"{provider.display}: 重试后仍失败（{exc}）")
                    self._on_failure(provider, "重试后仍失败", str(exc))
                    self._record_failure(provider, exc)
                    break
                except ProviderError as exc:
                    self.ledger.mark_error(provider.name, str(exc))
                    reasons.append(f"{provider.display}: {exc}")
                    self._on_failure(provider, "接口报错", str(exc))
                    self._record_failure(provider, exc)
                    break
                except Exception as exc:  # 防御：任何未预期异常都不该中断整批任务
                    self.ledger.mark_error(provider.name, repr(exc))
                    reasons.append(f"{provider.display}: 未预期异常 {exc!r}")
                    self._on_failure(provider, "未预期异常", repr(exc))
                    self._record_failure(provider, exc)
                    break

                self._on_success(provider, seg)
                return provider.name, utterances

        raise AllProvidersFailed(f"片段 {seg.index} 全部 provider 失败：" + "；".join(reasons))

    def _on_success(self, provider: Provider, seg: Segment) -> None:
        hours = seg.duration_s / 3600.0
        self.ledger.record_usage(provider.name, hours)
        with self._lock:
            prev = self._active
            self._active = provider.name
        # 上一家如果是因为额度/资格被排除了，直接用 rank() 记下的原因，比“未知原因”清楚
        reason = None
        if prev:
            reason = self._skip_reasons.get(prev) or self._last_reason
        self._announce_switch(provider, prev, reason)
        self.log.info("[完成] 片段 %d 用时 %s，%s 剩余 %s / 到期 %s",
                      seg.index,
                      f"{seg.duration_s:.0f}s",
                      provider.display,
                      fmt_hours(self.ledger.remaining_hours(provider.name)),
                      self.expiry_text(provider.name))
        self._last_reason = None

    def _on_failure(self, provider: Provider, kind: str, detail: str) -> None:
        with self._lock:
            self._last_reason = f"{kind}：{detail}" if detail else kind
        self.log.warning("[降级] %s %s：%s", provider.display, kind, detail)

    def _record_failure(self, provider: Provider, detail) -> None:
        """连续失败计数，达到阈值后熔断，避免同一家被每段反复选中。"""
        self.ledger.record_failure(
            provider.name,
            str(detail),
            self.cfg.routing.max_consecutive_failures,
            self.cfg.routing.failure_cooldown_minutes,
        )
