"""「地图定位到错误地区」回归守卫：防止非内置地名再次被静默定位到太湖/苏州。

## 为什么需要这个测试

2026-09-23 用户报告：「问恩施大峡谷的 NDVI，地图显示的却是苏州」。

排查结论是：**后端数据完全正确，错的是前端地图定位**。

链路（每一环看起来都人畜无害）：

  1. `恩施大峡谷` 不在内置区域库（regions.py 只有 13 个地名）里；
  2. 前端 `App.jsx: regionCenter(name)` 按**精确名字**查区域字典 → 返回 null；
  3. `MapPanel.jsx` 的兜底是**硬编码的太湖坐标** `setView([31.2, 120.1], 9)`；
  4. 纯栅格图层（只有 tile_url、没有 geojson）算不出 bounds，
     所以没有任何东西能把视口纠正回来。

结果：底图停在苏州，而恩施的瓦片在 **1043 km 外**（约 7.3 个视口宽），
用户完全看不到自己的图层，只看到一张苏州地图。

## 为什么这类 bug 特别危险

它**不报错**。任务状态是 succeeded，统计数字也对，只是地图把用户带到了
1000 公里外的另一个省。用户不会觉得"功能坏了"，而会觉得"这个产品在胡说八道"。
**错误的自信比明确的失败更糟** —— 所以修复的核心不只是"让恩施能定位"，
而是"定位不了的时候必须说出来"。

## 检查项

  A. 后端确实会回传图层地理范围（bbox），覆盖真实 GEE 与离线两条链路
  B. bbox 解析函数对各类 GeoJSON 都正确（且对坏输入不抛异常）
  C. `恩施大峡谷` 这类非内置地名：兜底代码不再落到太湖，且会显式告警
  D. 前端源码里**不存在**硬编码的具体地点坐标作为地图兜底
  E. MapPanel 的定位优先级是 bbox → 区域中心 → 中性全局视图（而不是猜一个地方）
  F. 提示词要求 AOI 变量必须叫 `aoi`（bbox 捕获的依赖）

用法（在 backend 目录下）：
    .venv\\\\Scripts\\\\python.exe tools\\\\test_region_map_fallback.py
"""

import ast
import os
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../backend/tools
BACKEND = HERE.parent                            # .../backend
FRONTEND_SRC = BACKEND.parent / "frontend" / "src"

sys.path.insert(0, str(BACKEND))

ok: list[bool] = []


def check(name, cond, extra=""):
    ok.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------------------
# A. 后端会回传 bbox
# ---------------------------------------------------------------------------
print("A. 后端图层地理范围（bbox）回传")

from app.models import LayerData  # noqa: E402

ld = LayerData(name="x", kind="tile", tile_url="https://x/{z}/{x}/{y}",
               bbox=[109.05, 30.65, 109.25, 30.85])
dump = ld.model_dump()
check("LayerData 有 bbox 字段", "bbox" in LayerData.model_fields)
check("bbox 能随 model_dump 序列化（前端才拿得到）", dump.get("bbox") == [109.05, 30.65, 109.25, 30.85],
      dump.get("bbox"))

from app.execution._runner import (  # noqa: E402
    Reporter, _geojson_bbox, _polygon_bbox,
)

r = Reporter()
item = r.add_layer("tile 图层", tile_url="https://x/{z}/{x}/{y}", legend=[])
check("无 AOI 时不硬塞 bbox", "bbox" not in item)
r.aoi_bbox = [109.05, 30.65, 109.25, 30.85]
item2 = r.add_layer("tile 图层2", tile_url="https://x/{z}/{x}/{y}", legend=[])
check("有 AOI 时栅格图层自动挂上 bbox", item2.get("bbox") == [109.05, 30.65, 109.25, 30.85],
      item2.get("bbox"))
item3 = r.add_layer("矢量图层", geojson={"type": "Feature", "geometry": None, "properties": {}}, legend=[])
check("矢量图层不吃 bbox（它自己有坐标）", "bbox" not in item3)

# gee.py / offline.py 两个后端都要传递 bbox
gee_src = (BACKEND / "app" / "execution" / "gee.py").read_text(encoding="utf-8")
check("gee.py 把 bbox 传进 LayerData", "bbox=l.get(\"bbox\")" in gee_src)
off_src = (BACKEND / "app" / "execution" / "offline.py").read_text(encoding="utf-8")
check("offline.py 定义了 _bbox 并填充图层", "_bbox(cx, cy)" in off_src and "def _bbox" in off_src)
check("offline.py 四处图层都带上 bbox", off_src.count("bbox=_bbox(cx, cy)") >= 4,
      f"{off_src.count('bbox=_bbox(cx, cy)')} 处")

# ---------------------------------------------------------------------------
# B. bbox 解析正确性
# ---------------------------------------------------------------------------
print("\nB. bbox 解析函数")

check("Polygon", _polygon_bbox([[[120, 31], [120.3, 31], [120.3, 31.3], [120, 31.3]]]) == [120.0, 31.0, 120.3, 31.3],
      _polygon_bbox([[[120, 31], [120.3, 31], [120.3, 31.3], [120, 31.3]]]))
check("Point", _polygon_bbox([120, 31]) == [120.0, 31.0, 120.0, 31.0])
check("FeatureCollection",
      _geojson_bbox({"type": "FeatureCollection", "features": [
          {"type": "Feature", "geometry": {"type": "Polygon",
                                           "coordinates": [[[109, 30], [110, 30], [110, 31], [109, 31]]]},
           "properties": {}}]}) == [109.0, 30.0, 110.0, 31.0])
check("MultiPolygon",
      _geojson_bbox({"type": "MultiPolygon", "coordinates": [
          [[[109, 30], [109.1, 30], [109.1, 30.1]]],
          [[[109.3, 30.3], [109.4, 30.3], [109.4, 30.4]]]]}) == [109.0, 30.0, 109.4, 30.4])
check("GeometryCollection",
      _geojson_bbox({"type": "GeometryCollection", "geometries": [
          {"type": "Point", "coordinates": [100, 20]},
          {"type": "Point", "coordinates": [110, 30]}]}) == [100.0, 20.0, 110.0, 30.0])
# 坏输入必须安全返回 None，而不是把整个任务带崩
for bad_name, bad in [("None", None), ("空 dict", {}), ("未知 type", {"type": "Xyz"}),
                      ("空 coords", {"type": "Polygon", "coordinates": []}),
                      ("坐标非数值", {"type": "Polygon", "coordinates": ["a", "b"]})]:
    try:
        res = _geojson_bbox(bad)
        check(f"坏输入不抛异常（{bad_name}）", res is None, res)
    except Exception as e:  # noqa: BLE001
        check(f"坏输入不抛异常（{bad_name}）", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
# C. 非内置地名的兜底行为
# ---------------------------------------------------------------------------
print("\nC. 非内置地名的兜底行为")

from app.codegen import _fallback_code  # noqa: E402
from app.models import AnalysisRequest, TaskType  # noqa: E402
from app.regions import get_center  # noqa: E402

TAIHU = (120.13, 31.20)

def strip_py_comments(src: str) -> str:
    """去掉 Python 注释与文档字符串，只留可执行代码。

    ⚠ 必须去注释再扫坐标：本次修复的**说明性注释里就会提到**旧坐标
    （"原先是 `or (120.13, 31.20)` = 太湖"），不去掉的话，
    "不再有硬编码"这条断言会被自己写的注释判成失败 —— 那是假失败。
    这里用 ast 解析拿真实的字符串/注释边界，比正则可靠。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    drop: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # 文档字符串（表达式语句形式的字符串常量）整段丢
            if getattr(node, "lineno", None):
                drop.append((node.lineno, node.end_lineno or node.lineno))
    lines = src.splitlines()
    for a, b in drop:
        for i in range(a, min(b + 1, len(lines) + 1)):
            lines[i - 1] = ""
    # 行注释（简单切分；字符串里的 # 极少出现在本项目的坐标行里）
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in lines)


def strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


# ---------------------------------------------------------------------------
# C2. 区域库的覆盖度与匹配正确性（2026-09-24 扩容后）
# ---------------------------------------------------------------------------
# 背景：用户提的诉求是「不管问哪个区域都能正确回答」。
# 靠枚举不可能覆盖所有地名，但**库的厚度与匹配的正确性**必须钉住：
# 库薄了 → 大量地名落到全国兜底；匹配错了 → 比查不到更糟（答案是错的）。
print("\nC2. 区域库覆盖与匹配")

from app.gazetteer import GAZETTEER, CHINA_BBOX as GZ_BBOX, lookup as gz_lookup  # noqa: E402
from app.regions import REGION_LIBRARY, get_half, get_region, normalize, to_bbox  # noqa: E402

check("区域库规模足够（≥300 条）", len(REGION_LIBRARY) >= 300, len(REGION_LIBRARY))
check("坐标簿规模足够（≥350 条：省级 + 地级市）", len(GAZETTEER) >= 350, len(GAZETTEER))

# 坐标合理性：全部落在中国疆域包围盒内（含南海，南界 3.5°N）
_bad_coord = [
    r["name"] for r in REGION_LIBRARY
    if not (GZ_BBOX[0] <= r["lon"] <= GZ_BBOX[2] and GZ_BBOX[1] <= r["lat"] <= GZ_BBOX[3])
]
check("区域库坐标全在中国疆域内", not _bad_coord, _bad_coord[:5])
_bad_gz = [
    n for n, lo, la, _ in GAZETTEER
    if not (GZ_BBOX[0] <= lo <= GZ_BBOX[2] and GZ_BBOX[1] <= la <= GZ_BBOX[3])
]
check("坐标簿坐标全在中国疆域内", not _bad_gz, _bad_gz[:5])

# half 合理性：太小则 AOI 框不住目标，太大则统计被稀释
_bad_half = [(r["name"], r["half"]) for r in REGION_LIBRARY
             if not (0.03 <= r["half"] <= 2.1)]
check("区域库 half 全在合理区间 [0.03, 2.1]", not _bad_half, _bad_half[:5])
check("每条的 half 字段都存在", all("half" in r for r in REGION_LIBRARY))

# 正式名不得重复（重复会导致排序不稳定、匹配结果随实现细节漂移）
_dups = [k for k, v in Counter(r["name"] for r in REGION_LIBRARY).items() if v > 1]
check("区域库无重复正式名", not _dups, _dups[:5])

# --- 排序键必须用「所有名称（含别名）的最大长度」，而不是正式名长度 ---
# 这是个**真实踩过的坑**：排序只看正式名时，"短别名 + 长正式名"的组合会错位。
#   反例：`五大连池`(正式名 4) vs `大连市`(别名 `大连` 2)。
#   若排序键用正式名长度，五大连池侥幸排在前面；但只要新增一条
#   "正式名 2 字、别名 8 字" 的记录，它的长别名就永远抢不到匹配。
from app.regions import _sort_len  # noqa: E402
# 取的是**所有名称里最长的**，所以"大连市"(3) vs 别名"大连"(2) → 3
check("排序键 = 所有名称（含别名）的最大长度",
      _sort_len({"name": "大连市", "aliases": ["大连"]}) == 3
      and _sort_len({"name": "太湖流域", "aliases": ["太湖"]}) == 4
      and _sort_len({"name": "吉林市", "aliases": ["吉林"]}) == 3)
check("排序确实按 _sort_len 降序",
      all(_sort_len(REGION_LIBRARY[i]) >= _sort_len(REGION_LIBRARY[i + 1])
          for i in range(len(REGION_LIBRARY) - 1)))

# --- 关键歧义对：长短名共存时，长名必须赢，且短名单独出现时也要能命中 ---
# 这些组合都是实测过的真实翻车点，不是构造出来的
_AMBIG = [
    ("太湖流域", "太湖"), ("长江三角洲", "长三角"), ("四川盆地", "四川"),
    ("青海湖流域", "青海湖"), ("五大连池", "大连"), ("巴丹吉林沙漠", "吉林"),
    ("九华山", "华山"), ("黄河入海口", "海口"), ("漠河北极村", "河北"),
    ("中山陵", "中山"), ("长白山天池", "长白山"), ("天山天池", "天山"),
    ("太行山大峡谷", "太行山"), ("莲花山森林公园", "莲花山"),
    ("塔克拉玛干沙漠公路", "塔克拉玛干沙漠"), ("内蒙古高原", "内蒙古"),
]
_bad_ambig = []
for long_name, short_name in _AMBIG:
    rl, rs = get_region(long_name), get_region(short_name)
    # 长名必须解析到"包含 long_name 的那条记录"
    ok_long = rl is not None and (
        long_name == rl["name"] or long_name in rl.get("aliases", [])
        or rl["name"] in long_name)
    # 短名必须解析到"包含 short_name 的那条记录"
    ok_short = rs is not None and (
        short_name == rs["name"] or short_name in rs.get("aliases", [])
        or rs["name"] in short_name)
    if not (ok_long and ok_short):
        _bad_ambig.append((long_name, rl["name"] if rl else None,
                           short_name, rs["name"] if rs else None))
check("长短名歧义对全部正确（长名优先、短名独立可查）",
      not _bad_ambig, _bad_ambig[:5])

# --- 名称标准化 ---
check("标准化处理全角空格", normalize("恩施\u3000大峡谷") == normalize("恩施大峡谷"))
check("标准化处理半角空格", normalize(" 恩施 大峡谷 ") == normalize("恩施大峡谷"))
check("标准化不破坏中文", normalize("太湖流域") == "太湖流域")

# --- 曾经案例必须能解析（回归锚点） ---
_MUST_RESOLVE = [
    "恩施大峡谷", "九寨沟", "黄山", "泰山", "三亚", "莫干山", "洱海",
    "太湖流域", "深圳", "苏州", "南京", "六盘水市", "神农架", "张家界",
    "稻城亚丁", "可可西里", "三江源", "青海湖", "长白山", "武夷山",
    "西双版纳", "香格里拉", "阿尔山", "额济纳", "崆峒山", "云台山",
    # 2026-09-24 覆盖率探针实测出的缺口，已补齐
    "泸沽湖", "民勤县", "塔里木河",
]
_unresolved = [n for n in _MUST_RESOLVE if get_region(n) is None]
check("曾在 bug 里出现/易错的地名全部可解析", not _unresolved, _unresolved)

# --- 2026-09-24 补的 3 条：正式名与别名都要能查到 ---
# 为什么要单独断言别名：这 3 条正是"用户这么问却定位不了"的缺口，
# 而用户问的写法往往带后缀（「泸沽湖景区」「塔里木河流域」「民勤」）。
for _alias in ("泸沽湖", "泸沽湖景区", "民勤", "民勤县",
               "塔里木河", "塔里木河流域", "塔里木河干流"):
    check(f"新增条目别名可解析：{_alias}", get_region(_alias) is not None)
check("塔里木河 不得被 塔里木盆地 抢走",
      (get_region("塔里木河流域") or {}).get("name") == "塔里木河",
      (get_region("塔里木河流域") or {}).get("name"))
check("塔里木河 的范围小于塔里木盆地（河≠盆地）",
      get_half("塔里木河") < get_half("塔里木盆地"),
      f"{get_half('塔里木河')} vs {get_half('塔里木盆地')}")

# --- 库外名称必须**明确返回 None**，不能猜一个坐标 ---
# ⚠ 选例子要挑**真的不在任何表里**的 —— 坐标簿收录了 470+ 条省市县，
#    原先拿来当"库外"例子的「阳朔县」现在已被收录（更好了），
#    继续拿它断言 None 就成了假失败。
for _unk in ("瓦坎达", "刚铎", "某某新城", ""):
    check(f"库外名称返回 None（{_unk or '空串'}）", get_region(_unk) is None)

check("to_bbox 对已知区域返回正确格式",
      (lambda b: isinstance(b, list) and len(b) == 4 and b[0] < b[2] and b[1] < b[3])(
          to_bbox("恩施大峡谷")))
check("to_bbox 对未知区域返回 None（不猜）", to_bbox("瓦坎达") is None)
check("get_half 已知区域取自己的值", get_half("长江流域") > get_half("恩施大峡谷"),
      f"{get_half('长江流域')} vs {get_half('恩施大峡谷')}")

# --- 坐标簿解析 ---
check("坐标簿能解析纯地级市", gz_lookup("六盘水市的植被") is not None)
check("坐标簿解析不到时返回 None", gz_lookup("瓦坎达") is None)

check("恩施大峡谷 现在**在**内置区域库（原 bug 前提已被消除）",
      get_region("恩施大峡谷") is not None)
check("苏州 现在也在内置区域库", get_region("苏州") is not None)
check("太湖流域 仍是内置的（对照组）", get_center("太湖流域") == TAIHU)

req_enshi = AnalysisRequest(task_type=TaskType.ndvi, region="恩施大峡谷")
code_enshi = _fallback_code(req_enshi)
try:
    compile(code_enshi, "<enshi>", "exec")
    check("恩施兜底代码语法正确", True)
except SyntaxError as e:
    check("恩施兜底代码语法正确", False, str(e))

# 关键断言：不能再出现太湖坐标
lon_t, lat_t = TAIHU
taihu_literal = f"{lon_t - 0.25:.4f}, {lat_t - 0.25:.4f}"
check("恩施兜底代码不再包含太湖坐标",
      taihu_literal not in code_enshi and f"({lon_t}, {lat_t})" not in code_enshi,
      f"仍在用 {taihu_literal}" if taihu_literal in code_enshi else "已清除")
# 范围应随区域大小自适应，而不是一律 0.5°
check("恩施兜底 AOI 用区域自己的 half（0.20 → ±0.2）",
      "109.2800, 30.0800, 109.6800, 30.4800" in code_enshi)
_code_da = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="长江流域"))
check("大区域（长江流域）用大范围而不是 0.5°",
      "110.0000, 28.5000, 114.0000, 32.5000" in _code_da)

# 对照：内置区域仍应解析出它自己的中心，而不是被无差别改成全国范围
# ⚠ 断言要跟着 `half` 走，不能写死 0.25 —— 扩容后太湖流域的 half 是 0.5、
#    北京市是 0.5（直辖市），写死 0.25 会变成假失败。
_taihu_half = get_half("太湖流域")
code_taihu = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="太湖流域"))
check("太湖流域仍用自己的中心坐标（对照组未被误伤）",
      f"{lon_t - _taihu_half:.4f}, {lat_t - _taihu_half:.4f}" in code_taihu)
check("太湖流域的 AOI 不被降级成全国范围",
      "73.5, 18.0, 135.0, 53.5" not in code_taihu)
_bj_half = get_half("北京市")
code_bj = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="北京市"))
check("北京市用 116.4/39.9 而不是太湖",
      f"{116.40 - _bj_half:.4f}, {39.90 - _bj_half:.4f}" in code_bj
      and "119.8800" not in code_bj)

# 库外地名：必须显式告警 + 全国范围
code_unk = _fallback_code(AnalysisRequest(task_type=TaskType.ndvi, region="瓦坎达"))
check("库外地名兜底用全国范围", "73.5, 18.0, 135.0, 53.5" in code_unk)
check("库外地名兜底显式告警「不在内置区域库」", "不在内置区域库" in code_unk)


# codegen.py 里不应再有硬编码的地点兜底常量
cg_src_raw = (BACKEND / "app" / "codegen.py").read_text(encoding="utf-8")
cg_src = strip_py_comments(cg_src_raw)
check("codegen.py 不再有 `or (120.13, 31.20)` 式硬编码", "or (120.13, 31.20)" not in cg_src)
check("codegen.py 不再有 get_center(...) or <常量> 的写法",
      not re.search(r"get_center\([^)]*\)\s*or\s*\(", cg_src))
# 兜底常量绝不能是具体地点：检查是否还有"取不到中心就退回某个固定经纬度"
check("codegen.py 里没有其它写死的地点坐标兜底",
      not re.search(r"or\s*\(\s*1[0-9]{2}\.\d+\s*,\s*[0-9]{2}\.\d+\s*\)", cg_src),
      "仍有 or (经度, 纬度) 形式的兜底")

# ---------------------------------------------------------------------------
# D. 前端不得再有硬编码地点兜底
# ---------------------------------------------------------------------------
print("\nD. 前端地图兜底不得写死具体地点")

mp = FRONTEND_SRC / "MapPanel.jsx"
app_jsx = FRONTEND_SRC / "App.jsx"
check("MapPanel.jsx 存在", mp.exists())
check("App.jsx 存在", app_jsx.exists())

mp_src_raw = mp.read_text(encoding="utf-8") if mp.exists() else ""
# 同样先去注释：修复说明里会提到旧的太湖坐标，否则会把注释判成代码
mp_src = strip_js_comments(mp_src_raw)

# 太湖/苏州一带的坐标不该作为兜底出现
check("MapPanel 不再硬编码 [31.2, 120.1]（太湖）",
      "31.2, 120.1" not in mp_src and "[31.2, 120.1]" not in mp_src)
check("MapPanel 代码里不再出现 120.1（太湖经度）", "120.1" not in mp_src,
      "仍出现 120.1" if "120.1" in mp_src else "已清除")
check("MapPanel 代码里不再出现 31.2（太湖纬度）", "31.2" not in mp_src,
      "仍出现 31.2" if "31.2" in mp_src else "已清除")

# 兜底应是中性的全局视图
check("MapPanel 有中性全局视图兜底 [35, 105]",
      "[35, 105]" in mp_src)

# 必须存在"定位失败要说出来"的路径
check("MapPanel 有 locWarn 状态（定位失败提示）", "locWarn" in mp_src_raw)
check("MapPanel 渲染定位失败提示文案", "未能确定该图层的地理位置" in mp_src_raw)

# 定位优先级：bbox 必须参与，且优先于区域中心
check("computeBounds 会读 layer.bbox", "l.bbox" in mp_src)
check("MapPanel 有 firstBbox 兜底取值", "function firstBbox" in mp_src)

# ---------------------------------------------------------------------------
# D2. effect 依赖必须稳定 —— 否则定位会被空态渲染覆盖回全局视图
# ---------------------------------------------------------------------------
# 2026-09-23 实测到的**真实回归**（不是理论担忧）：
#   `layers` 由父组件每次渲染新建数组，引用恒变；直接写进 useEffect 依赖会让
#   效果反复重跑，而重跑常发生在"父组件先渲空态、再渲结果"的时序里 ——
#   第二次运行拿着空图层把刚 fitBounds 好的视野重新打回 [35,105]，
#   并弹出"定位不了"。修复（回填 bbox）因此被静默抵消。
#   所以这里把"依赖必须是内容指纹而非数组引用"钉住。
check("MapPanel 用 layerKey（内容指纹）做 effect 依赖",
      "layerKey" in mp_src and "JSON.stringify" in mp_src)
check("effect 依赖不再是裸的 layers 数组",
      not re.search(r"\},\s*\[\s*layers\s*,", mp_src),
      "仍以 layers 作为 effect 依赖")

# 提示文案必须区分"有没有范围信息"，否则会误导用户（实测踩到）
check("定位失败文案区分了有无范围信息",
      "未记录分析区域范围" in mp_src_raw and "hasRange" in mp_src,
      "文案仍是单一写法")

# ---------------------------------------------------------------------------
# E. 提示词要求 AOI 变量名
# ---------------------------------------------------------------------------
print("\nE. 提示词要求 AOI 变量名为 aoi")

check("提示词明确要求 AOI 变量叫 aoi",
      "AOI 变量名必须是 `aoi`" in cg_src_raw or "AOI 变量名必须是" in cg_src_raw)
# 沙箱侧要有别名兜底，避免模型偶尔换名就彻底丢失定位
run_src = (BACKEND / "app" / "execution" / "_runner.py").read_text(encoding="utf-8")
check("沙箱对 AOI 变量名有别名兜底",
      all(k in run_src for k in ("aoi_rect", "roi")))

# ---------------------------------------------------------------------------
# E2. 沙箱内运行时区域解析（WB.resolve_region）
# ---------------------------------------------------------------------------
# 这是「不管问哪个区域」诉求的真正兜底：库不可能覆盖所有地名，
# 所以给生成代码一个**运行时**解析入口，三层依次回落。
print("\nE2. 沙箱内运行时区域解析")

check("Reporter 提供 resolve_region", "def resolve_region" in run_src)
check("Reporter 提供 resolve_region_bbox", "def resolve_region_bbox" in run_src)
check("解析器有 default_region（不传参也能工作）",
      "default_region" in run_src)
check("main() 把请求的 region 注入报告器",
      "wb.default_region = str(request.get(\"region\"" in run_src)
check("解析器三层回落（区域库 → 坐标簿 → None）",
      "_resolve" in run_src and "get_region" in run_src and "_gz_lookup" in run_src)
# 提示词必须告诉模型这个入口存在，否则模型不会调用
check("提示词告知模型可用 WB.resolve_region",
      "WB.resolve_region" in cg_src_raw)
check("提示词禁止模型自行编造经纬度",
      "不要自行猜测经纬度" in cg_src_raw or "不许自己编经纬度" in cg_src_raw)

# ---------------------------------------------------------------------------
# G. 意图解析的规则兜底：地名必须抽得出来、且不能抽错
# ---------------------------------------------------------------------------
# 「问恩施大峡谷却被反问'分析哪个区域'」和「地名里粘着动词」
# 都会让用户看到一次失败或错误的分析，属于同一类体验缺陷。
print("\nG. 意图解析的地名抽取")

from app.intent import _rule_parse  # noqa: E402

# (输入, 期望 region)  —— None 表示"抽不出来，应该去追问用户"
_INTENT_CASES = [
    ("恩施大峡谷的植被情况怎么样", "恩施大峡谷"),
    ("帮我看看九寨沟的植被覆盖", "九寨沟"),
    ("黄山的植被长势如何", "黄山"),
    ("泰山上的植被怎么样", "泰山"),
    ("分析一下张家界的植被", "张家界"),
    ("洱海的水体面积", "洱海"),
    ("三亚的植被", "三亚"),
    ("莫干山的森林覆盖", "莫干山"),
    ("看看鄱阳湖", "鄱阳湖"),          # 动词不能粘进地名
    ("太湖流域的植被", "太湖流域"),      # 长名不能被"太湖"抢走
    ("六盘水市的植被", "六盘水市"),      # 带后缀写法原样保留（坐标簿已收录）
    ("长江流域的变化", "长江流域"),
    ("分析一下新疆的植被", "新疆"),
    ("秦岭的森林覆盖", "秦岭"),
    ("深圳的植被", "深圳"),
    ("苏州的水体", "苏州"),
    # ↓ 这些**不是地名**，必须返回 None（宁可追问，也不要拿假地名去跑任务）
    ("今天天气不错", None),
    ("我想看点东西", None),
    ("请问可以做什么", None),
    ("谢谢", None),
    ("", None),
]
_intent_bad = []
for _text, _want in _INTENT_CASES:
    _got = _rule_parse(_text)["region"]
    if _got != _want:
        _intent_bad.append((_text, _got, _want))
check(f"规则兜底地名抽取全部正确（{len(_INTENT_CASES)} 例）",
      not _intent_bad, _intent_bad[:6])

# 抽出来的地名必须能真的查到坐标 —— 否则等于把"定位不了"往后推了一步
_unlocatable = [
    t for t, w in _INTENT_CASES
    if w and get_region(_rule_parse(t)["region"]) is None
]
check("抽出的地名全部可定位（不会把问题推给下游）", not _unlocatable, _unlocatable)

# 任务类型识别不能因为新增关键词而互相抢
check("「森林覆盖」判为 ndvi 而不是 classification",
      _rule_parse("莫干山的森林覆盖")["task_type"] == "ndvi",
      _rule_parse("莫干山的森林覆盖")["task_type"])
check("「水体面积」仍判为 water",
      _rule_parse("洱海的水体面积")["task_type"] == "water")
check("「变化」仍判为 change_detection",
      _rule_parse("长江流域的变化")["task_type"] == "change_detection")

# 规则层不得引入「猜一个坐标」的兜底
intent_src = (BACKEND / "app" / "intent.py").read_text(encoding="utf-8")
check("intent.py 无硬编码经纬度兜底",
      not re.search(r"=\s*\(\s*1[0-9]{2}\.\d+\s*,\s*[0-3][0-9]\.\d+\s*\)", intent_src))

# ---------------------------------------------------------------------------
# F. 真实 GEE 链路：bbox 真的能带出来（走 mock，不联网）
# ---------------------------------------------------------------------------
print("\nF. 端到端（mock 沙箱）bbox 贯通")

try:
    os.environ["WB_GEE_MOCK"] = "1"
    from app.execution.sandbox import run_in_sandbox

    probe = (
        "import ee\n"
        "aoi = ee.Geometry.Rectangle([109.05, 30.65, 109.25, 30.85])\n"
        "WB.add_image_layer('恩施大峡谷 NDVI', ee.Image(0.5), {'min':0,'max':1})\n"
    )
    res = run_in_sandbox(probe, {"region": "恩施大峡谷", "start_date": "2025-01-01",
                                 "end_date": "2025-12-31", "cloud_threshold": 20})
    check("沙箱执行成功", res.ok, res.error)
    tiles = [l for l in res.layers if l.get("tile_url")]
    check("有栅格图层", bool(tiles), len(res.layers))
    if tiles:
        bb = tiles[0].get("bbox")
        check("栅格图层带上了 bbox", isinstance(bb, list) and len(bb) == 4, bb)
        if isinstance(bb, list) and len(bb) == 4:
            # mock 的 Geometry 是固定坐标，这里只验证"贯通且数值合法"
            check("bbox 数值合法（W<S、S<N、经度在南纬度北的合理区间）",
                  bb[0] < bb[2] and bb[1] < bb[3]
                  and -180 <= bb[0] <= 180 and -90 <= bb[1] <= 90, bb)

    # 运行时解析必须真的能在沙箱里用（这是「不管问哪个区域」的落点）
    probe2 = (
        "import ee\n"
        "_bb = WB.resolve_region_bbox()\n"
        "_c = WB.resolve_region()\n"
        "print('BB=', _bb)\n"
        "print('C=', _c)\n"
        "aoi = ee.Geometry.Rectangle(_bb or [73.5, 18.0, 135.0, 53.5])\n"
        "WB.add_image_layer('t', ee.Image(0.5), {})\n"
    )
    res2 = run_in_sandbox(probe2, {"region": "六盘水市", "start_date": "2025-01-01",
                                   "end_date": "2025-12-31", "cloud_threshold": 20})
    check("沙箱内 WB.resolve_region() 不传参可用", res2.ok, res2.error)
    check("无参调用解析到当前任务的 region（六盘水）",
          "BB= [104.5, 26.24" in (res2.stdout or ""), (res2.stdout or "")[:120])

    res3 = run_in_sandbox(probe2, {"region": "瓦坎达", "start_date": "2025-01-01",
                                   "end_date": "2025-12-31", "cloud_threshold": 20})
    check("库外地名在沙箱内返回 None（不猜坐标）",
          "BB= None" in (res3.stdout or ""), (res3.stdout or "")[:120])
except Exception as e:  # noqa: BLE001
    import traceback
    check("端到端 mock 链路", False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")

print("\nH. AOI 守卫（生成代码的分析范围校正）")

# 背景：提示词里同时有两条关于 AOI 的指令 ——
#   ① 「逐字使用上面注入的矩形」（来自内置区域库，按区域类型给 half）
#   ② 「AOI 必须 ≤0.5°×0.5°」（_RESOURCE_RULES 的资源硬约束）
# 对省域/大流域这两条**互相矛盾**。2026-09-24 随机自检实测：5 个任务里
# 「广州市」把注入的 0.70° 缩成 0.50°（正好是②的上限），中心没变、范围偏小。
# 修法分两半：先把提示词的矛盾讲清楚，再用这道守卫兜住模型的偏离。

from app.codegen import _RESOURCE_RULES, _build_prompt, enforce_aoi  # noqa: E402

# --- 复现实测的那次偏离 ---
_gz_bad = "import ee\naoi = ee.Geometry.Rectangle([113.0100, 22.8800, 113.5100, 23.3800])\n"
_gz_fixed, _gz_note = enforce_aoi(_gz_bad, "广州市")
check("广州：被模型缩小的 AOI 被校正回 0.70°",
      "112.9100, 22.7800, 113.6100, 23.4800" in _gz_fixed, _gz_fixed)
check("校正不是静默的（给出了可读说明）", "校正" in _gz_note, _gz_note)

_gz_ok = "aoi = ee.Geometry.Rectangle([112.9100, 22.7800, 113.6100, 23.4800])\n"
check("已合规的代码一个字都不改", enforce_aoi(_gz_ok, "广州市") == (_gz_ok, ""))

# --- 中心跑偏 = "分析到别处"那一类 bug，必须无条件拽回（守卫的主要价值）---
_shift = "aoi = ee.Geometry.Rectangle([109.28, 30.08, 109.68, 30.48])\n"
_sh_fixed, _sh_note = enforce_aoi(_shift, "梅里雪山")
check("中心被挪到 1000km 外：拽回本区域",
      "98.4500, 28.2500, 98.8500, 28.6500" in _sh_fixed, _sh_fixed)
check("中心跑偏的说明里报了公里数", "km" in _sh_note, _sh_note)

# --- allow_shrink：超时/内存超限时缩小 AOI 是**合法缓解**，不能一律拽回 ---
# 否则省域任务超时后会把 2.6° 强行恢复，再超时一次，永远收敛不了。
_smaller = "aoi = ee.Geometry.Rectangle([98.5500, 28.3500, 98.7500, 28.5500])\n"
check("allow_shrink=True 放行「围绕原中心」的缩小",
      enforce_aoi(_smaller, "梅里雪山", allow_shrink=True) == (_smaller, ""))
check("allow_shrink=False 时同样的缩小会被还原",
      "98.4500, 28.2500, 98.8500, 28.6500" in enforce_aoi(_smaller, "梅里雪山")[0])
_bigger = "aoi = ee.Geometry.Rectangle([97.65, 27.45, 99.65, 29.45])\n"
check("即使 allow_shrink=True 也不许放大（放大只会更慢）",
      "98.4500, 28.2500, 98.8500, 28.6500"
      in enforce_aoi(_bigger, "梅里雪山", allow_shrink=True)[0])

# --- 不确定时**不乱动**，这比"改错"安全 ---
_runtime = "aoi = WB.resolve_region_bbox()\n"
check("运行时解析的写法不碰", enforce_aoi(_runtime, "广州市") == (_runtime, ""))
_unknown = "aoi = ee.Geometry.Rectangle([1.0, 2.0, 3.0, 4.0])\n"
check("库外地名不猜、也不改", enforce_aoi(_unknown, "瓦坎达") == (_unknown, ""))
_extra = "aoi = ee.Geometry.Rectangle([113.01, 22.88, 113.51, 23.38], None, False)\n"
check("带额外参数时只替换四个数、尾部保留",
      enforce_aoi(_extra, "广州市")[0].endswith(", None, False)\n"))
_multi = "aoi = ee.Geometry.Rectangle([\n    113.01,\n    22.88,\n    113.51,\n    23.38])\n"
check("跨行写法也能校正",
      "112.9100, 22.7800, 113.6100, 23.4800" in enforce_aoi(_multi, "广州市")[0])

# --- 守卫不得变成「一律压到 0.5°」：省域本来就该是 2.6° ---
_sx = "aoi = ee.Geometry.Rectangle([111.00, 36.30, 113.60, 38.90])\n"
check("省域 2.6° 视为合规（守卫生效的前提是注入值本身对）",
      enforce_aoi(_sx, "山西省") == (_sx, ""))

# --- 提示词本身不能再自相矛盾，否则守卫只是事后补救 ---
check("资源规则已说明 0.5° 上限不适用于已注入的范围",
      ("不适用于" in _RESOURCE_RULES) and ("没有给出" in _RESOURCE_RULES))
try:
    _p_sx = _build_prompt(AnalysisRequest(
        task_type=TaskType.ndvi, region="山西省",
        start_date="2025-03-01", end_date="2025-05-31", cloud_threshold=30))
    check("注入省域 2.6° 的同时明确要求「照抄、别缩」",
          ("2.60°×2.60°" in _p_sx) and ("照抄" in _p_sx))
except Exception as _e:  # noqa: BLE001
    check("注入省域范围并提示照抄", False, f"{type(_e).__name__}: {_e}")

# --- orchestrator 必须真的调用了守卫，否则守卫只是一段没人用的代码 ---
_orch = (BACKEND / "app" / "orchestrator.py").read_text(encoding="utf-8")
check("生成路径调用了 enforce_aoi", "enforce_aoi" in _orch)
check("修复路径按错误类型决定 allow_shrink（超时/内存才放行缩小）",
      "allow_shrink" in _orch and '"timeout"' in _orch and "memory" in _orch)

print("\n结果：%d/%d 通过" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
