"""GEE 凭据解析与初始化。

支持三种凭据来源，按优先级自动挑选：
  A. 服务账号 JSON 文件     GEE_SERVICE_ACCOUNT_JSON=<绝对路径>
  B. 加密凭据库（推荐）      backend/data/credentials/gee.enc（由 tools/gee_doctor.py 生成）
  C. 个人 OAuth 凭据         ~/.config/earthengine/credentials（earthengine authenticate 生成）

对外只暴露 resolve() / diagnose()，不返回任何私钥明文。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import secure_store
from .config import settings


@dataclass
class GeeAuthInfo:
    ok: bool
    source: str = "none"          # service_account_file | encrypted_store | oauth | none
    account: str = ""             # 服务账号邮箱 或 oauth 标识
    project: str = ""
    message: str = ""
    details: dict = field(default_factory=dict)

    def public(self) -> dict:
        """可安全下发给前端的字段（不含密钥，账号做脱敏）。"""
        return {
            "ok": self.ok,
            "source": self.source,
            "account": secure_store.masked(self.account),
            "project": self.project,
            "message": self.message,
        }


def apply_proxy() -> None:
    """对外暴露的代理配置入口（OAuth 等非 resolve 路径也需要）。"""
    _apply_proxy()


def _apply_proxy() -> None:
    """GEE 全站在国内需要代理；用 GEE_PROXY 显式指定。

    注意：必须**强制覆盖**而不是 setdefault。服务进程可能从宿主环境继承
    http_proxy/https_proxy（例如沙箱注入的 127.0.0.1:60722），那种代理对
    TLS 隧道会返回 502，会静默把 GEE 请求打死。同理要清掉 NO_PROXY 里的干扰项。
    """
    # 无论是否配置代理，都先装上可重试网络层：代理节点抖动是常态，
    # 默认的 urllib3 策略不重试「隧道失败」，会把偶发 502 直接变成任务失败。
    from app import net_retry

    net_retry.install()

    if not settings.gee_proxy:
        return
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ[var] = settings.gee_proxy
    # 确保 googleapis 不会被 NO_PROXY 排除掉
    no_proxy = [
        x.strip() for x in os.environ.get("NO_PROXY", "").split(",")
        if x.strip() and "google" not in x.lower()
    ]
    os.environ["NO_PROXY"] = ",".join(no_proxy)
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


def _oauth_path() -> Path:
    if settings.gee_oauth_credentials:
        return Path(settings.gee_oauth_credentials)
    return Path.home() / ".config" / "earthengine" / "credentials"


def _ee_import_error(e: ImportError) -> str:
    """把 ee 导入失败翻译成人话。

    「沙箱策略拦截」和「真的没装」是两回事，混成一句「未安装 earthengine-api」
    会让人跑去白装一遍依赖，而真正的问题在沙箱黑名单里。
    """
    text = str(e)
    if "沙箱安全策略" in text:
        return (
            f"沙箱策略拦截了 earthengine-api 的导入（{text}）"
            f"—— 检查 app/execution/_runner.py 的 _BLOCKED 是否误伤该模块的依赖"
        )
    return f"未安装 earthengine-api：{e}"


def resolve() -> tuple[object | None, GeeAuthInfo]:
    """返回 (ee 模块的凭据对象, 诊断信息)。凭据对象为 None 表示不可用。"""
    try:
        import ee
    except ImportError as e:
        return None, GeeAuthInfo(ok=False, message=_ee_import_error(e))

    _apply_proxy()

    # A. 服务账号 JSON 文件
    sa = settings.gee_sa_path
    if sa is not None:
        if not sa.exists():
            return None, GeeAuthInfo(
                ok=False,
                source="service_account_file",
                message=f"GEE_SERVICE_ACCOUNT_JSON 指向的文件不存在：{sa}",
            )
        try:
            info = ee.ServiceAccountCredentials(None, str(sa))
            return info, GeeAuthInfo(
                ok=True,
                source="service_account_file",
                account=getattr(info, "service_account_email", "") or "",
                project=settings.gee_project,
                message="使用服务账号 JSON 文件",
            )
        except Exception as e:  # noqa: BLE001
            return None, GeeAuthInfo(ok=False, source="service_account_file", message=f"凭据文件解析失败：{e}")

    # B. 加密凭据库
    enc = settings.gee_enc_path
    if enc.exists():
        data = secure_store.load_credentials(enc)
        if data:
            try:
                # 注意：ee.ServiceAccountCredentials 的 key_data 要求 JSON 字符串，
                # 传 dict 会触发 "the JSON object must be str, bytes or bytearray, not dict"
                info = ee.ServiceAccountCredentials(data.get("client_email", ""), key_data=json.dumps(data))
                return info, GeeAuthInfo(
                    ok=True,
                    source="encrypted_store",
                    account=data.get("client_email", ""),
                    project=data.get("project_id", settings.gee_project),
                    message="使用加密凭据库 gee.enc",
                )
            except Exception as e:  # noqa: BLE001
                return None, GeeAuthInfo(ok=False, source="encrypted_store", message=f"加密凭据解密后不可用：{e}")
        return None, GeeAuthInfo(ok=False, source="encrypted_store", message="加密凭据库存在但无法解密（密钥可能丢失）")

    # C. 个人 OAuth
    oauth = _oauth_path()
    if oauth.exists():
        return None, GeeAuthInfo(
            ok=True,
            source="oauth",
            account="个人 Google 账号（OAuth 刷新令牌）",
            project=settings.gee_project,
            message="检测到 earthengine authenticate 生成的凭据文件",
        )

    return None, GeeAuthInfo(
        ok=False,
        source="none",
        message=(
            "未找到任何 GEE 凭据。凭据需要自行创建，不是自动生成的：\n"
            "  路线 A（服务账号）：Google Cloud Console 建服务账号 → 下载 JSON → "
            "GEE_SERVICE_ACCOUNT_JSON=路径\n"
            "  路线 B（个人账号）：pip install earthengine-api 后执行 earthengine authenticate\n"
            "详见 docs/GEE凭据配置指南.md，或运行 python tools/gee_doctor.py 一键自检"
        ),
    )


def initialize() -> tuple[object | None, GeeAuthInfo]:
    """解析凭据并 ee.Initialize()。返回 (ee 模块, 诊断信息)。"""
    creds, info = resolve()
    if not info.ok:
        return None, info
    try:
        import ee
    except ImportError as e:
        return None, GeeAuthInfo(ok=False, message=_ee_import_error(e))

    kwargs: dict = {"opt_url": "https://earthengine-highvolume.googleapis.com"}
    if settings.gee_project:
        kwargs["project"] = settings.gee_project
    # 个人 OAuth 凭据由 ee 的默认凭据链接管，不能显式传 credentials；
    # 且此时 creds 本来就是 None（不回传官方 SDK 项目，见 resolve 的说明）。
    if creds is not None and info.source != "oauth":
        kwargs["credentials"] = creds

    # 代理节点抖动会造成偶发 SSL 中断，鉴权这一步也做几次重试
    last_err: Exception | None = None
    for attempt in range(max(1, settings.gee_net_retries)):
        try:
            ee.Initialize(**kwargs)
            return ee, info
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < settings.gee_net_retries - 1 and _looks_network(str(e)):
                import time as _t

                _t.sleep(1.2 * (attempt + 1))
                continue
            break

    return None, GeeAuthInfo(
        ok=False,
        source=info.source,
        account=info.account,
        project=info.project,
        message=f"ee.Initialize 失败：{type(last_err).__name__}: {last_err}",
    )


def _looks_network(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in (
        "ssl", "eof occurred", "connection reset", "connection aborted", "max retries",
        "timed out", "timeout", "remotedisconnected", "temporarily unavailable", "getaddrinfo",
    ))


def diagnose() -> dict:
    """给 /api/gee/status 与自检脚本用的完整诊断（不含密钥）。"""
    out: dict = {
        "package_installed": False,
        "credentials_found": False,
        "source": "none",
        "account": "(未配置)",
        "project": settings.gee_project or "",
        "proxy": settings.gee_proxy or "",
        "initialized": False,
        "network_ok": None,
        "message": "",
    }
    try:
        import ee  # noqa: F401

        out["package_installed"] = True
    except ImportError:
        out["message"] = "未安装 earthengine-api，请执行：pip install earthengine-api"
        return out

    creds, info = resolve()
    out.update(
        {
            "credentials_found": info.ok,
            "source": info.source,
            "account": secure_store.masked(info.account) if info.account else "(未配置)",
            "message": info.message,
        }
    )
    if not info.ok:
        return out

    ee, init_info = initialize()
    out["initialized"] = ee is not None
    if ee is not None:
        out["message"] = "GEE 认证成功，可以切换 EXECUTION_BACKEND=gee"
        try:
            out["project"] = ee.data.getAssetRoots()[0]["id"].split("/")[0]
        except Exception:  # noqa: BLE001
            pass
    else:
        out["message"] = init_info.message
    return out
