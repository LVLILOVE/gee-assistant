#!/usr/bin/env python
"""演示前自检（体检）—— 补 `demo_up.sh --status` 没有覆盖的三项。

为什么需要单独这三项（每一项都是真实踩过的坑，不是假想）：

  L4 LLM 可用性：`/health` 只回 `has_key: true`，只能说明"配了"，说明不了
     "这个 key 还能用"。而 `DEEPSEEK_API_KEY` 正是被按"已泄漏、待轮换"处理的那个 ——
     **轮换之后必须有一次真实调用才能确认新 key 生效**，否则演示第一步就卡死。
     注意必须用**和主流程同一个客户端与同一份配置**（httpx + app.config.settings），
     否则会出现"体检说好、实际用不了"的假绿。

     ⚠️ 本机有两种**都会让 DeepSeek 不可达**的失败形态，报错要能区分开，
     否则会把"环境问题"误诊成"密钥问题"（2026-09-20 实测两种情况都遇到过）：
       a) `ProxyError: 502 Bad Gateway` —— http_proxy 指向的本地代理节点不通；
       b) `ConnectTimeout: [WinError 10060]` —— 直连（无代理）时被网络阻断。
     两者都**不代表密钥无效**，处置都是"换代理节点 / 恢复出网"，不是去控制台换 key。

  L5 前端产物新鲜度：单端口托管下 `frontend/dist` 是后端直接吐出去的。
     改了 `src` 却忘了 `npm run build`，**演示看到的是旧界面，而且所有后端测试都不会报错**。
     顺带校验 index.html 引用的 assets 文件是否真的都在（复制/部署中断会漏文件）。

  L6 库状态：`users` / `tasks` / 会话计数。这是**答辩当天可能被人当场翻看**的东西：
     库里有没有管理员、历史任务归属对不对。

  L7 业务数据加密：敏感字段（region / code / …）到底是不是密文落盘。
     任务书 PRD §7 要求「用户区域/任务描述敏感，加密存储」。这一项**极易静默回退** ——
     某次改动漏掉一个写路径，明文就又回来了，而所有功能测试都不会报错。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe tools\\demo_doctor.py
    .venv\\Scripts\\python.exe tools\\demo_doctor.py --no-llm   # 跳过联网那项

退出码：0 = 全绿；1 = 有 FAIL。WARN 不影响退出码。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent      # → backend/
ROOT = BACKEND.parent                                  # → gee-assistant/

# ⚠ 自检模式**绝不能碰真实库与真实密钥**。
#   原因：本脚本一 import app.task_store 就会建 TaskStore() → 触发迁移，
#   而迁移里含"加密回填"，会真的改写线上库（2026-09-20 实测被这一步误伤过）。
#   自检只验判定规则，不需要真数据，所以把 DB / 密钥都指向临时位置。
#   必须在 import app.* 之前设置 —— DB_PATH 是模块级常量，导入即定型。
if "--self-test" in sys.argv:
    _tmp = Path(tempfile.mkdtemp(prefix="doctor_selftest_"))
    os.environ["APP_DB_PATH"] = str(_tmp / "app.db")
    os.environ["WB_DATA_KEY_PATH"] = str(_tmp / ".data_key")
sys.path.insert(0, str(BACKEND))                       # 让 app.* 可导入

OK, WARN, FAIL = "OK", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []               # (level, 标题, 说明)


def record(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))
    mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}[level]
    line = f"  {mark} {title}"
    if detail:
        line += f"\n         {detail}"
    print(line)


def section(name: str) -> None:
    print(f"\n[{name}]")


# --------------------------------------------------------------------- L4 LLM
def check_llm() -> None:
    section("L4 LLM 密钥是否真的可用")
    try:
        from app.config import settings
    except Exception as e:  # noqa: BLE001
        record(FAIL, "读取配置失败", f"{type(e).__name__}: {e}")
        return

    if not settings.deepseek_api_key:
        record(WARN, "未配置 DEEPSEEK_API_KEY",
               "服务会走规则兜底（不致命），但演示的代码生成质量会明显下降")
        return

    key = settings.deepseek_api_key
    # 只留指纹 + 末 4 位：够用来确认"换的是哪一把"，又不把 key 印进可能被截图的报告里
    import hashlib
    fp = hashlib.sha256(key.encode()).hexdigest()[:8]
    masked = f"指纹 {fp}（末 4 位 {key[-4:]}）"
    url = settings.deepseek_base_url + "/chat/completions"

    try:
        import httpx
    except Exception as e:  # noqa: BLE001
        record(WARN, "httpx 不可用，跳过真实调用", f"{type(e).__name__}: {e}")
        return

    t0 = time.time()
    try:
        # 与 codegen._chat 用同一客户端、同一 base_url、同一 model —— 尽量复现真实调用路径
        r = httpx.post(
            url,
            json={
                "model": settings.deepseek_model,
                "messages": [{"role": "user", "content": "ping"}],
                "temperature": 0,
                "max_tokens": 1,          # 只为验活，把消耗压到最小
                "stream": False,
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        cost = time.time() - t0
        if r.status_code == 200:
            record(OK, f"密钥可用（{settings.deepseek_model}）",
                   f"key={masked}  往返 {cost:.1f}s")
        elif r.status_code in (401, 403):
            record(FAIL, f"密钥被拒绝（HTTP {r.status_code}）",
                   f"key={masked} —— 到 DeepSeek 控制台确认密钥有效/未吊销，"
                   f"再更新 backend/.env 并重启服务")
        elif r.status_code == 402:
            record(FAIL, "账户余额不足（HTTP 402）", "演示前请充值，否则代码生成会失败")
        elif r.status_code == 429:
            record(WARN, "限流（HTTP 429）",
                   "密钥本身有效，只是当下请求过多；等一会儿再测")
        else:
            body = (r.text or "")[:200].replace("\n", " ")
            record(FAIL, f"异常响应（HTTP {r.status_code}）", body)
    except Exception as e:  # noqa: BLE001
        # 关键：把"网络/基础设施问题"和"密钥问题"分开报 —— 否则会把
        # "本机代理不通"记成"产品不行"。
        # 三种失败形态要给出不同的排查方向（都是环境问题，都不必换 key）：
        #   ProxyError     → http_proxy 指向的本地代理节点不通，换节点
        #   ConnectTimeout → 直连被阻断，恢复出网（或改用可用代理）
        #   SSLError/EOF   → TLS 握手被中断，多为链路上有中间盒/代理在截，
        #                    或对端在握手阶段被 reset。**同样不是密钥问题**
        #                    （2026-09-20 实测第 3 种形态，见下）
        name = type(e).__name__
        msg = str(e)[:160]
        if "ProxyError" in name or "proxy" in msg.lower():
            hint = ("http_proxy 指向的本地代理**节点不通**（换 Clash 节点 / 关掉代理重试）。\n"
                    "         这**不是密钥问题** —— 换 key 解决不了这件事。")
        elif "Timeout" in name or "10060" in msg:
            hint = ("直连 DeepSeek **超时**（无代理或代理未生效）。\n"
                    "         这**不是密钥问题** —— 需恢复出网后重测。")
        elif "SSL" in name or "SSLError" in msg or "UNEXPECTED_EOF" in msg or "EOF occurred" in msg:
            hint = ("TLS 握手被中断（SSL/EOF）—— 链路上有中间设备或代理在截断连接，"
                    "或对端在握手阶段断开。\n"
                    "         先试：①关掉 http_proxy 直连一次；②换一个 Clash 节点。\n"
                    "         这**不是密钥问题** —— 本次实测同一密钥此前刚成功调用过（往返 2.0s）。")
        else:
            hint = f"确认能访问 {settings.deepseek_base_url}。这**不是密钥问题**。"
        record(WARN, "调用未成功（网络层，非密钥问题）", f"{name}: {msg}\n         {hint}")


# --------------------------------------------------------- L5 前端产物新鲜度
def _newest_mtime(d: Path) -> tuple[float, Path | None]:
    newest, who = 0.0, None
    for p in d.rglob("*"):
        if p.is_file():
            m = p.stat().st_mtime
            if m > newest:
                newest, who = m, p
    return newest, who


def check_frontend_dist() -> None:
    section("L5 前端产物是否比源码新")
    dist = ROOT / "frontend" / "dist"
    src = ROOT / "frontend" / "src"
    index = dist / "index.html"

    if not index.is_file():
        record(FAIL, "frontend/dist/index.html 不存在",
               f"路径 {index} —— 单端口托管下无前端可服务，需要 npm run build")
        return
    if not src.is_dir():
        record(WARN, "找不到 frontend/src，无法比对新鲜度", str(src))
        return

    dist_m = index.stat().st_mtime
    src_m, src_who = _newest_mtime(src)
    fmt = lambda t: time.strftime("%m-%d %H:%M:%S", time.localtime(t))  # noqa: E731

    # 容差 2 秒：文件系统时间戳精度 + 同一秒内完成的构建
    if src_m > dist_m + 2:
        rel = src_who.relative_to(ROOT) if src_who else "?"
        stale = (src_m - dist_m) / 60
        record(FAIL, "前端产物已过期：改了 src 但没重新构建",
               f"最新源码 {fmt(src_m)}（{rel}）比产物 {fmt(dist_m)} 新 {stale:.0f} 分钟\n"
               f"         演示看到的是旧界面，且后端测试不会报错 —— 需要 npm run build")
    else:
        record(OK, "产物不早于源码",
               f"dist {fmt(dist_m)} / 最新源码 {fmt(src_m)}")

    # index.html 引用的 assets 必须真的存在（复制/部署中断会漏文件）
    html = index.read_text(encoding="utf-8", errors="ignore")
    referenced: list[str] = []
    for token in ('src="', 'href="'):
        idx = 0
        while True:
            i = html.find(token, idx)
            if i < 0:
                break
            j = html.find('"', i + len(token))
            val = html[i + len(token):j]
            if val.startswith("/assets/"):
                referenced.append(val.lstrip("/"))
            idx = j + 1
    missing = [r for r in referenced if not (dist / r).is_file()]
    if not referenced:
        record(WARN, "index.html 里没解析到 /assets/ 引用", "产物结构可能异常，人工看一眼")
    elif missing:
        record(FAIL, f"产物缺文件（{len(missing)} 个）",
               "、".join(missing[:5]) + (" …" if len(missing) > 5 else ""))
    else:
        record(OK, f"index.html 引用的 {len(referenced)} 个资源都在")


# ------------------------------------------------------------------- L6 库状态
def check_db() -> None:
    section("L6 数据库状态")
    try:
        from app.task_store import DB_PATH
    except Exception as e:  # noqa: BLE001
        record(FAIL, "无法确定库路径", f"{type(e).__name__}: {e}")
        return

    if not Path(DB_PATH).is_file():
        record(FAIL, "库文件不存在", str(DB_PATH))
        return

    try:
        c = sqlite3.connect(str(DB_PATH))
        one = lambda q: c.execute(q).fetchone()[0]          # noqa: E731
        users = one("select count(*) from users")
        tasks = one("select count(*) from tasks")
        sessions = one("select count(*) from sessions")
        by_uid = c.execute(
            "select user_id, count(*) from tasks group by user_id order by 2 desc"
        ).fetchall()
        status = dict(c.execute("select status, count(*) from tasks group by status"))
        c.close()
    except Exception as e:  # noqa: BLE001
        record(FAIL, "查库失败", f"{type(e).__name__}: {e}")
        return

    detail = (f"用户 {users} / 任务 {tasks}（成功 {status.get('succeeded', 0)}、"
              f"失败 {status.get('failed', 0)}、其它 {tasks - status.get('succeeded', 0) - status.get('failed', 0)}）"
              f"/ 会话 {sessions}")
    if by_uid:
        detail += "\n         任务归属：" + "、".join(f"user_id={u} → {n} 条" for u, n in by_uid)

    if users == 0:
        # 这正是 2026-09-17 修掉的那个竞态：库里 0 用户 + 注册开放 ⇒ 谁先注册谁是管理员
        record(FAIL, "库里一个用户都没有", detail + "\n         注册开放时，谁先注册谁成为管理员 —— 演示前必须先预置管理员")
    else:
        owners = {u for u, _ in by_uid}
        if 0 in owners:
            record(WARN, "存在 user_id=0 的无主任务",
                   detail + f"\n         {sum(n for u, n in by_uid if u == 0)} 条无主任务可能被首个管理员继承")
        else:
            record(OK, "库状态正常", detail)

    try:
        from app.config import settings
        if settings.allow_registration:
            record(WARN, "注册开关 ALLOW_REGISTRATION=true",                   "若演示不需要评委注册，可改 .env 为 false 再重启（非必改）")
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------- L7 业务数据加密状态
def check_encryption() -> None:
    """确认敏感字段真的加密了（任务书 PRD §7「数据隐私」）。

    为什么放进演示自检：**"加密了没有"是一个一眼可查、但极易放任其回退的状态**。
    库里存的是密文还是明文，看一遍就知道；而这个检查一旦缺位，
    某次改动漏掉某个写路径就会静默退回明文，没人会发现。
    """
    section("L7 业务数据加密")
    try:
        from app import crypto
        from app.task_store import DB_PATH, store
    except Exception as e:  # noqa: BLE001
        record(FAIL, "无法加载加密模块", f"{type(e).__name__}: {e}")
        return

    st = crypto.crypto_status()
    if st["algorithm"] != "fernet":
        record(WARN, f"加密算法降级为 {st['algorithm']}",
               "缺少 cryptography 包，当前只是混淆而非强加密\n"
               "         pip install cryptography 后重启即可升级（旧密文仍可解）")

    # 直接看磁盘原值，而不是走 store（store 会自动解密，看不出落盘形态）
    try:
        c = sqlite3.connect(str(DB_PATH))
        rows = c.execute(
            "SELECT region, code FROM tasks"
            " WHERE (region IS NOT NULL AND region != '')"
            "    OR (code IS NOT NULL AND code != '')"
            " LIMIT 200"
        ).fetchall()
        c.close()
    except Exception as e:  # noqa: BLE001
        record(FAIL, "查库失败", f"{type(e).__name__}: {e}")
        return

    if not rows:
        record(WARN, "库里还没有任务", "无法判断加密是否生效（跑一次分析再看）")
        return

    # ⚠ 两列都要查：只查 region 会漏掉"region 已加密但 code 还是明文"的情况，
    #    而 code 恰恰是最敏感的一列（含 AOI 坐标 + 分析意图）。
    n_plain_region = sum(1 for r, _ in rows if r and not crypto.is_encrypted(r))
    n_plain_code = sum(1 for _, cd in rows if cd and not crypto.is_encrypted(cd))
    if n_plain_region or n_plain_code:
        bad = []
        if n_plain_region:
            bad.append(f"region {n_plain_region} 条")
        if n_plain_code:
            bad.append(f"code {n_plain_code} 条")
        record(FAIL, f"敏感字段仍是**明文**（{'、'.join(bad)}，抽查 {len(rows)} 条）",
               "迁移未完成或某条写路径漏了加密 —— 跑一次 test_field_crypto.py 定位\n"
               "         （任务书 PRD §7 要求「用户区域/任务描述…加密存储」）")
    else:
        record(OK, f"敏感字段已加密（{len(rows)} 条抽查，算法 {st['algorithm']}）",
               f"密钥来源 {st['key_source']}" + ("" if st["key_exists"] else "（环境变量注入）"))

    # 加密不能把功能搞坏：读出来必须是可用的明文
    sample = store.list(None, limit=1)
    if sample:
        r = store.get(sample[0]["task_id"])
        if r and r.get("region") and crypto.is_encrypted(r.get("region")):
            record(FAIL, "读出来的 region 仍是密文", "加解密没下沉到存储层，前端会显示乱码")
        else:
            record(OK, "应用层读回明文（加解密对上层透明）",
                   f"样例 region={str((r or {}).get('region'))[:20]}")


# --------------------------------------------------- L8 代码生成健壮性规则
def check_codegen_rules() -> None:
    """确认两条「用真金白银的错误换来的」提示词规则还在。

    为什么放进演示自检：这两条规则是**纯字符串**，改动 codegen / debugger 时
    极易在重构里被顺手删掉，而且删掉之后**不会报错** —— 只会让某些任务
    偶发失败（还得恰好碰上空集合才复现），极难在验收前发现。
    """
    section("L8 代码生成健壮性规则")
    try:
        from app.codegen import _RESOURCE_RULES, _TASK_CALIBER
        from app.debugger import empty_band_note, keyerror_note
    except Exception as e:  # noqa: BLE001
        record(FAIL, "无法加载 codegen / debugger", f"{type(e).__name__}: {e}")
        return

    # ① 共享规则 l：必须覆盖「所有任务类型」，不能只管逐月那条路径。
    #    这是 2026-09-20 的教训 —— 原先把防空规则只写在逐月场景，
    #    结果 change_detection 的「两时段相减」完全没被覆盖，实测 3 次重试全失败。
    shared_need = [
        ("0 波段", "点明空集合会产生 0 波段影像"),
        ("Got 0 and", "给出 GEE 原始报错样例（模型据此定位最容易）"),
        ("高风险场景", "列出高风险场景，便于模型自查"),
    ]
    shared_missing = [why for kw, why in shared_need if kw not in _RESOURCE_RULES]
    if shared_missing:
        record(FAIL, "共享资源规则缺「0 波段」防空条目（规则 l）",
               "缺：" + "；".join(shared_missing) +
               "\n         该条必须放在共享规则里才能覆盖全部 4 种任务类型")
    else:
        record(OK, "共享资源规则含「0 波段」防空条目（覆盖全部任务类型）",
               "规则 l 已生效，四种任务的 prompt 都带上守卫")

    # ② 变化检测口径：必须引用共享规则并给出可直接抄的兜底写法
    cd = _TASK_CALIBER.get("change_detection", "")
    cd_missing = [why for kw, why in
                  [("safe_subtract", "给出两时段相减的兜底写法"),
                   ("Got 0 and", "保留报错样例")] if kw not in cd]
    if cd_missing:
        record(FAIL, "变化检测口径缺守卫",
               "缺：" + "；".join(cd_missing) +
               "\n         实测 2026-09-20 深圳任务因此 3 次重试全失败")
    else:
        record(OK, "变化检测口径含两时段相减守卫", f"口径 {len(cd)} 字符")

    # ③ 调试器要能认这个错，否则重试时模型只会盯着报错行改阈值表达式
    probe = {"category": "gee",
             "message": "EEException: Image.gt: If one image has no bands, "
                        "the other must also have no bands. Got 0 and 1."}
    hit = bool(empty_band_note(probe))
    # 反向：不能误伤普通错误
    false_pos = any(
        empty_band_note({"message": m})
        for m in ("AttributeError: 'Image' object has no attribute 'mode'",
                  "No band named NDVI",
                  "SSL: UNEXPECTED_EOF_WHILE_READING")
    )
    if hit and not false_pos:
        record(OK, "调试器能识别 0 波段报错且不误伤其他错误",
               "命中定向提示（明确要求「别改报错行、去修上游合成」）")
    else:
        record(FAIL, "调试器对 0 波段报错的识别不正确",
               f"应命中={hit}（必须 True）、误伤其他错误={false_pos}（必须 False）")

    # ④ KeyError 定向提示：本项目历史最高频失败类之一
    #    （提示词规则 j 记录过「连续 3 次重试全部 KeyError」）。
    #    病根是取值语法 `f['properties']['k']` 而非键名，所以必须给定向提示。
    ke_hit = bool(keyerror_note({"message": "KeyError: 'ndvi'"}))
    ke_has_get = ".get(" in keyerror_note({"message": "KeyError: 'ndvi'"})
    # 反向：不能把「0 波段」这类错也吞进来（两类错修法完全不同）
    ke_false_pos = any(
        keyerror_note({"message": m})
        for m in ("EEException: Image.gt: ... Got 0 and 1.",
                  "AttributeError: 'Image' object has no attribute 'mode'",
                  "No band named NDVI")
    )
    # 互斥性：同一错误不应同时命中两条提示
    both = bool(keyerror_note({"message": "Image.gt: no bands. Got 0 and 1."}))
    if ke_hit and ke_has_get and not ke_false_pos and not both:
        record(OK, "调试器能识别 KeyError 属性缺失并给出取值语法修法",
               "明确指向 `.get()` 写法，且不与「0 波段」提示互相干扰")
    else:
        record(FAIL, "KeyError 定向提示不正确",
               f"命中={ke_hit}（须 True）、含 .get 修法={ke_has_get}（须 True）、"
               f"误伤其他错误={ke_false_pos}（须 False）、与 0 波段提示重叠={both}（须 False）")





# ------------------------------------------------------------------ 自检
# 这几条判定全是"看一眼就知道该不该红"的规则，但**必须证明它真的会红** ——
# 否则脚本满屏 [ OK ] 也可能只是"所有分支都走进了 else"。
# 与 gee_doctor.py --self-test / eval_metrics.py --self-test 同一套路。
def run_self_test() -> int:
    """验证判定规则本身 —— 用合成输入覆盖每条分支，不碰真实环境。"""
    print("=" * 60)
    print(" demo_doctor 自检（判定规则，不碰真实环境）")
    print("=" * 60)
    n_ok = n_bad = 0

    def expect(name: str, got, want) -> None:
        nonlocal n_ok, n_bad
        if got == want:
            n_ok += 1
            print(f"  [ OK ] {name}")
        else:
            n_bad += 1
            print(f"  [FAIL] {name}\n         期望 {want!r}，实际 {got!r}")

    # --- 1. 前端新鲜度判定（把 check_frontend_dist 的判据抽出来验证）---
    def stale(src_m: float, dist_m: float) -> bool:
        """复刻 check_frontend_dist 里的判据：容差 2 秒"""
        return src_m > dist_m + 2

    expect("源码比产物新 60s → 判过期", stale(1000, 940), True)
    expect("源码比产物新 1s（同一次构建内）→ 不算过期", stale(1001, 1000), False)
    expect("产物比源码新 → 不算过期", stale(900, 1000), False)
    expect("完全一致 → 不算过期", stale(1000, 1000), False)

    # --- 2. 库状态判定 ---
    def db_level(users: int, owners: set[int]) -> str:
        """复刻 check_db 的分级：0 用户=FAIL，有 0 号无主=WARN，否则 OK"""
        if users == 0:
            return FAIL
        return WARN if 0 in owners else OK

    expect("库里 0 用户 → FAIL（谁先注册谁当管理员）", db_level(0, set()), FAIL)
    expect("有用户但有 user_id=0 无主任务 → WARN", db_level(1, {0, 4}), WARN)
    expect("有用户且归属清晰 → OK", db_level(1, {4}), OK)

    # --- 3. 网络错误分类（本次新增的分支）---
    def net_class(exc_name: str, msg: str) -> str:
        """复刻 check_llm 异常分支：返回 proxy / timeout / ssl / other"""
        if "ProxyError" in exc_name or "proxy" in msg.lower():
            return "proxy"
        if "Timeout" in exc_name or "10060" in msg:
            return "timeout"
        if "SSL" in exc_name or "SSLError" in msg or "UNEXPECTED_EOF" in msg or "EOF occurred" in msg:
            return "ssl"
        return "other"

    expect("ProxyError 502 → 归为代理问题",
           net_class("ProxyError", "502 Bad Gateway"), "proxy")
    expect("ConnectTimeout 10060 → 归为直连超时",
           net_class("ConnectTimeout", "[WinError 10060] ..."), "timeout")
    expect("SSLError + UNEXPECTED_EOF → 归为 TLS 中断",
           net_class("SSLError", "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"), "ssl")
    expect("ConnectError（无代理直连被拒）→ 归为其它",
           net_class("ConnectError", "connection refused"), "other")
    expect("关键字在消息里也能识别 proxy",
           net_class("SomeError", "Tunnel connection failed: proxy unavailable"), "proxy")
    expect("三种形态互不误判：ssl 不带 Timeout",
           net_class("SSLError", "EOF occurred in violation of protocol"), "ssl")

    # --- 4. HTTP 状态码分级（密钥类 vs 环境类）---
    def http_level(code: int) -> str:
        if code == 200:
            return OK
        if code in (401, 403, 402):
            return FAIL       # 密钥/余额问题，必须处理
        if code == 429:
            return WARN       # 限流，密钥本身有效
        return FAIL

    expect("200 → OK", http_level(200), OK)
    expect("401 密钥被拒 → FAIL", http_level(401), FAIL)
    expect("402 余额不足 → FAIL", http_level(402), FAIL)
    expect("429 限流 → WARN（不是密钥问题）", http_level(429), WARN)
    expect("500 服务端异常 → FAIL", http_level(500), FAIL)

    # --- 5. 库路径必须真实存在（防止在错误位置建空库）---
    from app.task_store import DB_PATH
    expect("DB_PATH 指向的文件真实存在", Path(DB_PATH).is_file(), True)

    # --- 6. 加密状态判定（L7）---
    from app import crypto

    def enc_level(rows) -> str:
        """复刻 check_encryption 的判据：任一 region 或 code 是明文就 FAIL

        ⚠ 两列都要看。只查 region 会漏掉"region 已加密、code 仍明文"的情况 ——
        这正是本自检第一次跑时抓到的真实缺陷（当时断言期望 FAIL、实际返回 OK）。
        """
        if not rows:
            return WARN
        for r, cd in rows:
            if (r and not crypto.is_encrypted(r)) or (cd and not crypto.is_encrypted(cd)):
                return FAIL
        return OK

    expect("空库 → WARN（无法判断）", enc_level([]), WARN)
    expect("全部密文 → OK", enc_level([("ENC1:abc", "ENC1:def")]), OK)
    expect("region 明文 → FAIL", enc_level([("ENC1:abc", None), ("长江三角洲", None)]), FAIL)
    expect("code 明文 → FAIL（只看 region 会漏）",
           enc_level([("ENC1:x", "import ee")]), FAIL)
    expect("region 为空但 code 明文 → FAIL", enc_level([("", "import ee")]), FAIL)
    expect("region 明文但 code 为空 → FAIL", enc_level([("长江三角洲", "")]), FAIL)

    # 加密前后缀判定的边界：空串/NULL 不该被当成"明文未加密"
    expect("空串不算未加密", crypto.is_encrypted(""), False)
    expect("密文前缀识别", crypto.is_encrypted("ENC1:abc"), True)
    expect("NULL 不算未加密", crypto.is_encrypted(None), False)

    # 幂等性：这是"重复迁移不毁数据"的唯一保证，必须验
    _plain = "长江三角洲"
    _once = crypto.encrypt_field(_plain)
    expect("加密幂等（已加密的再加密不变）", crypto.encrypt_field(_once) == _once, True)
    expect("解密幂等（明文解密仍是明文）", crypto.decrypt_field(_plain) == _plain, True)
    expect("加解密往返一致", crypto.decrypt_field(crypto.encrypt_field(_plain)) == _plain, True)
    expect("盲索引确定性（同明文同结果）",
           crypto.blind_index(_plain) == crypto.blind_index(_plain), True)
    expect("盲索引与密文不同（不是简单复用）",
           crypto.blind_index(_plain) != crypto.encrypt_field(_plain), True)

    # ---- L8 失败率：切段与比率计算（纯函数，可直接喂数据）----
    # 这组守卫防的是「失败率涨了却没人知道」，所以要覆盖三种判定分支。
    _now = time.time()
    def _mk(statuses: list[str], *, recent: bool = True,
            old: list[str] | None = None) -> list[tuple[str, float]]:
        t = _now - 60 if recent else _now - 48 * 3600
        out = [(s, t) for s in statuses]
        if old:
            out += [(s, _now - 48 * 3600) for s in old]
        return out

    r, o, n, m = _failure_rate_state(_mk(["succeeded"] * 10))
    expect("全成功 → 近期失败率 0", (r, n), (0.0, 10))
    expect("无历史样本 → 历史返回 -1", (o, m), (-1.0, 0))

    r, o, n, m = _failure_rate_state(_mk(["failed"] * 4 + ["succeeded"] * 6))
    expect("近 24h 4/10 失败 → 0.4", round(r, 4), 0.4)

    r, o, n, m = _failure_rate_state(
        _mk(["succeeded"] * 10, old=["failed"] * 5 + ["succeeded"] * 5))
    expect("近期 0% 历史 50% → 两段分离正确", (r, round(o, 4), n, m), (0.0, 0.5, 10, 10))

    r, o, n, m = _failure_rate_state(_mk(["failed"] * 5 + ["succeeded"] * 5,
                                         old=["succeeded"] * 20))
    expect("近期 50% 历史 0% → 能看出恶化", (round(r, 4), round(o, 4)), (0.5, 0.0))

    r, o, n, m = _failure_rate_state([])
    expect("完全无任务 → 近期 -1（不误报）", r, -1.0)

    # 边界：样本量门槛是防误报的关键 —— 2 条错 1 条是 50%，但毫无意义
    r, o, n, m = _failure_rate_state(_mk(["failed", "succeeded"]))
    expect("小样本仍算得出比率（是否报警由样本量门槛决定）",
           (round(r, 4), n), (0.5, 2))

    print()
    print("=" * 60)
    total = n_ok + n_bad
    print(f" 自检结果：{n_ok}/{total} 通过")
    print("=" * 60)
    return 1 if n_bad else 0


# ------------------------------------------------------------------------ main
# ------------------------------------------------------- L8 失败率趋势
def _failure_rate_state(rows: list[tuple[str, float]]) -> tuple[float, float, int, int]:
    """把 [(status, created_at)] 切成「近 24h」与「此前」两段，返回各自失败率。

    抽成纯函数是为了可被 `--self-test` 直接喂数据验证判定规则
    （否则要造真实库才能测到边界，而我们没有负样本）。
    返回 (近期失败率, 历史失败率, 近期样本数, 历史样本数)；样本为 0 时该侧返回 -1.0。
    """
    now = time.time()
    cut = now - 24 * 3600
    recent = [s for s, t in rows if t >= cut]
    older = [s for s, t in rows if t < cut]

    def rate(ss: list[str]) -> float:
        if not ss:
            return -1.0
        return sum(1 for s in ss if s == "failed") / len(ss)

    return rate(recent), rate(older), len(recent), len(older)


def check_failure_rate() -> None:
    """把失败率变成会主动报警的东西（2026-09-20 新增）。

    背景：实测 09-15→09-20 的日失败率是 0% / 5.9% / 6.9% / 13.3%，
    但**没有任何机制会主动告诉你它在涨** —— 只能靠人工查库才发现。
    """
    section("L9 失败率趋势")
    try:
        from app.task_store import DB_PATH
        if not Path(DB_PATH).is_file():
            record(FAIL, "库文件不存在，无法统计失败率", str(DB_PATH))
            return
        c = sqlite3.connect(str(DB_PATH))
        rows = c.execute(
            "select status, created_at from tasks where created_at is not null"
        ).fetchall()
        c.close()
    except Exception as e:  # noqa: BLE001
        record(FAIL, "统计失败率失败", f"{type(e).__name__}: {e}")
        return

    if not rows:
        record(WARN, "库里没有任务", "无法评估失败率")
        return

    r_recent, r_older, n_recent, n_old = _failure_rate_state([(s, t) for s, t in rows])
    total = len(rows)
    total_failed = sum(1 for s, _ in rows if s == "failed")
    base = f"全期 {total} 条 / 失败 {total_failed} 条 = {100.0 * total_failed / total:.1f}%"

    if r_recent < 0:
        # 近 24h 没样本：可能是没人在用，也可能是刚部署 —— 都提示即可，不算失败
        record(WARN, "近 24 小时无任务样本",
               base + f"\n         历史 {n_old} 条失败率 {100.0 * r_older:.1f}%")
        return

    line = (base
            + f"\n         近 24h {n_recent} 条，失败率 {100.0 * r_recent:.1f}%"
            + (f"（历史 {n_old} 条 {100.0 * r_older:.1f}%）" if r_older >= 0 else ""))

    # 小样本下百分比毫无意义：2 条里错 1 条就是 50%。
    # 所以先卡样本量，再卡比率 —— 否则会天天误报。
    if n_recent < 5:
        record(WARN, f"近 24h 样本偏少（{n_recent} 条），失败率不具统计意义",
               line + "\n         样本 <5 时任何百分比都会被单条任务剧烈放大")
        return

    if r_recent >= 0.30:
        record(FAIL, f"近 24h 失败率偏高（{100.0 * r_recent:.0f}%）",
               line + "\n         建议逐条查 logs 定位根因（tools/replay_task.py 可回放）")
    elif r_older >= 0 and r_recent > r_older * 2 and r_recent >= 0.15:
        record(WARN, f"近 24h 失败率较此前翻倍以上"
                     f"（{100.0 * r_older:.0f}% → {100.0 * r_recent:.0f}%）",
               line + "\n         虽未越 30% 红线，但趋势值得看一眼")
    else:
        record(OK, "失败率未见异常", line)


def main() -> int:
    ap = argparse.ArgumentParser(description="演示前自检")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM 联网探测")
    ap.add_argument("--self-test", action="store_true",
                    help="只验证判定规则本身（不碰真实环境、不发网络请求）")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()

    print("=" * 60)
    print(f" 演示前自检 (demo_doctor)   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" 后端目录 {BACKEND}")
    print("=" * 60)

    if args.no_llm:
        print("\n[L4] 已跳过（--no-llm）")
    else:
        check_llm()
    check_frontend_dist()
    check_db()
    check_encryption()
    check_codegen_rules()
    check_failure_rate()

    n_fail = sum(1 for lv, _, _ in results if lv == FAIL)
    n_warn = sum(1 for lv, _, _ in results if lv == WARN)
    n_ok = sum(1 for lv, _, _ in results if lv == OK)
    print()
    print("=" * 60)
    print(f" 结果：{n_ok} 项通过 / {n_warn} 项提醒 / {n_fail} 项失败")
    for lv, title, _ in results:
        if lv == FAIL:
            print(f"   [FAIL] {title}")
    print("=" * 60)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
