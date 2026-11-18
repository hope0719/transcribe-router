"""火山引擎豆包 —— 录音文件识别（标准版，AUC）。

文档：https://www.volcengine.com/docs/6561/80820
- 提交：POST https://openspeech.bytedance.com/api/v1/auc/submit
- 查询：POST https://openspeech.bytedance.com/api/v1/auc/query
- 鉴权信息放在请求体 app 节点：appid / token / cluster（cluster 是控制台的 Cluster ID）
- 只接受可下载的音频 URL；<5 小时、≤512MB
- 返回 resp.utterances[]，含 start_time / end_time（毫秒）

code=1000 为成功；其余为失败。
TODO(未验证)：官方文档只写「超出设定阈值报错」，未给出固定 QPS/并发数值，
      limits.qps 默认 1，请按你账号实测情况调整。
"""

from __future__ import annotations

import time

from ..errors import AuthError, FatalProviderError, QuotaExhausted, TransientError
from ..utils import http_post_json
from .base import Provider, Segment, Utterance, clean_utterances

SUBMIT_URL = "https://openspeech.bytedance.com/api/v1/auc/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v1/auc/query"


class VolcanoProvider(Provider):
    name = "volcano"
    display = "火山引擎豆包（录音文件识别）"
    requires_url = True
    CREDENTIAL_FIELDS = ("appid", "token", "cluster")

    def _app(self) -> dict:
        return {
            "appid": self.credential("appid"),
            "token": self.credential("token"),
            "cluster": self.credential("cluster"),
        }

    @staticmethod
    def _raise_for_code(code, message: str, display: str) -> None:
        text = f"{code} {message}".strip()
        if str(code) == "1000":
            return
        if str(code) == "1002":
            raise AuthError(f"{display} 鉴权失败：{text}")
        if str(code) == "1001":
            raise FatalProviderError(f"{display} 请求参数无效：{text}")
        if any(key in text for key in ("额度", "欠费", "余额", "quota", "Quota")):
            raise QuotaExhausted(f"{display} 额度不足：{text}")
        # 其余（含限流、服务内部错误）先按瞬时错误重试，重试完由路由层降级
        raise TransientError(f"{display} 接口报错：{text}")

    # ------------------------------------------------------------------ #
    def transcribe(self, seg: Segment) -> list[Utterance]:
        submit_payload = {
            "app": self._app(),
            "user": {"uid": str(self.option("uid", "transcribe-router"))},
            "audio": {"format": seg.suffix, "url": self.url_for(seg)},
            "additions": {
                "use_itn": "True",
                "with_speaker_info": "True" if self.option("with_speaker_info", True) else "False",
            },
        }
        self.pace()
        resp = http_post_json(SUBMIT_URL, submit_payload, timeout=self.ctx.http_timeout)
        try:
            data = resp.json()
        except Exception:
            if resp.status >= 500:
                raise TransientError(f"{self.display} 返回 HTTP {resp.status}：{resp.text[:200]}")
            raise FatalProviderError(f"{self.display} 返回非 JSON（HTTP {resp.status}）：{resp.text[:200]}")

        body = data.get("resp") or {}
        self._raise_for_code(body.get("code"), str(body.get("message") or ""), self.display)
        task_id = body.get("id")
        if not task_id:
            raise FatalProviderError(f"{self.display} 提交后未返回任务 id：{data}")

        deadline = time.monotonic() + self.ctx.poll_timeout
        query_payload = dict(self._app(), id=task_id)
        while True:
            self.pace()
            resp = http_post_json(QUERY_URL, query_payload, timeout=self.ctx.http_timeout)
            data = resp.json()
            body = data.get("resp") or {}
            code = body.get("code")
            if str(code) != "1000":
                self._raise_for_code(code, str(body.get("message") or ""), self.display)
            utterances = body.get("utterances")
            if utterances is not None:
                return self._parse(utterances, body.get("text"))
            if time.monotonic() > deadline:
                raise TransientError(f"{self.display} 轮询超时（id={task_id}）")
            self.ctx.sleep(self.ctx.poll_interval)

    def _parse(self, utterances: list, text: str | None) -> list[Utterance]:
        rows: list[Utterance] = []
        for item in utterances:
            speaker = (item.get("additions") or {}).get("speaker")
            rows.append(
                Utterance(
                    start_ms=int(item.get("start_time") or 0),
                    end_ms=int(item.get("end_time") or 0),
                    text=str(item.get("text") or ""),
                    speaker=speaker,
                )
            )
        if not rows and text:
            rows.append(Utterance(start_ms=0, end_ms=0, text=str(text)))
        return clean_utterances(rows)
