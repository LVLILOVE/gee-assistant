# GEE 凭据配置指南（保姆级）

> 面向对象：本项目部署者。目标：让 `EXECUTION_BACKEND=gee` 这条真实 Earth Engine 执行通道跑起来。
> 配套工具：`backend/tools/gee_doctor.py`（一键自检 + 一键导入凭据）。

---

## 0. 先纠正一个认知：凭据不是"找到"的，是"建"的

很多人在电脑里翻 `credentials.json`、翻浏览器密码、翻 Google Drive，结果一无所获——**因为 GEE 凭据从来不会自动出现在你机器上**。服务账号必须由你在 Google Cloud Console 里**主动创建并下载**；个人 OAuth 则必须跑一次授权流程。

本机**直连** Google 不通（`earthengine.googleapis.com` TCP 443 超时），但**经本机代理已完全打通**。

完整链路实际是 **三件事**：

| 顺序 | 卡点 | 当前状态 | 解决方式 |
|---|---|---|---|
| ① | 网络能到 Google | ✅ **已解决** | 见第 1 节（`GEE_PROXY` 已配好 + 自动重试兜抖动） |
| ② | 有 GEE 凭据 | ✅ **已解决**（个人 OAuth，2026-09-16） | 见第 3 节；服务账号路线见第 2 节 |
| ③ | 有一个**已注册 Earth Engine 的云项目** | ✅ **已完成**（`weixing-508708`，2026-09-16） | 见第 3.5 节 |

> 2026-09-16 状态：三件事全部就位，`EXECUTION_BACKEND=gee` 已切换，
> 太湖流域 2024 年 NDVI 真实任务端到端跑通（12 个逐月真实值 + 真实 GEE 瓦片图层）。

> ③ 是最容易被忽略的一环：凭据再正确，只要 `project` 指向一个没注册 EE 的项目，
> 就会得到 `Not signed up for Earth Engine or project is not registered`。

---

## 1. 网络：已打通（走本机代理）

本机跑着 **Clash Verge / Mihomo**，其默认混合代理端口是 `127.0.0.1:7897`。
实测经该代理访问各端点全部正常：

| 端点 | 结果 |
|---|---|
| `earthengine.googleapis.com` 发现文档 | **HTTP 200** |
| `earthengine-highvolume.googleapis.com` 发现文档 | **HTTP 200** |
| `oauth2.googleapis.com` | 404（端点活着，裸 GET 的正常响应） |
| `console.cloud.google.com` | 302（可正常访问，重定向到登录页） |
| `code.earthengine.google.com` | 302（可正常访问） |

`backend/.env` 里已经配好：

```ini
GEE_PROXY=http://127.0.0.1:7897
```

**注意事项（都是真踩过的坑）：**

1. **`GEE_PROXY` 必须强制覆盖，不能 `setdefault`。**
   服务进程可能从宿主环境继承 `http_proxy` / `https_proxy`（例如沙箱注入的
   `127.0.0.1:60722`）。那种代理对 TLS 隧道会返回 502，会**静默**把 GEE 请求打死
   （表现为莫名其妙的鉴权失败，而不是网络错误）。
   项目代码已改为硬覆盖，并清理 `NO_PROXY` 中排除 google 的条目。
2. **代理工具要保持运行。** 它退出后自检的 `[2/5]` 会重新变红。
3. 代理模式用「规则」或「全局」都行，但别让规则把 `googleapis.com` 判成直连。

### 已验证到「只差凭据」这一步

为确认凭据一到位就能用，项目做过一次**穿透性验证**：临时生成一个结构合法但 Google 侧不存在的
服务账号（真实 RSA 私钥），走完整鉴权链路向 Google 换令牌，得到的是：

```
RefreshError: invalid_grant: Invalid grant: account not found
```

这句是 **Google 服务器返回的真实业务错误**，而不是 `TimeoutError` / `Max retries exceeded`。
它证明了三件事：

1. ✅ 令牌请求经代理成功抵达 `oauth2.googleapis.com`
2. ✅ JWT 签名被 Google 正常解析（`google-auth` 链路无问题）
3. ✅ 沙箱、加密凭据库、`ee.Initialize` 调用链全部就位

**唯一缺的就是一个真实存在的服务账号。** 把真凭据 `--import-sa` 进来即可。
`--import-sa` 会当场再验一次，问题（服务账号不存在 / 密钥失效 / 缺角色 / 未启用 API）
会在**导入阶段**就暴露并给出对应处置建议，而不是等到首次跑任务。

> 该测试凭据已移出项目（备份在 `~/.workbuddy/tmp/gee-probe-backup/`），未留在运行环境里。

### 代理会抖，重试已经兜住

实测本机代理节点抖动明显：同一端口连发 6 次请求只通 1~2 次，失败形态多为
`ProxyError: Tunnel connection failed: 502 Bad Gateway`。

坑在于 **urllib3 自带的 Retry 不会重试它**：
`Retry._is_connection_error()` 会把 `ProxyError` 拆成 `original_error` 再判断，
而隧道失败的 `original_error` 往往不是 `ConnectTimeoutError`，于是直接放弃重试。

所以项目加了 `app/net_retry.py`：

| 能力 | 说明 |
|---|---|
| `retry_policy()` | 自定义 Retry，把「代理隧道失败」显式纳入可重试范围；退避 0.5s 起 |
| `install()` | 挂到所有新建 `requests.Session` 上，**覆盖 google-auth 的 AuthorizedSession**（换令牌走的就是它） |
| `retry_call()` | 对适配器管不到的角落做指数退避（如凭据创建时的即时刷新）；编程类异常不重试，避免白等并掩盖 bug |

效果：代理健康度实测从 1~2/6 提升到 **6/6**。真实执行侧另有 `GEE_NET_RETRIES`
（默认 3）对整段代码做重跑兜底。

### 换了网络工具 / 改了端口？用自动发现

```bash
.venv\Scripts\python.exe tools\gee_doctor.py --detect-proxy
```

会枚举本机 LISTENING 端口 → 对候选端口逐个发**真实 HTTPS 请求**（能识破「端口开着但出不了海」）
→ 命中后直接告诉你该往 `.env` 写什么。

> 请**在干净的终端里执行**。如果带着宿主注入的 `http_proxy` 变量跑，探测结果会被污染。
> 必要时先 `env -u http_proxy -u https_proxy` 再执行。

### 如果哪天代理彻底没了

任务书 6.3 明确写了：「系统依赖 Google Earth Engine 云端服务的可用性，其访问受网络环境限制，
**演示与测试采用公开样例数据集与离线样例结果兜底**」。
把 `EXECUTION_BACKEND` 改回 `offline` 即可 —— **这是任务书认可的交付形态**，全链路功能不受影响。

---

## 2. 路线 A：服务账号（推荐用于对外部署）

> 优点：无浏览器交互、可长期无人值守、适合 cpolar 对外提供服务。
> 缺点：步骤多，需要访问 Cloud Console。
> 耗时：约 10 分钟（**不需要**再走 Earth Engine 注册审核，见下方说明）。

> ✅ **本项目已注册过 Earth Engine，项目 ID 是 `weixing-508708`。**
> 所以你**不需要**再走一遍步骤 1 —— 直接把服务账号建在 `weixing-508708` 里，
> 然后在步骤 3 给它授权即可。当前正在用的是个人 OAuth，换成服务账号是为了对外部署时
> 不依赖你个人 Google 账号的刷新令牌。

> ⚠️ **导入前必读：服务账号会抢占优先级，可能把好用的 OAuth 顶掉。**
> 凭据选择顺序是 **A 服务账号文件 > B 加密凭据库 > C 个人 OAuth**，
> 也就是说一旦 `data/credentials/gee.enc` 存在，OAuth 就再也用不上了。
> 如果导入的服务账号配错（项目不对 / 缺 IAM 角色 / 密钥失效），
> 原本正常的环境会**立刻变成不可用**。
>
> 回退很简单，一条命令，不用去猜该删哪个文件：
>
> ```bash
> .venv\Scripts\python.exe tools\gee_doctor.py --drop-sa
> ```
>
> 该命令会删掉加密凭据库与密钥文件，并当场确认凭据来源已落回 OAuth。
> `--import-sa` 执行时也会把当前生效来源和这条回退命令打印出来，不用记。

### 步骤 1：注册 Earth Engine 并拿到「云项目」

> **本项目可跳过本步**，`weixing-508708` 已注册。以下内容保留给需要重建项目的场合。

1. 浏览器打开 <https://code.earthengine.google.com/register>，用你的 Google 账号登录。
2. 在注册页选择：
   - **Project selection**：选 `Create a new Google Cloud Project`，或选一个已有的项目。
   - **Choose the project's usage intent**：个人研究/学习选 `Unpaid usage` → 再选 `Academia & Research` 或 `Nonprofit`（商业用途选 `Commercial / Paid`，会涉及费用）。
   - 填写用途说明（用自己的话描述要做遥感影像分析、教学研究即可）。
3. 提交后等待审核。**记下项目 ID**（形如 `ee-yourname-123456`，注意这是项目 ID 不是项目名称）。
4. 注册通过后，同一项目会自动启用 Earth Engine API。若没自动启用：
   `Console → APIs & Services → Library → 搜索 "Google Earth Engine API" → Enable`。

> ⚠️ 这一步最容易漏。**跳过注册直接建服务账号，之后 `ee.Initialize` 一定报 `PERMISSION_DENIED` 或 "not registered for Earth Engine"**。
> 判断是否已注册：`.venv\Scripts\python.exe tools\gee_doctor.py --set-project weixing-508708`，
> 输出 `[ OK ] 已注册 Earth Engine，真实调用成功` 即说明该项目可用。

### 步骤 2：创建服务账号

1. 打开 <https://console.cloud.google.com>，**务必先在顶部项目选择器里切到 `weixing-508708`**（或步骤 1 建的你的项目）。
2. 左侧菜单 → `IAM 和管理` → `服务账号`。
3. 点 `+ 创建服务账号`：
   - 名称：`gee-webapp`
   - ID：自动生成即可
   - 描述：`卫星遥感影像智能分析助手 GEE 执行账号`
4. 点 `创建并继续`。角色分配这里**可以先跳过**（下一步在 IAM 里统一授），直接点 `完成`。

### 步骤 3：授予权限（关键，漏了必报 403）

1. 左侧菜单 → `IAM 和管理` → `IAM`。
2. 点 `+ 授予访问权限`（或 `添加`）。
3. **新主休**填服务账号邮箱，形如：
   `gee-webapp@你的项目ID.iam.gserviceaccount.com`
   （在服务账号列表里点进去就能复制到这个邮箱）
4. 添加以下角色（官方文档要求）：

   | 角色 | 是否必需 | 用途 |
   |---|---|---|
   | **Earth Engine Resource Viewer** | ✅ 必需 | 读取影像、执行计算 |
   | **Service Usage Consumer** | ⚠️ 通常需要 | 调用 Google API 的配额归属 |
   | Earth Engine Resource Writer | 可选 | 需要创建/导出 EE Assets 时 |

5. 保存。

### 步骤 4：下载 JSON 密钥

1. 回到 `IAM 和管理` → `服务账号`，点进刚才那个服务账号。
2. 切到 `密钥` 标签 → `添加密钥` → `创建新密钥`。
3. 选择 **JSON** → `创建`。
4. 浏览器会自动下载一个 `.json` 文件（**这是唯一一次下载机会，私钥不会再次展示**）。
5. 建议把它挪到一个不进版本库的目录，例如：
   `C:\Users\Administrator\.gee\gee-webapp-key.json`

> 🔐 这个文件里含私钥明文。**不要**放进项目目录、不要提交 git、不要发到聊天工具里。

### 步骤 5：导入本项目（一条命令）

在 `backend` 目录执行：

```bash
.venv\Scripts\python.exe tools\gee_doctor.py --import-sa "C:\Users\Administrator\.gee\gee-webapp-key.json"
```

脚本会**先校验再导入**：

| 校验项 | 不合格时的处理 |
|---|---|
| 文件存在 | 报「文件不存在」并退出 1 |
| 是合法 JSON | 报「不是合法的 JSON」并退出 1 |
| `type == "service_account"` | 报错并提示「OAuth 客户端属于另一条路线」 |
| 含 `client_email` / `private_key` / `project_id` | 报出缺哪个字段 |

通过后用 **Fernet 对称加密**写入 `backend/data/credentials/gee.enc`，
密钥单独存 `data/credentials/.key`（两者都已被 `.gitignore` 忽略）。
导入成功不等于能用 —— 脚本会**立刻用该凭据向 Google 换一次令牌**，
把问题暴露在导入阶段而不是首次跑任务时。这一步会打印两项 Google 侧授权提醒。

> 已验证（`tools/test_sa_route.py`，44 项断言全通过）：密文中**搜不到私钥片段，
> 也搜不到 `client_email` 明文**；密钥与密文分开存放；`resolve()` 会正确选中
> `encrypted_store` 而非 OAuth；账户以 `p***@域名` 形式脱敏展示。

**如果导入后反而不可用**（凭据抢占优先级的副作用），一条命令回退：

```bash
.venv\Scripts\python.exe tools\gee_doctor.py --drop-sa
```

它会删除 `gee.enc` 与 `.key`，并当场确认凭据来源已落回 OAuth。该命令幂等，重复执行不报错。

### 步骤 6：填项目 ID 并切换后端

编辑 `backend/.env`：

```ini
EXECUTION_BACKEND=gee
GEE_PROJECT=weixing-508708
# 若网络工具是本地端口模式，再补一行：
# GEE_PROXY=http://127.0.0.1:7890
```

> `GEE_PROJECT` 用你实际导入的服务账号所在的**项目 ID**（不是服务账号邮箱，也不是项目名称）。
> 本项目应填 `weixing-508708`。

### 步骤 7：验证

```bash
.venv\Scripts\python.exe tools\gee_doctor.py
```

看到 `结论：GEE 通道已就绪 → 把 backend/.env 的 EXECUTION_BACKEND 改为 gee 即可` 即成功。
随后重启后端服务，在页面发起一次 NDVI 任务，地图上应出现**真实 Sentinel-2 瓦片**。

> 服务进程有缓存，**改完 `.env` 必须重启后端**才会生效。
> 页面右上角点「GEE 已就绪」标签可看到当前生效的凭据来源与脱敏账号。

---

## 3. 路线 B：个人账号 OAuth（最快，适合本地自用）

> 优点：3 条命令搞定。
> 缺点：需要浏览器能打开 Google 登录页；凭据是个人刷新令牌，不适合长期对外服务。
> 前提：**同样需要先完成第 2 节步骤 1 的 Earth Engine 注册**。

```bash
cd backend
.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple earthengine-api

# 方式一：用封装好的脚本
.venv\Scripts\python.exe tools\gee_doctor.py --oauth

# 方式二：直接用官方 CLI（等价）
.venv\Scripts\earthengine.exe authenticate
```

授权成功后会生成 `C:\Users\Administrator\.config\earthengine\credentials`（刷新令牌，自动续期）。
本项目会自动识别这个文件，无需额外配置路径。

**授权成功后，还差最后一步：项目 ID。** 个人 OAuth 凭据**必须**显式指定一个已注册
Earth Engine 的云项目（官方 SDK 项目 `517222506229` 是无权使用的，ee 会直接报
`Caller does not have required permission to use project ...`）。

有项目 ID 就一条命令：

```bash
.venv\Scripts\python.exe tools\gee_doctor.py --set-project 你的项目ID
```

它会做真实的 `ee.Initialize(project=...)` + 一次数据集调用验证，通过后自动把
`GEE_PROJECT` 与 `EXECUTION_BACKEND=gee` 写回 `.env`。

**没有项目 ID / 不确定有没有？**

```bash
.venv\Scripts\python.exe tools\gee_doctor.py --detect-project
```

它会尝试列举你账号下可见的云项目，逐个实测哪个真正注册了 Earth Engine。
但要注意一个**硬限制**：列出项目依赖 Cloud Resource Manager API，而它必须在使用它的
项目上启用 —— 个人 OAuth 凭据的配额项目是 earthengine-api 官方客户端项目，你无权启用，
所以这条命令对 OAuth 用户通常会返回 403 并跳过列举。这**不影响使用**，只需手工提供项目 ID。

项目 ID 从哪来？去 <https://code.earthengine.google.com/register> 注册时会让你
**选择或新建一个云项目**，那个项目的 ID（形如 `my-ee-project-123456`）就是它。
也可以在 <https://console.cloud.google.com/> 顶部的项目选择器里看到。
注册是免费的，个人账号通常即时通过或几天内审核。

---

## 3.5 卡点 ③：Earth Engine 项目注册（✅ 已完成：`weixing-508708`）

> **2026-09-16 已解决**。本项目采用的云项目 ID 为 **`weixing-508708`**，
> 已通过 `--set-project` 完成真实校验（`ee.Initialize` + Sentinel-2 真实数据集调用均成功），
> 并写回 `backend/.env`：`GEE_PROJECT=weixing-508708`、`EXECUTION_BACKEND=gee`。
> 下面保留完整操作路径，供换机器 / 换项目时复现。

OAuth 凭据本身已经可用（实测换令牌成功，EE API 返回的是业务错误而非鉴权错误），
但 `ee.Initialize()` 还缺一个 `project`。原因是：

- 个人 OAuth 凭据的 `quota_project_id` 是空的，ee 会**回退到官方 SDK 项目 `517222506229`**；
- 那个项目对普通用户无权使用，于是报：
  `Caller does not have required permission to use project 517222506229`。

**解决路径：**

1. 打开 <https://code.earthengine.google.com/register>
2. 登录同一个 Google 账号 → 选择「为项目注册」→ **新建**或**选择一个已有的云项目**
3. 复制那个项目的 **Project ID**（形如 `ee-yourname-482913`，注意不是项目名称、也不是项目编号）
4. 回来执行一条命令：

```bash
cd backend
.venv\Scripts\python.exe tools\gee_doctor.py --set-project ee-yourname-482913
```

该命令会做真实的 `ee.Initialize(project=...)` + 一次 Sentinel-2 数据集调用，
**验证通过才写回配置**（`GEE_PROJECT` + `EXECUTION_BACKEND=gee`）。

如果报 `Not signed up for Earth Engine or project is not registered`，
说明注册还没通过（或项目 ID 填错），等审核通过后重跑即可。

> 已经有一个项目、但**不确定 ID 是什么**时，不要指望自动枚举：
> Cloud Resource Manager API 在个人账号下常常无权启用，EE 侧也没有"列出我注册了哪些项目"的端点。
> 直接去 <https://console.cloud.google.com/cloud-resource-manager> 或项目选择器里看 ID 最快。

---

## 3.6 验证真实通道（不依赖大模型）

凭据就绪后，用这条命令确认「沙箱 + 真实 Earth Engine」链路本身是否通 —— 它跑的是
`app/codegen.py` 里的兜底模板代码，不经过 DeepSeek，因此能把**协议问题**和**代码生成问题**分开：

```bash
cd backend
.venv\Scripts\python.exe tools\verify_real_gee.py 太湖流域
```

2026-09-16 实测输出（太湖流域 2024 全年，植被口径）：

```
全年影像数= 146
NDVI 统计= {'NDVI_max': 0.8673, 'NDVI_mean': 0.5199, 'NDVI_min': -0.6206}
逐月 NDVI= [0.352, 0.387, 0.412, 0.503, 0.512, 0.473,
            0.569, 0.598, 0.589, 0.567, 0.503, 0.394]
```

图层瓦片 URL 形如
`https://earthengine-highvolume.googleapis.com/v1/projects/weixing-508708/maps/...`，
URL 里带着项目 ID，是判断"到底有没有真连上 GEE"最直接的证据。

### 关于 NDVI 均值的正负（口径问题，别当成 bug 或植被退化）

太湖流域的 AOI 是一个 0.5°×0.5° 的矩形，**太湖水面占了约 88%**，
而水体的 NDVI 恒为负值。所以：

| 口径 | 全年均值 | 逐月曲线 |
|---|---|---|
| 不掩膜水体（混合口径） | **-0.12** | -0.21 ~ -0.01，**看不出季节变化** |
| 掩膜水体（植被口径，**本项目采用**） | **+0.52** | 0.35 → 0.60（8 月峰值）→ 0.39，**物候曲线清晰** |

水面主导时植被信号会被完全压垮，曲线的季节性也随之消失。
本项目已把「植被口径」固化进 `app/codegen.py` 的 `_TASK_CALIBER`（详见下节），
所以 NDVI 任务输出的均值是正的、且呈正常物候曲线。

---

## 3.7 分析口径必须固化（否则同一请求两次结果无法解释）

**这是接入真实 GEE 之后最容易踩、也最容易被忽视的一类问题。**
实测：同一句「太湖流域 NDVI 2024」，修口径前两次执行分别给出 **-0.12** 和 **+0.52**，差 0.64。
三个独立原因，都与代码正确性无关：

| 原因 | 现象 | 处理 |
|---|---|---|
| 是否掩膜水体（SCL≠6） | 均值差 0.64，有无季节变化完全相反 | 按任务类型固化口径 |
| `.map()` 用 `copyProperties` 只保留 `system:time_start` | 云量属性被抹掉 → `.sort('CLOUDY_PIXEL_PERCENTAGE')` **静默失效**（不报错、集合也非空） | 两个属性都要保留 |
| 逐月按 `system:time_start` 排序 | 取到月初最云的几景，掩膜后可能无有效像元 → 统计返回空 → 曲线出现假的「无数据」 | 必须 `sort('CLOUDY_PIXEL_PERCENTAGE')` |

> 附带一条：`NDVI = (B8-B4)/(B8+B4)`，当 `B8 = 0`（S2_SR 的无效像元填充值）时恒为 **-1**，
> 最小值统计会出现理论边界值。加一层反射率有效性掩膜
> （`img.select('B8').gt(0).And(img.select('B4').gt(0))`）即可，实测 `min` 由 -1 → -0.62
> 而 `mean`/`max` 完全不变。

**落地位置**：`backend/app/codegen.py` 的 `_TASK_CALIBER`（四类任务各一段规范），
同时注入**生成**与**修复**两条提示词。修复提示词里额外硬约束「严禁改变分析口径」——
不加这条的话，自动修复会顺手把口径改掉，前后结果就不可比了。

**效果验证**（2026-09-16）：固化后模板路径与 DeepSeek 生成路径的结果差异 **< 0.004**：

| 路径 | 全年均值 | 逐月曲线（部分） |
|---|---|---|
| 模板代码 | 0.5199 | 0.352 → 0.598（8月）→ 0.394 |
| DeepSeek 生成 | 0.5164 | 0.351 → 0.601（8月）→ 0.401 |

---

## 4. 两条路线怎么选

| 维度 | 路线 A 服务账号 | 路线 B 个人 OAuth |
|---|---|---|
| 配置耗时 | ~15 分钟 | ~3 分钟 |
| 需要浏览器 | 建密钥时需要 | 授权时需要 |
| 无人值守 | ✅ 长期有效 | ⚠️ 刷新令牌可能失效需重登 |
| 对外部署（cpolar） | ✅ 推荐 | △ 可用但不推荐 |
| 凭据落盘位置 | 项目内 `data/credentials/gee.enc`（加密） | `~/.config/earthengine/credentials` |
| 合规性（任务书要求凭据加密） | ✅ 强加密 | △ 官方明文文件 |
| **当前状态** | 未配置 | ✅ **已完成授权**（2026-09-16） |

**结论**：本地先把功能跑通 → 路线 B（当前已走通）；要对外演示/交作业 → 路线 A。
两条路线**都需要**先完成第 3.5 节的项目注册。

---

## 5. 报错对照表（照着查）

| 报错关键字 | 真实含义 | 处理 |
|---|---|---|
| `未找到任何 GEE 凭据` / `DefaultCredentialsError` | 三条候选路径都没有凭据文件 | 按第 2 或第 3 节创建并导入 |
| `Could not automatically determine credentials` | 同上，`ee.Initialize()` 没拿到凭据 | 确认 `--import-sa` 成功，或 OAuth 文件存在 |
| `Not signed up for Earth Engine or project is not registered` | **该项目没在 EE 侧注册**（也可能是 ID 拼错） | 去 <https://code.earthengine.google.com/register> 注册该项目 |
| `Caller does not have required permission to use project xxx` | 用的项目无权访问（OAuth 用户最常见的坑：未指定 `project=`，ee 回退到官方 SDK 项目） | 用 `--set-project 你的项目ID` 显式指定自己的项目 |
| `Cloud Resource Manager API has not been used in project 517222506229` | 列举项目需该 API，而它对官方客户端项目未启用，用户无权开启 | **正常现象**，改用 `--set-project 项目ID`，别依赖 `--detect-project` |
| `403 ... has not been used in project` | **Google Earth Engine API 未启用** | Console → APIs & Services → Library → 启用 Google Earth Engine API |
| `403 User does not have permission` | ①项目未注册 EE ②服务账号缺 IAM 角色 | 先确认 <https://code.earthengine.google.com/register> 已注册该项目；再补 `Earth Engine Resource Viewer` |
| `not registered for Earth Engine` | 项目未在 EE 侧注册 | 走注册页完成注册，等待审核通过 |
| `PERMISSION_DENIED: Caller does not have required permission` | 同上，或缺 `Service Usage Consumer` | 补齐 IAM 角色 |
| `invalid_grant` / `Token has been expired or revoked` | OAuth 刷新令牌失效 | `earthengine authenticate --force` 重新授权 |
| `invalid_grant: account not found` | 服务账号在 Google 侧不存在（JSON 被改过 / 账号已删） | 回 Cloud Console 重建服务账号并重新下载密钥 |
| `TypeError: the JSON object must be str, bytes or bytearray, not dict` | `ee.ServiceAccountCredentials(key_data=...)` 传了 dict | 项目代码已修（须传 `json.dumps(...)`）；自建脚本注意同样问题 |
| `AttributeError: 'Credentials' object has no attribute 'with_scopes'` | `with_scopes` 只有服务账号凭据有，个人 OAuth 凭据没有 | 项目代码已修；自建脚本先 `hasattr` 判断 |
| `ProxyError: Tunnel connection failed: 502 Bad Gateway` | **代理节点抖动**（国内代理常见，不是配置错误） | 已由 `app/net_retry.py` 自动退避重试兜住；若仍频繁出现就换个节点 |
| `HttpError 403 ... PERMISSION_DENIED` 但网络正常 | `GEE_PROXY` 没生效，请求走了错误的代理（返回 502 被吞） | 确认 `.env` 的 `GEE_PROXY` 是本机可用端口，用 `--detect-proxy` 复核 |
| `TimeoutError` / `Connection refused` / `Max retries exceeded` | **网络到不了 Google** | 回到第 1 节；或临时切回 `offline` |
| `SSL: CERTIFICATE_VERIFY_FAILED` / `UNEXPECTED_EOF_WHILE_READING` | 代理做了 TLS 中间人，或节点抖动中断连接 | 换 TUN/全局模式；抖动类已被重试兜住 |
| `沙箱未返回结构化结果` | 生成的代码崩了或超时 | 看任务详情里的日志与 traceback，点重试会触发自动修复 |
| `ImportError: 沙箱安全策略禁止导入模块：shutil/msvcrt/signal` | **沙箱黑名单误伤了工具链依赖**（不是用户代码的问题） | 见第 6 节：改为「封能力」而非「封模块」；跑 `--self-test` 复核 |
| `TypeError: function() argument 'code' must be code, not str` | 沙箱把 `subprocess.Popen` 换成了函数，而 `asyncio` 要继承它 | 见第 6 节：封 `Popen` 必须用**类**，不能用函数 |
| `沙箱网络白名单拒绝访问：xxx` | 沙箱策略拦截了非 Google 域名 | 属正常防护（见第 6 节），无需处理 |
| 地图空白、无瓦片 | 代码没调用 `WB.add_image_layer(...)` | 在需求里补一句「请用 add_image_layer 输出图层」，或直接在页面重试 |
| `EEException: User memory limit exceeded` | AOI 过大 / scale 过细 / 对全年影像整体 `limit()` 后仍全量参与运算 | 已在 `app/codegen.py` 的 `_RESOURCE_RULES` 中硬约束（AOI ≤0.5°、scale ≥250、`tileScale=16`、`bestEffort=True`） |
| `Image.normalizedDifference: No band named 'B8'. Available band names: []` | 该时段筛选后**影像集合为空**，`median()` 返回空影像。长三角 4 月/6 月在 60% 云量下都可能一景都没有 | 逐月**不要硬套云量阈值**，改为 `.sort('CLOUDY_PIXEL_PERCENTAGE').limit(6)` 取最晴的几景，并用 `ee.Algorithms.If(size().gt(0), ..., None)` 兜底 |
| 逐月曲线出现一串 `0` | 硬套云量阈值导致该月无影像，代码把 `None` 静默写成了 `0` | 同上；`None` 必须原样保留，否则等于伪造数据 |
| 任务一直 `running`、日志不更新 | 任务状态只在收尾时写回，执行期间无进度回写属正常；若超过 `GEE_TIMEOUT` 仍未结束会被杀 | 等一轮超时；或查 `backend/logs/backend.log` |
| 地图面板只有底图、没有数据图层 | 前端没渲染 `tile_url`（真实 GEE 返回的是瓦片模板，不是 geojson） | 已修：`MapPanel` 支持 `tile_url`；若仍空白见下一行 |
| 地图**整块空白**（连底图都没有） | 浏览器直连 `earthengine-highvolume.googleapis.com` 与 `tile.openstreetmap.org` 都是 **HTTP 000** | 必须走后端代理：`/api/tile` + `/api/basemap`（见 README） |
| 瓦片接口 400「域名不在白名单」 | 属预期防护（只放行 Google 域名） | 无需处理；确需别的源就改 `tile_proxy.ALLOWED_HOST_SUFFIXES` |
| 瓦片接口 502 | 上游 5xx 且重试 3 次仍失败（代理节点抖动） | 换节点；瓦片是逐张拉的，偶发失败 Leaflet 会重试 |

---

## 6. 沙箱安全边界（任务书「受限沙箱」的落地口径）

真实 GEE 执行**不在服务主进程里跑代码**，而是在独立子进程中执行，并施加以下限制：

| 限制项 | 实现 |
|---|---|
| 进程隔离 | 独立子进程执行 `exec(code)`，崩溃不影响 API 服务 |
| 超时控制 | `GEE_TIMEOUT`（默认 420s），超时直接杀进程，返回 `category=timeout`。真实 GEE 云端合成+统计常需数分钟，120s 会稳定误杀 |
| 模块黑名单 | `ctypes` / `multiprocessing` / `http.server` / `pty` / `telnetlib` / `smtplib` / `pickle` / `winreg` / `code` / `codeop` |
| **能力封禁（进程）** | `subprocess.Popen.run/call/...`、`os.system/popen/exec*/spawn*`、`_winapi.CreateProcess` 一律抛 `PermissionError` |
| 网络白名单 | **在 socket 层拦截**：只放行 `*.googleapis.com` / `*.google.com` / `*.gstatic.com` / `*.googleusercontent.com` 与 `localhost`（供代理） |
| 资源限额 | 递归深度限 3000；用户 stdout 截断至 20000 字符 |
| 结果契约 | 代码只能通过 `WB` 报告器回传图层/图表/统计，不回传任意对象 |

### 关键设计原则：**封能力，不要封模块**

这是踩了两个致命坑之后才纠正过来的。模块黑名单看着直观，但 stdlib 与第三方库之间
的 import 关系是隐式的 —— 拉黑一个模块往往是「连带打死自己」，而且报错发生在很下游：

| 曾拉黑的模块 | 谁在 import 它 | 后果 |
|---|---|---|
| `shutil` | Python 3.13 的 `tempfile`；`dotenv` 依赖 `tempfile` | `import app.config` 直接炸 → **真实执行通道 100% 不可用** |
| `subprocess` | `google.auth.transport._mtls_helper`（**模块级**） | google-auth 炸 → 同上 |
| `msvcrt` / `signal` | `tempfile`(Win) / `subprocess` | 同上 |
| `importlib.machinery` | 导入系统自身 | 同上 |
| `marshal` | 导入系统读 `.pyc` 时使用 | 报 `TypeError: function() argument 'code' must be code, not str` |
| `socket` | `requests` / `urllib3` / `httplib2` | 同上 |

**更阴的一个坑**：`subprocess.Popen` 一开始是被替换成普通函数的，结果
`asyncio/windows_utils.py` 里有 `class Popen(subprocess.Popen):` —— 它在**类定义阶段**
继承这个属性。换成函数后整个 `asyncio` 导入失败，进而打死 google-auth
（而报错信息是完全不相干的 `TypeError: function() argument 'code' must be code`）。
所以封禁 `Popen` 必须用**类**（`__init__` 里抛错），不能用函数。

因此现在的规则是：
- **模块黑名单**：只放「实测确认工具链不需要」的模块；
- **危险能力**：用运行时打补丁封禁（socket 域名白名单、进程创建封禁）。

而且**校验是自动的**：沙箱每次启动都会跑 `_verify_toolchain()`，逐个导入
`app.config` / `app.gee_auth` / `ee` / `requests` / `google.auth.transport.requests`，
一旦被黑名单误伤就立刻给出明确报错，而不是等到跑任务时报一句莫名其妙的 ImportError。

自检（不需要凭据、不联网）：

```bash
# 链路 + 安全策略 + 工具链完整性
.venv\Scripts\python.exe tools\gee_doctor.py --self-test

# 在真实沙箱子进程里跑一次 GEE 鉴权（验证生产路径每一层）
.venv\Scripts\python.exe tools\gee_doctor.py --probe-sandbox
```

> 为什么要有 `--probe-sandbox`：前 5 个环节都是在**主进程**里验证的，而真实执行发生在
> **子进程**里。上面的 `shutil` 坑正好只在这个差值上暴露 —— 主进程一切正常，
> 子进程全崩。这条命令就是专门打这个差值的。

---

## 7. 一页速查（全部命令）

```bash
cd C:\Users\Administrator\WorkBuddy\2026-09-15-10-21-57\gee-assistant\backend

# 1) 全链路自检：依赖 / 网络（走代理）/ 凭据 / 鉴权 / 真实调用
.venv\Scripts\python.exe tools\gee_doctor.py

# 1.5) 自动发现可用的本地代理（换工具/改端口时用）
.venv\Scripts\python.exe tools\gee_doctor.py --detect-proxy

# 1.8) 指定并校验项目 ID，自动写回 .env（推荐路径，个人 OAuth 用户必用）
.venv\Scripts\python.exe tools\gee_doctor.py --set-project 你的项目ID

# 1.9) 尝试自动探测项目 ID（服务账号可用；OAuth 用户会因 API 未启用而跳过，属正常）
.venv\Scripts\python.exe tools\gee_doctor.py --detect-project

# 2) 不需要凭据，验证沙箱与结果契约链路是否通
.venv\Scripts\python.exe tools\gee_doctor.py --self-test

# 2.5) 在真实沙箱子进程里跑一次 GEE 鉴权（验证生产路径每一层）
.venv\Scripts\python.exe tools\gee_doctor.py --probe-sandbox

# 3) 导入服务账号 JSON（自动加密落盘）
.venv\Scripts\python.exe tools\gee_doctor.py --import-sa "C:\路径\key.json"

# 3.5) 回退：删除加密凭据库，落回个人 OAuth（服务账号抢了优先级导致不可用时用）
.venv\Scripts\python.exe tools\gee_doctor.py --drop-sa

# 4) 个人账号 OAuth 登录
.venv\Scripts\python.exe tools\gee_doctor.py --oauth

# 5) 查询运行中的服务当前识别到的凭据状态（该接口需登录，见下方说明）
curl -H "Authorization: Bearer <登录后拿到的 token>" http://127.0.0.1:8010/api/gee/status
```

> **`--import-sa` 与 `--drop-sa` 是一对可逆开关**，来回切换都幂等，可以放心试。
> 记住 `resolve()` 的优先级是 **服务账号 > 加密凭据库 > 个人 OAuth**，
> 所以只要 `gee.enc` 存在，个人 OAuth 就永远轮不到 —— 服务账号一旦验证失败，
> 第一件事就是 `--drop-sa` 把它摘掉，而不是去怀疑 OAuth 配置。
> 两条命令都会在动文件**之前**打印一行「当前生效的凭据」，出错时照着那行判断即可。

---

## 8. 如果暂时拿不到凭据，项目还能交付什么

这不是"降级"，而是任务书允许的正式交付形态。当前 `offline` 后端下这些能力全部可用：

- 自然语言 → 意图解析（含多轮澄清）→ 多目标任务自动拆解
- DeepSeek 真实生成 GEE Python 代码（代码可查看、可存档、可导出报告）
- 4 类任务模板：NDVI / 水体提取 / 地表分类 / 时序变化检测
- 地图渲染（Leaflet）+ 统计图表（ECharts）+ 图层图例
- Ask（专家问答）/ Do（代码执行）双模式、Agent Profiles、示例 prompt、数据集知识库检索
- 任务历史、SQLite 持久化、Markdown 报告导出
- cpolar 公网访问

凭据就绪后，**只需在 `.env` 改一行 `EXECUTION_BACKEND=gee` 并重启**，离线样例结果即切换为真实 GEE 计算结果，其余功能零改动。
