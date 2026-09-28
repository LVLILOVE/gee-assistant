#!/usr/bin/env python
"""2026-09-28 产品自检发现的缺陷 —— 回归守卫。

为什么单独一个文件：这些缺陷**回归套件原本一条都覆盖不到**（当时 537/537 全绿，
但下面每一条都是真实可复现的错误行为）。全绿却不等于没问题，所以把这次的
判据钉死在这里，防止将来改动把它们悄悄带回来。

覆盖的 7 个缺陷（每条都是"能构造出触发路径"的真实问题，不是理论风险）：

  A. 结论摘要的"峰值/最低月份"在有空月时指到错月份
     —— 过滤 None 后的下标被直接拿去索引 labels。
  B. 兜底代码把用户可控文本（区域名/日期）未转义就拼进生成代码
     —— 一个引号 = SyntaxError；一个换行 = 注释逃逸 + 代码注入。
  C. fail_orphans() 拿**密文**直接 json.loads
     —— 抛异常被吞 → 上次中断任务的日志被整体抹掉，且把明文写回加密列。
  D. 意图解析的 start/end 各自独立兜底 → 可产出倒置区间
     —— GEE filterDate 静默过滤成空集合，白烧重试。
  E. 枚举外的 task_type 被判成 complete
     —— 前端自动提交 → Pydantic 422，用户无法自助恢复。
  F. 瓦片代理的重试层数叠乘 → 单张瓦片尾部延迟可达分钟级
     —— 同步端点占满线程池 → 整个服务假死。
  G. 沙箱图层生成失败只写进 Reporter.notes，而 notes 从不被读
     —— 任务报 succeeded 但地图空白，用户拿不到失败原因。

纪律：只用临时库 + 临时密钥，**绝不碰生产 data/app.db**。
输出格式必须收尾于「结果：N/M 通过」（regress_all.sh 靠它抓结果）。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

# ---------------------------------------------------------------- 环境隔离
# ⚠ 必须在 import app.* 之前落盘：一 import app.task_store 就会触发
#   建表 + 加密回填，不隔离的话会真改写线上库（本项目被这一步误伤过）。
_HERE = pathlib.Path(__file__).resolve().parent
_BACKEND = _HERE.parent
sys.path.insert(0, str(_BACKEND))

_tmp = tempfile.mkdtemp(prefix="audit_regress_")
os.environ["APP_DB_PATH"] = str(pathlib.Path(_tmp) / "app.db")
os.environ["WB_DATA_KEY_PATH"] = str(pathlib.Path(_tmp) / "data.key")

from app import crypto  # noqa: E402
from app import tile_proxy  # noqa: E402
from app.codegen import _fallback_code  # noqa: E402
from app.conclusion import _chart_digest  # noqa: E402
from app.intent import _summarize  # noqa: E402
from app.models import AnalysisRequest, TaskType  # noqa: E402
from app.net_retry import retry_policy  # noqa: E402
from app.task_store import TaskStore  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  -> {detail}" if detail else ""))


def _top_level_imports(code: str) -> list[str]:
    """列出生成代码里**顶层可执行到的** import 的顶层模块名。

    用于判定"注入是否成功"：注入的语句一定表现为多出一个 import。
    """
    return sorted({
        n.names[0].name.split(".")[0]
        for n in ast.walk(ast.parse(code))
        if isinstance(n, (ast.Import, ast.ImportFrom)) and n.names
    })


print("=" * 66)
print(" 产品自检修复回归（2026-09-28）   临时目录 " + _tmp)
print("=" * 66)

# ============================================================ A 结论下标
print("\n[A] 结论摘要：空月不得导致峰值/最低月份错位")
d1 = _chart_digest([{
    "title": "逐月 NDVI 均值", "kind": "line",
    "labels": ["1月", "2月", "3月"],
    "series": [{"name": "NDVI", "data": [None, 0.3, 0.5]}],
}])
check("中间月缺测时峰值指到 3月而非 2月",
      "峰值 0.5（3月）" in d1 and "峰值 0.5（2月）" not in d1, d1)
check("中间月缺测时最低指到 2月", "最低 0.3（2月）" in d1, d1)

d2 = _chart_digest([{
    "title": "t", "kind": "line",
    "labels": ["1月", "2月", "3月"],
    "series": [{"name": "v", "data": [0.9, None, 0.1]}],
}])
check("首月缺测时下标仍对齐（峰值 1月 / 最低 3月）",
      "峰值 0.9（1月）" in d2 and "最低 0.1（3月）" in d2, d2)

# ============================================================ B 兜底代码转义
print("\n[B] 兜底代码：用户可控文本必须转义（不得语法错误 / 不得注入）")
try:
    code_q = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="L'Aquila"))
    compile(code_q, "<gen>", "exec")
    check("含单引号的正常地名可编译（原先是 SyntaxError）", True)
except SyntaxError as exc:
    check("含单引号的正常地名可编译（原先是 SyntaxError）", False, str(exc))

_INJECT = '太湖"\nimport os\nimport subprocess\nprint("PWNED")\n#  '
try:
    code_i = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region=_INJECT))
    compile(code_i, "<gen>", "exec")
    imports = _top_level_imports(code_i)
    bad = [m for m in ("os", "subprocess", "sys", "shutil") if m in imports]
    check("区域名无法逃逸注释注入语句", not bad, f"imports={imports}")
except SyntaxError as exc:
    check("区域名无法逃逸注释注入语句", False, f"SyntaxError: {exc}")

try:
    code_d = _fallback_code(AnalysisRequest(
        task_type=TaskType.water, region="太湖流域",
        start_date="2025-01-01';import os#", end_date="2025-12-31"))
    compile(code_d, "<gen>", "exec")
    imports_d = _top_level_imports(code_d)
    check("日期字段无法注入语句", "os" not in imports_d, f"imports={imports_d}")
except SyntaxError as exc:
    check("日期字段无法注入语句", False, f"SyntaxError: {exc}")

code_n = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="太湖流域"))
check("正常区域仍能生成 AOI 矩形（修转义不能把功能修坏）",
      "ee.Geometry.Rectangle([" in code_n)

# ============================================================ C fail_orphans
print("\n[C] fail_orphans：不得抹掉日志 / 不得破坏加密不变量")
store = TaskStore(pathlib.Path(_tmp) / "app.db")
tid = store.create(AnalysisRequest(task_type=TaskType.ndvi, region="守卫测试区域"), user_id=1)
store.update(tid, status="running", logs=["[生成] 第一版代码", "[执行] 沙箱返回 ok"])
handled = store.fail_orphans()
check("能识别并处理遗留的 running 任务", handled >= 1, f"处理 {handled} 条")

conn = sqlite3.connect(str(pathlib.Path(_tmp) / "app.db"))
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT status, logs FROM tasks WHERE task_id=?", (tid,)).fetchone()
conn.close()
check("写回的 logs 仍是密文（加密列恒为密文）",
      bool(row["logs"]) and crypto.is_encrypted(row["logs"]),
      f'前缀={(row["logs"] or "")[:5]}')
kept = json.loads(crypto.decrypt_field(row["logs"]))
check("原有日志未被抹掉（原先是整体丢失）",
      "[生成] 第一版代码" in kept and "[执行] 沙箱返回 ok" in kept, str(kept))
check("已追加重启说明且状态置为 failed",
      any("服务重启" in x for x in kept) and row["status"] == "failed")

# ============================================================ D/E 意图解析
print("\n[D] 意图解析：不得产出倒置的时间区间")
r1 = _summarize({"task_type": "ndvi", "region": "北京市", "start_date": "2026-03-01"}, "test")
f1 = r1["fields"]
check("只给 start 时区间仍然有序", f1["start_date"] <= f1["end_date"],
      f'{f1["start_date"]} ~ {f1["end_date"]}')
r2 = _summarize({"task_type": "ndvi", "region": "北京市",
                 "start_date": "2025-01-01", "end_date": "2025-12-31"}, "test")
check("合法区间不被改动（避免修出新问题）",
      r2["fields"]["start_date"] == "2025-01-01"
      and r2["fields"]["end_date"] == "2025-12-31")

print("\n[E] 意图解析：枚举外的 task_type 不得判成 complete")
r3 = _summarize({"task_type": "vegetation", "region": "北京市"}, "test")
check("非法 task_type → complete=False 且 task_type 置空",
      r3["complete"] is False and r3["fields"]["task_type"] is None,
      f'complete={r3["complete"]} task_type={r3["fields"]["task_type"]}')
r4 = _summarize({"task_type": "ndvi", "region": "北京市",
                 "start_date": "2025-01-01", "end_date": "2025-12-31"}, "test")
check("合法 task_type 仍判 complete（别把正常路径堵死）", r4["complete"] is True)

# ============================================================ F 瓦片重试封顶
print("\n[F] 瓦片代理：尾部延迟必须封顶（否则同步端点占满线程池）")
worst = tile_proxy.TILE_RETRY_ATTEMPTS * tile_proxy.TILE_TIMEOUT
check("单张瓦片最坏耗时 < 60s", worst < 60, f"约 {worst}s")
check("适配器层已关闭重试（避免内外两层叠乘）", retry_policy(0).total == 0,
      f"adapter total={retry_policy(0).total}")
check("外层重试次数 <= 3", tile_proxy.TILE_RETRY_ATTEMPTS <= 3,
      f"attempts={tile_proxy.TILE_RETRY_ATTEMPTS}")

# ============================================================ G 图层警告可见
print("\n[G] 沙箱：图层生成失败必须可见（不得静默报成功）")
from app.execution.sandbox import run_in_sandbox  # noqa: E402

res = run_in_sandbox(
    "import ee\nWB.note('模拟图层生成失败：getMapId 超限')\nWB.stat('面积', 1.23)\nprint('done')\n",
    {"region": "太湖流域", "start_date": "2025-01-01", "end_date": "2025-12-31"},
    mock=True,
)
check("mock 执行成功", res.ok is True)
check("stats 仍然贯通（别把统计契约弄坏）", res.stats.get("面积") == 1.23, str(res.stats))
check("图层警告已进入用户可见 stdout", "图层警告" in res.stdout, repr(res.stdout[:120]))
check("原有 print 输出未丢失", "done" in res.stdout)

# ============================================================ 汇总
print()
print("=" * 66)
total = PASS + FAIL
print(f"结果：{PASS}/{total} 通过")
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
