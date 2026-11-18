"""通义听悟 API —— 离线转写任务。

文档：
- CreateTask：https://help.aliyun.com/zh/tingwu/api-tingwu-2023-09-30-dir-other/
- GetTaskInfo：https://help.aliyun.com/zh/tingwu/api-tingwu-2023-09-30-gettaskinfo
- 请求语法：PUT /openapi/tingwu/v2/tasks，GET /openapi/tingwu/v2/tasks/{TaskId}
- 离线任务必须设置 Input.SourceLanguage 与 Input.FileUrl
- 结果以 URL 形式返回：Data.Result.Transcription

!! 未验证 !!
官方页面明确写了“自签名细节非常复杂，建议在专家指导下对接”，没有给出完整签名示例。
本实现按阿里云 OpenAPI V3（ACS3-HMAC-SHA256）通用签名规则构造请求：
    Authorization: ACS3-HMAC-SHA256 Credential=<AK>,SignedHeaders=...,Signature=...
如果听悟实际使用的是另一套签名（例如 acs v1），本 provider 会报 4xx，路由层会自动降级到下一家。
另外，转写结果 JSON 的内部字段（Paragraphs[].Words[] 的 Text/Start/End）也未能从官方页面核对，
代码里同时兼容了 sentences[].begin_time/end_time 的写法。

TODO(未验证)：接入前请用官方 SDK / OpenAPI Explorer 发一次请求，核对签名与结果字段，再启用本家。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

from ..errors import AuthError, FatalProviderError, QuotaExhausted, TransientError
from ..utils import http_get_json, http_request, new_uuid
from .base import Provider, Segment, Utterance, clean_utterances

HOST = "tingwu.cn-beijing.aliyuncs.com"
API_VERSION = "2023-09-30"
TASKS_PATH = "/openapi/tingwu/v2/tasks"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TingwuProvider(Provider):
    name = "tingwu"
    display = "通义听悟 API"
    requires_url = True
    experimental = True
    CREDENTIAL_FIELDS = ("access_key_id", "access_key_secret", "app_key")

    # ------------------------------------------------------------------ #
    def _headers(self, method: str, path: str, query: str, body: bytes) -> dict:
        access_key_id = self.credential("access_key_id")
        access_key_secret = self.credential("access_key_secret")

        date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        content_type = "application/json"
        hashed_payload = _sha256_hex(body)

        canonical_headers = {
            "host": HOST,
            "x-acs-version": API_VERSION,
            "x-acs-date": date,
            "x-acs-content-sha256": hashed_payload,
        }
        if body:
            canonical_headers["content-type"] = content_type

        signed_headers = ";".join(sorted(canonical_headers))
        header_block = "".join(f"{key}:{canonical_headers[key]}\n" for key in sorted(canonical_headers))

        canonical_request = "\n".join(
            [method, path, query, header_block, signed_headers, hashed_payload]
        )
        string_to_sign = f"ACS3-HMAC-SHA256\n{_sha256_hex(canonical_request.encode('utf-8'))}"
        signature = hmac.new(
            access_key_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        headers = {
            "Authorization": (
                f"ACS3-HMAC-SHA256 Credential={access_key_id},"
                f"SignedHeaders={signed_headers},Signature={signature}"
            ),
            "x-acs-version": API_VERSION,
            "x-acs-date": date,
            "x-acs-content-sha256": hashed_payload,
            "Host": HOST,
        }
        if body:
            headers["Content-Type"] = content_type
        return headers

    def _request(self, method: str, path: str, query: str = "", payload: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload else b""
        url = f"https://{HOST}{path}" + (f"?{query}" if query else "")
        self.pace()
        resp = http_request(
            method,
            url,
            headers=self._headers(method, path, query, body),
            body=body or None,
            timeout=self.ctx.http_timeout,
        )
        try:
            data = resp.json()
        except Exception:
            if resp.status >= 500:
                raise TransientError(f"{self.display} 返回 HTTP {resp.status}：{resp.text[:200]}")
            raise FatalProviderError(f"{self.display} 返回非 JSON（HTTP {resp.status}）：{resp.text[:200]}")

        code = str(data.get("Code") or "")
        message = str(data.get("Message") or "")
        if code in ("0", ""):
            return data
        text = f"{code} {message}".strip()
        if resp.status in (401, 403) or "Signature" in text or "AccessKey" in text:
            raise AuthError(f"{self.display} 鉴权/签名失败：{text}")
        if resp.status == 429 or "Throttling" in text:
            raise TransientError(f"{self.display} 触发限流：{text}")
        if any(key in text for key in ("额度", "欠费", "余额", "试用")):
            raise QuotaExhausted(f"{self.display} 额度不足：{text}")
        raise FatalProviderError(f"{self.display} 接口报错：{text}")

    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> list[Utterance]:
        payload = {
            "AppKey": self.credential("app_key"),
            "Input": {
                "SourceLanguage": str(self.option("source_language", "cn")),
                "FileUrl": self.url_for(seg),
                "TaskKey": f"tr-{seg.index}-{int(time.time())}",
            },
            "Parameters": {
                "Transcription": {"DiarizationEnabled": bool(self.option("diarization", True))}
            },
        }
        created = self._request("PUT", TASKS_PATH, query="type=offline", payload=payload)
        task_id = (created.get("Data") or {}).get("TaskId")
        if not task_id:
            raise FatalProviderError(f"{self.display} 未返回 TaskId：{created}")

        deadline = time.monotonic() + self.ctx.poll_timeout
        while True:
            info = self._request("GET", f"{TASKS_PATH}/{task_id}")
            data = info.get("Data") or {}
            status = str(data.get("TaskStatus") or "").upper()
            if status == "COMPLETED":
                url = (data.get("Result") or {}).get("Transcription")
                if not url:
                    raise FatalProviderError(f"{self.display} 任务完成但没有 Transcription 结果")
                return self._parse(http_get_json(url, timeout=self.ctx.http_timeout))
            if status in ("FAILED", "INVALID"):
                raise FatalProviderError(
                    f"{self.display} 任务失败：{data.get('ErrorCode')} {data.get('ErrorMessage')}"
                )
            if time.monotonic() > deadline:
                raise TransientError(f"{self.display} 轮询超时（TaskId={task_id}）")
            self.ctx.sleep(self.ctx.poll_interval)

    # ------------------------------------------------------------------ #
    def _parse(self, payload: dict) -> list[Utterance]:
        transcription = payload.get("Transcription") or payload
        rows: list[Utterance] = []

        # 形式 A：paragraphs[].words[]（听悟常见结构）
        for paragraph in transcription.get("Paragraphs") or []:
            speaker = paragraph.get("SpeakerId")
            buffer: list[str] = []
            start = end = None
            for word in paragraph.get("Words") or []:
                text = str(word.get("Text") or "")
                if not text:
                    continue
                if start is None:
                    start = int(word.get("Start") or 0)
                end = int(word.get("End") or 0)
                buffer.append(text)
                if text[-1:] in "。！？!?；;，,":
                    rows.append(Utterance(start_ms=start, end_ms=end, text="".join(buffer), speaker=speaker))
                    buffer, start = [], None
            if buffer and start is not None:
                rows.append(Utterance(start_ms=start, end_ms=end or start, text="".join(buffer), speaker=speaker))

        # 形式 B：sentences[]（部分版本/回调格式）
        if not rows:
            for item in transcription.get("Sentences") or transcription.get("sentences") or []:
                rows.append(
                    Utterance(
                        start_ms=int(item.get("BeginTime") or item.get("begin_time") or 0),
                        end_ms=int(item.get("EndTime") or item.get("end_time") or 0),
                        text=str(item.get("Text") or item.get("text") or ""),
                        speaker=item.get("SpeakerId") or item.get("speaker_id"),
                    )
                )
        return clean_utterances(rows)
