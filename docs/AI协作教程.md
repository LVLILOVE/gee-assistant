# AI 协作教程：如何用 AI 从零完成一个卫星遥感智能分析 App

| 项目 | 内容 |
|---|---|
| 文档版本 | V1.0 |
| 更新日期 | 2026-09-15 |
| 适用对象 | LLM 部署/开发初学者，单人开发者 |
| 交付依据 | 任务书 5.2「AI 协作教程」 |

---

## 0. 教程说明

本教程完整复盘「卫星遥感影像智能分析助手」如何通过 **AI 协作**，由单人（1–2 周）从一份任务书逐步搭建出可对外访问的 WebApp。每一步给出：**做什么 → 怎么对 AI 说 → 产出 → 踩坑**。

> 核心心法：**把 AI 当资深搭档，而不是搜索引擎。** 给出完整上下文（任务书、代码、报错截图），让它直接动手改，而不是只给方案。

---

## 1. 阶段一：需求理解（0.5 天）

### 做什么
读懂任务书，把模糊需求变成可执行的产品规划。

### 怎么对 AI 说
```
请以资深产品经理的视角，系统梳理这份任务书：
明确项目目标与范围、拆分关键任务模块及优先级、
各任务的依赖关系与交付顺序、潜在风险、资源需求、验收标准。
```

### 产出
- 目标三层拆解（业务/产品/技术）；
- 任务模块（M0–M9）与关键路径；
- 风险矩阵（GEE 网络可达性是最高危）；
- 量化指标的验收口径。

### 踩坑
- 任务书里"意图识别 ≥90%""一次成功率 ≥70%"**没定义判定口径**，必须主动向用户澄清，否则验收会扯皮。
- 先确认"参考产品"（本项目的 [Earth Agent](https://github.com/wybert/earth-agent-chrome-ext)），了解行业现有方案再动手。

---

## 2. 阶段二：需求澄清（0.5 天）

### 做什么
用"确认清单"把方案锁死，避免返工。

### 怎么对 AI 说（AI 反向提问用户）
```
基于现有方案，列出多个不确定的问题向我确认：
目标用户？核心功能？数据存储？登录？移动端？
```

### 关键决策（本项目实际确认结果）
| 问题 | 决策 |
|---|---|
| 大模型 | DeepSeek（OpenAI 兼容、国内直连） |
| GEE 账号 | 已有（但国内网络是风险） |
| 技术栈 | 严格按任务书：Python3.12 + LangGraph + FastAPI + React + sqlite3 |
| 部署 | 本地 + cpolar 对外可访问 |
| 团队/工期 | 单人，1–2 周 |

### 踩坑
- **最大的坑**：GEE 在国内经常连不上。对策是把执行层做成**可插拔后端**（离线兜底 + 真实 GEE），没有 GEE 也能演示全链路。

---

## 3. 阶段三：环境搭建（0.5 天）

### 做什么
建干净 venv，配国内镜像。

### 怎么对 AI 说
```
用 conda Python 3.12 建一个干净的 venv，
pip 配清华源、npm 配 npmmirror 源，
安装 fastapi/uvicorn/httpx/langgraph/numpy/pandas 等后端依赖。
```

### 产出
- `backend/.venv`（干净虚拟环境）；
- `backend/requirements.txt`；
- 前后端依赖安装完成。

### 踩坑（本项目实测）
1. **conda 3.12 的 pip 是坏的**（ensurepip 也坏），改用托管 Python 3.13 建 venv。
2. **官方 PyPI 不通**，必须走清华源：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`。
3. **npm 走 npmmirror**：`npm install --registry=https://registry.npmmirror.com`，否则超时。

---

## 4. 阶段四：后端核心链路（2–3 天）

### 做什么
实现「代码生成 → 沙箱执行 → 自动调试」主链路。

### 怎么对 AI 说
```
实现可插拔执行后端：
- OfflineBackend：内置样例数据，无 GEE 也能出 NDVI 地图和统计图
- GEEBackend：真实调用 Earth Engine（骨架）
- DeepSeek 代码生成，未配 key 时降级内置模板
- LangGraph 编排「生成→执行→调试」循环，错误分类重试
- FastAPI 异步任务接口（提交/进度/结果）
```

### 产出
`models.py`（I/O 契约）、`codegen.py`、`debugger.py`、`orchestrator.py`、`execution/`（offline/gee/base）、`main.py`。

### 关键设计
- **可插拔后端**：`ExecutionOutcome` 统一契约（图层 + 图表 + 错误分类），前端不感知后端差异。
- **降级兜底**：无 key 也能跑，系统永远可用。

### 踩坑
- langgraph 装到了 1.2.11（新大版本），写完后必须跑**冒烟测试**验证编排 API 兼容性。
- 端口 8000 被别的项目占用，改用 8010。

---

## 5. 阶段五：前端脚手架（1–2 天）

### 怎么做
Vite + React + AntD + Leaflet + ECharts，对话 + 地图 + 图表。

### 怎么对 AI 说
```
搭一套 Vite + React 前端，UI 用 Ant Design，
地图用 Leaflet、图表用 ECharts，
对接后端异步任务接口（提交→轮询→展示地图图表）。
```

### 产出
`package.json`、`vite.config.js`、`src/App.jsx`、`src/api.js`、`src/style.css`。

### 踩坑
- 前端依赖体量大（antd/echarts/leaflet），`npm install` 约 17 分钟属正常，用 `run_in_background` 后台装。

---

## 6. 阶段六：验证与迭代（2–3 天）

### 怎么对 AI 说（迭代式追加需求）
```
1. 接入 DeepSeek 真实代码生成（把 key 填进 .env 并验证）
2. 加自然语言对话 + 多轮澄清
3. 加 sqlite 持久化 + 历史任务 + 报告导出
4. 加常用区域库 + 快捷选择
5. cpolar 对外发布
```

### 产出
意图解析（`intent.py`）、持久化（`task_store.py`）、区域库（`regions.py`）、静态托管 + cpolar 映射。

### 关键验证方式
每次改完都做**端到端实测**：提交一个真实任务 → 看是否真的调用了 DeepSeek → 结果是否正确落图。

### 踩坑（本项目实测，含借鉴 Earth Agent 的经验）
1. **类内方法名遮蔽内置函数**：`TaskStore` 里已有 `list()` 方法，导致后续 `list[dict]` 类型注解报 `'function' object is not subscriptable`。类内方法名别与 `list/dict/str` 等冲突。
2. **`curl -w "%{size_download}"` 显示 0**：chunked/gzip 响应会显示 0，用 `| head -c` 或直接看内容判断，别被 size=0 误导。
3. **删除钩子行为不定**：删目录可能被移进回收站（异步），删除后要以"回收站大小 + 磁盘可用空间"实测复核。
4. **停旧进程**：改代码后重启前，先 `netstat -ano | grep :端口` 找到 PID 再 `Stop-Process`，否则端口被占启动失败。

---

## 7. 阶段七：对外发布（0.5 天）

### 怎么做
让 FastAPI 托管前端构建产物（单端口对外），cpolar 映射。

### 怎么对 AI 说
```
让后端托管 frontend/dist，改成单端口对外，
用 cpolar http 8010 -region cn_top 映射到公网，
验证外网端到端可访问。
```

### 产出
外网地址（如 `https://xxxx.r30.cpolar.top`）。

### 踩坑
- cpolar 免费版每次重启子域名会变；要固定域名需升级。

---

## 8. 阶段八：文档交付（1–2 天）

### 怎么做
按任务书 5.2 补齐全部文档。

### 怎么对 AI 说
```
补齐交付文档：PRD、UI/UX、数据库设计、API 文档、
测试报告、部署手册、AI 协作教程、RAG 语料库、PPT。
PRD 要借鉴 earth-agent-chrome-ext 的产品设计。
```

### 产出清单
`PRD.md`、`UI-UX设计.md`、`数据库设计.md`、`API文档.md`、`测试报告.md`、`部署手册.md`、`AI协作教程.md`（本文）、`RAG语料库.md`。

---

## 9. 借鉴 Earth Agent 的 5 条产品经验

1. **先理解后执行**（Ask/Do 双模式）：给用户"只读咨询"选项，降低误执行成本。
2. **Custom Instructions**：让用户能设定科学规范/表达风格，产出更专业。
3. **Agent Profiles**：不同人设适配不同任务（编码/教学/分析）。
4. **数据集知识库**（geeDocs）：代码生成配 RAG 检索，正确率显著提升。
5. **示例 prompt**：降低提问门槛，用户照着抄就会用。

---

## 10. 通用协作技巧总结

| 技巧 | 说明 |
|---|---|
| 给足上下文 | 任务书全文、现有代码、报错原文一起给 |
| 让它直接动手 | 说"帮我改"而不是"怎么改" |
| 反向确认清单 | 方案不确定时让 AI 列问题逐条确认 |
| 每步实测 | 改完就跑端到端验证，别只信它说"好了" |
| 记录踩坑 | 让 AI 把踩坑写进记忆/技能，下次复用 |
| 串行执行依赖 | 避免在 IDE 里并发跑 pip/conda 与后台安装冲突 |
