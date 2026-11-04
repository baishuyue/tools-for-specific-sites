# -*- coding: utf-8 -*-
"""只读探针：验证「网关连通 / headerSign 是否被接受 / token 是否有效」。

不提交任何跑步数据，只用两个只读接口：
    GET /api/v3/getserver/gmttime     匿名可访问，验证信封与签名结构
    GET /api/v70390/user/info         验证 token/uid 是否匹配

用法：
    python tools/probe.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sportwin import store                     # noqa: E402
from sportwin.client import EP_SERVER_TIME, EP_USER_INFO, SportClient, SportError  # noqa: E402


def main() -> int:
    store.ensure_utf8_console()
    cfg = store.load_config()
    cred = store.load_credentials()
    net = cfg.get("net") or {}
    client = SportClient(uid=cred.get("uid"), token=cred.get("token"), unid=cred.get("unid"),
                         device=cfg.get("device"), timeout=10, retries=2,
                         proxy=net.get("proxy") or None, insecure=bool(net.get("insecure", False)))

    print("== 1. 匿名时间接口 ==")
    print("   请求头 timeStamp =", client.now_ms())
    try:
        obj = client._send("GET", EP_SERVER_TIME)
        print("   原始响应:", json.dumps(obj, ensure_ascii=False)[:200])
        if isinstance(obj, dict) and obj.get("error") == 10000:
            print("   [OK] 信封/headerSign 已被网关接受")
    except SportError as exc:
        print("   [FAIL] %s" % exc)
        return 1

    print("== 2. 账号接口（验证 token） ==")
    if not cred.get("token"):
        print("   [SKIP] 本地没有 token，先执行  python sport_run.py login")
        return 0
    try:
        client.sync_time()
        info = client.user_info()
        print("   [OK] %s（uid=%s unid=%s %s）" % (
            info.get("name"), info.get("uid"), info.get("unid"),
            info.get("campusName") or info.get("universityName") or ""))
    except SportError as exc:
        print("   [FAIL] %s" % exc)
        print("   提示：401「头部信息不安全」= token 失效 / uid 不匹配 / appVersion 变更")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
