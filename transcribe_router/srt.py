"""句级时间戳 -> SRT。

报告里的 7 家 API 都只返回 JSON 句级时间戳，SRT 需要自己转。
"""

from __future__ import annotations

PUNCTUATION = "，。！？；、,.!?;"


def ms_to_srt(ms: int) -> str:
    ms = max(0, int(ms))
    hours, remainder = divmod(ms, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _split_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = max(window.rfind(ch) for ch in PUNCTUATION)
        if cut < max_chars // 2:
            cut = max_chars
        else:
            cut += 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk.strip()]


def to_srt(utterances, max_chars: int = 0) -> str:
    """utterances 需带 start_ms / end_ms / text 三个属性。"""
    lines: list[str] = []
    index = 1
    for item in utterances:
        text = (item.text or "").strip()
        if not text:
            continue
        start = int(item.start_ms)
        end = max(int(item.end_ms), start + 1)
        chunks = _split_text(text, max_chars)
        if len(chunks) == 1:
            lines.append(f"{index}\n{ms_to_srt(start)} --> {ms_to_srt(end)}\n{text}\n")
            index += 1
            continue

        total_chars = sum(len(chunk) for chunk in chunks) or 1
        span = end - start
        cursor = start
        for chunk in chunks:
            if not chunk.strip():
                continue
            length = max(1, int(round(span * len(chunk) / total_chars)))
            chunk_end = min(end, cursor + length)
            lines.append(f"{index}\n{ms_to_srt(cursor)} --> {ms_to_srt(chunk_end)}\n{chunk.strip()}\n")
            index += 1
            cursor = chunk_end
    return "\n".join(lines)
