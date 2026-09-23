"""跑一条真实任务，截图结果区（验证地图/图表/图例/结论卡片的新配色）。

用离线后端（offline）跑，避免占用真实 GEE 配额；配色逻辑两条后端共用，验证有效。
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import _env  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\153.0.4234.48\msedge.exe"
PORT = 9223
BASE = "http://127.0.0.1:8010"
out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
# ⚠ 必须从 backend/.env 读凭据：只读 os.environ 会拿到空密码，登录失败后
#   截到的是登录页 —— 且脚本不会报错，等于白截一轮还以为成功了。
user = _env("ADMIN_USERNAME", "admin")
pwd = _env("ADMIN_PASSWORD", "")
if not pwd:
    print("[FAIL] 未取到 ADMIN_PASSWORD（环境变量与 backend/.env 都没有）")
    sys.exit(1)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
profile = os.path.join(tempfile.gettempdir(), "wb_ui_result_profile")
shutil.rmtree(profile, ignore_errors=True)
os.makedirs(profile, exist_ok=True)
os.makedirs(out_dir, exist_ok=True)


class CDP:
    def __init__(self, ws_url):
        from urllib.parse import urlparse
        import socket
        u = urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _send(self, payload):
        data = payload.encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126); head += n.to_bytes(2, "big")
        else:
            head.append(0x80 | 127); head += n.to_bytes(8, "big")
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise EOFError
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self):
        b1, b2 = self._exact(2)
        ln = b2 & 0x7F
        if ln == 126:
            ln = int.from_bytes(self._exact(2), "big")
        elif ln == 127:
            ln = int.from_bytes(self._exact(8), "big")
        return self._exact(ln).decode("utf-8", "ignore")

    def call(self, method, params=None, timeout=90):
        self._id += 1
        mid = self._id
        self._send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(self._frame())
            if m.get("id") == mid:
                return m.get("result", {})
        raise TimeoutError(method)

    def js(self, expr, await_promise=False, timeout=90):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "awaitPromise": await_promise,
                       "returnByValue": True}, timeout=timeout)
        return r.get("result", {}).get("value")

    def shot(self, path, width=1440, height=900, full=False):
        if full:
            m = self.call("Page.getLayoutMetrics")
            css = m.get("cssContentSize") or m.get("contentSize")
            height = min(max(int(css["height"]), 400), 5000)
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1,
                   "mobile": width < 768})
        time.sleep(1.5)
        r = self.call("Page.captureScreenshot",
                      {"format": "png", "captureBeyondViewport": full})
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print(f"  [OK] {os.path.basename(path)}  {os.path.getsize(path)} B")

    def close(self):
        self.sock.close()


proc = subprocess.Popen([
    EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
    f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
    "--disable-gpu", "--hide-scrollbars", "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    for _ in range(40):
        try:
            opener.open(f"http://127.0.0.1:{PORT}/json/version", timeout=5)
            break
        except Exception:
            time.sleep(0.5)

    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
    with opener.open(req, timeout=15) as r:
        tab = json.load(r)
    c = CDP(tab["webSocketDebuggerUrl"])

    c.call("Page.navigate", {"url": BASE + "/"})
    time.sleep(3)
    print("  登录:", c.js(
        "fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},"
        "credentials:'include',body:JSON.stringify({username:%s,password:%s})}).then(r=>r.status)"
        % (json.dumps(user), json.dumps(pwd)), await_promise=True))

    c.call("Page.navigate", {"url": BASE + "/"})
    time.sleep(4)

    # 提交一条离线任务（ndvi，走 offline 后端不需要外部配额）
    task_id = c.js("""
    (async () => {
      const r = await fetch('/api/tasks', {
        method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include',
        body: JSON.stringify({task_type:'ndvi', region:'太湖流域',
          start_date:'2024-06-01', end_date:'2024-08-31', cloud_threshold:20, use_llm:false})
      });
      const d = await r.json();
      return d.task_id || JSON.stringify(d);
    })()
    """, await_promise=True)
    print("  任务 ID:", task_id)

    # 等任务落库
    for i in range(30):
        st = c.js("""fetch('/api/tasks/%s',{credentials:'include'}).then(r=>r.json())
                     .then(d=>d.status||'?')""" % task_id, await_promise=True, timeout=30)
        if st in ("succeeded", "failed"):
            print(f"  任务状态: {st}（轮询 {i+1} 次）")
            break
        time.sleep(2)

    # 触发前端重新加载该任务并渲染结果
    c.call("Page.navigate", {"url": BASE + "/"})
    time.sleep(3)
    c.js("""
    (async () => {
      const r = await fetch('/api/tasks/%s', {credentials:'include'});
      return (await r.json()).status;
    })()
    """ % task_id, await_promise=True)
    time.sleep(2)

    # 用「历史任务」列表点进去（最贴近真实用户路径）。
    # 注意：列表项是 .ant-list-item，且文本在嵌套 span 里 ——
    # 直接找"文本最短的元素"会命中 span 而非可点击的 li，点了没反应。
    info = c.js("""(() => {
      const items = [...document.querySelectorAll('.ant-list-item')];
      const hit = items.find(li => /太湖流域/.test(li.textContent));
      if (!hit) return 'not-found:' + items.length;
      hit.scrollIntoView({block:'center'});
      hit.click();
      return 'clicked:' + hit.textContent.replace(/\\s+/g,' ').trim().slice(0,60);
    })()""")
    print("  点击历史任务:", info)
    # 等地图瓦片与图表 chunk 都就绪（懒加载 + 网络请求，给足时间）
    time.sleep(12)

    layers = c.js("""(() => ({
      maps: document.querySelectorAll('.leaflet-container').length,
      charts: document.querySelectorAll('.chart-container canvas').length,
      legends: document.querySelectorAll('.legend-item').length,
      conclusion: document.querySelectorAll('.conclusion-card').length,
      tiles: document.querySelectorAll('.leaflet-tile').length,
    }))()""")
    print("  渲染元素:", layers)

    c.shot(os.path.join(out_dir, "05-结果区-桌面.png"), width=1440, height=900)
    c.shot(os.path.join(out_dir, "06-结果区-整页.png"), width=1440, full=True)
    c.shot(os.path.join(out_dir, "07-结果区-手机.png"), width=390, height=844)

    c.close()
finally:
    proc.terminate()
    time.sleep(1)
    try:
        proc.kill()
    except Exception:
        pass
print("完成")
