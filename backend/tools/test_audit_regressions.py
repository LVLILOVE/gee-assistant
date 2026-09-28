#!/usr/bin/env python
"""2026-09-28 产品自检发现的缺陷 —— 回归守卫。

为什么单独一个文件：这些缺陷**回归套件原本一条都覆盖不到**（当时 9 套件 488/488 全绿，
但下面每一条都是真实可复现的错误行为）。全绿却不等于没问题，所以把这次的
判据钉死在这里，防止将来改动把它们悄悄带回来。

⚠ 引用历史总数只写**可查证**的值：488 见提交信息里的 `462/462 → 488/488` 那轮。
  本文件早前曾误写 537，属笔误，别照抄。

覆盖的 7 个缺陷（每条都是"能构造出触发路径"的真实问题，不是理论风险）：

  A. 结论摘要的"峰值/最低月份"在有空月时指到错月份
     —— 过滤 None 后的下标被直接拿去索引 labels。
  B. 兜底代码把用户可控文本（区域名/日期）未转义就拼进生成代码
     —— 一个引号 = SyntaxError；一个换行 = 注释逃逸 + 代码注入。
  C. fail_orphans() 拿**密文**直接 json.loads
     —— 抛异常被吞 → 上次中断任务的日志被整体抹掉，且把明文写回加密列。
  D. 意图解析的 start/end 各自独立兜底 → 可产出倒置区间
     —— GEE filterDate 静默过滤成空集合，白烧重试。
  E. 枚举外的 task_type 被判成 complete
     —— 前端自动提交 → Pydantic 422，用户无法自助恢复。
  F. 瓦片代理的重试层数叠乘 → 单张瓦片尾部延迟可达分钟级
     —— 同步端点占满线程池 → 整个服务假死。
  G. 沙箱图层生成失败只写进 Reporter.notes，而 notes 从不被读
     —— 任务报 succeeded 但地图空白，用户拿不到失败原因。

纪律：只用临时库 + 临时密钥，**绝不碰生产 data/app.db**。
输出格式必须收尾于「结果：N/M 通过」（regress_all.sh 靠它抓结果）。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------- 环境隔离
# ⚠ 必须在 import app.* 之前落盘：一 import app.task_store 就会触发
#   建表 + 加密回填，不隔离的话会真改写线上库（本项目被这一步误伤过）。
_HERE = pathlib.Path(__file__).resolve().parent
_BACKEND = _HERE.parent
sys.path.insert(0, str(_BACKEND))

_tmp = tempfile.mkdtemp(prefix="audit_regress_")
os.environ["APP_DB_PATH"] = str(pathlib.Path(_tmp) / "app.db")
os.environ["WB_DATA_KEY_PATH"] = str(pathlib.Path(_tmp) / "data.key")

from app import crypto  # noqa: E402
from app import tile_proxy  # noqa: E402
from app.codegen import _fallback_code  # noqa: E402
from app.conclusion import _chart_digest  # noqa: E402
from app.intent import _summarize  # noqa: E402
from app.models import AnalysisRequest, TaskType  # noqa: E402
from app.net_retry import retry_policy  # noqa: E402
from app.task_store import TaskStore  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  -> {detail}" if detail else ""))


def _top_level_imports(code: str) -> list[str]:
    """列出生成代码里**顶层可执行到的** import 的顶层模块名。

    用于判定"注入是否成功"：注入的语句一定表现为多出一个 import。
    """
    return sorted({
        n.names[0].name.split(".")[0]
        for n in ast.walk(ast.parse(code))
        if isinstance(n, (ast.Import, ast.ImportFrom)) and n.names
    })


print("=" * 66)
print(" 产品自检修复回归（2026-09-28）   临时目录 " + _tmp)
print("=" * 66)

# ============================================================ A 结论下标
print("\n[A] 结论摘要：空月不得导致峰值/最低月份错位")
d1 = _chart_digest([{
    "title": "逐月 NDVI 均值", "kind": "line",
    "labels": ["1月", "2月", "3月"],
    "series": [{"name": "NDVI", "data": [None, 0.3, 0.5]}],
}])
check("中间月缺测时峰值指到 3月而非 2月",
      "峰值 0.5（3月）" in d1 and "峰值 0.5（2月）" not in d1, d1)
check("中间月缺测时最低指到 2月", "最低 0.3（2月）" in d1, d1)

d2 = _chart_digest([{
    "title": "t", "kind": "line",
    "labels": ["1月", "2月", "3月"],
    "series": [{"name": "v", "data": [0.9, None, 0.1]}],
}])
check("首月缺测时下标仍对齐（峰值 1月 / 最低 3月）",
      "峰值 0.9（1月）" in d2 and "最低 0.1（3月）" in d2, d2)

# ============================================================ B 兜底代码转义
print("\n[B] 兜底代码：用户可控文本必须转义（不得语法错误 / 不得注入）")
try:
    code_q = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="L'Aquila"))
    compile(code_q, "<gen>", "exec")
    check("含单引号的正常地名可编译（原先是 SyntaxError）", True)
except SyntaxError as exc:
    check("含单引号的正常地名可编译（原先是 SyntaxError）", False, str(exc))

_INJECT = '太湖"\nimport os\nimport subprocess\nprint("PWNED")\n#  '
try:
    code_i = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region=_INJECT))
    compile(code_i, "<gen>", "exec")
    imports = _top_level_imports(code_i)
    bad = [m for m in ("os", "subprocess", "sys", "shutil") if m in imports]
    check("区域名无法逃逸注释注入语句", not bad, f"imports={imports}")
except SyntaxError as exc:
    check("区域名无法逃逸注释注入语句", False, f"SyntaxError: {exc}")

try:
    code_d = _fallback_code(AnalysisRequest(
        task_type=TaskType.water, region="太湖流域",
        start_date="2025-01-01';import os#", end_date="2025-12-31"))
    compile(code_d, "<gen>", "exec")
    imports_d = _top_level_imports(code_d)
    check("日期字段无法注入语句", "os" not in imports_d, f"imports={imports_d}")
except SyntaxError as exc:
    check("日期字段无法注入语句", False, f"SyntaxError: {exc}")

code_n = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="太湖流域"))
check("正常区域仍能生成 AOI 矩形（修转义不能把功能修坏）",
      "ee.Geometry.Rectangle([" in code_n)

# ============================================================ C fail_orphans
print("\n[C] fail_orphans：不得抹掉日志 / 不得破坏加密不变量")
store = TaskStore(pathlib.Path(_tmp) / "app.db")
tid = store.create(AnalysisRequest(task_type=TaskType.ndvi, region="守卫测试区域"), user_id=1)
store.update(tid, status="running", logs=["[生成] 第一版代码", "[执行] 沙箱返回 ok"])
handled = store.fail_orphans()
check("能识别并处理遗留的 running 任务", handled >= 1, f"处理 {handled} 条")

conn = sqlite3.connect(str(pathlib.Path(_tmp) / "app.db"))
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT status, logs FROM tasks WHERE task_id=?", (tid,)).fetchone()
conn.close()
check("写回的 logs 仍是密文（加密列恒为密文）",
      bool(row["logs"]) and crypto.is_encrypted(row["logs"]),
      f'前缀={(row["logs"] or "")[:5]}')
kept = json.loads(crypto.decrypt_field(row["logs"]))
check("原有日志未被抹掉（原先是整体丢失）",
      "[生成] 第一版代码" in kept and "[执行] 沙箱返回 ok" in kept, str(kept))
check("已追加重启说明且状态置为 failed",
      any("服务重启" in x for x in kept) and row["status"] == "failed")

# ============================================================ D/E 意图解析
print("\n[D] 意图解析：不得产出倒置的时间区间")
r1 = _summarize({"task_type": "ndvi", "region": "北京市", "start_date": "2026-03-01"}, "test")
f1 = r1["fields"]
check("只给 start 时区间仍然有序", f1["start_date"] <= f1["end_date"],
      f'{f1["start_date"]} ~ {f1["end_date"]}')
r2 = _summarize({"task_type": "ndvi", "region": "北京市",
                 "start_date": "2025-01-01", "end_date": "2025-12-31"}, "test")
check("合法区间不被改动（避免修出新问题）",
      r2["fields"]["start_date"] == "2025-01-01"
      and r2["fields"]["end_date"] == "2025-12-31")

print("\n[E] 意图解析：枚举外的 task_type 不得判成 complete")
r3 = _summarize({"task_type": "vegetation", "region": "北京市"}, "test")
check("非法 task_type → complete=False 且 task_type 置空",
      r3["complete"] is False and r3["fields"]["task_type"] is None,
      f'complete={r3["complete"]} task_type={r3["fields"]["task_type"]}')
r4 = _summarize({"task_type": "ndvi", "region": "北京市",
                 "start_date": "2025-01-01", "end_date": "2025-12-31"}, "test")
check("合法 task_type 仍判 complete（别把正常路径堵死）", r4["complete"] is True)

# ============================================================ F 瓦片重试封顶
print("\n[F] 瓦片代理：尾部延迟必须封顶（否则同步端点占满线程池）")
worst = tile_proxy.TILE_RETRY_ATTEMPTS * tile_proxy.TILE_TIMEOUT
check("单张瓦片最坏耗时 < 60s", worst < 60, f"约 {worst}s")
check("适配器层已关闭重试（避免内外两层叠乘）", retry_policy(0).total == 0,
      f"adapter total={retry_policy(0).total}")
check("外层重试次数 <= 3", tile_proxy.TILE_RETRY_ATTEMPTS <= 3,
      f"attempts={tile_proxy.TILE_RETRY_ATTEMPTS}")

# ============================================================ G 图层警告可见
print("\n[G] 沙箱：图层生成失败必须可见（不得静默报成功）")
from app.execution.sandbox import run_in_sandbox  # noqa: E402

res = run_in_sandbox(
    "import ee\nWB.note('模拟图层生成失败：getMapId 超限')\nWB.stat('面积', 1.23)\nprint('done')\n",
    {"region": "太湖流域", "start_date": "2025-01-01", "end_date": "2025-12-31"},
    mock=True,
)
check("mock 执行成功", res.ok is True)
check("stats 仍然贯通（别把统计契约弄坏）", res.stats.get("面积") == 1.23, str(res.stats))
check("图层警告已进入用户可见 stdout", "图层警告" in res.stdout, repr(res.stdout[:120]))
check("原有 print 输出未丢失", "done" in res.stdout)

# ============================================================ H 前端源码守卫
print("\n[H] 前端源码守卫（前端整块无测试框架，这里做最小静态断言）")
_SRC = _BACKEND.parent / "frontend" / "src"


def _js_code(name: str) -> str:
    """剥掉 `//` 行注释后再扫。

    为什么必须剥：修复代码的**注释里会引用旧写法/旧坐标**（本项目踩过 ——
    直接扫原文会把正确代码判成违规）。这里虽然都是"必须存在"型断言，
    剥注释仍能避免注释里的示例让断言假通过。
    """
    p = _SRC / name
    if not p.exists():
        return ""
    return "\n".join(ln.split("//", 1)[0] for ln in p.read_text(encoding="utf-8").splitlines())


_MAP = _js_code("MapPanel.jsx")
_APP = _js_code("App.jsx")
_CHART = _js_code("ChartPanel.jsx")

check("MapPanel 监听 tileerror（瓦片 400/链接过期不再静默）",
      "tileerror" in _MAP and "noteTileError" in _MAP)
check("MapPanel 卸载时销毁 Leaflet 实例（防地图泄漏）",
      "mapRef.current.remove()" in _MAP)
check("MapPanel 用内容指纹而非「有没有 geojson」",
      "geoFingerprint" in _MAP)
check("ChartPanel 饼图分支对 chart.series 兜底（防整栏白屏）",
      "chart.series || []" in _CHART)
check("App 轮询回调认领任务 id（防结果错位）",
      "activeTaskIdRef.current !== id" in _APP)
check("App 轮询失败有兜底（防转圈永不消失）",
      "任务状态多次刷新失败" in _APP)
check("App 有 chat 并发闸门", "chatBusyRef" in _APP)
check("App 登出会清空对话（防跨用户残留）",
      "setConvo([])" in _APP)

# ============================================================ I 环境路径与汇总格式守卫
print("\n[I] 环境路径守卫（不许把「当时的机器状态」写死进代码）")

# ⚠ 为什么守这个（2026-09-28 实测的真缺陷）：
#   两个无障碍套件 + 三个截图脚本（_shot_ui / _shot_focus / _shot_result）
#   原先都把浏览器路径写死成 `Application\153.0.4234.48\msedge.exe`。
#   Edge 自动更新后该目录被改名为 `153.0.4234.48.deleting`，于是
#   `subprocess.Popen` 直接 FileNotFoundError —— **不是校验失败，是整套无障碍/
#   截图校验根本没跑起来**；而那两个套件只有显式 `--with-a11y` 才执行，
#   所以"校验层已整体不可用"这件事长期无人发现（跑的人只看到一句"没解析到结果行"）。
#   这与 test_no_stale_year.py 守的是**同一类错误**：把当时有效的环境值固化进代码。
#   ⚠ 下面这行注释**故意不写成带引号的路径**，否则会被下面自己的扫描命中。
#   反面样本（不要照抄）：C:\Program Files (x86)\Microsoft\Edge\Application\153.0.4234.48\msedge.exe
_VER_PIN = re.compile(
    r"""["'][A-Za-z]:[\\/][^"'\n]*?[\\/]\d+\.\d+\.\d+(?:\.\d+)?[\\/][^"'\n]*?\.exe["']"""
)
# 扫**全部** tools/*.py，不只那两个套件：同一批硬编码实测散落在 5 个文件里，
# 只守其中两个就是典型的"补一个点、漏一个面"。
# 排除本文件自身（上面那段注释要引用反面样本）。
_scan_targets = sorted(
    p for p in _HERE.glob("*.py") if p.name != pathlib.Path(__file__).name
)
check("tools/ 下有可扫描的 python 文件（防 glob 落空导致空断言假绿）",
      len(_scan_targets) >= 5, f"扫到 {len(_scan_targets)} 个")
_bad_paths = []
for _p in _scan_targets:
    _hits = _VER_PIN.findall(_p.read_text(encoding="utf-8"))
    if _hits:
        _bad_paths.append(f"{_p.name}: {_hits[0]}")
check("没有任何 tools/*.py 写死带版本号的浏览器绝对路径（自动更新后会失效）",
      not _bad_paths, "；".join(_bad_paths[:3]))

for _name in ("test_a11y_contrast.py", "test_a11y_kbd.py",
              "_shot_ui.py", "_shot_focus.py", "_shot_result.py"):
    _src = (_HERE / _name).read_text(encoding="utf-8")
    check(f"{_name} 用 find_edge() 动态解析浏览器", "find_edge()" in _src)

_bro = (_HERE / "browser_util.py").read_text(encoding="utf-8")
check("浏览器定位器存在且会跳过 .deleting 残留目录",
      "def find_edge" in _bro and "deleting" in _bro)

# 汇总格式：shell 套件用「N 通过，M 失败」，解析器必须认，否则 `--http` 那三个
# 套件即使零失败也会被判成 ❌（**假红**），汇总直接变成"3 个套件没全绿"。
_ra = (_HERE / "regress_all.sh").read_text(encoding="utf-8")
check("regress_all 解析器含 shell 套件的「N 通过」正则",
      "结果：[0-9]+ *通过" in _ra)
check("regress_all 解析器含「M 失败」正则与跳过通道",
      "[0-9]+ *失败" in _ra and "跳过：" in _ra)


def _parse_summary(text: str) -> str:
    """复刻 regress_all.sh 的解析规则，用行为断言钉住三种格式。"""
    m = re.findall(r"(\d+)/(\d+) *通过", text)
    if m:
        return f"{m[-1][0]}/{m[-1][1]} 通过"
    ok = re.findall(r"结果：(\d+) *通过", text)
    fail = re.findall(r"(\d+) *失败", text)
    if ok and fail:
        return f"{ok[-1]}/{int(ok[-1]) + int(fail[-1])} 通过"
    return ""


check("认 标准格式「N/M 通过」",
      _parse_summary("结果：120/120 通过") == "120/120 通过")
check("认 auth_http 的「N 通过，M 失败，K 跳过」",
      _parse_summary("结果：41 通过，0 失败，2 跳过") == "41/41 通过")
check("认 isolation/quota 的「N 通过 / M 失败」",
      _parse_summary("结果：49 通过 / 0 失败") == "49/49 通过")
check("有失败时不得算成全绿（防假绿）",
      _parse_summary("结果：40 通过 / 2 失败") == "40/42 通过")

# ============================================================ J 依赖可提交性守卫
print("\n[J] 依赖可提交性（脚本依赖的本地模块必须真的进得了版本库）")

# ⚠️ 为什么守这个（2026-09-28 实测，**提交前一刻才发现**的坑）：
#   `.gitignore` 里有一条 `backend/tools/_*.py`（注释写的是"一次性排查脚本"），
#   而我新加的共享模块原本叫 `_browser.py` → **被静默忽略**。
#   本机一切正常（文件就在磁盘上），但提交时它压根不会被 add →
#   **新鲜克隆上 a11y 与截图脚本直接 ModuleNotFoundError**。
#   即"验证工具依赖了一个进不了版本库的文件"，与 T1 同一族：坏得很安静，
#   而且**本地自测永远发现不了**。
#   教训：**共享模块不要用 `_` 前缀** —— 那个前缀在本仓库意味着"可丢弃的脚手架"。
#   （顺带发现：`_env.py` / `_pub_e2e.py` / `_shot_*.py` 能被跟踪，只是因为
#     那条规则加得比它们晚，已跟踪文件不受新增规则影响 —— 属于历史侥幸。）


def _is_ignored(p: pathlib.Path):
    """True=被 .gitignore 命中（提交时会漏掉）；False=可提交；None=本机无 git。"""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(p)],
                           cwd=str(_BACKEND.parent), capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    # git check-ignore 约定：0=命中忽略规则，1=未命中，其它=出错（如不在仓库里）
    return True if r.returncode == 0 else (False if r.returncode == 1 else None)


_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", re.M
)
_dep_checked, _dep_unknown, _dep_bad = 0, 0, []
for _p in _scan_targets:
    for _m in _IMPORT_RE.finditer(_p.read_text(encoding="utf-8")):
        _mod = _m.group(1) or _m.group(2)
        _dep = _HERE / f"{_mod}.py"
        if not _dep.is_file():
            continue  # 非本地模块（标准库 / 第三方）
        _dep_checked += 1
        _ig = _is_ignored(_dep)
        if _ig is None:
            _dep_unknown += 1
        elif _ig:
            _dep_bad.append(f"{_p.name} → {_dep.name}")

check("tools/ 内部依赖已实际检查（防正则落空导致空断言假绿）",
      _dep_checked >= 3, f"检查了 {_dep_checked} 处")
check("没有脚本依赖被 .gitignore 忽略的本地模块（否则新鲜克隆上直接 ImportError）",
      not _dep_bad, "；".join(_dep_bad[:3]))
if _dep_unknown:
    print(f"  ⚠ 有 {_dep_unknown} 处依赖无法判定（本机 git 不可用），未计入断言")

# ============================================================ 汇总
print()
print("=" * 66)
total = PASS + FAIL
print(f"结果：{PASS}/{total} 通过")
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
