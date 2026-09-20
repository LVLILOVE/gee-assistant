"""受限 Python 沙箱（父进程侧）。

对应任务书要求：「搭建受限 Python 沙箱（资源限额、超时控制、网络白名单）」。

设计：
  * 隔离性：在**独立子进程**中执行大模型生成的代码，避免污染服务进程；
  * 超时控制：父进程 subprocess timeout，超时即杀进程（不会挂死 API）；
  * 资源限额：子进程内限制内存/递归深度，禁用危险模块与系统调用；
  * 网络白名单：子进程内劫持 socket，仅放行 googleapis.com / google.com 域；
  * 结果回传：子进程通过哨兵行回传 JSON（图层 / 图表 / 打印日志）。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import BASE_DIR, settings

_RUNNER = Path(__file__).resolve().parent / "_runner.py"
_SENTINEL = "__WB_JSON__"

# 网络/代理抖动类错误的指纹。国内代理节点常出现 SSL 中断、连接重置，
# 这类失败重跑一次往往就过了，值得自动重试。
_NETWORK_SIGNS = (
    "ssl", "unexpected_eof", "eof occurred", "connection reset", "connection aborted",
    "connectionrefused", "remotedisconnected", "max retries exceeded", "timed out",
    "timeout", "temporarily unavailable", "broken pipe", "getaddrinfo",
)


def _looks_like_network_error(err: dict | None) -> bool:
    if not err:
        return False
    text = f"{err.get('message', '')} {err.get('traceback', '')}".lower()
    return any(s in text for s in _NETWORK_SIGNS)


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    layers: list = field(default_factory=list)
    charts: list = field(default_factory=list)
    error: dict | None = None
    # 代码里 WB.stat(k, v) 上报的结构化统计。_runner 一直在回传它，
    # 但这里曾经漏了字段 → gee.py 读 res.stats 直接 AttributeError，
    # 让"执行成功"的任务在最后一步变成 failed（2026-09-20 端到端实测抓到）。
    stats: dict = field(default_factory=dict)


def _venv_python() -> str:
    """优先使用当前后端 venv 的解释器，保证子进程拥有 earthengine-api。"""
    candidate = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def run_in_sandbox(code: str, request_payload: dict | None = None, *, mock: bool = False) -> SandboxResult:
    """执行一次；网络类错误自动重试（代理节点抖动是国内环境的常态）。

    注意：超时与沙箱策略拦截**不重试**（重试没意义且浪费时间）。
    """
    attempts = 1 if mock else max(1, settings.gee_net_retries)
    last: SandboxResult | None = None
    for i in range(attempts):
        res = _run_once(code, request_payload, mock=mock)
        if res.ok:
            if i:
                res.stdout = f"[重试] 第 {i + 1} 次尝试成功（前 {i} 次为网络抖动）\n" + res.stdout
            return res
        last = res
        cat = (res.error or {}).get("category")
        if cat in ("timeout", "sandbox") or not _looks_like_network_error(res.error):
            return res  # 非网络问题，重试无意义
        if i < attempts - 1:
            import time as _t

            _t.sleep(1.5 * (i + 1))
    if last is not None:
        last.error = dict(last.error or {})
        last.error["message"] = (
            f"连续 {attempts} 次尝试均因网络失败（代理不稳定？）：{last.error.get('message', '')}"
        )
        last.error["category"] = "network"
    return last or SandboxResult(ok=False, error={"category": "other", "message": "未知错误"})


def _run_once(code: str, request_payload: dict | None = None, *, mock: bool = False) -> SandboxResult:
    payload = {"code": code, "request": request_payload or {}}
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(BASE_DIR)
    if mock:
        env["WB_GEE_MOCK"] = "1"
    if settings.gee_proxy:
        env["HTTPS_PROXY"] = settings.gee_proxy
        env["HTTP_PROXY"] = settings.gee_proxy

    try:
        proc = subprocess.run(
            [_venv_python(), str(_RUNNER)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.gee_timeout,
            env=env,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(
            ok=False,
            error={"category": "timeout", "message": f"沙箱执行超过 {settings.gee_timeout}s 已终止（可能在等待 GEE 云端计算）"},
        )
    except Exception as e:  # noqa: BLE001
        return SandboxResult(ok=False, error={"category": "operator", "message": f"沙箱启动失败：{type(e).__name__}: {e}"})

    out_lines = []
    result_obj = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith(_SENTINEL) and line.endswith(_SENTINEL):
            try:
                result_obj = json.loads(base64.b64decode(line[len(_SENTINEL) : -len(_SENTINEL)]).decode("utf-8"))
            except Exception:  # noqa: BLE001
                pass
        else:
            out_lines.append(line)

    stdout = "\n".join(out_lines).strip()
    if result_obj is None:
        merged = (proc.stderr or "")[-800:].strip() or stdout
        return SandboxResult(
            ok=False,
            stdout=stdout,
            error={"category": "operator", "message": f"沙箱未返回结构化结果（退出码 {proc.returncode}）：{merged}"},
        )
    if result_obj.get("ok"):
        return SandboxResult(
            ok=True,
            stdout=result_obj.get("stdout", stdout),
            layers=result_obj.get("layers", []),
            charts=result_obj.get("charts", []),
            stats=result_obj.get("stats") or {},
        )
    return SandboxResult(
        ok=False,
        stdout=result_obj.get("stdout", stdout),
        error=result_obj.get("error") or {"category": "other", "message": "未知错误"},
    )
