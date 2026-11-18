"""Mock Provider：干跑模式专用，不发起任何网络请求。

用于验证「额度轮换 + 降级 + 切段 + 合并 + SRT」整条链路，
每段返回按固定规则生成的句级结果，时间戳基于片段内相对时间。

行为可通过 config 的 mock.providers.<name> 注入：
    auth_fail: true           一直鉴权失败
    transient_failures: 2     前 2 次调用抛瞬时错误
    fatal_fail: true          一直抛不可重试的接口错误（用于验证连续失败熔断）
    latency_seconds: 0.2      每次调用模拟耗时
"""

from __future__ import annotations

import threading

from ..errors import AuthError, FatalProviderError, TransientError
from .base import Provider, Segment, Utterance


class MockProvider(Provider):
    """所有真实 provider 在 --dry-run 下都会被替换成它，但额度/限制仍取真实配置。"""

    def __init__(self, cfg, ctx, behavior: dict | None = None):
        super().__init__(cfg, ctx)
        self.name = cfg.name
        self.display = cfg.display
        self.requires_url = cfg.requires_url
        self.experimental = cfg.experimental
        self.behavior = behavior or {}
        self._lock = threading.Lock()
        self._transient_left = int(self.behavior.get("transient_failures", 0) or 0)

    def precheck(self, seg: Segment) -> str | None:
        # 干跑也要反映「只收公网 URL」这类硬约束，否则验证结果会失真
        if self.cfg.requires_url and not seg.url:
            return "只接受公网音频 URL，且未配置 hosting.url_prefix"
        return None

    def transcribe(self, seg: Segment) -> list[Utterance]:
        if self.behavior.get("auth_fail"):
            raise AuthError(f"[mock] {self.display} 模拟鉴权失败")
        if self.behavior.get("fatal_fail"):
            raise FatalProviderError(f"[mock] {self.display} 模拟不可重试的接口错误")

        with self._lock:
            if self._transient_left > 0:
                self._transient_left -= 1
                raise TransientError(f"[mock] {self.display} 模拟网络抖动（剩余 {self._transient_left} 次）")

        latency = float(self.behavior.get("latency_seconds", 0) or 0)
        if latency:
            self.ctx.sleep(latency)

        duration_ms = int(seg.duration_s * 1000)
        if duration_ms <= 0:
            return []

        chunk = max(1, duration_ms // 4)
        rows: list[Utterance] = []
        cursor = 0
        index = 0
        while cursor < duration_ms:
            end = min(duration_ms, cursor + chunk)
            index += 1
            rows.append(
                Utterance(
                    start_ms=cursor,
                    end_ms=end,
                    text=f"【模拟】{self.name} 片段{seg.index} 第{index}句",
                    speaker="1" if index % 2 else "2",
                )
            )
            cursor = end
        return rows
