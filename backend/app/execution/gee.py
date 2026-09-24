"""真实 GEE 执行后端。

链路：解析凭据 → 子进程受限沙箱 exec 代码 → 回收图层（瓦片/GeoJSON）与图表。
凭据未就绪时返回 auth 类错误，由编排层终止重试（重试无意义），并提示切回 offline。
"""

from __future__ import annotations

from ..gee_auth import diagnose
from ..models import AnalysisRequest, ChartData, ExecutionOutcome, LayerData
from .base import ExecutionBackend
from .sandbox import run_in_sandbox


class GEEBackend(ExecutionBackend):
    name = "gee"

    def execute(self, code: str, request: AnalysisRequest) -> ExecutionOutcome:
        diag = diagnose()
        if not diag.get("package_installed"):
            return ExecutionOutcome(ok=False, error={"category": "auth", "message": diag["message"]})
        if not diag.get("credentials_found"):
            return ExecutionOutcome(
                ok=False,
                error={"category": "auth", "message": f"GEE 凭据未就绪：{diag['message']}"},
            )
        if not diag.get("initialized"):
            return ExecutionOutcome(
                ok=False,
                error={"category": "auth", "message": f"GEE 初始化失败：{diag['message']}"},
            )

        res = run_in_sandbox(
            code,
            {
                "region": request.region,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "cloud_threshold": request.cloud_threshold,
            },
        )
        if not res.ok:
            err = res.error or {"category": "gee", "message": "未知错误"}
            if res.stdout:
                err["traceback"] = res.stdout[-1500:]
            return ExecutionOutcome(ok=False, stdout=res.stdout, error=err)

        layers = []
        for l in res.layers:
            layers.append(
                LayerData(
                    name=l.get("name", "分析结果"),
                    kind=l.get("kind", "geojson"),
                    geojson=l.get("geojson"),
                    tile_url=l.get("tile_url"),
                    legend=l.get("legend") or [],
                    # AOI 边界由沙箱从用户代码里的 aoi 变量求得；
                    # 前端靠它把视口定位到真正出图的位置（不再按区域名猜）。
                    bbox=l.get("bbox"),
                )
            )
        charts = []
        for c in res.charts:
            charts.append(
                ChartData(
                    title=c.get("title", "统计结果"),
                    kind=c.get("kind", "line"),
                    labels=[str(x) for x in c.get("labels", [])],
                    series=c.get("series", []),
                )
            )
        stdout = res.stdout or "[GEE] 执行完成"
        if not layers and not charts:
            stdout += "\n[提示] 代码未产生可视图层；可在代码中用 WB.add_image_layer(...) 上报结果。"
        # stats 从沙箱一路带出来（代码里的 WB.stat(k, v)），供结论摘要层使用。
        # 用 getattr 兜底：统计只是"附加信息"，绝不该因为它缺失就把一个
        # 已经执行成功的任务判成失败（2026-09-20 的 AttributeError 就是这么来的）。
        return ExecutionOutcome(
            ok=True, layers=layers, charts=charts, stdout=stdout,
            stats=getattr(res, "stats", None) or {},
        )
