r"""守卫：**Markdown 语法漏进渲染给用户看的 JSX 正文**。

为什么值得单独立一条（2026-09-28 真实缺陷）：

    结果页的「图层令牌已过期」告警里写的是

        令牌只能由 GEE 在**重新计算时**签发，没法凭空续期 ——

    这段是 **JSX 文本节点**，不是注释；而 JSX 不解析 Markdown。
    于是用户屏幕上原样显示 `**重新计算时**`（四个星号）。

    实测证据（无头浏览器抓到的告警正文，不是读代码猜的）：
        `栅格图层有 8 张瓦片加载失败（…），… 令牌只能由 GEE 在**重新计算时**签发 …`

为什么"看代码"发现不了：这个仓库里 `**` 大量出现在**注释**里（写文档式注释是这里的风格），
`grep -n '\*\*'` 出来的几十条里只有 1 条是正文 —— 噪音淹没信号。
所以判据必须是"**先剥掉注释，再找 `**`**"。

本脚本零依赖、纯文本扫描，不启动浏览器。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

# 想故意让某行保留字面 `**` 时，在这一行加注释标记即可
ALLOW = "md-ok"

# ⚠️ 只查 `**粗体**`，**故意不查**反引号行内代码与 `[](...)` 链接：
#   JS 里反引号是**模板串**，`{`...${x}...`}` 这种写法在 JSX 里到处都是且完全合法，
#   想区分"模板串"与"Markdown 行内代码"就得知道自己在不在 JSX 表达式容器里 —— 那需要
#   真解析 JSX，纯文本扫描只能产出一堆假阳性（实测：第一版扫出 16 处，**全部**是模板串）。
#   而 `**...**` 在 JS 里只可能是幂运算符 `a ** b`，凑不出"四个星号夹一段文字"的形状，
#   所以这一条判据是精确的、不会误报的 —— 宁可只守一类真问题，不要一类问题配十条噪音。
MD_BOLD = re.compile(r"\*\*[^*\n]{1,80}\*\*")


def strip_comments(text: str) -> str:
    """剥掉 // 行注释与 /* */ 块注释，其余字符原样保留（保持行号不漂移）。

    JSX 注释 `{/* ... */}` 天然包含在块注释规则里，不用单独处理。
    """
    out = []
    i, n = 0, len(text)
    state = None  # None | 'line' | 'block' | 'str' | 'tpl'
    quote = ""
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == "/" and nxt == "/":
                state, i = "line", i + 2
                continue
            if ch == "/" and nxt == "*":
                state, i = "block", i + 2
                continue
            if ch in "\"'":
                state, quote = "str", ch
                out.append(ch)
                i += 1
                continue
            if ch == "`":
                state = "tpl"
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
        elif state == "line":
            if ch == "\n":
                state = None
                out.append("\n")
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
                continue
            if ch == "\n":
                out.append("\n")  # 保留换行，行号才不会错位
            i += 1
        elif state == "str":
            out.append(ch)
            if ch == "\\":
                if nxt:
                    out.append(nxt)
                i += 2
                continue
            if ch == quote:
                state = None
            i += 1
        else:  # tpl 模板串
            out.append(ch)
            if ch == "\\":
                if nxt:
                    out.append(nxt)
                i += 2
                continue
            if ch == "`":
                state = None
            i += 1
    return "".join(out)


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """返回 [(行号, 命中的片段, 该行内容)]。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    stripped = strip_comments(raw)
    raw_lines = raw.splitlines()
    hits = []
    for idx, line in enumerate(stripped.splitlines(), start=1):
        if ALLOW in (raw_lines[idx - 1] if idx - 1 < len(raw_lines) else ""):
            continue
        for m in MD_BOLD.finditer(line):
            hits.append((idx, m.group(0), "粗体"))
    return hits


def main() -> int:
    # 前置条件不满足时按仓库约定说「跳过：」，由 regress_all.sh 记入"已跳过"，
    # **绝不**悄悄变成 0/0 的假绿。
    if not SRC.is_dir():
        print(f"跳过：找不到前端源码目录 {SRC}")
        return 0

    files = sorted(list(SRC.rglob("*.jsx")) + list(SRC.rglob("*.js")))
    files = [f for f in files if "node_modules" not in f.parts]

    bad = []
    for f in files:
        hits = scan_file(f)
        if hits:
            for ln, frag, kind in hits:
                bad.append((f.relative_to(SRC).as_posix(), ln, kind, frag))

    total = len(files)
    ok = total - len({b[0] for b in bad})
    for rel, ln, kind, frag in bad:
        print(f"[FAIL] {rel}:{ln}  [{kind}]  {frag}")

    if bad:
        print(f"\n渲染正文里出现了 Markdown 语法（JSX 不解析它，用户会看到裸星号）。")
        print(f"正文要强调就用 <strong>；确实要显示字面量时在那一行加注释标记 `{ALLOW}` 显式豁免。")

    # regress_all.sh 按「N/M 通过」解析，格式必须一致
    print(f"结果：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
