"""公网可访问的音频 URL 托管。

阿里云 ISI / 百炼 / 通义听悟 / 火山引擎都只接受公网 URL，不接受本地上传。
本模块不做内网穿透，只做一件可预期的事：把切好的片段复制到一个「静态目录」，
再按约定拼出 URL（例如本地 ~/public 映射到 https://files.example.com/asr）。

真正让 URL 可访问的 Web 服务器 / 对象存储需要你自己准备，见 README。
"""

from __future__ import annotations

import shutil
import urllib.parse
from pathlib import Path

from .config import HostingConfig


class Hosting:
    def __init__(self, cfg: HostingConfig, base_dir: Path):
        self.cfg = cfg
        self.base_dir = base_dir
        self.root = self._resolve(cfg.local_root) if cfg.local_root else None

    def _resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.base_dir / path)

    @property
    def available(self) -> bool:
        return self.cfg.mode == "url_prefix" and bool(self.cfg.url_prefix) and self.root is not None

    def publish(self, local_path: Path, job_name: str) -> str | None:
        """把文件复制到静态目录并返回公网 URL；未配置时返回 None。"""
        if not self.available:
            return None
        assert self.root is not None
        target_dir = self.root / job_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / local_path.name
        if not target.exists() or target.stat().st_size != local_path.stat().st_size:
            shutil.copy2(local_path, target)
        quoted_dir = urllib.parse.quote(job_name.strip("/"))
        quoted_name = urllib.parse.quote(local_path.name)
        return f"{self.cfg.url_prefix.rstrip('/')}/{quoted_dir}/{quoted_name}"

    def cleanup(self, job_name: str) -> None:
        if not self.available or self.root is None:
            return
        target_dir = self.root / job_name
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
