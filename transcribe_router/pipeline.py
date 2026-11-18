"""整条流水线：切段 -> 选路转写 -> 合并时间戳 -> 落 JSON / SRT -> 断点续跑。"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .audio import cut_segment, detect_silences, plan_segments, probe_duration
from .config import AppConfig
from .errors import AllProvidersFailed, AudioError, JobBusy
from .hosting import Hosting
from .ledger import Ledger
from .providers import Provider, Segment, Utterance
from .router import Router
from .srt import to_srt
from .store import Job, JobStore
from .utils import fmt_hours, local_iso

_STRIP_RE = re.compile(r"[\s，。！？；、,.!?;…~“”\"'（）()\[\]]")


def merge_segment_utterances(
    rows: list[Utterance],
    *,
    segment_start_ms: int,
    boundary_ms: int,
    duration_ms: int,
) -> list[dict]:
    """把单段结果换算成全文件绝对时间，并处理重叠区。

    规则：
    - 完全落在重叠区（结束点不晚于切点 +100ms）的句子属于上一段，丢弃；
    - 跨切点的句子**保留**，只把起点裁到切点，交给 dedupe_utterances 合并文本，
      避免「上一段拿到半句、下一段整句被丢掉」造成内容缺失。
    """
    merged: list[dict] = []
    for item in rows:
        start = item.start_ms + segment_start_ms
        end = min(item.end_ms + segment_start_ms, duration_ms)
        if start < boundary_ms:
            if end <= boundary_ms + 100:
                continue
            start = boundary_ms
        if end <= start:
            continue
        payload = item.to_dict()
        payload["start_ms"] = int(start)
        payload["end_ms"] = int(end)
        merged.append(payload)
    return merged


def dedupe_utterances(rows: list[dict], *, gap_ms: int = 200) -> list[dict]:
    """合并相邻/重叠的重复句子（主要来自切点两侧）。

    两种情况会被合并：
    1. 文本互相包含（截断的半句 + 完整句）——保留更长的文本，时间取并集；
    2. 时间重叠超过较短者一半——保留时长更长的一条。
    文本不同且重叠不明显的（例如两人同时说话）保持原样。
    """
    ordered = sorted(rows, key=lambda row: (row["start_ms"], row["end_ms"]))
    out: list[dict] = []
    for row in ordered:
        row = dict(row)
        if not out:
            out.append(row)
            continue
        prev = out[-1]
        if row["start_ms"] > prev["end_ms"] + gap_ms:
            out.append(row)
            continue

        left = _STRIP_RE.sub("", prev.get("text") or "")
        right = _STRIP_RE.sub("", row.get("text") or "")
        if left and right and (left in right or right in left):
            keep = row if len(row["text"]) >= len(prev["text"]) else prev
            out[-1] = {
                **keep,
                "start_ms": min(prev["start_ms"], row["start_ms"]),
                "end_ms": max(prev["end_ms"], row["end_ms"]),
                "speaker": keep.get("speaker") or prev.get("speaker") or row.get("speaker"),
            }
            continue

        overlap = min(prev["end_ms"], row["end_ms"]) - max(prev["start_ms"], row["start_ms"])
        shorter = min(prev["end_ms"] - prev["start_ms"], row["end_ms"] - row["start_ms"])
        if overlap > 0 and shorter > 0 and overlap >= 0.5 * shorter:
            keep = prev if (prev["end_ms"] - prev["start_ms"]) >= (row["end_ms"] - row["start_ms"]) else row
            out[-1] = {
                **keep,
                "start_ms": min(prev["start_ms"], row["start_ms"]),
                "end_ms": max(prev["end_ms"], row["end_ms"]),
                "speaker": keep.get("speaker") or prev.get("speaker") or row.get("speaker"),
            }
            continue
        out.append(row)
    return out


@dataclass
class FileResult:
    source: Path
    outputs: list[Path] = field(default_factory=list)
    done: int = 0
    failed: int = 0
    providers: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.failed == 0


class Pipeline:
    def __init__(
        self,
        cfg: AppConfig,
        ledger: Ledger,
        router: Router,
        store: JobStore,
        hosting: Hosting,
        logger,
    ):
        self.cfg = cfg
        self.ledger = ledger
        self.router = router
        self.store = store
        self.hosting = hosting
        self.log = logger
        self._print_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def segment_seconds(self, providers: list[Provider]) -> float:
        if self.cfg.segmentation.max_seconds > 0:
            return float(self.cfg.segmentation.max_seconds)
        usable = [p.max_file_seconds for p in providers if self.ledger.remaining_hours(p.name) > 0]
        if not usable:
            usable = [p.max_file_seconds for p in providers]
        return max(60.0, min(usable) if usable else 3600.0)

    def run(self, files: list[Path], out_dir: Path, *, write_json: bool = True, write_srt: bool = True) -> list[FileResult]:
        results: list[FileResult] = []
        total = len(files)
        for index, source in enumerate(files, start=1):
            self.log.info("")
            self.log.info("(%d/%d) %s", index, total, source.name)
            try:
                results.append(self._process_file(source, out_dir, write_json=write_json, write_srt=write_srt))
            except JobBusy as exc:
                self.log.error("跳过：%s", exc)
                results.append(FileResult(source=source, error=str(exc)))
            except (AudioError, AllProvidersFailed) as exc:
                self.log.error("失败：%s", exc)
                results.append(FileResult(source=source, error=str(exc)))
            except Exception as exc:  # 单文件异常不阻断整批
                self.log.exception("未预期错误：%s", exc)
                results.append(FileResult(source=source, error=repr(exc)))
        self._summary(results)
        return results

    # ------------------------------------------------------------------ #
    def _process_file(self, source: Path, out_dir: Path, *, write_json: bool, write_srt: bool) -> FileResult:
        duration_s = probe_duration(source)
        job = self.store.load_or_create(source, int(duration_s * 1000))
        # 同一文件只允许一个进程处理，避免并发提交导致重复扣额度
        with self.store.lock(job):
            if job.complete:
                self.log.info("已完成过，跳过（如需重跑请删除 %s）", job.dir)
                return FileResult(source=source, outputs=self._existing_outputs(source, out_dir))

            self._ensure_segments(job, source, duration_s)
            pending = job.pending_segments()
            if pending:
                self.log.info("共 %d 段，待处理 %d 段（%s）",
                              len(job.segments), len(pending), fmt_hours(duration_s / 3600))
                self._run_segments(job, pending)

            return self._finish(job, source, out_dir, write_json=write_json, write_srt=write_srt)

    # ------------------------------------------------------------------ #
    def _ensure_segments(self, job: Job, source: Path, duration_s: float) -> None:
        max_seconds = self.segment_seconds(self.router.providers)
        silences: list[tuple[float, float]] = []
        if self.cfg.segmentation.mode == "silence" and duration_s > max_seconds:
            self.log.info("检测静音位置，用于把切点放在句间停顿处…")
            try:
                silences = detect_silences(
                    source,
                    self.cfg.segmentation.silence_noise_db,
                    self.cfg.segmentation.silence_min_seconds,
                )
            except Exception as exc:
                self.log.warning("静音检测失败，退回固定切点：%s", exc)

        plans = plan_segments(
            duration_s,
            max_seconds,
            overlap=self.cfg.segmentation.overlap_seconds,
            mode=self.cfg.segmentation.mode,
            silences=silences,
            search_window=self.cfg.segmentation.search_window_seconds,
        )

        existing = job.segments
        same_plan = len(existing) == len(plans) and all(
            seg["index"] == plan.index and seg["start_ms"] == plan.start_ms and seg["end_ms"] == plan.end_ms
            for seg, plan in zip(existing, plans)
        )
        if same_plan:
            missing = [
                seg for seg in job.segments
                if seg.get("status") != "done" and not (job.dir / seg["path"]).exists()
            ]
            for seg in missing:
                self.log.info("补切缺失片段 %d", seg["index"])
                cut_segment(source, job.dir / seg["path"], seg["start_ms"] / 1000, (seg["end_ms"] - seg["start_ms"]) / 1000)
            if missing:
                self.store.save(job)
            self.log.info("检测到未完成任务，从断点继续（已完成 %d/%d 段）",
                          sum(1 for seg in existing if seg.get("status") == "done"), len(existing))
            return

        job.segments = []
        for plan in plans:
            path = job.dir / f"seg_{plan.index:04d}.mp3"
            if not path.exists() or path.stat().st_size == 0:
                self.log.info("切段 %d/%d：%s ~ %s", plan.index + 1, len(plans),
                              self._clock(plan.start_ms), self._clock(plan.end_ms))
                cut_segment(source, path, plan.start_s, plan.duration_s)
            job.segments.append(
                {
                    "index": plan.index,
                    "start_ms": plan.start_ms,
                    "end_ms": plan.end_ms,
                    "boundary_ms": plan.boundary_ms,
                    "path": path.name,
                    "status": "pending",
                    "provider": None,
                    "attempts": 0,
                    "error": None,
                    "utterances": [],
                }
            )
        self.store.save(job)

    def _run_segments(self, job: Job, pending: list[dict]) -> None:
        workers = max(1, min(self.cfg.concurrency.workers, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._run_one, job, seg): seg for seg in pending}
            for future in as_completed(futures):
                seg = futures[future]
                try:
                    future.result()
                except AllProvidersFailed as exc:
                    self.store.update_segment(job, seg["index"], status="failed", error=str(exc))
                    self._log_error("片段 %d 失败：%s", seg["index"], exc)
                except Exception as exc:
                    self.store.update_segment(job, seg["index"], status="failed", error=repr(exc))
                    self._log_error("片段 %d 未预期失败：%r", seg["index"], exc)

    def _run_one(self, job: Job, seg: dict) -> None:
        path = job.dir / seg["path"]
        segment = Segment(
            index=seg["index"],
            path=path,
            start_ms=seg["start_ms"],
            end_ms=seg["end_ms"],
            boundary_ms=seg["boundary_ms"],
            url=self.hosting.publish(path, job.dir.name),
        )
        self.store.update_segment(job, seg["index"], status="running", attempts=int(seg.get("attempts") or 0) + 1)
        provider_name, utterances = self.router.transcribe(segment)

        merged = merge_segment_utterances(
            utterances,
            segment_start_ms=segment.start_ms,
            boundary_ms=segment.boundary_ms,
            duration_ms=job.duration_ms,
        )

        self.store.update_segment(
            job,
            seg["index"],
            status="done",
            provider=provider_name,
            utterances=merged,
            error=None,
        )

    # ------------------------------------------------------------------ #
    def _finish(self, job: Job, source: Path, out_dir: Path, *, write_json: bool, write_srt: bool) -> FileResult:
        utterances = dedupe_utterances(job.done_utterances())
        providers = sorted({seg["provider"] for seg in job.segments if seg.get("provider")})
        failed = [seg for seg in job.segments if seg.get("status") != "done"]
        succeeded = len(job.segments) - len(failed)
        if succeeded == 0:
            # 一段都没成功时不写空文件，保留断点等下次续跑
            self.store.save(job)
            self.log.warning("没有成功的片段，未生成输出，已保留断点")
            return FileResult(source=source, done=0, failed=len(failed), providers=providers,
                              error=f"{len(failed)} 段未完成")

        outputs: list[Path] = []
        stem = source.stem
        if write_json:
            path = out_dir / f"{stem}.json"
            path.write_text(
                json.dumps(
                    {
                        "source": str(source),
                        "duration_ms": job.duration_ms,
                        "created_at": local_iso(),
                        "providers": providers,
                        "segments": [
                            {
                                "index": seg["index"],
                                "start_ms": seg["start_ms"],
                                "end_ms": seg["end_ms"],
                                "boundary_ms": seg["boundary_ms"],
                                "provider": seg.get("provider"),
                                "status": seg.get("status"),
                                "utterances": seg.get("utterances") or [],
                            }
                            for seg in sorted(job.segments, key=lambda item: item["index"])
                        ],
                        "utterances": utterances,
                        "text": "\n".join(item["text"] for item in utterances),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            outputs.append(path)

        if write_srt and utterances:
            path = out_dir / f"{stem}.srt"
            path.write_text(
                to_srt(
                    [Utterance(start_ms=u["start_ms"], end_ms=u["end_ms"], text=u["text"], speaker=u.get("speaker"))
                     for u in utterances],
                    max_chars=self.cfg.output.srt_max_chars,
                ),
                encoding="utf-8",
            )
            outputs.append(path)

        if failed:
            job.complete = False
            self.store.save(job)
            self.log.warning("文件未全部完成：%d 段成功，%d 段失败，已保留断点（重跑即可续传）",
                             len(job.segments) - len(failed), len(failed))
        else:
            job.complete = True
            self.store.save(job)
            if not self.cfg.output.keep_segments:
                self.store.prune_segments(job)
            self.hosting.cleanup(job.dir.name)
            self.log.info("完成：%s（provider: %s）", source.name, ", ".join(providers) or "无")

        return FileResult(
            source=source,
            outputs=outputs,
            done=len(job.segments) - len(failed),
            failed=len(failed),
            providers=providers,
            error=None if not failed else f"{len(failed)} 段未完成",
        )

    # ------------------------------------------------------------------ #
    def _existing_outputs(self, source: Path, out_dir: Path) -> list[Path]:
        return [path for path in (out_dir / f"{source.stem}.json", out_dir / f"{source.stem}.srt") if path.exists()]

    def _summary(self, results: list[FileResult]) -> None:
        ok = sum(1 for item in results if item.ok)
        self.log.info("")
        self.log.info("全部结束：成功 %d 个，失败 %d 个", ok, len(results) - ok)
        for item in results:
            if not item.ok:
                self.log.warning("  - %s：%s", item.source.name, item.error)

    def _log_error(self, message: str, *args) -> None:
        with self._print_lock:
            self.log.error(message, *args)

    @staticmethod
    def _clock(ms: int) -> str:
        seconds = max(0, ms // 1000)
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
