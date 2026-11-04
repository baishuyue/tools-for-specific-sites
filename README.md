# 运动世界校园 · 一键跑步 v2.1（纯 Windows，手机号密码直接登录）

不再需要模拟器、不需要 adb、**也不需要抓包**。打开工具 → 输入手机号和密码 →
浏览器里拖一次滑块（几秒）→ 之后每天一键完成，token 本地加密保存。

> 适用客户端版本：**7.3.92**。协议与签名全部在 `sportwin/protocol.py` 里纯 Python 还原。

---

## 1. 登录：手机号 + 密码，全程自动

官方登录链路（工具已全自动）：

```
POST /api/v70100/login                先直接登（很多账号/设备到这一步就成了）
        ↓ 只有服务端确实要求人机验证时
本地桥 + 极验 v4（浏览器里拖一次；"adaptive"模式下经常直接放行，不用拖）
POST /api/v70270/security/geevalidate 绑定滑块结果 → 成功返回 {"error":10000,"data":""}
POST /api/v70100/login                再登一次 → token
```

关键细节（也是上一版"滑完又让你滑"的原因）：
`geevalidate` 成功时返回的是 `{"error":10000,"data":""}` —— **`data` 是空字符串，不是 `true`**。
旧逻辑按 `data` 的真值判断，把成功当失败，于是无限重试滑块。现已改为只看 `error == 10000`。

- 滑块默认用**本机浏览器**完成（127.0.0.1 一次性页面，拖完自动回传并关闭），无 Node/无 Chromium/无抓包；
- 想彻底无人值守：`login --captcha api --captcha-key <2captcha 兼容平台 key>`；
- 登录成功 token 用 Windows **DPAPI** 加密写入 `credentials.bin`，之后直接用；密码不落盘；
- 每一步的原始请求/响应都会写进 `logs/login-debug-*.json`（token/密码已脱敏），排障时直接看这个文件。

### 关于"手机号验证码登录"

官方 App 的短信登录接口没有公开文档，我按常见命名探测了 20+ 条路径
（`/sms/send`、`/user/sendSms`、`/loginBySms`、`/user/getSmsCode` …）全部返回 404，
`/api/v70100/login` 也只接受用户名+密码（`loginType` 2/3/4/5 都返回 11307）。
所以工具**不伪造短信接口**：忘记密码时，在官方 App 里用短信重置一次密码（30 秒），
之后就能用手机号+密码登录，不需要再碰 App。

> 如果你后来抓到了真实的短信接口（HAR 里出现 `/api/.../sms...`），
> 把它加到 `sportwin/login.py` 的端点常量里即可扩展，其余链路不用改。

---

## 1.5 多账号：一台电脑管多个号

```bat
一键跑步.bat login --phone 138xxxxxxxx --password 密码   :: 新增一次，自动切到新账号
一键跑步.bat accounts                                   :: 列出本机所有账号（token 已 DPAPI 加密）
一键跑步.bat switch 139xxxxxxxx                         :: 按手机号/uid 切换
一键跑步.bat run --account <uid>                     :: 只这一次用指定账号
一键跑步.bat status --account 138...
```

- 账号存在 `credentials.bin` 的账本里（`{accounts:{uid:{token,unid,phone,name,campus,device_id}}}`），
  互不覆盖；界面顶部有「当前账号」下拉框，切换后自动刷新数据。
- **每个账号一套独立设备指纹**（`device.per_account=true`，按手机号派生并落盘，之后不变），
  避免"同一台设备挂多个账号"的关联特征；老账号升级时会沿用原来的整机指纹，不会突然变新设备。
- **运行状态也按账号隔离**：`state.json` 结构为 `{current, accounts:{uid:{last_submit,…}}}`，
  提交锁、核验结果、当日完成标记都只对当前账号生效（旧版全局状态会自动迁移并做归属校验）。
- 换账号会不会触发验证？取决于服务端设备绑定策略：可能出现一次滑块（工具自动处理），
  或返回 `18001/18002`（需在官方 App 里确认设备），按提示操作即可。

## 1.6 每次数据都不一样，但都在合规区间里

学校判据（`runModePolicy`）会被解析成"硬区间"，工具在这个区间内**每次随机抽取**：

```
学校限制：单次≥1000m，速度 2.00–10.00 m/s（配速 8'20"/km–1'40"/km），步频 60–300 步/分，日目标 2000m
本次参数：2115m / 790s / 6'14"/km / 速度 2.68 m/s / 步频 177 步/分 / 步数 2335 / 步幅 0.91m
```

可调的三项 + 默认区间（都在 `config.json` 的 `run` 里，改完立即生效）：

| 项 | 配置键 | 默认 | 与学校规则的关系 |
|---|---|---|---|
| 里程 | `distance_range_m` | `[2000, 2200]` | 下限不低于 `minDistance`；超过日目标 1.15 倍会提示 |
| 配速/速度 | `pace_range_sec_per_km` | `[360, 430]`（6'00"–7'10"） | 与学校速度区间取交集；无交集时退回人类合理区间 |
| 步频 | `cadence_range_spm` | `[166, 182]` | 与学校 `stepBottom–stepTop` 取交集 |
| 结束时刻 | `stop_offset_sec` | `[0, 25]` | 每次比"现在"提前 0–25 秒，避免每天同一秒结束 |
| 固定里程/时长 | `distance_m` / `duration_sec` | `0`（关闭） | >0 时覆盖随机，调试复现用 |

自洽校验（不通过直接中止，不发脏包）：

```
速度 = 里程 / 时长            必须落在 [speedBottom, speedTop]
步频 = 步数 × 60 / 时长        必须落在 [stepBottom, stepTop]
步幅 = 里程 / 步数             必须在 0.55–1.75 m（人类区间）
里程 ≥ minDistance，时长 ≥ minRunTime，且 ≤ maxRunTime
```

`selftest` 里有 2×60 次抽样回归：宽松校规/严格校规下 **0 次越界**，且里程/配速/步频各出现 15–39 种不同取值。

## 2. 五分钟装好

1. 装 **Python 3.9+**（勾选 *Add to PATH*），无第三方 pip 依赖。
2. 双击 **`一键跑步.bat`**，点 **「登录账号」** → 填手机号、密码 → 登录
   （首次会自动弹出滑块页面，拖一下即可）。
3. 点 **「① 一键完成今天」**，等"核验通过"；
4. 点 **「② 核对数据」**（或跑完自动刷新）看服务端回执：

   ```
   今日：已完成，1 条记录，有效合计 2.10 km
   服务端记录：rrid=134xxxxxxx  complete=True  有效=2100.0 m  用时=780 s
   判定信息：太棒了，恭喜您完成了跑步，成功挑战了自己！
   轨迹落地：jsonFromObs=1（1 = 服务端已从对象存储读到轨迹）  isCheat=0
   学期里程：35579 m（有效 34000 m）
   ```

5. 可选：`一键跑步.bat task install --time 06:10` 注册每日计划任务。

命令行等价写法：

```bat
一键跑步.bat login --phone 138xxxxxxxx --password 你的密码
一键跑步.bat login --phone 138xxxxxxxx                :: 密码交互输入（不回显）
一键跑步.bat status
一键跑步.bat run --dry-run
一键跑步.bat run
```

---

## 3. 命令一览

```
一键跑步.bat                      图形界面（登录 + 一键跑）
一键跑步.bat login                登录向导（密码登录 / 设备导入 / 抓包导入）
一键跑步.bat login --phone ... --password ...
一键跑步.bat login --captcha api --captcha-key ...   全自动过滑块（可选）
一键跑步.bat status               数据核对：今日记录 / 服务端判定 / 学期里程
一键跑步.bat verify               同 status（只核对，不跑）
一键跑步.bat accounts             列出本机账号（多账号账本）
一键跑步.bat switch <手机号|uid>  切换当前账号
一键跑步.bat run --account <key>  指定账号跑一次
一键跑步.bat run [--dry-run]      跑一次（对时→规则→打卡点→轨迹→上传→结算→核验）
一键跑步.bat run --sport-type 6 --distance 3000 --duration 1100
一键跑步.bat doctor               环境自检（网关 / token / 密码学）
一键跑步.bat selftest             离线全链路自测（不联网）
一键跑步.bat task install         每日计划任务（默认 06:10）
```

---

## 4. 一次"一键跑步"做了什么

```
① 对时       GET  /api/v3/getserver/gmttime        与服务端 GMT 对齐
② 账号       GET  /api/v70390/user/info            取 uid / unid / 校区
③ 规则       POST /api/v70103/runModePolicy        最低里程、最小时长、有效时段
             POST /api/v1/getGeoFenceForRun        电子围栏
④ 打卡点     POST /api/v560/get/1/distance/{d}     服务端下发 5 个必经点
⑤ 轨迹合成   本地                                  闭环路线 + 速度变形 + 3s 采样 + 10s 分桶
⑥ 上传       POST /api/obs/temporary/url → PUT     华为云 OBS 预签名直传（gzip+base64）
⑦ 结算       POST /api/v70260/runnings/save/record 31 字段签名 + headerSign
⑧ 核验       POST /api/v70320/runnings/get_one_record  轮询 status / unCompleteReason
```

④ 之后先跑本地一致性校验（`track.verify_track`）：首末时间、严格递增、里程偏差、
打卡点覆盖。任何一项不过就中止，**不发脏包**。

---

## 5. 目录

```
一键跑步.bat
sport_run.py
sportwin/
  protocol.py    NetSecKit 4.2.4 + tokenSign / 31 字段签名（纯标准库）
  client.py      网关客户端：headerSign、重试、代理、OBS 直传
  captcha.py     极验 v4：本地浏览器桥（默认）/ 打码平台（可选）
  login.py       checkGeeUse → 滑块 → geevalidate → 密码登录 → 落盘
  auth.py        HAR / adb / 手工 token 导入（备用）
  store.py       配置、DPAPI 凭据、幂等锁、日志
  track.py       轨迹合成、分桶、结算报文、本地校验
  runner.py      全流程编排 + 结果轮询
  gui.py/cli.py  界面 / 命令行
  selftest.py    离线全链路自测
  assets/        极验官方 gt4.js（v4.2.0，本地渲染用）
docs/PROTOCOL.md  协议细节、登录链、风控点、版本失效排查顺序
docs/MIGRATION.md 从 MuMu 版迁移
docs/DEPLOY.md    任意电脑部署 / 网络异常 / 换学校 / 错误码速查
LICENSE           MIT（含第三方组件声明）
```

---

## 5.9 换台电脑能不能用？（发布前审计）

在**干净目录**（复制一份、删掉 `config.json`/`credentials.bin`/`state.json`/`logs`），
并且放在 `...\Temp\sportwin-clean 测试 目录` 这种**带空格+中文**的路径下实测：

| 项 | 结果 |
|---|---|
| 依赖 | ✅ 仅标准库；`ast` 按 3.9 语法全量校验通过（3.9–3.13 可跑） |
| 硬编码 | ✅ 无常量绝对路径、无账号/手机号；个人信息只在本地 `credentials.bin`（DPAPI） |
| 中文/空格路径 | ✅ `selftest`、`doctor`、`accounts`、`run` 全部正常 |
| 首次运行 | ✅ 自动生成 `config.json`；无凭据时提示"请先 login"而不是抛异常 |
| 时区 | ✅ 判定统一按服务端 UTC+8（把系统时区改成 UTC 后，日期与有效时段判断不变） |
| 离线自测 | ✅ 6 组全部通过，含"严格校规（min2000m/minRunTime900s）端到端" |
| 打包卫生 | ✅ `selftest` 不再往包目录写 `dryrun.json`；`.gitignore` 覆盖 config/credentials/state/logs |
| 证书/网络 | ⚠️ 依赖系统 CA；公司网/校园网若拦 `*.myqcloud.com` 会导致上传失败 |

**还需要真人确认的两件事**（我这边没有条件）：

1. **第二台真实电脑**：从 GitHub 下载 → 解压 → `selftest` → 登录 → 跑一次（重点看 DPAPI 新用户加密、浏览器滑块能否弹出）；
2. **第二所学校/校区**：换一个校规字段差异大的学校（如 `minRunTime>0`、多跑步区域、开启人脸抽检），
   确认打卡点/围栏/规则解析正常——工具遇到多区域或无人脸能力会**明确报错/提示**，不会瞎跑。

## 5.95 风控与违规（务必先读）

### 什么数据会被判作弊（实测）

同一账号 10-13 / 10-14 的记录被服务端判为作弊（`isCheat=1`，`statusInfo="本次跑步不可见，已被系统判为作弊"`，
`appealStatus=4` 申诉驳回），共同特征：

| 时间 | 里程 | 用时 | 速度 | 步频 | 判定 |
|---|---|---|---|---|---|
| 10-14 12:51 | 2092m | 573s | **4.56 m/s（16.4 km/h）** | **271** | 作弊 |
| 10-13 19:39 | 2092m | 573s | 4.56 m/s | 271 | 作弊 |
| 10-13 18:49 | 2196m | 845s | **6.41 m/s（23 km/h）** | 190 | 作弊 |
| 10-13 18:09 | 462m | 194s | **6.99 m/s（25 km/h）** | 163 | 作弊 |
| 10-13 18:31 | 285m | 80s | 4.67 m/s | 281 | 作弊 |
| 10-13 18:40 | 434m | 113s | 4.34 m/s | **312** | 数据异常→成绩无效 |

三条硬伤：**速度超过人类跑步上限**、**步频 250–312 步/分不可能**、**20 分钟内连续提交 8 次**。
旧模板（2092m/573s、speed=4560、步频 271）每天一模一样，是典型机器特征。

### 账号现在的状态：跑步冻结

`POST /api/v70103/runModePolicy` 对两个账号都返回：

```json
{"policy": 0, "runRuleModel": null, "runGeoFence": null, "freezeRunTime": 452932}
```

`freezeRunTime` 是**剩余冻结秒数**（实测每次查询递减）：约 5.2 天 / 6.2 天后解冻。
冻结期间提交的记录即便 `complete=true`，也可能在复核时被撤销。

**工具现在的默认行为：直接拒绝提交**（`blocked`），并给出剩余冻结时间：

```
规则：服务端未下发（policy=0，freezeRunTime=452932）
[拒绝] 服务端未下发跑步规则或账号处于冻结状态（…冻结剩余约 5.2 天）。冻结期内提交大概率被判无效甚至加重处罚。
```

确需强行提交才用 `run --force`（不建议）。

### 现在的数据为什么更安全

| 维度 | 旧模板 | v2 | v3（当前） |
|---|---|---|---|
| 速度 | 4.56–6.99 m/s | 2.44–2.83 m/s | 同 v2，且被 `human_speed_mps=[1.8,4.2]` 夹住 |
| 步频 | 271 / 312 | 166–182 | 同 v2，被 `human_cadence_spm=[150,190]` 夹住 |
| 步幅 | 未校验 | 0.65–1.35 m | 同 v2 |
| 里程/用时 | 每天相同 | 每次随机 | 每次随机，且由几何反算 |
| **10 秒分桶** | 线性 | **线性（σ=0.005）** | **真实波动（σ=2.5–8.6）** |
| **采样间隔** | — | 3s 固定 | 0.9–10.1s 随机（含丢点） |
| **停顿** | 无 | 无 | 0–3 次，8–45s，speed≈0 |
| **accuracy** | — | 恒 2.5–3.2 | 3–45m 真实分布 |
| **speed 与位移** | — | 严格相等 | ±7% 解耦 |
| 结束时刻 | 固定 | 随机前移 0–30 分钟 | 同 v2 |
| 频率 | 一天 8 次 | 每天 1 次 | 同 v2 + 冻结拦截 |

> 为什么这些比"数值像人"更重要：真人 09-21 那条记录速度 **7.99 m/s（28.8 km/h）**、步频 **97**，
> 却是有效成绩；我们 10-24 那条 2.66 m/s / 173 步却被判 `运动数据异常`。
> 说明系统查的是**数据序列像不像传感器采的**（分桶方差、采样抖动、accuracy 分布、speed 与位移的偏差），
> 不是总里程/配速这些聚合值。v3 就是按这个方向重写的。

### 已经判违规了怎么办

1. **别再在冻结期提交**（工具已拦，不要用 `--force` 绕过）；
2. `canAppeal=0` 的记录已过申诉期，申诉入口只对 `canAppeal=1` 开放；
3. 等冻结结束（页面会显示剩余时间），或带截图找体育老师/管理员说明情况；
4. 解冻后按下面的安全参数跑，**不要一天多次**。

### 安全参数（config.json）

```json
"run": {
  "human_speed_mps": [1.8, 4.2],
  "human_cadence_spm": [150, 190],
  "human_stride_m": [0.65, 1.35],
  "distance_range_m": [2000, 2200],
  "pace_range_sec_per_km": [360, 430],
  "cadence_range_spm": [166, 182],
  "stop_offset_sec": [0, 1800],
  "max_runs_per_day": 1,
  "allow_frozen": false
}
```

想更保守：`pace_range_sec_per_km` 调到 `[390, 450]`（6'30"–7'30"/km）、`distance_range_m` 调到 `[2000, 2100]`、
`max_runs_per_day` 保持 1，隔天跑一次。

## 6. 排障

| 现象 | 处理 |
|---|---|
| 登录失败 `10003 验证失败` | 账号或密码不对；忘记密码就在 App 里短信重置一次 |
| 登录失败 `18001 / 18002` | 需要先在官方 App 里完成设备绑定 / 确认换设备 |
| 滑块页面打不开 | 看命令行打印的 `http://127.0.0.1:xxxxx/?t=...`，手动粘到浏览器；公司网络挡了 `static.geetest.com` 时换网络 |
| 滑块一直加载失败 | 本机无法访问 `static.geetest.com`；配打码平台 key 可绕过 |
| `401 头部信息不安全` | token 失效 / uid 不匹配：重新 `login` |
| 提交后 `unCompleteReason` 非 0 | 见 `docs/PROTOCOL.md` 的原因表（里程/配速/打卡点/时段） |
| 提示"今天已完成" | 当日有效里程额度已到，次日 06:00 后可再跑 |
| 提示"仍在核验" | 服务端异步核验，等 5–15 分钟，`status` 复查，别重复提交 |

---

## 7. 安全与合规

- 密码只在内存里用于一次 Basic 认证，**不落盘**；token 用 DPAPI 加密存储，仅本机可解。
- 日志不含密码，token 只显示前缀。
- 只调用官方客户端同款接口，不触碰他人账号、不写入任何第三方系统。
- 请仅用于本人账号的自动化研究，遵守学校考核规定与用户协议。

## 8. 许可与致谢

- MIT（沿用原作者 baishuyue）。
- 协议还原参考：[`Inverx/com.zjwh.android_wh_physicalfitness`](https://github.com/Inverx/com.zjwh.android_wh_physicalfitness)（MIT），独立重写。
- `sportwin/assets/gt4.js` 为极验官方 v4.2.0 前端库，仅用于本地渲染验证码。
