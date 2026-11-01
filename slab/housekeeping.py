"""日常维护：按天清理日志、清理过期安装包。

给"长期挂着跑"的用户准备：控制台自己写的日志会按天切分并保留 N 天，
下载缓存里的发行包也不会无限堆积。默认保留 7 天，可在「配置」页改。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from . import paths
from .events import bus


@dataclass
class CleanupReport:
    removed: List[str] = field(default_factory=list)
    freed_bytes: int = 0
    kept: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "removed": self.removed[:50],
            "removed_count": len(self.removed),
            "freed_mb": round(self.freed_bytes / (1024 * 1024), 2),
            "kept": self.kept,
            "errors": self.errors,
        }


def _sweep(directory: Path, days: int, report: CleanupReport, patterns=("*.log", "*.zip", "*.tar.gz", "*.part")) -> None:
    if days <= 0 or not directory.is_dir():
        return
    cutoff = time.time() - days * 86400
    for pattern in patterns:
        for file in directory.glob(pattern):
            try:
                if not file.is_file():
                    continue
                stat = file.stat()
                if stat.st_mtime >= cutoff:
                    report.kept += 1
                    continue
                size = stat.st_size
                file.unlink()
                report.removed.append(str(file))
                report.freed_bytes += size
            except OSError as exc:  # 文件被占用：跳过，下次再清
                report.errors.append(f"{file.name}: {exc}")


def _rotate_huge_log(log_file: Path, max_mb: int = 20) -> None:
    """单个日志超过 20MB 就截断（保留最后 1MB），避免长期运行把磁盘写满。"""

    try:
        if not log_file.is_file():
            return
        if log_file.stat().st_size <= max_mb * 1024 * 1024:
            return
        with open(log_file, "rb") as fh:
            fh.seek(-1024 * 1024, 2)
            tail = fh.read()
        with open(log_file, "wb") as fh:
            fh.write("...[旧日志已截断]...\n".encode("utf-8"))
            fh.write(tail)
        bus.log("console", f"日志过大已截断：{log_file.name}", level="warn")
    except OSError:
        pass


def cleanup_all(log_days: int = 7, download_days: int = 7) -> CleanupReport:
    """清理日志与下载缓存；返回删了什么、释放了多少。"""

    report = CleanupReport()

    # 1) 控制台自己写的每个服务日志：先截断超大文件，再按天清理
    if paths.LOG_DIR.is_dir():
        for log_file in paths.LOG_DIR.glob("*.log"):
            _rotate_huge_log(log_file)
    _sweep(paths.LOG_DIR, log_days, report)

    # 2) 下载的发行包
    _sweep(paths.DOWNLOAD_DIR, download_days, report)

    # 3) 解压用的临时目录（残包）
    tmp_dir = paths.DATA_DIR / "tmp"
    _sweep(tmp_dir, 1, report, patterns=("*",))

    if report.removed:
        bus.log(
            "console",
            f"已清理 {len(report.removed)} 个过期文件，释放 {report.as_dict()['freed_mb']} MB",
            level="info",
        )
    return report


def cleanup_snapshot(log_days: int, download_days: int) -> Dict[str, Any]:
    """只统计不删除，给配置页展示"会清理多少"。"""

    out = {"logs": {"count": 0, "mb": 0.0}, "downloads": {"count": 0, "mb": 0.0}}
    for key, directory, days in (
        ("logs", paths.LOG_DIR, log_days),
        ("downloads", paths.DOWNLOAD_DIR, download_days),
    ):
        if not directory.is_dir() or days <= 0:
            continue
        cutoff = time.time() - days * 86400
        for file in directory.glob("*"):
            try:
                if file.is_file() and file.stat().st_mtime < cutoff:
                    out[key]["count"] += 1
                    out[key]["mb"] = round(out[key]["mb"] + file.stat().st_size / (1024 * 1024), 2)
            except OSError:
                continue
    return out
