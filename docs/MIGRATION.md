# 从 MuMu 版迁移到纯 Windows 版

## 1. 你需要先确认一件事

旧版把安卓环境当选签名服务用（`127.0.0.1:8765`，由 LSPosed 模块 SportSniffer 提供）。
直接换成本目录后，**唯一需要"迁一次"的东西是 token**；其它（轨迹模型、报文模板、校区坐标）都能重新生成。

## 2. 迁移步骤（约 5 分钟）

```
# ① 旧目录先备份（别急着删，签发失败时好回退）
mv 运动世界一键跑步 运动世界一键跑步.bak

# ② 新目录就位
#    把 sportworld-win/ 放到原位置，或任意位置

# ③ 登录（推荐：手机号+密码，自动过一次滑块；无需抓包）
一键跑步.bat login --phone 手机号 --password 密码

#    备用路径（不想用密码时）：
一键跑步.bat login --adb                     # MuMu 还开着：从已登录实例读 token
一键跑步.bat login --har 抓包.har            # 之前存过 HAR

# ④ 自检 + 试跑
一键跑步.bat doctor
一键跑步.bat run --dry-run
一键跑步.bat run
```

`--adb` 的默认探测路径包含 MuMu 的 `nx_main/adb.exe`；若你是自建实例，
用 `--adb-path "D:\...\adb.exe" --serial 127.0.0.1:16384`。

## 3. 可以删掉的东西

- MuMu 模拟器、Android 15 实例、KernelSU / Zygisk Next / LSPosed(Vector) / HMA / LocationSpoofer / SportSniffer；
- `config.json` 里的 `mumu_nx_dir / device / vmindex / hide_window_after_launch / boot_timeout_sec`
  （新配置见 `sportwin/store.py: DEFAULT_CONFIG`）；
- `requirements.txt` 里的 `pycryptodome`（已用标准库实现，可不装）。

旧目录里唯一还可能有用的：`data/save_2264_body.json` 的 `fivePointJson`
（本校区 5 个打卡点坐标）—— 已复制到新目录 `data/`，仅在"打卡点接口临时不可用"时作为兜底。

## 4. 老配置项与新配置项对照

| 旧 | 新 | 说明 |
|---|---|---|
| `mumu_nx_dir` / `device` / `vmindex` | — | 删除，不再需要 |
| `hide_window_after_launch` / `boot_timeout_sec` | — | 删除 |
| `semester_id` | — | 不再必要（学期信息改走接口） |
| — | `run.sport_type` | 1=计分跑，6=自由跑 |
| — | `run.distance_m` / `run.duration_sec` | 目标里程/用时；留空则按学校规则下限 + 20 秒自动推 |
| — | `run.steps_per_km` | 步频基数（1240 左右），腿长/步幅不同可调 |
| — | `run.pace_variance` | 每 10 秒速度抖动幅度（0.06） |
| — | `net.proxy` | 可选 HTTP 代理（调试抓包用） |
| — | `net.insecure` | 忽略 TLS 校验（仅调试代理用） |

老 `config.json` 可以留着，新版会忽略未知字段并自动补齐 `device.DeviceId`。

## 5. 行为差异（重要）

| 项 | 旧版 | 新版 |
|---|---|---|
| 提交耗时 | ~3 秒（靠 App 代发） | ~2 秒（本地直连） |
| 需要开的东西 | MuMu + App + 签名模块 | 只要 Python |
| 轨迹 | 复制模板 + 高斯抖动 | 按打卡点现场合成闭环路线 |
| 打卡点 | 硬编码模板 | 服务端实时下发，模板兜底 |
| 轨迹上传 | 旧版 AES-ECB 单文件（6.x/7.0 结构） | 7.3.92 的 OBS gzip+base64 11 键 |
| 换校区 | 必须重做轨迹/报文模板 | 不用改代码 |
| 重复点击 | 会重复提交 | 20 分钟幂等锁 + 当日额度预检 |
| token 存放 | App 内 | DPAPI 加密的 `credentials.bin` |

## 6. 回退方案

若新版因为协议升级跑不通，而你又急着用：

1. `git checkout` 旧目录备份，按旧 README 启动 MuMu 跑一次；
2. 新版只需在 `protocol.py` 里修一处（通常是盐或 KDF），就能重新追上；
3. 两者可以共存：旧版跑通后，用 `login --har` 把 token 导进新版，逐步替换。

## 7. 版本升级检查表（每次 App 发版后）

- [ ] `doctor` 是否仍能看到服务端时间（说明 headerSign 结构没变）
- [ ] `status` 是否能取到账号（说明 tokenSign 没变）
- [ ] `selftest` 是否全绿（说明本地密码学/报文没坏）
- [ ] `run --dry-run` 的 `signature` 是否 32 位、`originalSign` 是否 31 字段
- [ ] 真跑一次后 `status` 是否出现有效记录，`unCompleteReason` 是否为 0
