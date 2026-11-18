"""阿里云百炼 Paraformer —— 录音文件识别（非实时）。

文档：https://help.aliyun.com/zh/model-studio/paraformer-recorded-speech-recognition-restful-api
- 强制异步：请求头 X-DashScope-Async: enable，返回 output.task_id
- 轮询 GET /api/v1/tasks/{task_id}
- input.file_urls 只支持 1 个公网 URL
- 结果在 output.results[].transcription_url（有效期 24 小时），下载后是 transcripts[].sentences[]
- 单文件 ≤12 小时、≤2GB

百炼的模型名可配置：paraformer-v2 / paraformer-1 / fun-asr（不同模型免费额度不同，额度以控制台为准）。
"""

from __future__ import annotations

import time

from ..errors import AuthError, FatalProviderError, QuotaExhausted, TransientError
from ..utils import http_get_json, http_post_json
from .base import Provider, Segment, Utterance, clean_utterances

DEFAULT_HOST = "https://dashscope.aliyuncs.com"
SUBMIT_PATH = "/api/v1/services/audio/asr/transcription"
TASK_PATH = "/api/v1/tasks/{task_id}"


class BailianProvider(Provider):
    name = "bailian"
    display = "阿里云百炼 Paraformer"
    requires_url = True
    CREDENTIAL_FIELDS = ("api_key",)

    def _host(self) -> str:
        workspace = str(self.option("workspace_id", "") or "").strip()
        if workspace:
            return f"https://{workspace}.cn-beijing.maas.aliyuncs.com"
        return str(self.option("host", DEFAULT_HOST)).rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.credential('api_key')}",
            "X-DashScope-Async": "enable",
        }

    @staticmethod
    def _raise_for_error(data: dict, display: str) -> None:
        code = str(data.get("code") or "")
        message = str(data.get("message") or "")
        if not code and not message:
            return
        text = f"{code} {message}".strip()
        if code in ("InvalidApiKey", "Unauthorized", "InvalidAuthentication"):
            raise AuthError(f"{display} 鉴权失败：{text}")
        if code.startswith("Throttling") or code in ("ServiceUnavailable",):
            raise TransientError(f"{display} 暂时不可用：{text}")
        if "FreeTier" in code or "AllocationQuota" in code or any(key in text for key in ("额度", "欠费", "余额", "免费额度")):
            raise QuotaExhausted(f"{display} 额度不足：{text}")
        raise FatalProviderError(f"{display} 接口报错：{text}")

    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> list[Utterance]:
        payload = {
            "model": str(self.option("model", "paraformer-v2")),
            "input": {"file_urls": [self.url_for(seg)]},
            "parameters": {"channel_id": [0]},
        }
        self.pace()
        resp = http_post_json(
            f"{self._host()}{SUBMIT_PATH}",
            payload,
            headers=self._headers(),
            timeout=self.ctx.http_timeout,
        )
        try:
            data = resp.json()
        except Exception:
            if resp.status >= 500:
                raise TransientError(f"{self.display} 返回 HTTP {resp.status}：{resp.text[:200]}")
            raise FatalProviderError(f"{self.display} 返回非 JSON（HTTP {resp.status}）：{resp.text[:200]}")

        if resp.status == 401:
            raise AuthError(f"{self.display} 鉴权失败：{resp.text[:200]}")
        if resp.status == 429:
            raise TransientError(f"{self.display} 触发限流：{resp.text[:200]}")
        self._raise_for_error(data, self.display)

        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise FatalProviderError(f"{self.display} 未返回 task_id：{data}")

        deadline = time.monotonic() + self.ctx.poll_timeout
        while True:
            self.pace()
            poll = http_get_json(
                f"{self._host()}{TASK_PATH.format(task_id=task_id)}",
                headers={"Authorization": f"Bearer {self.credential('api_key')}"},
                timeout=self.ctx.http_timeout,
            )
            self._raise_for_error(poll, self.display)
            output = poll.get("output") or {}
            status = str(output.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                return self._fetch_result(output)
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise FatalProviderError(f"{self.display} 任务失败：{output.get('message') or status}")
            if time.monotonic() > deadline:
                raise TransientError(f"{self.display} 轮询超时（task_id={task_id}）")
            self.ctx.sleep(self.ctx.poll_interval)

    def _fetch_result(self, output: dict) -> list[Utterance]:
        results = output.get("results") or []
        if not results:
            raise FatalProviderError(f"{self.display} 任务成功但没有 results")
        first = results[0]
        if str(first.get("subtask_status") or "").upper() == "FAILED":
            raise FatalProviderError(f"{self.display} 子任务失败：{first.get('message') or ''}")
        url = first.get("transcription_url")
        if not url:
            raise FatalProviderError(f"{self.display} 没有 transcription_url")
        payload = http_get_json(url, timeout=self.ctx.http_timeout)

        rows: list[Utterance] = []
        for transcript in payload.get("transcripts") or []:
            for item in transcript.get("sentences") or []:
                rows.append(
                    Utterance(
                        start_ms=int(item.get("begin_time") or 0),
                        end_ms=int(item.get("end_time") or 0),
                        text=str(item.get("text") or ""),
                        speaker=item.get("speaker_id"),
                    )
                )
        return clean_utterances(rows)
