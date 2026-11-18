"""任务队列持久化：按音频文件建 job，记录每段的状态与结果，支持断点续跑。"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .errors import JobBusy
from .utils import file_lock, local_iso


@dataclass
class Job:
    source: str
    fingerprint: str
    duration_ms: int
    dir: Path
    segments: list[dict] = field(default_factory=list)
    complete: bool = False
    created_at: str = ""
    updated_at: str = ""

    def pending_segments(self) -> list[dict]:
        return [seg for seg in self.segments if seg.get("status") != "done"]

    def done_utterances(self) -> list[dict]:
        rows: list[dict] = []
        for seg in sorted(self.segments, key=lambda item: item["index"]):
            if seg.get("status") != "done":
                continue
            rows.extend(seg.get("utterances") or [])
        rows.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
        return rows


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def _job_dir(self, source: Path) -> Path:
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
        path = self.root / digest
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def fingerprint(source: Path) -> str:
        stat = source.stat()
        return f"{stat.st_size}:{int(stat.st_mtime)}"

    def load_or_create(self, source: Path, duration_ms: int) -> Job:
        with self._lock:
            job_dir = self._job_dir(source)
            job_path = job_dir / "job.json"
            fingerprint = self.fingerprint(source)
            if job_path.exists():
                try:
                    raw = json.loads(job_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw = None
                if raw and raw.get("fingerprint") == fingerprint:
                    return Job(
                        source=raw["source"],
                        fingerprint=raw["fingerprint"],
                        duration_ms=raw.get("duration_ms", duration_ms),
                        dir=job_dir,
                        segments=raw.get("segments") or [],
                        complete=bool(raw.get("complete")),
                        created_at=raw.get("created_at", ""),
                        updated_at=raw.get("updated_at", ""),
                    )
            job = Job(
                source=str(source),
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                dir=job_dir,
                created_at=local_iso(),
                updated_at=local_iso(),
            )
            self.save(job)
            return job

    def save(self, job: Job) -> None:
        with self._lock:
            job.updated_at = local_iso()
            payload = {
                "version": 1,
                "source": job.source,
                "fingerprint": job.fingerprint,
                "duration_ms": job.duration_ms,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "complete": job.complete,
                "segments": job.segments,
            }
            job.dir.mkdir(parents=True, exist_ok=True)
            tmp = job.dir / "job.json.tmp"
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(job.dir / "job.json")

    def update_segment(self, job: Job, index: int, **fields) -> None:
        with self._lock:
            for seg in job.segments:
                if seg["index"] == index:
                    seg.update(fields)
                    break
            self.save(job)

    def prune_segments(self, job: Job) -> None:
        """任务成功后删掉切出来的音频，但保留 job.json，便于下次直接跳过。"""
        with self._lock:
            for path in job.dir.glob("seg_*.mp3"):
                try:
                    path.unlink()
                except OSError:
                    pass

    @contextmanager
    def lock(self, job: Job):
        """同一个文件加跨进程独占锁，防止两个进程同时处理导致重复提交/重复记账。"""
        job.dir.mkdir(parents=True, exist_ok=True)
        with file_lock(job.dir / "job.lock", exclusive=True, blocking=False) as acquired:
            if not acquired:
                raise JobBusy(f"该文件正在被另一个进程处理：{job.source}")
            yield
