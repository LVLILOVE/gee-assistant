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
    "ndvi": ["植被", "ndvi", "绿度", "长势", "绿化", "植被指数", "作物", "叶面积", "森林覆盖"],
    "water": ["水体", "水域", "湖泊", "河流", "水库", "水面", "湿地", "水"],
    "classification": ["分类", "地类", "土地利用", "地表", "土地覆盖", "用地", "覆盖", "林覆盖"],
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
    res["region"] = _extract_region(text)
    return res


#: 动词 / 客套话前缀。模型在 `_build_parse_prompt` 里被明确要求剥掉，
#: 规则兜底同样必须剥 —— 否则 "看看鄱阳湖" 会被当成完整地名，
#: 而 `get_center("看看鄱阳湖")` 虽然靠子串匹配侥幸能命中，
#: 落库的 region 字段却是脏的（前端区域选择、历史回填都会拿它去匹配）。
_VERB_PREFIXES = (
    "帮我看看", "帮我分析一下", "帮我分析", "帮我看下", "帮我查一下", "帮我查",
    "请帮我看看", "请帮我分析", "请分析一下", "请分析", "请看看", "看一下", "看下",
    "看看", "查一下", "查查", "分析一下", "分析下", "分析", "评估一下", "评估",
    "监测一下", "监测", "检测一下", "检测", "对比一下", "对比", "比较一下", "比较",
    "统计一下", "统计", "计算一下", "计算", "研究一下", "研究", "我想知道", "我想看",
    "想知道", "看一下", "给我看看", "给我看", "了解一下", "关注一下",
)

#: 常见地名后缀。**先按它做一轮贪心抽取，再退回区域库匹配。**
#: 用 `finditer` 而不是 `search`：一句话里可能出现多个候选地名
#: （"从太湖流域到鄱阳湖"），取最长的那个更可能是用户真正想分析的目标。
_REGION_SUFFIX = (
    "自然保护区|自治区|特别行政区|国家公园|国家森林公园|开发区|风景区|名胜区|"
    "旅游度假区|大峡谷|三角洲|丘陵|山脉|山地|高原|盆地|平原|草原|沙漠|沙地|"
    "流域|湖区|水库|湿地|林场|林区|梯田|古镇|古城|遗址|公园|景区|"
    "省|市|县|区|盟|州|旗|"
    "湖|河|江|海|岛|山|峰|岭|峡|湾|泉|池|关|口|原"
)


#: 日常词汇 —— 出现这些词就基本可以断定**不是地名**。
#:
#: 为什么需要它：③ 短句兜底要决定"这句话到底是不是一个地名"。
#: 前三道门槛（无任务词 / 无动词 / 无语气词）挡不住「今天天气不错」——
#: 它三个都不沾，长度也合法。但把它当地名落库，下游 `get_center` 一定查不到，
#: 于是又变成一次"定位不了"。
#:
#: 这里的取舍是**宁可漏也不要错**：漏了 → 返回 None → 上层问一句"分析哪个区域"，
#: 用户补充一句就好了；错了 → 拿假地名跑一次任务，白烧 token 和 GEE 额度，
#: 还给出一份没有区域针对性的结果。两者代价不对等。
_NON_PLACE_WORDS = (
    "天气", "不错", "东西", "什么", "怎么", "为什么", "可以", "可以吗", "帮我",
    "请问", "谢谢", "你好", "问题", "事情", "时候", "地方", "知道", "明白",
    "意思", "功能", "用法", "推荐", "建议", "介绍", "说明", "讲讲", "聊聊",
)


def _strip_verbs(s: str) -> str:
    """剥掉地名前面的动词/客套话（"看看鄱阳湖" → "鄱阳湖"）。"""
    out = s.strip()
    changed = True
    # 反复剥，处理"帮我看看…"这种叠加前缀
    while changed:
        changed = False
        for v in _VERB_PREFIXES:
            if out.startswith(v) and len(out) > len(v):
                out = out[len(v):]
                changed = True
                break
    # 结尾的疑问/语气词也去掉（"鄱阳湖怎么样了" → "鄱阳湖"）
    out = re.sub(r"(怎么样|怎样|如何|是什么|在哪儿|在哪|了吗|呢|吗|的|了)+$", "", out.strip())
    return out.strip()


def _extract_region(text: str) -> str | None:
    """从自然语言里抽出地名。三级策略，从最可信到最宽松。

    2026-09-24 重写。原实现只做「后缀正则 search」，实测三类失败：
      · 「恩施大峡谷的植被情况」→ 无后缀命中 → region=None
        → 前端反问"请问分析哪个区域？"（用户明明已经说了）
      · 「看看鄱阳湖」→ 抽成 "看看鄱阳湖"，动词粘在地名里
      · 「今天天气不错」→ 整句被当地名

    现在的顺序（**顺序不能换**，见每步的理由）：

      ① **后缀贪心**：抽 "XX市/XX湖/XX流域/XX大峡谷" 这类完整地名。
         放在最前面是因为它**最贴近用户原话** —— 用户写"六盘水市"就返回
         "六盘水市"，不要退成"六盘水"。而且必须校验候选**真的能查库**
         （坐标簿有"六盘水"没有"六盘水市"，所以 ①返回"六盘水市"后要能
         回落到"六盘水"——见 `_normalize_candidate`）。
      ② **区域库直查**：库里/坐标簿里的名称直接出现在原文就取它（长名优先）。
         覆盖 ① 抽不出来的写法（如无后缀的"恩施"）。
      ③ 都没有 → 短句兜底 or None（**不猜**）。
    """
    if not text:
        return None
    cleaned = _strip_verbs(text)

    # 加载可匹配名称（区域库正式名 + 别名 + 坐标簿）
    names: list[str] = []
    try:
        from .gazetteer import GAZETTEER_KEYS
        from .regions import REGION_LIBRARY
        for r in REGION_LIBRARY:
            names.append(r["name"])
            names.extend(r.get("aliases", []))
        names.extend(GAZETTEER_KEYS)
    except Exception:  # noqa: BLE001
        pass
    names = [n for n in set(names) if len(n) >= 2]
    # 长度降序 —— 长名优先，避免短名抢走长名
    names.sort(key=len, reverse=True)

    # ① 区域库直查 —— **放在后缀贪心之前**。
    #
    # 为什么顺序是这样（实测教训）：后缀贪心和区域库直查都用"最长优先"，
    # 但两者的"长"含义不同，混在一起会互相压制：
    #   · `恩施大峡谷的植被情况` —— 库里有「恩施大峡谷」(5 字) 和「恩施」(2 字)。
    #     直查能取到「恩施大峡谷」；若先跑后缀贪心，正则会先咬到「...的植被」
    #     这类尾巴，或者只取到「恩施」再靠去尾查库蒙对，**结果不稳定**。
    #   · 库里的名字都带坐标、是人工核过的，比正则抽出来的更可信。
    # 所以：**先信库，再用正则兜库外的**。
    for n in names:
        if n in cleaned:
            return n

    # ② 后缀贪心（库外地名，如"某某新城"）
    cands = [
        m.group(0) for m in re.finditer(
            r"[\u4e00-\u9fa5]{1,9}(?:" + _REGION_SUFFIX + ")", cleaned
        )
    ]
    cands = [c for c in cands if len(c) >= 2]
    if cands:
        best = max(cands, key=len)
        if best in names:
            return best
        # 去尾再查库（"六盘水市" → "六盘水"），返回**库里的规范名**
        for cut in range(1, min(3, len(best))):
            trimmed = best[:-cut]
            if len(trimmed) >= 2 and trimmed in names:
                return trimmed
        # 库外的新地名：要求它**真的像地名**才接受。
        # 实测「我想看点东西」会被 ①的窗口咬出「点东西」——
        # 这类结果的共性是结尾落在"东西/什么/地方"这类空泛词上，
        # 或者整串里含动词。
        if not re.search(r"(东西|什么|地方|事情|问题|情况|时候|一下)$", best) \
                and not any(v in best for v in _VERB_PREFIXES):
            return best

    # ③ 短句兜底：整句短、**不含任务词/动词/疑问词**时，才把整句当地名。
    # ⚠ 这个兜底很容易误伤：实测「今天天气不错」「我想看点东西」都曾被误判成地名
    # → 前端拿它去查库查不到 → 又是一次"定位不了"。
    #
    # 加四道门槛（缺一不可）：
    #   a. 不含任何任务关键词（"植被""水体"…）—— 否则抽的是任务不是地名
    #   b. 不含任何动词/客套话（"看看""分析""想看"…）—— 否则抽的是整句话
    #   c. 不以疑问/语气词/标点结尾
    #   d. **不含日常词汇**（"天气""不错""东西"…）—— 见 `_NON_PLACE_WORDS`
    #
    # 门槛 d 是补上来的：前三道过不了「今天天气不错」（它没任务词、没动词、
    # 不以语气词结尾，长度也合规）。但"没有地名线索"和"这就是个地名"是两回事 ——
    # 与其猜，不如**返回 None 让上层去追问**，这正是本模块的一贯原则。
    if 2 <= len(cleaned) <= 10 \
            and not any(k in cleaned for kws in TASK_KEYWORDS.values() for k in kws) \
            and not any(v in cleaned for v in _VERB_PREFIXES) \
            and not any(w in cleaned for w in _NON_PLACE_WORDS) \
            and not re.search(r"[？?。，,、！!的了呢吗啊呀嘛]$", cleaned):
        return cleaned
    return None


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
