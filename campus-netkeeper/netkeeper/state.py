"""运行状态持久化。

tick 模式下每次执行都是一个全新的进程，进程内内存态无法跨轮次保留。
因此会话索引、上次保活时间、退避截止时间、已探测到的编码方式等
必须落盘 —— 这也是 tick 模式能做到"零常驻内存"的前提。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .log import get_logger

_log = get_logger()


@dataclass
class State:
    username: str = ""
    user_index: str = ""

    last_login_at: float = 0.0
    last_online_at: float = 0.0
    last_keepalive_at: float = 0.0
    last_tick_at: float = 0.0

    mac_registered: bool = False
    mac_registered_value: str = ""

    # 探测成功后缓存，避免每轮重复试错
    adapter: str = ""
    portal_url: str = ""
    encode_variant: str = ""

    last_error: str = ""
    login_attempts: int = 0
    consecutive_failures: int = 0
    next_attempt_at: float = 0.0


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return State()
        if not isinstance(raw, dict):
            return State()
        known = {f.name for f in fields(State)}
        return State(**{k: v for k, v in raw.items() if k in known})

    def save(self, state: State) -> None:
        payload = asdict(state)
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:  # 状态写失败不应影响认证主流程
            _log.debug("状态写入失败：%s", exc)


def backoff_seconds(state: State, base: int, cap: int) -> int:
    """指数退避：base, 2*base, 4*base ... 直到 cap。"""
    exponent = min(max(0, state.consecutive_failures), 5)
    return min(base * (2 ** exponent), cap)


class Backoff:
    """把退避字段的读写收敛到一处，避免各调用点各写一套。"""

    def __init__(self, store: StateStore, state: State, base: int, cap: int) -> None:
        self.store = store
        self.state = state
        self.base = base
        self.cap = cap

    def blocked(self) -> bool:
        import time

        return time.time() < self.state.next_attempt_at

    def remaining(self) -> float:
        import time

        return max(0.0, self.state.next_attempt_at - time.time())

    def fail(self, reason: str) -> int:
        import time

        self.state.consecutive_failures += 1
        delay = backoff_seconds(self.state, self.base, self.cap)
        self.state.next_attempt_at = time.time() + delay
        self.state.last_error = reason
        self.store.save(self.state)
        _log.warning(
            "[退避] %s —— 连续第 %d 次失败，%d 秒后再试",
            reason,
            self.state.consecutive_failures,
            delay,
        )
        return delay

    def reset(self) -> None:
        if self.state.consecutive_failures:
            _log.info("已恢复正常，退避计数清零")
        self.state.consecutive_failures = 0
        self.state.next_attempt_at = 0.0
        self.state.last_error = ""
