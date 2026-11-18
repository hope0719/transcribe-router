"""网易见外 / 有道智云 —— 长语音转写。

文档：https://ai.youdao.com/DOCSIRMA/html/tts/api/cyyzx/index.html
（“网易见外”使用的是有道智云的长语音转写 API，接口域名 openapi.youdao.com）

流程：prepare -> upload（分片）-> merge -> get_progress（status=9）-> get_result
签名：sign = sha256(appKey + salt + curtime + appSecret)，signType=v4，salt 用 UUID
- 本地文件分片上传，不要求公网 URL
- 单文件 ≤500MB；中文（zh-CHS2）≤4 小时；结果仅保留 7 天
- 返回 result[]：sentence + word_timestamps / word_timestamps_eds（毫秒）

报告里把它排在最后：免费 300 分钟/30 天，超额 0.4 元/分钟（约 24 元/小时）。
"""

from __future__ import annotations

import hashlib
import math
import time

from ..errors import AuthError, FatalProviderError, FileTooLarge, QuotaExhausted, TransientError
from ..utils import http_post_form, http_post_multipart, new_uuid
from .base import Provider, Segment, Utterance, clean_utterances

BASE = "https://openapi.youdao.com"

AUTH_CODES = {"108", "110", "111", "112", "205", "207"}
QUOTA_CODES = {"401"}
TRANSIENT_CODES = {"303", "411", "412"}
FILE_CODES = {"13", "4000000", "4000001", "4000007", "4000008"}


class NeteaseProvider(Provider):
    name = "netease"
    display = "网易见外（有道长语音转写）"
    CREDENTIAL_FIELDS = ("app_key", "app_secret")

    # ------------------------------------------------------------------ #
    def _auth_fields(self) -> dict:
        app_key = self.credential("app_key")
        salt = new_uuid()
        curtime = str(int(time.time()))
        sign = hashlib.sha256(
            (app_key + salt + curtime + self.credential("app_secret")).encode("utf-8")
        ).hexdigest()
        return {
            "appKey": app_key,
            "salt": salt,
            "curtime": curtime,
            "sign": sign,
            "signType": "v4",
        }

    @staticmethod
    def _raise_for_code(code, message: str, display: str) -> None:
        code = str(code)
        if code in ("0", "None", ""):
            return
        text = f"{code} {message}".strip()
        if code in AUTH_CODES:
            raise AuthError(f"{display} 鉴权失败：{text}")
        if code in QUOTA_CODES:
            raise QuotaExhausted(f"{display} 账户欠费/额度不足：{text}")
        if code in FILE_CODES:
            raise FileTooLarge(f"{display} 文件不满足要求：{text}")
        if code in TRANSIENT_CODES:
            raise TransientError(f"{display} 暂时不可用：{text}")
        raise FatalProviderError(f"{display} 接口报错：{text}")

    def _call(self, path: str, fields: dict) -> object:
        self.pace()
        resp = http_post_form(f"{BASE}{path}", fields, timeout=self.ctx.http_timeout)
        try:
            data = resp.json()
        except Exception:
            if resp.status >= 500:
                raise TransientError(f"{self.display} 返回 HTTP {resp.status}：{resp.text[:200]}")
            raise FatalProviderError(f"{self.display} 返回非 JSON（HTTP {resp.status}）：{resp.text[:200]}")
        self._raise_for_code(data.get("errorCode"), str(data.get("msg") or ""), self.display)
        return data.get("result")

    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> list[Utterance]:
        size = seg.path.stat().st_size
        slice_mb = max(1, int(self.option("slice_mb", 10)))
        slice_bytes = slice_mb * 1024 * 1024
        slice_num = max(1, math.ceil(size / slice_bytes))

        fields = self._auth_fields()
        fields.update(
            {
                "type": "1",
                "name": seg.path.stem,
                "fileSize": str(size),
                "sliceNum": str(slice_num),
                "langType": str(self.option("lang_type", "zh-CHS2")),
                "format": seg.suffix,
                "noitn": "0",
                "needSpeakerId": "1" if self.option("need_speaker_id", False) else "0",
                "speakerNum": "0",
            }
        )
        task_id = self._call("/api/audio/prepare", fields)
        if not task_id:
            raise FatalProviderError(f"{self.display} prepare 未返回任务 ID")

        with seg.path.open("rb") as handle:
            for index in range(1, slice_num + 1):
                chunk = handle.read(slice_bytes)
                if not chunk:
                    break
                upload_fields = self._auth_fields()
                upload_fields.update({"q": str(task_id), "sliceId": str(index), "type": "1"})
                self.pace()
                resp = http_post_multipart(
                    f"{BASE}/api/audio/upload",
                    upload_fields,
                    file_field="file",
                    filename=seg.path.name,
                    file_bytes=chunk,
                    timeout=max(self.ctx.http_timeout, 120),
                )
                data = resp.json()
                self._raise_for_code(data.get("errorCode"), str(data.get("msg") or ""), self.display)

        merge_fields = self._auth_fields()
        merge_fields["q"] = str(task_id)
        self._call("/api/audio/merge", merge_fields)

        deadline = time.monotonic() + self.ctx.poll_timeout
        while True:
            progress_fields = self._auth_fields()
            progress_fields["q"] = str(task_id)
            progress = self._call("/api/audio/get_progress", progress_fields) or []
            status = ""
            if isinstance(progress, list) and progress:
                status = str((progress[0] or {}).get("status") or "")
            elif isinstance(progress, dict):
                status = str(progress.get("status") or "")
            if status == "9":
                break
            if status in ("6", "12", "13", "21"):
                raise FatalProviderError(f"{self.display} 转写失败 status={status}")
            if time.monotonic() > deadline:
                raise TransientError(f"{self.display} 轮询超时（task={task_id}）")
            self.ctx.sleep(max(self.ctx.poll_interval, 1.0))

        result_fields = self._auth_fields()
        result_fields["q"] = str(task_id)
        rows = self._call("/api/audio/get_result", result_fields) or []
        return self._parse(rows)

    def _parse(self, rows) -> list[Utterance]:
        utterances: list[Utterance] = []
        if not isinstance(rows, list):
            return utterances
        for item in rows:
            if not isinstance(item, dict):
                continue
            text = str(item.get("sentence") or "").strip()
            if not text:
                continue
            starts = item.get("word_timestamps") or []
            ends = item.get("word_timestamps_eds") or []
            start = int(min(starts)) if starts else 0
            end = int(max(ends)) if ends else start
            utterances.append(Utterance(start_ms=start, end_ms=end, text=text, speaker=item.get("speaker")))
        return clean_utterances(utterances)
