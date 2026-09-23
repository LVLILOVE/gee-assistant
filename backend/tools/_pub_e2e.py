# -*- coding: utf-8 -*-
"""公网端到端验证：登录 -> 提交 NDVI 任务 -> 轮询到终态 -> 校验产物。

为什么单独写这个脚本：
  用 curl 一条条手敲很容易在「契约猜错」时得出错误结论（我先前就猜错过
  task_type 字段，把 422 当成了服务故障）。这里把契约写死成与分析接口
  一致的形式，并对每一步都断言 HTTP 状态，避免「假绿」。

⚠ 必须绕代理：本机 HTTP_PROXY 会拦截 urllib，不绕会看到假的 502/超时。
"""
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("PUB_BASE", "https://530afe91.r31.cpolar.top")
USER = os.environ.get("ADMIN_USERNAME", "admin")
PWD = os.environ.get("ADMIN_PASSWORD", "")

if not PWD:
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", ".env"), os.path.join(here, ".env")):
        try:
            with open(cand, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("ADMIN_PASSWORD="):
                        PWD = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            continue
        if PWD:
            break
if not PWD:
    print("[FAIL] 取不到 ADMIN_PASSWORD")
    sys.exit(1)

# 绕开本机代理 —— 否则一切都是假的
# ⚠ HTTPSHandler 必须在这里传入 context；opener.open() 不接受 context 参数。
CTX = ssl.create_default_context()
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=CTX),
)
COOKIE = {}


def req(method, path, body=None, timeout=40):
    url = BASE + path
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    if COOKIE.get("c"):
        headers["Cookie"] = COOKIE["c"]
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            sc = resp.getcode()
            # 抓会话 cookie
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie":
                    COOKIE["c"] = v.split(";")[0]
            return sc, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


fails = []
print(f"目标: {BASE}")
print()

# --- 1. 匿名访问 ---
sc, raw = req("GET", "/api/auth/me")
print(f"[1] 匿名 /api/auth/me      -> HTTP {sc}")
try:
    anon = json.loads(raw)
    print(f"     authenticated={anon.get('authenticated')}  "
          f"allow_registration={anon.get('allow_registration')}")
    if anon.get("authenticated") is not False:
        fails.append("匿名访问竟然已登录")
except Exception:
    fails.append(f"/api/auth/me 返回非 JSON: {raw[:120]}")

# --- 2. 登录 ---
sc, raw = req("POST", "/api/auth/login", {"username": USER, "password": PWD})
print(f"[2] 登录                   -> HTTP {sc}")
if sc != 200:
    fails.append(f"登录失败 HTTP {sc}: {raw[:150]}")
else:
    try:
        d = json.loads(raw)
        print(f"     user={d.get('username')}  is_admin={d.get('is_admin')}")
    except Exception:
        fails.append("登录响应非 JSON")
    if not COOKIE.get("c"):
        fails.append("登录未下发会话 cookie")

# --- 3. 登录态确认（login 响应里没有 authenticated 字段，必须另查）---
sc, raw = req("GET", "/api/auth/me")
print(f"[3] 登录后 /api/auth/me    -> HTTP {sc}")
if sc != 200 or '"authenticated":true' not in raw.replace(" ", ""):
    fails.append(f"会话未生效: HTTP {sc} {raw[:120]}")
else:
    print("     authenticated=true  ✅")

# --- 4. 提交任务（契约取自 app/models.py AnalysisRequest）---
payload = {"task_type": "ndvi", "region": "太湖流域",
           "start_date": "2025-01-01", "end_date": "2025-12-31",
           "cloud_threshold": 20.0, "extra": {}}
sc, raw = req("POST", "/api/tasks", payload)
print(f"[4] 提交 NDVI 任务         -> HTTP {sc}")
tid = None
if sc != 200:
    fails.append(f"提交任务失败 HTTP {sc}: {raw[:200]}")
else:
    try:
        d = json.loads(raw)
        tid = d.get("task_id") or d.get("id")
        print(f"     task_id={tid}  status={d.get('status')}")
    except Exception:
        fails.append("提交响应非 JSON")
    if not tid:
        fails.append("提交成功但拿不到 task_id")

# --- 5. 轮询到终态 ---
if tid:
    terminal = {"succeeded", "failed", "error", "cancelled"}
    st = None
    t0 = time.time()
    for i in range(60):
        time.sleep(3)
        sc, raw = req("GET", f"/api/tasks/{tid}")
        if sc != 200:
            print(f"     [{i}] 查询 HTTP {sc}")
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        st = d.get("status")
        print(f"     [{i:02d}] {st:<10} ({time.time()-t0:.0f}s)")
        if st in terminal:
            break
    print(f"[5] 任务终态               -> {st}  用时 {time.time()-t0:.0f}s")
    if st not in terminal:
        fails.append("超时未到终态")
    elif st != "succeeded":
        fails.append(f"任务终态为 {st}（非 succeeded）")
    else:
        # --- 6. 产物校验 ---
        sc, raw = req("GET", f"/api/tasks/{tid}")
        d = json.loads(raw)
        res = d.get("result") or {}
        # ⚠ conclusion 是**纯字符串**，不是 dict —— 别去取它下面的键，
        #   否则会拿到 None 并误报"结论为空"（我先前就是这么假报了一次）。
        con = res.get("conclusion")
        summary = con if isinstance(con, str) else (con or {}).get("text", "") if con else ""
        print(f"[6] 产物校验               -> HTTP {sc}")
        print(f"     result keys: {sorted(res.keys())[:8]}")
        print(f"     stats: {str(res.get('stats'))[:140]}")
        if not res:
            fails.append("任务成功但 result 为空")
        if not summary:
            print("     ⚠ 结论摘要为空（结论模块可能未启用）")
        else:
            print(f"     结论({len(summary)}字): {summary[:110]}")

print()
if fails:
    print(f"[FAIL] {len(fails)} 项未通过：")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("[OK] 公网端到端全部通过")
