"""离线兜底后端：不真正调用 GEE，返回内置样例地图图层与统计图表。

作用：在 GEE 账号/网络未就绪时，也能跑通"一句话 → 地图 + 图表"的完整链路，
与任务书 6.3「演示与测试采用公开样例数据集与离线样例结果兜底」一致。
"""

from ..daterange import default_year
from ..models import AnalysisRequest, ChartData, ExecutionOutcome, LayerData, TaskType
from ..regions import DEFAULT_CENTER, get_center
from .base import ExecutionBackend


def _center(request: AnalysisRequest) -> tuple[float, float]:
    return get_center(request.region) or DEFAULT_CENTER


def _year_pairs(request: AnalysisRequest, max_pairs: int = 4) -> list[str]:
    """按请求里的真实年份区间生成「起年-止年」标签。

    原先写死 ["2020-2021", "2021-2022", "2022-2023", "2023-2024"]。离线模式是
    没配 GEE 凭据时的演示路径，图上标着两年前的年份，会让整个产品看起来停在过去
    （本次全链路清理「冻结年份」时一并处理）。兜底年份也走 daterange，不写常量。
    """

    def _yr(s) -> int | None:
        try:
            return int(str(s)[:4])
        except (TypeError, ValueError):
            return None

    y0, y1 = _yr(request.start_date), _yr(request.end_date)
    if y0 is None or y1 is None or y1 <= y0:
        y1 = default_year()
        y0 = y1 - max_pairs
    pairs = []
    y = y0
    while y < y1 and len(pairs) < max_pairs:
        pairs.append(f"{y}-{y + 1}")
        y += 1
    # 起止同年或只差一年时上面的循环会产出空列表，退化成一个直接区间
    return pairs or [f"{y0}-{y1}"]


def _ring(cx: float, cy: float, d: float = 0.18) -> list:
    return [[cx - d, cy - d], [cx + d, cy - d], [cx + d, cy + d], [cx - d, cy + d], [cx - d, cy - d]]


def _aoi_feature(cx: float, cy: float) -> dict:
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [_ring(cx, cy)]}, "properties": {"name": "分析区域"}}

def _grid_features(cx: float, cy: float, n: int = 5, d: float = 0.18, value_fn=None) -> list:
    step = 2 * d / n
    feats = []
    for i in range(n):
        for j in range(n):
            x0 = cx - d + i * step
            y0 = cy - d + j * step
            r = [[x0, y0], [x0 + step, y0], [x0 + step, y0 + step], [x0, y0 + step], [x0, y0]]
            val = value_fn(i, j) if value_fn else round(((i * 7 + j * 3) % 100) / 100, 2)
            feats.append(
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [r]}, "properties": {"value": val}}
            )
    return feats


def _fc(features: list) -> dict:
    return {"type": "FeatureCollection", "features": features}


# NDVI 分级配色：沿用遥感界通用的"红→黄→绿"植被梯度语义
# （红=低植被、黄=过渡、绿=高植被），这一点**不能为了配色统一而改动** ——
# 用户与文献都按这个读图，改成别的色系会造成误读。
# 只做了一处微调：末端高值绿从 #1a9850 调向深林绿，与界面主色呼应。
NDVI_LEGEND = [
    {"label": "< 0.2", "color": "#d73027"},
    {"label": "0.2-0.4", "color": "#fc8d59"},
    {"label": "0.4-0.6", "color": "#fee08b"},
    {"label": "0.6-0.8", "color": "#a6d96a"},
    {"label": "> 0.8", "color": "#2d6a4f"},
]


def _ndvi(cx: float, cy: float, request: AnalysisRequest) -> ExecutionOutcome:
    feats = _grid_features(cx, cy, value_fn=lambda i, j: round(0.15 + ((i * 13 + j * 7) % 80) / 100, 2))
    layer = LayerData(name="NDVI 空间分布", geojson=_fc(feats), legend=NDVI_LEGEND)
    months = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"]
    series = [0.32, 0.38, 0.45, 0.52, 0.60, 0.66, 0.68, 0.64, 0.55, 0.47, 0.40, 0.35]
    chart = ChartData(
        title=f"{request.region} 逐月 NDVI 均值",
        kind="line",
        labels=months,
        series=[{"name": "NDVI", "data": series}],
    )
    peak = max(series)
    stats = {
        "NDVI 年均值": round(sum(series) / len(series), 2),
        "NDVI 峰值": peak,
        "峰值月份": months[series.index(peak)],
        "NDVI 最低值": min(series),
    }
    return ExecutionOutcome(
        ok=True, layers=[layer], charts=[chart], stats=stats,
        stdout=f"[离线] NDVI 计算完成，区域={request.region}",
    )


def _water(cx: float, cy: float, request: AnalysisRequest) -> ExecutionOutcome:
    # 一个近似水体多边形（中心偏下）
    blob = [
        [cx - 0.06, cy - 0.10], [cx + 0.02, cy - 0.12], [cx + 0.08, cy - 0.06],
        [cx + 0.05, cy + 0.04], [cx - 0.04, cy + 0.08], [cx - 0.10, cy + 0.02], [cx - 0.06, cy - 0.10],
    ]
    water_feat = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [blob]}, "properties": {"value": 1.0, "name": "水体"}}
    aoi = _aoi_feature(cx, cy)
    layer = LayerData(name="水体提取结果", geojson=_fc([aoi, water_feat]), legend=[{"label": "水体", "color": "#2f6f9f"}])
    months = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"]
    series = [210, 215, 220, 225, 228, 230, 235, 232, 225, 218, 212, 208]
    chart = ChartData(
        title=f"{request.region} 逐月水体面积",
        kind="bar",
        labels=months,
        series=[{"name": "水体面积(km²)", "data": series}],
    )
    peak = max(series)
    stats = {
        "水体面积均值": round(sum(series) / len(series), 1),
        "水体面积最大": peak,
        "最大月份": months[series.index(peak)],
        "水体面积最小": min(series),
    }
    return ExecutionOutcome(
        ok=True, layers=[layer], charts=[chart], stats=stats,
        stdout=f"[离线] 水体提取完成，区域={request.region}",
    )


# 地表分类配色：地物色**必须符合直觉约定**（森林绿 / 水体蓝 / 农田浅绿 /
# 城市暖橙 / 裸地土黄），这是用户读图的第一依据，不能为了"配色统一"而改动 ——
# 把水体画成绿色是严重的可读性事故。
# 这里只做饱和度和明度的微调，让五色放在暖米底上不互相打架，
# 同时保证相邻色（森林/农田）在灰度下也能区分。
CLASS_COLORS = {
    "森林": "#2d6a4f",   # 深林绿
    "水体": "#2f6f9f",   # 水蓝（降饱和，避免与主色绿抢眼）
    "农田": "#a8c66c",   # 耕地黄绿（比森林明显更亮更黄）
    "城市": "#c96f2e",   # 建成区暖橙
    "裸地": "#d9bd90",   # 裸土沙色
}


def _classification(cx: float, cy: float, request: AnalysisRequest) -> ExecutionOutcome:
    classes = ["森林", "水体", "农田", "城市", "裸地"]
    def cls_fn(i, j):
        k = (i + j * 2) % len(classes)
        return classes[k]
    cells = _grid_features(cx, cy, value_fn=cls_fn)
    for f in cells:
        f["properties"]["name"] = f["properties"].pop("value")
    aoi = _aoi_feature(cx, cy)
    layer = LayerData(
        name="地表分类结果",
        geojson=_fc([aoi, *cells]),
        legend=[{"label": k, "color": v} for k, v in CLASS_COLORS.items()],
    )
    chart = ChartData(
        title=f"{request.region} 地表覆盖占比",
        kind="pie",
        labels=classes,
        series=[{"name": "占比(%)", "data": [32, 18, 35, 9, 6]}],
    )
    share = [32, 18, 35, 9, 6]
    top = max(zip(classes, share), key=lambda x: x[1])
    stats = {
        "主导地类": top[0],
        "主导地类占比": f"{top[1]}%",
        "水体占比": "18%",
        "分类类别数": len(classes),
    }
    return ExecutionOutcome(
        ok=True, layers=[layer], charts=[chart], stats=stats,
        stdout=f"[离线] 地表分类完成，区域={request.region}",
    )


def _change(cx: float, cy: float, request: AnalysisRequest) -> ExecutionOutcome:
    def val_fn(i, j):
        v = ((i * 11 + j * 5) % 40) - 10  # -10 ~ +29 变化幅度
        return v / 100
    feats = _grid_features(cx, cy, value_fn=val_fn)
    layer = LayerData(
        name="变化检测结果（红=增加，蓝=减少）",
        geojson=_fc(feats),
        # 变化检测沿用"红增蓝减"的既有约定（与项目文档、标题文案一致）。
        # 但注意：**红/蓝在红绿色盲眼中最难分辨**，所以标题里显式写出
        # "红=增加，蓝=减少"，不依赖颜色单独表意。
        legend=[{"label": "减少", "color": "#2f6f9f"}, {"label": "无变化", "color": "#e8e6de"}, {"label": "增加", "color": "#a33b2c"}],
    )
    years = _year_pairs(request)
    # 数值是合成的演示值，长度必须跟着标签走；用确定性公式而非随机数，
    # 保证同一请求每次离线演示出来的图一致（便于比对）。
    data = [round(3.0 + ((i * 7) % 9) * 0.6, 1) for i in range(len(years))]
    chart = ChartData(
        title=f"{request.region} 逐年变化面积",
        kind="bar",
        labels=years,
        series=[{"name": "变化面积(km²)", "data": data}],
    )
    stats = {
        "统计期数": len(years),
        "变化面积均值": round(sum(data) / len(data), 1),
        "变化面积最大": max(data),
        "变化面积最小": min(data),
    }
    return ExecutionOutcome(
        ok=True, layers=[layer], charts=[chart], stats=stats,
        stdout=f"[离线] 变化检测完成，区域={request.region}",
    )


class OfflineBackend(ExecutionBackend):
    name = "offline"

    def execute(self, code: str, request: AnalysisRequest) -> ExecutionOutcome:
        cx, cy = _center(request)
        if request.task_type == TaskType.ndvi:
            return _ndvi(cx, cy, request)
        if request.task_type == TaskType.water:
            return _water(cx, cy, request)
        if request.task_type == TaskType.classification:
            return _classification(cx, cy, request)
        if request.task_type == TaskType.change_detection:
            return _change(cx, cy, request)
        return ExecutionOutcome(ok=False, error={"category": "param", "message": f"未知任务类型 {request.task_type}"})
