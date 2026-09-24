import math
import re

from .config import settings
from .daterange import default_end, default_start
from .debugger import summarize_error
from .knowledge import build_knowledge_note
from .models import AnalysisRequest, TASK_LABELS
from .preferences import build_preference_note, get_profile
from .regions import DEFAULT_HALF, get_region

SYSTEM_PROMPT = (
    "你是资深卫星遥感与 Google Earth Engine 专家，负责把自然语言需求转成可运行的 Python 代码。"
)

# 真实 GEE 是在 Google 服务端跑计算，AOI 一大、分辨率一细就会撞上
# 「User memory limit exceeded」或干脆算不出来。这些硬约束写死在 prompt 里，
# 比事后让大模型"自己修"可靠得多。
_RESOURCE_RULES = (
    "【资源预算｜硬约束，违反会导致 Earth Engine 报 User memory limit exceeded 或执行超时】\n"
    "a. AOI：**若上面已给出矩形范围，就严格照抄那个范围**（中心与尺寸都别动），"
    "本条的 0.5° 上限**不适用于**已注入的范围。\n"
    "   ⚠ 只有在**完全没有给出**矩形范围时，才适用 0.5°×0.5°（约 50km×50km）的上限，"
    "以给定中心向外展开，禁止跨省大范围框选。\n"
    "   ⚠ 内置区域库给省域/大流域的范围**本来就大于 0.5°**（如省域 2.6°），那是有意为之——"
    "统计要有代表性。按 0.5° 裁掉会让「山西省植被」只统计到省会周边一小块，与用户所问不符。\n"
    "   实测教训（2026-09-24）：提示词同时给了注入范围 0.70° 和本条的 0.5° 上限，"
    "模型自行折中成 0.50°，中心没变但**分析范围被缩小**——即本条与注入范围冲突时的错误解法。\n"
    "b. 禁止对全域调用 .reproject()；如需降采样，直接把 scale 写大（>=250）即可。\n"
    "c. reduceRegion 必须带 bestEffort=True、tileScale=16、scale>=250、maxPixels=1e9。\n"
    "d. 控制数据量只能【在时间分组内部】做：逐月合成时每月取 .sort('CLOUDY_PIXEL_PERCENTAGE').limit(4)；"
    "严禁对全年 ImageCollection 整体 .limit(24)——那会按时间截断，导致后半年各月完全没有数据、曲线出现一串 0。\n"
    "e. 逐月曲线要在服务端一次性聚合（ImageCollection.aggregate_array 或 for 循环里只做服务器端运算），"
    "禁止在 for 循环里反复调用 getInfo()（12 次往返必超时）。\n"
    "f. 逐月统计【不要硬套云量阈值】：长三角地区 4 月、6 月常年整月没有低云影像，硬筛会让该月为空、"
    "median() 直接报 No band named（可用波段为空）。正确做法是每月 filterDate 后 "
    ".sort('CLOUDY_PIXEL_PERCENTAGE').limit(6) 取最晴的几景，不再按云量过滤。\n"
    "g. 逐月必须有兜底：用 ee.Algorithms.If(size().gt(0), 统计值, None)，无影像的月份返回 None；"
    "禁止把 None 静默写成 0，那会伪造出一条错误的曲线。\n"
    "h. 只取计算必需的波段，用 .select() 裁剪，不要整幅多波段图全参与运算。\n"
    "i. 用 .map() 加工影像后【必须把属性一起带过去】，且两个都要带：\n"
    "   return ndvi.copyProperties(img, ['system:time_start', 'CLOUDY_PIXEL_PERCENTAGE'])\n"
    "   —— 漏掉 system:time_start 会让后续 filterDate 返回空集合（表现为整段没数据）；\n"
    "   漏掉 CLOUDY_PIXEL_PERCENTAGE 则 .sort('CLOUDY_PIXEL_PERCENTAGE') 会静默失去意义\n"
    "   （不报错、集合也非空，但选出来的是随机几景），实测极易被误判成「该月无影像」。\n"
    "j. 【None 属性会被省略】规则 g 要求空月返回 None，这是对的；但要注意：Feature "
    "属性值为 None 时，getInfo() 返回的 properties 里**根本不会有这个键**。"
    "所以从 getInfo() 结果里读属性必须用 f['properties'].get('k')，"
    "绝不能写 f['properties']['k']——后者会在第一个空月直接 KeyError 掉整段分析。\n"
    "   实测教训：2026-09-17 变化检测任务写成 f['properties']['ndvi']，"
    "鄱阳湖 2024-2025 存在空月，连续 3 次重试全部 KeyError，指标二/三因此不达标。\n"
    "k. 【Image 与 ImageCollection 的方法不通用，别混用】"
    "`ee.Image` **没有** `.mode()`；`.mode()` 是 ImageCollection 的方法。"
    "要取「出现次数最多的类别」这类众数结果，只有两条合法路径："
    "① 对 ImageCollection 调 `.mode()`；② 对 Image 调 `.reduce(ee.Reducer.mode())`。"
    "同理 `.mosaic()` / `.median()` / `.first()` 也都是 ImageCollection 的，"
    "对 Image 调用会报 `AttributeError: 'Image' object has no attribute 'xxx'`。"
    "写之前先确认手里的对象到底是 Image 还是 ImageCollection："
    "在 Image 上执行过 `.select()` / `.clip()` / `.updateMask()` 之后，"
    "它仍然是 Image，不能当集合用。\n"
    "l. 【空集合会变成「0 波段影像」，此后任何运算必炸 —— 所有任务类型都要防，"
    "不只逐月那一条路径】\n"
    "   任意一次 `ImageCollection` 合成（`.median()` / `.mosaic()` / `.mean()` / `.mode()` / "
    "`.first()`）在集合为空时，得到的是一张**没有任何波段的 Image**；"
    "再对它做 subtract / gt / lt / add / reduceRegion 等任何操作，GEE 直接抛：\n"
    "     `EEException: Image.gt: If one image has no bands, the other must also have no bands. "
    "Got 0 and 1.`\n"
    "   报错里的 `Got 0 and N`，**0 就代表有一侧那张图 0 波段** —— 这就是判据。\n"
    "   ⚠ 报错行只是**症状**，病根在**上游那次合成**。别去改报错那一行（比如调阈值），"
    "那会原地打转、重试到超次仍不通过。\n"
    "   正确做法：**凡是对集合做合成，都要先接住空集合**，例如\n"
    "     comp = col.median()\n"
    "     comp = ee.Image(ee.Algorithms.If(col.size().gt(0), comp, "
    "ee.Image.constant(0).rename('X')))\n"
    "   或至少在运算前 `.unmask(0)`；并在统计前用 `.bandNames().size()` 自检，"
    "为空就打印告警而不是硬算。\n"
    "   高风险场景（务必加守卫）：① 全年/多年合成一张基准影像；② 两个时段相减；"
    "③ 云量阈值设得很严、AOI 很小、或该区域该季节常年多云。\n"
    "   实测教训：2026-09-20 深圳 change_detection 任务，某时段过滤后为空，"
    "`delta.gt(0.1)` 抛上述异常，连续 3 次重试全失败。\n"
)


#: 每类任务的分析口径。**必须固化**：口径不固定，同一个请求两次结果会差到无法解释。
#: 实测依据（太湖流域 2024，AOI 0.5°×0.5°、其中约 88% 是水面，同一份数据同一段代码逻辑）：
#:   · 不掩膜水体：全年均值 -0.12，逐月 -0.21~-0.01，几乎没有季节变化；
#:   · 掩膜水体：  全年均值 +0.52，逐月 0.35→0.60（8 月峰值）→0.39，物候曲线清晰。
#: 两者差 0.64，且后者才符合「植被指数」的语义。
_TASK_CALIBER = {
    "ndvi": (
        "【NDVI 分析口径｜必须严格执行，否则结果不可复现】\n"
        "1. 波段：只需要 .select(['B4', 'B8', 'SCL'])。\n"
        "2. 必须做像元级掩膜，排除 SCL 的：3（云影）、8/9（云）、10（卷云）、11（雪）、"
        "**6（水体）**。\n"
        "   排除水体是关键：水面 NDVI 恒为负，会把植被信号整体拉垮，曲线的季节性也随之消失。\n"
        "   实测同一 AOI：不掩水体均值 -0.12 且曲线几乎无季节变化；掩膜水体后均值 +0.52、"
        "呈清晰物候曲线。本任务语义是「植被指数」，所以必须取植被口径。\n"
        "3. 掩膜写法（SCL 类别 + 反射率有效性，两者都要）：\n"
        "   scl = img.select('SCL')\n"
        "   mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))"
        ".And(scl.neq(11)).And(scl.neq(6))\n"
        "   mask = mask.And(img.select('B8').gt(0)).And(img.select('B4').gt(0))\n"
        "   ndvi = img.normalizedDifference(['B8', 'B4']).rename('NDVI').updateMask(mask)\n"
        "   —— 反射率有效性这一条不能省：S2_SR 的无效像元填充值是 0，B8=0 时\n"
        "   NDVI 恰好算成 -1，最小值统计里会出现理论边界值。实测加上它之后 min 由 -1 恢复正常，\n"
        "   而 mean/max 完全不变（说明只清掉了无效填充值，没有误伤有效像元）。\n"
        "4. 返回时务必 copyProperties 带上 ['system:time_start', 'CLOUDY_PIXEL_PERCENTAGE']（见资源预算 i）。\n"
        "5. 逐月排序只能按云量，**严禁按时间**：\n"
        "   ndvi_col.filterDate(s, e).sort('CLOUDY_PIXEL_PERCENTAGE').limit(6)\n"
        "   按 system:time_start 会取到月初那几景（往往最云），掩膜后可能一个有效像元都不剩，\n"
        "   统计直接返回空字典，曲线出现假的「无数据」。\n"
    ),
    "water": (
        "【水体提取口径｜必须严格执行】\n"
        "1. 用 MNDWI 判水体：normalizedDifference(['B3', 'B11'])（Sentinel-2 有 B11）；\n"
        "   若选 Landsat 则用 normalizedDifference(['green', 'swir1'])。\n"
        "2. 先掩膜云与云影（SCL 的 3/8/9/10/11），再按 MNDWI > 0 提取水体。\n"
        "3. 面积统计必须用真实面积加权，而不是像元计数：\n"
        "   water = mndwi.gt(0).selfMask()\n"
        "   area_km2 = water.multiply(ee.Image.pixelArea()).divide(1e6)"
        ".reduceRegion(ee.Reducer.sum(), geometry=aoi, scale=250,\n"
        "                 bestEffort=True, tileScale=16, maxPixels=1e9).get('B3')\n"
        "   （波段名以 multiply 后实际名称为准，必要时先 .rename('water_area')）\n"
        "4. 逐月同样按 sort('CLOUDY_PIXEL_PERCENTAGE').limit(6) 取最晴几景；\n"
        "   无有效影像的月份返回 None，不要写 0（0 会被当成「水体面积为 0」）。\n"
    ),
    "classification": (
        "【地表分类口径｜必须严格执行】\n"
        "1. 优先用现成分类产品，不要自己训分类器：Dynamic World（GOOGLE/DYNAMICWORLD/V1）"
        "或 ESA WorldCover（ESA/WorldCover/v200）。\n"
        "2. 若用 Dynamic World，按 label 取众数合成；统计各类别面积占比时用 pixelArea 加权。\n"
        "3. 逐月/逐年对比时，口径必须一致（同一产品、同一分类体系），并在图表标题里写明产品名。\n"
    ),
    "change_detection": (
        "【时序变化检测口径｜必须严格执行】\n"
        "1. 变化检测必须锁定两个时段，且【口径完全一致】——同一传感器、同一波段组合、"
        "同一合成方法、同一掩膜规则；否则差异里混着口径差异，结论不成立。\n"
        "2. 用 NDVI（或 NDWI）差值法：delta = later.subtract(earlier)；"
        "对 delta 做阈值划分（如 ±0.1）并统计各变化类型的面积。\n"
        "3. 两个时段都要各自做云掩膜与兜底，避免某段无数据被误判为「变化」。\n"
        "4. 【本任务最高频失败点：某个时段集合为空 → 0 波段 → 相减必炸】"
        "本任务是「两时段相减」，属于通用规则 l 里的**最高风险场景 ②**，务必按 l 加守卫。"
        "差值的两侧各自都要确认 `.bandNames().size().gt(0)`：\n"
        "     def safe_subtract(a, b):\n"
        "         return ee.Image(ee.Algorithms.If(\n"
        "             a.bandNames().size().gt(0).And(b.bandNames().size().gt(0)),\n"
        "             a.subtract(b), ee.Image.constant(0).rename('delta')))\n"
        "   实测教训：2026-09-20 深圳任务两个时段各取 median 后相减，"
        "其中一个时段过滤后为空，`delta.gt(0.1)` 抛"
        "`Image.gt: ... Got 0 and 1.`，连续重试 3 次全失败。\n"
    ),
}


def _build_prompt(req: AnalysisRequest, user_id: int | None = None) -> str:
    pref_note = build_preference_note(user_id=user_id)
    pref_block = f"\n{pref_note}\n" if pref_note else ""
    kb_note = build_knowledge_note(req.task_type.value, req.region)
    kb_block = f"\n{kb_note}\n" if kb_note else ""
    profile = get_profile(user_id)
    reg = get_region(req.region)
    if reg:
        lon, lat = reg["lon"], reg["lat"]
        half = float(reg.get("half", DEFAULT_HALF))
        coord_block = (
            f"区域中心坐标（**已从内置区域库精确解析，请直接使用，不要自行猜测或改动**）："
            f"经度 {lon}、纬度 {lat}（{reg.get('desc', '')}）。\n"
            f"AOI 请以该点为中心，用 [{lon - half:.4f}, {lat - half:.4f}] ~ "
            f"[{lon + half:.4f}, {lat + half:.4f}] 这一矩形范围构造 ee.Geometry.Rectangle"
            f"（{half * 2:.2f}°×{half * 2:.2f}°）。\n"
        )
    else:
        # 没解析出坐标时**不要叫模型自己编经纬度** —— 那正是 2026-09-23
        # 「恩施大峡谷显示成苏州」的成因之一（模型编的坐标与硬编码兜底叠加）。
        # 改为要求它在运行时调用 WB.resolve_region()，让解析发生在沙箱里、
        # 有据可查；再解析不到就明确告知"无法定位"。
        coord_block = (
            "⚠ 该区域**未能在内置区域库中解析出中心坐标**（可能是小众地名或写法特殊）。\n"
            "**不要自行猜测经纬度**。请按下面的顺序处理：\n"
            "  ① 先调用 `WB.resolve_region()`（不传参数则用注入的 region 变量）取坐标；\n"
            "     它内部会查「业务区域库 + 全国省级/地级市坐标簿」，能解析就直接用。\n"
            "  ② 若仍返回 None，则用全国范围 `[73.5, 18.0, 135.0, 53.5]` 兜底，\n"
            "     并 print 一句明确的中文警告说明「未能确定分析区域坐标，当前为全国范围」。\n"
            "**绝不能默默用一个与用户所问无关的坐标**。\n"
        )
    return (
        f"请生成一段 Google Earth Engine（Python API，模块名 ee）分析代码。\n"
        f"任务类型：{TASK_LABELS.get(req.task_type, req.task_type.value)}\n"
        f"分析区域：{req.region}\n"
        f"{coord_block}"
        f"时间范围：{req.start_date} ~ {req.end_date}\n"
        f"云量阈值：{req.cloud_threshold}%\n"
        f"{kb_block}\n"
        f"{pref_block}\n"
        f"{_RESOURCE_RULES}\n"
        f"{_TASK_CALIBER.get(req.task_type.value, '')}\n"
        "要求：\n"
        "1. 只输出 Python 代码本身，不要任何解释文字，不要 markdown 代码块围栏。\n"
        "2. 第一行 import ee（ee 已由沙箱预初始化，无需再调用 ee.Initialize()）。\n"
        "3. 用 ee.Geometry.Rectangle 定义 AOI，并**逐字使用上面给出的那个矩形范围**"
        "（四个数照抄：不要自己改尺寸、不要为了「节省资源」缩到 0.5°、不要只取中心点）。\n"
        "4. 使用 Sentinel-2 或 Landsat 数据，按云量阈值筛选。\n"
        "5. 计算并 print 关键统计结果。\n"
        "6. 必须用 WB 报告器上报可视化结果，否则前端拿不到地图与图表：\n"
        "   · WB.add_image_layer('图层名', image, vis_params, legend=[{'label':'低','color':'#fff'}])"
        "  —— 上报栅格图层（自动转瓦片）\n"
        "   · WB.add_feature_layer('图层名', featureCollection) —— 上报矢量图层\n"
        "   · WB.add_chart('图表标题', labels=[...], series=[{'name':'系列名','data':[...]}], kind='line'|'bar'|'pie')\n"
        "   · WB.stat('指标名', 数值) —— 上报关键统计值\n"
        "7. 可用上下文变量：region、start_date、end_date、cloud_threshold。\n"
        "8. 【AOI 变量名必须是 `aoi`】定义分析范围时务必写成 `aoi = ee.Geometry.Rectangle(...)`"
        "（或 Polygon），不要用 aoi_rect / roi / geom 等别名。"
        "沙箱会读取这个变量算出分析范围并回传前端用于**地图自动定位**；"
        "变量名不对会导致前端无法把地图定位到你的分析区域。\n"
        "9. 【区域坐标解析】若上面已给出中心坐标就直接用；**没有给出时**不许自己编经纬度，"
        "用 `WB.resolve_region()` 在运行时解析（它查内置区域库 + 全国省市坐标簿），"
        "仍取不到就用全国范围兜底并 print 明确的警告。\n"
        f"10. 代码风格：{profile['code_style']}\n"
    )


def _fallback_code(req: AnalysisRequest) -> str:
    """大模型不可用时的兜底代码。

    已按 _RESOURCE_RULES 约束：小 AOI、scale=250、bestEffort，逐月曲线
    用服务端一次聚合后单次 getInfo 取回（而不是循环里打 12 次网络请求）。
    """
    label = TASK_LABELS.get(req.task_type, req.task_type.value)
    # ⚠ 兜底中心**不能**写死某个具体地方（原先是 `or (120.13, 31.20)` = 太湖）。
    # 那会让"恩施大峡谷"这类非内置地名的兜底图默默画在太湖 —— 用户看到的是
    # 一张苏州底图，而分析对象在 1000 km 外，属于静默的错误结果。
    # 取不到中心时改用全国范围 + 显式提示，让"不知道"这件事可见。
    #
    # 2026-09-24：内置库已从 13 条扩到 400+ 条（覆盖全国主要景区/山脉/湖泊/城市/省），
    # 并且每条自带建议范围 `half`（景区小、流域大），所以这里改用它给出的范围，
    # 不再一律 0.5°×0.5° —— 对"长江流域"这种大区域，0.5° 框得太小、统计没代表性。
    reg = get_region(req.region)
    if reg:
        lon, lat = reg["lon"], reg["lat"]
        half = float(reg.get("half", DEFAULT_HALF))
        coord_line = (
            f"# 区域中心由内置区域库解析：{req.region}（{reg.get('desc', '')}）\n"
            f"aoi = ee.Geometry.Rectangle(["
            f"{lon - half:.4f}, {lat - half:.4f}, {lon + half:.4f}, {lat + half:.4f}])\n"
        )
        print_line = (
            f'print("[提示] 区域「{req.region}」使用内置中心坐标 '
            f'({lon}, {lat})，范围 {half * 2:.2f}°×{half * 2:.2f}°")\n'
        )
    else:
        # 全国范围（仅供定位，实际的统计意义有限），并在输出里说清楚
        coord_line = (
            f"# ⚠ 内置区域库中没有「{req.region}」的中心坐标，无法自动定位。\n"
            "# 这里退化为全国范围仅用于跑通链路；如需真实分析请改用内置区域名，\n"
            "# 或等待大模型可用时由模型自行给定坐标。\n"
            "aoi = ee.Geometry.Rectangle([73.5, 18.0, 135.0, 53.5])\n"
        )
        print_line = (
            f'print("[警告] 区域「{req.region}」不在内置区域库中，'
            '当前使用全国范围兜底，分析结果不具备区域针对性")\n'
        )
    return (
        "import ee\n\n"
        f'region = globals().get("region") or "{req.region}"\n'
        f"{coord_line}"
        f"{print_line}"
        # 兜底日期优先跟随本次请求（req.start_date 自身已由 models.py 按当天
        # 推算默认值），再退化到 daterange 的全局默认。
        # 这段代码会原样交付给用户看/改，写死 '2024-01-01' 等于把过期基准固化进生成物。
        f"start_date = globals().get('start_date') or '{req.start_date or default_start()}'\n"
        f"end_date = globals().get('end_date') or '{req.end_date or default_end()}'\n"
        "cloud = globals().get('cloud_threshold') or 20\n\n"
        'base = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")\n'
        "        .filterBounds(aoi)\n"
        "        .filterDate(start_date, end_date)\n"
        "        .select(['B4', 'B8', 'SCL']))\n\n"
        "def add_ndvi(img):\n"
        "    # 掩膜云影/云/卷云/雪，并排除水体：排除水体后统计口径才是「植被 NDVI」\n"
        "    scl = img.select('SCL')\n"
        "    mask = (scl.neq(3).And(scl.neq(8)).And(scl.neq(9))\n"
        "            .And(scl.neq(10)).And(scl.neq(11)).And(scl.neq(6)))\n"
        "    # 反射率有效性：S2_SR 无效像元填充值为 0，B8=0 会让 NDVI 恰好算成 -1\n"
        "    mask = mask.And(img.select('B8').gt(0)).And(img.select('B4').gt(0))\n"
        "    ndvi = img.normalizedDifference(['B8', 'B4']).rename('NDVI').updateMask(mask)\n"
        "    # 两个属性都要带过去：丢了时间则 filterDate 为空，丢了云量则排序失去意义\n"
        "    return ndvi.copyProperties(\n"
        "        img, ['system:time_start', 'CLOUDY_PIXEL_PERCENTAGE'])\n\n"
        "ndvi_col = base.map(add_ndvi)\n"
        "print('全年影像数=', ndvi_col.size().getInfo())\n"
        "composite = ndvi_col.sort('CLOUDY_PIXEL_PERCENTAGE').limit(60).median()\n"
        "stats = composite.reduceRegion(reducer=ee.Reducer.mean().combine(\n"
        "    ee.Reducer.minMax(), sharedInputs=True), geometry=aoi, scale=250,\n"
        "    bestEffort=True, tileScale=16, maxPixels=1e9).getInfo()\n"
        "print('NDVI 统计=', stats)\n\n"
        "def monthly(m):\n"
        "    # 逐月取最晴的几景，不硬套云量阈值（4/6 月常整月无低云影像）\n"
        "    m = ee.Number(m)\n"
        "    y = ee.Date(start_date).get('year')\n"
        "    s = ee.Date.fromYMD(y, m, 1)\n"
        "    sub = ndvi_col.filterDate(s, s.advance(1, 'month')).sort(\n"
        "        'CLOUDY_PIXEL_PERCENTAGE').limit(6)\n"
        "    val = sub.mean().reduceRegion(\n"
        "        reducer=ee.Reducer.mean(), geometry=aoi, scale=250,\n"
        "        bestEffort=True, tileScale=16, maxPixels=1e9).get('NDVI')\n"
        "    return ee.Algorithms.If(sub.size().gt(0), val, None)\n\n"
        "monthly_vals = ee.List.sequence(1, 12).map(monthly).getInfo()\n"
        "monthly_vals = [None if v is None else round(v, 3) for v in monthly_vals]\n"
        "print('逐月 NDVI=', monthly_vals)\n\n"
        f'WB.stat("任务", "{label}")\n'
        "if stats:\n"
        "    for k, v in stats.items():\n"
        "        if v is not None:\n"
        "            WB.stat(k, round(v, 3))\n"
        "WB.add_image_layer('NDVI 分布（植被口径）', composite, {'min': 0.0, 'max': 0.8,\n"
        "                   'palette': ['#d73027', '#fee08b', '#1a9850']},\n"
        "                   legend=[{'label': '低', 'color': '#d73027'},\n"
        "                           {'label': '高', 'color': '#1a9850'}])\n"
        "WB.add_feature_layer('分析范围', ee.FeatureCollection([ee.Feature(aoi)]))\n"
        "WB.add_chart('逐月 NDVI 均值', labels=[f'{i+1}月' for i in range(12)],\n"
        "             series=[{'name': 'NDVI', 'data': monthly_vals}], kind='line')\n"
    )


def _chat(messages: list[dict], temperature: float = 0.2) -> str:
    import httpx

    url = settings.deepseek_base_url + "/chat/completions"
    payload = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    r = httpx.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _strip_fence(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = code.strip("`")
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()
    return code


#: 匹配 `aoi = ee.Geometry.Rectangle([ ... ])`，允许任意空白与换行。
#: 只认 **4 个字面量** 的写法。`[^\[\]]*` 故意排除方括号，所以写成
#: `Rectangle([[a,b],[c,d]])` 或变量拼接/f-string 的都匹配不到 —— 那种情况
#: **不强行改**（改坏合法代码的风险大于收益），由单测里的形态断言守住。
_AOI_RECT_RE = re.compile(
    r"(?P<head>\baoi\s*=\s*ee\.Geometry\.Rectangle\(\s*\[)"
    r"(?P<body>[^\[\]]*)"
    r"(?P<tail>\])",
    re.S,
)

#: 中心允许偏移（度）。0.02° ≈ 2km，只用来吸收四舍五入 —— **不是"容忍跑偏"**。
#: 超过它就意味着分析对象已经不是用户问的那个地方了。
_AOI_CENTER_TOL_DEG = 0.02

#: 尺寸允许偏差（度）。0.01° 远小于任何真实 half（最小 0.05°），
#: 所以"模型改了尺寸"必然被抓到，而浮点写法差异不会误报。
_AOI_EXTENT_TOL = 0.01


def enforce_aoi(code: str, region: str, *, allow_shrink: bool = False) -> tuple[str, str]:
    """把生成代码里 `aoi` 的矩形**校正回区域库给的范围**。返回 (代码, 说明)。

    为什么要这一道：提示词里同时存在两条关于 AOI 的指令 ——
      ① 「逐字使用上面注入的矩形」（来自内置区域库，按区域类型给 half）
      ② 「AOI 必须 ≤0.5°×0.5°」（资源预算硬约束）
    对省域/大流域这两条**互相矛盾**，模型会不一致地自行折中。
    2026-09-24 实测：随机抽的 5 个任务里，「广州市」把注入的 0.70° 缩成 0.50°
    （正好是②的上限），中心没变但分析范围偏小 —— 地图仍落在广州，只是面积不符。

    处理策略（**中心不可协商，尺寸可协商**）：
      · 中心偏移 > 2km              → 一律替换成注入的矩形（这是"分析到别处"那一类 bug）
      · 尺寸不符且 allow_shrink=False → 替换成注入的矩形
      · 尺寸偏大且 allow_shrink=True  → 压回注入的矩形（不能超出，否则只会更慢）
      · 尺寸偏小且 allow_shrink=True  → **保留**（超时/内存超限时缩小是合法缓解手段）
      · 区域解析不出来 / 匹配不到字面量 → 原样返回，一个字都不改

    `allow_shrink` 由调用方按错误类型决定：沙箱超时或 GEE 内存超限时为 True，
    否则为 False。**不能一律强制恢复**——省域任务超时后强行把 2.6° 拽回去，
    只会再超时一次，永远收敛不了。
    """
    reg = get_region(region)
    if not reg:
        return code, ""          # 解析不出意图就别乱动，代码里可能用的是运行时解析

    lon, lat = float(reg["lon"]), float(reg["lat"])
    half = float(reg.get("half", DEFAULT_HALF))
    want = (lon - half, lat - half, lon + half, lat + half)

    hits = list(_AOI_RECT_RE.finditer(code))
    if not hits:
        return code, ""

    notes, out, cursor = [], [], 0
    for m in hits:
        nums = re.findall(r"-?\d+(?:\.\d+)?", m.group("body"))
        if len(nums) != 4:
            continue                       # 不是 4 个字面量（变量拼接等），不碰
        w, s, e, n = (float(x) for x in nums)
        clon, clat = (w + e) / 2, (s + n) / 2
        shift = max(abs(clon - lon), abs(clat - lat))
        size_off = max(abs((e - w) - half * 2), abs((n - s) - half * 2))

        centered = shift <= _AOI_CENTER_TOL_DEG
        exact = size_off <= _AOI_EXTENT_TOL
        shrinking = (e - w) < half * 2 - _AOI_EXTENT_TOL

        if centered and (exact or (allow_shrink and shrinking)):
            continue                       # 合规，放行

        out.append((m.start("body"), m.end("body"),
                    "%.4f, %.4f, %.4f, %.4f" % want))
        if not centered:
            dkm = ((clon - lon) * 111 * math.cos(math.radians(lat))) ** 2
            dkm = math.sqrt(dkm + ((clat - lat) * 111) ** 2)
            notes.append(f"AOI 中心被移动了 {dkm:.1f}km（{clon:.3f},{clat:.3f} → "
                         f"{lon},{lat}），已校正回区域库坐标")
        else:
            notes.append(f"AOI 尺寸被从 {(e - w):.2f}°×{(n - s):.2f}° 改成 "
                         f"{half * 2:.2f}°×{half * 2:.2f}°，已校正回区域库给的范围")

    if not out:
        return code, ""

    # 倒序拼回，避免前面的替换把后面的下标打乱
    new_code = code
    for a, b, txt in sorted(out, reverse=True):
        new_code = new_code[:a] + txt + new_code[b:]
    return new_code, "；".join(dict.fromkeys(notes))    # 多处同样的问题只报一次


def generate_code(req: AnalysisRequest, user_id: int | None = None) -> tuple[str, str]:
    """返回 (code, 来源)，来源为 deepseek 或 fallback

    `user_id` 用于取该用户自己的分析偏好；不传则用全局默认值。
    """
    if not settings.deepseek_api_key:
        return _fallback_code(req), "fallback"
    try:
        code = _chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(req, user_id)},
            ]
        )
        return _strip_fence(code), "deepseek"
    except Exception as e:
        return _fallback_code(req), f"fallback({type(e).__name__})"


def fix_code(code: str, error: dict, note: str = "") -> str:
    """根据错误信息让大模型修复代码；无 key 时原样返回

    `note` 可选：额外塞进提示词的强提醒。用来处理「上一轮修了等于没修」——
    实测分类任务曾连续 3 次返回逐字相同的错误，说明模型没意识到自己没改动。
    """
    if not settings.deepseek_api_key:
        return code
    msg = error.get("message", "")
    cat = error.get("category", "other")
    # 超时 / 内存超限的本质都是"算得太重"，必须给明确的减法指令，
    # 否则大模型倾向于继续加代码（加波段、加精度），越修越慢。
    if cat == "timeout":
        hint = (
            "本次是【执行超时】，说明计算量过大。修复请做减法而非加法："
            "把 AOI 缩小到 0.25°×0.25°、scale 提到 500、影像 .limit(12)、"
            "去掉一切 reproject/裁剪/逐月循环，只保留一次 reduceRegion 统计和一个 WB 图层。"
        )
    elif "memory" in msg.lower() or "maxpixels" in msg.lower():
        hint = (
            "本次是【Earth Engine 内存超限】，修复请降低单次计算规模："
            "缩小 AOI、把 scale 提到 500、tileScale=16、bestEffort=True、只 select 必需波段。"
        )
    else:
        hint = "保持原有分析意图，修正导致报错的具体调用。"
    try:
        return _strip_fence(
            _chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "下面这段 GEE Python 代码执行报错，请修复后只输出完整可运行的 Python 代码，"
                            "不要任何解释。\n"
                            "修复要求：不要调用 ee.Initialize()（沙箱已预初始化）；"
                            "继续用 WB.add_image_layer / WB.add_feature_layer / WB.add_chart 上报结果"
                            "（至少要有一个 WB.add_image_layer 和一次 WB.stat）。\n"
                            "【严禁改变分析口径】只修导致报错的地方，不要改动：掩膜规则"
                            "（哪些 SCL 类别被排除，尤其水体是否被排除）、排序方式"
                            "（必须仍为 sort('CLOUDY_PIXEL_PERCENTAGE')）、统计方法。"
                            "口径一变，同一请求前后两次的结果就不可比了。\n"
                            "⚠ **AOI 的中心坐标绝对不能移动** —— 那会让结果分析到另一个地方去。"
                            "确因超时/内存需要降低计算量时，只允许**围绕原中心等比缩小**，"
                            "并保持矩形形状；禁止把中心点到别处、也禁止换成全国范围。\n"
                            f"{hint}\n"
                            f"{note}"
                            f"{_RESOURCE_RULES}\n"
                            f"失败信息：\n{summarize_error(error, code)}\n\n代码：\n{code}"
                        ),
                    },
                ]
            )
        )
    except Exception:
        return code
