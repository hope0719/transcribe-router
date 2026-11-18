"""配置加载：config.yaml + .env。

- 真实密钥只从环境变量 / .env 读，配置文件里写 ``${VAR}`` 占位符
- 内置一个 YAML 子集解析器：没装 PyYAML 也能跑。装了 PyYAML 则用完整解析器
- 支持的环境变量写法：``${VAR}``、``${VAR:-默认值}``
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

# --------------------------------------------------------------------------- #
# 7 家 Provider 的内置默认值（额度/限制来自报告）
# --------------------------------------------------------------------------- #

PROVIDER_DEFAULTS: dict[str, dict] = {
    "tencent": {
        "display": "腾讯云 ASR（录音文件识别）",
        "priority": 10,
        "requires_url": False,
        "quota": {"mode": "monthly", "hours": 10, "expires_at": None},
        "limits": {"max_file_seconds": 5 * 3600, "max_file_mb": 1024, "qps": 20},
        "options": {
            "region": "ap-shanghai",
            "engine_model_type": "16k_zh",
            "res_text_format": 3,
            "speaker_diarization": 0,
        },
    },
    "aliyun_isi": {
        "display": "阿里云智能语音交互 ISI（录音文件识别）",
        "priority": 10,
        "requires_url": True,
        "quota": {"mode": "daily", "hours": 2, "expires_at": None},
        "limits": {"max_file_seconds": 12 * 3600, "max_file_mb": 512, "qps": 1},
        "options": {"region": "cn-shanghai", "version": "4.0", "enable_words": False},
    },
    "bailian": {
        "display": "阿里云百炼 Paraformer（非实时）",
        "priority": 10,
        "requires_url": True,
        "quota": {"mode": "fixed", "hours": 10, "expires_at": None},
        "limits": {"max_file_seconds": 12 * 3600, "max_file_mb": 2048, "qps": 1},
        "options": {"model": "paraformer-v2", "workspace_id": ""},
    },
    "tingwu": {
        # 通义听悟的 HTTP 签名/返回结构官方页未给完整示例，标为实验性
        "display": "通义听悟 API（离线转写）",
        "priority": 10,
        "requires_url": True,
        "experimental": True,
        "quota": {"mode": "daily", "hours": 2, "expires_at": None},
        "limits": {"max_file_seconds": 12 * 3600, "max_file_mb": 2048, "qps": 1},
        "options": {"source_language": "cn", "app_key": "", "diarization": True},
    },
    "iflytek": {
        "display": "讯飞开放平台（语音转写）",
        "priority": 3,  # 个人 5 小时/年，额度小，排最后
        "requires_url": False,
        "quota": {"mode": "fixed", "hours": 5, "expires_at": None},
        "limits": {"max_file_seconds": 5 * 3600, "max_file_mb": 500, "qps": 20},
        "options": {"language": "cn", "has_seperate": False, "slice_mb": 10},
    },
    "volcano": {
        "display": "火山引擎豆包（录音文件识别标准版）",
        "priority": 10,
        "requires_url": True,
        "quota": {"mode": "fixed", "hours": 20, "expires_at": None},
        "limits": {"max_file_seconds": 5 * 3600, "max_file_mb": 512, "qps": 1},
        "options": {"cluster": "", "uid": "transcribe-router", "with_speaker_info": True},
    },
    "netease": {
        "display": "网易见外（有道长语音转写）",
        "priority": 1,  # 300 分钟试用 + 24 元/小时，排最后
        "requires_url": False,
        "quota": {"mode": "fixed", "hours": 5, "expires_at": None},
        "limits": {"max_file_seconds": 4 * 3600, "max_file_mb": 500, "qps": 1},
        "options": {"lang_type": "zh-CHS2", "need_speaker_id": False, "slice_mb": 10},
    },
}

PROVIDER_ORDER = ["tencent", "aliyun_isi", "bailian", "tingwu", "iflytek", "volcano", "netease"]


# --------------------------------------------------------------------------- #
# 配置对象
# --------------------------------------------------------------------------- #


@dataclass
class QuotaConfig:
    mode: str = "monthly"  # monthly | daily | fixed
    hours: float = 0.0
    expires_at: str | None = None


@dataclass
class LimitsConfig:
    max_file_seconds: float = 5 * 3600
    max_file_mb: float = 512
    qps: float = 1.0


@dataclass
class ProviderConfig:
    name: str
    display: str
    enabled: bool = True
    priority: int = 10
    weight: float = 1.0
    experimental: bool = False
    requires_url: bool = False
    credentials: dict = field(default_factory=dict)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    options: dict = field(default_factory=dict)
    env_credentials: list[str] = field(default_factory=list)


@dataclass
class RoutingConfig:
    min_remaining_minutes: float = 1.0
    retries_per_provider: int = 2
    retry_backoff_seconds: float = 3.0
    poll_interval_seconds: float = 5.0
    poll_timeout_seconds: float = 7200.0
    auth_retry_minutes: float = 30.0
    max_consecutive_failures: int = 3
    failure_cooldown_minutes: float = 15.0


@dataclass
class ConcurrencyConfig:
    workers: int = 2
    per_provider: int = 1


@dataclass
class SegmentationConfig:
    mode: str = "silence"  # silence | fixed
    max_seconds: float = 0.0  # 0 = 用当前 provider 的单文件上限
    overlap_seconds: float = 0.5
    silence_noise_db: float = -35.0
    silence_min_seconds: float = 0.4
    search_window_seconds: float = 30.0


@dataclass
class OutputConfig:
    out_dir: str = "./out"
    json: bool = True
    srt: bool = True
    srt_max_chars: int = 0
    keep_segments: bool = False


@dataclass
class HostingConfig:
    mode: str = "none"  # none | url_prefix
    url_prefix: str = ""
    local_root: str = ""


@dataclass
class AppConfig:
    path: Path
    state_dir: Path
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    hosting: HostingConfig = field(default_factory=HostingConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    mock: dict = field(default_factory=dict)

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise ConfigError(f"未知的 provider: {name}，可选值：{', '.join(PROVIDER_ORDER)}") from exc


# --------------------------------------------------------------------------- #
# 极简 YAML 解析（PyYAML 缺失时兜底）
# --------------------------------------------------------------------------- #

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(text: str):
    text = text.strip()
    if text == "" or text.lower() in ("null", "~"):
        return None
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [] if not inner else [_scalar(part) for part in inner.split(",")]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        if not inner:
            return {}
        result = {}
        for part in inner.split(","):
            key, _, value = part.partition(":")
            result[key.strip()] = _scalar(value.strip())
        return result
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


class _MiniYaml:
    """只支持配置文件里实际用到的子集：嵌套映射、标量、标量列表、内联 {} []。"""

    def __init__(self, text: str):
        self.lines = text.splitlines()

    def parse(self) -> dict:
        value, _ = self._block(0, 0)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError("配置文件顶层必须是映射（key: value）")
        return value

    def _skip(self, idx: int) -> int:
        while idx < len(self.lines):
            stripped = _strip_comment(self.lines[idx]).strip()
            if stripped:
                break
            idx += 1
        return idx

    def _block(self, idx: int, indent: int):
        idx = self._skip(idx)
        if idx >= len(self.lines):
            return None, idx
        line = self.lines[idx]
        if _indent(line) < indent:
            return None, idx
        if _strip_comment(line).strip().startswith("- "):
            return self._list(idx, _indent(line))
        return self._map(idx, _indent(line))

    def _map(self, idx: int, indent: int):
        result: dict = {}
        while True:
            idx = self._skip(idx)
            if idx >= len(self.lines):
                break
            raw = self.lines[idx]
            cur = _indent(raw)
            if cur < indent:
                break
            if cur > indent:
                raise ConfigError(f"第 {idx + 1} 行缩进异常：{raw.strip()}")
            text = _strip_comment(raw).strip()
            if text.startswith("- "):
                break
            key, sep, rest = text.partition(":")
            if not sep:
                raise ConfigError(f"第 {idx + 1} 行缺少冒号：{text}")
            key = key.strip().strip("'\"")
            rest = rest.strip()
            idx += 1
            if rest:
                result[key] = _scalar(rest)
                continue
            nxt = self._skip(idx)
            if nxt < len(self.lines):
                nxt_indent = _indent(self.lines[nxt])
                nxt_text = _strip_comment(self.lines[nxt]).strip()
                if nxt_indent > indent:
                    result[key], idx = self._block(nxt, nxt_indent)
                    continue
                if nxt_indent == indent and nxt_text.startswith("- "):
                    result[key], idx = self._list(nxt, indent)
                    continue
            result[key] = None
        return result, idx

    def _list(self, idx: int, indent: int):
        items: list = []
        while True:
            idx = self._skip(idx)
            if idx >= len(self.lines):
                break
            raw = self.lines[idx]
            if _indent(raw) != indent:
                break
            text = _strip_comment(raw).strip()
            if not text.startswith("- "):
                break
            item = text[2:].strip()
            idx += 1
            if ":" in item and not item.startswith(("'", '"', "{")):
                key, _, value = item.partition(":")
                items.append({key.strip(): _scalar(value.strip())})
            else:
                items.append(_scalar(item))
        return items, idx


def _load_yaml(text: str) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _MiniYaml(text).parse()
    loaded = yaml.safe_load(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("配置文件顶层必须是映射（key: value）")
    return loaded


def _load_dotenv(path: Path) -> None:
    """把 .env 里的键值注入 os.environ（不覆盖已有环境变量）。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _expand_env(value):
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name, default if default is not None else "")

        return _ENV_RE.sub(repl, value)
    return value


# --------------------------------------------------------------------------- #
# 加载入口
# --------------------------------------------------------------------------- #


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _as_float(value, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} 必须是数字，当前值：{value!r}") from exc


def load_config(path: str | os.PathLike) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在：{config_path}")

    base_dir = config_path.parent
    _load_dotenv(base_dir / ".env")
    _load_dotenv(Path.cwd() / ".env")

    raw = _expand_env(_load_yaml(config_path.read_text(encoding="utf-8")))
    cfg = AppConfig(path=config_path, state_dir=base_dir / ".tr_state")

    state_dir = raw.get("state_dir")
    if state_dir:
        cfg.state_dir = _resolve_path(base_dir, state_dir)

    routing = raw.get("routing") or {}
    cfg.routing = RoutingConfig(
        min_remaining_minutes=_as_float(routing.get("min_remaining_minutes", 1.0), "routing.min_remaining_minutes"),
        retries_per_provider=int(routing.get("retries_per_provider", 2)),
        retry_backoff_seconds=_as_float(routing.get("retry_backoff_seconds", 3.0), "routing.retry_backoff_seconds"),
        poll_interval_seconds=_as_float(routing.get("poll_interval_seconds", 5.0), "routing.poll_interval_seconds"),
        poll_timeout_seconds=_as_float(routing.get("poll_timeout_seconds", 7200.0), "routing.poll_timeout_seconds"),
        auth_retry_minutes=_as_float(routing.get("auth_retry_minutes", 30.0), "routing.auth_retry_minutes"),
        max_consecutive_failures=int(routing.get("max_consecutive_failures", 3)),
        failure_cooldown_minutes=_as_float(
            routing.get("failure_cooldown_minutes", 15.0), "routing.failure_cooldown_minutes"
        ),
    )

    conc = raw.get("concurrency") or {}
    cfg.concurrency = ConcurrencyConfig(
        workers=max(1, int(conc.get("workers", 2))),
        per_provider=max(1, int(conc.get("per_provider", 1))),
    )

    seg = raw.get("segmentation") or {}
    cfg.segmentation = SegmentationConfig(
        mode=str(seg.get("mode", "silence")),
        max_seconds=_as_float(seg.get("max_seconds", 0), "segmentation.max_seconds"),
        overlap_seconds=_as_float(seg.get("overlap_seconds", 0.5), "segmentation.overlap_seconds"),
        silence_noise_db=_as_float(seg.get("silence_noise_db", -35), "segmentation.silence_noise_db"),
        silence_min_seconds=_as_float(seg.get("silence_min_seconds", 0.4), "segmentation.silence_min_seconds"),
        search_window_seconds=_as_float(seg.get("search_window_seconds", 30), "segmentation.search_window_seconds"),
    )
    if cfg.segmentation.mode not in ("silence", "fixed"):
        raise ConfigError("segmentation.mode 只能是 silence 或 fixed")
    if cfg.segmentation.overlap_seconds < 0:
        raise ConfigError("segmentation.overlap_seconds 不能为负")

    out = raw.get("output") or {}
    cfg.output = OutputConfig(
        out_dir=str(out.get("dir", "./out")),
        json=bool(out.get("json", True)),
        srt=bool(out.get("srt", True)),
        srt_max_chars=int(out.get("srt_max_chars", 0)),
        keep_segments=bool(out.get("keep_segments", False)),
    )

    hosting = raw.get("hosting") or {}
    cfg.hosting = HostingConfig(
        mode=str(hosting.get("mode", "none")),
        url_prefix=str(hosting.get("url_prefix", "") or ""),
        local_root=str(hosting.get("local_root", "") or ""),
    )
    if cfg.hosting.mode not in ("none", "url_prefix"):
        raise ConfigError("hosting.mode 只能是 none 或 url_prefix")
    if cfg.hosting.mode == "url_prefix":
        if not cfg.hosting.url_prefix:
            raise ConfigError("hosting.mode=url_prefix 时必须填写 hosting.url_prefix")
        if not cfg.hosting.local_root:
            raise ConfigError("hosting.mode=url_prefix 时必须填写 hosting.local_root")

    user_providers = raw.get("providers") or {}
    for unknown in set(user_providers) - set(PROVIDER_DEFAULTS):
        raise ConfigError(f"providers 下有未知项：{unknown}")

    for name in PROVIDER_ORDER:
        defaults = PROVIDER_DEFAULTS[name]
        override = user_providers.get(name) or {}
        merged = _merge(defaults, override)

        quota_raw = merged.get("quota") or {}
        quota = QuotaConfig(
            mode=str(quota_raw.get("mode", "monthly")),
            hours=_as_float(quota_raw.get("hours", 0), f"providers.{name}.quota.hours"),
            expires_at=quota_raw.get("expires_at") or None,
        )
        if quota.mode not in ("monthly", "daily", "fixed"):
            raise ConfigError(f"providers.{name}.quota.mode 只能是 monthly / daily / fixed")

        limits_raw = merged.get("limits") or {}
        limits = LimitsConfig(
            max_file_seconds=_as_float(limits_raw.get("max_file_seconds", 5 * 3600), f"providers.{name}.limits.max_file_seconds"),
            max_file_mb=_as_float(limits_raw.get("max_file_mb", 512), f"providers.{name}.limits.max_file_mb"),
            qps=max(0.01, _as_float(limits_raw.get("qps", 1), f"providers.{name}.limits.qps")),
        )

        cfg.providers[name] = ProviderConfig(
            name=name,
            display=str(merged.get("display", name)),
            enabled=bool(merged.get("enabled", True)),
            priority=int(merged.get("priority", 10)),
            weight=_as_float(merged.get("weight", 1.0), f"providers.{name}.weight"),
            experimental=bool(merged.get("experimental", False)),
            requires_url=bool(merged.get("requires_url", False)),
            credentials=merged.get("credentials") or {},
            quota=quota,
            limits=limits,
            options=merged.get("options") or {},
            env_credentials=merged.get("env_credentials") or [],
        )

    cfg.mock = raw.get("mock") or {}
    return cfg


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(os.path.expanduser(str(value)))
    return path if path.is_absolute() else (base_dir / path)


def resolve_output_dir(cfg: AppConfig, override: str | None) -> Path:
    raw = override or cfg.output.out_dir
    path = _resolve_path(cfg.path.parent, raw)
    path.mkdir(parents=True, exist_ok=True)
    return path
