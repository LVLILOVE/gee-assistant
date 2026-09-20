"""GEE 数据集知识库与检索（借鉴 Earth Agent 的 geeDocs 数据集知识库定位）。

内置常用 GEE 数据集的结构化知识：ee_id、分辨率、重访周期、波段、适用任务、
一句话说明、示例代码片段。代码生成时按任务类型 + 关键词打分检索 top-k 数据集，
注入 prompt，让模型用对数据、少踩坑。纯本地实现，无外部依赖。
"""

from __future__ import annotations

# 任务类型 → 强相关数据集 id（权重更高）
TASK_DATASETS = {
    "ndvi": ["sentinel2", "modis_ndvi", "landsat8"],
    "water": ["jrc_water", "sentinel1", "sentinel2"],
    "classification": ["sentinel2", "dynamic_world", "worldcover"],
    "change_detection": ["jrc_water", "sentinel2", "landsat8"],
}

DATASETS = [
    {
        "id": "sentinel2",
        "name": "Sentinel-2 地表反射率",
        "ee_id": "COPERNICUS/S2_SR_HARMONIZED",
        "resolution": "10m（可见光/近红外）",
        "revisit": "5 天",
        "bands": "B2(蓝)/B3(绿)/B4(红)/B8(近红外)/B11,B12(短波红外)",
        "tasks": ["ndvi", "classification", "water"],
        "tags": ["植被", "水体", "分类", "ndvi", "多光谱", "高分辨率"],
        "desc": "最常用的中高分辨率多光谱数据，10 米分辨率，适合植被指数、水体提取、地表分类。",
        "snippet": (
            "ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')\n"
            "  .filterBounds(aoi).filterDate(start, end)\n"
            "  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))\n"
            "  .median()"
        ),
    },
    {
        "id": "landsat8",
        "name": "Landsat 8/9 地表反射率",
        "ee_id": "LANDSAT/LC08/C02/T1_L2",
        "resolution": "30m",
        "revisit": "16 天",
        "bands": "B2~B7 多光谱 + B10 热红外",
        "tasks": ["ndvi", "change_detection", "classification"],
        "tags": ["植被", "变化", "分类", "长时序", "历史"],
        "desc": "30 米分辨率，自 2013 年持续至今，适合长时序变化检测与历史对比分析。",
        "snippet": (
            "ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')\n"
            "  .filterBounds(aoi).filterDate(start, end)\n"
            "  .filter(ee.Filter.lt('CLOUD_COVER', 20))\n"
            "  .median()"
        ),
    },
    {
        "id": "sentinel1",
        "name": "Sentinel-1 SAR",
        "ee_id": "COPERNICUS/S1_GRD",
        "resolution": "10m",
        "revisit": "6 天",
        "bands": "VV、VH 极化",
        "tasks": ["water"],
        "tags": ["水体", "雷达", "洪水", "sar", "全天候"],
        "desc": "合成孔径雷达数据，不受云雨影响，是水体提取、洪涝监测的首选。",
        "snippet": (
            "ee.ImageCollection('COPERNICUS/S1_GRD')\n"
            "  .filterBounds(aoi).filterDate(start, end)\n"
            "  .filter(ee.Filter.eq('instrumentMode', 'IW'))\n"
            "  .select('VH').median()"
        ),
    },
    {
        "id": "modis_ndvi",
        "name": "MODIS NDVI (MOD13Q1)",
        "ee_id": "MODIS/061/MOD13Q1",
        "resolution": "250m",
        "revisit": "16 天合成",
        "bands": "NDVI、EVI",
        "tasks": ["ndvi"],
        "tags": ["植被", "ndvi", "evi", "大范围", "全球"],
        "desc": "全球 250 米 NDVI/EVI 产品，适合大范围、长时序植被监测，无需自己计算指数。",
        "snippet": (
            "ee.ImageCollection('MODIS/061/MOD13Q1')\n"
            "  .filterBounds(aoi).filterDate(start, end)\n"
            "  .select('NDVI').median()"
        ),
    },
    {
        "id": "jrc_water",
        "name": "JRC 全球地表水",
        "ee_id": "JRC/GSW1_4/GlobalSurfaceWater",
        "resolution": "30m",
        "revisit": "1984 至今",
        "bands": "occurrence（出现频率）、seasonality、transition",
        "tasks": ["water", "change_detection"],
        "tags": ["水体", "变化", "洪水", "湖泊", "水库", "历史"],
        "desc": "1984 年至今的全球地表水分布与变化，适合水体范围、季节性变化、长期变迁分析。",
        "snippet": (
            "ee.Image('JRC/GSW1_4/GlobalSurfaceWater')\n"
            "  .select('occurrence').clip(aoi)"
        ),
    },
    {
        "id": "dynamic_world",
        "name": "Dynamic World 近实时地物分类",
        "ee_id": "GOOGLE/DYNAMICWORLD/V1",
        "resolution": "10m",
        "revisit": "近实时",
        "bands": "label（9 类）、各类别概率",
        "tasks": ["classification"],
        "tags": ["分类", "土地利用", "近实时", "地类", "cover"],
        "desc": "Google 的 10 米近实时土地覆盖分类（水/树/草/农田/建成区等 9 类），无需训练。",
        "snippet": (
            "ee.ImageCollection('GOOGLE/DYNAMICWORLD/V1')\n"
            "  .filterBounds(aoi).filterDate(start, end)\n"
            "  .select('label').mode()"
        ),
    },
    {
        "id": "worldcover",
        "name": "ESA WorldCover 全球土地覆盖",
        "ee_id": "ESA/WorldCover/v200",
        "resolution": "10m",
        "revisit": "2020/2021 年度",
        "bands": "Map（11 类）",
        "tasks": ["classification"],
        "tags": ["分类", "土地利用", "土地覆盖", "esa", "年度"],
        "desc": "ESA 的 10 米全球土地覆盖产品（11 类），适合直接获取某年份的权威地类分布。",
        "snippet": "ee.ImageCollection('ESA/WorldCover/v200').mosaic().select('Map').clip(aoi)",
    },
    {
        "id": "cloud_score",
        "name": "Cloud Score+ 云质量",
        "ee_id": "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED",
        "resolution": "10m（与 S2 对应）",
        "revisit": "与 S2 同步",
        "bands": "cs_cdf（清晰概率）",
        "tasks": ["ndvi", "classification", "water", "change_detection"],
        "tags": ["云", "掩膜", "云掩膜", "质量", "cloud"],
        "desc": "与 Sentinel-2 配套的高质量云掩膜数据，比 CLOUDY_PIXEL_PERCENTAGE 更精细。",
        "snippet": (
            "cs = ee.ImageCollection('GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED')\n"
            "s2 = s2.linkCollection(cs, ['cs_cdf'])\n"
            "def mask(img): return img.updateMask(img.select('cs_cdf').gte(0.6))"
        ),
    },
    {
        "id": "srtm",
        "name": "SRTM 数字高程模型",
        "ee_id": "USGS/SRTMGL1_003",
        "resolution": "30m",
        "revisit": "静态",
        "bands": "elevation、slope、aspect",
        "tasks": ["classification"],
        "tags": ["高程", "地形", "坡度", "dem", "海拔"],
        "desc": "全球 30 米数字高程模型，可计算坡度坡向，辅助地形相关分类。",
        "snippet": "ee.Image('USGS/SRTMGL1_003').select('elevation').clip(aoi)",
    },
]


def _score(ds: dict, task_type: str, text: str) -> float:
    score = 0.0
    if task_type in ds.get("tasks", []):
        score += 3.0
    if task_type in TASK_DATASETS and ds["id"] in TASK_DATASETS[task_type]:
        score += 2.0
    t = (text or "").lower()
    for tag in ds.get("tags", []):
        if tag.lower() in t:
            score += 1.5
    for kw in ds["name"].lower().split():
        if kw in t:
            score += 0.5
    return score


def retrieve(task_type: str, text: str = "", top_k: int = 3) -> list[dict]:
    """按任务类型 + 关键词打分，返回 top_k 个数据集。"""
    ranked = sorted(DATASETS, key=lambda d: _score(d, task_type, text), reverse=True)
    result = []
    for d in ranked:
        if _score(d, task_type, text) > 0:
            result.append(d)
        if len(result) >= top_k:
            break
    return result or ranked[:top_k]


def search(q: str, top_k: int = 10) -> list[dict]:
    """按关键词搜索数据集（不限定任务类型）。"""
    q = (q or "").strip().lower()
    if not q:
        return DATASETS[:top_k]
    ranked = sorted(DATASETS, key=lambda d: _score(d, "", q), reverse=True)
    return [d for d in ranked if _score(d, "", q) > 0][:top_k] or ranked[:top_k]


def build_knowledge_note(task_type: str, text: str = "", top_k: int = 3) -> str:
    """把检索到的数据集知识转成注入代码生成 prompt 的说明。"""
    datasets = retrieve(task_type, text, top_k)
    if not datasets:
        return ""
    lines = ["可参考的 GEE 数据集（按相关度排序，请优先选用合适者）："]
    for i, d in enumerate(datasets, 1):
        lines.append(f"{i}. {d['name']} — ee_id: {d['ee_id']}（{d['resolution']}，{d['desc']}）")
    return "\n".join(lines)
