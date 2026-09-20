# 卫星遥感影像智能分析助手

对话式遥感分析 WebApp：用一句中文描述需求，系统自动解析意图（多轮澄清）、生成 GEE 代码、在受控后端执行并返回地图与统计图表；执行出错时自动修复重试。

技术栈（严格对齐任务书）：Python 3.12+ / LangGraph / FastAPI / React / sqlite3 / Leaflet / ECharts / Ant Design，LLM 使用 DeepSeek（OpenAI 兼容、国内直连）。

## 目录结构

```
gee-assistant/
├── backend/            # FastAPI 后端
│   ├── app/
│   │   ├── main.py         # 路由入口（任务/意图解析/报告/静态托管）
│   │   ├── intent.py       # 自然语言意图解析 + 多轮澄清
│   │   ├── regions.py      # 常用区域坐标库
│   │   ├── orchestrator.py # LangGraph 编排（生成→执行→调试循环）
│   │   ├── codegen.py      # DeepSeek 代码生成
│   │   ├── debugger.py     # 错误分类与修复
│   │   ├── models.py       # 数据模型 / I/O 契约
│   │   ├── execution/      # 可插拔执行后端
│   │   │   ├── base.py     #   抽象契约
│   │   │   ├── offline.py  #   离线兜底后端（无需 GEE）
│   │   │   └── gee.py      #   真实 GEE 后端（已接入，沙箱子进程执行）
│   │   └── task_store.py   # sqlite3 持久化（backend/data/app.db）
│   ├── requirements.txt
│   ├── .env.example
│   └── run.py
├── frontend/           # Vite + React + AntD + Leaflet + ECharts
│   └── src/
│       ├── App.jsx    # 对话式输入 + 地图 + 图表 + 历史任务
│       └── api.js
└── docs/               # 交付文档
    ├── PRD.md          # 产品需求文档（借鉴 earth-agent-chrome-ext）
    ├── UI-UX设计.md     # UI/UX 设计文档
    ├── 数据库设计.md     # 数据库设计文档
    ├── API文档.md       # 接口文档
    ├── 测试报告.md      # 测试报告
    ├── 部署手册.md      # 部署运维手册
    ├── GEE凭据配置指南.md # GEE 服务账号/OAuth 凭据创建与导入（保姆级）
    ├── AI协作教程.md    # AI 协作开发教程
    └── RAG语料库.md     # GEE 数据集语料 + 提示词模板库
```

## 快速开始

### 1. 后端

```bash
cd backend
.venv/Scripts/python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
cp .env.example .env   # 填入 DEEPSEEK_API_KEY（不配则用规则兜底）
.venv/Scripts/python.exe run.py   # http://127.0.0.1:8010
```

### 2. 前端（开发模式）

```bash
cd frontend
npm install --registry=https://registry.npmmirror.com
npm run dev            # http://localhost:5173（已代理 /api 到 8010）
```

### 3. 生产模式（单端口对外）

```bash
cd frontend && npm run build   # 产出 dist/
# 重启后端后，FastAPI 自动托管 dist：http://127.0.0.1:8010 即完整应用
```

### 3.5 启用真实 GEE 执行（✅ 已接入）

> **当前状态（2026-09-16）**：三项前提全部就位，`EXECUTION_BACKEND=gee` 已切换，
> 项目 ID `weixing-508708`（个人 OAuth 凭据）。真实任务端到端跑通 ——
> 太湖流域 2024 年 NDVI 任务返回 1 个真实 GEE 瓦片图层 + 12 个逐月真实统计值。
> 不想用离线兜底时，直接 `curl -X POST .../api/tasks` 提交任务即可走真实 GEE。

若要在新机器上重新接入，按下面步骤走（每步都会做**真实校验**，验证通过才写配置）：

```bash
cd backend
.venv/Scripts/python.exe tools/gee_doctor.py            # ① 自检：依赖/网络/凭据/鉴权/真实调用
.venv/Scripts/python.exe tools/gee_doctor.py --self-test  #    无需凭据，验证沙箱链路是否正常
.venv/Scripts/python.exe tools/gee_doctor.py --detect-proxy    # 自动发现本机可用代理
.venv/Scripts/python.exe tools/gee_doctor.py --oauth      # ② 个人账号授权（最快，3 分钟）
.venv/Scripts/python.exe tools/gee_doctor.py --import-sa "C:\路径\key.json"  # 或：导入服务账号（自动加密 + 当场验证）
.venv/Scripts/python.exe tools/gee_doctor.py --set-project 你的项目ID  # ③ 指定已注册 EE 的项目（自动写回 .env）
.venv/Scripts/python.exe tools/verify_real_gee.py 太湖流域  # ④ 不依赖大模型，验证真实 GEE 链路
# ⑤ 重启服务即切换为真实 Earth Engine（--set-project 已顺手把 EXECUTION_BACKEND 改为 gee）
```

> 三个前提缺一不可：**网络能到 Google** → **有凭据** → **有已注册 Earth Engine 的云项目**。
> 第三步最易被忽略，项目未注册会报 `Not signed up for Earth Engine or project is not registered`。
> 详见 `docs/GEE凭据配置指南.md`。

> 本机直连 Google 不通，已通过 `GEE_PROXY=http://127.0.0.1:7897`（本机 Clash 混合端口）打通，
> GEE 计算端点实测可达；代理节点抖动由 `app/net_retry.py` 自动退避重试兜住。

凭据创建步骤见 [docs/GEE凭据配置指南.md](docs/GEE凭据配置指南.md)。
页面右上角「GEE · 查看」标签可随时查看当前接入状态，未配置时会直接给出补配置命令。
接口：`GET /api/gee/status`（凭据脱敏，不返回密钥）。

> **地图瓦片为什么要走后端代理？**
> 实测国内浏览器直连这两个域名都是 **HTTP 000**（TCP 层就失败）：
> GEE 栅格瓦片 `earthengine-highvolume.googleapis.com`、底图 `tile.openstreetmap.org`。
> 所以地图面板会一片空白。解决办法是后端已有的代理通道替浏览器去取：
> `GET /api/tile/{z}/{x}/{y}?u=`（GEE 栅格）与 `GET /api/basemap/{z}/{x}/{y}`（底图）。
> 好处是**前端完全不依赖能否访问外网**，本地与公网映射场景都成立。
> 两个端点都做了安全约束（域名白名单 / 坐标校验 / 体积上限），不会变成开放代理。

> **真实任务调参提醒**：`GEE_TIMEOUT` 默认 420s（真实云端合成+统计常需数分钟）；
> 逐月统计不要硬套云量阈值 —— 长三角 4 月/6 月可能整月没有低云影像，
> 应改为 `.sort('CLOUDY_PIXEL_PERCENTAGE').limit(6)` 取最晴的几景。细节见配置指南第 5、6 节。

### 4. 对外发布（cpolar）

```bash
# 注意：需绕过本地沙箱代理直连（否则 cpolar 连隧道端口会 502）
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
  cpolar http 8010 -region cn_top
# 输出形如 https://xxxx.r31.cpolar.top 的公网地址，外网可直接访问
# 当前公网地址：https://19f0a78b.r31.cpolar.top（免费版随机子域名，重启会变）
```

## 核心接口

> 除标注「公开」外均需登录；未登录返回 `401`。会话走 `gee_session` HttpOnly cookie，
> 也支持 `Authorization: Bearer <token>` 便于脚本调用。

- **鉴权**：`POST /api/auth/register`｜`login`｜`logout`｜`password`（改密），`GET /api/auth/me`（公开）
- `GET /health` → 后端状态、执行后端类型、LLM 是否配置（公开）
- `GET /api/tasks/types` → 四类任务模板（公开）
- `GET /api/regions` → 常用区域列表（公开）；`POST /api/regions` 保存自定义；`DELETE /api/regions/{name}` 删除
- `POST /api/parse` → 自然语言意图解析（入参 `{text, partial}`，返回 `{fields, missing, question, complete}`）
- `POST /api/plan` → 规划推理：多目标需求拆解（入参 `{text}`，返回 `{multi, tasks, source}`）
- `POST /api/ask` → 咨询模式专家问答（入参 `{question, history}`）
- `POST /api/tasks` → 提交分析（异步），`GET /api/tasks/{id}` → 轮询进度与结果
- `GET /api/tasks` → 历史任务列表
- `GET /api/tasks/{id}/report` → 导出 Markdown 分析报告
- `GET/PUT /api/preferences` → 分析偏好（数据源 / 自定义指令 / 助手画像）
- `GET /api/profiles` → 助手画像列表
- `GET /api/knowledge` → GEE 数据集知识库（公开）；`GET /api/knowledge/search?q=&task_type=` → 检索
- `GET /api/tile/{z}/{x}/{y}?u=` / `GET /api/basemap/{z}/{x}/{y}` → 瓦片代理（公开，见下方说明）

## 关键设计

- **访问控制**：服务在公网，除静态数据与瓦片代理外全部接口需登录。密码用标准库
  PBKDF2-HMAC-SHA256（20 万次迭代 + 每用户独立盐）；会话用**服务端 token 表**而非 JWT，
  这样注销与改密码能**立即吊销**。登录失败 5 次锁定 60 秒，且"用户不存在"与"密码错误"
  返回完全相同的提示，避免被用来枚举账号。可用 `.env` 的 `ADMIN_USERNAME`/`ADMIN_PASSWORD`
  幂等预置管理员，消除"公网上谁先注册谁是管理员"的竞态。
- **多用户数据隔离**：`tasks` / `regions` / `user_settings` 三张表都带 `user_id`
  （统一 `0` = 无主 / 全局默认值），列表只列自己的；按 ID 取详情/报告、删区域时
  不属于自己一律返 **404 而不是 403**（403 等于承认"这个 ID 存在"，会变成可探测的信息泄漏面）。
  `user_id` 由服务端从会话写入，请求体里没有这个字段，客户端无法伪造归属。
  `regions` 用 **`(name, user_id)` 联合主键**，不同用户可各有同名区域而不会互相覆盖。
  旧库启动时自动迁移，老数据一次性划归首个管理员。
- **对话式意图解析**：DeepSeek 把中文需求解析为结构化参数，缺 task_type/region 时返回澄清问题，`partial` 携带已确认参数实现多轮澄清；无 key 时走关键词规则兜底。
- **规划推理**：一句话含多个独立目标（如"水体+植被"）时自动拆成多步子任务串行执行、合并展示。
- **可插拔执行后端**：`EXECUTION_BACKEND=gee`（真实 Earth Engine，当前已启用）或 `offline`（离线样例兜底，无网可用）。
- **I/O 契约**：执行后端统一返回 `ExecutionOutcome`（图层 + 图表 + 错误分类），前端不感知后端差异。
- **自动调试**：错误按「鉴权 / 参数 / 区域 / 算子」分类，非鉴权错误由 DeepSeek 修复后重试，达上限给出人工建议。
- **分析口径固化**：按任务类型把掩膜类别、排序方式、统计方法写死进提示词，
  避免同一请求两次结果不可比（曾实测差 0.64）。
- **检索增强（RAG）**：内置 9 个 GEE 数据集知识库，代码生成时按任务类型自动检索注入，让模型用对数据。
- **助手画像 / 分析偏好**：借鉴 Earth Agent，画像与偏好注入问答与代码生成 prompt。
- **前端按需加载**：`manualChunks` 把 vendor 拆成 react / antd / echarts / leaflet 四块，
  地图与图表用 `React.lazy` + `Suspense` 延迟加载。首屏 JS 852KB（原 2.0MB，-58%），
  echarts（1.0MB）与 leaflet（150KB）只在用户看到任务结果时才下载。
  关键细节：**子库的 CSS 必须跟着组件 import** —— 放在入口文件会让 Vite 把该 chunk 一并
  `modulepreload` 进首屏，懒加载就白做了。
- **任务配额**：鉴权只挡住「没账号的人」，挡不住「注册一个号然后无限提交」。
  四层额度（全局并发 3 / 单用户并发 2 / 每小时 20 / 每天 60），超限返 `429` + `Retry-After`。
  额度**按库里的任务记录统计**而非内存计数，所以重启不清零、重新部署也绕不过；
  **校验与建任务必须放在同一把锁里**，否则两个并发提交会同时读到"还差一个名额"然后都放行。
  启动时还会把上个进程遗留的 `pending`/`running` 标记为失败 —— 否则它们会
  **永久占住并发名额**，用户再也提交不了新任务。
- **CORS 白名单**：生产前端由本服务同源托管，不需要 CORS，所以 `allow_origins`
  只放行开发用的 Vite 端口（`CORS_ORIGINS` 可追加）。此前写成 `["*"]` +
  `allow_credentials=True`，实测**任意 Origin 都被原样回显** —— 靠 Cookie 的
  `SameSite=Lax` 才没被打穿，是典型的"靠另一层挡住所以没出事"的隐患配置。
- **SPA 兜底路由必须防路径穿越**：单端口生产模式下，没匹配上的 GET 会回退到
  `index.html`（支持前端单页路由）。这条兜底曾经只判 `candidate.is_file()` ——
  而 `%2e` 是 `.` 的合法编码，uvicorn 解码后 `full_path` 就是 `../../backend/.env`，
  于是**匿名访客能直接读走 `.env`（含 LLM 密钥）、后端源码和 `data/app.db`（整库）**，
  公网同样打得通。现在 `_safe_dist_file()` 要求 `resolve()` 后仍在 `dist/` 内，
  带 `..` 的路径直接 404，且 `/api/`、`/health` 等前缀未匹配时返 **JSON 404 而不是 HTML**
  （以前打错的接口会拿到 200 + HTML，调用方以为成功、只在解析时炸）。
  回归见 `docs/测试报告.md` 3.19 节。
- **执行沙箱要封文件系统**：沙箱只封了网络（域名白名单）与子进程，**文件系统曾经是敞开的**
  —— 实测生成代码 `open("backend/.env")` 能读到 `DEEPSEEK_API_KEY`、`os.listdir(".")`
  能列出后端目录。而沙箱跑的代码是 LLM 按用户输入生成的、注册又开放，
  等于任何注册用户写一句话就能把密钥取回去（与上一条**同一根因的两个入口**）。
  现在在 `exec(用户代码)` 前装 `_block_sensitive_file_access()`，把
  `builtins.open` / `io.open` / `io.FileIO` / `io.open_code` / `os.open` / `os.stat` /
  `os.lstat` / `os.listdir` / `os.scandir` 逐个换成带敏感路径判定的版本。
  **三个坑**：① 加固点必须在 `_verify_toolchain()` / `_install_ee()` **之后** ——
  `python-dotenv` 导入时就要读 `.env`，放前面会把整条链路打挂；
  ② 必须显式放行 GEE 凭据路径，否则真实初始化失败；
  ③ **同一个「读文件能力」有多条并列入口，必须逐个封** —— 第一版只封了
  `builtins.open`/`io.open`/`os.open` 且 26 项断言全绿，但 `io.FileIO(".env").read()`
  一句就把三道全绕过去了（实测读出密钥与整库）；
  所以测试里专门有一组**对抗性绕过用例**把 8 条入口逐个钉住。见 `docs/测试报告.md` 3.20 节。

## 测试与验证脚本

| 脚本 | 覆盖 | 说明 |
|---|---|---|
| `backend/tools/test_auth.py` | 28 项 | 鉴权逻辑层（哈希、节流、会话、修改密码） |
| `backend/tools/test_auth_http.sh` | 43 项 | 鉴权 HTTP 层，**跑自己的临时实例**（会注册账号，绝不能指向线上） |
| `backend/tools/test_isolation.py` | 80 项 | 多用户隔离逻辑层（跑真实库副本） |
| `backend/tools/test_isolation_http.sh` | 49 项 | 多用户隔离 HTTP 层（独立端口 + 临时库） |
| `backend/tools/test_quota_http.sh` | 27 项 | 任务配额、429 语义、重启清僵尸任务 |
| `backend/tools/test_sa_route.py` | 44 项 | 服务账号凭据路线（加密落盘、导入、回退） |
| `backend/tools/test_static_route.py` | 57 项 | 静态托管安全：**路径穿越**、未知接口 404、前端路由回退 |
| `backend/tools/test_sandbox_fs.py` | 35 项 | 沙箱文件隔离 + **对抗性绕过**（`io.FileIO`/`io.open_code`/`os.stat` 等 8 条入口），正常文件仍可读 |
| `backend/tools/test_no_stale_year.py` | 22 项 | **年代漂移守卫**：默认日期必须随当天推算，且 `app/`、`frontend/src` 里不得再出现写死的日期字面量。含 **C2 段扫描器自检**——注入探针文件验证「真的会失败」，并校验报出的行号准确 |
| `backend/tools/eval_metrics.py` | 4 个指标<br>+ 16 项自检 | **量化指标评测**：把任务书四个指标跑成可复现数字（`--self-test` 自检判定规则是否可信，16 项，纯离线） |
| `backend/tools/smoke_live.sh` | 25 项 | **只读**线上冒烟（含穿越回归），这才是该对生产跑的东西 |

> 另外 `tools/gee_doctor.py --self-test` 有 7 项链路自检（模块黑名单未误伤 / 子进程封禁 /
> 网络白名单 / **敏感文件禁读** / 超时）。**改沙箱后必须跑它** —— 沙箱按「能力」封禁，
> 而 stdlib 内部互相 import，很容易连带打死自己，且只在真实执行时暴露。

> **规矩**：会写库的测试一律跑在自己的临时实例上（`APP_DB_PATH` + 独立端口 + `trap` 清理）；
> 想验证生产部署，只用 `smoke_live.sh` 这种只读脚本。
> 这条规矩是踩出来的 —— 详见 `docs/测试报告.md` 3.18 节。

### 量化指标评测（任务书验收用）

```bash
cd backend
.venv/Scripts/python.exe tools/eval_metrics.py --self-test    # 先自检判定规则是否可信
.venv/Scripts/python.exe tools/eval_metrics.py --intent-only   # 只跑指标一（快，不依赖 GEE）
.venv/Scripts/python.exe tools/eval_metrics.py                 # 全量：13 条意图标注 + 8 条端到端
```

- 测试任务集在 `backend/tools/eval_dataset.py`（时间期望按当天推算，**不写死年份**）。
- 端到端会真实提交任务并调 GEE，约 30-75s/条，且**受任务配额约束**：
  一小时上限 20 条，所以连着跑两遍评测必然撞 429。
  runner 会把配额拒绝识别为「测量条件问题」并等待重试，不计入成功率分母
  （否则测出来的是"我提交得太快"，不是"产品不行"）。
- 报告自动写入 `docs/评测报告-<日期>.md`。
>
> 打穿越用例时**必须带 `--path-as-is`**：curl 默认会自己把 `..` 归一化掉，
> 不加这个参数请求根本发不出去，脚本跑出来全绿却什么都没验到。

## 待办

- [x] 真实 GEE 后端接入（受限沙箱：资源限额 / 超时 / 网络白名单）—— 2026-09-16 完成，项目 `weixing-508708`
- [x] **分析口径固化**（`app/codegen.py` 的 `_TASK_CALIBER`）—— 修复「同一请求两次结果差 0.64」：
      太湖 AOI 约 88% 是水面，不掩水体均值 -0.12 且曲线无季节变化，掩膜水体后 +0.52 且物候清晰。
      现已按任务类型固化掩膜/排序/统计口径，模板路径与 LLM 路径结果差异 < 0.004
- [x] **真实浏览器渲染验证** —— 2026-09-16 完成。`chrome-headless-shell` + 原生 CDP 实测：
      控制台 0 报错、2 个栅格图层、GEE 瓦片 200 `image/png`、0 失败请求；
      截图见 `docs/验收截图/`，报告见 `docs/测试报告.md` 第 3.11 节
- [x] **注册登录** —— 2026-09-16 完成。PBKDF2 加盐哈希 + 服务端会话表（可即时吊销）+
      全接口鉴权 + 登录失败节流 + 修改密码。验证：逻辑层 28 项、HTTP 层 37 项、
      真实浏览器端到端（注册→进应用→地图渲染，控制台 0 报错）。见 `docs/测试报告.md` 3.12 节
- [x] **多用户隔离** —— 2026-09-16 完成。`tasks` / `regions` / `user_settings` 加 `user_id`
      （统一 `0` = 无主/全局），越权返 404 不返 403，旧库自动迁移且历史数据划归首个管理员
      （实测接管 22 条）。顺带修掉一个**跨用户数据破坏**：`regions` 主键从 `name` 单列改成
      `(name, user_id)` —— 旧结构下 B 保存同名区域会把 A 那一行直接替换掉。
      验证：逻辑层 80 项（`tools/test_isolation.py`）+ HTTP 层 49 项（`tools/test_isolation_http.sh`）。
      见 `docs/测试报告.md` 3.13 节
- [x] **前端代码分割 / 懒加载** —— 2026-09-16 完成。`manualChunks` 按 vendor 拆分 +
      `React.lazy` 延迟加载地图/图表，首屏 JS 2,047,209 B → **852,805 B（-58%）**，
      echarts（1.0MB）与 leaflet（150KB）不再进首屏。
      踩到一个会让懒加载白做的坑：`import 'leaflet/dist/leaflet.css'` 放在 `main.jsx` 会让
      Vite 把整个 leaflet chunk 一并 `modulepreload` 进首屏 —— **样式 import 必须跟着组件走**。
      运行时验证取的是真实浏览器发出的请求，见 `docs/测试报告.md` 3.9 / 3.14 节
- [x] **任务配额与并发限制** —— 2026-09-17 完成。鉴权只挡住「没账号的人」，
      挡不住「注册一个号然后无限提交」，而每条任务都烧 DeepSeek token + GEE 云端算力。
      四层额度：全局并发 3 / 单用户并发 2 / 每小时 20 / 每天 60，超限返 `429` + `Retry-After`。
      两个关键点：额度**按库里的记录统计**（重启不清零），**校验与建任务在同一把锁里**
      （否则并发提交会同时读到"还差一个名额"然后都放行）。顺带修掉一个连带的僵尸状态：
      重启后被遗留的 `pending`/`running` 任务会**永久占住并发名额**，现在启动时自动标记失败。
      验证：HTTP 层 27 项（`tools/test_quota_http.sh`）。见 `docs/测试报告.md` 3.16 节
- [x] **CORS 收窄** —— 2026-09-17 完成。原配置 `allow_origins=["*"]` +
      `allow_credentials=True`，实测**任意 Origin 都被原样回显**并带
      `Access-Control-Allow-Credentials: true`。当时靠 Cookie 的 `SameSite=Lax`
      兜住（跨站 fetch 不带 cookie）才没被打穿，但只要改成 `SameSite=None` 就会变成
      「任意网站可读你的全部任务列表」。生产前端与后端同源，本就不需要 CORS ——
      改为白名单（默认只放行 Vite 5173，可用 `CORS_ORIGINS` 追加）。
      验证：`tools/test_auth_http.sh` 第 11 组断言「陌生 Origin 不被回显」
- [x] **静态托管路径穿越修复** —— 2026-09-17 完成。**本轮最严重的一处，且已真实暴露在公网。**
      SPA 兜底路由只判了 `candidate.is_file()`，没判「resolve 后仍在 `dist/` 内」；
      `%2e` 是 `.` 的合法编码、uvicorn 会先解码，于是 `/%2e%2e/%2e%2e/backend/.env`
      让**匿名访客读走后端源码（5435 B）、`.env`（2717 B，含 `DEEPSEEK_API_KEY`）、
      整个 `data/app.db`（233472 B）**，且这条路由没有鉴权。
      发现它的是一条顺手敲的 curl —— 探活端点其实是 `/health`，敲 `/api/health` 却拿到
      200 + HTML，顺着"为什么会返 200"才挖到洞。
      修复：`resolve()` 后必须 `is_relative_to(dist)`、带 `..` 直接 404、
      未知 `/api/*` 返 **JSON 404**。验证 `tools/test_static_route.py` 57 项
      （用**裸 ASGI scope** 发请求，避免 httpx 归一化导致假通过）+ `smoke_live.sh`
      第 5/6 组（本地与公网均 25/25）。见 `docs/测试报告.md` 3.19 节
- [x] **执行沙箱文件隔离** —— 2026-09-17 完成。修完路径穿越后做「外部输入最终落到哪个危险操作」
      的排查，发现同一根因的第二个入口：沙箱只封了网络与子进程，**文件系统敞开**，
      生成代码 `open(".env")` 直接读到 `DEEPSEEK_API_KEY`、`os.listdir(".")` 列出后端目录；
      而生成代码是 LLM 按用户输入产出的、注册又开放，等于注册用户一句话就能取走密钥。
      修复：`exec(用户代码)` 前封 `builtins.open`/`io.open`/`os.open`/`os.listdir`/`os.scandir`，
      显式放行 GEE 凭据路径。踩坑：加固点放太前会把 `python-dotenv` 读 `.env` 一起封死、
      整条链路报 `PermissionError`，必须放在 `_verify_toolchain()`/`_install_ee()` 之后。
      验证：`tools/test_sandbox_fs.py` 35 项（mock 与**真实凭据**两种模式都实测 ——
      后者 `ee.Number(42).add(1).getInfo()` 仍返回 43）+ `gee_doctor --self-test` 7 项。
      见 `docs/测试报告.md` 3.20 节
- [ ] **吊销并重新生成 `DEEPSEEK_API_KEY`** —— 上一条的后续动作：`.env` 曾有**两条**匿名可读路径
      （静态托管穿越 + 沙箱文件读取），密钥应按已泄漏处理。需到 DeepSeek 控制台操作
- [ ] **沙箱改「不碰文件系统」**（更彻底）：当前是「精确放行凭据 + 其余敏感项禁读」的名单策略，
      普通源码仍可读。彻底做法是由父进程解密凭据后经 stdin 的 `request` payload 传入，
      沙箱只许读临时工作目录 —— 需要改 `gee_auth.initialize()` 的取值方式
- [ ] 服务账号路线（对外部署用，凭据加密落盘 `data/credentials/gee.enc`；当前用个人 OAuth）。
      工具链已 44 项验通，只差在 Google Cloud Console 建账号并授予两项 IAM 角色
- [ ] 密码找回 / 邮箱验证：注册不需要邮箱，忘记密码目前只能由管理员在库里重置
