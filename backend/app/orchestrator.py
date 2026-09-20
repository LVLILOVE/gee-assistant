"""LangGraph 编排：意图解析 → 代码生成 → 沙箱执行 → 自动调试（生成—执行—调试循环）。"""

from langgraph.graph import END, START, StateGraph

from . import codegen, conclusion, debugger
from .config import settings
from .execution import get_backend
from .models import AnalysisRequest


def _node_generate(state: dict) -> dict:
    code, src = codegen.generate_code(state["request"], state.get("user_id"))
    state["code"] = code
    state["code_source"] = src
    state["logs"].append(f"[生成] 代码来源={src}")
    return state


def _node_execute(state: dict) -> dict:
    backend = get_backend(settings.execution_backend)
    req = state["request"]
    out = backend.execute(state["code"], req)
    state["attempts"] += 1
    state["result"] = out
    if out.ok:
        state["status"] = "succeeded"
        state["logs"].append(f"[执行] 第 {state['attempts']} 次执行成功")
    else:
        cat = out.error.get("category") or debugger.classify_error(out.error.get("message", ""))
        out.error["category"] = cat
        state["logs"].append(f"[执行] 第 {state['attempts']} 次失败 [{cat}]：{out.error.get('message', '')[:80]}")
    return state


def _should_retry(state: dict) -> str:
    result = state["result"]
    if result is None or result.ok:
        return "end"
    if state["attempts"] >= state["max_retries"]:
        return "end"
    cat = result.error.get("category")
    if cat == "auth":
        return "end"  # 鉴权错误重试无意义
    if cat == "timeout" and state["attempts"] >= 2:
        # 每轮超时都要实打实烧掉 GEE_TIMEOUT 秒（默认 420s），再修下去
        # 用户要干等十几分钟。给一次修复机会，之后直接收敛并给出建议。
        return "end"
    return "repair"


def _node_repair(state: dict) -> dict:
    err = state["result"].error
    state["logs"].append(f"[调试] 错误分类={err.get('category')}，请求大模型修复代码")
    old = state["code"]
    # 上一轮如果"修了等于没修"，这一轮必须把这件事明确告诉模型。
    # 只说"请修复"，模型很可能再返回一份一模一样的代码，再白烧一轮。
    note = ""
    if state.get("no_change_count"):
        note = (
            "⚠ 重要：你上一轮返回的代码与再上一轮**完全相同**，等于没有修复，"
            "所以这一次的报错和上一次逐字一致。这一轮必须**真正产生改动**："
            "把报错涉及的每个方法逐个核对是否真实存在于对应对象上；"
            "不确定就换成等价的合法写法。例如求众数：ee.Image 没有 .mode()，"
            "应写 image.reduce(ee.Reducer.mode())，或对 ImageCollection 调用 .mode()。"
        )
    new = codegen.fix_code(old, err, note=note)
    if new.strip() == old.strip():
        # 修复**没有发生**（而不是"修了但没好"）。接着跑只会得到逐字相同的错误。
        # 实测：分类任务曾连报 3 次同样的 AttributeError，正是这种情形。
        # 记一条明确日志，把静默浪费变成可见事件。
        state["no_change_count"] = state.get("no_change_count", 0) + 1
        state["logs"].append(
            "[调试] ⚠ 修复后代码与上一次完全相同（本轮未产生改动），"
            "下次修复会附带强提醒"
        )
    state["code"] = new
    return state


def _node_finalize(state: dict) -> dict:
    result = state["result"]
    # 结论摘要：把统计量翻译成业务语言（"图看完了所以该做什么"那一跳）。
    # 只在成功时生成 —— 失败任务的结论应该是"哪一步错了、怎么处置"，那由 logs 承担。
    # 整段包在 try 里：摘要是锦上添花，生成失败绝不能影响任务结果本身。
    if result is not None and result.ok:
        try:
            result.conclusion = conclusion.generate_conclusion(
                state["request"],
                stats=getattr(result, "stats", {}) or {},
                charts=[c.model_dump() for c in (result.charts or [])],
                stdout=result.stdout or "",
            )
            if result.conclusion:
                state["logs"].append("[结论] 已生成结果摘要")
        except Exception as e:  # noqa: BLE001
            state["logs"].append(f"[结论] 摘要生成失败（不影响结果）：{type(e).__name__}")

    if result is not None and not result.ok:
        state["status"] = "failed"
        cat = (result.error or {}).get("category")
        if cat == "auth":
            state["logs"].append(
                "[结束] 凭据/鉴权问题，已跳过自动修复（改代码无法解决）。"
                "请运行 backend/tools/gee_doctor.py 自检，或临时切回 EXECUTION_BACKEND=offline"
            )
        elif cat == "timeout":
            state["logs"].append(
                f"[结束] 连续超时（单次上限 {settings.gee_timeout}s，已尝试自动降低计算量）。"
                "建议：缩小分析区域 / 缩短时间范围 / 调大 .env 里的 GEE_TIMEOUT 后重试"
            )
        elif cat == "network":
            state["logs"].append(
                "[结束] 网络/代理不稳定导致执行失败（已自动重试仍失败）。"
                "建议在代理工具里切换更稳定的节点，或临时切回 offline"
            )
        elif cat == "sandbox":
            state["logs"].append("[结束] 被沙箱安全策略拦截，请检查生成代码是否存在越界操作")
        else:
            state["logs"].append(f"[结束] 已达重试上限（{state['attempts']} 次），建议人工介入修改")
    return state


def _build_graph():
    g = StateGraph(dict)
    g.add_node("generate", _node_generate)
    g.add_node("execute", _node_execute)
    g.add_node("repair", _node_repair)
    g.add_node("finalize", _node_finalize)
    g.add_edge(START, "generate")
    g.add_edge("generate", "execute")
    g.add_conditional_edges("execute", _should_retry, {"repair": "repair", "end": "finalize"})
    g.add_edge("repair", "execute")
    g.add_edge("finalize", END)
    return g.compile()


_graph = _build_graph()


def run_analysis(request: AnalysisRequest, user_id: int | None = None) -> dict:
    state = {
        "request": request,
        # 任务在后台线程里跑，拿不到 request 上下文，所以用户身份必须显式带进来 ——
        # 代码生成时要按这个 id 取分析偏好，否则会串用别人的自定义指令。
        "user_id": user_id,
        "code": "",
        "code_source": "",
        "attempts": 0,
        "max_retries": settings.max_retries,
        "result": None,
        "status": "running",
        "logs": [],
    }
    return _graph.invoke(state)
