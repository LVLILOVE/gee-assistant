"""量化指标评测 runner：把任务书里的四个指标跑成可复现的数字。

对应任务书「2.2 具体目标」第 6 条的四个量化指标：

    指标一  意图识别准确率   ≥ 90%
    指标二  一次执行成功率   ≥ 70%
    指标三  调试后完成率     ≥ 85%
    指标四  典型任务耗时     ≤ 60s

## 为什么需要它

在此之前，这四个指标的处境是「有弱信号、无证据」：线上 22 条真实任务能算出一个
好看的一次成功率（19/22 = 86.4%），但样本小、非受控、不可复现，而且耗时指标在
架构上根本测不出来（tasks 表原先没有 started_at/finished_at）。
验收要的不是"看起来还行"，而是"按固定任务集跑出来的可复现数字"。

## 数据来源

  * 指标一：调用 app.intent.parse，只依赖 DeepSeek，**不依赖 GEE**，本机可跑。
  * 指标二/三/四：通过 HTTP 提交真实任务到本机服务（会真的调 GEE），
    状态/尝试次数/耗时从 tasks 表读——埋点写的就是那里，是权威来源。
    读库而不读日志，是因为日志重定向到文件时是块缓冲，重启期间那几行可能没落盘。

## 用法

    cd backend
    .venv\\Scripts\\python.exe tools\\eval_metrics.py --self-test   # 只自检判定规则
    .venv\\Scripts\\python.exe tools\\eval_metrics.py --intent-only # 只跑指标一（快）
    .venv\\Scripts\\python.exe tools\\eval_metrics.py               # 全量（含真实 GEE）
    .venv\\Scripts\\python.exe tools\\eval_metrics.py --limit 2     # 端到端只跑前 2 条

退出码：全部指标达标为 0，否则 1。
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(TOOLS))

from app import intent  # noqa: E402
from app.config import settings  # noqa: E402
from app.task_store import store  # noqa: E402
from eval_dataset import E2E_CASES, INTENT_CASES  # noqa: E402

# 任务书里的目标值，集中一处便于核对
TARGET = {
    "intent_accuracy": 90.0,
    "first_run_success": 70.0,
    "post_debug_completion": 85.0,
    "median_latency_s": 60.0,
}

AVG_DAYS_PER_MONTH = 30.44


# =====================================================================
# 日期判定
# =====================================================================
def _d(s) -> date | None:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def judge_date(spec: dict, start, end, today: date) -> tuple[bool, str]:
    """按 eval_dataset 里的规则词汇表判定时间区间。返回 (是否通过, 说明)。"""
    kind = spec.get("kind", "any")
    if kind == "any":
        return True, "不检查"

    s, e = _d(start), _d(end)
    if not s or not e:
        return False, f"日期缺失或非法：{start!r} ~ {end!r}"
    if s > e:
        return False, f"起止倒置：{s} ~ {e}"

    if kind == "exact":
        es, ee = _d(spec["start"]), _d(spec["end"])
        ok = abs((s - es).days) <= 1 and abs((e - ee).days) <= 1
        return ok, f"{s} ~ {e}｜期望 {es} ~ {ee}"

    if kind == "months":
        first, last = spec["months"][0], spec["months"][-1]
        # 期望年份 = 最后一个月的月末还没过的最近一年
        y = today.year
        if _last_day(y, last) > today:
            y -= 1
        es, ee = date(y, first, 1), _last_day(y, last)
        ok = (
            s.month == first
            and e.month == last
            and abs((s - es).days) <= 1
            and abs((e - ee).days) <= 1
        )
        return ok, f"{s} ~ {e}｜期望 {es} ~ {ee}"

    if kind == "recent":
        max_age = spec["max_age_months"]
        cutoff = today - timedelta(days=int(round(max_age * AVG_DAYS_PER_MONTH)))
        problems = []
        if e < cutoff:
            problems.append(
                f"结束日 {e} 早于下限 {cutoff}（时间基准过旧，超 {max_age} 个月）"
            )
        if e > today:
            problems.append(f"结束日 {e} 在未来（今天 {today}）")
        cap = spec.get("max_span_months")
        if cap:
            span = (e - s).days
            if span > int(round(cap * AVG_DAYS_PER_MONTH)):
                problems.append(f"跨度 {span} 天超出 {cap} 个月上限")
        return (not problems), ("；".join(problems) if problems else f"{s} ~ {e} 落在近期窗口内")

    return False, f"未知规则 kind={kind!r}"


# =====================================================================
# 指标四：耗时的统计口径
# =====================================================================
def sample_median(values) -> float | None:
    """样本中位数：偶数个样本取中间两个的均值（= `statistics.median` 的定义）。

    单独抽成函数，是为了让 self_test 能测到**指标四真正调用的那段代码**。
    如果测试里自己写一遍 `statistics.median`，那么源码哪天被改回
    `lat[len(lat)//2]`（上中位数）也测不出来 —— 测试和被测代码必须是同一份。
    """
    vals = sorted(v for v in values if v)
    return statistics.median(vals) if vals else None


# =====================================================================
# 判定规则自检
# =====================================================================
def self_test() -> int:
    """验证判定规则本身可信——特别是"能抓到修复前的那个错误值"。

    一个抓不到已知 bug 的评测规则等于没有规则：如果 judge_date 对
    2024-12-31（修复前"现在"被解析成的值）也放行，那么指标一就会假通过。
    """
    print("\n[自检] 日期判定规则")
    today = date(2026, 9, 17)
    ok = []

    def c(name, cond, detail=""):
        ok.append(bool(cond))
        print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(detail)) if detail else ""))

    recent18 = {"kind": "recent", "max_age_months": 18}
    months68 = {"kind": "months", "months": [6, 8]}

    # 核心：修复前的错误值必须被判错
    good, why = judge_date(recent18, "2024-01-01", "2024-12-31", today)
    c("修复前的 2024 兜底值被判错", not good, why)

    # 修复后的兜底值（最近完整自然年）必须放行
    good, why = judge_date(recent18, "2025-01-01", "2025-12-31", today)
    c("修复后的 2025 兜底值被判对", good, why)

    # 显式年份不能有容差滥用
    c("exact 命中", judge_date({"kind": "exact", "start": "2023-01-01", "end": "2023-12-31"},
                              "2023-01-01", "2023-12-31", today)[0])
    c("exact 年份错被判错", not judge_date({"kind": "exact", "start": "2023-01-01", "end": "2023-12-31"},
                                          "2024-01-01", "2024-12-31", today)[0])

    # 月份区间：6-8 月在 2026-09 应补成 2026 年
    c("months 6-8 月补成 2026", judge_date(months68, "2026-06-01", "2026-08-31", today)[0])
    c("months 6-8 月补成 2025 被判错", not judge_date(months68, "2025-06-01", "2025-08-31", today)[0])

    # 月份区间还没过完时（今天 2026-05）应取上一年
    c("5 月跑 6-8 月取上一年",
      judge_date(months68, "2025-06-01", "2025-08-31", date(2026, 5, 20))[0])

    # 跨度上限要生效
    capped = {"kind": "recent", "max_age_months": 3, "max_span_months": 2}
    c("跨两年虽近期但超跨度被判错",
      not judge_date(capped, "2024-01-01", "2026-09-01", today)[0])

    # 起止倒置、缺失
    c("起止倒置被判错", not judge_date(recent18, "2026-05-01", "2026-01-01", today)[0])
    c("日期缺失被判错", not judge_date(recent18, None, None, today)[0])
    c("any 不检查", judge_date({"kind": "any"}, None, None, today)[0])

    # ---- 指标四的统计口径 ----
    # 用第一轮 8 条端到端的真实耗时做回归样本。真实中位数 47.5s；
    # 修复前用的上中位数会算成 56.5s（第 5 个值）。这条断言就是防止改回去。
    first_run_lat = [23.8, 25.0, 30.6, 38.5, 56.5, 61.3, 75.0, 76.0]
    c("偶数样本取标准中位数 47.5",
      sample_median(first_run_lat) == 47.5, sample_median(first_run_lat))
    c("偶数样本不再取上中位数 56.5",
      sample_median(first_run_lat) != 56.5, sample_median(first_run_lat))
    c("奇数样本取正中位", sample_median([10.0, 20.0, 30.0]) == 20.0)
    c("空样本返回 None", sample_median([]) is None)
    c("含 None 的样本被忽略", sample_median([None, 20.0, 40.0]) == 30.0)

    print("\n结果：%d/%d 通过" % (sum(ok), len(ok)))
    return 0 if all(ok) else 1


# =====================================================================
# 指标一：意图识别准确率
# =====================================================================
def judge_intent(case: dict, res: dict, today: date) -> list[tuple[str, bool, str]]:
    fields = res.get("fields") or {}
    checks: list[tuple[str, bool, str]] = []

    exp_tt = case["task_type"]
    got_tt = fields.get("task_type")
    if isinstance(exp_tt, list):
        checks.append((f"task_type ∈ {exp_tt}", got_tt in exp_tt, str(got_tt)))
    else:
        checks.append((f"task_type = {exp_tt}", got_tt == exp_tt, str(got_tt)))

    accept_region = [str(x or "").strip() for x in case["region"]]
    got_r = str(fields.get("region") or "").strip()
    checks.append((f"region ∈ {accept_region}", got_r in accept_region, got_r))

    d_ok, d_detail = judge_date(
        case["date"], fields.get("start_date"), fields.get("end_date"), today
    )
    checks.append(("时间区间", d_ok, d_detail))

    if "cloud_threshold" in case:
        got_c = fields.get("cloud_threshold")
        try:
            c_ok = got_c is not None and abs(float(got_c) - case["cloud_threshold"]) < 0.5
        except (TypeError, ValueError):
            c_ok = False
        checks.append((f"云量 = {case['cloud_threshold']}", c_ok, str(got_c)))

    exp_complete = case["complete"]
    checks.append(
        (f"complete = {exp_complete}", bool(res.get("complete")) == exp_complete,
         str(res.get("complete")))
    )

    if "missing" in case:
        got_m = res.get("missing") or []
        m_ok = all(k in got_m for k in case["missing"])
        checks.append((f"应追问 {case['missing']}", m_ok, str(got_m)))

    return checks


def _parse_once(case: dict, today: date, retries: int = 3):
    """解析一条用例，返回 (checks, degraded_reason)。

    `degraded_reason` 非空表示**这次测量本身不可信**，不计入指标：
    `app.intent.parse` 在 DeepSeek 调用失败时会静默降级到规则兜底，并把 source
    标成 `rule(XXXError)`。如果不识别这一点，一次 API 抖动就会被记成"意图识别
    错误"，指标一凭空下跌——测出来的是网络，不是产品。
    """
    last = None
    for i in range(retries):
        try:
            res = intent.parse(case["text"])
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
            continue

        src = str(res.get("source") or "")
        if src.startswith("rule("):
            last = f"大模型调用失败，已降级到规则兜底（source={src}）"
            time.sleep(1.5 * (i + 1))
            continue
        if src == "rule":
            # 没配 key 时走的是设计内的兜底路径，但要明确提示"测的不是大模型"
            return judge_intent(case, res, today), "未配置 DeepSeek key，测到的是规则兜底"
        return judge_intent(case, res, today), None

    return [("调用解析", False, last or "未知失败")], last or "未知失败"


def run_intent(repeat: int = 1) -> dict:
    """跑指标一。

    `repeat>1` 时量化波动：该指标实测存在 run-to-run 波动（同一批用例曾跑出
    100% 与 84.6%），只报单轮数字会误导验收方，必须给出区间。
    """
    print("\n" + "=" * 72)
    print("指标一：意图识别准确率（目标 ≥ %.0f%%）" % TARGET["intent_accuracy"])
    if repeat > 1:
        print("重复 %d 轮以量化波动" % repeat)
    print("=" * 72)

    today = date.today()
    stats = {c["id"]: {"case": c, "pass": 0, "valid": 0, "fails": []} for c in INTENT_CASES}
    run_rates: list = []
    degraded: list = []

    for i in range(repeat):
        passed_this = valid_this = 0
        for case in INTENT_CASES:
            checks, de = _parse_once(case, today)
            st = stats[case["id"]]
            if de:
                degraded.append((case["id"], de))
                continue
            valid_this += 1
            ok = all(c[1] for c in checks)
            st["valid"] += 1
            st["pass"] += 1 if ok else 0
            if not ok:
                st["fails"].append(checks)
            passed_this += 1 if ok else 0
        if valid_this:
            run_rates.append(100.0 * passed_this / valid_this)
        print("  第 %d 轮：%d/%d = %.1f%%"
              % (i + 1, passed_this, valid_this,
                 (100.0 * passed_this / valid_this) if valid_this else 0.0))

    if degraded:
        print("\n  ⚠ 有 %d 次测量因大模型调用失败而未计入（已排除）：%s"
              % (len(degraded), "；".join("%s: %s" % d for d in degraded[:4])))

    print("\n  逐用例稳定性：")
    flaky, rows = [], []
    for c in INTENT_CASES:
        st = stats[c["id"]]
        if st["valid"] == 0:
            continue
        tag = ("稳定通过" if st["pass"] == st["valid"]
               else ("全部失败" if st["pass"] == 0 else "不稳定"))
        if tag == "不稳定":
            flaky.append(c["id"])
        print("    %-22s %d/%d  %s" % (c["id"], st["pass"], st["valid"], tag))
        rows.append({"id": c["id"], "text": c["text"], "ok": st["pass"] == st["valid"],
                     "pass": st["pass"], "valid": st["valid"],
                     "checks": st["fails"][-1] if st["fails"] else [],
                     "why": c.get("why", "")})

    mean_rate = sum(run_rates) / len(run_rates) if run_rates else 0.0
    lo, hi = (min(run_rates), max(run_rates)) if run_rates else (0.0, 0.0)
    print("\n  【结果】平均 %.1f%%（单轮区间 %.1f%% ~ %.1f%%，共 %d 轮）  目标 ≥ %.0f%%  → %s"
          % (mean_rate, lo, hi, len(run_rates), TARGET["intent_accuracy"],
             "达标" if mean_rate >= TARGET["intent_accuracy"] else "未达标"))
    if flaky:
        print("  ⚠ 存在不稳定用例（同一输入多次结果不一致）：%s" % "、".join(flaky))
        print("    它们会让指标在目标线附近摆动，属于需要单独跟进的问题。")
    if run_rates and lo < TARGET["intent_accuracy"] <= hi:
        print("  ⚠ 单轮最低值 %.1f%% 低于目标线 —— 该指标是**压线**的，"
              "单次跑到达标不等于稳定达标。" % lo)

    return {"metric": "intent_accuracy", "value": mean_rate, "min": lo, "max": hi,
            "runs": len(run_rates), "rows": rows, "flaky": flaky, "degraded": degraded,
            "target": TARGET["intent_accuracy"],
            "ok": mean_rate >= TARGET["intent_accuracy"]}


# =====================================================================
# 指标二/三/四：端到端执行
# =====================================================================
def _submit_with_quota_retry(client, base: str, payload: dict, max_wait_s: float = 1800.0):
    """提交任务，遇到 429（配额拒绝）就等 Retry-After 后重试。

    ## 为什么必须区分 429 与执行失败

    2026-09-17 实测：第一遍评测跑完后立刻跑第二遍，后 5 条全部 429。
    原因是产品自己的配额（TASK_RATE_LIMIT_PER_HOUR 默认 20，一小时内刚好提交满 20 条
    就拒绝下一条）——**这是加固功能在正常工作，不是产品退步**。
    但当时的 runner 把 429 当成"任务失败"计入，指标二/三瞬间从 100% 掉到 25%，
    测出来的其实是"我提交得太快"，而不是"产品不行"。

    结论：测量工具必须把「被测系统的失败」和「测量基础设施的问题」分开。
    配额拒绝属于后者，正确做法是等待并重试，把它记成"排队等待"而非"执行失败"。

    返回值：(task_id 或 None, 说明字符串)
    """
    deadline = time.time() + max_wait_s
    waited = 0.0
    while True:
        resp = client.post(base + "/api/tasks", json=payload)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()["task_id"], "直接受理"

        try:
            detail = resp.json().get("detail", "")
        except Exception:  # noqa: BLE001
            detail = resp.text[:120]
        if time.time() >= deadline:
            return None, f"配额持续拒绝，等待超过 {max_wait_s:.0f}s：{detail}"

        try:
            retry_after = float(resp.headers.get("Retry-After", 30))
        except (TypeError, ValueError):
            retry_after = 30.0
        retry_after = max(5.0, min(retry_after, 120.0))
        print("     配额拒绝（%s），等待 %.0fs 后重试…" % (detail[:60], retry_after))
        time.sleep(retry_after)
        waited += retry_after


def _wait_terminal(tid: str, timeout_s: float) -> dict | None:
    deadline = time.time() + timeout_s
    row = None
    while time.time() < deadline:
        row = store.get(tid)
        if row and row.get("status") in ("succeeded", "failed"):
            return row
        time.sleep(5)
    return row


def run_e2e(limit: int | None, poll_timeout: float, max_quota_wait: float = 1800.0) -> dict:
    import httpx

    base = "http://127.0.0.1:%d" % settings.backend_port
    cases = E2E_CASES[:limit] if limit else E2E_CASES

    print("\n" + "=" * 72)
    print("指标二/三/四：端到端执行（真实 GEE，共 %d 条）" % len(cases))
    print("=" * 72)

    client = httpx.Client(timeout=30.0)
    try:
        if settings.auth_enabled and settings.admin_username:
            r = client.post(
                base + "/api/auth/login",
                json={"username": settings.admin_username,
                      "password": settings.admin_password},
            )
            r.raise_for_status()
            print("  已登录为 %s" % settings.admin_username)
        else:
            print("  未启用鉴权，直接提交")
    except Exception as e:  # noqa: BLE001
        print("  登录失败：%s" % e)
        client.close()
        return {"error": "login_failed: %s" % e, "ok": False}

    rows = []
    try:
        for case in cases:
            payload = {k: v for k, v in case.items() if k != "id"}
            print("\n  [提交] %s  %s / %s  %s~%s"
                  % (case["id"], case["task_type"], case["region"],
                     case["start_date"], case["end_date"]))
            try:
                tid, note = _submit_with_quota_retry(client, base, payload, max_quota_wait)
                if tid is None:
                    print("     提交未成功：%s" % note)
                    rows.append({"id": case["id"], "submit_error": note, "status": "submit_failed"})
                    continue
                if note != "直接受理":
                    print("     %s" % note)
            except Exception as e:  # noqa: BLE001
                print("     提交失败：%s" % e)
                rows.append({"id": case["id"], "submit_error": str(e), "status": "submit_failed"})
                continue

            row = _wait_terminal(tid, poll_timeout)
            if not row:
                print("     等待超时（%ss），仍在执行" % poll_timeout)
                rows.append({"id": case["id"], "task_id": tid, "status": "timeout"})
                continue

            started, finished = row.get("started_at"), row.get("finished_at")
            exec_s = (finished - started) if (started and finished) else None
            queue_s = (started - row["created_at"]) if started else None
            print("     状态=%-9s 尝试=%s  执行=%.1fs  排队=%.1fs"
                  % (row["status"], row.get("attempts"), exec_s or -1, queue_s or -1))
            rows.append({
                "id": case["id"], "task_id": tid, "status": row["status"],
                "attempts": row.get("attempts"), "exec_s": exec_s, "queue_s": queue_s,
            })
    finally:
        client.close()

    done = [r for r in rows if r.get("status") in ("succeeded", "failed")]
    # 配额拒绝 / 等待超时的用例**不计入分母**：它们没有产生任何关于产品的信息，
    # 计入就等于把"我提交得太快"记成"产品不行"。但必须在报告里披露，不能静默丢弃。
    infra = [r for r in rows if r.get("status") in ("submit_failed", "timeout")]
    succeeded = [r for r in done if r["status"] == "succeeded"]
    first_run = [r for r in succeeded if (r.get("attempts") or 0) == 1]

    total = len(done)
    if infra:
        print("\n  ⚠ 有 %d 条用例未进入执行（配额拒绝或等待超时），已排除出指标分母：%s"
              % (len(infra), "、".join(r["id"] for r in infra)))
        print("    这属于测量条件问题，不代表产品失败；结论请按实际样本数 %d 条解读。"
              % total)
    first_rate = 100.0 * len(first_run) / total if total else 0.0
    debug_rate = 100.0 * len(succeeded) / total if total else 0.0

    lat = sorted(r["exec_s"] for r in succeeded if r.get("exec_s"))
    # 中位数用 statistics.median（偶数样本取中间两个的均值）。
    #
    # ⚠ 这里原先写的是 lat[len(lat) // 2]，对偶数样本取的是**上中位数**：
    # 第一轮 8 条的真实中位数是 (38.5+56.5)/2 = 47.5s，却被算成 56.5s。
    # 方向上是在苛待自己（报得更差），但口径不标准 —— 验收方自己按标准中位数
    # 一算就会发现对不上，反而伤整体可信度。所以按标准定义修掉，并把修正前后
    # 的差别写进报告，而不是悄悄改小。
    median_lat = sample_median(lat)
    # 超标的单条任务必须单独报出来。只报中位数会掩盖真相：
    # 实测中位数 47.5s 达标，但 8 条里有 3 条超过 60s（最长 76s）。
    # 「典型任务耗时」按中位数口径是达标的；若验收方理解为「每条都 ≤60s」，
    # 那就没达标 —— 这个差别必须让验收方自己看到数字后判断，不能由报告替他们决定。
    over = [r for r in succeeded if (r.get("exec_s") or 0) > TARGET["median_latency_s"]]

    print("\n  【结果】")
    print("   一次执行成功率 = %d/%d = %.1f%%   目标 ≥ %.0f%%  → %s"
          % (len(first_run), total, first_rate, TARGET["first_run_success"],
             "达标" if first_rate >= TARGET["first_run_success"] else "未达标"))
    print("   调试后完成率   = %d/%d = %.1f%%   目标 ≥ %.0f%%  → %s"
          % (len(succeeded), total, debug_rate, TARGET["post_debug_completion"],
             "达标" if debug_rate >= TARGET["post_debug_completion"] else "未达标"))
    if median_lat is None:
        print("   典型任务耗时   = 无成功样本")
    else:
        print("   典型任务耗时   = 中位数 %.1fs（最小 %.1fs / 最大 %.1fs）"
              "   目标 ≤ %.0fs  → %s"
              % (median_lat, lat[0], lat[-1], TARGET["median_latency_s"],
                 "达标" if median_lat <= TARGET["median_latency_s"] else "未达标"))
        print("   ⚠ 其中 %d/%d 条单次耗时超过 %.0fs（%s）"
              % (len(over), len(succeeded), TARGET["median_latency_s"],
                 "、".join("%s %.1fs" % (r["id"].replace("e2e-", ""), r["exec_s"]) for r in over)
                 or "无"))
        print("     口径提示：本项按**中位数**判定「典型任务耗时」。若验收口径是"
              "「每条都 ≤60s」，则上述超时条目会导致该项不达标。")
        print("   注：耗时口径 = finished_at - started_at，即真实执行时长；"
              "不含排队（created_at→started_at）。")
    print("   注：「一次执行成功率」按 attempts==1 判定（首次执行即成功、未触发自动调试）。"
          "\n       若把「一次执行」理解为「一次提交交互内完成」，则该数字等于调试后完成率。")

    ok = (
        first_rate >= TARGET["first_run_success"]
        and debug_rate >= TARGET["post_debug_completion"]
        and median_lat is not None
        and median_lat <= TARGET["median_latency_s"]
    )
    return {
        "rows": rows,
        "first_run_success": {"value": first_rate, "n": len(first_run), "total": total,
                              "target": TARGET["first_run_success"],
                              "ok": first_rate >= TARGET["first_run_success"]},
        "post_debug_completion": {"value": debug_rate, "n": len(succeeded), "total": total,
                                  "target": TARGET["post_debug_completion"],
                                  "ok": debug_rate >= TARGET["post_debug_completion"]},
        "median_latency": {"value": median_lat, "min": lat[0] if lat else None,
                           "max": lat[-1] if lat else None,
                           "over": [{"id": r["id"], "exec_s": r["exec_s"]} for r in over],
                           "target": TARGET["median_latency_s"],
                           "ok": median_lat is not None and median_lat <= TARGET["median_latency_s"]},
        "ok": ok,
    }


# =====================================================================
# 报告
# =====================================================================
def write_report(intent_res: dict | None, e2e_res: dict | None) -> Path:
    out_dir = BACKEND.parent / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("评测报告-%s.md" % date.today().isoformat())

    L = []
    L.append("# 量化指标评测报告\n")
    L.append("- 评测日期：%s" % date.today().isoformat())
    L.append("- 评测入口：`backend/tools/eval_metrics.py`")
    L.append("- 测试任务集：`backend/tools/eval_dataset.py`")
    L.append("- 服务地址：`http://127.0.0.1:%d`" % settings.backend_port)
    L.append("- 解析模型：`%s`\n" % settings.deepseek_model)
    L.append("> 说明：时间期望不写死年份，全部按评测当天的日期推算——")
    L.append("> 否则评测集自身会变成下一个「年代漂移」缺陷。\n")

    L.append("## 总览\n")
    L.append("| 指标 | 结果 | 目标 | 判定 |")
    L.append("| --- | --- | --- | --- |")
    if intent_res and "value" in intent_res:
        L.append("| 意图识别准确率 | %.1f%%（%d 轮平均，单轮区间 %.1f%%~%.1f%%） | ≥ %.0f%% | %s |" % (
            intent_res["value"], intent_res["runs"], intent_res["min"], intent_res["max"],
            intent_res["target"], "达标" if intent_res["ok"] else "未达标"))
    if e2e_res and "first_run_success" in e2e_res:
        f, d, m = e2e_res["first_run_success"], e2e_res["post_debug_completion"], e2e_res["median_latency"]
        L.append("| 一次执行成功率 | %.1f%%（%d/%d） | ≥ %.0f%% | %s |" % (
            f["value"], f["n"], f["total"], f["target"], "达标" if f["ok"] else "未达标"))
        L.append("| 调试后完成率 | %.1f%%（%d/%d） | ≥ %.0f%% | %s |" % (
            d["value"], d["n"], d["total"], d["target"], "达标" if d["ok"] else "未达标"))
        if m["value"] is not None:
            L.append("| 典型任务耗时（中位数） | %.1fs | ≤ %.0fs | %s |" % (
                m["value"], m["target"], "达标" if m["ok"] else "未达标"))
            L.append("")
            over = m.get("over") or []
            if over:
                L.append("⚠️ **口径提醒**：本项按**中位数**判定。「典型」若被理解为"
                         "「所有任务都不超过 %.0fs」，则不达标——共 %d/%d 条超标：%s。\n"
                         % (m["target"], len(over), d["n"],
                            "、".join("`%s` %.1fs" % (o["id"], o["exec_s"]) for o in over)))
            else:
                L.append("本次所有任务单次耗时均未超过 %.0fs。\n" % m["target"])
        else:
            L.append("| 典型任务耗时（中位数） | 无成功样本 | ≤ %.0fs | 未达标 |" % m["target"])
    L.append("")

    L.append("## 口径与方法\n")
    L.append("- **中位数定义**：`statistics.median`，即偶数个样本取中间两个的均值。")
    L.append("  （早期版本写成 `lat[len(lat)//2]`，对偶数样本取的是**上中位数**，")
    L.append("  会把结果报得更差、且不是标准口径，已修正并加了自检断言。）")
    L.append("- **耗时口径**：`finished_at - started_at`，即真实执行时长；")
    L.append("  不含排队等待（`created_at → started_at`，受并发配额影响）。")
    L.append("- **一次执行成功率**：按 `attempts == 1` 判定（首次执行即成功、未触发自动调试）。")
    L.append("  若把「一次执行」理解为「一次提交交互内完成」，则该数字等于调试后完成率。")
    L.append("- **单轮即样本**：意图识别已做多轮重复以量化波动；端到端每条要烧 30-100s 真实 GEE 计算")
    L.append("  且受配额限制，因此**单轮 8 条只是样本、不是稳定值**。")
    L.append("  引用这些数字时应说明轮次，或跑两轮取区间，不要当成固定值。\n")

    if intent_res and "rows" in intent_res:
        L.append("## 指标一：意图识别（逐条）\n")
        L.append("| 用例 | 输入 | 结果 |")
        L.append("| --- | --- | --- |")
        for r in intent_res["rows"]:
            L.append("| %s | %s | %s |" % (r["id"], r["text"], "PASS" if r["ok"] else "FAIL"))
        L.append("")
        bad = [r for r in intent_res["rows"] if not r["ok"]]
        if bad:
            L.append("### 未通过用例明细\n")
            for r in bad:
                L.append("- **%s**：%s" % (r["id"], r["text"]))
                for name, ok, detail in r["checks"]:
                    if not ok:
                        L.append("  - `%s` → %s" % (name, detail))
                if r.get("why"):
                    L.append("  - 设计意图：%s" % r["why"])
            L.append("")

    if e2e_res and "rows" in e2e_res:
        L.append("## 指标二/三/四：端到端执行明细\n")
        L.append("| 用例 | 任务ID | 状态 | 尝试次数 | 执行耗时 | 排队耗时 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for r in e2e_res["rows"]:
            if r.get("status") == "submit_failed":
                L.append("| %s | - | 提交失败 | - | - | - |" % r["id"])
                continue
            L.append("| %s | `%s` | %s | %s | %s | %s |" % (
                r["id"], r.get("task_id", "-"), r.get("status", "-"),
                r.get("attempts", "-"),
                ("%.1fs" % r["exec_s"]) if r.get("exec_s") else "-",
                ("%.1fs" % r["queue_s"]) if r.get("queue_s") else "-"))
        L.append("")
        L.append("耗时口径：`finished_at - started_at`（真实执行），不含排队等待"
                 "（`created_at → started_at`，受并发配额影响）。\n")

    path.write_text("\n".join(L), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="四个量化指标的评测 runner")
    ap.add_argument("--self-test", action="store_true", help="只自检日期判定规则")
    ap.add_argument("--intent-only", action="store_true", help="只跑指标一（不依赖 GEE）")
    ap.add_argument("--intent-repeat", type=int, default=3,
                    help="指标一重复轮数（默认 3）。该指标有 run-to-run 波动，"
                         "单轮不足以判断是否稳定达标")
    ap.add_argument("--limit", type=int, default=None, help="端到端只跑前 N 条")
    ap.add_argument("--poll-timeout", type=float, default=600.0, help="单条任务等待上限（秒）")
    ap.add_argument("--max-quota-wait", type=float, default=1800.0,
                    help="单条任务被配额拒绝时最多等待多久（秒）。默认 1800：一小时限额 20 条，"
                         "连着跑第二遍评测必然撞配额，需要等窗口滑动让出名额")
    ap.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    intent_res = run_intent(args.intent_repeat)
    e2e_res = None if args.intent_only else run_e2e(
            args.limit, args.poll_timeout, args.max_quota_wait)

    print("\n" + "=" * 72)
    print("总结")
    print("=" * 72)
    all_ok = bool(intent_res.get("ok"))
    print("  指标一 意图识别准确率 : %.1f%%  %s" % (
        intent_res["value"], "达标" if intent_res["ok"] else "未达标"))
    if e2e_res and "first_run_success" in e2e_res:
        for key, label in (("first_run_success", "指标二 一次执行成功率"),
                           ("post_debug_completion", "指标三 调试后完成率"),
                           ("median_latency", "指标四 典型任务耗时  ")):
            m = e2e_res[key]
            all_ok = all_ok and m["ok"]
            val = ("%.1f%%" % m["value"]) if key != "median_latency" else (
                ("%.1fs" % m["value"]) if m["value"] is not None else "N/A")
            print("  %s : %-8s %s" % (label, val, "达标" if m["ok"] else "未达标"))
    elif e2e_res and e2e_res.get("error"):
        all_ok = False
        print("  指标二/三/四：未执行（%s）" % e2e_res["error"])

    if not args.no_report:
        try:
            p = write_report(intent_res, e2e_res)
            print("\n  报告已写入：%s" % p)
        except Exception as e:  # noqa: BLE001
            print("\n  报告写入失败：%s" % e)

    print("\n  全部达标：%s" % ("是" if all_ok else "否"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
