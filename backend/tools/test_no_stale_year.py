"""「年代漂移」回归守卫：防止默认日期被再次写死。

## 为什么需要这个测试

2026-09-17 在全栈查出 8 处写死的默认日期：

    backend/app/intent.py     兜底 2024-01-01/2024-12-31，提示词"补全为 2025 年"
    backend/app/planner.py    提示词"补全为 2025 年"
    backend/app/codegen.py    生成代码里 or '2024-01-01'
    backend/app/models.py     契约默认 2024-01-01/2024-12-31
    frontend/src/App.jsx      表单初始态 + 提交兜底 2024-01-01/2024-12-31

这类 bug 有两个要命的特点：

  1. **不报错**。代码跑得好好的，只是默默用了两年前的数据。只有人肉核对结果里的
     时间范围才能发现——典型表现是用户说"现在"，界面显示 2024 年。
  2. **修好之后还会再犯**。因为写死一个日期的那一刻，它看起来永远是对的。

所以下面的断言刻意**不是**"当前值对不对"，而是"值是否随当天推算"——这样它今天
能过、明年也能过，但一旦有人再把日期写死就立刻失败。

## 检查项

  A. daterange 默认区间随基准日变化（换基准日必须换出对应结果，且同一自然年内稳定）
  B. AnalysisRequest 契约默认值确实来自 daterange（而不是自己的常量）
  C. app/ 下可执行代码里没有日期字面量（用 ast 解析，注释天然不算）
  C2. 扫描器自身的精度——叙述文本不误报、代码字面量不漏报、拼接字面量的行号报得准
  D. frontend/src 下没有日期字面量（正则扫描 + 去注释）

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe tools\\test_no_stale_year.py
"""

import ast
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BACKEND = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = BACKEND / "app"
FRONTEND_SRC = BACKEND.parent / "frontend" / "src"

import app.daterange as dr  # noqa: E402
from app.models import AnalysisRequest, TaskType  # noqa: E402

ok = []


def check(name, cond, extra=""):
    ok.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))


def with_today(ref: date, fn):
    """把 daterange 的"当天"替换成 ref 后调用 fn。

    替换的是 daterange.today 而不是 datetime.date：daterange 内部的
    default_start/default_end 都经由 today() 取值，models 的 default_factory
    也是直接引用 daterange 的函数对象，所以换这一个点就能覆盖整条链路。
    """
    orig = dr.today
    dr.today = lambda: ref
    try:
        return fn()
    finally:
        dr.today = orig


# =====================================================================
# A. 默认区间必须随基准日推算
# =====================================================================
print("\n[A] daterange 默认区间随基准日推算")

check("today() 不再是常量", dr.today() == date.today(), dr.today())

# 同一自然年内的三个基准日必须给出**同一个**区间（这是选"完整自然年"口径的
# 好处之一：年内结果可复现，做回归比对时不会因为跑测试的月份不同而漂移）。
for ref in (date(2026, 1, 1), date(2026, 9, 17), date(2026, 12, 31)):
    s, e = with_today(ref, dr.default_range)
    check(
        f"基准日 {ref} 落在完整自然年 {ref.year - 1}",
        (s, e) == (f"{ref.year - 1}-01-01", f"{ref.year - 1}-12-31"),
        f"{s} ~ {e}",
    )

# 跨年必须跟着走——这一条是"没被写死"的硬证据：如果默认值还是常量，
# 2031 年的基准日会返回和 2026 年相同的结果，本条会 FAIL。
r2026 = with_today(date(2026, 6, 1), dr.default_range)
r2031 = with_today(date(2031, 6, 1), dr.default_range)
check("跨 5 年后默认区间跟着前移", r2026 != r2031, f"{r2026} vs {r2031}")
check("2031 年的默认年份是 2030", with_today(date(2031, 6, 1), dr.default_year) == 2030)

# 提示词里注入的"今天"同样要跟着变，否则模型会按旧日期推算月份区间
check(
    "month_range_rule 里的今天随基准日变",
    "2031-06-01" in with_today(date(2031, 6, 1), dr.month_range_rule),
)
check(
    "month_range_rule 不含写死的年份",
    "补全为 20" not in with_today(date(2031, 6, 1), dr.month_range_rule),
    with_today(date(2031, 6, 1), dr.month_range_rule),
)


# =====================================================================
# B. 接口契约的默认值也走 daterange
# =====================================================================
print("\n[B] AnalysisRequest 契约默认值来自 daterange")

req = with_today(
    date(2030, 5, 5), lambda: AnalysisRequest(task_type=TaskType.ndvi, region="太湖流域")
)
check("start_date 随当天推算", req.start_date == "2029-01-01", req.start_date)
check("end_date 随当天推算", req.end_date == "2029-12-31", req.end_date)

# 显式传值时不能被默认值覆盖（否则用户指定的历史年份会被悄悄改掉）
req_explicit = AnalysisRequest(
    task_type=TaskType.ndvi, region="太湖流域", start_date="2020-01-01", end_date="2020-12-31"
)
check("显式日期不被默认值覆盖", (req_explicit.start_date, req_explicit.end_date) == ("2020-01-01", "2020-12-31"))


# =====================================================================
# C. app/ 可执行代码里不能有日期字面量
# =====================================================================
print("\n[C] app/ 可执行代码中无日期字面量")

# 匹配 2024-01-01 / 2025/6/1 / 2025年6月 这类日期写法。
# 末尾的 (?![0-9]) 是为了不把 "2020-2021" 这种年份区间在这里命中成
# 半截的 "2020-20"（假阳性且文本残缺）。
#
# 但年份区间本身**也是**要防的：offline.py 原来就写死了
# ["2020-2021", ..., "2023-2024"] 作为图表标签，是这次审计里的一处真实缺陷。
# 所以第二条分支专门匹配「完整的年份区间」，两条合起来：
#   完整日期 → 第一条命中；年份区间 → 第二条命中；两者都不会产出残缺文本。
DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}\s*[-/年]\s*\d{1,2}(?![0-9])"
    r"|\b(?:19|20)\d{2}\s*[-–~]\s*(?:19|20)\d{2}\b"
)


def _docstring_constants(tree):
    """收集所有 docstring 的节点 id。

    daterange.py 的模块注释里**故意**记录着历史写死值（作为案例说明），
    那是文档不是代码，扫描时必须跳过，否则测试会把自己的说明文档告了。
    """
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


# 叙述性长文本的阈值：超过这个长度且含中文，按「说明文字」处理，不判违规。
PROSE_MIN_LEN = 40


def _is_prose(value: str) -> bool:
    """判断一个字符串常量是不是「叙述文本」而不是「数据字面量」。

    为什么需要它：提示词里写「实测教训：2026-09-17 那次……」是**说明文字**，
    不是会被当成日期用的值。如果扫描器把这种也算违规，那么每次往 prompt 里
    补一句经验教训都会让守卫变红 —— 而反复误报的守卫会被无视，
    被无视的守卫和没有守卫是同一个结果。

    ⚠ 已知盲区（写在这里，不藏起来）：一个**超过 40 字的中文串**里如果
    真的藏着默认日期（例如某条中文错误提示里写死 "默认区间 2024-01-01"），
    会被这里跳过。这是有意的取舍 —— 主要防线不是这个粗扫描，而是：
      [A] daterange 是否随基准日推算  [B] AnalysisRequest 契约默认值
      [D] App.jsx 是否走 defaultDateRange()
    这几条是按语义断言的，不受文本长短影响。真被跳过时下面会打 NOTE，
    不是静默丢弃。
    """
    return len(value) >= PROSE_MIN_LEN and _has_cjk(value)


def _locate_lines(source_lines, node, matched: str):
    """在 node 覆盖的源码行范围里，找出 `matched` 真正出现的那一行（1-based）。

    为什么不能直接用 node.lineno：相邻的字符串字面量会被 Python 隐式拼接成
    **同一个** Constant 节点，而它的 lineno 指向拼接体的**第一行**。
    实测 `app/codegen.py` 里第 40 行的「2026-09-17」被报成了第 17 行 ——
    指出一个错误的行号，比不指出更浪费时间（会让人去改错地方）。
    这在 debugger.py 里是同一类问题（丢弃 traceback → 丢位置信息），
    在那里踩过一次，这里不该再踩。
    """
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    for i in range(start, min(end, len(source_lines)) + 1):
        if matched in source_lines[i - 1]:
            return i
    return start


def scan_python(path: Path):
    """返回 (hits, notes)。

    hits  = [(行号, 命中文本)]  —— 判违规
    notes = [(行号, 命中文本)]  —— 命中但判定为叙述文本，只提示不判违规
    用 ast 解析而不是正则搜全文，注释天然被排除。
    """
    src = path.read_text(encoding="utf-8")
    source_lines = src.splitlines()
    tree = ast.parse(src, filename=str(path))
    skip = _docstring_constants(tree)
    hits, notes = [], []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip:
            continue
        for m in DATE_RE.finditer(node.value):
            text = m.group(0)
            lineno = _locate_lines(source_lines, node, text)
            (notes if _is_prose(node.value) else hits).append((lineno, text))
    return hits, notes


py_files = sorted(APP_DIR.rglob("*.py"))
check("扫到了 app/ 下的 python 文件", len(py_files) >= 10, f"{len(py_files)} 个")

py_hits = []
py_notes = []
for f in py_files:
    h, n = scan_python(f)
    py_hits += [f"{f.relative_to(BACKEND)}:{ln} {tx}" for ln, tx in h]
    py_notes += [f"{f.relative_to(BACKEND)}:{ln} {tx}" for ln, tx in n]
check("app/ 下没有写死的日期字面量", not py_hits, "; ".join(py_hits) or "0 处")
if py_notes:
    print("  NOTE  以下命中被判定为叙述文本（不判违规，但列出来供人复核）：")
    for it in py_notes:
        print("        " + it)


# ---------------------------------------------------------------------
# C2. 扫描器自身的精度：会漏报吗？会报错行号吗？
# ---------------------------------------------------------------------
print("\n[C2] 扫描器自身的精度")

_SELF_PROBE = '''
_RULES = (
    "这是一段足够长的中文叙述性说明文字，里面顺带提到了 2026-09-17 这个日期，"
    "它属于经验记录，不是数据字面量，扫描器应当跳过而不是判违规。\\n"
    "第二行继续叙述，长度也足够被判定为叙述文本。\\n"
)
DEFAULT_START = "2024-01-01"
RANGE_LABELS = ["2020-2021", "2021-2022"]
'''
with tempfile.TemporaryDirectory() as td:
    probe = Path(td) / "probe.py"
    probe.write_text(_SELF_PROBE, encoding="utf-8")
    ph, pn = scan_python(probe)
    probe_lines = _SELF_PROBE.splitlines()

    check("叙述文本里的日期不判违规（但会打 NOTE）",
          len(ph) == 3 and len(pn) == 1,
          f"违规={len(ph)} 叙述={len(pn)}（期望 违规=3、叙述=1）")
    check("代码里的完整日期被判违规",
          any(tx == "2024-01" for _, tx in ph),
          "; ".join(f"{ln}:{tx}" for ln, tx in ph))
    check("代码里的年份区间被判违规（旧正则漏掉的那类）",
          sum(1 for _, tx in ph if tx.startswith("2020") or tx.startswith("2021")) == 2,
          "; ".join(f"{ln}:{tx}" for ln, tx in ph))
    check("拼接字面量的行号定位到命中所在行（不是首行）",
          all(probe_lines[ln - 1].find(tx) >= 0 for ln, tx in ph),
          "; ".join(f"上报第{ln}行 → 「{probe_lines[ln-1].strip()[:26]}」" for ln, tx in ph))
    check("叙述命中行号也定位准确",
          all(probe_lines[ln - 1].find(tx) >= 0 for ln, tx in pn),
          "; ".join(f"第{ln}行 → 「{probe_lines[ln-1].strip()[:26]}」" for ln, _ in pn))


# =====================================================================
# D. frontend/src 里也不能有日期字面量
# =====================================================================
print("\n[D] frontend/src 中无日期字面量")


def strip_js_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，避免把注释里的举例日期算成违规。

    注意：这个做法会把字符串里的 "//"（例如 URL）也一并截断。对本测试无影响
    ——截断只会让扫描区间变短，不会凭空造出日期；而字符串里的真实日期（如
    start_date: '2024-01-01'）在注释符**之前**，依然会被扫到。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


js_files = sorted(list(FRONTEND_SRC.glob("*.jsx")) + list(FRONTEND_SRC.glob("*.js")))
if not js_files:
    check("前端源码目录存在", False, f"未找到 {FRONTEND_SRC}")
else:
    js_hits = []
    for f in js_files:
        stripped = strip_js_comments(f.read_text(encoding="utf-8"))
        for i, line in enumerate(stripped.splitlines(), 1):
            for m in DATE_RE.finditer(line):
                js_hits.append(f"frontend/src/{f.name}:{i} {m.group(0)}")
    check(f"扫到了前端源文件（{len(js_files)} 个）", True)
    check("frontend/src 下没有写死的日期字面量", not js_hits, "; ".join(js_hits) or "0 处")

    app_jsx = FRONTEND_SRC / "App.jsx"
    if app_jsx.exists():
        src = app_jsx.read_text(encoding="utf-8")
        check("App.jsx 使用 defaultDateRange() 取默认区间", "defaultDateRange()" in src)
        check("App.jsx 定义了 defaultDateRange 函数", "function defaultDateRange" in src)


print("\n结果：%d/%d 通过" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
