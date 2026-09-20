import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ask, auth, intent, orchestrator, planner, preferences
from .config import settings
from .knowledge import DATASETS, retrieve, search
from .models import AnalysisRequest, TASK_LABELS, TASK_LABELS_BY_VALUE
from .regions import REGION_LIBRARY
from .task_store import store

app = FastAPI(title="卫星遥感影像智能分析助手", version="0.1.0")

# 来源白名单，不是 "*"。生产前端由本服务同源托管，不需要 CORS；
# 列出的只是「开发时跑在 5173 的前端」。理由见 config.py 的 cors_origins 注释。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

SESSION_COOKIE = "gee_session"


# ---- 访问控制 ----
#
# 服务经 cpolar 暴露在公网，若不做鉴权，任何人都能提交任务、烧掉 DeepSeek 与 GEE 额度。
# 会话用服务端 token（而非 JWT），注销即失效；cookie 走 HttpOnly，脚本读不到。


def _is_https(request: Request) -> bool:
    """判断外部访问是否走 https。

    cpolar 是 https 入口但回源到本机 http，所以不能只看 request.url.scheme，
    要认反代给的 X-Forwarded-Proto。本地 http 调试时 Secure 必须为假，否则
    cookie 根本存不下来。
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def _token_from(request: Request) -> str | None:
    """优先读 cookie；同时支持 Authorization: Bearer，方便 curl 与自动化验证。"""
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        return tok
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def current_user_optional(request: Request) -> dict | None:
    if not settings.auth_enabled:
        return {"id": 0, "username": "(鉴权未启用)", "is_admin": True}
    return auth.store.resolve_session(_token_from(request))


def require_user(user: dict | None = Depends(current_user_optional)) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    return user


def _owner(user: dict) -> int | None:
    """把登录用户转成数据归属 ID。

    **鉴权关闭时返回 `None`**，语义是"不按用户过滤" —— 保持单机自用时的原有行为，
    否则关掉 AUTH_ENABLED 后老数据会全部看不见。
    """
    return None if not settings.auth_enabled else user["id"]


def _adopt_legacy_data() -> int:
    """把鉴权上线前产生的无主任务/区域划归管理员（最早注册的用户）。

    幂等：只动 `user_id = 0`（无主）的行。跑完打一行日志，方便确认到底接管了多少。
    """
    try:
        uid = auth.store.first_admin_id()
        if uid is None:
            return 0
        changed = store.backfill_owner(uid)
        if changed:
            print(f"[数据归属] 已把 {changed} 条历史任务划归管理员（user_id={uid}）")
        return changed
    except Exception as e:  # noqa: BLE001
        print(f"[数据归属] 历史数据接管失败（不影响启动）：{type(e).__name__}: {e}")
        return 0


def _issue_session(request: Request, user: dict) -> JSONResponse:
    ttl = max(1, settings.session_ttl_hours) * 3600
    token = auth.store.create_session(user["id"], ttl)
    resp = JSONResponse({"ok": True, "username": user["username"], "is_admin": bool(user.get("is_admin"))})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )
    return resp


class AuthReq(BaseModel):
    username: str
    password: str


@app.get("/api/auth/me")
def auth_me(request: Request):
    """未登录返回 200 + authenticated=false。

    刻意不用 401：前端每次进入都要问一次，返 401 会在控制台刷一片红，
    真正的报错会被淹掉。
    """
    if not settings.auth_enabled:
        return {"auth_enabled": False, "authenticated": True, "username": "", "allow_registration": False}
    user = auth.store.resolve_session(_token_from(request))
    return {
        "auth_enabled": True,
        "authenticated": user is not None,
        "username": user["username"] if user else "",
        "is_admin": bool(user.get("is_admin")) if user else False,
        "allow_registration": settings.allow_registration,
    }


@app.post("/api/auth/register")
def auth_register(req: AuthReq, request: Request):
    if not settings.auth_enabled:
        raise HTTPException(status_code=400, detail="当前未启用鉴权，无需注册")
    if not settings.allow_registration and auth.store.count_users() > 0:
        raise HTTPException(status_code=403, detail="本服务已关闭自助注册")
    try:
        user = auth.store.create_user(req.username, req.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    if user.get("is_admin"):
        # 首个用户即管理员：顺手接管鉴权上线前的无主数据
        _adopt_legacy_data()
    return _issue_session(request, user)


@app.post("/api/auth/login")
def auth_login(req: AuthReq, request: Request):
    if not settings.auth_enabled:
        raise HTTPException(status_code=400, detail="当前未启用鉴权，无需登录")
    try:
        user = auth.store.verify_login(req.username, req.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    return _issue_session(request, user)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.store.delete_session(_token_from(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


class PasswordReq(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/password")
def auth_password(req: PasswordReq, user: dict = Depends(require_user)):
    """修改密码。成功后该用户全部会话失效，需要重新登录。"""
    try:
        auth.store.change_password(user["id"], req.old_password, req.new_password)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    resp = JSONResponse({"ok": True, "message": "密码已更新，请重新登录"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _seed_admin() -> None:
    """按 .env 预置管理员，消除"谁先注册谁是管理员"的竞态。"""
    if not settings.auth_enabled:
        return
    if not (settings.admin_username and settings.admin_password):
        return
    import logging

    result = auth.store.ensure_admin(settings.admin_username, settings.admin_password)
    logging.getLogger("uvicorn.error").info(
        "[auth] 预置管理员 %r -> %s", settings.admin_username, result
    )


_seed_admin()
_adopt_legacy_data()


# ---- 任务配额 ----
#
# 鉴权挡住了"未登录"的人，但挡不住"注册一个账号然后无限提交"。每条任务都要烧
# DeepSeek token 并占用 GEE 云端算力，所以提交口还需要一层配额。
#
# 三个维度：全局并发（保护本机）、单用户并发（防一个人占满）、单用户滑动窗口速率。
# 计数直接查库而不是用内存计数器 —— 服务重启不会把已用额度清零，重新部署绕不过配额。

_submit_lock = threading.Lock()


def _quota_429(msg: str, retry_after: int) -> HTTPException:
    """配额类拒绝统一返回 429，并给出 Retry-After（秒）。"""
    return HTTPException(status_code=429, detail=msg, headers={"Retry-After": str(retry_after)})


def _ensure_task_quota(owner: int | None) -> None:
    """提交前的配额校验，超限抛 429。**调用方必须持有 `_submit_lock`**。

    锁的必要性：`count_active()` 与随后的 `store.create()` 之间若不加锁，
    两个并发提交会同时读到"还差一个名额"然后都放行，配额形同虚设。
    """
    now = time.time()

    running_all = store.count_active()
    if running_all >= settings.max_concurrent_tasks:
        raise _quota_429(
            f"服务繁忙：当前有 {running_all} 个任务在执行"
            f"（全局上限 {settings.max_concurrent_tasks}），请稍后再试",
            30,
        )

    if owner is None:  # 鉴权关闭（单人自用）：只保留全局保护
        return

    mine = store.count_active(owner)
    if mine >= settings.max_concurrent_tasks_per_user:
        raise _quota_429(
            f"你已有 {mine} 个任务在执行"
            f"（每人上限 {settings.max_concurrent_tasks_per_user}），等它们结束再提交",
            30,
        )
    if settings.task_rate_limit_per_hour > 0:
        n = store.count_since(now - 3600, owner)
        if n >= settings.task_rate_limit_per_hour:
            raise _quota_429(
                f"一小时内已提交 {n} 个任务"
                f"（上限 {settings.task_rate_limit_per_hour}），请稍后再试",
                600,
            )
    if settings.task_rate_limit_per_day > 0:
        n = store.count_since(now - 86400, owner)
        if n >= settings.task_rate_limit_per_day:
            raise _quota_429(
                f"24 小时内已提交 {n} 个任务"
                f"（上限 {settings.task_rate_limit_per_day}），请改天再试",
                3600,
            )


def _fail_orphans() -> int:
    """启动时清掉上个进程遗留的"执行中"僵尸任务（幂等，失败不影响启动）。"""
    try:
        n = store.fail_orphans()
        if n:
            print(f"[任务清理] {n} 条上次中断的任务已标记为失败（并释放其并发名额）")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"[任务清理] 僵尸任务清理失败（不影响启动）：{type(e).__name__}: {e}")
        return 0


_fail_orphans()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "backend": settings.execution_backend,
        "llm": settings.deepseek_model,
        "has_key": bool(settings.deepseek_api_key),
    }


@app.get("/api/tasks/types")
def task_types():
    return [{"value": t.value, "label": label} for t, label in TASK_LABELS.items()]


@app.get("/api/gee/status", dependencies=[Depends(require_user)])
def gee_status():
    """GEE 凭据与连通性诊断（凭据脱敏，绝不返回密钥内容）。"""
    from .gee_auth import diagnose

    return diagnose()


@app.get("/api/tile/{z}/{x}/{y}")
def gee_tile(z: str, x: str, y: str, u: str):
    """GEE 瓦片代理。

    国内浏览器**直连不通** earthengine-highvolume.googleapis.com（实测 TCP 层就失败），
    所以栅格图层必须由后端经本机代理转发，前端不需要能访问 Google。

    `u` = 瓦片模板去掉 `/{z}/{x}/{y}` 之后的地址（前端做 encodeURIComponent）。
    """
    from .net_retry import PermanentError
    from .tile_proxy import TileProxyError, build_url, fetch

    try:
        url = build_url(u, z, x, y)
        content, content_type = fetch(url)
    except PermanentError as e:
        raise HTTPException(status_code=400, detail=f"瓦片请求被拒绝：{e}")
    except TileProxyError as e:
        raise HTTPException(status_code=502, detail=f"瓦片获取失败：{e}")
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/basemap/{z}/{x}/{y}")
def basemap_tile(z: str, x: str, y: str):
    """地图底图代理。

    浏览器直连 OpenStreetMap 同样不通（实测 HTTP 000），底图空白会让整个地图面板失效。
    这里 URL 完全由后端拼装，客户端只能传 z/x/y，不存在 SSRF 面。
    """
    from .net_retry import PermanentError
    from .tile_proxy import TileProxyError, fetch_basemap

    try:
        got = fetch_basemap(z, x, y)
    except PermanentError as e:
        raise HTTPException(status_code=400, detail=f"底图请求被拒绝：{e}")
    except TileProxyError as e:
        raise HTTPException(status_code=502, detail=f"底图获取失败：{e}")
    if got is None:
        return Response(status_code=204)  # 远洋等无数据区域，正常留空
    content, content_type = got
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/api/knowledge")
def knowledge_list():
    """数据集知识库全量列表（前端知识库面板用）。"""
    return {
        "total": len(DATASETS),
        "datasets": [
            {
                "id": d["id"],
                "name": d["name"],
                "ee_id": d["ee_id"],
                "resolution": d["resolution"],
                "desc": d["desc"],
                "tasks": d["tasks"],
                "tags": d["tags"],
            }
            for d in DATASETS
        ],
    }


@app.get("/api/knowledge/search")
def knowledge_search(q: str = "", task_type: str = ""):
    """按关键词或任务类型检索数据集。"""
    if task_type:
        return {"datasets": retrieve(task_type, q, top_k=5)}
    return {"datasets": search(q, top_k=10)}


@app.get("/api/regions")
def list_regions(user: dict | None = Depends(current_user_optional)):
    """自定义区域按用户隔离；未登录只能看到内置区域。

    该端点保持公开（内置区域是产品的一部分，登录页不需要它），但**匿名拿不到别人的**。
    """
    builtin = [
        {"name": r["name"], "lon": r["lon"], "lat": r["lat"], "desc": r["desc"], "builtin": True}
        for r in REGION_LIBRARY
    ]
    if user is None:
        return {"regions": builtin}
    custom = [
        {"name": r["name"], "lon": r["lon"], "lat": r["lat"], "desc": r["desc"], "builtin": False}
        for r in store.list_regions(_owner(user))
    ]
    return {"regions": builtin + custom}


class RegionReq(BaseModel):
    name: str
    lon: float
    lat: float
    desc: str = ""


@app.post("/api/regions")
def add_region(req: RegionReq, user: dict = Depends(require_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="区域名不能为空")
    store.add_region(name, req.lon, req.lat, req.desc, _owner(user))
    return {"ok": True, "name": name}


@app.delete("/api/regions/{name}")
def del_region(name: str, user: dict = Depends(require_user)):
    if not store.delete_region(name, _owner(user)):
        raise HTTPException(status_code=404, detail="区域不存在")
    return {"ok": True}


class ParseReq(BaseModel):
    text: str = ""
    partial: dict = Field(default_factory=dict)


@app.post("/api/parse", dependencies=[Depends(require_user)])
def parse_intent(req: ParseReq):
    """自然语言意图解析 + 多轮澄清。"""
    return intent.parse(req.text, req.partial)


class AskReq(BaseModel):
    question: str = ""
    history: list[dict] = Field(default_factory=list)


@app.post("/api/ask")
def ask_expert(req: AskReq, user: dict = Depends(require_user)):
    """Ask 模式：遥感/GEE 专家问答（不生成代码、不执行）。"""
    return ask.ask(req.question, req.history, _owner(user))


class PlanReq(BaseModel):
    text: str = ""


@app.post("/api/plan", dependencies=[Depends(require_user)])
def plan_tasks(req: PlanReq):
    """规划推理：判断需求是否含多个独立目标，含则拆解为多个子任务。"""
    return planner.plan(req.text)


class PrefsReq(BaseModel):
    instructions: str | None = None
    data_source: str | None = None
    output_language: str | None = None
    agent_profile: str | None = None


@app.get("/api/profiles")
def list_profiles(user: dict = Depends(require_user)):
    from .preferences import AGENT_PROFILES

    return {
        "current": preferences.get_preferences(_owner(user)).get("agent_profile", "analyst"),
        "profiles": [
            {"value": k, "label": v["label"], "desc": v["desc"]}
            for k, v in AGENT_PROFILES.items()
        ],
    }


@app.get("/api/preferences")
def get_prefs(user: dict = Depends(require_user)):
    return {"preferences": preferences.get_preferences(_owner(user))}


@app.put("/api/preferences")
def put_prefs(req: PrefsReq, user: dict = Depends(require_user)):
    updates = req.model_dump(exclude_none=True)
    return {"preferences": preferences.save_preferences(updates, _owner(user))}


class SubmitResp(BaseModel):
    task_id: str


@app.post("/api/tasks", response_model=SubmitResp)
def submit(req: AnalysisRequest, user: dict = Depends(require_user)):
    owner = _owner(user)
    # 配额校验与建任务放在同一临界区：分开写会让并发提交同时通过校验（见 _ensure_task_quota）
    with _submit_lock:
        _ensure_task_quota(owner)
        tid = store.create(req, owner)
    threading.Thread(target=_run, args=(tid, req, owner), daemon=True).start()
    return {"task_id": tid}


def _run(tid: str, req: AnalysisRequest, user_id: int | None = None):
    # started_at/finished_at 是任务书量化指标「端到端响应时长」的唯一数据来源。
    # 两者相减是真实执行耗时；created_at→started_at 是排队等待（受并发配额影响）。
    store.update(tid, status="running", started_at=time.time())
    try:
        final = orchestrator.run_analysis(req, user_id)
        store.update(
            tid,
            status=final["status"],
            code=final["code"],
            result=final["result"],
            logs=final["logs"],
            attempts=final["attempts"],
            finished_at=time.time(),
        )
    except Exception as e:  # noqa: BLE001
        store.update(
            tid,
            status="failed",
            logs=[f"服务异常：{type(e).__name__}: {e}"],
            finished_at=time.time(),
        )


def _own_task_or_404(tid: str, user: dict) -> dict:
    """取任务并校验归属。不属于自己时返 404 而不是 403。

    刻意不区分"不存在"与"是别人的"：403 会暴露"这个 ID 确实存在"，
    等于把别人的任务 ID 空间变成一个可探测的信息泄漏面。
    """
    t = store.get(tid)
    owner = _owner(user)
    if not t or (owner is not None and t.get("user_id") != owner):
        raise HTTPException(status_code=404, detail="任务不存在")
    return t


@app.get("/api/tasks")
def list_tasks(limit: int = 50, offset: int = 0, user: dict = Depends(require_user)):
    """任务列表（分页）。

    ⚠ 必须把 `total` / `has_more` 一起返回：原先只返回 `{"tasks": [...]}`，
    而底层 `store.list()` 默认只取 50 条 —— 超过 50 条任务时接口**静默截断**，
    调用方无从判断列表是否完整。2026-09-17 实测：任务涨到 51 条时，
    UI 只显示 50 条，没有任何提示。
    """
    uid = _owner(user)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    tasks = store.list(uid, limit=limit, offset=offset)
    total = store.count(uid)
    return {
        "tasks": tasks,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(tasks) < total,
    }


@app.get("/api/tasks/{tid}")
def get_task(tid: str, user: dict = Depends(require_user)):
    t = _own_task_or_404(tid, user)
    return {
        "task_id": t["task_id"],
        "status": t["status"],
        "attempts": t.get("attempts", 0),
        "code": t.get("code", ""),
        "result": t.get("result"),
        "logs": t.get("logs", []),
        # 前端靠这几个字段定位地图视野（栅格图层没有 geojson，算不出 bounds）
        "region": t.get("region", ""),
        "task_type": t.get("task_type", ""),
        "start_date": t.get("start_date", ""),
        "end_date": t.get("end_date", ""),
        # 2026-09-20 补：此前漏了 cloud_threshold，导致三处问题 ——
        #   ① 前端「重新运行」读 t.cloud_threshold ?? 20 永远退回默认 20；
        #   ② 报告导出显示「云量阈值：%」（空值）；
        #   ③ 用户无法在详情里核对自己当时填的参数。
        # 值本来就在 t 里（store.get 是全列查询），只是没往外投影。
        "cloud_threshold": t.get("cloud_threshold"),
        # 已提交的反馈回填给前端，刷新页面后按钮仍显示选中态
        "feedback": t.get("feedback"),
        "feedback_note": t.get("feedback_note") or "",
    }


class FeedbackReq(BaseModel):
    value: str = Field(description="up 或 down")
    note: str = Field(default="", description="可选的一句话补充")


@app.post("/api/tasks/{tid}/feedback")
def submit_feedback(tid: str, body: FeedbackReq, user: dict = Depends(require_user)):
    """记录用户对本次结果的评价。

    用途：项目至今 0 真实用户，而"产品价值验证"只有用户能证明。
    在结果区加一行极简反馈，把用户验证从"一件要安排的事"变成"默认会发生的事"。

    归属校验走 `_own_task_or_404`（别人的任务返 404）——
    否则知道别人的 task_id 就能给他打差评。
    """
    t = _own_task_or_404(tid, user)
    if body.value not in store.FEEDBACK_VALUES:
        raise HTTPException(status_code=422, detail="反馈取值只能是 up 或 down")
    ok = store.set_feedback(tid, body.value, body.note)
    if not ok:
        raise HTTPException(status_code=422, detail="反馈写入失败（取值非法或任务不存在）")
    return {"ok": True, "task_id": tid, "value": body.value}


@app.get("/api/feedback/summary")
def feedback_summary(user: dict = Depends(require_user)):
    """反馈汇总。同时给出分子与分母 —— 只报"3 个赞"没有意义。"""
    return store.feedback_summary(_owner(user))


@app.get("/api/tasks/{tid}/report")
def report(tid: str, user: dict = Depends(require_user)):
    t = _own_task_or_404(tid, user)
    lines = [
        "# 卫星遥感影像智能分析报告",
        "",
        f"- 任务 ID：{t['task_id']}",
        f"- 任务类型：{TASK_LABELS_BY_VALUE.get(t.get('task_type'), t.get('task_type'))}",
        f"- 分析区域：{t.get('region', '')}",
        f"- 时间范围：{t.get('start_date', '')} ~ {t.get('end_date', '')}",
        # 取不到时写「未记录」而不是留空：`f"...{''}%"` 会渲染成「云量阈值：%」，
        # 看起来像程序出错，读者分不清是"没填"还是"坏了"。
        # 用 :g 而非直接插值 —— 该列在 SQLite 里是 REAL，直接插会渲染成「37.0%」，
        # 而我们对外展示（含前端表单）一律是整数百分比。
        "- 云量阈值：" + (
            f"{t['cloud_threshold']:g}%"
            if t.get("cloud_threshold") is not None
            else "未记录（历史任务或未指定）"
        ),
        f"- 执行状态：{t['status']}（尝试 {t.get('attempts', 0)} 次）",
        "",
        "## 生成的分析代码",
        "",
        "```python",
        t.get("code") or "",
        "```",
        "",
    ]
    result = t.get("result")
    if result:
        layers = result.get("layers") or []
        charts = result.get("charts") or []
        # 结论摘要放在图层/图表之前 —— 看报告的人是先要结论再要细节
        if result.get("conclusion"):
            lines += ["## 结论摘要", "", result["conclusion"], ""]
        stats = result.get("stats") or {}
        if stats:
            lines += ["## 关键统计量", ""]
            lines += [f"- {k}：{v}" for k, v in stats.items()]
            lines.append("")
        lines += ["## 结果图层", ""]
        for l in layers:
            lines.append(f"- {l.get('name')}（{l.get('kind')}）")
        lines += ["", "## 统计图表", ""]
        for c in charts:
            lines.append(f"- {c.get('title')}（{c.get('kind')}）")
        if result.get("error"):
            lines += ["", "## 错误信息", "", f"- 分类：{result['error'].get('category')}", f"- 详情：{result['error'].get('message')}"]
    logs = t.get("logs") or []
    if logs:
        lines += ["", "## 执行日志", ""] + [f"- {x}" for x in logs]
    lines.append("")
    lines.append("> 本报告由 AI 自动生成，仅供研究与分析参考，不构成权威监测结论。")
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=report_{tid}.md"},
    )


# ---- 前端静态托管（生产模式：单端口对外）----
FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

# 这些前缀下没有「前端页面」。未匹配到路由就是真 404，绝不能回退到 index.html：
# 否则 GET /api/typo 会拿到 200 + HTML，调用方以为成功、只在解析时炸，
# 排查时看的是一个 200，极难定位。
NON_SPA_PREFIXES = ("/api/", "/health", "/docs", "/openapi.json", "/redoc")


def _safe_dist_file(full_path: str):
    """把 URL 路径映射为 dist 目录内的真实文件；越界返回 None。

    必须做「resolve() 后仍在 dist 之内」的判定，只判 is_file() 是不够的：
    `%2e` 是 `.` 的合法编码，而 uvicorn 在构造请求对象**之前**就把它解码了，于是
        GET /%2e%2e/%2e%2e/backend/.env
    的 full_path 就是 ../../backend/.env，与 FRONTEND_DIST 拼接后指向 dist 之外，
    is_file() 为真 —— 后端源码、.env（含 LLM 密钥）、data/app.db（含密码哈希）
    会被直接发给**不需要登录**的任何人。resolve() 同时会解开符号链接，
    所以「在 dist 里放个软链指向外面」也一并挡住。
    """
    if not full_path or "\x00" in full_path:
        return None
    try:
        dist_root = FRONTEND_DIST.resolve()
        candidate = (FRONTEND_DIST / full_path).resolve()
    except (OSError, ValueError):
        return None
    if candidate == dist_root or not candidate.is_relative_to(dist_root):
        return None
    return candidate if candidate.is_file() else None


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # 1) 接口路径没匹配上就是 404，且必须是 JSON，不能吐 HTML
        url = "/" + full_path
        if any(url == p.rstrip("/") or url.startswith(p) for p in NON_SPA_PREFIXES):
            return JSONResponse({"detail": "接口不存在"}, status_code=404)
        # 2) 带 .. 的路径直接判非法。_safe_dist_file 已经挡得住，这里再挡一道
        #    是为了「越界尝试」返回 404 而不是 SPA 外壳，语义清楚、也防止将来
        #    有人改 _safe_dist_file 时不小心把洞放回来。
        if ".." in Path(full_path).parts:
            return JSONResponse({"detail": "路径非法"}, status_code=404)
        # 3) dist 内确实存在的静态文件（favicon 等）原样返回
        real = _safe_dist_file(full_path)
        if real is not None:
            return FileResponse(real)
        # 4) 其余回退 index.html，支持前端单页路由
        return FileResponse(FRONTEND_DIST / "index.html")
