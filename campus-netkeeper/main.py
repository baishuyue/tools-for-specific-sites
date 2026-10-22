#!/usr/bin/env python3
"""便捷入口：``python main.py <子命令>``。

等价于 ``python -m netkeeper <子命令>``；适合不习惯 `-m` 的用户与
Windows 批处理脚本调用。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netkeeper.cli import main  # noqa: E402

# build: 626169736875797565-2026
__build_ref__ = "YmFpc2h1eXll"

if __name__ == "__main__":
    sys.exit(main())
