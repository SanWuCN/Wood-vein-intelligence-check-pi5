"""日志（PRD §2、§10）。

旧版问题：日志持续追加没有上限，长期占用 GUI 内存。
这里做两件事：
  · 内存里只保留最近 N 行（默认 2000）的环形缓冲，界面读它；
  · 磁盘按大小轮转（默认 2MB × 3 份 + 当前），现场跑一整天也不会把卡写满。

日志同时带"来源"字段（本机 / 平台 / 相机 / 回放 / 更新），因为现场排查时
最常问的是"这条是设备说的还是平台说的"（PRD §5.7 连接分层）。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

LOGGER_NAME = "woodpulse"

#: 来源标识 → 中文标签。界面用这个分组显示。
SOURCE_LABEL = {
    "app": "本机",
    "platform": "平台",
    "camera": "相机",
    "replay": "样例回放",
    "telemetry": "遥测",
    "storage": "存储",
    "update": "更新",
    "ui": "界面",
    "capture": "采集",
    "selfcheck": "自检",
}


@dataclass
class LogLine:
    """界面日志的一行。`seq` 用于界面判断有没有新内容，避免整体重排。"""

    seq: int
    at: str
    level: str
    source: str
    text: str
    detail: str = ""

    @property
    def source_label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)

    @property
    def level_label(self) -> str:
        return {"DEBUG": "调试", "INFO": "信息", "WARNING": "注意", "ERROR": "故障", "CRITICAL": "故障"}.get(
            self.level, self.level
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "level": self.level,
            "source": self.source,
            "sourceLabel": self.source_label,
            "text": self.text,
            "detail": self.detail,
        }


class MemoryLogHandler(logging.Handler):
    """把日志同时写进内存环形缓冲，并回调通知界面。"""

    def __init__(self, capacity: int = 2000, on_append: Optional[Callable[[LogLine], None]] = None) -> None:
        super().__init__()
        self._lines: Deque[LogLine] = deque(maxlen=max(100, capacity))
        self._on_append = on_append
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败也不能让日志本身炸掉
            text = "<日志格式化失败>"
        with self._lock:
            self._seq += 1
            line = LogLine(
                seq=self._seq,
                at=datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                level=record.levelname,
                source=getattr(record, "source", "app"),
                text=text,
                detail=getattr(record, "detail", ""),
            )
            self._lines.append(line)
        callback = self._on_append
        if callback is not None:
            try:
                callback(line)
            except Exception:  # noqa: BLE001
                pass

    def lines(self, limit: Optional[int] = None) -> List[LogLine]:
        with self._lock:
            data = list(self._lines)
        return data[-limit:] if limit else data

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def set_callback(self, callback: Optional[Callable[[LogLine], None]]) -> None:
        self._on_append = callback


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    *,
    memory_capacity: int = 2000,
    file_max_bytes: int = 2 * 1024 * 1024,
    file_backups: int = 3,
    on_append: Optional[Callable[[LogLine], None]] = None,
    console: bool = True,
) -> MemoryLogHandler:
    """装配日志：内存缓冲 + 轮转文件 +（可选）控制台。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    memory = MemoryLogHandler(memory_capacity, on_append)
    memory.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(memory)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "woodpulse.log",
                maxBytes=file_max_bytes,
                backupCount=file_backups,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s [%(source)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            logger.addHandler(file_handler)
        except OSError as exc:  # 只读文件系统也不该让程序起不来
            logger.warning("日志文件不可写（%s），仅使用内存日志", exc, extra={"source": "app"})

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(source)s] %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(stream)

    return memory


def get_logger(source: str = "app") -> "SourcedLogger":
    return SourcedLogger(logging.getLogger(LOGGER_NAME), source)


class SourcedLogger:
    """带默认来源的 logger。用法：`get_logger("camera").info("打开设备 %s", device)`。"""

    def __init__(self, logger: logging.Logger, source: str) -> None:
        self._logger = logger
        self._source = source

    def _log(self, level: int, message: str, args: tuple, detail: str = "") -> None:
        self._logger.log(level, message, *args, extra={"source": self._source, "detail": detail})

    def debug(self, message: str, *args: Any) -> None:
        self._log(logging.DEBUG, message, args)

    def info(self, message: str, *args: Any) -> None:
        self._log(logging.INFO, message, args)

    def warning(self, message: str, *args: Any) -> None:
        self._log(logging.WARNING, message, args)

    def error(self, message: str, *args: Any) -> None:
        self._log(logging.ERROR, message, args)

    def exception(self, message: str, *args: Any) -> None:
        self._logger.exception(message, *args, extra={"source": self._source})

    def with_detail(self, message: str, detail: str, level: int = logging.INFO) -> None:
        self._log(level, message, (), detail=detail)
