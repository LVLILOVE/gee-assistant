# 卫星遥感影像智能分析助手 · API 文档

- 基础地址：`http://127.0.0.1:8010`（生产模式）或 `http://localhost:5173`（开发模式代理）
- 数据格式：JSON（请求与响应均为 UTF-8 JSON）
- 说明：任务提交为异步，提交后需轮询进度

## 接口总览

> **鉴权说明**：服务已部署到公网，除下表中标注「公开」的接口外，其余全部需要登录。
> 未登录访问会返回 **401**，响应体为 `{"detail": "请先登录后再操作"}`。
> 会话通过 `gee_session` cookie 传递（HttpOnly），也支持 `Authorization: Bearer <token>`
> 以便脚本与自动化调用。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | 健康检查 |
| GET | `/api/auth/me` | 公开 | 当前登录状态 |
| POST | `/api/auth/register` | 公开 | 注册 |
| POST | `/api/auth/login` | 公开 | 登录 |
| POST | `/api/auth/logout` | 公开 | 注销 |
| POST | `/api/auth/password` | 需登录 | 修改密码 |
| GET | `/api/tasks/types` | 公开 | 任务类型模板 |
| GET | `/api/regions` | 公开 | 常用区域列表 |
| GET | `/api/knowledge` | 公开 | 数据集知识库全量 |
| GET | `/api/knowledge/search` | 公开 | 数据集检索 |
| GET | `/api/tile/{z}/{x}/{y}?u=` | 公开 | **GEE 栅格瓦片代理**（浏览器直连 Google 不通） |
| GET | `/api/basemap/{z}/{x}/{y}` | 公开 | **地图底图代理**（浏览器直连 OSM 不通） |
| GET | `/api/gee/status` | 需登录 | GEE 凭据与连通性诊断（凭据脱敏） |
| POST | `/api/regions` | 需登录 | 保存自定义区域 |
| DELETE | `/api/regions/{name}` | 需登录 | 删除自定义区域 |
| POST | `/api/parse` | 需登录 | 意图解析 + 多轮澄清 |
| POST | `/api/ask` | 需登录 | 专家问答（Ask 模式） |
| POST | `/api/plan` | 需登录 | 多目标任务拆解 |
| GET | `/api/profiles` | 需登录 | 助手画像列表 |
| GET | `/api/preferences` | 需登录 | 读取分析偏好 |
| PUT | `/api/preferences` | 需登录 | 保存分析偏好 |
| POST | `/api/tasks` | 需登录 | 提交分析任务 |
| GET | `/api/tasks` | 需登录 | 历史任务列表 |
| GET | `/api/tasks/{id}` | 需登录 | 任务进度与结果 |
| GET | `/api/tasks/{id}/report` | 需登录 | 导出 Markdown 报告 |

**为什么瓦片代理不鉴权**：它只转发影像，而 GEE 瓦片地址中的 `mapid` 是随机哈希，
不通过受保护的 `/api/tasks` 拿不到，也就无从构造。底图则是纯 OSM 中继，没有私有数据。
真正会消耗额度的是**任务提交与代码生成**，这两条已全部纳入鉴权。

### 多用户隔离（重要）

`/api/tasks`、`/api/tasks/{id}`、`/api/tasks/{id}/report`、`/api/regions`、`/api/preferences`、
`/api/profiles` 这 7 条接口返回的都是**当前登录用户自己的数据**，不会有别人的内容混进来。

| 场景 | 行为 |
|---|---|
| `GET /api/tasks` | 只列自己的任务；`user_id` 由服务端从会话写入，客户端传了也被忽略 |
| `GET /api/tasks/{id}`（别人的） | **404**，不是 403 |
| `GET /api/tasks/{id}/report`（别人的） | **404** |
| `DELETE /api/regions/{name}`（别人的） | **404**，且对方区域不会被删掉 |
| `POST /api/regions`（名字与别人相同） | 200，各自独立存储（联合主键 `(name, user_id)`），**不会覆盖对方的坐标** |
| `GET /api/regions`（未登录） | 200，但**只返回 13 个内置区域**，不含任何人的自定义区域 |
| `GET /api/preferences`（未登录） | 401 |

> **为什么越权返 404 而不是 403**：403 等于承认"这个任务 ID 确实存在"，
> 会把别人的任务 ID 空间变成可逐位探测的信息泄漏面。404 让"不存在"与"不是你的"不可区分。

鉴权关闭时（`AUTH_ENABLED=false`）上述过滤全部取消，行为回到单机自用的原样。

---

## POST /api/auth/register

注册并自动登录。

**请求**

```json
{ "username": "your_name", "password": "at-least-8-chars" }
```

**约束**

| 项 | 规则 |
|---|---|
| 用户名 | 3–32 位，中英文、数字、`_` `.` `-` |
| 密码 | 至少 8 位，最长 128 位 |
| 唯一性 | 用户名重复返回 409 |
| 首个用户 | 数据库为空时，第一个注册的用户自动成为管理员 |

**响应** `200`

```json
{ "ok": true, "username": "your_name", "is_admin": true }
```

同时下发 `Set-Cookie: gee_session=...; HttpOnly; SameSite=Lax; Path=/`。

**失败**

| 状态码 | 场景 |
|---|---|
| 400 | 用户名或密码不符合规则（`detail` 给出具体原因） |
| 403 | 已关闭自助注册（`ALLOW_REGISTRATION=false` 且已有用户） |
| 409 | 用户名已被占用 |

---

## POST /api/auth/login

**请求**

```json
{ "username": "your_name", "password": "your_password" }
```

**响应** `200`：同注册，并下发会话 cookie。

**失败**

| 状态码 | 场景 |
|---|---|
| 401 | 用户名或密码不正确 |
| 429 | 连续失败 5 次后被临时锁定 60 秒 |

> 安全设计：用户不存在与密码错误返回**完全相同**的提示，避免被用来枚举账号。
> 密码比较使用 `hmac.compare_digest`，防时序侧信道。

---

## POST /api/auth/logout

注销当前会话，并清除 cookie。**无需登录**（未登录时也返回 `200`，方便前端无脑调用）。

---

## POST /api/auth/password

修改当前用户密码。**需登录**。

**请求**

```json
{ "old_password": "旧密码", "new_password": "新密码至少8位" }
```

**响应** `200`

```json
{ "ok": true, "message": "密码已更新，请重新登录" }
```

> 修改成功后，该用户的**全部会话立即失效**（含当前会话），需重新登录。
> 这样即使旧会话已经泄露，也无法继续使用。

**失败**：`401` 原密码不正确 / `400` 新密码不足 8 位。

---

## GET /api/auth/me

查询当前登录状态。**公开**。

**响应示例（已登录）**

```json
{ "auth_enabled": true, "authenticated": true, "username": "your_name",
  "is_admin": true, "allow_registration": true }
```

**响应示例（未登录）**

```json
{ "auth_enabled": true, "authenticated": false, "username": "",
  "is_admin": false, "allow_registration": true }
```

> 未登录时刻意返回 **200 而不是 401**：前端每次进入都要问一次，
> 返 401 会在浏览器控制台刷一片红色报错，把真正的错误淹掉。

---

## GET /health

健康检查。

**响应示例**

```json
{"status":"ok","backend":"offline","llm":"deepseek-chat","has_key":true}
```

| 字段 | 说明 |
|---|---|
| status | ok / 异常 |
| backend | 执行后端类型（offline / gee） |
| llm | LLM 模型名 |
| has_key | 是否已配置 DeepSeek key |

---

## GET /api/tile/{z}/{x}/{y}?u=

**GEE 栅格瓦片代理**。真实 Earth Engine 返回的图层是瓦片模板，但
`earthengine-highvolume.googleapis.com` 在国内**浏览器直连不通**（实测 HTTP 000），
必须由后端经本机代理转发，前端不需要能访问 Google。

**参数**

| 参数 | 位置 | 说明 |
|---|---|---|
| z / x / y | 路径 | 瓦片坐标，必须为纯数字 |
| u | query | 瓦片模板去掉 `/{z}/{x}/{y}` 后的地址（前端 `encodeURIComponent`） |

前端用法（Leaflet）：

```js
const base = tileUrl.replace(/\/\{z\}\/\{x\}\/\{y\}\/?$/, '')
L.tileLayer(`/api/tile/{z}/{x}/{y}?u=${encodeURIComponent(base)}`)
```

**响应**：`image/jpeg` 或 `image/png`（带 `Cache-Control: max-age=86400`）

**安全边界**（避免退化成开放 SSRF 代理）：仅允许 https、仅允许
`*.googleapis.com` / `*.google.com`、坐标必须为数字、单张上限 4MB。
非白名单域名返回 **400**；上游 5xx 重试 3 次后返回 **502**。

---

## GET /api/basemap/{z}/{x}/{y}

**地图底图代理**。`tile.openstreetmap.org` 在国内同样是浏览器直连不通（实测 HTTP 000），
底图空白会让整个地图面板失效。

与 GEE 瓦片代理不同，这里的 URL **完全由后端拼装**，客户端只能给 z/x/y，不存在 SSRF 面。
请求会带上符合 OSM 使用政策的 User-Agent（否则会被 403）。

**响应**：`image/png`（`Cache-Control: max-age=604800`）；
远洋等无数据区域上游返回 404 → 本接口返回 **204**；坐标非法返回 **400**。

---

## GET /api/tasks/types

返回四类任务模板。

**响应示例**

```json
[
  {"value":"ndvi","label":"植被指数 NDVI 计算"},
  {"value":"water","label":"水体提取"},
  {"value":"classification","label":"地表分类"},
  {"value":"change_detection","label":"时序变化检测"}
]
```

---

## GET /api/regions

返回常用区域列表（内置 + **自己保存的**自定义区域）。公开接口。

未登录时仍返回 200，但 `regions` 里**只有内置的 13 个**。

**响应示例**

```json
{"regions":[
  {"name":"太湖流域","lon":120.13,"lat":31.2,"desc":"长江三角洲主要湖泊","builtin":true},
  {"name":"我的研究区","lon":108.0,"lat":34.5,"desc":"","builtin":false}
]}
```

---

## POST /api/regions

保存自定义常用区域（同一用户下同名覆盖）。区域归当前登录用户所有。

**请求体**

```json
{"name":"我的研究区","lon":108.0,"lat":34.5,"desc":"自定义区域"}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| name | string | ✅ | 区域名 |
| lon | number | ✅ | 中心经度 |
| lat | number | ✅ | 中心纬度 |
| desc | string | ❌ | 描述 |

**响应示例**

```json
{"ok":true,"name":"我的研究区"}
```

> 区域名在**同一用户内唯一**（主键是 `(name, user_id)` 联合主键）：
> 同一用户重复保存同名区域是"更新坐标"，不会插出重复行；
> **不同用户可以各有同名区域，互不可见、互不影响** —— B 保存一个和 A 同名的区域
> 不会顶掉 A 的坐标（旧结构下会，属于跨用户数据破坏）。

---

## DELETE /api/regions/{name}

删除**自己名下**的自定义区域（name 需 URL 编码）。

删别人的或不存在的一律返回 **404 `区域不存在`**，两种情况不作区分。
因为存在联合主键，`AND user_id = ?` 条件是必需的 —— 缺了它会把所有同名区域一起删掉。

**响应示例**

```json
{"ok":true}
```

---

## POST /api/parse

意图解析 + 多轮澄清。`partial` 携带已确认参数，实现多轮对话。

**请求体**

```json
{"text":"帮我分析太湖流域 6-8 月的植被状况","partial":{}}
```

**响应示例（参数齐全）**

```json
{
  "source":"deepseek",
  "fields":{"task_type":"ndvi","region":"太湖流域","start_date":"2025-06-01","end_date":"2025-08-31","cloud_threshold":20.0},
  "missing":[],
  "question":null,
  "complete":true,
  "summary":"任务类型：植被指数（NDVI），分析区域：太湖流域，时间范围：2025-06-01 ~ 2025-08-31，云量阈值：20.0%"
}
```

**响应示例（缺任务类型）**

```json
{
  "source":"deepseek",
  "fields":{"task_type":null,"region":"洞庭湖","start_date":"2024-01-01","end_date":"2024-12-31","cloud_threshold":20.0},
  "missing":["task_type"],
  "question":"请问你想做哪类分析？可选：植被指数(NDVI)、水体提取、地表分类、时序变化检测。",
  "complete":false,
  "summary":"任务类型：未指定，分析区域：洞庭湖，时间范围：2024-01-01 ~ 2024-12-31，云量阈值：20.0%"
}
```

| 字段 | 说明 |
|---|---|
| source | 解析来源（deepseek / rule / empty） |
| fields | 结构化参数（task_type / region / start_date / end_date / cloud_threshold） |
| missing | 缺失的必填字段（task_type 或 region） |
| question | 澄清问题（complete 为 false 时） |
| complete | 参数是否齐全（true 可直接提交任务） |
| summary | 解析结果的人类可读摘要 |

---

## POST /api/tasks

提交分析任务（异步，立即返回 task_id）。

**请求体**

```json
{"task_type":"ndvi","region":"太湖流域","start_date":"2025-06-01","end_date":"2025-08-31","cloud_threshold":20.0}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| task_type | string | ✅ | ndvi / water / classification / change_detection |
| region | string | ✅ | 分析区域描述 |
| start_date | string | ❌ | 起始日期 YYYY-MM-DD（默认 2024-01-01） |
| end_date | string | ❌ | 结束日期 YYYY-MM-DD（默认 2024-12-31） |
| cloud_threshold | number | ❌ | 云量阈值 %（默认 20） |

> 请求体里**没有** `user_id`：归属由服务端从会话写入。即使客户端多传一个 `user_id` 也会被忽略，
> 所以无法通过伪造请求体把任务挂到别人名下。

**响应示例**

```json
{"task_id":"09042306d83c"}
```

**配额限制（重要）**

登录只挡住「没账号的人」。服务在公网且允许自助注册，所以提交口还有四层额度，
任一超限返回 **`429`**，并带 `Retry-After`（秒）：

| 维度 | 默认 | 触发时的提示 | `Retry-After` |
|---|---|---|---|
| 全局并发 | 3 | 服务繁忙，请稍后重试 | 30 |
| 单用户并发 | 2 | 已达单用户并发上限（2），请等待当前任务完成 | 30 |
| 单用户每小时 | 20 | 已达每小时提交上限（20），请稍后重试 | 3600 |
| 单用户每天 | 60 | 已达每天提交上限（60），请明天再试 | 86400 |

响应形如：

```json
{"detail":"已达单用户并发上限（2），请等待当前任务完成"}
```

几个要点：

- **额度按库里的任务记录统计**，不是内存计数器 —— 重启服务不会清零，重新部署也绕不过。
- **校验与建任务在同一把锁里**。分开写会让两个并发提交同时读到"还差一个名额"然后都放行。
- **服务重启时会清理僵尸任务**：上个进程遗留的 `pending`/`running` 会被标记为
  `failed` 并在日志里写明「服务重启」。不清的话它们会**永久占住并发名额**，
  用户再也提交不了新任务。
- 额度可通过 `MAX_CONCURRENT_TASKS` / `MAX_CONCURRENT_TASKS_PER_USER` /
  `TASK_RATE_LIMIT_PER_HOUR` / `TASK_RATE_LIMIT_PER_DAY` 调整，设 `0` 表示该维度不限制。

> 前端会把 `detail` 原文显示给用户，而不是笼统的 "Request failed with status code 429"。

---

## GET /api/tasks

**自己的**历史任务列表（按创建时间倒序，最多 50 条）。

**响应示例**

```json
{"tasks":[
  {"task_id":"09042306d83c","status":"succeeded","task_type":"ndvi","region":"太湖流域","created_at":1789460000,"attempts":1,"user_id":1}
]}
```

---

## GET /api/tasks/{id}

任务进度与结果。**只能查自己的任务**；别人的或不存在的一律返回
**404 `任务不存在`**（两种情况响应一致，不泄露任务 ID 是否存在）。

**响应示例（成功）**

```json
{
  "task_id":"09042306d83c",
  "status":"succeeded",
  "attempts":1,
  "code":"import ee\n...",
  "logs":["[生成] 代码来源=deepseek","[执行] 第 1 次执行成功"],
  "result":{
    "ok":true,
    "layers":[{"name":"NDVI 空间分布","kind":"geojson","geojson":{...},"legend":[...]}],
    "charts":[{"title":"太湖流域 逐月 NDVI 均值","kind":"line","labels":[...],"series":[...]}],
    "stdout":"[离线] NDVI 计算完成，区域=太湖流域"
  }
}
```

| 字段 | 说明 |
|---|---|
| status | pending / running / succeeded / failed |
| attempts | 尝试次数 |
| code | 生成（或修复后）的 GEE 代码 |
| logs | 执行日志 |
| result.ok | 是否成功 |
| result.layers | 图层数组（name / geojson / legend） |
| result.charts | 图表数组（title / kind / labels / series） |
| result.error | 失败时返回（category / message） |

---

## GET /api/tasks/{id}/report

导出 Markdown 分析报告（触发下载）。只能导出**自己的**任务，否则 404。

**响应**：`Content-Disposition: attachment; filename=report_{id}.md`，`Content-Type: text/markdown`。

报告包含：任务参数、生成代码、结果图层、统计图表、执行日志、AI 免责声明。

---

## GET /api/preferences

读取**自己的**分析偏好。需登录。

**响应示例**

```json
{"preferences":{
  "instructions":"优先用 Cloud Score+ 做云掩膜",
  "data_source":"auto",
  "output_language":"zh",
  "agent_profile":"analyst"
}}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| instructions | string | 自由文本自定义指令，会注入代码生成 prompt |
| data_source | `auto` / `sentinel2` / `landsat` | 数据源偏好 |
| output_language | string | 代码注释语言 |
| agent_profile | `analyst` / `mentor` / `concise` | 助手画像 |

> 用户没设过的字段会继承**全局默认值**；设过的只认自己的，不受别人改动影响。

---

## PUT /api/preferences

合并写入**自己的**偏好（只传要改的字段）。

**请求体**

```json
{"agent_profile":"mentor"}
```

**响应示例**：同 `GET`，返回合并后的完整偏好。

---

## GET /api/profiles

助手画像列表，`current` 为**当前用户**选中的画像。需登录。

**响应示例**

```json
{"current":"mentor","profiles":[
  {"value":"analyst","label":"分析师（默认）","desc":"专业严谨、结论清晰，适合日常分析"},
  {"value":"mentor","label":"导师","desc":"解释详尽、教学导向，适合学习"},
  {"value":"concise","label":"极简","desc":"只给结论、不废话，适合快速查询"}
]}
```

> 画像会影响两处：问答的 `system` 提示词（`POST /api/ask`）与代码生成风格
> （`POST /api/tasks`，通过后台线程按任务归属用户的画像生成）。
