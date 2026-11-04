# 变更记录

## v3.0.0 — 运动数据真实性重写（针对"运动数据异常"判罚）

**触发背景**：10-24 的记录被服务端判定 `complete=False reason=运动数据异常`、`isCheat=1`；
复盘发现 10 秒分桶是**按比例线性切分**的（每 10 秒距离 σ=0.005 m、步数 30/30/31…），
采样间隔固定 3s、accuracy 恒定 2.5–3.2、speed 与 Δd/Δt 严格相等 —— 真实传感器不可能这么整齐。

**轨迹引擎（`sportwin/track.py` 重写）**
1. 路径 Chaikin 平滑 + 加密（8m），不再用打卡点直线弦；
2. 采样间隔随机 0.9–1.6s，8% 概率 GPS 丢点 3–9s；
3. 插入 0–3 次真实停顿（8–45s，speed≈0，不产生距离/步数）；
4. 位置噪声 = 低频摆动 + 小幅抖动，`accuracy` 拉到 3–45m（3% 概率 25–60m）；
5. `speed` 字段与几何解耦（±7% 噪声），不再严格等于 Δd/Δt；
6. 10 秒分桶按"分段长度 × 时间重叠"统计，步数按移动时间 + 抖动分配后校正总量（保证汇总=总里程/总步数）；
7. 实际里程由几何反算，采样带"噪声补偿"迭代，确保贴近目标（±3%）。

**实测对比（同一目标 2100m）**

| 指标 | v2 | v3 |
|---|---|---|
| 10 秒距离 σ | 0.005 m | 2.5–8.6 m |
| 10 秒步数 σ | 0.45 | 2.2–8.6 |
| 零距离桶 | 0 | 0–6 |
| 采样间隔 | 3.0s 固定 | 0.9–10.1s，σ≈1.75 |
| accuracy | 2.5–3.2 | 3–45（偶发 40+） |
| speed 与几何 | 严格相等 | ±7% 解耦 |

**其它**
- `selftest` 增加分桶真实性断言（σ>0.5、步数≥5 种取值）、采样间隔不固定、accuracy 分布；
- 新增配置项：`sample_interval_s` / `gps_gap_prob` / `gps_gap_s` / `gps_noise_m` / `accuracy_range` /
  `pause_prob` / `pause_sec` / `speed_noise` / `path_smooth` / `path_seg_m`；
- 版本号 3.0.0。

## v2.4.0 — 反作弊对齐：冻结拦截 + 人类可信区间

**背景**：实测两个账号 10-13/10-14 的旧数据被判 `isCheat=1`（速度 4.5–7.0 m/s、步频 271–312、
20 分钟内 8 次提交），随后 `runModePolicy` 返回 `policy=0 + freezeRunTime≈45–54 万秒`（约 5–6 天冻结）。

**新增硬拦**
- `runModePolicy` 无 `runRuleModel` / `policy=0` / `freezeRunTime>0` → 直接 `blocked`，不再提交；
  日志给出剩余冻结天数；`--force` 或 `run.allow_frozen=true` 才能绕过（不建议）。
- `max_runs_per_day`（默认 1）：当天已有记录就跳过，避免"20 分钟 8 次"这种送分行为。

**数值可信度**
- 新增 `human_speed_mps=[1.8,4.2]`、`human_cadence_spm=[150,190]`、`human_stride_m=[0.65,1.35]`，
  与校规区间取交集后才随机；`validate()` 额外拒绝 speed>4.5、cadence>200。
- 结束时刻随机前移 0–30 分钟（旧默认只有 0–25 秒，等于每天同一秒）。
- 提交前打印"数据体检"（速度/步频/步幅/人类上限）。

**文档**：README 增加「风控与违规」章节（含被撤销记录的实测表格、冻结字段含义、安全参数）；
PROTOCOL 增加「反作弊判定与冻结」字段说明与触发特征。

## v2.3.0 — 发布就绪（跨机器/跨学校普适性）

**时区**
- 新增 `store.SERVER_TZ=UTC+8`、`now_server()`、`server_midnight_ms()`、`today_key()` 按服务端时区切日；
  `in_valid_window`、`today_records`、`already_done`、对象 key 的小时目录全部改用 UTC+8。
  之前用本机时区，人在国外/系统时区改成 UTC 时会误判有效时段和"今天"。

**跨学校**
- 打卡点拿不到时**默认报错**，不再悄悄套用 `data/` 里的东莞模板；确需模板要显式
  `run.allow_template_checkpoints=true`（并提示"仅同校区有效"）。
- 多跑步区域且未选定 → 直接报错并列出可选 `run_area_id`，不再用 -1 瞎试。
- 校规含 `faceVerify` / `bluetoothVerify` 时给出明确警告（纯协议提交可能判无效）。

**跨机器**
- `ctypes.wintypes` 导入加保护，非 Windows 环境不再整包 import 失败（DPAPI 自动降级）。
- `config.json` 新增 `device.appVersion`（协议升级只需改这一处）。
- `selftest` 把 `BASE_DIR/LOG_DIR` 指到临时目录，不在发行包里留 `dryrun.json`/`logs`。

**发布物料**
- 新增 `LICENSE`（MIT + 第三方组件声明）、`docs/DEPLOY.md`（任意电脑部署/网络异常/换学校/错误码速查）。
- 删除多余的 `assets/gt4-index.source.html`，只保留渲染所需的 `gt4.js`。
- `selftest` 新增"严格校规端到端"（min2000m / minRunTime900s / 速度2.5–3.5），验证强制抬升逻辑。

## v2.2.1 — 修掉"切号后串用上一个账号状态"（第二账号不提交）

**现象**：登录第二个账号后运行，日志出现
`④ 上一次提交（rrid=…）仍在服务端核验，先查结果` → 直接退出；但第二个账号当天其实没有任何记录。

**根因**：`state.json` 是全局的，`last_submit` 属于上一个账号。切号后仍被当成"本账号待核验的提交"，
于是跳过提交、只去轮询了别人的 rrid（`get_one_record` 不校验归属，还真返回了结果，更容易误判为成功）。

**修复**
- `state.json` 改为按账号分区：`{current, accounts:{uid:{last_submit,last_result,last_success_date}}}`
  所有读写都带 account；旧扁平文件迁移时**立即落盘**（只迁一次，不会来回认领）。
- 提交锁增加归属校验：本地残留 uuid 不在当前账号的记录列表里 → 自动清空并继续正常提交。
- 「数据核对」不再展示其他账号的 `last_submit`（stale 过滤，显示"暂无本次提交"）。
- `verify_now()/runner` 全部按账号读写；`mark_submitted` 记录 `account` 字段便于排查。
- 另做了一次性修复脚本：把旧 state 归还给真正拥有该记录的账号（实测 <uid>）。

## v2.2.0 — 多账号 + 每次数据不同（且合规）

**多账号**
- 凭据从"单账号扁平结构"升级为账本：`{current, accounts:{uid:{token,unid,phone,name,campus,device_id}}}`，
  旧文件自动迁移，互不覆盖。
- 新增 `accounts` / `switch` 命令、`run --account` / `status --account` 参数；GUI 顶部加「当前账号」下拉框。
- 每个账号一套**持久化设备指纹**（`device.per_account=true`，按手机号派生并落盘，之后不漂移）；
  单账号老用户沿用原整机 DeviceId，升级不会变"新设备"。

**合规随机**
- 新增 `sportwin/policy.py`：解析 `runRuleModel`（minDistance / minRunTime / speedTop / speedBottom /
  stepTop / stepBottom / dayGoal / validTime）→ 区间化。
- 新增 `choose_run()`：速度窗口（学校 ∩ 用户配速）→ 时长（≥ minRunTime）→ 里程（速度×时长或区间随机）
  → 步数（步频×时长）→ 步幅校验；三项互相自洽，越界直接抛错不发包。
- 配置新增 `distance_range_m` / `pace_range_sec_per_km` / `cadence_range_spm` / `stop_offset_sec`；
  旧的固定 `distance_m`/`duration_sec` 自动迁移为区间（并在置 0 后可继续用固定模式）。
- 结束时刻每次随机前移 0–25 秒，避免每天同一秒。
- `selftest` 增加 2×60 次抽样回归：宽松/严格校规下 0 次越界，里程/配速/步频各 15–39 种取值。

## v2.1.3 — 看得见的"服务端回执"（数据核对）

之前提交成功只在日志里留一句"核验通过"，界面上看不到服务端到底收没收到、判没判有效。现在补上：

- **GUI 新增「数据核对」面板**：今日记录数/有效合计、服务端 rrid + complete + 有效里程 + 用时、
  判定文案、`jsonFromObs`（=1 表示服务端已从对象存储读到我们上传的轨迹）、学期里程与本次增量。
  启动时自动拉一次，跑完自动刷新，也可点「② 核对数据」手动刷新。
- **CLI**：`status` 改为完整核对报告；新增同义命令 `verify`。
- **提交留痕**：`state.json` 的 `last_submit` 现在记录 `object_key`、`upload{status,bytes}`、
  `baseline`（提交前学期里程）、`verdict`（服务端判定快照），下次打开也能看到"本次提交"的完整链路证据。
- **学期 sid 自动发现**：`recordssummary/semester` 传 `sid=0` 服务端会返回全 0，改为先调
  `getPersonalSemesterInfo` 拿真实 sid（如 9379）再查，学期里程才正确。

## v2.1.2 — 打穿真实链路（对象存储 + 判定语义）

- **⑥ 修复**：`/api/obs/temporary/url` 的 `data` 是 **JSON 字符串**（里面才是 `signedUrl` /
  `actualSignedRequestHeaders`），旧代码当 dict 用 → `'str' object has no attribute 'get'`。
  现在统一 `_load/as_dict/as_list` 解包，所有接口都兼容"再包一层字符串"的返回；
  `actualSignedRequestHeaders` 也会二次解包，`save_record` 支持 rrid 直接是数字/字符串。
- **⑧ 修复**：核验成功判定改为 `complete=true && isCheat=0 && validDis>0`。
  用真实账号 5 条历史记录校准：成功记录的 `status` 仍然是 **0**，`statusInfo` 是
  "太棒了！恭喜你，本次跑步挑战成功…"。旧逻辑等 `status==1` 会永远等不到成功。
- 提交后若响应没带 `rrid`，按 `uuid` 回查历史列表补齐，再进入轮询。
- 结算请求超时放宽到 300 秒；OBS PUT 超时 60 秒起，失败时带出响应体。
- 三个带签名字段对齐"已被服务端判定有效"的真实报文：`errorCode=200`、`goalId=0`、`themeId=-1`。
- 新增 `run --stop-before-submit`：只做对时/规则/打卡点/轨迹/上传，不提交结算（排障用）。

## v2.1.1 — 修掉"滑完验证又让你滑"的死循环

**根因**：`/api/v70270/security/geevalidate` 成功时返回 `{"error":10000,"data":""}`，
`data` 是**空字符串**。旧逻辑用 `bool(data)` 当成功标志，空串为假 → 判定失败 →
重开一次滑块 → 再失败 → 三次后退出。滑块本身其实一直是对的。

**修复**
- 成功判定改为 `error == 10000`（data 为空串/null/true 都算通过），失败只有 `10003 验证失败`。
- 登录顺序改为"先直接登、需要再验证"：`checkGeeUse` 只记录不阻断；
  直接登录若返回 10175/10003（既可能是密码错，也可能只是没验证），带一次验证再判，
  仍失败才报密码错误，不再无谓连拖三次。
- 错误码补充 `10175 账号或密码错误`；`10003/10004/10175/11307/18001/18002` 一律不再重试滑块。
- `logs/login-debug-*.json` 默认开启（token/密码自动脱敏），`--no-debug` 可关。
- 新增 `diagnose --phone` 只读诊断（对时 + checkGeeUse 原始响应）。
- 尝试从 checkGeeUse 响应里自动识别服务端下发的 `captchaId`，并支持 `login.captcha_id` 覆盖。

## v2.1.0 — 手机号密码登录（不再需要抓包）

- 新增 `sportwin/login.py`：`checkGeeUse → 极验 → geevalidate → /api/v70100/login` 全自动链路，
  密码只在内存中参与一次 Basic 认证，不落盘。
- 新增 `sportwin/captcha.py` + `sportwin/assets/gt4.js`：极验 v4 两种过法
  - 本地桥（默认）：127.0.0.1 一次性页面 + 系统浏览器，拖一次滑块即回传，无需 Node/Chromium；
  - 打码平台（可选）：2captcha 兼容协议，`login.captcha_api_key` 配置后全自动。
- CLI：`login` 改为登录向导；新增 `--captcha browser|api`、`--captcha-key`、`--no-browser`；
  `--phone/--password` 直接登录；HAR/adb/手工导入保留为备用路径。
- GUI：新增「登录账号」窗口（手机号、密码、一次滑块），点击「一键完成」时若未登录会自动弹出。
- 错误码中文化：10003/10004/11307（密码错）、18001（需绑定设备）、18002（需确认换设备）。
- 配置新增 `login.{phone,captcha_mode,captcha_api_key,captcha_timeout}`。
- 文档：README 重写登录章节；`docs/PROTOCOL.md` 增加登录链与短信接口探测结论。

## v2.0.0 — 纯 Windows 实现（去模拟器）

**架构级改动**

- 删除 `EnvManager`：不再需要 MuMu 模拟器、adb、`MuMuManager.exe`、`adb forward`、`monkey` 启动、窗口隐藏。
- 新增 `sportwin/protocol.py`：NetSecKit 4.2.4 纯标准库实现（AES-128-CBC、Feistel 变体 KDF、RSA PKCS#1 v1.5），
  彻底移除对安卓端签名服务（`127.0.0.1:8765`）的运行时依赖。
- 新增 `sportwin/client.py`：统一构造 `headerSign` / `tokenSign`，带重试、退避、代理、TLS 选项。
- 报文对齐 7.3.92：轨迹上传改为华为云 OBS 预签名 + gzip/base64 的 11 键载荷，
  不再使用旧版 AES-ECB 单文件结构；结算仍是 31 字段加盐 MD5。

**功能改动**

- 打卡点改为 `/api/v560/get/1/distance/{d}` 实时下发（含 `sign=md5(http://host/path+盐)`），
  不再硬编码单校区坐标；接口异常时回退到 `data/save_2264_body.json` 模板。
- 轨迹合成重写：闭环路线 + 时间变形（起跑慢/途中稳/尾部略快）+ 3s 采样 + 10s 分桶，
  里程、用时、配速三者自洽；拐点按时间变形反解插入，保证必经点被覆盖。
- 新增 `verify_track()` 本地一致性校验（首末时间、严格递增、里程误差、打卡点覆盖），不通过直接中止。
- 幂等：当日有效记录预检 + 20 分钟提交锁，重复点击不会重复上报。
- 结果核验：`get_one_record` 轮询到 `status=1 && unCompleteReason=0 && validDis>0`。

**工程改动**

- 凭据用 Windows **DPAPI** 加密（`credentials.bin`），支持 HAR / adb / 密码 / 手工四种导入方式。
- 移除 `pycryptodome` 运行依赖；`selftest` 若检测到它则做 AES 交叉校验，否则跳过。
- 新增 `doctor`（含 token 有效性）、`selftest`（离线全链路）、`status`（学期/记录）、`task install`（计划任务）。
- GUI 重建：账号/里程展示、一键完成、试运行、导入凭据、日志目录。
- 新增 `build-exe.ps1`（PyInstaller 单文件打包）、`tools/probe.py`（只读探针）。

**兼容**

- `python sport_run.py dry-run` 仍可用（等价 `run --dry-run`）。
- 旧 `config.json` 可直接沿用（未知字段忽略，自动补 `device.DeviceId`）。
