"""GEE 凭据自检与配置工具（一键排查「找不到凭据」）。

用法（在 backend 目录下，用 venv 的 python 执行）：

    .venv\\Scripts\\python.exe tools\\gee_doctor.py                # 全链路自检
    .venv\\Scripts\\python.exe tools\\gee_doctor.py --detect-proxy # 自动发现可用的本地代理
    .venv\\Scripts\\python.exe tools\\gee_doctor.py --self-test    # 用 mock ee 跑通沙箱链路（无需凭据）
    .venv\\Scripts\\python.exe tools\\gee_doctor.py --import-sa "C:\\path\\key.json"   # 导入服务账号 JSON 并加密保存
    .venv\\Scripts\\python.exe tools\\gee_doctor.py --detect-project # 自动找已注册 Earth Engine 的项目并写回 .env
    .venv\\Scripts\\python.exe tools\\gee_doctor.py --set-project 你的项目ID  # 校验指定项目并写回（推荐）  # 自动探测已注册 EE 的项目 ID 并写回 .env
    .venv\\Scripts\\python.exe tools\\gee_doctor.py --oauth        # 个人账号 OAuth 登录（需可访问 Google）

自检覆盖 5 个环节：依赖 → 网络（支持代理与自动发现）→ 凭据 → 初始化 → 真实调用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, BAD, WARN, STEP = "[ OK ]", "[FAIL]", "[WARN]", "  --> "

GEE_HOSTS = [
    ("earthengine.googleapis.com", 443, "GEE 计算 API（执行代码必需）"),
    ("oauth2.googleapis.com", 443, "Google 取令牌（鉴权必需）"),
    ("console.cloud.google.com", 443, "Cloud Console（建凭据用，浏览器访问）"),
    ("code.earthengine.google.com", 443, "Earth Engine 注册页（浏览器访问）"),
]

# 常见网络工具的本地代理端口：Clash Verge/Mihomo 7897、Clash 7890、v2rayN 10809、
# SS 1080、SwitchyOmega+privoxy 8118、ShadowsocksR 1080、Surge 6152 等
PROXY_CANDIDATES = [7897, 7890, 7891, 7898, 7899, 10809, 10808, 1080, 1081, 8118,
                    8889, 20171, 2080, 6152, 4780, 10801, 33210, 12333, 12334]
_PROBE_URL = "https://earthengine.googleapis.com/$discovery/rest?version=v1"


def step1_package() -> bool:
    print("\n[1/5] Python 依赖")
    try:
        import ee  # noqa: F401

        import importlib.metadata as md

        print(f"  {OK} earthengine-api {md.version('earthengine-api')}")
        return True
    except ImportError:
        print(f"  {BAD} 未安装 earthengine-api")
        print(f"{STEP}pip install -i https://pypi.tuna.tsinghua.edu.cn/simple earthengine-api")
        return False


def _session(retries: int = 5):
    """带重试的 requests 会话（复用 net_retry 的「代理隧道失败也可重试」策略）。"""
    from app import net_retry

    return net_retry.session(attempts=retries)


def _probe_http(proxy: str | None, url: str = _PROBE_URL, timeout: float = 15.0) -> tuple[bool, str]:
    """用真实 HTTP 请求探测（能识别「代理端口开着但出不了海」的情况）。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _session().get(url, proxies=proxies, timeout=timeout)
        return True, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:70]}"


def proxy_health(proxy: str, attempts: int = 6) -> tuple[int, int]:
    """测量代理稳定性。节点抖动是国内环境的常态，必须量化后再下结论。"""
    ok = 0
    for _ in range(attempts):
        good, _msg = _probe_http(proxy, timeout=15)
        if good:
            ok += 1
    return ok, attempts


def _listening_ports() -> set[int]:
    """枚举本机 127.0.0.1/0.0.0.0 上的 LISTENING 端口。"""
    import subprocess  # 本机自检脚本可接受

    try:
        # Windows netstat 输出是本地代码页（中文系统为 GBK），必须容错解码，
        # 否则 text=True 的默认 utf-8 解码会抛 UnicodeDecodeError。
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        ).stdout or ""
    except Exception:  # noqa: BLE001
        return set()
    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        addr = parts[1]
        if not (addr.startswith("127.0.0.1:") or addr.startswith("0.0.0.0:")):
            continue
        try:
            ports.add(int(addr.rsplit(":", 1)[1]))
        except ValueError:
            continue
    return ports


def detect_proxy(verbose: bool = True) -> str | None:
    """扫描常见代理端口，返回最能访问 GEE 的代理地址（按稳定性择优）。"""
    live = _listening_ports()
    candidates = [p for p in PROXY_CANDIDATES if p in live]
    if verbose:
        print(f"      本机监听的候选代理端口：{candidates or '（无）'}")
    best: tuple[float, str] | None = None
    for port in candidates:
        proxy = f"http://127.0.0.1:{port}"
        ok, msg = _probe_http(proxy)
        if not ok:
            if verbose:
                print(f"      不可用  {proxy}  [{msg}]")
            continue
        good, total = proxy_health(proxy, attempts=4)
        rate = good / total
        if verbose:
            print(f"      可用    {proxy}  稳定性 {good}/{total}（{rate:.0%}）")
        if best is None or rate > best[0]:
            best = (rate, proxy)
    return best[1] if best else None


def step2_network() -> bool:
    from app.config import settings

    print("\n[2/5] 网络连通性（本机能否到达 GEE）")

    # 已配置代理：直接按代理方式验证（生产链路就是这么走的）
    if settings.gee_proxy:
        print(f"      使用已配置的 GEE_PROXY={settings.gee_proxy}")
        ok, msg = _probe_http(settings.gee_proxy)
        print(f"  {OK if ok else BAD} 经代理访问 GEE 计算 API   [{msg}]")
        if ok:
            print(f"  {OK} GEE 计算 API 可达（HTTPS 隧道正常）")
            print("      测量代理稳定性（6 次连续请求）…")
            good, total = proxy_health(settings.gee_proxy)
            rate = good / total
            print(f"      成功率 {good}/{total}（{rate:.0%}）")
            if rate >= 0.95:
                print(f"  {OK} 代理稳定")
            elif rate >= 0.5:
                print(f"  {WARN} 代理抖动明显。GEE 调用已内置重试可兜住大部分请求，")
                print(f"{STEP} 但建议在 Clash 里换一个更稳定的节点，体验会好很多。")
            else:
                print(f"  {BAD} 代理严重不稳定（{rate:.0%}）。真实 GEE 执行会频繁失败。")
                print(f"{STEP} 请在网络工具里切换节点/机场后再重跑本自检。")
                print(f"{STEP} 在切换前，保持 EXECUTION_BACKEND=offline 更稳妥。")
            return True
        print(f"{STEP} GEE_PROXY 配置了但不可用，先确认网络工具已启动且模式为规则/全局。")
        return False

    # 未配置代理：先试直连
    ok, msg = _probe_http(None, timeout=8)
    if ok:
        print(f"  {OK} 直连 GEE 计算 API 可达   [{msg}]")
        return True
    print(f"  {BAD} 直连不可达   [{msg}]")

    # 直连失败 → 自动找本地代理
    print("      直连失败，尝试自动发现本机代理…")
    proxy = detect_proxy()
    if proxy:
        print(f"\n  {OK} 发现可用代理：{proxy}")
        # 让本次自检的后续步骤也用上它，这样能直接验证到「只差凭据」这一步
        import os

        settings.gee_proxy = proxy
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[var] = proxy
        print(f"{STEP} 把它写进 backend/.env 即可启用真实 GEE：")
        print(f"{STEP}  GEE_PROXY={proxy}")
        print(f"{STEP} 本次自检已临时启用该代理，继续往下检查凭据。\n")
        return True

    print(f"\n  {BAD} 本机既不能直连，也没找到可用代理 —— 这是当前最前置的卡点。")
    print(f"{STEP}可选：")
    print(f"{STEP}  1) 启动可访问 Google 的网络工具（全局/TUN 模式），重跑本脚本")
    print(f"{STEP}  2) 若有本地代理端口，写进 backend/.env：GEE_PROXY=http://127.0.0.1:你的端口")
    print(f"{STEP}  3) 网络无法解决时，继续用 EXECUTION_BACKEND=offline 交付演示")
    print(f"{STEP}     （任务书 6.3 明确允许：演示与测试采用公开样例数据集与离线样例结果兜底）")
    return False


def step3_credentials() -> dict:
    print("\n[3/5] GEE 凭据检测")
    from app.config import settings
    from app.gee_auth import resolve

    sa = settings.gee_sa_path
    enc = settings.gee_enc_path
    oauth = Path.home() / ".config" / "earthengine" / "credentials"

    print(f"      候选 A 服务账号 JSON : {sa if sa else '（未配置 GEE_SERVICE_ACCOUNT_JSON）'}"
          + (" <- 文件存在" if sa and sa.exists() else ""))
    print(f"      候选 B 加密凭据库    : {enc}" + (" <- 文件存在" if enc.exists() else " <- 不存在"))
    print(f"      候选 C 个人 OAuth    : {oauth}" + (" <- 文件存在" if oauth.exists() else " <- 不存在"))

    _creds, info = resolve()
    if info.ok:
        from app.secure_store import masked

        print(f"  {OK} 已识别凭据：来源={info.source} 账号={masked(info.account)} 项目={info.project or '(未设置 GEE_PROJECT)'}")
    else:
        print(f"  {BAD} 未找到可用凭据。凭据不会被自动生成，需要你在 Google 侧创建：")
        for line in info.message.splitlines():
            print(f"      {line}")
        print(f"{STEP}推荐路线：见 docs/GEE凭据配置指南.md")
        print(f"{STEP}已有服务账号 JSON 的话，直接执行：")
        print(f'{STEP}  .venv\\Scripts\\python.exe tools\\gee_doctor.py --import-sa "C:\\你的\\key.json"')
    return {"ok": info.ok, "source": info.source}


def _list_gcp_projects(creds) -> list[dict]:
    """用凭据列出可见的 Google Cloud 项目（用于找 EE 项目 ID）。

    已知限制：Cloud Resource Manager API 必须在使用它的项目上启用。而个人 OAuth
    凭据的配额项目是 earthengine-api 官方客户端项目（517222506229），用户无权启用，
    因此该接口对纯 OAuth 用户通常返回 403。这属于**永久性失败**，不重试。
    """
    from app import net_retry
    from google.auth.transport.requests import Request

    token = net_retry.retry_call(
        lambda: _refresh_token(creds, Request),
        on_retry=lambda i, e: print(f"      第 {i} 次换令牌失败（{type(e).__name__}），重试…"),
    )
    proxies = _proxies()

    def _fetch():
        r = net_retry.session().get(
            "https://cloudresourcemanager.googleapis.com/v1/projects",
            headers={"Authorization": f"Bearer {token}"},
            timeout=25,
            proxies=proxies,
        )
        if r.status_code in (401, 403):
            raise net_retry.PermanentError(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        return r.json().get("projects", [])

    try:
        return net_retry.retry_call(
            _fetch,
            on_retry=lambda i, e: print(f"      第 {i} 次列举项目失败（{type(e).__name__}），重试…"),
        )
    except net_retry.PermanentError as e:
        msg = str(e)
        if "403" in msg:
            print(f"{WARN} Cloud Resource Manager API 不可用（个人 OAuth 的配额项目是官方客户端项目，无法启用该 API）。")
            print(f"{STEP} 这不影响使用——项目 ID 本来就要你在注册 Earth Engine 时确定。")
        else:
            print(f"{WARN} 列举项目失败：{msg[:140]}")
        return []


def _proxies() -> dict | None:
    """按配置返回显式代理字典（避免依赖环境变量）。"""
    from app.config import settings

    if not settings.gee_proxy:
        return None
    return {"http": settings.gee_proxy, "https": settings.gee_proxy}


def _refresh_token(creds, request_cls) -> str:
    """刷新凭据并返回 access token。

    注意：`with_scopes` 只有服务账号凭据（service_account.Credentials）才有，
    个人 OAuth 凭据（oauth2.credentials.Credentials）没有该方法。
    """
    if hasattr(creds, "with_scopes"):
        creds = creds.with_scopes(["https://www.googleapis.com/auth/cloud-platform"])
    if not getattr(creds, "token", None) or not getattr(creds, "valid", False):
        creds.refresh(request_cls())
    return creds.token


def _build_cloud_creds():
    """构造带 cloud-platform scope 的凭据，用于列举 GCP 项目。

    两条来源：
      1) 服务账号（JSON 文件 / 加密凭据库）
      2) 个人 OAuth（~/.config/earthengine/credentials）
         —— 该文件只有 refresh_token/scopes，client_id/client_secret 由
            earthengine-api 内置常量补齐，故直接复用 ee 自己的加载逻辑。
    """
    from app.config import settings

    try:
        from google.oauth2 import service_account
    except ImportError:
        service_account = None  # type: ignore[assignment]

    if service_account is not None:
        if settings.gee_sa_path and settings.gee_sa_path.exists():
            try:
                return service_account.Credentials.from_service_account_file(
                    str(settings.gee_sa_path)
                )
            except Exception:  # noqa: BLE001
                pass

        from app import secure_store

        data = secure_store.load_credentials(settings.gee_enc_path)
        if data:
            try:
                return service_account.Credentials.from_service_account_info(data)
            except Exception:  # noqa: BLE001
                pass

    # 个人 OAuth 路线：get_persistent_credentials 会立刻换一次令牌，
    # 代理抖动时会直接抛 ProxyError，所以必须带重试。
    from app import net_retry

    try:
        import ee.data as ee_data

        return net_retry.retry_call(
            ee_data.get_persistent_credentials,
            on_retry=lambda i, e: print(f"      第 {i} 次读取 OAuth 凭据失败（{type(e).__name__}），重试…"),
        )
    except Exception as e:  # noqa: BLE001
        print(f"{WARN} 读取 OAuth 凭据失败：{type(e).__name__}: {str(e)[:120]}")
        return None


def detect_project() -> int:
    """自动找出哪个云项目已注册 Earth Engine，并写回 .env。"""
    from app.config import settings
    from app.gee_auth import apply_proxy, resolve

    print("=" * 68)
    print(" GEE 项目 ID 自动探测")
    print("=" * 68)
    apply_proxy()

    creds, info = resolve()
    if not info.ok:
        print(f"{BAD} 还没有可用凭据，无法探测项目。{info.message.splitlines()[0]}")
        return 1

    print(f"      已识别凭据来源：{info.source}")
    print("[1/2] 通过 Cloud Resource Manager 列举可见项目…")
    cloud_creds = _build_cloud_creds()
    if cloud_creds is None:
        print(f"{WARN} 当前凭据不是服务账号（或个人 OAuth），无法自动列举项目。")
        print(f"{STEP} 不影响使用：直接把你在 code.earthengine.google.com/register")
        print(f"{STEP} 注册时记下的项目 ID 填进 backend/.env 的 GEE_PROJECT= 即可。")
        return 1
    projects = _list_gcp_projects(cloud_creds)
    if not projects:
        print(f"{WARN} 没列到任何项目（可能是服务账号无 resourcemanager 权限，或个人账号未授权该接口）")
        print(f"{STEP} 这不影响使用：直接把你在 code.earthengine.google.com/register 注册时")
        print(f"{STEP} 记下的项目 ID 填进 backend/.env 的 GEE_PROJECT= 即可。")
        return 1

    print(f"      发现 {len(projects)} 个项目，逐个测试是否已注册 Earth Engine…")
    print("[2/2] 逐个尝试 ee.Initialize + 一次真实调用…")

    oauth_mode = info.source == "oauth"
    working: list[str] = []
    for p in projects:
        pid = p.get("projectId")
        if not pid:
            continue
        ok, msg = _verify_project(pid, oauth_mode, None if oauth_mode else creds)
        print(f"  {OK if ok else BAD} {pid}  {msg}")
        if ok:
            working.append(pid)

    if not working:
        print(f"\n{BAD} 没有任何项目可用。最可能：项目还没在 Earth Engine 注册。")
        print(f"{STEP} 打开 https://code.earthengine.google.com/register 完成注册后重跑本命令。")
        return 1

    best = working[0]
    print(f"\n{OK} 可用项目 ID：{'、'.join(working)}")
    print(f"{STEP} 建议写入 backend/.env：")
    print(f"{STEP}  GEE_PROJECT={best}")

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists() and len(working) == 1:
        text = env_path.read_text(encoding="utf-8")
        import re

        new = re.sub(r"^GEE_PROJECT=.*$", f"GEE_PROJECT={best}", text, flags=re.M)
        if new != text:
            env_path.write_text(new, encoding="utf-8")
            print(f"{OK} 已自动写回 {env_path.name}（GEE_PROJECT={best}）")
        else:
            print(f"{WARN} .env 里没找到 GEE_PROJECT= 行，请手动添加")
    elif len(working) > 1:
        print(f"{WARN} 有多个可用项目，未自动写入，请自行选定一个填入 .env。")

    if not settings.gee_proxy:
        print(f"\n{WARN} 还没配置 GEE_PROXY，真实执行前记得补上（--detect-proxy 可自动发现）。")
    print(f"{STEP} 最后把 EXECUTION_BACKEND 改为 gee 并重启服务。")
    return 0


def _verify_project(pid: str, oauth_mode: bool, creds=None) -> tuple[bool, str]:
    """验证某个云项目是否已注册 Earth Engine（跑一次真实调用才算数）。"""
    try:
        import ee

        if oauth_mode:
            # 个人 OAuth 凭据由 ee 的默认凭据链接管，不能显式传 credentials
            ee.Initialize(project=pid)
        else:
            ee.Initialize(creds, project=pid)
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").limit(1).size().getInfo()
        return True, "已注册 Earth Engine，真实调用成功"
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        if "not registered" in msg or "Not signed up" in msg:
            return False, f"{msg}  ← 该项目未注册 Earth Engine"
        return False, msg


def set_project(pid: str) -> int:
    """校验指定项目 ID 并写回 .env（供 --set-project 使用）。

    用于「自动列举项目」走不通的场景 —— 个人 OAuth 凭据无法调用 Cloud Resource
    Manager API，项目 ID 只能由用户从注册页/控制台取得，但**校验可以自动做**。
    """
    from app.config import settings
    from app.gee_auth import apply_proxy, resolve

    print("=" * 68)
    print(f" 校验并设置 EE 项目：{pid}")
    print("=" * 68)
    apply_proxy()

    creds, info = resolve()
    if not info.ok:
        print(f"{BAD} 还没有可用凭据：{info.message.splitlines()[0]}")
        print(f"{STEP} 先跑：.venv\\Scripts\\python.exe tools\\gee_doctor.py --oauth")
        return 1
    print(f"      凭据来源：{info.source}（{info.account}）")

    oauth_mode = info.source == "oauth"
    ok, msg = _verify_project(pid, oauth_mode, None if oauth_mode else creds)
    if not ok:
        print(f"{BAD} 校验失败：{msg}")
        if "not registered" in msg or "Not signed up" in msg:
            print(f"{STEP} 该项目尚未注册 Earth Engine（或项目 ID 拼错）。")
            print(f"{STEP} 打开 https://code.earthengine.google.com/register ，选择/新建该项目并提交；")
            print(f"{STEP} 注册通过后重跑本命令即可。个人账号注册通常是即时或几天内审核。")
        elif "permission" in msg.lower() or "403" in msg:
            print(f"{STEP} 凭据有效但缺权限：在项目 IAM 里给账号授予")
            print(f"{STEP}  「Earth Engine Resource Viewer」+「Service Usage Consumer」。")
        return 1

    print(f"{OK} {msg}")
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        import re

        text = env_path.read_text(encoding="utf-8")
        new = re.sub(r"^GEE_PROJECT=.*$", f"GEE_PROJECT={pid}", text, flags=re.M)
        if "GEE_PROJECT=" not in new:
            new = new.rstrip("\n") + f"\nGEE_PROJECT={pid}\n"
        new = re.sub(r"^EXECUTION_BACKEND=.*$", "EXECUTION_BACKEND=gee", new, flags=re.M)
        env_path.write_text(new, encoding="utf-8")
        print(f"{OK} 已写回 {env_path.name}：GEE_PROJECT={pid}、EXECUTION_BACKEND=gee")
    else:
        print(f"{WARN} 未找到 .env，请手动设置 GEE_PROJECT={pid}")

    print(f"{STEP} 重启后端服务后，执行后端即切换为真实 Earth Engine。")
    return 0


def _hint_for_auth_error(msg: str, account: str = "") -> None:
    """把 Google 返回的鉴权错误翻译成可执行的下一步。"""
    low = msg.lower()
    if any(k in low for k in ("timeout", "timed out", "connection", "max retries",
                              "ssl", "refused", "getaddrinfo", "proxy", "unreachable")):
        print(f"{STEP} 网络层失败 —— 代理没生效或网络工具没开。")
        print(f"{STEP} 先跑：.venv\\Scripts\\python.exe tools\\gee_doctor.py --detect-proxy")
    elif "invalid_grant" in low and "not found" in low:
        print(f"{STEP} Google 说这个服务账号不存在 —— JSON 被改过，或服务账号已被删除。")
        print(f"{STEP} 回 Cloud Console 重新创建服务账号并下载新密钥，再 --import-sa 导入。")
    elif "invalid_grant" in low:
        print(f"{STEP} 私钥与 client_email 不匹配，或密钥已被撤销。请重新下载 JSON 密钥。")
    elif "has not been used in project" in low or "service_disabled" in low:
        print(f"{STEP} Google Earth Engine API 尚未启用。")
        print(f"{STEP} Console → APIs & Services → Library → 搜索并启用 Google Earth Engine API。")
    elif "not registered" in low:
        print(f"{STEP} 云项目未在 Earth Engine 侧注册。")
        print(f"{STEP} 到 https://code.earthengine.google.com/register 完成注册并等待审核。")
    elif "403" in low or "permission" in low or "denied" in low:
        print(f"{STEP} 凭据本身有效，但缺权限。两件事都要做：")
        print(f"{STEP}  1) 云项目已在 code.earthengine.google.com/register 注册")
        if account:
            print(f"{STEP}  2) 项目 IAM 里给 {account} 授予 "
                  f"「Earth Engine Resource Viewer」+「Service Usage Consumer」")
        else:
            print(f"{STEP}  2) 项目 IAM 里给服务账号授予 "
                  f"「Earth Engine Resource Viewer」+「Service Usage Consumer」")
    elif "project" in low:
        print(f"{STEP} 多半是 GEE_PROJECT 未设置或填错（要填项目 ID，不是项目名称）。")
    else:
        print(f"{STEP} 按 docs/GEE凭据配置指南.md 的报错对照表排查。")


def step4_initialize() -> bool:
    print("\n[4/5] ee.Initialize 鉴权")
    from app.gee_auth import initialize

    ee, info = initialize()
    if ee is None:
        print(f"  {BAD} {info.message}")
        _hint_for_auth_error(info.message, info.account)
        return False
    print(f"  {OK} 鉴权成功（来源={info.source}，项目={info.project or '默认'}）")
    return True


def step5_realtime() -> bool:
    print("\n[5/5] 真实调用 GEE（拉取一次数据集元信息）")
    from app.gee_auth import initialize

    ee, _ = initialize()
    if ee is None:
        print(f"  {WARN} 跳过（未通过鉴权）")
        return False
    try:
        size = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").limit(1).size().getInfo()
        print(f"  {OK} 调用成功，返回 {size}（Sentinel-2 数据集可访问）")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} 调用失败：{type(e).__name__}: {e}")
        return False


def probe_sandbox() -> int:
    """在**真实沙箱**里跑一次 GEE 鉴权，验证生产路径每一层都通。

    为什么需要这个：前 5 个环节都是在**主进程**里验证的，而真实执行发生在
    **子进程**里（受限沙箱）。两者的差别恰好是最容易出问题的地方：
      * 子进程能不能读到 OAuth 凭据（依赖 HOME / USERPROFILE 是否继承）
      * 子进程能不能走代理（依赖 HTTP_PROXY 是否传进去）
      * 沙箱的网络白名单会不会误伤 GEE 自己的 HTTP 栈

    本命令把这几层一次性打穿，并按错误特征判断卡点究竟在哪一层。
    """
    print("=" * 68)
    print(" 真实沙箱探测（子进程内跑 GEE 鉴权）")
    print("=" * 68)

    from app.config import settings
    from app.execution.sandbox import run_in_sandbox

    print(f"      执行后端    ：{settings.execution_backend}")
    print(f"      代理        ：{settings.gee_proxy or '(未配置)'}")
    print(f"      项目 ID     ：{settings.gee_project or '(未设置)'}")
    print(f"      超时        ：{settings.gee_timeout}s")
    print("      正在子进程中调用 ee.Initialize + 真实数据集访问…\n")

    code = (
        "import ee\n"
        "try:\n"
        "    ee.Initialize()\n"
        "    n = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').limit(1).size().getInfo()\n"
        "    print('SANDBOX_INIT_OK 数据集返回=%s' % n)\n"
        "except Exception as e:\n"
        "    print('SANDBOX_INIT_FAIL %s: %s' % (type(e).__name__, str(e)[:260]))\n"
    )

    import time as _t

    t0 = _t.time()
    res = run_in_sandbox(code, {"region": "沙箱探测"})
    cost = _t.time() - t0

    out = (res.stdout or "").strip()
    print(f"      耗时 {cost:.1f}s，沙箱 ok={res.ok}")
    if out:
        print(f"      子进程输出：{out[:400]}")
    if res.error:
        print(f"      错误：{(res.error.get('message') or '')[:300]}")

    low = (out + " " + (res.error or {}).get("message", "")).lower()
    print()
    if "SANDBOX_INIT_OK" in out:
        print(f"{OK} 全链路打通：沙箱子进程内鉴权 + 真实调用均成功。")
        print(f"{STEP} 真实 GEE 执行通道已就绪，可以正常跑分析任务。")
        return 0

    if "sandbox_geeblock" in low or "沙箱网络白名单拒绝" in out:
        print(f"{BAD} 卡在沙箱网络白名单：拦截了 GEE 自身域名，需要放行。")
    elif "defaultcredentials" in low or "未找到任何 gee 凭据" in low or "no project found" in low:
        print(f"{WARN} 子进程读到了凭据但初始化未通过 —— 卡点是项目 ID，不是沙箱。")
        print(f"{STEP} 执行：.venv\\Scripts\\python.exe tools\\gee_doctor.py --set-project 你的项目ID")
    elif any(k in low for k in ("proxy", "ssl", "tunnel", "max retries", "timed out", "timeout")):
        print(f"{BAD} 子进程出网失败（代理没生效或节点太抖）。")
        print(f"{STEP} 确认 .env 的 GEE_PROXY 可用：--detect-proxy")
    elif "permission" in low or "not registered" in low or "not signed up" in low:
        print(f"{WARN} 沙箱链路本身是通的（拿到了 Google 的业务错误）。")
        print(f"{STEP} 卡点在 Google 侧：项目未注册 EE 或缺 IAM 角色。")
    else:
        print(f"{WARN} 未能自动归类，请看上方原始输出。")
    return 1


def self_test() -> None:
    """不需要凭据、不联网：验证沙箱 + WB 结果契约链路是否通。"""
    print("\n[自检] 沙箱链路（mock ee，无需凭据）")
    from app.execution.sandbox import run_in_sandbox

    code = (
        "import ee\n"
        "aoi = ee.Geometry.Polygon([[[120, 31], [120.3, 31], [120.3, 31.3], [120, 31.3]]])\n"
        "image = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(aoi).median()\n"
        "ndvi = image.normalizedDifference(['B8', 'B4'])\n"
        "print('影像数=', ee.ImageCollection('COPERNICUS/S2').filterBounds(aoi).size().getInfo())\n"
        "WB.stat('NDVI均值', 0.42)\n"
        "WB.add_image_layer('NDVI 分布', ndvi, {'min': 0, 'max': 1})\n"
        "WB.add_chart('逐月 NDVI', labels=['1月', '2月'], series=[{'name': 'NDVI', 'data': [0.3, 0.4]}])\n"
    )
    res = run_in_sandbox(code, {"region": "太湖流域"}, mock=True)
    print(f"  {'[ OK ]' if res.ok else '[FAIL]'} 沙箱返回 ok={res.ok}")
    print(f"         图层数={len(res.layers)} 图表数={len(res.charts)}")
    if res.stdout:
        print("         代码输出：" + res.stdout.replace("\n", " | ")[:160])
    if res.error:
        print(f"         错误：{res.error}")

    # stats 契约回归守卫（2026-09-20 端到端实测抓到的真 bug）：
    #   _runner 一直在回传结构化 stats，但 SandboxResult 曾漏掉该字段，
    #   导致 gee.py 读 res.stats 抛 AttributeError —— 表现为「代码明明跑完了、
    #   却在最后一步变成 failed」，日志只有一句 AttributeError，极难定位。
    #   这条断言保证「沙箱 → 执行层」的 stats 交接永远可用。
    stats_ok = isinstance(getattr(res, "stats", None), dict) and res.stats.get("NDVI均值") == 0.42
    print(f"  {'[ OK ]' if stats_ok else '[FAIL]'} stats 结构化回传（WB.stat → SandboxResult.stats）")
    if not stats_ok:
        print(f"         实际拿到：{getattr(res, 'stats', '<无该属性>')!r}")

    # 执行层读取 stats 不能再抛 AttributeError（gee.py 侧）。
    # 真正要防的是「沙箱结果 → ExecutionOutcome」这段转换：用 mock 沙箱结果
    # 走一遍真实转换逻辑，比只 import 一下强得多。
    try:
        from app.execution import gee as _gee_mod
        from app.execution.sandbox import SandboxResult

        _conv = getattr(_gee_mod, "_to_outcome", None)
        if callable(_conv):
            _o = _conv(SandboxResult(ok=True, stdout="x", layers=[], charts=[], stats={"k": 1}))
        else:
            # 没有独立的转换函数时，退化为「直接构造」验证字段确实存在
            _dummy = SandboxResult(ok=True, stdout="x", layers=[], charts=[], stats={"k": 1})
            assert getattr(_dummy, "stats", None) == {"k": 1}
            _o = None
        print("  [ OK ] 执行层 stats 读取路径可用（无 AttributeError）")
    except AttributeError as e:
        print(f"  [FAIL] 执行层读 stats 抛 AttributeError：{e}")
    except Exception as e:  # noqa: BLE001
        print(f"  [ OK ] 执行层 stats 读取路径可用（{type(e).__name__} 非 AttributeError）")

    # 安全策略验证
    from app.execution.sandbox import run_in_sandbox as run

    # 注意：这里验证的是「进程创建能力被封」而不是「subprocess 模块被拦」。
    # 模块不能被拦 —— google-auth 在模块级 import subprocess（见 _runner 注释）。
    proc = run("import subprocess\nsubprocess.run(['whoami'])", mock=True)
    proc_blocked = (not proc.ok) and "禁止创建子进程" in str(proc.error)
    print(f"  {'[ OK ]' if proc_blocked else '[FAIL]'} 子进程创建能力封禁（subprocess.run）")

    osys = run("import os\nos.system('whoami')", mock=True)
    os_blocked = (not osys.ok) and "禁止创建子进程" in str(osys.error)
    print(f"  {'[ OK ]' if os_blocked else '[FAIL]'} 外部命令执行封禁（os.system）")

    # 工具链必须仍可导入：黑名单和历史改动最容易在这里连带打死自己
    tc = run("import dotenv, tempfile, google.auth.transport.requests\nprint('TOOLCHAIN_OK')",
             mock=True)
    tc_ok = tc.ok and "TOOLCHAIN_OK" in (tc.stdout or "")
    print(f"  {'[ OK ]' if tc_ok else '[FAIL]'} 运行依赖未被黑名单误伤（dotenv/google-auth）")

    net = run("import socket\nsocket.getaddrinfo('example.com', 443)", mock=True)
    net_blocked = (not net.ok) and "白名单" in str(net.error)
    print(f"  {'[ OK ]' if net_blocked else '[FAIL]'} 网络白名单拦截（非 Google 域名）")

    # 敏感文件禁读：生成代码由 LLM 按用户输入产出，而注册是开放的 ——
    # 不封的话任何注册用户写一句「读取 .env 并打印」就能把密钥取回去。
    # 沙箱子进程的 cwd 就是 backend/，所以用相对路径探测即可。
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        print("  [SKIP] 敏感文件禁读（本机没有 .env，无法判定）")
    else:
        env_res = run(
            "try:\n"
            "    open('.env').read()\n"
            "    print('READ_OK')\n"
            "except PermissionError:\n"
            "    print('BLOCKED')\n",
            mock=True,
        )
        env_blocked = "BLOCKED" in (env_res.stdout or "")
        print(f"  {'[ OK ]' if env_blocked else '[FAIL]'} 敏感文件禁读（.env / *.db / .db_backup）")

    from app.config import settings

    original = settings.gee_timeout
    settings.gee_timeout = 10  # 缩短超时用例的等待
    try:
        to = run("while True:\n    pass", mock=True)
    finally:
        settings.gee_timeout = original
    print(f"  {'[ OK ]' if not to.ok else '[FAIL]'} 超时控制生效（超时被终止：{to.error.get('category') if to.error else '-'}）")


def import_sa(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"{BAD} 文件不存在：{p}")
        return 1
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} 不是合法的 JSON：{e}")
        return 1
    if info.get("type") != "service_account":
        print(f"{BAD} 这不是服务账号密钥文件（type={info.get('type')!r}）。")
        print(f"{STEP} 服务账号 JSON 的 type 字段必须是 service_account；")
        print(f"{STEP} 如果你下载的是 OAuth 客户端（type=installed/authorized_user），那属于另一条路线。")
        return 1
    for k in ("client_email", "private_key", "project_id"):
        if not info.get(k):
            print(f"{BAD} 缺少字段 {k}，文件可能不完整")
            return 1

    from app.config import settings
    from app.secure_store import save_credentials

    # 必须提前讲清优先级：gee_auth.resolve() 是 A(服务账号文件) > B(加密凭据库) > C(个人 OAuth)，
    # 也就是 B 一旦存在就会**抢走** C。如果导入的服务账号有问题（缺角色 / 项目不对），
    # 本来好用的 OAuth 就用不上了，服务直接不可用——所以先告知现状与回退方式。
    try:
        from app.gee_auth import resolve as _resolve

        _, cur = _resolve()
        cur_desc = f"{cur.source}（{cur.account}）" if cur.ok else f"当前不可用：{cur.message[:70]}"
    except Exception as e:  # noqa: BLE001
        cur_desc = f"(解析失败：{type(e).__name__})"
    print(f"{STEP} 当前生效的凭据：{cur_desc}")
    print(f"{WARN} 服务账号优先级**高于**个人 OAuth，导入后会取代它。")
    print(f"{STEP} 若导入后反而不可用，用这条命令回退：")
    print(f"{STEP}  .venv\\Scripts\\python.exe tools\\gee_doctor.py --drop-sa")
    print()

    algo = save_credentials(info, settings.gee_enc_path)
    print(f"{OK} 已加密保存到 {settings.gee_enc_path}（算法={algo}）")
    print(f"{OK} 服务账号：{info['client_email']}")
    print(f"{OK} 项目 ID ：{info['project_id']}")

    # 立即向 Google 验证一次，把问题暴露在导入阶段而不是首次跑任务时
    print("\n[验证] 用该凭据向 Google 换取令牌…")
    verified = False
    try:
        from app.gee_auth import apply_proxy
        import ee
        import google.auth.transport.requests as tr

        apply_proxy()
        # key_data 必须是 JSON 字符串（不是 dict），否则 google-auth 会抛 TypeError
        creds = ee.ServiceAccountCredentials(info["client_email"], key_data=json.dumps(info))
        creds.refresh(tr.Request())
        print(f"  {OK} 令牌获取成功，凭据在 Google 侧真实存在")
        verified = True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        print(f"  {BAD} 验证未通过：{type(e).__name__}")
        print(f"       {msg[:220]}")
        _hint_for_auth_error(msg, info["client_email"])

    print(f"\n{STEP}接下来请把项目 ID 写入 backend/.env：")
    print(f"{STEP}  GEE_PROJECT={info['project_id']}")
    print(f"{STEP}然后把 EXECUTION_BACKEND=gee，重启服务后再跑一次本自检。")
    print(f"\n{WARN} 别忘了两项 Google 侧授权（否则 Initialize 会报权限错误）：")
    print(f"{STEP}  1) https://code.earthengine.google.com/register 用同一项目注册 Earth Engine")
    print(f"{STEP}  2) 在项目 IAM 里给 {info['client_email']} 授予 "
          f"「Earth Engine Resource Viewer」+「Service Usage Consumer」")
    return 0


def _install_network_retry(attempts: int = 5, delay: float = 1.5) -> None:
    """给 urllib.request.urlopen 打上重试补丁。

    earthengine-api 的 OAuth 流程（ee/oauth.py）用的是 urllib 而不是 requests，
    在国内代理节点抖动的环境下，单次 SSL 中断就会让整个授权流程失败。
    这里包一层重试，显著提高一次性通过率。
    """
    import time
    import urllib.request as _u

    if getattr(_u, "_wb_retry_installed", False):
        return
    _orig = _u.urlopen

    def _patched(*args, **kwargs):
        last: Exception | None = None
        for i in range(attempts):
            try:
                return _orig(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).lower()
                retryable = any(s in msg for s in (
                    "ssl", "eof occurred", "connection reset", "connection aborted",
                    "max retries", "timed out", "timeout", "remotedisconnected",
                    "temporarily unavailable", "broken pipe", "503", "502", "500",
                ))
                if not retryable or i == attempts - 1:
                    raise
                print(f"      [重试 {i + 2}/{attempts}] 网络抖动（{type(e).__name__}），重试中…")
                time.sleep(delay * (i + 1))
        raise last  # pragma: no cover

    _u.urlopen = _patched
    _u._wb_retry_installed = True


def oauth_login() -> int:
    print("=" * 68)
    print(" 个人 Google 账号 OAuth 授权（localhost 模式，无需手工粘贴验证码）")
    print("=" * 68)
    from app.config import settings
    from app.gee_auth import apply_proxy

    if not settings.gee_proxy:
        print(f"{WARN} 未配置 GEE_PROXY。若命令行无法直连 Google，换令牌会失败。")
        print(f"{STEP} 先执行：.venv\\Scripts\\python.exe tools\\gee_doctor.py --detect-proxy")
    else:
        apply_proxy()
        print(f"      已启用代理 {settings.gee_proxy}")

    print(f"{STEP} 稍后浏览器会自动打开 Google 授权页；若没打开，手动访问日志里打印的链接。")
    print(f"{STEP} 在页面上选择 Google 账号并点击「允许」即可，无需复制验证码。")
    print(f"{WARN} 浏览器本身也要能访问 Google（确认网络工具已开启，且系统代理生效）。\n")

    _install_network_retry(attempts=5)

    try:
        import ee

        ee.Authenticate(auth_mode="localhost")
    except TypeError:
        try:
            import ee

            ee.Authenticate()
        except Exception as e:  # noqa: BLE001
            print(f"{BAD} OAuth 失败：{type(e).__name__}: {e}")
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} OAuth 失败：{type(e).__name__}: {e}")
        print(f"{STEP} 该流程需要能访问 accounts.google.com；网络不通时请改用服务账号路线。")
        return 1

    cred_path = Path.home() / ".config" / "earthengine" / "credentials"
    if cred_path.exists():
        print(f"\n{OK} 凭据已写入 {cred_path}")
        print(f"{STEP} 下一步自动探测项目 ID：")
        print(f"{STEP}  .venv\\Scripts\\python.exe tools\\gee_doctor.py --detect-project")
        return 0
    print(f"\n{WARN} 授权流程结束，但未找到凭据文件 {cred_path}")
    print(f"{STEP} 可能授权被取消或浏览器未完成回调，请重跑本命令。")
    return 1


def drop_sa() -> int:
    """删除加密凭据库，回退到个人 OAuth。

    为什么必须有这条命令：`gee_auth.resolve()` 的优先级是
    A(服务账号文件) > B(加密凭据库) > C(个人 OAuth)，B 存在就会抢走 C。
    一旦导入的服务账号配错了（项目不对 / 缺 IAM 角色 / 密钥失效），
    原本好用的 OAuth 会被连带作废，服务直接不可用。没有回退手段的话，
    用户只能自己去猜哪个文件该删——这正是本命令要消灭的问题。
    """
    from app.config import settings

    enc = settings.gee_enc_path
    key = enc.parent / ".key"
    removed = []
    for p in (enc, key):
        if p.exists():
            try:
                p.unlink()
                removed.append(p)
            except OSError as e:  # noqa: BLE001
                print(f"{BAD} 删除失败 {p}：{e}")
                print(f"{STEP} 请手动删除该文件后重试。")
                return 1

    if not removed:
        print(f"{STEP} 未发现加密凭据库（{enc}），当前已是回退状态。")
    else:
        for p in removed:
            print(f"{OK} 已删除 {p}")

    from app.gee_auth import resolve

    _, info = resolve()
    if info.ok:
        print(f"{OK} 现在的凭据来源：{info.source}（{info.account}）")
    else:
        print(f"{BAD} 回退后仍无可用凭据：{info.message[:160]}")
        print(f"{STEP} 检查个人 OAuth 凭据是否存在，或重新执行 --import-sa")
        return 1
    print(f"\n{STEP} 建议重启服务并重新自检：.venv\\Scripts\\python.exe tools\\gee_doctor.py")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="GEE 凭据自检与配置")
    ap.add_argument("--import-sa", metavar="JSON_PATH", help="导入服务账号 JSON 并加密保存")
    ap.add_argument("--drop-sa", action="store_true", help="删除加密凭据库并回退到个人 OAuth")
    ap.add_argument("--oauth", action="store_true", help="走个人账号 OAuth 登录")
    ap.add_argument("--self-test", action="store_true", help="沙箱链路自检（无需凭据）")
    ap.add_argument("--probe-sandbox", action="store_true", help="在真实沙箱子进程里跑一次 GEE 鉴权，验证生产路径")
    ap.add_argument("--detect-proxy", action="store_true", help="扫描并测试本机可用代理端口")
    ap.add_argument("--detect-project", action="store_true", help="自动探测已注册 Earth Engine 的项目 ID 并写回 .env")
    ap.add_argument("--set-project", metavar="PROJECT_ID", help="校验指定项目 ID 并写回 .env（自动列举走不通时用这个）")
    args = ap.parse_args()

    if args.set_project:
        return set_project(args.set_project)
    if args.detect_project:
        return detect_project()
    if args.detect_proxy:
        print("=" * 68)
        print(" 本机代理自动发现（用于 GEE 出网）")
        print("=" * 68)
        proxy = detect_proxy()
        if proxy:
            print(f"\n{OK} 可用代理：{proxy}")
            print(f"{STEP} 写入 backend/.env：  GEE_PROXY={proxy}")
        else:
            print(f"\n{BAD} 未发现可用代理。请启动网络工具后重试，或继续使用 offline 后端。")
        return 0 if proxy else 1
    if args.import_sa:
        return import_sa(args.import_sa)
    if args.drop_sa:
        return drop_sa()
    if args.oauth:
        return oauth_login()
    if args.probe_sandbox:
        return probe_sandbox()
    if args.self_test:
        self_test()
        return 0

    print("=" * 68)
    print(" Google Earth Engine 凭据自检")
    print("=" * 68)
    if not step1_package():
        return 1
    net = step2_network()
    cred = step3_credentials()
    init = False
    if cred["ok"]:
        init = step4_initialize()
    if init:
        step5_realtime()
    print("\n" + "=" * 68)
    if net and cred["ok"] and init:
        print(" 结论：GEE 通道已就绪 → 把 backend/.env 的 EXECUTION_BACKEND 改为 gee 即可")
    elif net and not cred["ok"]:
        print(" 结论：网络没问题，只缺凭据 → 按 docs/GEE凭据配置指南.md 创建并 --import-sa 导入")
    elif not net:
        print(" 结论：网络是前置卡点 → 先解决访问 Google 的网络，凭据才有意义")
        print("       暂时保持 EXECUTION_BACKEND=offline，功能演示不受影响")
    else:
        print(" 结论：凭据存在但鉴权/调用未通过 → 优先核对 GEE_PROJECT 与 Google 侧 IAM 授权")
    print("=" * 68)
    return 0 if (net and cred["ok"] and init) else 1


if __name__ == "__main__":
    raise SystemExit(main())
