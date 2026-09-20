"""参数回显回归守卫（2026-09-20 新增）。

背景：`GET /api/tasks/{tid}` 曾漏投影 `cloud_threshold`，导致
  ① 前端「重新运行」读 `t.cloud_threshold ?? 20` 永远退回默认值；
  ② 报告导出渲染成「云量阈值：%」（空值看着像故障）；
  ③ 用户无法核对自己提交的参数。

本文件锁死这个契约：**提交时填的参数，读回来必须一致。**

设计要点：
- 起**临时库 + 独立端口**的实例（不碰生产库）。
- 用真实 HTTP 走完「注册/登录 → 提交任务 → 读详情 → 读报告」。
- 必须绕代理（本机 HTTP_PROXY 会拦 127.0.0.1，不绕会得到假失败）。

跑法：
    cd backend && env -u PYTHONPATH ./.venv/Scripts/python.exe tools/test_task_echo.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PY = str(BACKEND / ".venv" / "Scripts" / "python.exe")
if not os.path.exists(PY):
    PY = sys.executable

_PASS = 0
_FAIL = 0
_DETAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"[ OK ] {name}")
    else:
        _FAIL += 1
        print(f"[FAIL] {name}  {detail}")
        _DETAIL.append(f"{name} | {detail}")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


#: 必须绕代理：本机 HTTP_PROXY 会把 127.0.0.1 也代理走，返回 502。
#: 不绕代理会得出「服务返 502」的错误结论，并让测试假失败。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method: str, url: str, body: dict | None = None,
        cookie: str | None = None, timeout: int = 20):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with _OPENER.open(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ck = resp.headers.get("Set-Cookie") or ""
            try:
                return resp.status, json.loads(raw), ck
            except json.JSONDecodeError:
                return resp.status, raw, ck
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), ""
        except json.JSONDecodeError:
            return e.code, raw, ""
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", ""


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wb_echo_")
    # ⚠ 必须 Windows 风格绝对路径。传 Git Bash 的 /c/... 会被 Python
    #   resolve 成 C:\c\... → 静默新建空库 → 测试自洽假绿。
    db = str(Path(tmp) / "app.db").replace("\\", "/")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["APP_DB_PATH"] = db
    env["ALLOW_REGISTRATION"] = "true"
    # 强制走离线后端，避免测试依赖外网与 GEE 额度
    env["GEE_BACKEND"] = env.get("GEE_BACKEND", "mock")

    print(f"临时库 {db}")
    print(f"端口   {port}")
    print("-" * 62)

    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # ---- 等启动 ----
        up = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            st, _, _ = req("GET", f"{base}/health", timeout=2)
            if st == 200:
                up = True
                break
            time.sleep(0.5)
        check("服务起来（/health 200）", up, "未在 30s 内就绪")
        if not up:
            return 1

        # ⚠ 立刻断言"库真的建在预期位置"，否则可能落 C:\c\... 造成假绿
        check("临时库文件确实创建在预期路径", os.path.exists(db), db)

        # ---- 注册 + 登录 ----
        user, pwd = "echo_user", "echo_pass_123"
        st, r, _ = req("POST", f"{base}/api/auth/register", {"username": user, "password": pwd})
        check("注册成功", st in (200, 201), f"{st} {r}")
        st, r, ck = req("POST", f"{base}/api/auth/login", {"username": user, "password": pwd})
        check("登录成功并拿到 Cookie", st == 200 and "session" in ck, f"{st} ck={ck[:60]}")
        cookie = ck.split(";")[0] if ck else ""

        # ---- 提交任务：故意用**非默认**云量 37，避免与默认 20 混淆 ----
        payload = {
            "task_type": "ndvi",
            "region": "太湖流域",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "cloud_threshold": 37,
        }
        st, r, _ = req("POST", f"{base}/api/tasks", payload, cookie)
        check("提交任务成功", st == 200 and "task_id" in (r or {}), f"{st} {r}")
        tid = (r or {}).get("task_id", "")
        if not tid:
            return 1

        # ---- 等终态 ----
        # ⚠ 必须带 cookie！漏传时 _own_task_or_404 会把所有请求判为 404，
        #   循环跑满 120 次后 task 里只剩 {"detail": "任务不存在"}，
        #   于是后面所有字段断言全 fail —— 症状极像"字段没投影"，其实是测试脚本的锅。
        #   （2026-09-20 自己踩过：差点去改后端，实际是这里漏传 cookie）
        task = {}
        last_st = 0
        for _ in range(120):
            last_st, task, _ = req("GET", f"{base}/api/tasks/{tid}", cookie=cookie, timeout=10)
            if (task or {}).get("status") in ("succeeded", "failed"):
                break
            time.sleep(1)
        check("轮询详情接口拿到了任务（非 404）",
              last_st == 200 and "task_id" in (task or {}),
              f"HTTP {last_st}, body={str(task)[:120]}")

        # ================= 核心断言 =================
        print("\n--- 核心：参数回显一致性 ---")
        check("详情含 cloud_threshold 字段",
              "cloud_threshold" in (task or {}),
              f"实际字段: {sorted((task or {}).keys())}")
        got = (task or {}).get("cloud_threshold")
        check("cloud_threshold 回显 == 提交值 37",
              got == 37, f"提交 37，读回 {got!r}")
        # 这条是防"退回默认值"的关键：默认是 20，37 != 20
        check("cloud_threshold 未被默认值 20 覆盖", got != 20, f"读回 {got!r}")

        print("\n--- 其余参数回显 ---")
        check("region 回显一致", (task or {}).get("region") == "太湖流域",
              repr((task or {}).get("region")))
        check("start_date 回显一致", (task or {}).get("start_date") == "2024-01-01",
              repr((task or {}).get("start_date")))
        check("end_date 回显一致", (task or {}).get("end_date") == "2024-12-31",
              repr((task or {}).get("end_date")))
        check("task_type 回显一致", (task or {}).get("task_type") == "ndvi",
              repr((task or {}).get("task_type")))

        print("\n--- 列表接口也要带 cloud_threshold（批量重跑依赖它）---")
        st, lst, _ = req("GET", f"{base}/api/tasks", cookie=cookie)
        rows = (lst or {}).get("tasks") or []
        check("列表返回本任务", any(t.get("task_id") == tid for t in rows),
              f"共 {len(rows)} 条")
        row = next((t for t in rows if t.get("task_id") == tid), {})
        check("列表项含 cloud_threshold", "cloud_threshold" in row,
              f"字段: {sorted(row.keys())}")
        check("列表项 cloud_threshold == 37", row.get("cloud_threshold") == 37,
              repr(row.get("cloud_threshold")))
        check("列表项含 start_date/end_date",
              row.get("start_date") == "2024-01-01" and row.get("end_date") == "2024-12-31",
              f"{row.get('start_date')} ~ {row.get('end_date')}")

        print("\n--- 报告导出不能出现空值「云量阈值：%」---")
        st, rep, _ = req("GET", f"{base}/api/tasks/{tid}/report", cookie=cookie)
        check("报告接口 200", st == 200, str(st))
        text = rep if isinstance(rep, str) else json.dumps(rep, ensure_ascii=False)
        check("报告含「云量阈值：37%」", "云量阈值：37%" in text,
              [l for l in text.splitlines() if "云量" in l][:2])
        # 库里该列是 REAL，若不做格式化会渲染成「37.0%」—— 与前端整数口径不一致
        check("报告未出现浮点尾巴「37.0%」", "云量阈值：37.0%" not in text,
              [l for l in text.splitlines() if "云量" in l][:2])
        check("报告未出现空值「云量阈值：%」", "云量阈值：%" not in text,
              "出现空值渲染")

        print("\n--- 隔离性：别人的任务读不到（顺带回归）---")
        st, r, ck2 = req("POST", f"{base}/api/auth/register",
                         {"username": "echo_other", "password": "echo_pass_456"})
        st, r, ck2 = req("POST", f"{base}/api/auth/login",
                         {"username": "echo_other", "password": "echo_pass_456"})
        ck2 = ck2.split(";")[0] if ck2 else ""
        st, r, _ = req("GET", f"{base}/api/tasks/{tid}", cookie=ck2)
        check("他人访问该任务返 404（不返 403）", st == 404, f"实际 {st}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(0.5)
        # 清理临时库（删不掉只警告，不静默）
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            print(f"WARN 临时目录未清理: {tmp} ({e})")

    print("-" * 62)
    # ⚠ 必须以「N/M 通过」收尾：regress_all.sh 用 `grep -oE '[0-9]+/[0-9]+ *通过'`
    #   抓最后一行。写成「N 通过 / M 失败」会解析不到 → 被判成"套件没全绿"，
    #   即使实际全过。（2026-09-20 自己踩过）
    total = _PASS + _FAIL
    if _FAIL:
        print(f"结果：{_PASS}/{total} 通过（失败 {_FAIL} 项）")
        print("失败明细：")
        for d in _DETAIL:
            print("  -", d)
    else:
        print(f"结果：{_PASS}/{total} 通过")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
