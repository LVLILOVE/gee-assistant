"""SPA 兜底路由与静态托管的安全断言（ASGI 内调用，不起服务、不碰生产库）。

这个文件是为了钉住一个真实存在过的洞：

    @app.get("/{full_path:path}")
    def spa(full_path):
        candidate = FRONTEND_DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)      # ← 只判了 is_file()

`full_path` 来自 URL。uvicorn 会把 `%2e` 解码成 `.`，于是

    GET /%2e%2e/%2e%2e/backend/.env

送进 Starlette 时 path 就是 `/../../backend/.env`，与 FRONTEND_DIST 拼接后
指到 dist 之外，is_file() 为真 —— 匿名访客可以直接拿到：

  · backend/app/*.py       后端源码
  · backend/.env           DEEPSEEK_API_KEY 等凭据
  · backend/data/app.db    全部用户、密码哈希、会话 token、任务

本条路由**没有任何鉴权依赖**，而服务是通过 cpolar 挂在公网的，所以这等于
「任何人拿到链接就能把整份配置和数据库拖走」。已修：resolve() 后必须仍在
dist 之内，带 `..` 的路径直接 404。

为什么用裸 ASGI 调用而不是 TestClient：httpx 可能自己把 `/%2e%2e/` 归一化掉，
那样测试会**假通过**（请求根本没带着越界路径到达应用）。这里手搓 scope，
保证送进 Starlette 的就是攻击者真正能发出来的那个 path。

跑法：
    .venv/Scripts/python.exe tools/test_static_route.py
"""
import asyncio
import os
import sys
import tempfile
import time

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

# 必须在 import app.* 之前设好：main.py 在**导入时**就会初始化 store 并清理僵尸任务，
# 不隔离就会动到生产库 data/app.db。
_tmp = os.path.join(tempfile.gettempdir(), "static_route_%d" % time.time())
os.makedirs(_tmp, exist_ok=True)
os.environ["APP_DB_PATH"] = os.path.join(_tmp, "app.db")
os.environ["EXECUTION_BACKEND"] = "offline"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["GEE_PROXY"] = ""
os.environ["GEE_PROJECT"] = ""

from app.main import FRONTEND_DIST, app  # noqa: E402

ok = []


def check(name, cond, extra=""):
    ok.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))


def skip(name, why):
    print("  SKIP  " + name + "  -> " + why)


def raw_get(path):
    """直接把 path 塞进 ASGI scope，绕过任何客户端的 URL 归一化。

    返回 (status, headers dict, body bytes)。path 应当是**解码后**的形式 ——
    uvicorn 在构造 scope 之前就会解百分号转义，所以
    `/%2e%2e/%2e%2e/backend/.env` 与 `/../../backend/.env`
    到达路由时是同一个字符串。这一点由 smoke_live.sh 的 HTTP 层用例交叉验证。
    """
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        messages.append(m)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", b"*/*")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, body


# 越界响应里绝不能出现的字节。前两条取自真实文件的首行，
# 第三条是 SQLite 文件头 magic —— 只要它出现在响应体里，就是库被读走了。
LEAK_MARKERS = [
    b"SQLite format 3",
    b"DEEPSEEK_API_KEY",
    b"from pathlib import Path",   # app/config.py 的首行内容
    b"ADMIN_PASSWORD",
]
# 越界响应也不该带这些 content-type
LEAK_CTYPES = ("text/x-python", "application/octet-stream", "application/x-sqlite3")

print("=== 0. 对照：确认目标文件真的在磁盘上 ===")
# 没有这组对照，第 3 组的 404 可能只是「文件本来就不存在」，说明不了拦截生效。
for rel in (".env", "app/config.py", "data/app.db"):
    p = os.path.join(BACKEND_DIR, rel)
    check("对照文件存在：backend/%s" % rel, os.path.isfile(p), "不存在则本文件结论无意义")
check("对照：backend/.env 确实含敏感键名（证明它值得被拦）",
      b"DEEPSEEK_API_KEY" in open(os.path.join(BACKEND_DIR, ".env"), "rb").read())

print("\n=== 1. 正常路径必须照旧可用 ===")
st, hd, body = raw_get("/")
check("GET / 返回 200", st == 200, st)
check("GET / 是 HTML", "text/html" in hd.get("content-type", ""), hd.get("content-type"))
check("GET / 返回的是前端外壳", b'id="root"' in body)

st, hd, body = raw_get("/health")
check("GET /health 返回 200", st == 200, st)
check("GET /health 是 JSON", "application/json" in hd.get("content-type", ""), hd.get("content-type"))
check("GET /health 内容正确", b'"status"' in body)

st, hd, body = raw_get("/api/tasks/types")
check("GET /api/tasks/types 返回 200 且是 JSON", st == 200 and "application/json" in hd.get("content-type", ""),
      "%s %s" % (st, hd.get("content-type")))

st, hd, body = raw_get("/api/regions")
check("GET /api/regions 返回 200 且是 JSON", st == 200 and "application/json" in hd.get("content-type", ""),
      "%s %s" % (st, hd.get("content-type")))

# 前端单页路由：不存在的「页面」路径应当仍然回退到 index.html
st, hd, body = raw_get("/some/deep/spa/route")
check("前端路由回退仍然有效（/some/deep/spa/route -> 200 HTML）",
      st == 200 and "text/html" in hd.get("content-type", ""), "%s %s" % (st, hd.get("content-type")))

# dist 内的真实静态文件应当照常 200
assets_dir = FRONTEND_DIST / "assets"
if assets_dir.is_dir():
    names = sorted(n for n in os.listdir(assets_dir) if os.path.isfile(assets_dir / n))
    if names:
        st, hd, body = raw_get("/assets/" + names[0])
        check("GET /assets/%s 返回 200" % names[0], st == 200, st)
        check("静态资源不是被 SPA 外壳顶掉", "text/html" not in hd.get("content-type", ""),
              hd.get("content-type"))
    else:
        skip("assets 目录下无文件", "构建产物可能未生成")
else:
    skip("assets 目录不存在", "未构建前端，跳过静态资源用例")

print("\n=== 2. 接口路径打错时必须是 JSON 404，不能吐 HTML ===")
for p in ("/api/nope-xyz", "/api/task", "/api/tasks/does-not-exist/x"):
    st, hd, body = raw_get(p)
    check("GET %-30s -> 404" % p, st == 404, st)
    check("GET %-30s 是 JSON 而不是 HTML" % p,
          "application/json" in hd.get("content-type", ""), hd.get("content-type"))
    check("GET %-30s 不含前端外壳" % p, b'id="root"' not in body)

print("\n=== 3. 路径穿越必须被拦（本次修复的核心）===")
# 解码后的等价形式。uvicorn 先解码 `%2e%2e`，所以攻击面就是这些字符串。
TRAVERSAL_PATHS = [
    "/../../backend/.env",
    "/../../backend/app/config.py",
    "/../../backend/data/app.db",
    "/../../backend/.db_backup",                 # 备份目录（含用户表）
    "/../../../.workbuddy/MEMORY.md",            # 再往上一层
    "/static/../../../backend/.env",
    "/assets/../../backend/.env",                # 走 /assets 挂载点绕
    "/assets/../../backend/data/app.db",
]
for p in TRAVERSAL_PATHS:
    st, hd, body = raw_get(p)
    check("越界 %-36s -> 404" % p, st == 404, st)
    leaked = [m.decode() for m in LEAK_MARKERS if m in body]
    check("越界 %-36s 响应体无敏感内容" % p, not leaked, "泄漏标记: %s" % leaked)
    check("越界 %-36s content-type 不是文件类型" % p,
          not any(c in hd.get("content-type", "") for c in LEAK_CTYPES), hd.get("content-type"))

print("\n=== 4. 换个编码再打一遍（%2e / %2f / 大小写 / 双写）===")
# 这些变体经 uvicorn 解码后仍应落到同一个越界判定上。
# 注意：raw_get 传的是**解码后**的 path，所以这里验证的是等价类；
# 端到端（编码形式真的穿过 HTTP 栈）由 smoke_live.sh 覆盖。
ENCODED_EQUIV = [
    "/../../backend/.env",       # /%2e%2e/%2e%2e/backend/.env
    "/../../backend/.env",       # /..%2f..%2fbackend%2f.env  -> 同上
    "/../../BACKEND/.env",       # 大小写（Windows 文件系统不区分大小写）
]
for p in ENCODED_EQUIV:
    st, hd, body = raw_get(p)
    check("等价编码 %-32s -> 404" % p, st == 404, st)
    check("等价编码 %-32s 无敏感内容" % p, not any(m in body for m in LEAK_MARKERS))

# %00 截断：路径含 NUL 必须被拒（_safe_dist_file 里有显式判断）
st, hd, body = raw_get("/index.html\x00.png")
check("路径含 NUL 字节 -> 不返回敏感文件", not any(m in body for m in LEAK_MARKERS), st)

print("\n=== 5. 用真实 HTTP 栈复核（本机 8010，若在跑）===")
try:
    import json
    import os
    import urllib.error
    import urllib.request

    # ⚠️ 必须绕开本机代理：http_proxy 指向 127.0.0.1:xxxx，当 8010 没在跑时
    # 代理会回 **502 Bad Gateway** —— 那是一个真实的 HTTP 状态码，不是连接异常，
    # 于是会被下面判成"服务返回了 502"（FAIL），而真实情况只是"服务没启动"。
    # 2026-09-20 实测踩到：本套件在服务未运行时假失败 2 项。
    # 处置：local 请求走 no-proxy handler；并在判定前排除代理自身产生的网关错误。
    _no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def http_probe(url):
        req = urllib.request.Request(url)
        try:
            with _no_proxy.open(req, timeout=10) as r:
                return r.status, r.headers.get("content-type", ""), r.read(4096)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("content-type", ""), e.read(4096)
        except Exception as e:  # 网络不通
            return None, "", str(e).encode()

    # 网关类状态码：只有代理/中间层才会产生，本应用从不返回它们。
    # 见到它们说明"探测被中间层截胡"，不能算被测系统的行为。
    GATEWAY_CODES = (502, 503, 504)
    PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")

    st, ct, body = http_probe("http://127.0.0.1:8010/health")
    if st is None:
        skip("HTTP 层复核", "本机 8010 未在运行")
    elif st in GATEWAY_CODES and b"Bad Gateway" in body:
        # 服务可能其实没跑，但请求被代理接走了 —— 这是测量环境问题，不是产品问题。
        used = [v for v in PROXY_VARS if os.environ.get(v)]
        skip("HTTP 层复核",
             "8010 未在运行且请求被中间代理接走（HTTP %s）；"
             "设了 %s —— 这是环境问题，不代表静态路由失败" % (st, "、".join(used) or "代理变量"))
    else:
        check("HTTP: GET /health -> 200 JSON", st == 200 and "application/json" in ct, "%s %s" % (st, ct))
        # urllib 会自己归一化 `..`，所以这里只能验证「归一化后的路径」不泄漏，
        # 真正带 `%2e%2e` 的原始请求由 smoke_live.sh 用 curl --path-as-is 覆盖。
        st, ct, body = http_probe("http://127.0.0.1:8010/api/nope-xyz")
        check("HTTP: GET /api/nope-xyz -> 404 JSON", st == 404 and "application/json" in ct, "%s %s" % (st, ct))
except Exception as e:  # pragma: no cover
    skip("HTTP 层复核", "无法发起请求：%r" % (e,))

passed = sum(ok)
total = len(ok)
print("\n=== 结果：%d/%d 通过 ===" % (passed, total))

# 清掉临时库，别留垃圾
for suffix in ("", "-wal", "-shm"):
    f = os.path.join(_tmp, "app.db" + suffix)
    if os.path.exists(f):
        try:
            os.remove(f)
        except OSError:
            pass
try:
    os.rmdir(_tmp)
except OSError:
    pass

sys.exit(0 if passed == total else 1)
