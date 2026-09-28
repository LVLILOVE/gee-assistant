r"""定位本机 Chromium 系浏览器（给"取真实计算样式"的无头浏览器套件用）。

⚠️ 为什么会有这个模块（2026-09-28 自检实测出来的真实缺陷）：

   两个无障碍套件原先各写着一个常量，它的值直接指向

       C:\Program Files (x86)\Microsoft\Edge\Application\153.0.4234.48\msedge.exe

   （同一批硬编码实测散落在 **5 个文件**里：两个无障碍套件 + `_shot_ui.py`
   / `_shot_focus.py` / `_shot_result.py`。下面故意不把那个值写成字符串字面量 ——
   `test_audit_regressions.py` 的 I 段会扫 `tools/*.py` 里的字符串字面量，
   写了本文件自己就会被判违规。）

   Edge 自动更新后，旧版本目录会被改名成 `153.0.4234.48.deleting`，
   于是 `subprocess.Popen` 直接抛 `FileNotFoundError: [WinError 2]` ——
   **不是校验失败，是整套校验根本没跑起来。**
   而这两个套件只有显式带 `--with-a11y` 才会执行，所以"无障碍校验层已经整体
   不可用"这件事长期没人发现（跑的人只看到一句 `没解析到结果行`）。

   结论：**任何绑死版本号的可执行文件绝对路径，都会随自动更新失效。**
   这条与 `test_no_stale_year.py` 守的是同一类问题：写死的"当时有效的环境值"。

定位顺序（先稳后新，逐级兜底）：

   1. PATH 上的 `msedge` / `chrome` / `chromium`（最通用，不依赖安装位置）
   2. Edge 的**无版本号稳定入口** `Application\\msedge.exe`（Windows 上一直存在，
      快捷方式指向的就是它）
   3. Chrome 的稳定入口
   4. 兜底：扫描 `Application\\<版本号>\\`，**显式跳过 `.deleting`**（那是更新残留，
      里面的 exe 可能已被删或不可执行），取版本号最大者

   任何一级命中就返回，都不命中返回 `None` —— 由调用方决定怎么"优雅跳过"，
   本模块不抛异常、也不猜路径。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# 可能安装到的根目录（含 32 位程序的 Program Files (x86)）
_ROOTS = (
    Path(os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"),
    Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files"),
    Path(os.environ.get("LOCALAPPDATA") or "."),
)

# 厂商目录 / 产品目录 / 可执行文件名
_LAYOUTS = (
    (("Microsoft", "Edge", "Application"), ("msedge.exe",)),
    (("Google", "Chrome", "Application"), ("chrome.exe",)),
)

# 版本目录名形如 154.0.4258.37
_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")

# 找不到时给用户的提示（列出真实找过的位置，避免"我也不知道它在哪找"）
HINT = "、".join(str(r.joinpath(*lay)) for r in _ROOTS[:2] for lay, _ in _LAYOUTS)


def _stable_entries():
    """无版本号的稳定入口，逐级返回。"""
    for root in _ROOTS:
        for lay, exes in _LAYOUTS:
            for exe in exes:
                yield root.joinpath(*lay, exe)


def _versioned_entries():
    """扫描版本目录（跳过 .deleting），按版本号从大到小返回。"""
    found: list[tuple[tuple[int, ...], Path]] = []
    for root in _ROOTS:
        for lay, exes in _LAYOUTS:
            base = root.joinpath(*lay)
            if not base.is_dir():
                continue
            try:
                children = list(base.iterdir())
            except OSError:
                continue
            for d in children:
                if not d.is_dir():
                    continue
                # ⚠️ 必须跳过：Edge/Chrome 更新时会把旧版本重命名成 xxx.deleting，
                #    里面的 exe 可能已被删掉或正处于被移除状态 —— 选中它就是重演本 bug。
                if "deleting" in d.name.lower():
                    continue
                m = _VER.match(d.name)
                if not m:
                    continue
                for exe in exes:
                    p = d / exe
                    if p.is_file():
                        found.append((tuple(int(x) for x in m.groups()), p))
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found]


def find_edge():
    """返回可用的浏览器可执行文件路径（str）；找不到返回 None。"""
    for name in ("msedge", "chrome", "chromium"):
        which = shutil.which(name)
        if which:
            return which
    for p in _stable_entries():
        if p.is_file():
            return str(p)
    versioned = _versioned_entries()
    return str(versioned[0]) if versioned else None


def capture_png(cdp, params=None, attempts=4):
    """调 `Page.captureScreenshot`，返回 base64 数据；重试到上限仍失败返回 None。

    ⚠️ 为什么"只拍一张"的脚本也需要重试：headless Edge 会**偶发丢弃**某一次
    `Page.captureScreenshot` 的响应 —— 实测丢弃率随时间/活动量上升（同一轮里
    0/30 → 1/30 → 2/20），且**与图幅大小无关**（小图同样会丢）；命中的那一刻
    连接与浏览器都是健康的（其它命令正常），**把同一条请求原样重发即成功**。
    完整证据链与已排除的假设见 `test_a11y_kbd.py` 里 `CDP.shot()` 的注释。

    单张脚本命中概率看着不高，但一旦命中就是"脚本跑完却没产出图"，
    很容易被误判成"页面本身有问题"。所以统一收在这里，避免每个脚本各写一遍。
    """
    p = {"format": "png"} if params is None else params
    for _ in range(attempts):
        try:
            r = cdp.call("Page.captureScreenshot", p)
        except (TimeoutError, OSError):
            continue
        if r and r.get("data"):
            return r["data"]
    return None


if __name__ == "__main__":  # 便于人工排查：python tools/browser_util.py
    hit = find_edge()
    print(hit or f"未找到浏览器；找过：{HINT}")
