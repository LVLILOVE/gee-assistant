# 卫星遥感影像智能分析助手 · RAG 语料库与提示词

| 项目 | 内容 |
|---|---|
| 文档版本 | V1.0 |
| 更新日期 | 2026-09-15 |
| 交付依据 | 任务书 5.2「RAG 语料库」+ 3.3「提示词模板库」 |
| 借鉴参考 | [Earth Agent](https://github.com/wybert/earth-agent-chrome-ext) 的 geeDocs 数据集知识库 |

---

## 1. 语料库定位与用途

本语料库服务于"代码生成时的知识检索（RAG）"，目标：**让大模型生成更准确、更专业的 GEE 分析代码**。

- **数据来源**：GEE 官方数据目录（Data Catalog）+ 社区数据集 + 四类任务常用配方；
- **检索方式**：按任务类型 / 关键词 / 数据集 ID 命中；
- **用途**：注入代码生成提示词，减少大模型"瞎编数据集 ID / 波段名"导致的执行失败。

> 借鉴 Earth Agent 的 `geeDocs` 能力（"Knows Earth Engine Data Catalog as well as community dataset"）。

---

## 2. GEE 数据集语料（核心数据集清单）

### 2.1 光学影像

| 数据集 ID | 中文名 | 关键波段 | 分辨率 | 适用任务 |
|---|---|---|---|---|
| `COPERNICUS/S2_SR_HARMONIZED` | Sentinel-2 地表反射率 | B2-B8A, B8, B11, B12 | 10-20m | NDVI、水体、分类、变化 |
| `LANDSAT/LC08/C02/T1_L2` | Landsat 8 地表反射率 | SR_B2-SR_B7 | 30m | NDVI、变化检测 |
| `LANDSAT/LC09/C02/T1_L2` | Landsat 9 地表反射率 | SR_B2-SR_B7 | 30m | 同上 |
| `MODIS/061/MOD13Q1` | MODIS 植被指数 | NDVI, EVI | 250m | 大范围 NDVI 时序 |

### 2.2 专用指数/产品

| 数据集 ID | 中文名 | 用途 |
|---|---|---|
| `COPERNICUS/S2_CLOUD_PROBABILITY` | Sentinel-2 云概率 | 云掩膜 |
| `GOOGLE/DYNAMICWORLD/V1` | 动态土地覆盖 | 快速地表分类 |
| `JRC/GSW1_4/GlobalSurfaceWater` | 全球地表水 | 水体/水变化检测 |
| `ESA/WorldCover/v200` | ESA 土地覆盖 | 分类对比基准 |

### 2.3 常用波段对照（Sentinel-2）

| 用途 | 波段 |
|---|---|
| NDVI | `(B8 - B4) / (B8 + B4)` |
| 水体（MNDWI） | `(B3 - B11) / (B3 + B11)` |
| 水体（NDWI） | `(B3 - B8) / (B3 + B8)` |
| 真彩色 | `B4, B3, B2` |

---

## 3. GEE API 用法语料（代码配方库）

### 3.1 通用模板（NDVI）

```python
import ee
ee.Initialize()

aoi = ee.Geometry.Point([lon, lat]).buffer(50000)  # 区域
col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
       .filterBounds(aoi)
       .filterDate("2025-06-01", "2025-08-31")
       .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20)))
img = col.median()
ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
# 区域统计
stats = ndvi.reduceRegion(
    reducer=ee.Reducer.mean(),
    geometry=aoi, scale=10, maxPixels=1e9)
print(stats.getInfo())
```

### 3.2 水体提取（MNDWI）

```python
mndwi = img.normalizedDifference(["B3", "B11"]).rename("MNDWI")
water = mndwi.gt(0.0)  # 阈值分割
water_area = water.multiply(ee.Image.pixelArea()).reduceRegion(
    reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9)
```

### 3.3 变化检测（两期 NDVI 差值）

```python
before = col.filterDate("2020-01-01", "2020-12-31").median().normalizedDifference(["B8","B4"])
after  = col.filterDate("2025-01-01", "2025-12-31").median().normalizedDifference(["B8","B4"])
diff = after.subtract(before).rename("NDVI_diff")
```

### 3.4 地表分类（简单阈值 / 监督）

```python
# 快速：用 DynamicWorld
dw = (ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
      .filterBounds(aoi).filterDate("2025-06-01", "2025-08-31").mosaic())
lc = dw.select("label")  # 类别标签
```

---

## 4. 提示词模板库

### 4.1 代码生成提示词（已用，`codegen.py`）

```
你是 Google Earth Engine（GEE）专家。根据下面的分析需求，
生成可直接运行的 GEE Python 代码，输出只有代码本身。

分析需求：
- 任务类型：{task_type}
- 分析区域：{region}
- 时间范围：{start_date} ~ {end_date}
- 云量阈值：{cloud_threshold}%

要求：
1. 使用 COPERNICUS/S2_SR_HARMONIZED 或合适的公开数据集；
2. 计算对应指数/结果，并用 reduceRegion 输出统计值；
3. 代码需包含 ee.Initialize()；
4. 只输出代码，不要解释。
```

### 4.2 意图解析提示词（已用，`intent.py`）

```
你是卫星遥感分析意图解析器。把用户的中文需求解析为 JSON，
只输出 JSON 本身。字段：task_type / region / start_date /
end_date / cloud_threshold，不确定一律 null。
```

### 4.3 错误修复提示词（`debugger.py` 扩展）

```
下面的 GEE 代码执行报错。请定位原因并给出修复后的完整代码。
错误分类：{category}
错误信息：{message}
原代码：
{code}
只输出修复后的代码。
```

### 4.4 RAG 检索增强提示词（🔶 建议）

```
以下是相关的 GEE 数据集与用法知识（检索结果）：
{retrieved_chunks}

请结合上述知识，生成分析代码，优先使用知识库中提到的
数据集 ID 与波段，不要臆造不存在的 ID。
```

---

## 5. 构建 RAG 助手的提示词

### 5.1 RAG 系统提示词（System Prompt）

```
你是一个"遥感分析 RAG 助手"，内置 Google Earth Engine（GEE）
数据集与 API 知识库。你的职责：
1. 根据用户的分析需求，从知识库检索合适的数据集与波段配方；
2. 生成准确、可运行的 GEE Python 代码；
3. 当知识库不足以支撑时，明确告知用户并给出建议。

原则：
- 数据集 ID 必须来自知识库，不臆造；
- 波段名必须准确（如 Sentinel-2 的 B8/B4）；
- 优先使用公开、长期可用的数据集。
```

### 5.2 语料分块与入库建议

| 步骤 | 说明 |
|---|---|
| 分块 | 按"数据集 ID + 用途 + 代码片段"切块，每块 200–500 token |
| 向量化 | 中文文本用 BGE / M3E 等开源 embedding 模型 |
| 检索 | 用任务类型 + 用户关键词做混合检索（关键词 + 向量） |
| 注入 | 命中 Top-K 块拼入代码生成提示词 |

### 5.3 最小可用实现（无向量库时的降级）

在 `geee_datasets` 表（见《数据库设计.md》4.4 节）存数据集元数据，代码生成时按 `task_type` + 关键词做 SQL LIKE 匹配，把命中的数据集 ID/波段/示例代码拼进提示词即可，无需引入重型向量库。

---

## 6. 语料维护

- 新增数据集：往 `gee_datasets` 表 / 本文档第 2 节补充；
- bad case 迭代：把执行失败的数据集 ID/波段错误回填到语料，形成"错误清单"反向约束生成（任务书 3.3）。
