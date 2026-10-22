"""日志与限流。

日志必须支持多进程并发写入（tick 模式下每次执行都是独立进程），
因此使用按天轮转的文件handler，并容忍 Windows 上偶发的文件占用冲突。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Optional

_LOGGER_NAME = "netkeeper"
_configured = False


class _SafeRotatingHandler(logging.handlers.RotatingFileHandler):
    """轮转失败时不抛出异常，避免日志问题影响主流程。"""

    def doRollover(self) -> None:  # noqa: D102
        try:
            super().doRollover()
        except OSError:
            pass

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            super().emit(record)
        except (OSError, ValueError):
            pass


def setup_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    keep_days: int = 14,
    console: bool = False,
) -> logging.Logger:
    """初始化日志。重复调用是安全的。"""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(numeric)
    logger.propagate = False

    if _configured:
        for handler in list(logger.handlers):
            handler.setLevel(numeric)
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = _SafeRotatingHandler(
            log_dir / "netkeeper.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=max(1, keep_days),
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.setLevel(numeric)
        logger.addHandler(handler)
    except OSError as exc:  # 无写权限时退化为不记录文件
        print(f"[警告] 无法写入日志目录 {log_dir}：{exc}", file=sys.stderr)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(numeric)
        logger.addHandler(stream)

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


class RateLimiter:
    """同一类消息在时间窗内只输出一次。

    守护进程每轮都会遇到同样的"不在校园网"状态，如果每轮都写日志，
    几天就能把日志撑到几百 MB。
    """

    def __init__(self, min_interval: float = 600.0) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self.min_interval:
            return False
        self._last[key] = now
        return True

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)
