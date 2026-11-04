# -*- coding: utf-8 -*-
"""Tkinter 图形界面：登录 → 一键跑步 → 数据核对（服务端真实数据）。"""
from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import auth, login as login_mod, store
from .client import SportError
from .runner import AuthRequired, Runner, format_verify, verify_now


def launch() -> None:
    cfg = store.load_config()
    log = store.Logger(echo=False)

    root = tk.Tk()
    root.title("运动世界校园 · 一键跑步（纯 Windows）")
    root.geometry("820x700")
    root.minsize(720, 600)

    head = ttk.Frame(root, padding=(14, 12, 14, 2))
    head.pack(fill="x")
    acct = tk.StringVar(value="账号：未登录")
    quota = tk.StringVar(value="点“登录账号”输入手机号和密码即可，无需抓包")
    ttk.Label(head, textvariable=acct, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
    ttk.Label(head, textvariable=quota, foreground="#555").pack(anchor="w", pady=(2, 0))
    row = ttk.Frame(head)
    row.pack(anchor="w", pady=(6, 0))
    ttk.Label(row, text="当前账号：").pack(side="left")
    acc_box = ttk.Combobox(row, state="readonly", width=34)
    acc_box.pack(side="left")
    ttk.Label(row, text="  （切换后自动刷新数据）", foreground="#999").pack(side="left")

    bar = ttk.Frame(root, padding=(14, 6))
    bar.pack(fill="x")
    login_btn = ttk.Button(bar, text="登录账号")
    run_btn = ttk.Button(bar, text="① 一键完成今天")
    verify_btn = ttk.Button(bar, text="② 核对数据")
    dry_btn = ttk.Button(bar, text="试运行（不提交）")
    st_btn = ttk.Button(bar, text="刷新状态")
    log_btn = ttk.Button(bar, text="日志目录")
    for b in (login_btn, run_btn, verify_btn, dry_btn, st_btn, log_btn):
        b.pack(side="left", padx=(0, 8))

    # ---- 数据核对面板 ----
    box = ttk.LabelFrame(root, text=" 数据核对（直接读服务端，不是本地推测） ", padding=8)
    box.pack(fill="x", padx=14, pady=(4, 6))
    verify_text = tk.Text(box, height=6, wrap="word", font=("Microsoft YaHei", 9),
                          relief="flat", background="#fafafa", foreground="#222")
    verify_text.pack(fill="x")
    verify_text.insert("end", "点「核对数据」查看今日记录、服务端判定与学期里程变化。")
    verify_text.configure(state="disabled")

    # ---- 日志 ----
    body = ttk.Frame(root, padding=(14, 0, 14, 4))
    body.pack(fill="both", expand=True)
    text = tk.Text(body, wrap="word", font=("Consolas", 9), height=13)
    sb = ttk.Scrollbar(body, command=text.yview)
    text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    text.pack(fill="both", expand=True)

    tip = ttk.Label(root, foreground="#888", padding=(14, 0, 14, 10),
                    text="提示：登录只需一次；跑步时段 06:00–22:00；提交后点「核对数据」可看到服务端判定与里程增量。")
    tip.pack(anchor="w")

    acc_keys = {"list": []}

    def reload_accounts() -> None:
        rows = store.list_accounts()
        acc_keys["list"] = [str(a.get("key")) for a in rows]
        labels = ["%s · %s" % (a.get("name") or "未命名", a.get("key")) for a in rows]
        acc_box["values"] = labels
        cur = store.current_key()
        if cur in acc_keys["list"]:
            acc_box.current(acc_keys["list"].index(cur))
        elif labels:
            acc_box.current(0)

    def on_pick_account(_evt=None):
        idx = acc_box.current()
        if idx < 0 or idx >= len(acc_keys["list"]):
            return
        key = acc_keys["list"][idx]
        if key == store.current_key():
            return
        if store.set_account(key):
            gui_log("已切换账号：%s" % key)
            refresh()
            do_verify(silent=True)

    busy = {"v": False}

    def gui_log(msg: str) -> None:
        def _append():
            text.insert("end", msg + "\n")
            text.see("end")
        root.after(0, _append)

    log.sink = gui_log

    def set_busy(v: bool) -> None:
        busy["v"] = v
        for b in (login_btn, run_btn, verify_btn, dry_btn, st_btn):
            b.configure(state="disabled" if v else "normal")

    def worker(fn):
        def wrap():
            try:
                fn()
            except Exception as exc:              # noqa: BLE001
                gui_log("出错：%r" % exc)
                root.after(0, lambda: messagebox.showerror("出错", str(exc)))
            finally:
                set_busy(False)
        set_busy(True)
        threading.Thread(target=wrap, daemon=True).start()

    def show_verify(v: dict) -> None:
        lines = format_verify(v)
        def _set():
            verify_text.configure(state="normal")
            verify_text.delete("1.0", "end")
            verify_text.insert("end", "\n".join(lines))
            verify_text.configure(state="disabled")
        root.after(0, _set)

    def do_verify(silent: bool = False):
        def job():
            v = verify_now(cfg, log)
            if v:
                show_verify(v)
                if not silent:
                    gui_log("已从服务端核对：" + ("今日已完成" if any(r.get("complete") for r in (v.get("today") or [])) else "今日暂无有效记录"))
            elif not silent:
                gui_log("核对失败：本地没有可用凭据")
        worker(job)

    def apply_info(info: dict) -> None:
        acct.set("账号：%s（%s）%s" % (info.get("name"), info.get("username"),
                                    info.get("campusName") or info.get("universityName") or ""))
        quota.set("uid=%s  unid=%s" % (info.get("uid"), info.get("unid")))
        reload_accounts()

    def refresh():
        def job():
            cred = store.load_credentials()
            if not cred.get("token"):
                acct.set("账号：未登录（点“登录账号”）")
                return
            try:
                apply_info(auth.whoami(cfg, log))
            except SportError as exc:
                acct.set("账号：登录已失效（%s）" % str(exc)[:50])
                quota.set("点“登录账号”重新登录")
        worker(job)

    # ---------------- 登录窗口 ----------------
    def open_login():
        win = tk.Toplevel(root)
        win.title("登录运动世界校园")
        win.geometry("470x340")
        win.transient(root)
        win.grab_set()

        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="手机号（与 App 登录用同一个）").pack(anchor="w")
        phone_var = tk.StringVar(value=(cfg.get("login") or {}).get("phone", ""))
        e_phone = ttk.Entry(frm, textvariable=phone_var, width=32)
        e_phone.pack(anchor="w", pady=(2, 10))

        ttk.Label(frm, text="密码").pack(anchor="w")
        pwd_var = tk.StringVar()
        e_pwd = ttk.Entry(frm, textvariable=pwd_var, width=32, show="*")
        e_pwd.pack(anchor="w", pady=(2, 6))
        ttk.Label(frm, foreground="#888",
                  text="首次登录会自动打开滑块页面（多数情况自动通过），之后不用再登。").pack(anchor="w")

        status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=status, foreground="#0a7d32").pack(anchor="w", pady=(8, 0))

        def do_login():
            phone = phone_var.get().strip()
            pwd = pwd_var.get()
            if not phone or not pwd:
                messagebox.showwarning("提示", "请填写手机号和密码")
                return

            def job():
                status.set("登录中…")
                try:
                    info = login_mod.login_with_password(phone, pwd, cfg, log=gui_log)
                except login_mod.LoginError as exc:
                    status.set("")
                    root.after(0, lambda: messagebox.showerror("登录失败", str(exc)))
                    return
                except Exception as exc:                    # noqa: BLE001
                    status.set("")
                    root.after(0, lambda: messagebox.showerror("登录失败", repr(exc)))
                    return
                cfg.setdefault("login", {})["phone"] = phone
                store.save_config(cfg)
                root.after(0, lambda: apply_info(info))
                root.after(0, reload_accounts)
                status.set("登录成功")
                root.after(0, win.destroy)
                do_verify(silent=True)

            threading.Thread(target=job, daemon=True).start()

        btns = ttk.Frame(frm)
        btns.pack(anchor="w", pady=(12, 0))
        ttk.Button(btns, text="登录", command=do_login).pack(side="left")
        ttk.Button(btns, text="从设备读取 token", command=lambda: threading.Thread(
            target=lambda: (auth.from_adb(log=gui_log), refresh()), daemon=True).start()).pack(side="left", padx=8)

        e_pwd.bind("<Return>", lambda _e: do_login())
        e_phone.focus_set()

    def do_run(dry: bool):
        if not store.load_credentials().get("token"):
            open_login()
            return
        if not dry and not messagebox.askyesno("确认", "将提交一次真实跑步记录（约 2km），是否继续？"):
            return

        def job():
            try:
                runner = Runner(cfg, log)
            except AuthRequired as exc:
                log(str(exc))
                root.after(0, open_login)
                return
            rep = runner.run(dry=dry, poll_sec=900 if not dry else 0)
            if rep.detail.get("verify"):
                show_verify(rep.detail["verify"])
            if rep.status == "dry":
                root.after(0, lambda: messagebox.showinfo("试运行完成", "报文已生成，未提交。"))
            elif rep.ok and rep.status == "already":
                root.after(0, lambda: messagebox.showinfo("今天已完成", "今天此前已判定有效，无需重复提交。"))
            elif rep.ok:
                root.after(0, lambda: messagebox.showinfo(
                    "成功", "核验通过，有效里程 %.2f 公里。\n详情见上方「数据核对」。" % (rep.distance_m / 1000.0)))
            elif rep.status == "pending":
                root.after(0, lambda: messagebox.showinfo(
                    "核验中", "已提交，服务端还在核验。稍后点「核对数据」复查，不要重复提交。"))
            else:
                root.after(0, lambda: messagebox.showwarning("未通过", rep.message))
        worker(job)

    acc_box.bind("<<ComboboxSelected>>", on_pick_account)
    reload_accounts()
    login_btn.configure(command=open_login)
    run_btn.configure(command=lambda: do_run(False))
    verify_btn.configure(command=lambda: do_verify(False))
    dry_btn.configure(command=lambda: do_run(True))
    st_btn.configure(command=refresh)
    log_btn.configure(command=lambda: subprocess.Popen(["explorer", str(store.LOG_DIR)]))

    root.after(200, refresh)
    root.after(600, lambda: do_verify(silent=True))
    root.mainloop()
