"""复现单条历史任务，用于定位失败原因。

## 为什么需要它

任务失败时，界面上和 `logs` 里只有一行摘要（如 `[gee]：KeyError: 'ndvi'`），
看不到出错行。而要修代码 / 判断是产品问题还是环境问题，必须知道**哪一行**炸了。

本工具把库里存的那份代码原样重跑一遍，打印完整的 stderr / traceback / stdout 尾部。
代码是从库里读的，所以复现的是**当时真正跑过的那份代码**，不受后续改代码影响。

## 用法

    cd backend
    .venv\\Scripts\\python.exe tools\\replay_task.py 0645a46f8523   # 指定任务
    .venv\\Scripts\\python.exe tools\\replay_task.py --last-failed   # 最近一条失败任务

注意：
  * 会真实执行（默认走 `.env` 里配的 EXECUTION_BACKEND，即会真调 GEE），
    耗时通常 30-75s，也**会占用任务配额之外的一次真实调用**（绕过了 /api/tasks，
    所以不受配额限制，但也因此不要拿它来刷量）。
  * 复现失败**不代表产品当前仍失败** —— 代码可能已被修复，任务记录是旧的。
    它回答的是"当时为什么炸"，不是"现在还会不会炸"。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app import crypto  # noqa: E402
from app.config import settings  # noqa: E402
from app.execution import get_backend  # noqa: E402
from app.models import AnalysisRequest, TaskType  # noqa: E402


def pick_task(conn: sqlite3.Connection, task_id: str | None, last_failed: bool):
    conn.row_factory = sqlite3.Row
    if last_failed or not task_id:
        row = conn.execute(
            "SELECT * FROM tasks WHERE status='failed' AND code IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("库里没有失败任务。")
            sys.exit(1)
        return row
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        print("找不到任务 %s" % task_id)
        sys.exit(1)
    return row


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    last_failed = "--last-failed" in sys.argv
    tid = args[0] if args else None

    db = BACKEND / "data" / "app.db"
    conn = sqlite3.connect(db)
    row = pick_task(conn, tid, last_failed)

    # 本工具直接开 sqlite 读原始行，绕过了 TaskStore 的加解密层，
    # 所以敏感字段（region / code）要在这里显式解密 —— 否则拿到的是 ENC1: 密文，
    # 拿去执行必然炸，而且报错信息会指向"代码语法错误"，把人往完全错误的方向带。
    task_id = row["task_id"]
    status = row["status"]
    attempts = row["attempts"]
    task_type = row["task_type"]
    region = crypto.decrypt_field(row["region"])
    start_date = row["start_date"]
    end_date = row["end_date"]
    cloud_threshold = row["cloud_threshold"]
    code = crypto.decrypt_field(row["code"])

    print("=" * 72)
    print("任务        = %s" % task_id)
    print("状态        = %s（尝试 %s 次）" % (status, attempts))
    print("类型/区域   = %s / %s" % (task_type, region))
    print("时间范围    = %s ~ %s" % (start_date, end_date))
    print("执行后端    = %s" % settings.execution_backend)
    print("=" * 72)

    if not code:
        print("该任务没有存代码，无法复现。")
        return 1

    req = AnalysisRequest(
        task_type=TaskType(task_type),
        region=region,
        start_date=start_date,
        end_date=end_date,
        cloud_threshold=cloud_threshold,
    )

    out = get_backend(settings.execution_backend).execute(code, req)

    print("复现结果 ok =", out.ok)
    if out.error:
        print("error        =", {k: v for k, v in out.error.items() if k != "traceback"})
        tb = (out.error.get("traceback") or "").strip()
        if tb:
            print("-" * 72)
            print("traceback：")
            print(tb)
    if out.stdout:
        print("-" * 72)
        print("stdout（尾部 2000 字）：")
        print(out.stdout[-2000:])
    if out.layers or out.charts:
        print("-" * 72)
        print("产物：%d 个图层 / %d 个图表" % (len(out.layers), len(out.charts)))

    return 0 if out.ok else 1


if __name__ == "__main__":
    sys.exit(main())
