# 在任何一台 Windows 电脑上部署

目标：拿到这个目录的人，**不装任何 pip 包**，五分钟内跑通自己的账号。

---

## 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11（x64） | 依赖 Windows DPAPI 加密凭据；其它平台会退化为可逆混淆，不建议 |
| Python | **3.9 – 3.13**，安装时勾选 *Add to PATH* 与 *tcl/tk* | 只用标准库；tkinter 用于界面，缺了会自动降级为控制台菜单 |
| 磁盘 | < 5 MB | 无模型、无浏览器内核 |
| 网络 | 能访问 `run.gxapp.iydsj.com`、`*.cos.*.myqcloud.com`（轨迹对象存储）、`static.geetest.com`（登录滑块） | 公司网/校园网被拦时配 `net.proxy` |
| 目录 | **可写目录**（不要放 `C:\Program Files`） | 首次运行会生成 `config.json` / `credentials.bin` / `logs/` |

> 不需要：MuMu 模拟器、adb、Node.js、Chromium/Playwright、pycryptodome、管理员权限、root。

---

## 2. 第一次跑（5 步）

```bat
:: ① 自检（离线，不联网）
一键跑步.bat selftest

:: ② 环境 + 网关自检
一键跑步.bat doctor

:: ③ 登录（手机号 + 密码，首次会弹一次滑块，多数情况自动通过）
一键跑步.bat login --phone 138xxxxxxxx --password 你的密码

:: ④ 只到上传、不结算（验证链路，不占当天额度）
一键跑步.bat run --stop-before-submit

:: ⑤ 正式完成
一键跑步.bat run
```

图形界面：双击 `一键跑步.bat` → 「登录账号」→「① 一键完成今天」→「② 核对数据」。

---

## 3. 换电脑 / 换人

- `credentials.bin` 用 **DPAPI 绑定当前 Windows 用户**，拷到别的电脑**解不开**（这是故意的）。
  换机后重新 `login` 一次即可，历史记录都在服务端，不会丢。
- 同一个目录可以放多个账号（`login` 新增、`accounts` 查看、`switch` 切换），
  每个账号有独立设备指纹与独立运行状态。

---

## 4. 网络异常怎么办

| 现象 | 处理 |
|---|---|
| `doctor` 报网关不可达 | 换网络；或 `config.json` 设 `net.proxy = "http://127.0.0.1:7890"` |
| 滑块页面一直"正在加载验证组件" | `static.geetest.com` 被拦：换网络，或 `login --captcha api --captcha-key <2captcha 兼容 key>` |
| OBS PUT 超时 | 对象存储域名被拦，换个网络环境（校园网常见） |
| 时间偏差大 | 工具会自动对时；仍失败说明本机系统时间严重不准，先校准系统时间 |

---

## 5. 换学校 / 换校区

工具不硬编码任何校区坐标，全部走接口：

1. `runModePolicy` → 里程下限、速度区间、步频区间、有效时段；
2. `getGeoFenceForRun` → 电子围栏；
3. `/api/v560/get/1/distance/{d}` → 服务端下发 5 个必经打卡点。

会明确报错的三种情况（不会再"悄悄用错模板"）：

| 报错 | 原因 | 处理 |
|---|---|---|
| `本校有 N 个跑步区域且未选定` | 学校开了多个跑步区域 | `config.json` 设 `run.run_area_id`（日志里会列出可选 id） |
| `服务端未下发打卡点` | 该校区未开放 / 参数不对 | 确认 App 内能正常起跑；确需模板才设 `run.allow_template_checkpoints=true`（仅当与 `data/` 模板同校区） |
| `人脸抽检 / 蓝牙打卡` 提示 | 该校 `faceVerify` 或 `bluetoothVerify` 为 true | 纯协议提交大概率判无效，工具只提示不阻拦 |

---

## 6. 排障：按错误码看

| 码 / 现象 | 含义 | 处理 |
|---|---|---|
| `401 头部信息不安全` | token 失效 / uid 不匹配 | 重新 `login` |
| `10003 验证失败` | 密码错 | 核对密码；忘记就在 App 里短信重置一次 |
| `10175 账号或密码错误` | 同上 | 同上 |
| `18001 / 18002` | 需要先绑定设备 / 确认换设备 | 在官方 App 里操作一次 |
| 提交后 `complete=false` | 未达最低里程/时长、配速越界、没过打卡点 | 看 `unCompleteReason`，对照 `docs/PROTOCOL.md` 第 5 节 |
| 一直是 `pending` | 服务端异步核验 | 5–15 分钟后 `一键跑步.bat status` |
| `今天已完成` | 当日额度已到 | 次日 06:00（UTC+8）后自动可跑 |

每一步的原始请求/响应都在 `logs/login-debug-*.json`（密码/token 已脱敏）与 `logs/sport-*.log`，
报 issue 时附上这两类文件即可定位。

---

## 7. 打包成 exe（可选）

```powershell
powershell -ExecutionPolicy Bypass -File build-exe.ps1
# 产物：dist\一键跑步.exe（单文件，目标机无需 Python）
```

exe 首次运行会在**当前目录**生成 `config.json` / `credentials.bin` / `logs/`，
所以别把它放进 `Program Files`。

---

## 8. 版本升级（App 发新版后）

按影响面从大到小排查，详见 `docs/PROTOCOL.md` 第 6 节：

1. `config.json` 的 `device.appVersion`（默认 7.3.92）；
2. `protocol.SALT`（业务签名盐）；
3. `protocol.py` 里的 NetSecKit 版本（KDF / RSA 公钥 / inner 字段顺序）；
4. 结算 31 字段与 OBS 载荷结构（`track.build_record`）。

验证顺序永远是：`selftest` → `doctor` → `run --dry-run` → `run --stop-before-submit` → `run`。

---

## 9. 合规

仅用于本人账号的自动化研究。请遵守所在学校体育考核规定与运动世界校园用户协议；
因使用产生的成绩无效、账号风控等后果由使用者自行承担。
