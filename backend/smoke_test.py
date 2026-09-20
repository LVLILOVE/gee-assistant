"""离线链路冒烟测试：不启动服务，直接跑编排，验证四类任务都能出结果。"""

from app import orchestrator
from app.models import AnalysisRequest, TaskType

if __name__ == "__main__":
    for tt in TaskType:
        req = AnalysisRequest(task_type=tt, region="太湖流域")
        r = orchestrator.run_analysis(req)
        res = r["result"]
        print(
            f"{tt.value:16s} -> status={r['status']:9s} attempts={r['attempts']} "
            f"layers={len(res.layers)} charts={len(res.charts)} ok={res.ok}"
        )
        for log in r["logs"]:
            print("   ", log)
