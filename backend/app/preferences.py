"""分析偏好（Custom Instructions）：借鉴 Earth Agent 的 Custom Instructions 设计。

用户可配置自定义分析偏好（自由文本指令 + 数据源偏好 + 输出语言等），
这些偏好会注入到代码生成 prompt，让生成的 GEE 代码符合用户习惯。
持久化在 sqlite 的 user_settings 表，key = "analysis_preferences"。
"""

from __future__ import annotations

from .task_store import store

PREF_KEY = "analysis_preferences"

DATA_SOURCES = ["sentinel2", "landsat", "auto"]

# 助手画像（Agent Profiles，借鉴 Earth Agent）
# 每个画像定义：问答风格 system 提示 + 代码生成风格要求
AGENT_PROFILES = {
    "analyst": {
        "label": "分析师（默认）",
        "desc": "专业严谨、结论清晰，适合日常分析",
        "ask_style": "以专业遥感分析师的风格回答：结构清晰、给出明确结论与可执行建议，必要处列出数据/公式依据。",
        "code_style": "代码结构清晰、注释精炼，关键步骤用中文注释说明。",
    },
    "mentor": {
        "label": "导师",
        "desc": "解释详尽、教学导向，适合学习",
        "ask_style": "以耐心的导师风格回答：由浅入深讲解原理，补充背景知识，帮用户真正理解而不仅是拿到答案。",
        "code_style": "代码注释详细，每一步都解释原理与参数含义，适合学习理解。",
    },
    "concise": {
        "label": "极简",
        "desc": "只给结论、不废话，适合快速查询",
        "ask_style": "以极简风格回答：直接给结论和要点，不做铺垫，能用一句话说清不用三句。",
        "code_style": "代码尽量精简，注释只写最必要的部分。",
    },
}

DEFAULT_PREFERENCES = {
    "instructions": "",          # 自由文本自定义指令
    "data_source": "auto",       # sentinel2 | landsat | auto
    "output_language": "zh",     # 代码注释语言
    "agent_profile": "analyst",  # 助手画像
}


def get_preferences(user_id: int | None = None) -> dict:
    """返回合并了默认值的偏好。

    `user_id=None` 时只读全局默认值（鉴权关闭 / 后台无需用户上下文的场景）。
    传了用户 ID 则私有值优先 —— 但不会串到别的用户，各人互不可见。
    """
    stored = store.get_settings(user_id).get(PREF_KEY) or {}
    merged = dict(DEFAULT_PREFERENCES)
    if isinstance(stored, dict):
        for k, v in stored.items():
            if v not in (None, ""):
                merged[k] = v
    if merged.get("data_source") not in DATA_SOURCES:
        merged["data_source"] = "auto"
    return merged


def save_preferences(updates: dict, user_id: int | None = None) -> dict:
    """合并写入偏好，返回最新值。"""
    current = get_preferences(user_id)
    for k in ("instructions", "data_source", "output_language", "agent_profile"):
        if k in updates and updates[k] is not None:
            current[k] = updates[k]
    if current.get("data_source") not in DATA_SOURCES:
        current["data_source"] = "auto"
    if current.get("agent_profile") not in AGENT_PROFILES:
        current["agent_profile"] = "analyst"
    store.set_setting(PREF_KEY, current, user_id)
    return current


def build_preference_note(prefs: dict | None = None, user_id: int | None = None) -> str:
    """把偏好转成一段注入代码生成 prompt 的中文说明；无自定义内容时返回空串。"""
    prefs = prefs or get_preferences(user_id)
    notes = []
    ds = prefs.get("data_source", "auto")
    if ds == "sentinel2":
        notes.append("优先使用 Sentinel-2 数据（COPERNICUS/S2_SR_HARMONIZED）")
    elif ds == "landsat":
        notes.append("优先使用 Landsat 数据（LANDSAT/LC08/C02/T1_L2）")
    instructions = (prefs.get("instructions") or "").strip()
    if instructions:
        notes.append(instructions)
    if not notes:
        return ""
    return "用户分析偏好（请尽量遵循）：\n" + "\n".join(f"- {n}" for n in notes)


def get_profile(user_id: int | None = None) -> dict:
    """返回当前助手画像定义。"""
    pid = get_preferences(user_id).get("agent_profile", "analyst")
    return AGENT_PROFILES.get(pid, AGENT_PROFILES["analyst"])
