from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .daterange import default_end, default_start


class TaskType(str, Enum):
    ndvi = "ndvi"
    water = "water"
    classification = "classification"
    change_detection = "change_detection"


TASK_LABELS = {
    TaskType.ndvi: "植被指数 NDVI 计算",
    TaskType.water: "水体提取",
    TaskType.classification: "地表分类",
    TaskType.change_detection: "时序变化检测",
}

TASK_LABELS_BY_VALUE = {t.value: label for t, label in TASK_LABELS.items()}


class AnalysisRequest(BaseModel):
    task_type: TaskType
    region: str = Field(default="太湖流域", description="分析区域描述")
    # default_factory 而不是常量默认值：常量在**模块导入时**只求值一次，
    # 服务长期不重启会跨年沿用旧值。用 factory 让每次构造请求都按当天推算。
    start_date: str = Field(default_factory=default_start)
    end_date: str = Field(default_factory=default_end)
    cloud_threshold: float = 20.0
    extra: dict[str, Any] = Field(default_factory=dict)


class LayerData(BaseModel):
    name: str
    kind: str = "geojson"
    geojson: Optional[dict] = None
    tile_url: Optional[str] = None
    legend: list[dict] = Field(default_factory=list)


class ChartData(BaseModel):
    title: str
    kind: str = "line"  # line | bar | pie
    labels: list[str] = Field(default_factory=list)
    series: list[dict] = Field(default_factory=list)


class ExecutionOutcome(BaseModel):
    ok: bool
    layers: list[LayerData] = Field(default_factory=list)
    charts: list[ChartData] = Field(default_factory=list)
    stdout: str = ""
    error: Optional[dict] = None  # {"category": str, "message": str}
    # 关键统计量（代码里 WB.stat(k, v) 上报的那些）。
    # 原始实现把 stats 只拼进 stdout 文本，于是「结论摘要」这一层只能去解析文本 ——
    # 脆弱且容易错。改成结构化字段一路带到前端。
    stats: dict[str, Any] = Field(default_factory=dict)
    # LLM 生成的业务语言结论摘要（"图看完了所以该做什么"那一跳）。
    # None = 未生成（如离线兜底或 LLM 不可用），前端不显示该卡片。
    conclusion: Optional[str] = None
