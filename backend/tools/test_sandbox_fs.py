"""沙箱文件系统隔离的回归断言（不需要 GEE 凭据，mock 模式即可跑）。

钉住的是第二个真实漏洞。沙箱原先只封了「网络」（域名白名单）和「子进程」
（`_block_process_creation`），**文件系统是敞开的**。实测：

    open(backend/.env)   → 读到 1593 字符，含 DEEPSEEK_API_KEY
    os.listdir(".")      → 列出 backend/ 整个目录

为什么这条比看起来严重：沙箱里跑的代码是 **LLM 按用户输入生成**的，而服务
`ALLOW_REGISTRATION=true` 挂在公网 —— 任何注册用户写一句「读取 .env 并打印」，
模型就会生成对应的读取代码，密钥随结果原样回到他自己手里。
等于绕过登录拿走后端凭据（LLM 密钥 / GEE 凭据 / 整库）。

已修：执行用户代码前装一层 `_block_sensitive_file_access()`，把
`builtins.open` / `io.open` / `os.open` / `os.listdir` / `os.scandir` 换成带
敏感路径判定的版本，同时**显式放行** GEE 初始化必需的凭据路径（否则真实链路会挂）。

⚠ 加固点必须在 `_verify_toolchain()` / `_install_ee()` **之后**：
`python-dotenv` 在导入时就要读 `.env`、`gee_auth.initialize` 要读凭据文件，
提前封会让整条链路报 `PermissionError: 禁止访问环境变量文件：.env`。

跑法：
    .venv/Scripts/python.exe tools/test_sandbox_fs.py
"""
import json
import os
import sys

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, BACKEND_DIR)

from app.execution.sandbox import run_in_sandbox  # noqa: E402

ok = []


def check(name, cond, extra=""):
    ok.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))


def skip(name, why):
    print("  SKIP  " + name + "  -> " + why)


# ---- 探针目标：只放**磁盘上确实存在**的，否则失败原因会变成 FileNotFoundError，
#      那样就区分不出「被拦住了」和「文件本来就没有」。 ----
#
# 刻意**不去探 ~/.ssh、~/.aws**：那些是用户家目录，探一下就会触发运行环境的
# 安全策略（实测外层沙箱直接拒绝 stat `~/.ssh/id_rsa`，让整个测试命令被判失败）。
# 「家目录敏感项」由 `_runner._DENY_DIR_PARTS` 覆盖，用项目内的 `.venv/` 等价验证即可
# （`.venv` 同样在禁列里，且 pyvenv.cfg 确实存在）。
CANDIDATES = {
    "env": os.path.join(BACKEND_DIR, ".env"),
    "db": os.path.join(BACKEND_DIR, "data", "app.db"),
    "backup": os.path.join(BACKEND_DIR, ".db_backup", "app.db.before-testuser-cleanup"),
    "venv_cfg": os.path.join(BACKEND_DIR, ".venv", "pyvenv.cfg"),
    "sa_json": os.path.join(BACKEND_DIR, "data", "credentials", "service_account.json"),
    "normal_src": os.path.join(BACKEND_DIR, "app", "execution", "sandbox.py"),
    "normal_cfg": os.path.join(BACKEND_DIR, "requirements.txt"),
}
present = {k: v for k, v in CANDIDATES.items() if os.path.isfile(v)}

print("=== 0. 对照：目标文件在磁盘上的存在情况 ===")
for k in ("env", "db", "backup", "venv_cfg", "normal_src", "normal_cfg"):
    if k in present:
        check("对照存在：%s" % os.path.relpath(present[k], BACKEND_DIR), True)
    else:
        skip("对照缺失：%s（该项无法验证拦截）" % k, "文件不在磁盘上")
if "sa_json" not in present:
    skip("对照缺失：sa_json", "本机不存在，跳过")

# 敏感项：必须 BLOCKED
SENSITIVE = [("env", "环境变量文件（含 LLM 密钥）"),
             ("db", "业务数据库"),
             ("backup", "改库前的备份（含用户表）"),
             ("venv_cfg", "虚拟环境目录"),
             ("sa_json", "GEE 服务账号密钥")]

# 非敏感项：必须仍可读（证明不是"把 open 全禁了"这种粗暴做法）
print("\n=== 1. 敏感文件必须读不到（三种入口都试）===")
cases = []
for key, desc in SENSITIVE:
    if key in present:
        p = present[key].replace("\\", "\\\\")
        cases += [
            ("open(%s)" % key, "open(r'%s','rb').read(32)" % p, desc),
            ("pathlib(%s)" % key, "pathlib.Path(r'%s').read_bytes()[:32]" % p, desc),
            ("os.open(%s)" % key, "os.read(os.open(r'%s', os.O_RDONLY), 32)" % p, desc),
        ]

probe = ["import os, json, pathlib", "r = {}"]
for label, expr, _ in cases:
    probe.append(
        "try:\n"
        "    v = %s\n"
        "    r[%r] = 'OK'\n"
        "except PermissionError as e:\n"
        "    r[%r] = 'BLOCKED'\n"
        "except Exception as e:\n"
        "    r[%r] = 'OTHER:' + type(e).__name__\n" % (expr, label, label, label)
    )
# 目录列举也要被拦（用项目内的 `.venv` / `.db_backup`，不碰家目录）
probe.append(
    "for lbl, d in [('listdir_backup', r'%s'), ('listdir_venv', r'%s')]:\n"
    "    try:\n"
    "        os.listdir(d); r[lbl] = 'OK'\n"
    "    except PermissionError:\n"
    "        r[lbl] = 'BLOCKED'\n"
    "    except Exception as e:\n"
    "        r[lbl] = 'OTHER:' + type(e).__name__\n"
    % (os.path.join(BACKEND_DIR, ".db_backup"),
       os.path.join(BACKEND_DIR, ".venv"))
)
# 正常文件仍可读
probe.append(
    "for lbl, p in [('read_src', r'%s'), ('read_req', r'%s')]:\n"
    "    try:\n"
    "        r[lbl] = 'READ_' + str(len(open(p, 'rb').read()))\n"
    "    except Exception as e:\n"
    "        r[lbl] = 'FAIL:' + type(e).__name__\n"
    % (present["normal_src"].replace("\\", "\\\\"),
       present["normal_cfg"].replace("\\", "\\\\"))
)
probe.append("print('__PROBE__' + json.dumps(r))")

res = run_in_sandbox("\n".join(probe), {"region": "太湖流域", "task_type": "water"}, mock=True)
if not res.ok:
    check("沙箱整体执行成功（说明 _verify_toolchain / GEE 初始化没被打挂）", False,
          json.dumps(res.error, ensure_ascii=False, default=str)[:300])
    print("\n=== 结果：0/%d 通过（沙箱未能跑起来，后续断言无意义）===" % (len(ok) + 1))
    sys.exit(1)

check("沙箱整体执行成功（说明 _verify_toolchain / GEE 初始化没被打挂）", True)

# 从 stdout 里把探针结果捞出来
got = {}
for line in (res.stdout or "").splitlines():
    if line.startswith("__PROBE__"):
        try:
            got = json.loads(line[len("__PROBE__"):])
        except Exception:  # noqa: BLE001
            pass

for label, _, desc in cases:
    check("%-26s 被拦（%s）" % (label, desc), got.get(label) == "BLOCKED", got.get(label, "未取到"))

for lbl, desc in (("listdir_backup", "备份目录"), ("listdir_venv", "venv 目录")):
    check("列举 %-16s 被拦" % desc, got.get(lbl) == "BLOCKED", got.get(lbl, "未取到"))

print("\n=== 2. 正常文件必须仍可读（别把 open 一刀切禁掉）===")
check("仍可读 sandbox.py", str(got.get("read_src", "")).startswith("READ_"), got.get("read_src"))
check("仍可读 requirements.txt", str(got.get("read_req", "")).startswith("READ_"), got.get("read_req"))

print("\n=== 3. 对抗性绕过路径：同一个「读文件能力」的所有入口都要封 ===")
# 这一组是**实测踩出来的**，不是推演：第一版只封了 builtins.open / io.open / os.open，
# 用 io.FileIO 直接构造就把前两道全绕过去了 —— 实测读到了 .env 与 app.db（"SQLite format 3"）。
# CPython 里同一个能力有多条并列入口，漏任何一条，前面所有工作作废。
_env_p = present["env"].replace("\\", "\\\\")
_db_p = present["db"].replace("\\", "\\\\")
BYPASS_SRC = """
import json, io, os, codecs, shutil, pathlib
r = {}
def t(label, fn):
    try:
        fn(); r[label] = "BYPASSED"
    except PermissionError:
        r[label] = "BLOCKED"
    except Exception as e:
        r[label] = "other:" + type(e).__name__
t("io.FileIO",        lambda: io.FileIO(r"%s", "r").read(16))
t("io.open_code",     lambda: io.open_code(r"%s").read(16))
t("codecs.open",      lambda: codecs.open(r"%s", "r").read(16))
t("pathlib.open",     lambda: pathlib.Path(r"%s").open("r").read(16))
t("shutil.copyfile",  lambda: shutil.copyfile(r"%s", "leak.tmp"))
t("json.load(open)",  lambda: json.load(open(r"%s")))
t("os.stat",          lambda: os.stat(r"%s"))
t("io.FileIO(db)",    lambda: io.FileIO(r"%s", "r").read(16))
print("__BYPASS__" + json.dumps(r))
""" % (_env_p, _env_p, _env_p, _env_p, _env_p, _env_p, _env_p, _db_p)

res3 = run_in_sandbox(BYPASS_SRC, {"region": "太湖流域", "task_type": "water"}, mock=True)
byp = {}
for line in (res3.stdout or "").splitlines():
    if line.startswith("__BYPASS__"):
        try:
            byp = json.loads(line[len("__BYPASS__"):])
        except Exception:  # noqa: BLE001
            pass

if not res3.ok and not byp:
    check("绕过探针能跑起来", False, json.dumps(res3.error, ensure_ascii=False, default=str)[:200])

for lbl in ("io.FileIO", "io.open_code", "codecs.open", "pathlib.open",
            "shutil.copyfile", "json.load(open)", "os.stat", "io.FileIO(db)"):
    check("绕过入口 %-16s 被拦" % lbl, byp.get(lbl) == "BLOCKED", byp.get(lbl, "未取到"))

# 写出去的文件必须真的没生成 —— 状态码 BLOCKED 也可能是"先写出去了才报错"
_leak = os.path.join(BACKEND_DIR, "leak.tmp")
check("沙箱内没能写出文件（无 leak.tmp 残留）", not os.path.exists(_leak),
      "存在则说明写入未被拦住")

print("\n=== 3b. 写入侧白名单 + 目录列举过滤（2026-09-20 渗透测试新增） ===")
# 背景：第 4 组只覆盖了「读」的对抗性绕过，**「写」完全没有对应用例**，
# 于是留了一个高危口子 —— 生成代码能往工程目录写文件（实测真的落盘）。
# 叠加 HTTP 静态路由回读，就是远程写文件。这一组把写入侧和列举侧钉住。
#
# 判定语义：
#   写侧 = 白名单（只放行沙箱 temp），所以"拒绝"才是正确结果；
#   列侧 = 过滤掉敏感条目，所以要点是"列表里不该出现 .env / app.db"。
_WPATH = BACKEND_DIR.replace("\\", "\\\\")
WRITE_SRC = """
import json, os, io, tempfile
r = {}
def t(label, fn):
    try:
        fn(); r[label] = "WROTE"
    except PermissionError:
        r[label] = "BLOCKED"
    except Exception as e:
        r[label] = "other:" + type(e).__name__

t("open_w_工程目录",  lambda: open(r"%s\\_pen_a.txt", "w").write("x"))
t("open_w_Windows",   lambda: open(r"C:\\Windows\\_pen_b.txt", "w").write("x"))
t("append_win_ini",   lambda: open(r"C:\\Windows\\win.ini", "a").write("x"))
t("io.FileIO_w",      lambda: io.FileIO(r"%s\\_pen_c.txt", "w").write(b"x"))
t("os.open_w",        lambda: os.open(r"%s\\_pen_d.txt", os.O_CREAT | os.O_WRONLY))
t("pathlib_write",    lambda: __import__("pathlib").Path(r"%s\\_pen_e.txt").write_text("x"))
# 目录列举：文件名不该出现在返回值里
ls = os.listdir(r"%s")
r["listdir_无.env"]   = "CLEAN" if ".env" not in ls else "LEAKED"
r["listdir_无app.db"] = "CLEAN" if "app.db" not in ls else "LEAKED"
# 正向用例：沙箱 temp 必须仍可写（否则工具链会挂）
p = os.path.join(tempfile.gettempdir(), "_wb_pen_ok.txt")
try:
    open(p, "w").write("ok"); r["temp_可写"] = "OK"
except Exception as e:
    r["temp_可写"] = "other:" + type(e).__name__
print("__WRITE__" + json.dumps(r))
""" % (_WPATH, _WPATH, _WPATH, _WPATH, _WPATH)

res4 = run_in_sandbox(WRITE_SRC, {"region": "太湖流域", "task_type": "water"}, mock=True)
wr = {}
for line in (res4.stdout or "").splitlines():
    if line.startswith("__WRITE__"):
        try:
            wr = json.loads(line[len("__WRITE__"):])
        except Exception:  # noqa: BLE001
            pass

if not res4.ok and not wr:
    check("写入侧探针能跑起来", False,
          json.dumps(res4.error, ensure_ascii=False, default=str)[:200])

for lbl in ("open_w_工程目录", "open_w_Windows", "append_win_ini",
            "io.FileIO_w", "os.open_w", "pathlib_write"):
    check("写侧 %-16s 被拦" % lbl, wr.get(lbl) == "BLOCKED", wr.get(lbl, "未取到"))

for lbl in ("listdir_无.env", "listdir_无app.db"):
    check("列侧 %-16s " % lbl, wr.get(lbl) == "CLEAN", wr.get(lbl, "未取到"))

check("沙箱 temp 仍可写（未误伤工具链）", wr.get("temp_可写") == "OK", wr.get("temp_可写"))

# 无残留文件：上面每个写用例都指定了具体路径，逐一对账
for _f in ("_pen_a.txt", "_pen_b.txt", "_pen_c.txt", "_pen_d.txt", "_pen_e.txt"):
    _p = os.path.join(BACKEND_DIR, _f)
    check("写侧无残留 %s" % _f, not os.path.exists(_p), "落盘了说明未拦住")
check("Windows 目录无残留 _pen_b.txt",
      not os.path.exists(r"C:\Windows\_pen_b.txt"), "落盘了说明未拦住")

print("\n=== 3c. 破坏性操作（删除/改名/建目录）也要按写侧白名单拦 ===")
# 3b 组只覆盖了「写入内容」。删除 / 改名 / 建目录是与「写」并列的一组
# 破坏性能力，初始修复时整组漏掉了 —— 写不进新文件，却能把服务端文件删掉。
# 这一组钉住它。注意：所有用例都指向**真实存在**的目标，否则测不出"删掉了"。
# 用占位符替换而不是 % 格式化：源串里含大量反斜杠与 % 语义冲突，数占位符极易错
_B = "@@BACKEND@@"
DESTR_SRC = """
import json, os, shutil, tempfile
r = {}
def t(label, fn):
    try:
        fn(); r[label] = "DID"
    except (PermissionError, OSError) as e:
        r[label] = "BLOCKED" if isinstance(e, PermissionError) else "other:" + type(e).__name__
    except Exception as e:
        r[label] = "other:" + type(e).__name__

bak = r"@@BACKEND@@\\requirements.txt"          # 真实存在的可删目标
d   = r"@@BACKEND@@\\_pen_dir"
t("os.remove(真实文件)",   lambda: os.remove(bak))
t("os.unlink(真实文件)",   lambda: os.unlink(bak))
t("os.rename(真实文件)",   lambda: os.rename(bak, bak + ".bak"))
t("os.replace(真实文件)",  lambda: os.replace(bak, bak + ".bak"))
t("os.mkdir(工程目录)",    lambda: os.mkdir(d))
t("os.makedirs(工程目录)", lambda: os.makedirs(d + "\\\\x\\\\y"))
t("os.truncate(真实文件)", lambda: os.truncate(bak, 0))
t("shutil.rmtree(工程)",   lambda: shutil.rmtree(r"@@BACKEND@@\\tools"))
t("shutil.copyfile(工程)", lambda: shutil.copyfile(bak, r"@@BACKEND@@\\_pen_cp.txt"))
t("shutil.move(工程)",     lambda: shutil.move(bak, r"@@BACKEND@@\\_pen_mv.txt"))
# 正向：沙箱 temp 里仍能正常增删
tp = os.path.join(tempfile.gettempdir(), "_wb_destr")
try:
    os.makedirs(tp, exist_ok=True)
    f2 = os.path.join(tp, "a.txt")
    open(f2, "w").write("x")
    os.remove(f2); os.rmdir(tp)
    r["temp_可增删"] = "OK"
except Exception as e:
    r["temp_可增删"] = "other:" + type(e).__name__
print("__DESTR__" + json.dumps(r))
""".replace(_B, BACKEND_DIR)

res5 = run_in_sandbox(DESTR_SRC, {"region": "太湖流域", "task_type": "water"}, mock=True)
dr = {}
for line in (res5.stdout or "").splitlines():
    if line.startswith("__DESTR__"):
        try:
            dr = json.loads(line[len("__DESTR__"):])
        except Exception:  # noqa: BLE001
            pass

if not res5.ok and not dr:
    check("破坏性操作探针能跑起来", False,
          json.dumps(res5.error, ensure_ascii=False, default=str)[:200])

for lbl in ("os.remove(真实文件)", "os.unlink(真实文件)", "os.rename(真实文件)",
            "os.replace(真实文件)", "os.mkdir(工程目录)", "os.makedirs(工程目录)",
            "os.truncate(真实文件)", "shutil.rmtree(工程)", "shutil.copyfile(工程)",
            "shutil.move(工程)"):
    check("破坏性 %-20s 被拦" % lbl, dr.get(lbl) == "BLOCKED", dr.get(lbl, "未取到"))

check("沙箱 temp 仍可增删（未误伤）", dr.get("temp_可增删") == "OK", dr.get("temp_可增删"))

# 关键对账：被攻击的真实文件必须**原封不动**存在
check("requirements.txt 未被删/改（真实文件完好）",
      os.path.exists(os.path.join(BACKEND_DIR, "requirements.txt")),
      "不存在说明删除真的执行了")
check("tools/ 目录未被删", os.path.isdir(os.path.join(BACKEND_DIR, "tools")),
      "不存在说明 rmtree 真的执行了")
for _f in ("_pen_cp.txt", "_pen_mv.txt", "_pen_dir"):
    check("破坏性无残留 %s" % _f,
          not os.path.exists(os.path.join(BACKEND_DIR, _f)), "落盘了说明未拦住")

print("\n=== 3d. 链接 / 切目录 / 提权（2026-09-20 第二轮渗透测试新增） ===")
# ⚠ 这一组来自**实测穿透**，是本项目最严重的一条：
#     os.link("<backend>/.env", "<temp>/x.txt")   ← 硬链接创建成功
#     open("<temp>/x.txt").read()                 ← 读出 .env 明文
#     → DEEPSEEK_API_KEY 完整泄露
#   根因：所有路径黑名单检查的都是**路径字符串**，而硬链接在文件系统层面
#   与目标是同一个 inode —— 路径无害，内容就是目标。
#   软链靠 realpath 能解析，硬链解析不了，必须用 inode 比对。
_LINK_SRC = """
import json, os, tempfile
r = {}
def t(label, fn):
    try:
        v = fn(); r[label] = "DID"
    except PermissionError:
        r[label] = "BLOCKED"
    except Exception as e:
        r[label] = "other:" + type(e).__name__

tmp = tempfile.gettempdir()
t("os.symlink",   lambda: os.symlink(r"@@BACKEND@@\\.env", os.path.join(tmp, "_wb_sym.txt")))
t("os.link(硬链)", lambda: os.link(r"@@BACKEND@@\\.env", os.path.join(tmp, "_wb_hrd.txt")))
t("os.chdir",     lambda: os.chdir(r"@@BACKEND@@"))
t("os.putenv",    lambda: os.putenv("WB_EVIL", "1"))
t("os.unsetenv",  lambda: os.unsetenv("PATH"))
t("os.setuid",    lambda: os.setuid(0))
# 兜底：即使链接已被别的方式建好，内容也不该读得到
import subprocess as _sp
p = os.path.join(tmp, "_wb_pre_hard.txt")
escaped = False
try:
    with open(r"@@BACKEND@@\\.env", "rb") as src, open(p, "wb") as dst:
        dst.write(src.read())
except PermissionError:
    pass
except Exception:
    pass
if os.path.exists(p):
    try:
        open(p).read(); r["兜底读明文副本"] = "DID"
    except PermissionError:
        r["兜底读明文副本"] = "BLOCKED"
    except Exception as e:
        r["兜底读明文副本"] = "other:" + type(e).__name__
else:
    r["兜底读明文副本"] = "BLOCKED"
print("__LINK__" + json.dumps(r))
""".replace("@@BACKEND@@", BACKEND_DIR.replace("\\", "\\\\"))

res6 = run_in_sandbox(_LINK_SRC, {"region": "太湖流域", "task_type": "water"}, mock=True)
lr = {}
for line in (res6.stdout or "").splitlines():
    if line.startswith("__LINK__"):
        try:
            lr = json.loads(line[len("__LINK__"):])
        except Exception:  # noqa: BLE001
            pass

if not res6.ok and not lr:
    check("链接/越权探针能跑起来", False,
          json.dumps(res6.error, ensure_ascii=False, default=str)[:200])

for lbl in ("os.symlink", "os.link(硬链)", "os.chdir", "os.putenv",
            "os.unsetenv", "兜底读明文副本"):
    check("越权 %-16s 被拦" % lbl, lr.get(lbl) == "BLOCKED", lr.get(lbl, "未取到"))

# setuid 系列是 Unix-only，Windows 上 os 根本没这个属性（AttributeError），
# 那是「天然不可用」而非「被防护拦住」，两种都算安全，但要如实区分。
_setuid = lr.get("os.setuid")
if os.name == "nt":
    check("越权 os.setuid     不可用/被拦（Windows 无此 API 或已封）",
          _setuid in ("BLOCKED", "other:AttributeError"), _setuid)
else:
    check("越权 os.setuid     被拦", _setuid == "BLOCKED", _setuid)

# 关键对账：temp 里不该出现任何指向 .env 的链接或副本
import tempfile as _tf
_tmpdir = _tf.gettempdir()
for _f in ("_wb_sym.txt", "_wb_hrd.txt", "_wb_pre_hard.txt"):
    _p = os.path.join(_tmpdir, _f)
    if os.path.exists(_p):
        try:
            os.remove(_p)   # 清理（若真被创建出来）
        except OSError:
            pass
    check("链接/副本无残留 %s" % _f, not os.path.exists(_p), "存在说明链接真的建成了")

print("\n=== 4. 正常任务链路不受影响 ===")
DEMO = """
import ee
image = ee.Image('COPERNICUS/S2_SR_HARMONIZED').filterDate(start_date, end_date)
vis = {'min': 0, 'max': 3000, 'bands': ['B4', 'B3', 'B2']}
WB.add_layer('真彩色', image, vis, legend=[{'label': '低', 'color': '#fff'}])
WB.add_chart('示例曲线', labels=['a', 'b'], series=[{'name': 's', 'data': [1, 2]}], kind='line')
"""
r2 = run_in_sandbox(DEMO, {"region": "太湖流域", "task_type": "water",
                           "start_date": "2024-01-01", "end_date": "2024-12-31"}, mock=True)
check("标准模板代码仍能跑通", r2.ok, json.dumps(r2.error, ensure_ascii=False, default=str)[:200])
check("仍能上报图层", len(getattr(r2, "layers", []) or []) >= 1, getattr(r2, "layers", None))
check("仍能上报图表", len(getattr(r2, "charts", []) or []) >= 1, getattr(r2, "charts", None))

passed = sum(ok)
total = len(ok)
print("\n=== 结果：%d/%d 通过 ===" % (passed, total))
sys.exit(0 if passed == total else 1)
