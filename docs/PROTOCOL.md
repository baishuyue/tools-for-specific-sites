# 协议与签名细节（7.3.92 / NetSecKit 4.2.4）

## 0. 登录链（本版新增，替代"抓包拿 token"）

```
POST /api/v65/security/checkGeeUse   {username, uuid, unid:0, type:2}
     → data == true   设备白名单，直接登录
     → data == false  必须过极验
POST /api/v70270/security/geevalidate {lotNumber, captchaOutput, passToken, genTime,
                                       isOffline:false, osType:0, businessType:0,
                                       uuid, username}
     → 成功：{"error":10000, "data":""}      ← 注意 data 是空串，不是 true
     → 失败：{"error":10003, "message":"验证失败"}
     → 缺字段：{"error":-10000, "message":"ERROR"}（isOffline/osType/businessType 必填）
POST /api/v70100/login               Header: Authorization: Basic base64(手机号:密码)
     body {loginType:1, device_model, os_version, username, uuid}
     → data {token, uid, unid, name, campusName, ...}
```

极验 v4 的 captchaId 固定为 `3b02ad39bd099fd3a8336d9347a189ab`。
`captcha_output / pass_token / gen_time / lot_number` 由极验前端（gt4.js）产生，
服务端只做校验；因此本地桥用官方 gt4.js 渲染、用户拖一次滑块即可，无需逆向 `w` 参数。

**短信登录**：`/api/v70100/login` 只接受用户名+密码（`loginType` 2/3/4/5 均返回 11307），
常见短信路径探测均为 404，故本工具不提供短信接口；忘记密码请在 App 内重置一次。

错误码：`10003/10004/11307` 账号或密码错误；`18001` 需先绑定设备；`18002` 需在 App 确认换设备。

## 1. 传输层：信封 {d,h,k,p,t}

客户端所有 `run.gxapp.iydsj.com` 请求（含 GET）都带 `headerSign`，body 也会被同样封装：

```
明文 inner（用于 headerSign 时是整个 headers 的 JSON；用于 body 时是业务 JSON）
  ↓
1. 生成 36 位种子：uuid4().hex + "ABCDEFGHIJKLMN12"，切成 4 段 kd1..kd4（各 10 字符）
2. 响应密钥 respKey = Feistel_KDF(md5_bytehash(kd1..kd4))          // 16 字节
3. 会话密钥 aesKey  = 16 位随机 [a-z0-9]
4. 待加密串 =
   {"data": base64(明文), "keyDataFour": kd4, "keyDataOne": kd1,
    "keyDataThree": kd3, "keyDataTwo": kd2, "platform": 2, "timeStamp": now} + "\n"
   —— 字段顺序固定，末尾必须有一个换行，h 是对这串字节的 MD5
5. d = base64(AES-128-CBC(待加密串, aesKey, IV=0))
   h = md5(待加密串)
   k = base64(RSA_PKCS1v15(aesKey))
   p = "101"（平台号），t = "0"
```

服务端响应：

```
{"r": base64(AES-128-CBC( {"data": base64(业务JSON), ...}, respKey )), ...}
```

> 换行、字段顺序、`p/t` 常量任一错了都会得到空响应或 401 —— 这是原方案"PC 端无法独立生成"的真正原因。

## 2. 请求头

```
Accept, Content-Type, appVersion=7.3.92, physicPixel, logicPixel, isRoot=0, osType=0,
cpuModel=arm64-v8a, deviceName, osVersion=13, User-Agent=okhttp/4.12.0,
DeviceId（32 位 hex，首次运行生成后固定）, timeStamp（毫秒）, nonce（uuid4）,
uid, token,
tokenSign = md5( "timeStamp=…&token=…&uid=…" + 盐 )      // 键按小写排序
headerSign = JSON 字符串，内容为 §1 的 {d,h,k,p,t}
```

`headerSign` 计算时，明文就是**除 headerSign 以外的全部头**序列化（含 tokenSign）。

## 3. 业务签名

| 场景 | 算法 |
|---|---|
| 结算 `save/record` 的 `signature` | md5(`k1=v1&k2=v2…`（31 字段按键排序）+ 盐) |
| 同报文 `originalSign` | 同上，但不加盐、值不做 bool 小写化之外的加工 |
| 五点下发 `/api/v560/get/1/distance/{d}` 的 body `sign` | md5(`http://host/path` + 盐) |

盐（33 字节，静态，全版本共用）：

```
2slhe02lsfiwowlcixisla_sls-_slaor
```

31 字段（顺序无关，按 key 排序；`goallId` 在签名串里写作字符串 `null`）：

```
address avgPower avgStepFreq calorie complete errorCode faceCheck geeToken getPrize goalId
policy selDistance selRunTime selectedUnid speed sportType startTime status stopTime themeId
totalAscent totalDis totalSteps totalTime uid unCompleteReason unauthorized useMobilityTools
uuid validDis validTime
```

`speed = round(总里程 / 总秒数 * 1000)`，`avgStepFreq = round(总步数 * 60 / 总秒数)`。

## 4. 关键接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v3/getserver/gmttime` | 服务端毫秒时间（对时，可匿名） |
| GET | `/api/v70390/user/info` | uid / unid / 校区 / 班级 |
| POST | `/api/v70103/runModePolicy` | `runRuleModel`：minDistance / minRunTime / dayGoal / validTime[] / speed / step 阈值；`runAreaModels`：跑步区域 |
| POST | `/api/v1/getGeoFenceForRun` | 电子围栏多边形（`points[].glat/glon` 为 GCJ-02） |
| POST | `/api/v560/get/1/distance/{距离}` | 下发的 5 个必经打卡点（`pointsResModels`） |
| POST | `/api/v41/running/getPersonalSemesterInfo` | 学期已跑 / 目标 |
| POST | `/api/v55/runnings/recordssummary/semester` | 学期汇总 |
| POST | `/api/v70230/runnings/records` | 历史分页 |
| POST | `/api/obs/temporary/url` | 华为云 OBS 预签名（桶 `iydsj-hbase-hot`） |
| PUT | 预签名 URL | gzip+base64 的 11 键轨迹载荷 |
| POST | `/api/v70260/runnings/save/record` | 结算，返回 rrid |
| POST | `/api/v70320/runnings/get_one_record` | 单次成绩核验 |
| POST | `/api/v65/security/checkGeeUse` | 登录前安全初审 |
| POST | `/api/v70100/login` | 密码登录（`Authorization: Basic base64(手机号:密码)`） |

OBS 载荷 11 键：`rrid uid uuid run_data step_freq_json speed_json segment_json
fixed_point_json runFaceCheck extension_json laps_json`，每项 `base64(gzip(文本))`。

## 4.5 学校规则字段（`runRuleModel`）与合规构造

实测一份真实配置（某高校，2026-10）：

```json
{"minDistance": 1000, "minRunTime": 0, "dayGoal": 2000.0,
 "speedTop": 2.0, "speedBottom": 10.0,
 "stepTop": 300, "stepBottom": 60,
 "timeInterval": 120, "runningPauseTime": 600,
 "validTime": [{"start": "06:00:00", "end": "22:00:00"}]}
```

- `speedTop/speedBottom` **没有固定大小关系**（这里 2.0 < 10.0），一律按无序区间取 min/max，
  单位是 **m/s**（与记录详情里的 `speed` 字段一致；提交报文里的 `speed` 是 mm/s，服务端会 /1000）。
- `stepTop/stepBottom` 是步频上下限（步/分），同样按无序区间处理。
- `speedStandard: 900`、`timeInterval: 120`、`runningPauseTime: 600` 与单次判定关系不大，仅作记录。
- 合规构造顺序：速度窗口（学校 ∩ 用户配速）→ 时长（≥ `minRunTime`）→ 里程（速度×时长，
  或用户区间内随机）→ 步数（步频×时长）→ 步幅校验（0.55–1.75 m）。任何一步越界直接中止。

## 5. 服务端判定（`unCompleteReason`）

| 值 | 含义 | 本工具的处理 |
|---|---|---|
| 0 | 有效 | 期望值 |
| 10 | 未达单次最低里程/时长 | `selDistance/selRunTime` 取自规则，且目标里程≥minDistance |
| 其他 | 配速越界、步频异常、未经过打卡点、非有效时段、疑似作弊 | 见下表 |

本地预防措施：

1. **打卡点覆盖**：把每个 `pointsResModels` 坐标直接作为路线拐点，逐点采样，误差 0.6m 内必有轨迹点；
2. **配速**：默认 6'00"/km 附近，`run.pace_variance` 控制抖动；<4'00" 或 >10'00" 会告警；
3. **步频**：按 `steps_per_km`（默认 1240）生成，落在 150–190 步/分；
4. **时段**：`validTime[]` 不在区间直接拒绝（`--force` 可强跑，但大概率判无效）；
5. **设备指纹**：`DeviceId` 首次生成后固定复用，避免"同账号一天换 8 台设备"的特征。

## 5.5 反作弊判定与冻结（实测）

`get_one_record` 的判定字段：

| 字段 | 含义 |
|---|---|
| `complete` | 是否达最低里程/时长 |
| `isCheat` | **1 = 判定作弊**，记录在 App 内不可见 |
| `status` | 0 正常 / 1 作弊(实测) / 2 处理中或已撤销 |
| `unCompleteReason` | 0 正常，非 0 为无效原因码 |
| `appealStatus` | 0 不可申诉 / 1 可申诉 / 4 申诉驳回 |
| `stepFreqStatus` | 步频合规标记 |
| `reasonList` | 逐项原因（含"已顺序通过所有点位"等） |

`runModePolicy` 的冻结字段：

```json
{"policy": 0, "runRuleModel": null, "freezeRunTime": 452932}
```

- `freezeRunTime` 是**剩余冻结秒数**（连续两次查询递减，实测）；
- `policy=0` + `runRuleModel=null` 表示当前不可计分（冻结 / 非考核期）；
- 判定作弊后常见 7 天冻结，两个账号的冻结起点相差约 1 天，符合"按账号单独处罚"。

触发判定的典型特征（由 10-13 那批被撤销的记录反推）：

1. 速度 > 4.5 m/s（16 km/h）或配速 < 3'40"/km；
2. 步频 > 220 步/分，或步幅 < 0.5 m / > 1.6 m；
3. 短距离（< 500m）却高步频；
4. 同一账号短时间内多次提交；
5. 每天里程/用时完全相同（模板化）。

## 5.8 "运动数据异常"是怎么判出来的（v3 复盘）

实测被判的那条记录，`reasonList` 多出一项 `complete=false, reason=运动数据异常`，
而里程/配速/步频/打卡点四项全部 `complete=true`。把被判记录与有效记录的报文拆开对比，
差异不在聚合值，而在**数据序列**：

| 特征 | 被判那版（v2） | 真实传感器应有 | v3 生成 |
|---|---|---|---|
| 10 秒距离 σ | **0.005 m** | 2–10 m | 2.5–8.6 m |
| 10 秒步数 σ | 0.45（30/30/31…） | 2–9（且含 0） | 2.2–8.6 |
| 零距离桶 | 0 | 有（红绿灯/停顿） | 0–6 个 |
| 采样间隔 | 3.000 s 固定 | 0.8–10 s 不规则 | 0.9–10.1 s |
| `accuracy` | 2.5–3.2 恒定 | 3–60 m 波动 | 3–45 m（偶发 40+） |
| `speed` vs Δd/Δt | **严格相等** | 有 ±5–10% 偏差 | ±7% 解耦 |
| 路径形状 | 5 点直线弦 | 沿道路/跑道 | Chaikin 平滑 + 8m 加密 |
| 停顿 | 无 | 每公里 0–2 次 | 0–3 次，8–45 s |

反证：真人记录 09-21 速度 **7.99 m/s（28.8 km/h）**、步频 **97**，仍然判有效 —— 说明阈值不是主要判据，
**序列的真实性**才是。v3 的生成器就是按上表逐项对齐的（见 `sportwin/track.py`）。

> 结论：以后凡是"提高数值合理性"的改动，都必须同时检查分桶方差、采样间隔分布、
> accuracy 分布、speed 与几何的偏差；只调总里程/配速没有意义。

## 6. 版本适配（协议变了怎么办）

按影响面从大到小排查，每层都有明确落点：

| 变更 | 现象 | 修改点 |
|---|---|---|
| `appVersion` | 401 / 空响应 | `protocol.APP_VERSION`、`store.DEFAULT_CONFIG.device` |
| 签名盐 | 401 或 `signature` 校验失败 | `protocol.SALT`（全局一处） |
| NetSecKit 升级（如 5.x） | headerSign 被拒，响应无法解密 | `protocol.py`：KDF / RSA 公钥 / inner 字段顺序 |
| 结算字段增减 | `signature` 校验失败 | `track.build_record` 的 `sign_map` + `protocol.SALT` |
| 轨迹载荷结构 | OBS 上传后仍判无数据 | `track.build_record` 的 `obs_payload` |
| 打卡点接口路径 | 拿不到 5 点 | `client.EP_POINTS` + `protocol.url_sign` |

验证顺序：`doctor`（连通/token）→ `selftest`（本地）→ `run --dry-run`（看 dryrun.json）→ `run`。

## 7. 兜底方案（当算法层彻底失效时）

1. **抓包比对**：用 Reqable/mitmproxy 抓一次真机请求，把 `headerSign` 里的 `d` 用会话密钥解密，
   与 `sportwin/protocol.py` 的 `Envelope.wrap` 输出逐字节 diff，定位是 KDF、字段顺序还是盐；
2. **unidbg 兜底**：把 App 的 `lib*.so`（NetSecKit）在 Windows JVM 里用 unidbg 直接调用，
   作为"离线签名 oracle"临时替换 `protocol.py`，仍然不需要模拟器；
3. **HAR 重放兜底**：token 未失效时，用 `login --har` + 真实抓包的 header 模板逐项对比。

（这三条都属于"临时恢复可用"，长期仍是把算法回落进 `protocol.py`。）
