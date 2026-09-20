"""瓦片代理（GEE 栅格瓦片 + 地图底图）。

为什么需要它
------------
实测本机（以及国内大多数网络环境）：

| 目标 | 浏览器直连 | 经本机代理 |
|---|---|---|
| `earthengine-highvolume.googleapis.com`（GEE 栅格瓦片） | **HTTP 000** | 200 |
| `tile.openstreetmap.org`（地图底图） | **HTTP 000** | 200 |

两者都连不通，结果是**地图面板一片空白**（只剩前端自己画的矢量矩形）。
后端进程已经有可用代理（`GEE_PROXY`），所以正确做法是：浏览器向后端要瓦片，
后端经代理取回来转发 —— 前端完全不需要能访问外网，公网映射场景也一样成立。

两个端点的安全模型不同
----------------------
  * `/api/tile`（GEE）：URL 含服务端生成的 mapid，客户端必须把地址传进来，
    因此做**严格校验** —— 只允许 https + `*.googleapis.com` / `*.google.com`，
    坐标必须是纯数字，单张体积设上限，避免退化成开放的 SSRF 代理。
  * `/api/basemap`（底图）：URL **完全由后端拼装**，客户端只能给 z/x/y，
    不存在 SSRF 面。
"""

from __future__ import annotations

from urllib.parse import urlparse

from .config import settings
from .net_retry import PermanentError, retry_call
from .net_retry import session as retry_session

#: 允许 GEE 瓦片代理的域名后缀（Google 自家域名）
ALLOWED_HOST_SUFFIXES = (".googleapis.com", ".google.com")

#: 单张瓦片体积上限（正常 GEE 瓦片是几十 KB 量级）
MAX_TILE_BYTES = 4 * 1024 * 1024

#: 取瓦片的超时（要快，瓦片是逐张拉的，不能按任务的 420s 算）
TILE_TIMEOUT = 30

#: 底图模板与 UA。OSM 的瓦片使用政策要求带可识别 UA，
#: 不带会被拒（403），这也是必须由后端统一代理的一个附带好处。
BASEMAP_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
BASEMAP_USER_AGENT = "gee-assistant/0.1 (local remote-sensing research demo)"


class TileProxyError(Exception):
    """瓦片获取失败的统一异常（可重试类）。"""


def _proxies() -> dict[str, str] | None:
    return (
        {"http": settings.gee_proxy, "https": settings.gee_proxy}
        if settings.gee_proxy
        else None
    )


def _check_xyz(z: str, x: str, y: str) -> None:
    for name, value in (("z", z), ("x", x), ("y", y)):
        if not str(value).lstrip("-").isdigit():
            raise PermanentError(f"非法瓦片坐标 {name}={value!r}")


def build_url(base: str, z: str, x: str, y: str) -> str:
    """校验并拼出完整 GEE 瓦片 URL。

    `base` 是前端传来的瓦片模板去掉 `/{z}/{x}/{y}` 之后的地址。
    """
    _check_xyz(z, x, y)

    parsed = urlparse(base)
    if parsed.scheme != "https":
        raise PermanentError("只允许代理 https 瓦片地址")
    host = (parsed.hostname or "").lower()
    if not host.endswith(ALLOWED_HOST_SUFFIXES):
        raise PermanentError(f"瓦片域名不在白名单：{host or '(空)'}")
    return f"{base.rstrip('/')}/{z}/{x}/{y}"


def _get(url: str, *, user_agent: str | None = None):
    """带重试的 GET（代理节点抖动是常态）。4xx 视为永久错误不重试。"""
    sess = retry_session()
    headers = {"User-Agent": user_agent} if user_agent else None

    def _once():
        resp = sess.get(url, proxies=_proxies(), timeout=TILE_TIMEOUT, headers=headers)
        if 400 <= resp.status_code < 500:
            return resp  # 交给调用方判定（404 在底图上属正常）
        if resp.status_code >= 500:
            raise TileProxyError(f"上游返回 {resp.status_code}")
        if not resp.content:
            raise TileProxyError("上游返回空内容")
        return resp

    return retry_call(_once, attempts=3, base_delay=0.4)


def fetch(url: str) -> tuple[bytes, str]:
    """经本机代理取一张 GEE 瓦片，返回 (内容, content-type)。"""
    resp = _get(url)
    if resp.status_code >= 400:
        # GEE 瓦片的 4xx 是永久错误（mapid 过期、路径错）
        raise PermanentError(f"GEE 返回 {resp.status_code}")
    if len(resp.content) > MAX_TILE_BYTES:
        raise PermanentError("瓦片体积异常，已拒绝")
    return resp.content, resp.headers.get("Content-Type", "image/png")


def fetch_basemap(z: str, x: str, y: str) -> tuple[bytes, str] | None:
    """取一张底图瓦片；上游 404（远洋等无数据区域）返回 None。"""
    _check_xyz(z, x, y)
    resp = _get(BASEMAP_TEMPLATE.format(z=z, x=x, y=y), user_agent=BASEMAP_USER_AGENT)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise PermanentError(f"底图源返回 {resp.status_code}")
    return resp.content, resp.headers.get("Content-Type", "image/png")
