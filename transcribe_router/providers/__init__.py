"""Provider 注册表。"""

from __future__ import annotations

from ..config import PROVIDER_ORDER, AppConfig, ProviderConfig
from .aliyun_isi import AliyunIsiProvider
from .bailian import BailianProvider
from .base import Provider, ProviderContext, RateLimiter, Segment, Utterance
from .iflytek import IflytekProvider
from .mock import MockProvider
from .netease import NeteaseProvider
from .tencent import TencentProvider
from .tingwu import TingwuProvider
from .volcano import VolcanoProvider

PROVIDER_CLASSES = {
    "tencent": TencentProvider,
    "aliyun_isi": AliyunIsiProvider,
    "bailian": BailianProvider,
    "tingwu": TingwuProvider,
    "iflytek": IflytekProvider,
    "volcano": VolcanoProvider,
    "netease": NeteaseProvider,
}


def build_providers(
    cfg: AppConfig,
    ctx: ProviderContext,
    *,
    dry_run: bool = False,
    only: str | None = None,
) -> tuple[list[Provider], list[str]]:
    """按配置实例化 provider，返回 (实例列表, 跳过说明)。"""
    providers: list[Provider] = []
    notes: list[str] = []
    mock_cfg = (cfg.mock or {}).get("providers") or {}

    for name in PROVIDER_ORDER:
        pcfg: ProviderConfig = cfg.providers[name]
        if only and name != only:
            continue
        if not pcfg.enabled:
            notes.append(f"{pcfg.display}: 配置中已禁用，跳过")
            continue

        if dry_run:
            behavior = mock_cfg.get(name) or {}
            providers.append(MockProvider(pcfg, ctx, behavior if isinstance(behavior, dict) else {}))
            continue

        instance = PROVIDER_CLASSES[name](pcfg, ctx)
        missing = instance.missing_credentials()
        if missing:
            notes.append(f"{pcfg.display}: 未配置凭据（{', '.join(missing)}），跳过")
            continue
        providers.append(instance)

    return providers, notes


__all__ = [
    "Provider",
    "ProviderContext",
    "RateLimiter",
    "Segment",
    "Utterance",
    "build_providers",
    "PROVIDER_CLASSES",
]
