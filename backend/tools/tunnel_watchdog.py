# -*- coding: utf-8 -*-
"""cpolar 隧道看门狗：探测公网是否可用，不可用则重启隧道。

【为什么需要它】
免费版 cpolar 的**数据通道**会不定期卡死，而进程和 TCP 连接都还"活着"：
  - `netstat` 显示到隧道服务端的连接是 ESTABLISHED；
  - 控制通道心跳（Ping/Pong）照常成功；
  - 但代理握手超时 —— 日志里刷 `Server failed to read StartProxy: ... i/o timeout`。
结果就是「进程在跑、端口在听、公网打不开」，靠肉眼完全看不出来。
手动重启隧道能恢复，但重启会**换域名**，所以不能盲目定时重启，
只能在**探测到真的不通**时才重启，并输出新域名。

【判定标准】
不看进程、不看 netstat，只看**公网 /health 是否返回 200**。
连续失败 N 次才动手（避免把偶发抖动误判成故障 —— 实测免费版本就
有约 1/4 的请求会耗时 20s+ 或超时，单次失败不能作为依据）。

用法：
    python tools/tunnel_watchdog.py            # 探测一次，坏了就重启
    python tools/tunnel_watchdog.py --check    # 只探测，不重启
    python tools/tunnel_watchdog.py --loop 60  # 每 60 秒探测一次，持续守护
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

CPOLAR = r"C:\Users\Administrator\cpolar\portable\cpolar.exe"
CPOLAR_DIR = r"C:\Users\Administrator\cpolar"
REGION = "cn_top"
LOCAL_PORT = 8010
PANEL = "http://127.0.0.1:4040/http/in"
FAILS_TO_RESTART = 3          # 连续失败几次才重启
PROBE_TIMEOUT = 12            # 单次探测超时（秒）

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def current_domain():
    """从 cpolar 本地面板取当前隧道域名（进程不在时返回 None）。"""
    try:
        with OPENER.open(PANEL, timeout=8) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = re.findall(r"https://([a-z0-9]+\.r\d+\.cpolar\.(?:top|cn))", html)
    return m[0] if m else None


def local_ok():
    """本地后端必须先活着，否则隧道通不通都白搭。"""
    try:
        with OPENER.open(f"http://127.0.0.1:{LOCAL_PORT}/health", timeout=8) as r:
            return r.getcode() == 200
    except Exception:
        return False


def public_ok(domain):
    """只看公网 /health 是否 200 —— 这是唯一可信的判据。"""
    if not domain:
        return False
    try:
        req = urllib.request.Request(f"https://{domain}/health")
        with OPENER.open(req, timeout=PROBE_TIMEOUT) as r:
            body = r.read(200).decode("utf-8", "replace")
            return r.getcode() == 200 and '"status"' in body
    except Exception:
        return False


def kill_cpolar():
    """按 PID 杀，不用 /IM（本机 taskkill 对 //F 这类写法会报参数错误）。"""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq cpolar.exe", "/FO", "CSV", "/NH"],
            text=True, timeout=20,
        )
    except Exception as e:
        print(f"    取进程列表失败: {e}")
        return
    pids = re.findall(r'"cpolar\.exe","(\d+)"', out)
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=20)
            print(f"    已终止 cpolar PID {pid}")
        except Exception as e:
            print(f"    终止 PID {pid} 失败: {e}")
    time.sleep(3)


def start_cpolar():
    """后台起新隧道。必须绕代理，否则 cpolar 会走本机 HTTP_PROXY 而连不上。"""
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [CPOLAR, "http", str(LOCAL_PORT), "-region", REGION, "-log", "stdout"],
        cwd=CPOLAR_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    # 等隧道建立 + 新域名上报（实测 10~18 秒）
    for i in range(30):
        time.sleep(1)
        d = current_domain()
        if d and public_ok(d):
            return d
    return current_domain()


def probe_once():
    print(f"[{time.strftime('%H:%M:%S')}] 探测…")
    if not local_ok():
        print("    ✗ 本地 :8010 不通 —— 问题在后端，不在隧道（先起后端）")
        return "backend_down"
    dom = current_domain()
    if not dom:
        print("    ✗ 取不到隧道域名（cpolar 进程可能已退出）")
        return "no_tunnel"
    if public_ok(dom):
        print(f"    ✓ https://{dom} 可用")
        return "ok"
    print(f"    ✗ https://{dom} 无响应（本地正常 → 隧道数据通道卡死）")
    return "tunnel_down"


def heal():
    """重建隧道，返回新域名。"""
    print("  → 重建隧道（域名会变）…")
    kill_cpolar()
    dom = start_cpolar()
    if dom and public_ok(dom):
        print(f"  ✓ 已恢复：https://{dom}")
        return dom
    print(f"  ✗ 重建后仍不通：{dom}")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只探测不重启")
    ap.add_argument("--loop", type=int, default=0, metavar="秒",
                    help="持续守护，间隔探测（默认只跑一次）")
    a = ap.parse_args()

    if a.loop:
        fails = 0
        while True:
            st = probe_once()
            if st == "ok":
                fails = 0
            elif st == "backend_down":
                fails = 0        # 后端的问题不归看门狗管，别去重启隧道
            else:
                fails += 1
                print(f"    连续失败 {fails}/{FAILS_TO_RESTART}")
                if not a.check and fails >= FAILS_TO_RESTART:
                    heal()
                    fails = 0
            time.sleep(a.loop)

    fails = 0
    for i in range(FAILS_TO_RESTART):
        st = probe_once()
        if st == "ok":
            print("结论：公网可用，无需处理")
            return 0
        if st == "backend_down":
            print("结论：后端没起，先解决后端")
            return 1
        fails += 1
        if i < FAILS_TO_RESTART - 1:
            time.sleep(2)
    print(f"结论：连续 {fails} 次公网不可用")
    if a.check:
        print("（--check 模式，不重启）")
        return 1
    dom = heal()
    print(f"\n新地址：https://{dom}" if dom else "\n未能恢复，请手动检查")
    return 0 if dom else 1


if __name__ == "__main__":
    sys.exit(main())
