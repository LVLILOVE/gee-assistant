"""统一的可重试网络层 —— 兜住本地代理节点的抖动。

背景：GEE 全链路依赖一条到 Google 的代理隧道，而国内代理节点普遍抖动
（实测同一代理 6 次请求只通 1~2 次，失败形态主要是
`ProxyError: Tunnel connection failed: 502 Bad Gateway`）。

为什么 urslib3 自带的 Retry 不够：
    urllib3 2.x 的 `Retry._is_connection_error()` 对 ProxyError 会取
    `original_error` 再判断，而隧道失败的 original_error 常常不是
    `ConnectTimeoutError`，于是**默认策略完全不重试这种错误**。
    实测：带默认 Retry 的会话仍会把 502 直接抛给上层。

因此这里做了两件事：
  1. 自定义 Retry 策略，把「代理隧道失败」显式纳入可重试范围；
  2. 提供 `install()` 把该策略挂到所有新创建的 requests.Session 上，
     从而覆盖 google-auth 的 AuthorizedSession（OAuth 换令牌走的就是它）。

用法：
    from app import net_retry
    net_retry.install()                      # 全局生效（幂等）
    sess = net_retry.session()                # 或手动建一个带重试的会话
    val = net_retry.retry_call(lambda: risky())   # 任意可重试调用
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

#: 默认重试次数与时延基数（指数退避：0.6s / 1.2s / 2.4s / 4.8s）
DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 0.6

#: 视为「可重试」的 HTTP 状态码
RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)

#: 明确属于编程错误、重试无意义的异常（重试只会白等并掩盖 bug）
class PermanentError(Exception):
    """明确不可恢复的错误（如 HTTP 4xx、配置缺失）。重试没有意义。"""


_NON_RETRYABLE = (
    AttributeError,
    TypeError,
    NameError,
    ImportError,
    SyntaxError,
    NotImplementedError,
    PermanentError,
)

_INSTALLED = False


def retry_policy(total: int = DEFAULT_ATTEMPTS):
    """构造对代理隧道失败也生效的 urllib3 Retry 策略。"""
    from urllib3.exceptions import ConnectTimeoutError, NewConnectionError, ProtocolError
    from urllib3.util.retry import Retry

    class _ProxyAwareRetry(Retry):
        """在默认判定之外，额外把代理隧道失败视为可重试。"""

        def _is_connection_error(self, err: Exception) -> bool:  # noqa: D102
            # 代理隧道失败：urllib3 抛 ProxyError，默认策略会因 original_error
            # 不是 ConnectTimeoutError 而放弃重试 —— 这正是 502 打穿的原因。
            if type(err).__name__ == "ProxyError":
                return True
            if isinstance(err, (ConnectTimeoutError, NewConnectionError, ProtocolError)):
                return True
            return False

    return _ProxyAwareRetry(
        total=total,
        connect=total,
        read=total,
        status=total,
        backoff_factor=0.5,
        status_forcelist=RETRY_STATUS,
        # 允许所有方法重试：换令牌 / 提交任务是 POST，抖动时同样需要重试
        allowed_methods=None,
        raise_on_status=False,  # 重试耗尽后返回响应，由上层按状态码处理
    )


def session(attempts: int = DEFAULT_ATTEMPTS):
    """返回一个带重试适配器的 requests.Session。"""
    import requests
    from requests.adapters import HTTPAdapter

    sess = requests.Session()
    adapter = HTTPAdapter(max_retries=retry_policy(attempts))
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


def install(attempts: int = DEFAULT_ATTEMPTS) -> bool:
    """让此后创建的所有 requests.Session 自动带重试（幂等）。

    覆盖范围包含 requests 自己的 Session 与 google-auth 的
    AuthorizedSession（二者都是 requests.Session 的子类）。
    httplib2 链路（googleapiclient 的 highvolume 传输）不在此列，
    该部分由沙箱层的 `GEE_NET_RETRIES` 做整段重跑兜底。
    """
    global _INSTALLED
    if _INSTALLED:
        return False

    import requests.adapters as _adapters
    import requests.sessions as _sessions
    from requests.adapters import HTTPAdapter

    policy = retry_policy(attempts)

    class _AutoRetryAdapter(HTTPAdapter):
        """默认就带重试策略的适配器。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("max_retries", policy)
            super().__init__(*args, **kwargs)

    # requests.Session.__init__ 从 requests.sessions 命名空间取 HTTPAdapter，
    # 必须两处都替换才能真正影响新建会话。
    _adapters.HTTPAdapter = _AutoRetryAdapter  # type: ignore[assignment,misc]
    _sessions.HTTPAdapter = _AutoRetryAdapter  # type: ignore[assignment,misc]
    _INSTALLED = True
    return True


def retry_call(
    fn: Callable[[], T],
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """对任意调用做指数退避重试（用于适配器管不到的角落，如凭据创建时的即时刷新）。

    编程错误（AttributeError / TypeError 等）不重试 —— 重试也没用，
    只会把 5 次退避等待白白耗掉，还会掩盖真正的 bug。
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except _NON_RETRYABLE as e:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if on_retry is not None:
                on_retry(i + 1, e)
            if i < attempts - 1:
                time.sleep(base_delay * (2**i))
    assert last is not None
    raise last
