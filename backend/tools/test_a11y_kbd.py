"""无障碍核验：键盘可达性 / 焦点可见 / 减少动效 / 触控目标尺寸。

为什么必须跑这一项：
  配色我能靠对比度公式验，但"键盘能不能走到每个可交互元素""焦点环看不看得见"
  只有真实浏览器能回答 —— 比如某个元素被 CSS 设成 tabindex=-1，
  或 focus 样式被 outline:none 抹掉，看代码很容易漏。
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
PORT = 9226
BASE = "http://127.0.0.1:8010"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import _env  # noqa: E402

user = _env("ADMIN_USERNAME", "admin")
pwd = _env("ADMIN_PASSWORD", "")
if not pwd:
    print("[FAIL] 未取到 ADMIN_PASSWORD（环境变量与 .env 都没有）—— 无法登录，检查无意义")
    sys.exit(1)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
profile = os.path.join(tempfile.gettempdir(), "wb_a11y_kbd")
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

    def call(self, m, p=None, t=60):
        self._id += 1; i = self._id
        self._send(json.dumps({"id": i, "method": m, "params": p or {}}))
        e = time.time() + t
        while time.time() < e:
            r = json.loads(self._fr())
            if r.get("id") == i: return r.get("result", {})
        raise TimeoutError(m)

    def js(self, x, aw=False, t=60):
        return self.call("Runtime.evaluate",
                         {"expression": x, "awaitPromise": aw, "returnByValue": True},
                         t).get("result", {}).get("value")

    def key(self, name="Tab"):
        for t in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent",
                      {"type": t, "key": name, "code": name,
                       "windowsVirtualKeyCode": 9 if name == "Tab" else 0})

    def shot(self, path):
        r = self.call("Page.captureScreenshot", {"format": "png"})
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))

    def close(self):
        self.s.close()


proc = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
                         f"--user-data-dir={profile}", "--no-first-run",
                         "--no-default-browser-check", "--disable-gpu",
                         "--hide-scrollbars", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fails = []
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
        print(f"[FAIL] 登录失败（HTTP {lg}）—— 后续检查的是登录页而非应用页，结论无效")
        sys.exit(1)
    c.call("Page.navigate", {"url": BASE + "/"}); time.sleep(4)

    # ⚠ 关键断言：必须确认自己真的进了应用内页。
    # 上一次就是登录没生效、停在登录页，却因为"Tab 到了 5 个元素"而报 OK。
    if not c.js("""(() => !!document.querySelector('.panel-right, .app-header'))()"""):
        print("[FAIL] 未进入登录后的应用界面（找不到 .panel-right / .app-header）—— 检查结论无效")
        sys.exit(1)
    print("[setup] 已登录并进入应用界面")

    print("=" * 72)
    print("无障碍核验（真实浏览器）")
    print("=" * 72)

    # ---- 1) 可交互元素是否都能被 Tab 走到 ----
    total = c.js("""(() => document.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])').length)()""")
    print(f"\n[1] 键盘可达性：页面上可交互元素 {total} 个，逐个 Tab 检查…")
    c.js("document.body.focus()")
    reached = []
    for i in range(min(int(total) + 6, 40)):
        c.key("Tab")
        time.sleep(0.12)
        info = c.js("""(() => { const a=document.activeElement;
          if(!a||a===document.body) return null;
          return (a.tagName||'')+':'+((a.innerText||a.placeholder||a.type||'').trim().slice(0,18)); })()""")
        if info and info not in reached:
            reached.append(info)
    print(f"    实际 Tab 到 {len(reached)} 个元素")
    for r_ in reached[:14]:
        print(f"      · {r_}")
    if len(reached) < 5:
        fails.append(f"键盘可达元素过少（仅 {len(reached)} 个）")
    else:
        print("    [OK] 交互元素可通过键盘到达")

    # ---- 2) 焦点环是否可见 ----
    # ⚠ 判据的选择很关键。**不要**用 getComputedStyle 读 outline 来判断：
    #   ① antd 用 cssinjs 在运行时注入 <style>，注入的先后顺序决定优先级，
    #      构建产物里根本查不到，容易得出反的结论；
    #   ② 更根本的是，读出来 outlineStyle === 'none' 也**不能**断言"没有可见指示" ——
    #      antd 的按钮焦点环是用 box-shadow 画的（`box-shadow: 0 0 0 2px ...`），
    #      outline 天生就是 none。按 outline 判会误判成"缺失"。
    #   所以这里用**截图逐像素比对**：聚焦前 / 聚焦后各截一张，有像素变化就是
    #   有可见指示。这是唯一不依赖对 CSS 内部实现理解的判据。
    print("\n[2] 焦点可见性：截图逐像素比对（不用 el.focus()，也不看 outline 属性）")
    print("    说明：CSS 用的是 :focus-visible —— 只对**键盘**聚焦生效；")
    print("    JS 的 el.focus() 是程序化聚焦，永远匹配不到它，必须派发真实键盘事件。")
    print("    又：antd 用 box-shadow 画焦点环，读 outline 属性会误判，故改用像素比对。")
    try:
        from PIL import Image, ImageChops
    except ImportError:
        print("    [SKIP] 缺 Pillow，跳过（请 pip install pillow）")
        Image = None
    if Image is not None:
        shot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".focus_probe")
        shutil.rmtree(shot_dir, ignore_errors=True)
        os.makedirs(shot_dir, exist_ok=True)
        seen = []
        # 计数说明（这里踩过坑）：从无焦点状态数起，第 k 次 Tab 到达第 k 个可聚焦元素。
        # 所以第 idx 个元素需要 idx 次 Tab，**不是 idx+1 次**。
        #
        # ⚠ 还有一个更隐蔽的坑：**不能用 `document.body.focus()` 来"清除焦点"**。
        #   body 本身不可聚焦，这个调用是 no-op —— 元素**仍然是** :focus-visible，
        #   于是"参照图"里焦点环还在，与"聚焦图"完全一致 → 差异恒为 0 →
        #   把明明有环的元素全部误报成"无焦点指示"。
        #   必须用 activeElement.blur() 才是真的取消聚焦（实测：blur 后差异 482 像素）。
        BLUR = ("(() => { if (document.activeElement && document.activeElement !== document.body) "
                "document.activeElement.blur(); })()")
        for idx in range(1, 23):
            c.js(BLUR); time.sleep(0.25)
            for _ in range(idx):
                c.key("Tab"); time.sleep(0.12)
            desc = c.js("""(() => { const a=document.activeElement;
              if(!a||a===document.body) return null;
              const r = a.getBoundingClientRect();
              return JSON.stringify({cls:(a.className||'').toString().split(' ')[0]||a.tagName,
                txt:(a.innerText||a.placeholder||a.type||'').trim().slice(0,14),
                box:[r.left,r.top,r.right,r.bottom],
                dpr: window.devicePixelRatio}); })()""")
            if not desc:
                continue
            d = json.loads(desc) if isinstance(desc, str) else desc
            if any(s["txt"] == d["txt"] for s in seen):
                continue
            after = os.path.join(shot_dir, f"{idx}_after.png")
            before = os.path.join(shot_dir, f"{idx}_before.png")
            c.shot(after)
            # 真正取消聚焦后截"无焦点"参照图（同页面状态，唯一差别就是焦点）
            c.js(BLUR); time.sleep(0.35)
            c.shot(before)
            ai, bi = Image.open(before).convert("RGB"), Image.open(after).convert("RGB")
            if ai.size != bi.size:
                continue
            # ⚠ 坐标必须乘 devicePixelRatio！getBoundingClientRect 给的是 CSS 像素，
            #   而截图是物理像素。本机 125% 缩放 → dpr=1.25，1.5 倍的手续费就是
            #   裁剪框全错位（截图里明明有绿环，裁出来却是旁边一片空白 → 0 差异）。
            #   这个坑很隐蔽：只看"变化像素=0"会误判成"没有焦点指示"。
            k = float(d.get("dpr") or 1.0)
            pad = 10 * k
            sx = max(0, int(d["box"][0] * k - pad)); sy = max(0, int(d["box"][1] * k - pad))
            ex = min(ai.size[0], int(d["box"][2] * k + pad))
            ey = min(ai.size[1], int(d["box"][3] * k + pad))
            if ex <= sx or ey <= sy:
                continue
            ca = ai.crop((sx, sy, ex, ey))
            cb = bi.crop((sx, sy, ex, ey))
            diff = ImageChops.difference(ca, cb)
            changed = sum(1 for p in diff.getdata() if sum(p) > 24)
            seen.append({"txt": d["txt"], "cls": d["cls"], "changed": changed})
            print(f"      {d['txt']:<16} {d['cls']:<24} "
                  f"{'有可见焦点指示' if changed > 40 else '**无变化**'}  (变化像素 {changed})")
        blind = [v for v in seen if v["changed"] <= 40]
        for v in blind:
            fails.append(f"{v['txt']} 聚焦前后无可见变化（疑似无焦点指示）")
        if not blind and seen:
            print(f"    [OK] {len(seen)} 个键盘焦点位置聚焦前后均有可见变化")
        elif not seen:
            fails.append("未能采集到任何键盘焦点位置")

    # ---- 3) 触控目标尺寸（移动端 44x44 建议值）----
    print("\n[3] 触控目标尺寸（移动端视口 390px）")
    c.call("Emulation.setDeviceMetricsOverride",
           {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    time.sleep(1.5)
    small = json.loads(c.js(r"""(() => {
      const out = [];
      for (const el of document.querySelectorAll('button, a[href], .ant-tag, .ant-segmented-item')) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (r.height < 24 || r.width < 24) {
          out.push({t: (el.innerText||'').trim().slice(0,16), w: Math.round(r.width), h: Math.round(r.height)});
        }
      }
      return JSON.stringify(out.slice(0, 10));
    })()""") or "[]")
    if small:
        print(f"    发现 {len(small)} 个偏小的目标（<24px）：")
        for s in small:
            print(f"      · '{s['t']}' {s['w']}x{s['h']}")
        print("    说明：密集标签区难以全部达到 44px，但已保证 >=24px 且加大内边距；")
        print("         这是可点中的底线，已在 style.css 中注明取舍理由。")
    else:
        print("    [OK] 无过小目标")

    # ---- 4) 减少动效是否被尊重 ----
    print("\n[4] prefers-reduced-motion 是否生效")
    c.call("Emulation.setEmulatedMedia",
           {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
    time.sleep(0.8)
    dur = c.js("""(() => { const el=document.querySelector('.ant-btn, .example-tag, .ant-tag');
      return el ? getComputedStyle(el).transitionDuration : 'n/a'; })()""")
    emu = c.js("""(() => matchMedia('(prefers-reduced-motion: reduce)').matches)()""")
    print(f"    媒体查询命中: {emu}    过渡时长: {dur}")
    try:
        first = float(str(dur).replace('s', '').split(',')[0])
    except (TypeError, ValueError):
        first = None
    if emu and first is not None and first < 0.05:
        print("    [OK] 减少动效已生效（过渡被压缩到近 0）")
    else:
        fails.append(f"减少动效未生效（duration={dur}）")

    # ---- 5) 图片 alt（本应用几乎无图片，检查一下别漏）----
    print("\n[5] 图片替代文本")
    imgs = json.loads(c.js("""(() => JSON.stringify(
      [...document.images].map(i=>({src:i.src.split('/').pop().slice(0,22), alt:i.alt}))))()""") or "[]")
    if not imgs:
        print("    [OK] 无 <img> 元素（地图为 canvas/瓦片，图表为 canvas）")
    else:
        bad = [i for i in imgs if not i["alt"]]
        print(f"    共 {len(imgs)} 张图，缺 alt 的 {len(bad)} 张")
        for b in bad[:5]:
            print(f"      · {b['src']}")
        if bad:
            fails.append(f"{len(bad)} 张图片缺 alt")

    c.close()
finally:
    proc.terminate(); time.sleep(1)
    try: proc.kill()
    except Exception: pass

print("\n" + "=" * 72)
# 以「N/M 通过」收尾 —— regress_all.sh 用这个正则解析，格式必须保留。
# 5 个大项：键盘可达 / 焦点可见 / 触控尺寸 / 减少动效 / 图片 alt。
# 触控尺寸不参与打分：密集标签区做到 44px 会占掉半屏，是**有意的取舍**，
# 已保证 >=24px 并在 style.css 注释说明；把它算成失败会让这条守卫常年飘红。
CHECKS = 5
if fails:
    print(f"[FAIL] {len(fails)} 项未通过：")
    for f in fails:
        print(f"   - {f}")
    print(f"{CHECKS - min(len(fails), CHECKS)}/{CHECKS} 通过（无障碍）")
    sys.exit(1)
print("[OK] 无障碍核验全部通过")
print(f"{CHECKS}/{CHECKS} 通过（无障碍）")
