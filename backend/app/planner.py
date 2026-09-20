"""规划推理（借鉴 Earth Agent 的规划/推理能力）。

把一句话包含多个独立分析目标的需求，拆解为多个子任务。
有 DeepSeek key 走大模型拆解，无 key 走规则兜底（识别多个任务关键词）。
"""

from __future__ import annotations

import json

from .config import settings
from .daterange import month_range_rule

SYSTEM_PROMPT = (
    "你是卫星遥感分析任务规划器。判断用户的一句话需求是单一分析目标还是多个独立目标，"
    "若含多个独立目标，拆解为多个子任务。只输出 JSON 本身，不要任何解释、不要 markdown 围栏。"
)

TASK_TYPE_ENUM = ["ndvi", "water", "classification", "change_detection"]


def _build_prompt(text: str) -> str:
    return (
        "请分析下面这句用户需求，输出 JSON：\n"
        '{"multi": boolean, "tasks": [{"task_type": string, "region": string|null, '
        '"start_date": string|null, "end_date": string|null, "cloud_threshold": number|null}]}\n\n'
        "规则：\n"
        "- 如果需求只含一个分析目标，multi=false，tasks 为空数组 []。\n"
        "- 如果含多个独立目标（例如同时要「水体提取」和「植被分析」），multi=true，"
        "tasks 里每个目标一个子任务。\n"
        "- task_type 只能是：ndvi(植被指数)、water(水体提取)、classification(地表分类)、"
        "change_detection(时序变化检测)。\n"
        "- region 是分析区域；start_date/end_date 为 YYYY-MM-DD 或 null"
        f"（{month_range_rule()}）。\n"
        "- cloud_threshold 用户没提就 null。\n"
        "只输出 JSON。\n\n"
        f"用户需求：{text}\n"
    )


def _extract_json(raw: str) -> dict:
    raw = (raw or "").strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    import re

    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _rule_plan(text: str) -> dict:
    """无 key 时的规则兜底：识别多个任务关键词 → 拆解。"""
    t = text.lower()
    kw_map = {
        "ndvi": ["植被", "ndvi", "绿度", "长势"],
        "water": ["水体", "水域", "湖泊", "河流", "水库", "水面"],
        "classification": ["分类", "地类", "土地利用", "地表覆盖"],
        "change_detection": ["变化", "变迁", "演变", "对比"],
    }
    found = [tt for tt, kws in kw_map.items() if any(k in t for k in kws)]
    if len(found) < 2:
        return {"multi": False, "tasks": [], "source": "rule"}
    # 提取区域（简单规则）
    import re as _re

    rm = _re.search(r"([\u4e00-\u9fa5]{2,8}(?:流域|湖|市|省|区|县|三角洲|盆地|平原))", text)
    region = rm.group(1) if rm else None
    tasks = [
        {
            "task_type": tt,
            "region": region,
            "start_date": None,
            "end_date": None,
            "cloud_threshold": None,
        }
        for tt in found
    ]
    return {"multi": True, "tasks": tasks, "source": "rule"}


def _llm_plan(text: str) -> dict:
    import httpx

    r = httpx.post(
        settings.deepseek_base_url + "/chat/completions",
        json={
            "model": settings.deepseek_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(text)},
            ],
            "temperature": 0.0,
            "stream": False,
            "response_format": {"type": "json_object"},
        },
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        timeout=60,
    )
    r.raise_for_status()
    return _extract_json(r.json()["choices"][0]["message"]["content"].strip())


def plan(text: str) -> dict:
    """拆解复杂需求，返回 {multi, tasks, source}。"""
    text = (text or "").strip()
    if not text:
        return {"multi": False, "tasks": [], "source": "empty"}

    result: dict = {}
    source = "rule"
    if settings.deepseek_api_key:
        try:
            result = _llm_plan(text)
            source = "deepseek"
        except Exception as e:  # noqa: BLE001
            result = _rule_plan(text)
            source = f"rule({type(e).__name__})"
    else:
        result = _rule_plan(text)

    tasks = result.get("tasks") or []
    # 校验任务类型合法性
    valid_tasks = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tt = t.get("task_type")
        if tt in TASK_TYPE_ENUM:
            valid_tasks.append(
                {
                    "task_type": tt,
                    "region": t.get("region") or "",
                    "start_date": t.get("start_date"),
                    "end_date": t.get("end_date"),
                    "cloud_threshold": t.get("cloud_threshold"),
                }
            )
    multi = bool(result.get("multi")) and len(valid_tasks) >= 2
    if not multi:
        valid_tasks = []
    return {"multi": multi, "tasks": valid_tasks, "source": source}
