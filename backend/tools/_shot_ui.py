"""用 Edge 无头浏览器截图当前 UI（走原生 CDP，不依赖 playwright）。

用法：python tools/_shot_ui.py <输出目录> [--keep]
输出：login.png（登录页）、app.png（登录后主界面）
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\153.0.4234.48\msedge.exe"
PORT = 9222
BASE = "http://127.0.0.1:8010"

# ⚠ 凭据必须从 backend/.env 读：这些脚本是独立进程，shell 里并没有
#   export ADMIN_PASSWORD。原来只读 os.environ 会拿到空密码 → 登录失败 →
#   截出来的是登录页，但你以为是"主界面"，而且脚本会一路"成功"退出。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import _env  # noqa: E402

USER, PWD = _env("ADMIN_USERNAME", "admin"), _env("ADMIN_PASSWORD", "")
if not PWD:
    print("[FAIL] 未取到 ADMIN_PASSWORD（环境变量与 backend/.env 都没有）—— 无法登录")
    sys.exit(1)

# 绕开本机代理
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
profile = os.path.join(tempfile.gettempdir(), "wb_ui_shot_profile")
if os.path.isdir(profile):
    shutil.rmtree(profile, ignore_errors=True)
os.makedirs(profile, exist_ok=True)
os.makedirs(out_dir, exist_ok=True)


def cdp(ws_url, method, params=None, _id=[0]):
    import websocket  # noqa
    raise RuntimeError("unused")


def http_json(path, method="GET"):
    # 新版 Edge 的 /json/new 只接受 PUT（GET 返 405）
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with opener.open(req, timeout=15) as r:
        return json.load(r)


class CDP:
    """极简 CDP 客户端（WebSocket 手写帧，避免依赖 websocket-client）。"""

    def __init__(self, ws_url):
        from urllib.parse import urlparse
        import socket

        u = urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=25)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], buf[:200]
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _send(self, payload: str):
        data = payload.encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += n.to_bytes(2, "big")
        else:
            head.append(0x80 | 127)
            head += n.to_bytes(8, "big")
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + masked)

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv_frame(self):
        b1, b2 = self._recv_exact(2)
        ln = b2 & 0x7F
        if ln == 126:
            ln = int.from_bytes(self._recv_exact(2), "big")
        elif ln == 127:
            ln = int.from_bytes(self._recv_exact(8), "big")
        payload = self._recv_exact(ln)
        return payload.decode("utf-8", "ignore")

    def call(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        self._send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self._recv_frame())
            if msg.get("id") == mid:
                return msg.get("result", {})
        raise TimeoutError(method)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def shot(cdp_client, path, full=False, width=1440, height=900):
    if full:
        m = cdp_client.call("Page.getLayoutMetrics")
        css = m.get("cssContentSize") or m.get("contentSize")
        h = int(css["height"])
        height = min(max(h, 400), 4000)
    cdp_client.call("Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1,
                     "mobile": width < 768})
    cdp_client.call("Page.enable")
    time.sleep(1.2)
    r = cdp_client.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": full})
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    print(f"[OK] {path}  ({os.path.getsize(path)} B)")


proc = subprocess.Popen([
    EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
    f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
    "--disable-gpu", "--hide-scrollbars", "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    # 等 CDP 端口
    for _ in range(40):
        try:
            http_json("/json/version")
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise RuntimeError("Edge CDP 未就绪")

    tab = http_json("/json/new?about:blank", method="PUT")
    ws = tab["webSocketDebuggerUrl"]
    c = CDP(ws)

    # ---- 1) 登录页 ----
    c.call("Page.navigate", {"url": BASE + "/"})
    time.sleep(3.5)
    shot(c, os.path.join(out_dir, "01-登录页.png"), width=1440, height=900)

    # ---- 2) 登录 ----
    if USER and PWD:
        js = """
        (async () => {
          const r = await fetch('/api/auth/login', {
            method:'POST', headers:{'Content-Type':'application/json'},
            credentials:'include', body: JSON.stringify({username:%s, password:%s})
          });
          return r.status;
        })()
        """ % (json.dumps(USER), json.dumps(PWD))
        res = c.call("Runtime.evaluate", {"expression": js, "awaitPromise": True,
                                          "returnByValue": True})
        print("   登录状态:", res.get("result", {}).get("value"))
        c.call("Page.navigate", {"url": BASE + "/"})
        time.sleep(5)
        shot(c, os.path.join(out_dir, "02-主界面-桌面.png"), width=1440, height=900)
        shot(c, os.path.join(out_dir, "03-主界面-手机.png"), width=390, height=844)
        shot(c, os.path.join(out_dir, "04-主界面-整页.png"), full=True, width=1440, height=900)
    else:
        print("   (跳过登录：未设 ADMIN_PASSWORD)")

    c.close()
finally:
    proc.terminate()
    time.sleep(1)
    try:
        proc.kill()
    except Exception:
        pass

print("完成")
