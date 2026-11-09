# -*- coding: utf-8 -*-
"""运动世界校园 · 一键跑步（纯 Windows / 无需模拟器）。

包结构：
    protocol  NetSecKit 4.2.4 密码学 + 业务签名（纯标准库）
    client    网关客户端（headerSign / tokenSign / 业务接口）
    store     配置、DPAPI 凭据、幂等锁、日志
    track     轨迹合成、分桶、结算报文、本地校验
    runner    全流程编排（对时 → 规则 → 打卡点 → 轨迹 → OBS → 结算 → 回查）
    login     手机号密码登录（checkGeeUse → 滑块 → geevalidate → login）
    captcha   极验 v4：本地浏览器桥 / 打码平台
    auth      HAR / adb / 手工导入（备用）
    cli       命令行
    gui       Tkinter 界面
"""
__version__ = "3.2.1"
__all__ = ["protocol", "client", "store", "track", "runner", "auth", "cli", "gui"]
