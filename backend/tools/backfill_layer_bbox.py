"""给历史任务的栅格图层回填地理范围（bbox）—— 修掉"打开旧任务地图跳错地方"。

## 为什么需要这个一次性迁移

2026-09-23 定位到一类 bug：**地图定位到了 1000 km 外的另一个省**。
根因（详见 `tools/test_region_map_fallback.py` 的文件头注释）是纯栅格图层
（只有 `tile_url`、没有 `geojson`）算不出地理范围，前端只能退到"按区域名查字典"
或更糟的硬编码太湖坐标。

修复方案是把 AOI 的真实边界（`bbox`）从后端一路带到前端。但**修复只对之后新建的
任务生效** —— 生产库里已有的栅格任务，`result.layers[*].bbox` 全都是 null：

    栅格任务总数 61 条，其中带 bbox 的：0 条

于是"用户点开历史任务，地图仍然跳错地方"。这个脚本把那 61 条补上。

## 设计取舍（这几条都是刻意的）

1. **默认 dry-run。** 不加 `--apply` 只看不写。对生产库的批量写入不该是
   "手一抖就执行"的动作。

2. **只在能从任务自身推出范围时才回填。** 优先用同任务里矢量图层的
   geojson 坐标（那 34 条任务自带 AOI 边界，可以精确算出来）；
   其余按区域名查内置字典（`app/regions.py`）。
   **推不出范围的（如"深圳市""恩施大峡谷"）一律跳过并列出** ——
   绝不猜一个坐标塞进去。这个 bug 的教训就是"错误的自信比明确的失败危险得多"，
   迁移工具自己不能重犯。

3. **绝不动已有数据。** 只给 `bbox` 为空的图层补字段；已有 bbox 的、
   `geojson` 图层的、`result` 解析失败的，一律原样保留。

4. **不绕过 `task_store`。** 通过 `store.update(tid, result=...)` 写入，
   这样 `result` 字段会自动走 `crypto.encrypt_field`（业务数据加密），
   而不是在脚本里手搓密文 —— 手搓的话一旦漏了加密，就是往库里塞明文。
   （注意 `store.update` 里 `result` 分支调的是 `.model_dump()`，
   所以这里要先还原成 `ExecutionOutcome` 对象。）

5. **改前先备份**，文件名带 `before-bbox-backfill-<时间戳>`，
   和 `.db_backup/` 下已有的备份命名习惯一致。

用法（在 backend 目录下）：

    # 1) 先看一遍要改什么（不写库）
    .venv\\Scripts\\python.exe tools\\backfill_layer_bbox.py

    # 2) 确认无误后执行
    .venv\\Scripts\\python.exe tools\\backfill_layer_bbox.py --apply

    # 3) 复核（只看不写，断言"栅格图层都有 bbox 或明确标为无法定位"）
    .venv\\Scripts\\python.exe tools\\backfill_layer_bbox.py --verify

⚠️ 不要传 `$PWD` 当 `--db` 参数：本机 Git Bash 的 `$PWD` 是 `/c/...`，
Windows 版 Python 会把它解析成 `C:\\c\\...`，静默建出一个空库。
直接省略 `--db` 用默认路径即可。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../backend/tools
BACKEND = HERE.parent                            # .../backend
sys.path.insert(0, str(BACKEND))

from app import crypto                                   # noqa: E402
from app.crypto import decrypt_field as _dec             # noqa: E402
from app.models import ExecutionOutcome                  # noqa: E402
from app.regions import get_center                       # noqa: E402
from app.execution._runner import _geojson_bbox          # noqa: E402

DEFAULT_DB = BACKEND / "data" / "app.db"
BACKUP_DIR = BACKEND / ".db_backup"


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 从任务自身推导 AOI 范围
# ---------------------------------------------------------------------------
def _bbox_from_geojson_layers(layers: list[dict]) -> list[float] | None:
    """优先从矢量图层的 geojson 坐标算范围 —— 这是任务自己的真实 AOI。

    比"按区域名查字典"可信得多：坐标是当时生成的代码实际用的。
    """
    boxes = []
    for l in layers:
        if l.get("bbox"):
            continue
        gj = l.get("geojson")
        if not gj:
            continue
        b = _geojson_bbox(gj)
        if b:
            boxes.append(b)
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _bbox_from_region(region: str) -> tuple[list[float], str] | None:
    """退一步：按区域名查内置字典，返回中心 0.5°×0.5° 的范围。

    半边长 0.25° 与 `codegen._fallback_code` 里内置区域框的写法保持一致，
    这样回填出来的范围和当时"若走兜底路径会画出的范围"是同一个。

    返回 (bbox, 说明)。查不到返回 None —— **不猜**。
    """
    c = get_center(region or "")
    if not c:
        return None
    lon, lat = c
    return ([lon - 0.25, lat - 0.25, lon + 0.25, lat + 0.25],
            f"内置区域库「{region}」中心 ({lon}, {lat}) ± 0.25°")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT task_id, region, result, user_id FROM tasks WHERE result IS NOT NULL"
        )]
    finally:
        conn.close()


def plan(rows: list[dict]) -> tuple[list[dict], list[dict], Counter]:
    """算出每个任务要做什么。**纯函数，不碰数据库。**

    返回 (可回填的, 无法定位的, 统计)。
    """
    todo: list[dict] = []
    skipped: list[dict] = []
    stats: Counter = Counter()

    for r in rows:
        tid = r["task_id"]
        try:
            region = _dec(r["region"]) if r["region"] else ""
        except Exception:  # noqa: BLE001
            region = ""
        try:
            data = json.loads(_dec(r["result"]))
        except Exception as e:  # noqa: BLE001
            stats["result_解析失败"] += 1
            skipped.append({"task_id": tid, "region": region, "why": f"result 解析失败：{e}"})
            continue

        layers = data.get("layers") or []
        raster = [l for l in layers if l.get("tile_url")]
        if not raster:
            stats["无栅格图层"] += 1
            continue
        targeted = [l for l in raster if not l.get("bbox")]
        if not targeted:
            stats["已有 bbox（无需处理）"] += 1
            continue

        # ① 任务自带的 AOI 边界最可信
        src = None
        bbox = _bbox_from_geojson_layers(layers)
        if bbox:
            src = "同任务 geojson 图层坐标"
        else:
            # ② 退到内置区域库
            got = _bbox_from_region(region)
            if got:
                bbox, desc = got
                src = desc

        if not bbox:
            stats["无法定位（跳过）"] += 1
            skipped.append({"task_id": tid, "region": region,
                            "why": "既无矢量图层坐标、区域名也不在内置库中"})
            continue

        stats["可回填"] += 1
        todo.append({
            "task_id": tid, "region": region, "data": data,
            "layer_names": [l.get("name") for l in targeted],
            "bbox": bbox, "src": src,
        })

    return todo, skipped, stats


def apply_one(store, item: dict) -> None:
    """把 bbox 写回图层，并走 task_store 正常路径存库（自动加密）。"""
    bbox = item["bbox"]
    for l in item["data"]["layers"]:
        if l.get("tile_url") and not l.get("bbox"):
            l["bbox"] = list(bbox)
    outcome = ExecutionOutcome(**item["data"])
    store.update(item["task_id"], result=outcome)


def backup(db: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"{db.name}.before-bbox-backfill-{ts}"
    shutil.copy2(db, dst)
    return dst


def verify(db: Path) -> int:
    """复核：栅格图层要么有 bbox，要么所在任务被明确记为"无法定位"。"""
    rows = load_rows(db)
    n_raster = n_ok = 0
    unknown = Counter()
    for r in rows:
        try:
            data = json.loads(_dec(r["result"]))
        except Exception:  # noqa: BLE001
            continue
        raster = [l for l in (data.get("layers") or []) if l.get("tile_url")]
        if not raster:
            continue
        n_raster += 1
        if all(l.get("bbox") for l in raster):
            n_ok += 1
        else:
            try:
                region = _dec(r["region"]) if r["region"] else ""
            except Exception:  # noqa: BLE001
                region = ""
            unknown[region] += 1
    _log(f"  栅格任务总数：{n_raster}")
    _log(f"  栅格图层已全部带 bbox：{n_ok}")
    _log(f"  仍无 bbox（应为「无法定位」那些）：{n_raster - n_ok}")
    for k, v in unknown.most_common():
        _log(f"      {k or '(区域名解密失败)':16} {v} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="给历史任务栅格图层回填 bbox")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="数据库路径（默认 data/app.db）")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只预览）")
    ap.add_argument("--verify", action="store_true", help="只复核不回填")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        _log(f"❌ 找不到数据库：{db}")
        return 2
    _log(f"数据库：{db}  （{db.stat().st_size/1024/1024:.1f} MB）")

    if args.verify:
        _log("\n=== 复核（只读）===")
        return verify(db)

    rows = load_rows(db)
    todo, skipped, stats = plan(rows)

    _log(f"\n=== 扫描 {len(rows)} 条有结果的任务 ===")
    for k in ("可回填", "无法定位（跳过）", "已有 bbox（无需处理）",
              "无栅格图层", "result_解析失败"):
        if stats.get(k):
            _log(f"  {k}：{stats[k]}")

    if not todo:
        _log("\n没有需要回填的任务。")
    else:
        _log(f"\n=== 待回填 {len(todo)} 条（按区域）===")
        for k, v in Counter(t["region"] for t in todo).most_common():
            _log(f"  {k:16} {v} 条")
        _log("\n  样例（前 5 条）：")
        for t in todo[:5]:
            _log(f"    {t['task_id']}  {t['region']:12} → bbox={[round(x,4) for x in t['bbox']]}"
                 f"  来源：{t['src']}")

    if skipped:
        _log(f"\n=== 无法定位、将跳过（{len(skipped)} 条）===")
        for k, v in Counter(s["region"] for s in skipped).most_common():
            _log(f"  {k or '(空)':16} {v} 条")
        _log("  ⚠ 这些任务打开后地图仍是全局视图并给出提示 —— "
             "在下方补坐标或改用内置区域名可解决：")
        for s in skipped[:8]:
            _log(f"    {s['task_id']}  {s['region'] or '(空)'}  —— {s['why']}")

    if not args.apply:
        _log("\n（预览模式，未写库。确认无误后加 --apply 执行）")
        return 0

    # ---- 写入 ----
    _log("\n=== 执行 ===")
    bak = backup(db)
    _log(f"  已备份：{bak}")

    from app.task_store import TaskStore  # 延后导入：确保上面的预览不依赖服务环境
    store = TaskStore(str(db))
    done = 0
    failed = []
    for t in todo:
        try:
            apply_one(store, t)
            done += 1
        except Exception as e:  # noqa: BLE001
            failed.append((t["task_id"], f"{type(e).__name__}: {e}"))
    _log(f"  已回填：{done}/{len(todo)}")
    if failed:
        _log(f"  ❌ 失败 {len(failed)} 条：")
        for tid, why in failed[:10]:
            _log(f"    {tid}  {why}")
        _log("  （回填是逐条幂等的，修掉原因后可重跑本脚本）")

    _log("\n=== 复核 ===")
    verify(db)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
