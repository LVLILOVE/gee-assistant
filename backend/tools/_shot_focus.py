"""聚焦截图：滚动右侧结果面板，分别拍「图表区」「图例区」「结论卡片」。

用途：整页截图对不了 —— 右栏是独立滚动容器（overflow-y:auto），
captureBeyondViewport 只能扩展文档高度，不会展开内部滚动区。
所以必须**先滚动容器**再截，否则永远只能看到首屏那一段。
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
PORT = 9224
BASE = "http://127.0.0.1:8010"
out_dir = sys.argv[1] if len(sys.argv) > 1 else "."

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import _env  # noqa: E402

# ⚠ 必须从 backend/.env 读凭据（同 _shot_result.py 的说明）：
#   只读 os.environ 会静默地截到登录页。
user = _env("ADMIN_USERNAME", "admin")
pwd = _env("ADMIN_PASSWORD", "")
if not pwd:
    print("[FAIL] 未取到 ADMIN_PASSWORD（环境变量与 backend/.env 都没有）")
    sys.exit(1)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
profile = os.path.join(tempfile.gettempdir(), "wb_ui_focus_profile")
shutil.rmtree(profile, ignore_errors=True)
os.makedirs(profile, exist_ok=True)
os.makedirs(out_dir, exist_ok=True)


class CDP:
    def __init__(self, ws):
        from urllib.parse import urlparse
        import socket
        u = urlparse(ws)
        self.s = socket.create_connection((u.hostname, u.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _send(self, p):
        d = p.encode(); m = os.urandom(4); n = len(d)
        h = bytearray([0x81])
        if n < 126: h.append(0x80 | n)
        elif n < 65536: h.append(0x80 | 126); h += n.to_bytes(2, "big")
        else: h.append(0x80 | 127); h += n.to_bytes(8, "big")
        self.s.sendall(bytes(h + m) + bytes(b ^ m[i % 4] for i, b in enumerate(d)))

    def _ex(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c: raise EOFError
            self.buf += c
        o, self.buf = self.buf[:n], self.buf[n:]
        return o

    def _fr(self):
        _, b2 = self._ex(2); ln = b2 & 0x7F
        if ln == 126: ln = int.from_bytes(self._ex(2), "big")
        elif ln == 127: ln = int.from_bytes(self._ex(8), "big")
        return self._ex(ln).decode("utf-8", "ignore")

    def call(self, m, p=None, t=90):
        self._id += 1; i = self._id
        self._send(json.dumps({"id": i, "method": m, "params": p or {}}))
        e = time.time() + t
        while time.time() < e:
            r = json.loads(self._fr())
            if r.get("id") == i: return r.get("result", {})
        raise TimeoutError(m)

    def js(self, x, aw=False, t=90):
        return self.call("Runtime.evaluate",
                         {"expression": x, "awaitPromise": aw, "returnByValue": True},
                         t).get("result", {}).get("value")

    def shot(self, path, w=1440, h=900):
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": w < 768})
        time.sleep(1.6)
        r = self.call("Page.captureScreenshot", {"format": "png"})
        open(path, "wb").write(base64.b64decode(r["data"]))
        print(f"  [OK] {os.path.basename(path)}  {os.path.getsize(path)} B")

    def close(self):
        self.s.close()


proc = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
                         f"--user-data-dir={profile}", "--no-first-run",
                         "--no-default-browser-check", "--disable-gpu",
                         "--hide-scrollbars", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            opener.open(f"http://127.0.0.1:{PORT}/json/version", timeout=5); break
        except Exception: time.sleep(0.5)
    rq = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
    with opener.open(rq, timeout=15) as r:
        tab = json.load(r)
    c = CDP(tab["webSocketDebuggerUrl"])

    c.call("Page.navigate", {"url": BASE + "/"}); time.sleep(3)
    print("  登录:", c.js("fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},"
                          "credentials:'include',body:JSON.stringify({username:%s,password:%s})}).then(r=>r.status)"
                          % (json.dumps(user), json.dumps(pwd)), aw=True))
    c.call("Page.navigate", {"url": BASE + "/"}); time.sleep(4)

    print("  打开任务:", c.js("""(() => {
      const li=[...document.querySelectorAll('.ant-list-item')]
        .find(e=>/太湖流域/.test(e.textContent));
      if(!li) return 'not-found'; li.scrollIntoView({block:'center'}); li.click();
      return 'ok' })()"""))
    time.sleep(12)

    # 逐个区块截图：滚动右栏到指定元素
    blocks = json.loads(c.js("""(() => {
      const p = document.querySelector('.panel-right');
      const out = {};
      const cards = [...p.querySelectorAll('.ant-card')];
      cards.forEach((cd, i) => {
        const t = cd.querySelector('.ant-card-head-title');
        out[(t ? t.textContent : 'card'+i).trim()] = i;
      });
      return JSON.stringify(out);
    })()""" ) or "{}")
    print("  结果卡片:", list(blocks.keys()))

    def focus(text, fname, w=1440, h=900):
        ok = c.js("""(() => {
          const p=document.querySelector('.panel-right');
          const cd=[...p.querySelectorAll('.ant-card')].find(x=>{
            const t=x.querySelector('.ant-card-head-title');
            return t && t.textContent.includes(%s);
          });
          if(!cd) return 'no';
          p.scrollTop = cd.offsetTop - 8;
          return 'ok';
        })()""" % json.dumps(text))
        if ok == 'ok':
            time.sleep(1.8)
            c.shot(os.path.join(out_dir, fname), w, h)
        else:
            print(f"   (未找到区块: {text})")

    focus("结论摘要", "08-结论卡片.png")
    focus("NDVI 均值", "09-地图与图例.png")
    focus("AOI", "10-AOI图层.png")

    c.close()
finally:
    proc.terminate(); time.sleep(1)
    try: proc.kill()
    except Exception: pass
print("完成")
