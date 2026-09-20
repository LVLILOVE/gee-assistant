"""错误分类与摘要：决定"喂给大模型什么信息来修代码"。

## 为什么这里需要解释 traceback（2026-09-17）

原先 `summarize_error()` 只返回 `error["message"]`，把 `error["traceback"]`
整个丢掉了。而沙箱其实**已经采集好** traceback，里面明确写着出错行：

    File "<gee_code>", line 72, in <module>
    KeyError: 'ndvi'

实测代价：变化检测任务报 `KeyError: 'ndvi'`，调试器只把这一句喂给大模型，
让它在 5300 字符的代码里自己找位置。同一段代码里有两处 `ndvi`，猜错一次就
白烧一轮 —— 结果 3 次重试原样失败，指标二/三（一次成功率、调试后完成率）
因此不达标。

教训：调试器拿不到位置信息时，"自动调试"就退化成"让大模型重写一遍碰运气"。
信息本来就在手里，没有理由丢。
"""

import re

RULES = [
    ("auth", r"auth|credential|initialize|permission|401|403|unauthorized"),
    ("region", r"geometry|region|aoi|coordinate|polygon|bounds|collection has no"),
    ("param", r"band|parameter|argument|not found|missing|invalid|keyerror|name"),
    ("operator", r"operator|method|attribute|nameerror|typeerror|syntaxerror|ee\.exception"),
]

#: 从 traceback 里抠出用户代码（<gee_code>）的出错行号
_USER_FRAME_RE = re.compile(r'File "<gee_code>", line (\d+)')

#: `.map()` / `.iterate()` 是**回调式**调用：里面的报错，行号会指向调用点
_CALLBACK_RE = re.compile(r"\.(?:map|iterate)\s*\(")

#: 找代码里定义过的顶层函数名（用于把回调函数体捞出来给大模型看）
_DEF_RE = re.compile(r"(?m)^def\s+([A-Za-z_]\w*)\s*\(")


def classify_error(message: str) -> str:
    m = (message or "").lower()
    for cat, pat in RULES:
        if re.search(pat, m):
            return cat
    return "other"


def locate_error_line(error: dict, code: str = "") -> tuple[int | None, str]:
    """从 traceback 定位用户代码的出错行；返回 (行号, 该行源码)。

    取**最后一个**匹配：traceback 从外到内排列，最后一条 <gee_code> 帧
    才是真正抛错的位置。
    """
    tb = (error or {}).get("traceback") or ""
    hits = _USER_FRAME_RE.findall(tb)
    if not hits:
        return None, ""
    lineno = int(hits[-1])
    src = ""
    if code:
        lines = code.splitlines()
        if 1 <= lineno <= len(lines):
            src = lines[lineno - 1].strip()
    return lineno, src


def extract_defs(code: str, names) -> str:
    """把指定顶层函数的完整定义抠出来（含从 `def` 到下一个顶格语句为止）。"""
    if not code or not names:
        return ""
    lines = code.splitlines()
    spans = []
    for name in names:
        start = None
        for i, ln in enumerate(lines):
            if _DEF_RE.match(ln) and _DEF_RE.match(ln).group(1) == name:
                start = i
                break
        if start is None:
            continue
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].strip() and not lines[j][:1].isspace():
                end = j
                break
        spans.append((start, end))
    spans.sort()
    return "\n".join("\n".join(lines[s:e]) for s, e in spans)


def callback_note(lineno: int | None, src: str, error: dict, code: str) -> str:
    """`.map()` / `.iterate()` 内的报错，位置指向调用点而不是缺陷点——必须说明。

    实测（2026-09-17）：分类任务报
        AttributeError: 'Image' object has no attribute 'mode'
    traceback 把 `<gee_code>` 帧定在第 133 行 `ee.List(months).map(monthly_class_stats)`。
    这一行**本身完全正确**，真正的问题在被 map 的函数体里
    （`sub.select('label').mode()` —— `ee.Image` 没有 `.mode()`）。
    只把"第 133 行"喂给大模型，它会去改一行没问题的代码，
    于是 3 次重试原地打转、错误信息逐字相同。

    所以这里额外做两件事：
      1. 明说"位置是调用点，缺陷在被 map 的函数里"；
      2. 把那个函数的**完整定义**捞出来一起给出——它通常只有十几行，
         比让模型在 200 行代码里找快得多。
    """
    if lineno is None or not src or not _CALLBACK_RE.search(src):
        return ""
    names = [m.group(1) for m in re.finditer(r"\b([A-Za-z_]\w*)\b", src)]
    body = extract_defs(code, names)
    msg = (
        f"⚠ 注意：第 {lineno} 行是一次 `.map()` / `.iterate()` 调用，**它本身通常没有错**。\n"
        f"  真正报错的是传给它的那个函数体内部（回调里抛出的异常，行号会指向调用点）。\n"
        f"  请优先检查下面这段函数体：\n{body}" if body else
        f"⚠ 注意：第 {lineno} 行是一次 `.map()` / `.iterate()` 调用，**它本身通常没有错**。\n"
        f"  真正报错的是传给它的那个函数体内部（回调里抛出的异常，行号会指向调用点）。\n"
        f"  请把该函数体里用到的每个方法逐个核对是否真的存在于对应的 EE 对象上。"
    )
    return msg


def traceback_tail(error: dict, limit: int = 1200) -> str:
    """traceback 的尾部。保留尾部是因为关键帧（用户代码 + 异常类型）在最后。"""
    return ((error or {}).get("traceback") or "").strip()[-limit:]


#: 空影像（0 波段）导致的运算报错。GEE 的报错格式是
#:   `Image.gt: If one image has no bands, the other must also have no bands. Got 0 and 1.`
#: 关键是 **`Got 0 and N`** 里的 0：说明有一侧那张 Image 一个波段都没有。
#: 成因几乎总是「某个时段的 ImageCollection 为空 → .median()/.mosaic() 产出 0 波段影像
#: → 再拿它做 subtract/gt/lt/add」。报错行只是**症状**，真正要修的是上游那次合成。
_EMPTY_BAND_RE = re.compile(
    r"no bands|Got 0 and|has no bands|bandNames.*empty", re.IGNORECASE
)


def empty_band_note(error: dict) -> str:
    """0 波段影像运算报错的定向提示。

    实测（2026-09-20）：深圳 change_detection 任务，两个时段各取 median 后相减，
    其中一个时段过滤后为空，`delta.gt(0.1)` 抛
        EEException: Image.gt: If one image has no bands, the other must also have no bands.
    连续 3 次重试全部失败 —— 因为模型只盯着报错行去改阈值表达式，没意识到
    病根是**上游某次合成拿到了空集合**。

    这里明确告诉模型：别改报错行，去给上游合成加空集合兜底。
    """
    msg = (error or {}).get("message", "") or ""
    if not _EMPTY_BAND_RE.search(msg):
        return ""
    return (
        "⚠ 定向提示：这个报错**不是**阈值/运算写法的问题，而是"
        "**上游某次影像合成拿到了空集合，产出了一张「0 波段」的 Image**。\n"
        "  判据：报错里的 `Got 0 and N` —— 0 就是那张没有波段的图。\n"
        "  根因通常是某个时段 / 某个月的 ImageCollection 过滤后一张影像都没有，"
        "对它调 `.median()` / `.mosaic()` / `.mean()` 就会得到 0 波段影像。\n"
        "  正确修法（**不要去改报错那一行**）：\n"
        "   1) 找到产出该影像的那次合成，先判空再合成，例如\n"
        "        comp = col.median() if col.size().getInfo() > 0 else ee.Image.constant(0).rename('X')\n"
        "      或在运算前统一 `.unmask(0)`；\n"
        "   2) 更稳的是用 ee.Algorithms.If 包一层，两侧都确认 `.bandNames().size().gt(0)` 再相减；\n"
        "   3) 顺带放宽该时段的取数条件（去掉过严的云量过滤、改用 filterDate 后 "
        "sort('CLOUDY_PIXEL_PERCENTAGE').limit(N) 取最晴几景），从源头避免空集合。"
    )


#: Feature 属性缺失（KeyError）。**这是本项目历史上最高频的失败类之一** ——
#: 提示词规则 j 记录了「鄱阳湖 2024-2025 存在空月，连续 3 次重试全部 KeyError」。
#: GEE 的 Feature 属性值为 None 时，`getInfo()` 返回的 properties 里**根本没有这个键**，
#: 于是 `f['properties']['ndvi']` 在第一个空月就 KeyError 掉整段分析。
_KEYERROR_RE = re.compile(r"KeyError[:\s]*['\"]?([A-Za-z_][\w]*)", re.IGNORECASE)


def keyerror_note(error: dict, code: str = "") -> str:
    """Feature 属性 KeyError 的定向提示。

    实测（2026-09-20 复核历史失败任务）：`change_detection` 任务报 `KeyError: 'ndvi'`，
    连续 3 次重试全部失败。模型反复去改取值表达式，但病根是
    **用了会崩的取值语法**（`f['properties']['ndvi']`），而不是键名写错。

    提示要点：换成 `.get()`，并且空月是**正常现象**（不是要先修数据）。
    """
    msg = (error or {}).get("message", "") or ""
    m = _KEYERROR_RE.search(msg)
    if not m:
        return ""
    key = m.group(1)
    return (
        f"⚠ 定向提示：`KeyError: '{key}'` 的成因**几乎肯定是取值语法**，而不是键名拼错。\n"
        "  根因：GEE 的 Feature 属性值为 None 时（**空月/空时段的正常表现**），"
        "`getInfo()` 返回的 properties 里**根本不会出现这个键**，"
        f"于是 `f['properties']['{key}']` 直接 KeyError，整段分析被打断。\n"
        "  正确修法（**不要**去改键名，也不要去动上游的数据筛选逻辑）：\n"
        f"   把 `f['properties']['{key}']` 全部换成 `f['properties'].get('{key}')`；\n"
        "   若是服务端取值（没用 getInfo），用 `feat.get('{0}')` 或 "
        "`feat.getNumber('{0}')` 形式；\n"
        "   并对 None 做显式处理（跳过该月 / 记 None），**不要静默写成 0**。\n"
        "  实测教训：空月是**正常数据现象**，不是要先修掉的缺陷 —— 让代码容忍它即可。"
    ).replace("{0}", key)


def summarize_error(error: dict, code: str = "") -> str:
    """把错误摘要成给大模型看的文本。

    `code` 可选：给了就能把出错那一行的源码直接摆到模型面前，
    比只给行号更省一轮猜测。
    """
    err = error or {}
    parts = [f"错误类型：{err.get('category', 'other')}",
             f"错误信息：{err.get('message', '')}"]

    empty_note = empty_band_note(err)
    if empty_note:
        parts.append(empty_note)

    ke_note = keyerror_note(err, code)
    if ke_note:
        parts.append(ke_note)

    lineno, src = locate_error_line(err, code)
    if lineno is not None:
        loc = f"出错位置：代码第 {lineno} 行"
        if src:
            loc += f"\n该行源码：{src}"
        parts.append(loc)
        note = callback_note(lineno, src, err, code)
        if note:
            parts.append(note)

    tail = traceback_tail(err)
    if tail:
        parts.append(f"Python traceback：\n{tail}")

    return "\n".join(parts)
