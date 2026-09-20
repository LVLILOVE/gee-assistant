"""常用区域库：内置一批常见分析区域的中心坐标，供离线后端落图与前端快捷选择。

真实 GEE 后端接入后，区域坐标由 GEE 内部的地理编码解析，本模块主要用于
离线兜底后端的样例落图。
"""

from __future__ import annotations

# 按名称长度降序，保证长名优先匹配
REGION_LIBRARY = [
    {"name": "珠江三角洲", "lon": 113.50, "lat": 22.80, "desc": "粤港澳大湾区"},
    {"name": "青藏高原", "lon": 92.00, "lat": 31.50, "desc": "高原生态系统"},
    {"name": "长江三角洲", "lon": 120.90, "lat": 31.00, "desc": "长三角城市群"},
    {"name": "太湖流域", "lon": 120.13, "lat": 31.20, "desc": "长江三角洲主要湖泊"},
    {"name": "洞庭湖", "lon": 112.80, "lat": 29.30, "desc": "长江中游大型湖泊"},
    {"name": "鄱阳湖", "lon": 116.20, "lat": 29.10, "desc": "中国最大淡水湖"},
    {"name": "黄河流域", "lon": 113.00, "lat": 35.00, "desc": "黄河中下游"},
    {"name": "三江源", "lon": 96.00, "lat": 34.00, "desc": "三江源头保护区"},
    {"name": "海南岛", "lon": 109.70, "lat": 19.10, "desc": "热带岛屿"},
    {"name": "北京市", "lon": 116.40, "lat": 39.90, "desc": "京津冀城市群"},
    {"name": "上海市", "lon": 121.47, "lat": 31.23, "desc": "长江口城市"},
    {"name": "成都市", "lon": 104.06, "lat": 30.67, "desc": "四川盆地城市"},
    {"name": "西安市", "lon": 108.94, "lat": 34.34, "desc": "关中平原城市"},
]

# 排序：名称长度降序（长名优先）
REGION_LIBRARY.sort(key=lambda r: -len(r["name"]))

DEFAULT_CENTER = (116.40, 39.90)


def get_center(region: str) -> tuple[float, float] | None:
    """根据区域描述返回中心坐标 (lon, lat)，无法匹配返回 None。"""
    region = (region or "").strip()
    if not region:
        return None
    for r in REGION_LIBRARY:
        if r["name"] == region:
            return (r["lon"], r["lat"])
    for r in REGION_LIBRARY:
        if r["name"] in region:
            return (r["lon"], r["lat"])
    return None
