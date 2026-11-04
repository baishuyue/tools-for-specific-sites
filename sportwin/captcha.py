# -*- coding: utf-8 -*-
"""极验 v4 滑块求解：本地回调桥（人工拖一次）/ 打码平台（全自动）。

背景：账号密码登录接口前置了 `checkGeeUse`，新设备首次登录必然要求过极验。
原方案靠 Node + Playwright 无头识别；本模块换了两条更省事的路：

1. **本地桥（默认）**：在 127.0.0.1 起一个一次性 HTTP 服务，把极验官方 gt4.js
   渲染到系统默认浏览器里，你拖一下滑块 → 结果直接回传给 Python 进程继续登录。
   没有 Node、没有 Chromium、没有抓包，全程 3 秒，且只有"首次登录/换设备"才会用到。

2. **打码平台（可选）**：配一个 2captcha 兼容的 key，全自动，连拖都不用拖。

两种都返回同样的 GeetestResult，交给 `/api/v70270/security/geevalidate` 校验。
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

#: 登录用极验 captchaId（客户端内置常量）
CAPTCHA_ID = "3b02ad39bd099fd3a8336d9347a189ab"
ASSETS = Path(__file__).resolve().parent / "assets"
GT4_JS = ASSETS / "gt4.js"

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>运动世界 · 人机验证</title>
<style>
 :root{color-scheme:light dark}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#222}
 .card{width:min(420px,92vw);background:#fff;border-radius:14px;padding:22px 22px 26px;
       box-shadow:0 10px 40px rgba(0,0,0,.08)}
 h1{font-size:17px;margin:0 0 6px}
 p{margin:0 0 16px;color:#666;font-size:13px}
 #cap{min-height:44px}
 #st{margin-top:14px;font-size:13px;color:#0a7d32;min-height:20px}
 #st.err{color:#c0392b}
 .hint{margin-top:18px;font-size:12px;color:#999}
</style></head>
<body><div class="card">
  <h1>完成一次人机验证</h1>
  <p>拖动滑块完成拼图，本页会自动回传结果并关闭。</p>
  <div id="cap"></div>
  <div id="st">正在加载验证组件…</div>
  <div class="hint">只需在首次登录或更换设备时完成一次。</div>
</div>
<script src="/gt4.js"></script>
<script>
(function () {
  var st = document.getElementById('st');
  function say(t, err) { st.textContent = t; st.className = err ? 'err' : ''; }
  if (typeof initGeetest4 !== 'function') { say('验证组件加载失败：请检查本机网络能否访问 static.geetest.com', true); return; }
  initGeetest4({ captchaId: "__CAPTCHA_ID__", product: "bind", protocol: "https://" }, function (obj) {
    obj.appendTo('#cap')
       .onReady(function () { say('请拖动滑块完成验证'); obj.showCaptcha(); })
       .onSuccess(function () {
          var v = obj.getValidate();
          if (!v) { say('未取到验证结果，请重试', true); return; }
          say('验证通过，正在回传…');
          fetch('__CB_PATH__', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify(v) })
            .then(function () { say('完成，可以关闭本页'); setTimeout(function(){ window.close(); }, 700); })
            .catch(function () { say('回传失败，请回到命令行查看', true); });
       })
       .onError(function (e) { say('验证出错：' + JSON.stringify(e), true); });
  });
})();
</script>
</body></html>
"""


class CaptchaError(RuntimeError):
    pass


@dataclass
class GeetestResult:
    lot_number: str
    captcha_output: str
    pass_token: str
    gen_time: str
    captcha_id: str = CAPTCHA_ID

    def validate_payload(self, session_uuid: str, username: str) -> dict:
        return {
            "lotNumber": self.lot_number,
            "captchaOutput": self.captcha_output,
            "passToken": self.pass_token,
            "genTime": self.gen_time,
            "isOffline": False,
            "osType": 0,
            "businessType": 0,
            "uuid": session_uuid,
            "username": username,
        }


# ---------------------------------------------------------------------------
# 本地桥
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "sportwin-captcha/1.0"

    def log_message(self, fmt, *args):      # 静音
        pass

    # -- 公共 --
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/gt4.js":
            try:
                self._send(200, GT4_JS.read_bytes(), "application/javascript; charset=utf-8")
            except Exception:
                self._send(404, b"gt4.js missing", "text/plain")
            return
        if path in ("/", "/index.html"):
            html = (PAGE.replace("__CAPTCHA_ID__", self.server.captcha_id)
                        .replace("__CB_PATH__", self.server.cb_path))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] != self.server.cb_path:
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 1 << 20))
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception as exc:
            self._send(400, json.dumps({"ok": False, "error": str(exc)}).encode(), "application/json")
            return
        self.server.result.put(data)
        self._send(200, b'{"ok":true}', "application/json")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, captcha_id: str, cb_path: str):
        super().__init__(addr, handler)
        self.captcha_id = captcha_id
        self.cb_path = cb_path
        self.result: "queue.Queue[dict]" = queue.Queue(maxsize=4)


def solve_in_browser(captcha_id: str = CAPTCHA_ID, timeout: float = 300.0,
                     open_browser: bool = True,
                     log: Callable[[str], None] = print) -> GeetestResult:
    """起本地桥 + 打开浏览器，等用户拖一次滑块。"""
    if not GT4_JS.exists():
        raise CaptchaError("缺少 sportwin/assets/gt4.js，无法渲染滑块")
    token = secrets.token_urlsafe(12)
    server = _Server(("127.0.0.1", 0), _Handler, captcha_id, "/cb/" + token)
    port = server.server_address[1]
    url = "http://127.0.0.1:%d/?t=%s" % (port, token)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        log("已启动本地验证页：%s" % url)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        log("请在浏览器里拖动滑块完成验证（最长等待 %d 秒）…" % int(timeout))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = server.result.get(timeout=0.5)
            except queue.Empty:
                continue
            if not isinstance(data, dict):
                continue
            lot = str(data.get("lot_number") or data.get("lotNumber") or "")
            if not lot:
                continue
            log("收到验证结果：lot_number=%s…" % lot[:12])
            return GeetestResult(
                lot_number=lot,
                captcha_output=str(data.get("captcha_output") or data.get("captchaOutput") or ""),
                pass_token=str(data.get("pass_token") or data.get("passToken") or ""),
                gen_time=str(data.get("gen_time") or data.get("genTime") or ""),
                captcha_id=captcha_id,
            )
        raise CaptchaError("等待滑块验证超时（%ds）" % int(timeout))
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 打码平台（2captcha 兼容协议，可选）
# ---------------------------------------------------------------------------
def solve_via_api(api_key: str, captcha_id: str = CAPTCHA_ID,
                  endpoint: str = "https://api.2captcha.com",
                  website_url: str = "https://run.gxapp.iydsj.com/",
                  task_type: str = "GeeTestTaskProxyless",
                  timeout: float = 180.0,
                  log: Callable[[str], None] = print) -> GeetestResult:
    def call(method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/" + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    task = {
        "type": task_type,
        "websiteUrl": website_url,
        "gt": captcha_id,
        "version": 4,
        "initParameters": {"riskType": "slide"},
    }
    log("提交打码任务（%s）…" % task_type)
    created = call("createTask", {"clientKey": api_key, "task": task})
    if created.get("errorId"):
        raise CaptchaError("打码平台拒绝：%s" % created.get("errorDescription") or created)
    task_id = created.get("taskId")
    if not task_id:
        raise CaptchaError("打码平台未返回 taskId：%s" % created)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        res = call("getTaskResult", {"clientKey": api_key, "taskId": task_id})
        if res.get("status") == "ready":
            sol = res.get("solution") or {}
            lot = str(sol.get("lot_number") or sol.get("lotNumber") or "")
            if not lot:
                raise CaptchaError("打码结果缺少 lot_number：%s" % sol)
            log("打码完成：lot_number=%s…" % lot[:12])
            return GeetestResult(lot_number=lot,
                                 captcha_output=str(sol.get("captcha_output") or ""),
                                 pass_token=str(sol.get("pass_token") or ""),
                                 gen_time=str(sol.get("gen_time") or ""),
                                 captcha_id=captcha_id)
        if res.get("errorId"):
            raise CaptchaError("打码失败：%s" % res.get("errorDescription"))
    raise CaptchaError("打码平台超时（%ds）" % int(timeout))


# ---------------------------------------------------------------------------
def solve(mode: str = "browser", captcha_id: str = CAPTCHA_ID, api_key: str = "",
          timeout: float = 300.0, open_browser: bool = True,
          log: Callable[[str], None] = print) -> GeetestResult:
    mode = (mode or "browser").lower()
    if mode in ("browser", "manual", "bridge"):
        return solve_in_browser(captcha_id, timeout=timeout, open_browser=open_browser, log=log)
    if mode in ("api", "2captcha", "yescaptcha"):
        if not api_key:
            raise CaptchaError("未配置打码平台 key（config.json: login.captcha_api_key）")
        return solve_via_api(api_key, captcha_id, timeout=timeout, log=log)
    raise CaptchaError("未知的验证码模式：%s（可选 browser / api）" % mode)
