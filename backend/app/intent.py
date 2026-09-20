"""自然语言意图解析：把中文需求解析为结构化分析参数，缺参时返回澄清问题。

支持多轮澄清：前端把用户的新输入 + 已确认参数一起提交，本模块合并后返回
下一步（追问 / 就绪）。有 DeepSeek key 走大模型解析，无 key 走规则兜底。
"""

from __future__ import annotations

import json
import re

from .config import settings
from .daterange import default_end, default_start, month_range_rule

TASK_TYPE_ENUM = ["ndvi", "water", "classification", "change_detection"]

REQUIRED_FIELDS = ["task_type", "region"]

TASK_KEYWORDS = {
    "ndvi": ["植被", "ndvi", "绿度", "长势", "绿化", "植被指数", "作物", "叶面积"],
    "water": ["水体", "水域", "湖泊", "河流", "水库", "水面", "湿地", "水"],
    "classification": ["分类", "地类", "土地利用", "地表", "土地覆盖", "用地", "覆盖"],
    "change_detection": ["变化", "对比", "变迁", "演变", "动态", "变化检测", "监测"],
}

TASK_LABEL_CN = {
    "ndvi": "植被指数（NDVI）",
    "water": "水体提取",
    "classification": "地表分类",
    "change_detection": "时序变化检测",
}

SYSTEM_PROMPT = (
    "你是卫星遥感分析意图解析器。把用户的中文需求解析为 JSON，只输出 JSON 本身，"
    "不要任何解释文字、不要 markdown 代码块围栏。"
)


def _build_parse_prompt(text: str, partial: dict) -> str:
    # 年份必须动态注入：原先这里写死"补全为 2025 年的完整日期"，到了 2026 年就
    # 成了错误基准（实测「6-8 月」被补成 2025-06-01）。措辞见 daterange 模块。
    return (
        "请解析下面的用户需求，输出如下结构的 JSON：\n"
        '{"task_type": string|null, "region": string|null, "start_date": string|null, '
        '"end_date": string|null, "cloud_threshold": number|null}\n\n'
        "字段规则：\n"
        "- task_type 只能是以下之一或 null：ndvi(植被指数)、water(水体提取)、"
        "classification(地表分类)、change_detection(时序变化检测)。\n"
        "- region 只填**地名本身**（如 太湖流域、洞庭湖、北京市），或 null。"
        "不要带动词、时间词、疑问词。实测「看看鄱阳湖现在的水域范围」曾被解析成 "
        'region="看看鄱阳湖"，地名里混进了动词"看看"；「太湖的水面面积最近有啥变化」'
        "则整个 region 丢空。这类句子请先剥掉动词与时间词，只留地名。\n"
        f"- start_date/end_date 为 YYYY-MM-DD 或 null。{month_range_rule()}。\n"
        "- cloud_threshold 是云量阈值(0-100 数字)，用户没提就 null。\n"
        "只填能从用户话里确定的值，不确定一律填 null。\n\n"
        f"已确认的参数（保持不变，除非用户新话里明确修改）：{json.dumps(partial, ensure_ascii=False)}\n\n"
        f"用户需求：{text}\n"
    )


def _extract_json(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                return data if isinstance(data, dict) else {}
            except Exception:
                pass
    return {}


def _rule_parse(text: str) -> dict:
    """无 key 时的规则兜底。"""
    res = {
        "task_type": None,
        "region": None,
        "start_date": None,
        "end_date": None,
        "cloud_threshold": None,
    }
    t = text.lower()
    for tt, kws in TASK_KEYWORDS.items():
        if any(k in t for k in kws):
            res["task_type"] = tt
            break
    # 日期：2025年6月1日 / 2025-06-01 等
    dates = re.findall(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", text)
    if dates:
        y, m, d = dates[0]
        res["start_date"] = f"{y}-{int(m):02d}-{int(d):02d}"
    # 云量
    cm = re.search(r"云量[^\d]{0,4}(\d{1,3})", text)
    if cm:
        res["cloud_threshold"] = float(cm.group(1))
    # 区域：常见后缀
    rm = re.search(r"([\u4e00-\u9fa5]{2,8}(?:流域|湖|市|省|区|县|三角洲|盆地|平原))", text)
    if rm:
        res["region"] = rm.group(1)
    return res


def _chat_json(messages: list[dict]) -> str:
    import httpx

    url = settings.deepseek_base_url + "/chat/completions"
    r = httpx.post(
        url,
        json={
            "model": settings.deepseek_model,
            "messages": messages,
            "temperature": 0.0,
            "stream": False,
            "response_format": {"type": "json_object"},
        },
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _coerce_cloud(value) -> float:
    try:
        v = float(value)
        if 0 <= v <= 100:
            return v
    except (TypeError, ValueError):
        pass
    return 20.0


def _describe(fields: dict) -> str:
    tt = fields.get("task_type")
    label = TASK_LABEL_CN.get(tt, tt or "未指定")
    parts = [
        f"任务类型：{label}",
        f"分析区域：{fields.get('region') or '未指定'}",
        f"时间范围：{fields.get('start_date')} ~ {fields.get('end_date')}",
        f"云量阈值：{fields.get('cloud_threshold')}%",
    ]
    return "，".join(parts)


def _summarize(merged: dict, source: str) -> dict:
    missing = [k for k in REQUIRED_FIELDS if not merged.get(k)]
    complete = len(missing) == 0
    question = None
    if not complete:
        if "task_type" in missing:
            question = "请问你想做哪类分析？可选：植被指数(NDVI)、水体提取、地表分类、时序变化检测。"
        elif "region" in missing:
            question = "请问分析哪个区域？例如：太湖流域、洞庭湖、北京市。"

    fields = {
        "task_type": merged.get("task_type") if merged.get("task_type") in TASK_TYPE_ENUM else None,
        "region": merged.get("region") or "",
        # 兜底默认值必须按当天推算。原先写死 "2024-01-01"/"2024-12-31"，导致
        # 用户只说"现在"（模型正确返回 null）时被静默钉死在 2024 年。
        "start_date": merged.get("start_date") or default_start(),
        "end_date": merged.get("end_date") or default_end(),
        "cloud_threshold": _coerce_cloud(merged.get("cloud_threshold")),
    }
    return {
        "source": source,
        "fields": fields,
        "missing": missing,
        "question": question,
        "complete": complete,
        "summary": _describe(fields),
    }


def parse(text: str, partial: dict | None = None) -> dict:
    """解析自然语言，返回 {source, fields, missing, question, complete, summary}。"""
    merged = dict(partial or {})

    if not (text or "").strip():
        return _summarize(merged, source="empty")

    source = "rule"
    parsed: dict = {}
    if settings.deepseek_api_key:
        try:
            raw = _chat_json(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_parse_prompt(text, merged)},
                ]
            )
            parsed = _extract_json(raw)
            source = "deepseek"
        except Exception as e:  # noqa: BLE001
            parsed = _rule_parse(text)
            source = f"rule({type(e).__name__})"
    else:
        parsed = _rule_parse(text)

    # 只覆盖非空的新值，已确认参数保持不变
    for k, v in parsed.items():
        if v not in (None, ""):
            merged[k] = v

    return _summarize(merged, source=source)
