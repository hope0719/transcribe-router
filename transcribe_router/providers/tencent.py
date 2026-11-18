"""腾讯云 ASR —— 录音文件识别。

文档：https://cloud.tencent.com/document/product/1093/37823 （CreateRecTask）
      https://cloud.tencent.com/document/product/1093/37822 （DescribeTaskStatus）
签名：TC3-HMAC-SHA256（公共参数 v3）

要点
----
- 异步：CreateRecTask 拿 TaskId，轮询 DescribeTaskStatus 直到 Status=2；
- ResTextFormat=3 才会返回按标点分句的 ResultDetail（句级时间戳，适合做字幕）；
- 音频 URL ≤5 小时 / 1GB；本地直传（base64）≤5MB，且会走 SourceType=1。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from ..errors import AuthError, FatalProviderError, FileTooLarge, QuotaExhausted, TransientError
from ..utils import http_request
from .base import Provider, Segment, Utterance, clean_utterances

HOST = "asr.tencentcloudapi.com"
SERVICE = "asr"
VERSION = "2019-06-14"


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class TencentProvider(Provider):
    name = "tencent"
    display = "腾讯云 ASR（录音文件识别）"
    CREDENTIAL_FIELDS = ("secret_id", "secret_key")

    # ------------------------------------------------------------------ #
    def precheck(self, seg: Segment) -> str | None:
        if not seg.url and seg.size_mb > 5:
            return "腾讯云本地直传仅支持 ≤5MB；未配置 hosting.url_prefix 时无法提交更大的文件"
        return None

    def _sign_headers(self, action: str, payload: str) -> dict:
        secret_id = self.credential("secret_id")
        secret_key = self.credential("secret_key")
        region = str(self.option("region", "ap-shanghai"))

        timestamp = int(time.time())
        date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
        content_type = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{content_type}\nhost:{HOST}\n"
        signed_headers = "content-type;host"
        hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
        )
        scope = f"{date}/{SERVICE}/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        key = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
        key = _hmac_sha256(key, SERVICE)
        key = _hmac_sha256(key, "tc3_request")
        signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        return {
            "Authorization": (
                f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "Content-Type": content_type,
            "Host": HOST,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": VERSION,
            "X-TC-Region": region,
        }

    def _call(self, action: str, payload: dict) -> dict:
        self.pace()
        # 注意：签名必须针对最终发送的字节串，序列化方式一旦改动签名就会失效
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        resp = http_request(
            "POST",
            f"https://{HOST}",
            headers=self._sign_headers(action, body),
            body=body.encode("utf-8"),
            timeout=self.ctx.http_timeout,
        )
        try:
            data = resp.json()
        except Exception:
            if resp.status >= 500:
                raise TransientError(f"{self.display} 返回 HTTP {resp.status}：{resp.text[:200]}")
            raise FatalProviderError(f"{self.display} 返回非 JSON（HTTP {resp.status}）：{resp.text[:200]}")

        response = data.get("Response") or {}
        error = response.get("Error")
        if error:
            code = str(error.get("Code", ""))
            message = str(error.get("Message", ""))
            text = f"{code} {message}".strip()
            if code.startswith("AuthFailure") or code in ("UnauthorizedOperation", "FailedOperation.UserNotRegistered"):
                raise AuthError(f"{self.display} 鉴权失败：{text}")
            if code in ("RequestLimitExceeded", "InternalError", "FailedOperation.ServiceIsolate"):
                raise TransientError(f"{self.display} 暂时不可用：{text}")
            # TODO(未验证)：腾讯云「免费额度耗尽」没有稳定的错误码，这里按文案兜底。
            if any(key in text for key in ("额度", "欠费", "余额", "资源包", "免费")):
                raise QuotaExhausted(f"{self.display} 额度不足：{text}")
            raise FatalProviderError(f"{self.display} 接口报错：{text}")
        return response

    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> list[Utterance]:
        payload = {
            "EngineModelType": str(self.option("engine_model_type", "16k_zh")),
            "ChannelNum": 1,
            "ResTextFormat": int(self.option("res_text_format", 3)),
            "SourceType": 0,
            "SpeakerDiarization": int(self.option("speaker_diarization", 0)),
        }
        if seg.url:
            payload["Url"] = seg.url
        else:
            if seg.size_mb > 5:
                raise FileTooLarge("腾讯云本地直传仅支持 ≤5MB")
            raw = seg.path.read_bytes()
            payload["SourceType"] = 1
            payload["Data"] = base64.b64encode(raw).decode("ascii")
            payload["DataLen"] = len(raw)

        created = self._call("CreateRecTask", payload)
        task_id = (created.get("Data") or {}).get("TaskId")
        if not task_id:
            raise FatalProviderError(f"{self.display} 未返回 TaskId：{created}")

        deadline = time.monotonic() + self.ctx.poll_timeout
        while True:
            status = self._call("DescribeTaskStatus", {"TaskId": int(task_id)})
            data = status.get("Data") or {}
            state = data.get("Status")
            if state == 2:
                return self._parse(data)
            if state == 3:
                message = data.get("ErrorMsg") or "识别失败"
                if any(key in str(message) for key in ("额度", "欠费", "余额", "资源包")):
                    raise QuotaExhausted(f"{self.display} 额度不足：{message}")
                raise FatalProviderError(f"{self.display} 识别失败：{message}")
            if time.monotonic() > deadline:
                raise TransientError(f"{self.display} 轮询超时（TaskId={task_id}）")
            self.ctx.sleep(self.ctx.poll_interval)

    def _parse(self, data: dict) -> list[Utterance]:
        rows: list[Utterance] = []
        for item in data.get("ResultDetail") or []:
            text = item.get("FinalSentence") or ""
            rows.append(
                Utterance(
                    start_ms=int(item.get("StartMs") or 0),
                    end_ms=int(item.get("EndMs") or 0),
                    text=text,
                    speaker=item.get("SpeakerId"),
                )
            )
        if not rows and data.get("Result"):
            # ResTextFormat=0 时只有整段文本，没有句级时间戳
            rows.append(Utterance(start_ms=0, end_ms=0, text=str(data["Result"])))
        return clean_utterances(rows)
