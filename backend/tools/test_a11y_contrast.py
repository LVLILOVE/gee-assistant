"""对比度实测：真的去浏览器里取计算后的颜色，而不是信手算的估算值。

为什么必须实测：
  1) 我手算的对比度是"设计稿上的值"，而真实渲染会被 antd 的派生色、
     半透明叠加、以及浏览器色彩管理改变；
  2) 只看 CSS 源码会漏掉"某处颜色是 antd 内部算出来的"这种情况。
所以这里用 CDP 拿到每个关键元素**实际生效的** color / background-color，
再按 WCAG 2.1 公式算对比度。
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
PORT = 9225
BASE = "http://127.0.0.1:8010"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import _env  # noqa: E402

user = _env("ADMIN_USERNAME", "admin")
pwd = _env("ADMIN_PASSWORD", "")
if not pwd:
    print("[FAIL] 未取到 ADMIN_PASSWORD（环境变量与 .env 都没有）—— 无法登录，测量无意义")
    sys.exit(1)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
profile = os.path.join(tempfile.gettempdir(), "wb_a11y_profile")
shutil.rmtree(profile, ignore_errors=True)
os.makedirs(profile, exist_ok=True)


class CDP:
    def __init__(self, ws):
        from urllib.parse import urlparse
        import socket
        u = urlparse(ws)
        self.s = socket.create_connection((u.hostname, u.port), timeout=30)
        k = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {k}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
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
    lg = c.js("fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},"
              "credentials:'include',body:JSON.stringify({username:%s,password:%s})})"
              ".then(r=>r.status).catch(e=>'err:'+e)"
              % (json.dumps(user), json.dumps(pwd)), aw=True)
    if lg != 200:
        print(f"[FAIL] 登录失败（HTTP {lg}）—— 量到的是登录页而不是应用页，结论无效")
        sys.exit(1)
    c.call("Page.navigate", {"url": BASE + "/"}); time.sleep(4)
    # ⚠ 必须断言真的进了应用内页：否则会"测了登录页"却报通过
    if not c.js("""(() => !!document.querySelector('.panel-right, .app-header'))()"""):
        print("[FAIL] 未进入登录后的应用界面 —— 测量结论无效")
        sys.exit(1)
    print("[setup] 已登录并进入应用界面")
    c.js("""(() => { const li=[...document.querySelectorAll('.ant-list-item')]
            .find(e=>/太湖流域/.test(e.textContent)); if(li) li.click(); })()""")
    time.sleep(11)

    probes = json.loads(c.js(r"""(() => {
      function effBg(el){
        let n = el;
        while (n && n !== document.documentElement) {
          const bg = getComputedStyle(n).backgroundColor;
          if (bg && bg !== 'transparent' && !/rgba\(0,\s*0,\s*0,\s*0\)/.test(bg)) return bg;
          n = n.parentElement;
        }
        return 'rgb(255,255,255)';
      }
      const S = (sel, label) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const cs = getComputedStyle(el);
        return { label, sel, color: cs.color, bg: effBg(el),
                 size: parseFloat(cs.fontSize), weight: cs.fontWeight };
      };
      const out = [
        S('.app-title', '应用标题'),
        S('.chat-hint', '对话提示文字'),
        S('.empty-state-desc', '空状态说明'),
        S('.empty-state-title', '空状态标题'),
        S('.conclusion-text', '结论正文'),
        S('.conclusion-note', '结论免责声明'),
        S('.legend-item', '图例文字'),
        S('.chat-msg.assistant .chat-bubble', '助手气泡文字'),
        S('.chat-msg.user .chat-bubble', '用户气泡文字'),
        S('.log-line', '执行日志'),
        S('.app-header .ant-tag', '状态标签'),
        S('.ant-btn-primary span', '主按钮文字'),
        S('.ant-list-item span', '历史任务标题'),
        S('.region-tags-label', '常用区域标签'),
      ].filter(Boolean);
      return JSON.stringify(out);
    })()""") or "[]")

    c.close()
finally:
    proc.terminate(); time.sleep(1)
    try: proc.kill()
    except Exception: pass


def parse(rgb):
    import re
    m = re.findall(r"[\d.]+", rgb)
    if len(m) < 3:
        return (255, 255, 255), 1.0
    r, g, b = (float(m[0]), float(m[1]), float(m[2]))
    a = float(m[3]) if len(m) > 3 else 1.0
    return (r, g, b), a


def lum(rgb):
    def f(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(fg, bg, alpha=1.0):
    # 前景若有透明度，先与背景做 alpha 合成（不做这步会高估对比度）
    f, a = parse(fg)
    b, _ = parse(bg)
    if a < 1.0:
        f = tuple(f[i] * a + b[i] * (1 - a) for i in range(3))
    l1, l2 = lum(f), lum(b)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


print("=" * 74)
print("WCAG 2.1 对比度实测（取值来自浏览器计算样式，非设计稿估算）")
print("=" * 74)
print(f"{'元素':<18}{'字号':>6}{'对比度':>9}{'门槛':>7}  判定")
print("-" * 74)

fails = []
for p in probes:
    r = ratio(p["color"], p["bg"])
    # WCAG：正常文字 4.5:1；≥18.66px 粗体 或 ≥24px 为「大字」，门槛 3:1
    big = p["size"] >= 24 or (p["size"] >= 18.66 and int(p["weight"]) >= 700)
    need = 3.0 if big else 4.5
    ok = r >= need
    if not ok:
        fails.append((p["label"], r, need))
    mark = "通过" if ok else "**不通过**"
    lvl = " AAA" if r >= 7.0 else ""
    print(f"{p['label']:<18}{p['size']:>5.0f}px{r:>9.2f}{need:>7.1f}  {mark}{lvl}")

print("-" * 74)

# ⚠ 结束语必须含「N/M 通过」—— regress_all.sh 靠这个正则解析结果。
#    少写这一行会因"没解析到结果行"被判失败，不是小事。
# ⚠ 探针数量下限是防"假通过"的最后一道闸：探针用 .filter(Boolean) 丢掉了
#    找不到的元素，一旦页面不是应用页（登录失效/报错/选择器改名），探针会
#    静默变少，然后"少数通过"就报 OK。初版就真的发生过：登录失效停在登录页，
#    只采到 1 个探针却报「[OK] 全部 1 处通过」。正常 >=12，低于 10 直接 fail。
if len(probes) < 10:
    print(f"[FAIL] 只采到 {len(probes)} 个探针（预期 >=12）—— 页面可能未正确加载")
    print("0/0 通过（对比度）")
    sys.exit(1)

if fails:
    print(f"[FAIL] {len(fails)} 处未达 AA 门槛：")
    for lbl, r, need in fails:
        print(f"   {lbl}: {r:.2f} < {need}")
    print(f"{len(probes) - len(fails)}/{len(probes)} 通过（对比度）")
    sys.exit(1)
print(f"最小对比度 {min(ratio(p['color'], p['bg']) for p in probes):.2f}:1")
print(f"{len(probes)}/{len(probes)} 通过（对比度）")
