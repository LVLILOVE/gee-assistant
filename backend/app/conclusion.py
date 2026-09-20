"""结论摘要：把统计量翻译成业务语言的一段话。

**为什么需要这一层**：当前输出是「图层 + 统计图表 + 可查看的代码」。对非专业用户
来说，图看完了还是不知道"所以我该做什么" —— 这是"零代码"产品最容易在最后一公里
失败的地方。数据其实已经在手（`WB.stat` 上报的统计量），只差一次把数字翻译成结论。

设计取舍：
  · **不新增计算**：只用已有的 stats + 图表数据，不做任何重算。
  · **必须有规则兜底**：LLM 不可用时也要给出结论，否则离线演示路径就没有这一跳。
  · **失败静默**：摘要生成失败绝不能影响任务结果 —— 它是锦上添花，不是主链路。
  · **口径与数据一致**：只允许引用给定的数字，禁止模型自行推算或补充未提供的数据。
"""

from __future__ import annotations

import json

from .config import settings
from .models import AnalysisRequest, TASK_LABELS_BY_VALUE

SYSTEM_PROMPT = (
    "你是卫星遥感分析结果解读专家。把给定的统计结果翻译成一段面向业务人员的结论，"
    "只输出结论本身，不要任何解释、不要 markdown 围栏、不要分点符号。"
)


def _fmt_stats(stats: dict) -> str:
    """把统计量渲染成紧凑的文本，供提示词与兜底共同使用。"""
    if not stats:
        return ""
    return "；".join(f"{k}={v}" for k, v in stats.items())


def _chart_digest(charts: list) -> str:
    """把图表数据整理成"能支撑结论"的摘要，而不是把整个系列塞进去。

    只给：标题 + 极值 + 首尾值。图表可能有 12 个月甚至更多点，
    整段塞进去会稀释模型注意力，且容易诱导它编造中间的细节。
    """
    lines = []
    for c in charts or []:
        title = c.get("title") or "统计图表"
        labels = c.get("labels") or []
        series = c.get("series") or []
        if not series:
            continue
        data = series[0].get("data") or []
        name = series[0].get("name") or "数值"
        if not data:
            continue
        try:
            nums = [float(x) for x in data if x is not None]
        except (TypeError, ValueError):
            nums = []
        if not nums:
            continue
        parts = [f"{title}：{name} 共 {len(data)} 项"]
        if len(data) == len(labels) and labels:
            mx_i = nums.index(max(nums))
            mn_i = nums.index(min(nums))
            # 标签下标要以原始 data 为准（nums 过滤过 None，索引会错位）
            def _label(idx: int) -> str:
                return str(labels[idx]) if idx < len(labels) else f"第{idx + 1}项"
            parts.append(f"峰值 {max(nums)}（{_label(mx_i)}）")
            parts.append(f"最低 {min(nums)}（{_label(mn_i)}）")
        else:
            parts.append(f"峰值 {max(nums)}、最低 {min(nums)}")
        parts.append(f"均值 {round(sum(nums) / len(nums), 2)}")
        lines.append("；".join(parts))
    return "\n".join(lines)


def _rule_conclusion(req: AnalysisRequest, stats: dict, charts: list) -> str:
    """规则兜底：LLM 不可用时也要给出结论。"""
    label = TASK_LABELS_BY_VALUE.get(req.task_type.value, req.task_type.value)
    head = f"{req.region} {req.start_date} ~ {req.end_date} 的{label}分析已完成。"

    facts = []
    if stats:
        facts.append("关键统计：" + _fmt_stats(stats))
    digest = _chart_digest(charts)
    if digest:
        facts.append("图表要点：" + digest.replace("\n", " ｜ "))

    if not facts:
        return head + "本次分析未上报统计量，可展开图表与代码查看细节。"

    # 针对四类任务各补一句口径相关的解读，避免结论只是复述数字
    tips = {
        "ndvi": "（NDVI 已按植被口径掩膜水体与云，0.4 以上通常表示植被覆盖较好）",
        "water": "（面积按真实像元面积加权统计，非像元计数）",
        "classification": "（基于现成分类产品，各类占比按面积加权）",
        "change_detection": "（两个时段口径一致，差值阈值 ±0.1）",
    }
    tip = tips.get(req.task_type.value, "")
    return head + "".join(facts) + (tip or "")


def generate_conclusion(
    req: AnalysisRequest,
    stats: dict | None = None,
    charts: list | None = None,
    stdout: str = "",
) -> str | None:
    """返回业务语言的结论摘要；无法生成时返回规则兜底（不会返回 None 除非数据全空）。"""
    stats = stats or {}
    charts = charts or []

    if not stats and not charts and not (stdout or "").strip():
        return None

    # LLM 可用就走模型（更自然的业务语言），不可用或失败退回规则。
    if settings.deepseek_api_key:
        try:
            return _llm_conclusion(req, stats, charts)
        except Exception:  # noqa: BLE001
            pass
    return _rule_conclusion(req, stats, charts)


def _llm_conclusion(req: AnalysisRequest, stats: dict, charts: list) -> str:
    import httpx

    label = TASK_LABELS_BY_VALUE.get(req.task_type.value, req.task_type.value)
    stat_text = _fmt_stats(stats) or "（无）"
    digest = _chart_digest(charts) or "（无）"

    user = (
        f"分析任务：{label}\n"
        f"分析区域：{req.region}\n"
        f"时间范围：{req.start_date} ~ {req.end_date}\n"
        f"云量阈值：{req.cloud_threshold}%\n\n"
        f"关键统计量：{stat_text}\n\n"
        f"图表要点：\n{digest}\n\n"
        "请写一段 2-3 句的中文结论摘要，要求：\n"
        "1. 用业务语言说明这次分析说明了什么（例如植被长势、水体规模、地类结构、变化趋势）；\n"
        "2. **只能引用上面给出的数字**，不要自行推算、不要编造未提供的数据；\n"
        "3. 若数据不足以支撑某个判断（如样本期数很少），明确说明局限；\n"
        "4. 不要用套话开头（如「综上所述」「根据分析」），直接给结论。\n"
        "只输出结论文本。\n"
    )

    r = httpx.post(
        settings.deepseek_base_url + "/chat/completions",
        json={
            "model": settings.deepseek_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "stream": False,
        },
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        timeout=60,
    )
    r.raise_for_status()
    text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    return text or _rule_conclusion(req, stats, charts)
