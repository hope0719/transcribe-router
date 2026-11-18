"""讯飞开放平台 —— 语音转写（Long Form ASR）。

文档：https://www.xfyun.cn/doc/asr/lfasr/API.html
流程：prepare（预处理拿 task_id）→ upload（分片上传）→ merge（合并并排队）→
      getProgress（status=9 才完成）→ getResult（取句级结果）

签名：signa = Base64(HmacSHA1(MD5(app_id + ts), secret_key))
- 本地文件分片上传，不要求公网 URL（这是它相对阿里云/火山的主要优势）
- 单文件 ≤5 小时、≤500MB；结果保留 30 天；getResult 最多调用 100 次
- 个人免费包 5 小时/年、2 并发；企业 50 小时/年（额度以控制台为准，本脚本只负责记账）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time

from ..errors import AuthError, FatalProviderError, FileTooLarge, QuotaExhausted, TransientError
from ..utils import http_post_form, http_post_multipart
from .base import Provider, Segment, Utterance, clean_utterances

BASE = "https://raasr.xfyun.cn/api"

# 错误码 -> 分类，见文档附录
AUTH_CODES = {26601}
QUOTA_CODES = {26625, 26633}
FILE_CODES = {26621, 26622, 26623, 26631, 26632, 26403}
TRANSIENT_CODES = {26603, 26605, 26000, 26500}


def make_signa(app_id: str, secret_key: str, ts: str) -> str:
    md5_hex = hashlib.md5((app_id + ts).encode("utf-8")).hexdigest()
    digest = hmac.new(secret_key.encode("utf-8"), md5_hex.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class SliceIdGenerator:
    """文档给出的 slice_id 生成器：aaaaaaaaaa -> aaaaaaaaab -> ..."""

    def __init__(self) -> None:
        self._chars = "aaaaaaaaa`"

    def next(self) -> str:
        chars = self._chars
        index = len(chars) - 1
        while index >= 0:
            current = chars[index]
            if current != "z":
                chars = chars[:index] + chr(ord(current) + 1) + chars[index + 1 :]
                break
            chars = chars[:index] + "a" + chars[index + 1 :]
            index -= 1
        self._chars = chars
        return chars


class IflytekProvider(Provider):
    name = "iflytek"
    display = "讯飞开放平台（语音转写）"
    CREDENTIAL_FIELDS = ("app_id", "secret_key")

    # ------------------------------------------------------------------ #
    def _auth_fields(self) -> dict:
        app_id = self.credential("app_id")
        ts = str(int(time.time()))
        return {"app_id": app_id, "ts": ts, "signa": make_signa(app_id, self.credential("secret_key"), ts)}

    def _call(self, path: str, fields: dict) -> object:
        self.pace()
        resp = http_post_form(f"{BASE}{path}", fields, timeout=self.ctx.http_timeout)
        try:
            data = resp.json()
        except Exception:
            if resp.status >= 500:
                raise TransientError(f"{self.display} 返回 HTTP {resp.status}：{resp.text[:200]}")
            raise FatalProviderError(f"{self.display} 返回非 JSON（HTTP {resp.status}）：{resp.text[:200]}")

        err_no = int(data.get("err_no") or 0)
        if data.get("ok") == 0 and err_no == 0:
            return data.get("data")

        message = f"{err_no} {data.get('failed') or ''}".strip()
        if err_no in AUTH_CODES:
            raise AuthError(f"{self.display} 鉴权失败：{message}")
        if err_no in QUOTA_CODES:
            raise QuotaExhausted(f"{self.display} 额度不足：{message}")
        if err_no in FILE_CODES:
            raise FileTooLarge(f"{self.display} 文件不满足要求：{message}")
        if err_no in TRANSIENT_CODES:
            raise TransientError(f"{self.display} 暂时不可用：{message}")
        raise FatalProviderError(f"{self.display} 接口报错：{message}")

    @staticmethod
    def _loads(data) -> object:
        if isinstance(data, (dict, list)):
            return data
        if not data:
            return None
        try:
            return json.loads(data)
        except ValueError as exc:
            raise FatalProviderError(f"讯飞返回的 data 不是 JSON：{str(data)[:200]}") from exc

    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> list[Utterance]:
        size = seg.path.stat().st_size
        slice_mb = max(1, int(self.option("slice_mb", 10)))
        slice_bytes = slice_mb * 1024 * 1024
        slice_num = max(1, math.ceil(size / slice_bytes))

        fields = self._auth_fields()
        fields.update(
            {
                "file_len": str(size),
                "file_name": seg.path.name,
                "slice_num": str(slice_num),
                "lfasr_type": "0",
                "language": str(self.option("language", "cn")),
                "has_seperate": "true" if self.option("has_seperate", False) else "false",
            }
        )
        task_id = self._call("/prepare", fields)
        if not task_id:
            raise FatalProviderError(f"{self.display} 预处理未返回 task_id")

        generator = SliceIdGenerator()
        with seg.path.open("rb") as handle:
            for _ in range(slice_num):
                chunk = handle.read(slice_bytes)
                if not chunk:
                    break
                upload_fields = self._auth_fields()
                upload_fields.update(
                    {
                        "task_id": str(task_id),
                        "slice_id": generator.next(),
                        "type": "1",
                    }
                )
                self.pace()
                resp = http_post_multipart(
                    f"{BASE}/upload",
                    upload_fields,
                    file_field="content",  # 文档：参数 content 为分片字节内容
                    filename=seg.path.name,
                    file_bytes=chunk,
                    timeout=max(self.ctx.http_timeout, 120),
                )
                data = resp.json()
                if data.get("ok") != 0:
                    raise FatalProviderError(
                        f"{self.display} 分片上传失败：{data.get('err_no')} {data.get('failed')}"
                    )

        merge_fields = self._auth_fields()
        merge_fields["task_id"] = str(task_id)
        self._call("/merge", merge_fields)

        deadline = time.monotonic() + self.ctx.poll_timeout
        while True:
            progress_fields = self._auth_fields()
            progress_fields["task_id"] = str(task_id)
            progress = self._loads(self._call("/getProgress", progress_fields)) or {}
            status = str(progress.get("status"))
            if status == "9":
                break
            if status in ("6", "12", "21"):
                raise FatalProviderError(f"{self.display} 转写失败 status={status}：{progress.get('desc')}")
            if time.monotonic() > deadline:
                raise TransientError(f"{self.display} 轮询超时（task_id={task_id}）")
            self.ctx.sleep(max(self.ctx.poll_interval, 1.0))

        result_fields = self._auth_fields()
        result_fields["task_id"] = str(task_id)
        rows = self._loads(self._call("/getResult", result_fields)) or []
        utterances = [
            Utterance(
                start_ms=int(float(item.get("bg") or 0)),
                end_ms=int(float(item.get("ed") or 0)),
                text=str(item.get("onebest") or ""),
                speaker=item.get("speaker"),
            )
            for item in rows
            if isinstance(item, dict)
        ]
        return clean_utterances(utterances)
