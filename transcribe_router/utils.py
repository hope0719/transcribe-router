"""通用工具：日志、时间格式化、零依赖 HTTP 客户端。

刻意只用标准库（urllib），这样脚本 clone 下来就能跑，不需要 pip install。
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import TransportError

try:  # POSIX 文件锁；Windows 下退化为进程内锁（本项目只承诺 macOS/Linux）
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def setup_logger(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    logger = logging.getLogger("transcribe_router")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO))
    logger.propagate = False
    return logger


def fmt_hours(hours: float) -> str:
    """把小时数格式化成人类可读的字符串。"""
    if hours <= 0:
        return "0 小时"
    if hours < 1 / 60:
        seconds = hours * 3600
        return f"{seconds:.1f} 秒" if seconds < 10 else f"{seconds:.0f} 秒"
    if hours < 1:
        return f"{hours * 60:.0f} 分钟"
    return f"{hours:.2f} 小时"


def fmt_ts(ts: float | None, tz=None) -> str:
    if not ts:
        return "无"
    dt = datetime.fromtimestamp(ts, tz=tz or datetime.now().astimezone().tzinfo)
    return dt.strftime("%Y-%m-%d %H:%M")


def now_ts() -> float:
    return time.time()


def utc_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time()).astimezone()
    return dt.isoformat(timespec="seconds")


def new_uuid() -> str:
    return str(uuid.uuid4())


@contextmanager
def file_lock(path, *, exclusive: bool = True, blocking: bool = True):
    """跨进程文件锁。用于账本写入与 job 独占，避免多进程互相覆盖。

    yield 一个布尔值表示是否拿到锁；blocking=False 且锁被占用时产出 False，不抛异常。
    """
    if fcntl is None:  # pragma: no cover - Windows 兜底
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        flags |= fcntl.LOCK_NB
    acquired = True
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

DEFAULT_TIMEOUT = 30


class Response:
    def __init__(self, status: int, body: bytes, url: str):
        self.status = status
        self.body = body
        self.url = url

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError(f"响应不是合法 JSON（HTTP {self.status}）：{self.text[:300]}") from exc


def http_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Response:
    """发一个 HTTP 请求。4xx/5xx 不抛异常，交给调用方按各家协议解析错误码。"""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), url)
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read(), url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"请求失败 {url}: {exc}") from exc


def http_get(url: str, *, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> Response:
    return http_request("GET", url, headers=headers, timeout=timeout)


def http_get_json(url: str, *, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
    return http_get(url, headers=headers, timeout=timeout).json()


def http_post_json(
    url: str,
    payload,
    *,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Response:
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return http_request("POST", url, headers=hdrs, body=body, timeout=timeout)


def http_post_form(
    url: str,
    fields: dict,
    *,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Response:
    hdrs = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    hdrs.update(headers or {})
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return http_request("POST", url, headers=hdrs, body=body, timeout=timeout)


def http_get_with_params(url: str, params: dict, *, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> Response:
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    sep = "&" if "?" in url else "?"
    return http_get(f"{url}{sep}{query}", headers=headers, timeout=timeout)


def http_post_multipart(
    url: str,
    fields: dict,
    *,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str = "application/octet-stream",
    headers: dict | None = None,
    timeout: int = 120,
) -> Response:
    boundary = f"----TranscribeRouter{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    hdrs.update(headers or {})
    return http_request("POST", url, headers=hdrs, body=body, timeout=timeout)
