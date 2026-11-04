# -*- coding: utf-8 -*-
"""兼容入口：python sport_run.py [gui|login|status|run|doctor|selftest|task]

旧版命令（gui / run / status / doctor / dry-run）全部保留，其中 dry-run 等价于 run --dry-run。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sportwin.cli import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "dry-run":
        argv = ["run", "--dry-run"] + argv[1:]
    raise SystemExit(main(argv))
