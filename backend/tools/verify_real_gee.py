"""真实 GEE 端到端验证：直接用兜底模板代码跑一次沙箱执行。

用途：接入 GEE 项目后，不依赖大模型生成的代码，验证「沙箱 + 真实 Earth Engine」
这条链路本身是否通、以及模板代码的资源约束是否合理（逐月曲线不能出现一串 0）。

用法：
    .venv/Scripts/python.exe tools/verify_real_gee.py [区域名]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.codegen import _fallback_code  # noqa: E402
from app.config import settings  # noqa: E402
from app.execution.sandbox import run_in_sandbox  # noqa: E402
from app.models import AnalysisRequest, TaskType  # noqa: E402


def main() -> int:
    region = sys.argv[1] if len(sys.argv) > 1 else "太湖流域"
    req = AnalysisRequest(
        task_type=TaskType.ndvi,
        region=region,
        start_date="2024-01-01",
        end_date="2024-12-31",
        cloud_threshold=20,
    )

    print(f"区域={region}  后端={settings.execution_backend}  超时={settings.gee_timeout}s")
    code = _fallback_code(req)
    res = run_in_sandbox(
        code,
        {
            "region": req.region,
            "start_date": req.start_date,
            "end_date": req.end_date,
            "cloud_threshold": req.cloud_threshold,
        },
    )

    print(f"ok={res.ok}")
    if not res.ok:
        if res.stdout:
            print("--- 失败前 stdout ---")
            print(res.stdout)
        print("错误：", res.error)
        return 1

    print("--- stdout ---")
    print(res.stdout)
    print(f"--- 图层 {len(res.layers)} 个 / 图表 {len(res.charts)} 个 ---")
    for lyr in res.layers:
        print("  图层:", lyr.get("name"), lyr.get("kind"), str(lyr.get("tile_url"))[:90])
    for ch in res.charts:
        print("  图表:", ch.get("title"), ch.get("labels"))
        for s in ch.get("series") or []:
            print("        ", s.get("name"), s.get("data"))

    # 逐月曲线若过半为空，说明云量/采样策略仍有问题
    for ch in res.charts:
        for s in ch.get("series") or []:
            data = s.get("data") or []
            empty = sum(1 for v in data if v is None or v == 0)
            if data and empty > len(data) / 2:
                print(f"[警告] 曲线「{s.get('name')}」有 {empty}/{len(data)} 个缺失值，采样策略需再调")
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
