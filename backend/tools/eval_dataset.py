"""测试任务集：四个量化指标的标注数据。

本文件就是任务书「四、项目任务分解」里要求的**测试任务集**交付物的实体。

## 两条设计原则

### 1. 标注里不写死年份

时间期望用**规则**表达（如"月份区间 6-8 月、年份取最近一个已完整过去"），由
runner 按运行当天推算。这一点是刻意的：如果这里写死 "2026-06-01"，评测集自己
就变成了下一个"年代漂移"bug——今年跑着对、明年跑着错，而且错误藏在"测试期望"
里，比生产代码更难发现（生产代码至少有用户看到不对，测试期望错了只会让人以为
产品坏了）。

同样地，端到端任务集里的时间也按当天推算，不写死。

### 2. 模糊处给可接受集合，不给唯一答案

"太湖流域"与"太湖"都是合理解析，标注就同时接受两者；只有真正有对错的
（task_type、是否该追问）才做严格比较。否则指标会惩罚合理的等价答案，
把准确率测成一个偏低的假数字。

## 日期规则词汇表（runner 据此判定）

    exact(start, end)        用户显式说了年月 → 严格比对，容差 ±1 天（吸收时区差）
    months([m...])           用户只说了月份区间 → 月份必须一致，年份必须是"最近一个
                             已完整过去"的那次；容差 ±1 天
    recent(max_age_months)   用户说"现在/最近/近两年"这类模糊时间 → 只要区间结束日
                             不早于 max_age_months 个月前就接受。
                             这条专门用来抓"把'现在'解析成两年前"的 bug。
    any                      不检查时间

## 端到端任务集

用于指标二/三/四（一次成功率、调试后完成率、典型任务耗时）。这些任务会真实提交
到服务并调用 GEE（实测约 30-50s/条），因此条数不宜过多。

取 **8 条 = 四类任务 × 2 个实例**：第一轮覆盖四类任务，第二轮给每类换一个区域与
时段。只放 4 条时，1 次失败就让成功率摆动 25%，测出来的更像运气；每类 2 个实例
才能区分"这类任务存在"和"这类任务换个区域/时段也能跑通"（泛化）。
"""

from __future__ import annotations

from datetime import date

# =====================================================================
# 指标一：意图识别准确率（本机可跑，只依赖 DeepSeek，不依赖 GEE）
# =====================================================================
# 每条包含：输入文本、期望解析结果、期望是否"参数已齐"。
# accept 类字段取列表语义 —— 命中任意一个即算对。

INTENT_CASES = [
    {
        "id": "ndvi-月份区间",
        "text": "帮我分析太湖流域 6-8 月的植被状况",
        "task_type": "ndvi",
        "region": ["太湖流域", "太湖"],
        "date": {"kind": "months", "months": [6, 8]},
        "complete": True,
        "why": "月份区间要补成最近一个已完整过去的年份（2026 跑时是 2026-06~08）",
    },
    {
        "id": "water-现在",
        "text": "看看鄱阳湖现在的水域范围",
        "task_type": "water",
        "region": ["鄱阳湖"],
        "date": {"kind": "recent", "max_age_months": 18},
        "complete": True,
        "why": "「现在」是模糊时间：模型答 null（正确）时兜底值必须按当天推算，"
               "不能是写死的 2024 —— 这条就是抓那个 bug 的",
    },
    {
        "id": "change-显式两年对比",
        "text": "对比洞庭湖 2020 年和 2024 年的水域变化",
        "task_type": "change_detection",
        "region": ["洞庭湖", "洞庭湖流域"],
        "date": {"kind": "exact", "start": "2020-01-01", "end": "2024-12-31"},
        "complete": True,
        "why": "用户显式给了年份，必须严格照办，不能被默认值改掉",
    },
    {
        "id": "classification-显式单年",
        "text": "分析北京市 2023 年的地表覆盖类型",
        "task_type": "classification",
        "region": ["北京市", "北京"],
        "date": {"kind": "exact", "start": "2023-01-01", "end": "2023-12-31"},
        "complete": True,
    },
    {
        "id": "ndvi-最近一月",
        "text": "看一下太湖流域最近一个月的植被长势",
        "task_type": "ndvi",
        "region": ["太湖流域", "太湖"],
        "date": {"kind": "recent", "max_age_months": 3, "max_span_months": 2},
        "complete": True,
        "why": "模糊时间只要求落在近期窗口内，不锁定成某个具体区间。"
               "max_span_months 是必要的：只查结束日的话，「2024-01-01~2026-09-01」"
               "这种跨两年的答案也会被判对，但那显然不是「最近一个月」",
    },
    {
        "id": "water-带云量阈值",
        "text": "提取太湖流域 2025 年 7 月的水体，云量控制在 10% 以内",
        "task_type": "water",
        "region": ["太湖流域", "太湖"],
        "date": {"kind": "exact", "start": "2025-07-01", "end": "2025-07-31"},
        "cloud_threshold": 10,
        "complete": True,
    },
    {
        "id": "缺任务类型",
        "text": "鄱阳湖",
        "task_type": None,
        "region": ["鄱阳湖"],
        "date": {"kind": "any"},
        "complete": False,
        "missing": ["task_type"],
        "why": "只给了区域，必须追问任务类型而不是瞎猜一个",
    },
    {
        "id": "缺区域",
        "text": "帮我做个植被分析",
        "task_type": "ndvi",
        "region": ["", None],
        "date": {"kind": "any"},
        "complete": False,
        "missing": ["region"],
        "why": "只给了类型，必须追问区域",
    },
    {
        "id": "classification-区域别名",
        "text": "看看长江三角洲的地表分类",
        "task_type": "classification",
        "region": ["长江三角洲", "长三角", "长江三角洲地区"],
        "date": {"kind": "any"},
        "complete": True,
    },
    {
        "id": "change-近两年",
        "text": "对比太湖流域近两年以来的变化",
        "task_type": "change_detection",
        "region": ["太湖流域", "太湖"],
        "date": {"kind": "recent", "max_age_months": 30, "max_span_months": 30},
        "complete": True,
    },
    {
        "id": "ndvi-跨年月区间",
        "text": "分析鄱阳湖 2023 年 11 月到 2024 年 2 月的植被",
        "task_type": "ndvi",
        "region": ["鄱阳湖"],
        "date": {"kind": "exact", "start": "2023-11-01", "end": "2024-02-29"},
        "complete": True,
        "why": "跨年的显式月份，终点要落在 2024-02-29（闰年）或 2024-02-28",
    },
    {
        "id": "水面变化-类型歧义",
        "text": "太湖的水面面积最近有啥变化",
        # 「水面面积变化」既可判为 water 也可判为 change_detection，两者都合理
        "task_type": ["water", "change_detection"],
        "region": ["太湖", "太湖流域"],
        "date": {"kind": "recent", "max_age_months": 18},
        "complete": True,
        "why": "真实歧义句：给可接受集合，不惩罚合理答案",
    },
    {
        "id": "无关输入",
        "text": "今天天气怎么样",
        "task_type": None,
        "region": ["", None],
        "date": {"kind": "any"},
        "complete": False,
        "why": "与遥感分析无关，应当澄清而不是硬套一个任务类型",
    },
]


# =====================================================================
# 指标二/三/四：端到端执行（需真实 GEE，约 30-60s/条）
# =====================================================================
# 时间同样按当天推算：default_year 是"最近一个完整自然年"，与生产默认口径一致。

_PREV_YEAR = date.today().year - 1

E2E_CASES = [
    {
        "id": "e2e-ndvi-太湖",
        "task_type": "ndvi",
        "region": "太湖流域",
        "start_date": f"{_PREV_YEAR}-06-01",
        "end_date": f"{_PREV_YEAR}-08-31",
        "cloud_threshold": 20,
    },
    {
        "id": "e2e-water-洞庭湖",
        "task_type": "water",
        "region": "洞庭湖",
        "start_date": f"{_PREV_YEAR}-05-01",
        "end_date": f"{_PREV_YEAR}-09-30",
        "cloud_threshold": 20,
    },
    {
        "id": "e2e-classification-北京",
        "task_type": "classification",
        "region": "北京市",
        "start_date": f"{_PREV_YEAR}-04-01",
        "end_date": f"{_PREV_YEAR}-10-31",
        "cloud_threshold": 20,
    },
    {
        "id": "e2e-change-鄱阳湖",
        "task_type": "change_detection",
        "region": "鄱阳湖",
        "start_date": f"{_PREV_YEAR - 1}-01-01",
        "end_date": f"{_PREV_YEAR}-12-31",
        "cloud_threshold": 20,
    },
    # ---- 第二轮：每类任务换一个区域与时段 ----
    # 加上这 4 条是为了让"成功率"不只是"每类任务有 1 个实例且它没挂"。
    # 4 条样本时，1 次失败就摆动 25%，测出来的更像运气；8 条（每类 2 个实例）
    # 才勉强能说明"同类任务换区域/换时段也能跑通"，即泛化而非存在性。
    {
        "id": "e2e-ndvi-鄱阳湖",
        "task_type": "ndvi",
        "region": "鄱阳湖",
        "start_date": f"{_PREV_YEAR}-04-01",
        "end_date": f"{_PREV_YEAR}-06-30",
        "cloud_threshold": 20,
    },
    {
        "id": "e2e-water-太湖",
        "task_type": "water",
        "region": "太湖流域",
        "start_date": f"{_PREV_YEAR}-07-01",
        "end_date": f"{_PREV_YEAR}-09-30",
        "cloud_threshold": 20,
    },
    {
        "id": "e2e-classification-长三角",
        "task_type": "classification",
        "region": "长江三角洲",
        "start_date": f"{_PREV_YEAR}-05-01",
        "end_date": f"{_PREV_YEAR}-08-31",
        "cloud_threshold": 20,
    },
    {
        "id": "e2e-change-太湖",
        "task_type": "change_detection",
        "region": "太湖流域",
        "start_date": f"{_PREV_YEAR - 1}-06-01",
        "end_date": f"{_PREV_YEAR}-09-30",
        "cloud_threshold": 20,
    },
]
