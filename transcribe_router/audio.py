"""音频探测与切段（依赖 ffmpeg / ffprobe）。

切段策略
--------
1. 目标段长取 ``min(provider 单文件上限, segmentation.max_seconds)``；
2. ``mode: silence`` 时先用 ffmpeg ``silencedetect`` 找静音区间，把切点吸附到目标点附近
   最近的静音中点（搜索窗口默认 30s），避免从词中间切断；
3. 找不到合适静音就退回固定切点；
4. 除第一段外，每段都向前多带 ``overlap_seconds`` 秒（默认 0.5s）的**重叠区**，
   该重叠区属于上一段，合并时会丢弃本段落在重叠区内的句子（见 pipeline 的 boundary_ms），
   因此不会出现重复句子，代价是跨边界的那个词可能被切开。
5. 每段统一转成 16kHz / 单声道 / mp3，既减小体积，也避免各家对采样率要求不一致。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import AudioError

AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr", ".mp4", ".mov", ".mkv"}

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


@dataclass
class SegmentPlan:
    index: int
    start_s: float
    end_s: float
    boundary_ms: int  # 该段之前属于上一段的重叠区结束点（绝对毫秒）；第一段为 0

    @property
    def start_ms(self) -> int:
        return int(round(self.start_s * 1000))

    @property
    def end_ms(self) -> int:
        return int(round(self.end_s * 1000))

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


def ensure_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise AudioError(
            f"未找到 {', '.join(missing)}，请先安装 ffmpeg（macOS: brew install ffmpeg / Ubuntu: apt install ffmpeg）"
        )


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    text = (proc.stdout or "").strip()
    if proc.returncode == 0 and text:
        try:
            value = float(text.splitlines()[0])
            if value > 0:
                return value
        except ValueError:
            pass

    proc = _run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path)])
    match = _DURATION_RE.search(proc.stderr or "")
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise AudioError(f"无法读取音频时长：{path}")


def detect_silences(path: Path, noise_db: float = -35.0, min_seconds: float = 0.4) -> list[tuple[float, float]]:
    """返回静音区间 [(start, end), ...]，单位秒。失败时返回空列表（退回固定切点）。"""
    proc = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_seconds}",
            "-f",
            "null",
            "-",
        ],
        timeout=600,
    )
    stderr = proc.stderr or ""
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            pending = max(0.0, float(start_match.group(1)))
        end_match = _SILENCE_END_RE.search(line)
        if end_match and pending is not None:
            silences.append((pending, max(pending, float(end_match.group(1)))))
            pending = None
    if pending is not None:
        silences.append((pending, max(pending, probe_duration(path))))
    return silences


def _snap_to_silence(
    target: float,
    silences: list[tuple[float, float]],
    window: float,
    lower_bound: float,
) -> float | None:
    best: tuple[float, float] | None = None
    for start, end in silences:
        if end - start < 0.2:
            continue
        mid = (start + end) / 2
        if mid <= lower_bound:
            continue
        if abs(mid - target) > window:
            continue
        if best is None or abs(mid - target) < abs(best[0] - target):
            best = (mid, end - start)
    return best[0] if best else None


def plan_segments(
    duration: float,
    max_seconds: float,
    *,
    overlap: float = 0.5,
    mode: str = "silence",
    silences: list[tuple[float, float]] | None = None,
    search_window: float = 30.0,
) -> list[SegmentPlan]:
    if duration <= 0:
        raise AudioError("音频时长为 0")
    if max_seconds <= 0:
        raise AudioError("max_seconds 必须大于 0")
    if duration <= max_seconds:
        return [SegmentPlan(0, 0.0, duration, 0)]

    overlap = min(max(0.0, overlap), max_seconds / 4)
    step = max(1.0, max_seconds - overlap)
    cuts: list[float] = [0.0]

    while duration - cuts[-1] > max_seconds:
        target = cuts[-1] + step
        boundary = None
        if mode == "silence" and silences:
            boundary = _snap_to_silence(target, silences, search_window, lower_bound=cuts[-1] + 1.0)
        if boundary is None or boundary <= cuts[-1] + 1.0 or boundary >= duration:
            boundary = min(target, duration)
        cuts.append(boundary)

    cuts.append(duration)

    plans: list[SegmentPlan] = []
    for i in range(len(cuts) - 1):
        start = 0.0 if i == 0 else max(0.0, cuts[i] - overlap)
        boundary_ms = 0 if i == 0 else int(round(cuts[i] * 1000))
        plans.append(SegmentPlan(index=i, start_s=start, end_s=cuts[i + 1], boundary_ms=boundary_ms))
    return plans


def cut_segment(src: Path, out: Path, start_s: float, duration_s: float) -> None:
    """按时间切一段并转成 16k 单声道 mp3。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-t",
        f"{duration_s:.3f}",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(out),
    ]
    proc = _run(cmd, timeout=600)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise AudioError(f"切段失败（{src.name} @ {start_s:.2f}s）：{(proc.stderr or '').strip()[:400]}")


def collect_inputs(paths: list[str], recursive: bool = True) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            files.extend(sorted(p for p in iterator if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file()))
        elif path.is_file():
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                raise AudioError(f"不支持的音频后缀：{path}")
            files.append(path)
        else:
            raise AudioError(f"路径不存在：{path}")
    # 去重但保持顺序
    seen: set[Path] = set()
    unique: list[Path] = []
    for file in files:
        if file not in seen:
            seen.add(file)
            unique.append(file)
    return unique
