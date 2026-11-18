"""阿里云智能语音交互 ISI —— 录音文件识别（filetrans）。

文档：https://help.aliyun.com/zh/isi/user-guide/api-reference-2
- 接口形态：RPC 风格 POP API，AccessKey 签名（SignatureVersion 1.0 / HMAC-SHA1）
- Version：2018-08-17；SubmitTask 用 POST（Task 参数是 JSON 字符串），GetTaskResult 用 GET
- 只接受公网可下载的 URL，不支持本地上传
- 单文件 ≤12 小时；音频 ≤512MB、视频 ≤2GB
- 返回 Result.Sentences[]：BeginTime / EndTime / Text / ChannelId
- 同一 TaskId 查询限 1 QPS，所以轮询间隔至少 1s

注意：这是阿里云 ISI 产品线自己的额度池，与百炼、通义听悟互不相通，账本里单独记账。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

from ..errors import AuthError, FatalProviderError, QuotaExhausted, TransientError
from ..utils import http_request, utc_iso, new_uuid
from .base import Provider, Segment, Utterance, clean_utterances

STATUS_SUCCESS = 21050000
STATUS_RUNNING = 21050001
STATUS_QUEUEING = 21050002

VERSION = "2018-08-17"


def _pe(value) -> str:
    return urllib.parse.quote(str(value), safe="~")


def sign_params(params: dict, secret: str, method: str) -> str:
    canonical = "&".join(f"{_pe(key)}={_pe(params[key])}" for key in sorted(params))
    string_to_sign = f"{method}&%2F&{_pe(canonical)}"
    digest = hmac.new((secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class AliyunIsiProvider(Provider):
    name = "aliyun_isi"
    display = "阿里云 ISI（录音文件识别）"
    requires_url = True
    CREDENTIAL_FIELDS = ("access_key_id", "access_key_secret", "appkey")

    def _endpoint(self) -> str:
        region = str(self.option("region", "cn-shanghai"))
        return f"https://filetrans.{region}.aliyuncs.com/"

    def _call(self, action: str, extra: dict | None = None) -> dict:
        access_key_id = self.credential("access_key_id")
        access_key_secret = self.credential("access_key_secret")
        method = "POST" if action == "SubmitTask" else "GET"

        params = {
            "Action": action,
            "Version": VERSION,
            "Format": "JSON",
            "AccessKeyId": access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": new_uuid(),
            "Timestamp": utc_iso(),
            "RegionId": str(self.option("region", "cn-shanghai")),
        }
        params.update(extra or {})
        params["Signature"] = sign_params(params, access_key_secret, method)

        self.pace()
        endpoint = self._endpoint()
        if method == "POST":
            body = urllib.parse.urlencode(params).encode("utf-8")
            resp = http_request(
                "POST",
                endpoint,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=body,
                timeout=self.ctx.http_timeout,
            )
        else:
            query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            resp = http_request("GET", f"{endpoint}?{query}", timeout=self.ctx.http_timeout)

        try:
            data = resp.json()
        except Exception:
            if resp.status >= 500:
                raise TransientError(f"{self.display} 返回 HTTP {resp.status}：{resp.text[:200]}")
            raise FatalProviderError(f"{self.display} 返回非 JSON（HTTP {resp.status}）：{resp.text[:200]}")

        code = str(data.get("Code") or "")
        if code:
            message = str(data.get("Message") or "")
            text = f"{code} {message}".strip()
            if code.startswith("InvalidAccessKeyId") or code.startswith("Signature") or code.startswith("Forbidden"):
                raise AuthError(f"{self.display} 鉴权失败：{text}")
            if code.startswith("Throttling"):
                raise TransientError(f"{self.display} 触发限流：{text}")
            if any(key in text for key in ("欠费", "余额", "额度", "试用")):
                raise QuotaExhausted(f"{self.display} 额度不足：{text}")
            raise FatalProviderError(f"{self.display} 接口报错：{text}")
        return data

    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> list[Utterance]:
        task = {
            "appkey": self.credential("appkey"),
            "file_link": self.url_for(seg),
            "version": str(self.option("version", "4.0")),
            "enable_words": bool(self.option("enable_words", False)),
            "enable_sample_rate_adaptive": True,
            "enable_punctuation_prediction": True,
        }
        created = self._call("SubmitTask", {"Task": json.dumps(task, ensure_ascii=False)})
        task_id = created.get("TaskId")
        if not task_id:
            raise FatalProviderError(f"{self.display} 提交后未返回 TaskId：{created}")

        deadline = time.monotonic() + self.ctx.poll_timeout
        while True:
            result = self._call("GetTaskResult", {"TaskId": task_id})
            status_code = result.get("StatusCode")
            if status_code == STATUS_SUCCESS:
                return self._parse(result)
            if status_code not in (STATUS_RUNNING, STATUS_QUEUEING):
                status_text = str(result.get("StatusText") or "")
                if any(key in status_text for key in ("欠费", "额度")):
                    raise QuotaExhausted(f"{self.display} 额度不足：{status_text}")
                raise FatalProviderError(f"{self.display} 任务失败：{status_code} {status_text}")
            if time.monotonic() > deadline:
                raise TransientError(f"{self.display} 轮询超时（TaskId={task_id}）")
            self.ctx.sleep(max(self.ctx.poll_interval, 1.0))

    def _parse(self, data: dict) -> list[Utterance]:
        rows: list[Utterance] = []
        for item in ((data.get("Result") or {}).get("Sentences") or []):
            rows.append(
                Utterance(
                    start_ms=int(item.get("BeginTime") or 0),
                    end_ms=int(item.get("EndTime") or 0),
                    text=str(item.get("Text") or ""),
                    speaker=item.get("ChannelId"),
                )
            )
        return clean_utterances(rows)
