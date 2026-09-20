"""Ask 模式：遥感专家问答（借鉴 Earth Agent 的 Ask 模式）。

用户在「咨询」模式下提问时，直接以遥感 / GEE 专家身份回答，
不生成代码、不执行任务。无 DeepSeek key 时返回说明文本。
"""

from __future__ import annotations

from .config import settings
from .preferences import get_profile

SYSTEM_PROMPT = (
    "你是资深卫星遥感与 Google Earth Engine（GEE）专家，面向零编程、非遥感专业的一线用户"
    "（生态环保监测、农业分析、科研人员）答疑解惑。回答要通俗、准确、有实操价值，"
    "多用中文，适当给出下一步可执行的分析建议。不要杜撰数据。"
)

FALLBACK = (
    "当前未配置 DeepSeek API Key，无法进行专家问答。\n"
    "配置方法：在 backend/.env 的 DEEPSEEK_API_KEY= 填入密钥后重启后端即可。"
    "（执行模式不受影响，仍可用离线样例演示。）"
)


def ask(question: str, history: list[dict] | None = None, user_id: int | None = None) -> dict:
    """回答遥感/GEE 相关问题，返回 {source, answer}。

    `user_id` 决定用谁的助手画像（分析师/导师/极简）。
    """
    q = (question or "").strip()
    if not q:
        return {"source": "empty", "answer": "请先输入你想咨询的问题。"}

    if not settings.deepseek_api_key:
        return {"source": "fallback", "answer": FALLBACK}

    import httpx

    profile = get_profile(user_id)
    system = SYSTEM_PROMPT + "\n\n" + profile["ask_style"]
    messages = [{"role": "system", "content": system}]
    for m in (history or [])[-6:]:
        role = m.get("role")
        text = (m.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": q})

    try:
        r = httpx.post(
            settings.deepseek_base_url + "/chat/completions",
            json={
                "model": settings.deepseek_model,
                "messages": messages,
                "temperature": 0.4,
                "stream": False,
            },
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            timeout=90,
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"].strip()
        return {"source": "deepseek", "answer": answer}
    except Exception as e:  # noqa: BLE001
        return {
            "source": f"error({type(e).__name__})",
            "answer": f"问答服务暂时不可用：{type(e).__name__}。请稍后重试或检查 DeepSeek 配置。",
        }
