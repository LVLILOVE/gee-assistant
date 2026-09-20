"""受限沙箱（子进程内运行）。

由 sandbox.py 以独立进程启动，stdin 收 JSON，stdout 回哨兵行 JSON。
在此进程内施加：模块黑名单、网络白名单、**敏感文件禁读禁写**、递归/输出限额、
GEE 结果上报契约。
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import traceback

_SENTINEL = "__WB_JSON__"
_MAX_OUTPUT_CHARS = 20000

# 危险模块黑名单。
#
# ⚠️ 设计原则：**能进黑名单的，只有「实测确认不影响工具链导入」的模块**。
# stdlib 与第三方库之间的 import 关系是隐式的，凭直觉拉黑一个模块往往会
# 「连带打死自己」，而且报错发生在很下游、极难定位。踩过的坑：
#
#   shutil             ← Python 3.13 的 tempfile 会 import 它，dotenv 依赖 tempfile
#   subprocess         ← google.auth.transport._mtls_helper 在**模块级** import 它
#   msvcrt / signal    ← tempfile(Win) 与 subprocess 分别依赖
#   importlib.machinery← 导入系统自身依赖
#
# 这些一律改为「封禁能力」而非「封禁模块」：
#   * socket     → 域名白名单            （见 _harden）
#   * subprocess → 进程创建能力封禁      （见 _block_process_creation）
#   * 文件系统   → 敏感文件禁读禁写      （见 _block_sensitive_file_access）
#
# 下方清单是用 tools 侧的探测（逐进程验证工具链导入）实测得出的。
# 修改本集合后**必须**跑 `gee_doctor.py --self-test`，它内含同样的校验。
_BLOCKED = {
    "ctypes", "multiprocessing", "http.server", "pty", "telnetlib", "smtplib",
    "pickle", "winreg", "code", "codeop",
}

# 真实执行链路在导入阶段会拉起的模块。_verify_toolchain() 用它确保
# 黑名单没有误伤运行依赖 —— 这是防止上面两个坑复发的自动闸门。
_TOOLCHAIN_MODULES = (
    "app.config",
    "app.gee_auth",
    "ee",
    "requests",
    "google.auth.transport.requests",
)


def _verify_toolchain() -> None:
    """校验黑名单没有误伤运行所需的模块。

    为什么必须验：黑名单是按「危险程度」挑的，但 stdlib 内部互相 import，
    很容易连带打死自己。这类错误只在真实执行时暴露，而且会被后续的降级
    import 掩盖成 `ModuleNotFoundError: No module named 'gee_auth'`。
    mock 自检也要跑这一步，否则会给出「沙箱全绿」的假象。
    """
    import importlib

    for mod in _TOOLCHAIN_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError as e:
            if "沙箱安全策略" in str(e):
                raise RuntimeError(
                    f"沙箱模块黑名单误伤了运行必需的模块（{mod}）：{e}。"
                    f"请改为「封禁能力」而不是「封禁模块」，参见 _runner._BLOCKED 注释。"
                ) from e
            raise


def _block_process_creation() -> None:
    """封禁「创建子进程」这一能力（保留 subprocess / os 模块本身可导入）。

    与 socket 的处理同一思路：google-auth 在模块级 import subprocess，
    拉黑模块会打死整条链路；但沙箱确实不该允许生成代码去起进程。
    所以保留模块、掐掉能力。

    ⚠️ 两个必须遵守的细节（都踩过）：

    1. `subprocess.Popen` 必须替换成**类**而不是函数。
       `asyncio.windows_utils` 里有 `class Popen(subprocess.Popen):` —— 在类定义
       阶段就继承它。换成函数会让整个 asyncio 导入炸掉，进而打死 google-auth
       （报错还是完全不相干的 `TypeError: function() argument 'code' must be code`）。
       而 google-auth 又会 import asyncio，所以这是个必炸项。

    2. 要顺手封 `_winapi.CreateProcess`。asyncio 的 Windows 版 Popen 绕过
       `subprocess.Popen` 直接调它，只封 subprocess 会留个后门。
    """
    import os
    import subprocess

    def _deny(*_a, **_k):
        raise PermissionError("沙箱安全策略禁止创建子进程 / 执行外部命令")

    class _DeniedPopen:
        """只用于封禁实例化；允许被继承（asyncio 需要）。"""

        def __init__(self, *_a, **_k):
            raise PermissionError("沙箱安全策略禁止创建子进程（Popen）")

    subprocess.Popen = _DeniedPopen  # type: ignore[assignment,misc]

    for name in ("run", "call", "check_call", "check_output",
                 "getoutput", "getstatusoutput"):
        if hasattr(subprocess, name):
            setattr(subprocess, name, _deny)

    for name in ("system", "popen", "startfile", "fork", "forkpty", "kill", "killpg",
                 "abort", "execv", "execve", "execvp", "execvpe",
                 "execl", "execle", "execlp", "execlpe",
                 "spawnv", "spawnve", "spawnvp", "spawnvpe",
                 "spawnl", "spawnle", "spawnlp", "spawnlpe"):
        if hasattr(os, name):
            setattr(os, name, _deny)

    # asyncio 的 Windows Popen 直接走 _winapi，需单独封（否则留后门）
    try:
        import _winapi

        _winapi.CreateProcess = _deny  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass


class _Blocker:
    """import 钩子：命中黑名单即抛错。"""

    def find_module(self, name, path=None):  # pragma: no cover - 兼容旧式
        return self if name.split(".")[0] in _BLOCKED else None

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ImportError(f"沙箱安全策略禁止导入模块：{name}")
        return None


# ---------------------------------------------------------------------------
# 文件系统：敏感文件禁读禁写
# ---------------------------------------------------------------------------
# 为什么补这一层：沙箱原先只封了「网络」和「子进程」，**文件系统是敞开的**。
# 实测（mock 模式即可复现）沙箱里的代码能：
#     open(backend/.env)      → 读到 DEEPSEEK_API_KEY
#     os.listdir(".")         → 列出 backend/ 整个目录
# 而生成代码是 LLM 按**用户输入**产出的，服务又开放注册 ——
# 任何注册用户写一句「读取 .env 并打印」，密钥就随结果回到他自己手里。
# 这等于绕过登录拿走后端凭据，是比网络白名单更值钱的一条路。
#
# 但不能一刀切禁掉 `data/`：GEE 初始化（`_install_ee` → `gee_auth.initialize`）
# 就在这个进程里读凭据文件。所以策略是
#   「精确放行凭据文件与 earthengine 配置目录 + 其余敏感项一律禁」。
_ALLOW_PATH_FRAGMENTS = (
    "data/credentials",           # 加密凭据库默认落点
    "data\\credentials",
    "/.config/earthengine",       # 个人 OAuth 默认落点
    "/appdata/roaming/earthengine",
)
_DENY_SUFFIXES = (".db", ".db-wal", ".db-shm", ".key", ".pem", ".p12", ".ppk", ".keystore")
_DENY_DIR_PARTS = (".db_backup", ".git", ".venv", ".ssh", ".aws", ".kube", ".gnupg")
_DENY_BASENAME_GLOBS = (
    "*service_account*.json", "service-account*.json", "*-key.json",
    "id_rsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
)

# ---------------------------------------------------------------------------
# 写入侧：**白名单**模型（2026-09-20 对抗性测试后重写）
# ---------------------------------------------------------------------------
# 为什么和「读」用两套模型：
#   原实现是纯黑名单 —— 只拦 .env / *.db / .ssh 这类"已知敏感"路径，
#   读之外的所有路径一律放行。对抗性测试实测（9 项里穿透 4 项）：
#       open(r'C:/Windows/win.ini').read()   → 成功读到系统文件
#       os.listdir('C:/')                     → 成功列出 C 盘
#       os.stat('C:/Windows')                 → 成功探测
#       open(r'<工程目录>/hacked.txt','w')     → **成功在服务端磁盘写文件**
#   最后一条最要命：任何注册用户都能让服务端在自己磁盘上写任意内容，
#   叠加"可由 HTTP 静态路由回读"就是远程写文件 → 可升级为代码执行。
#
#   黑名单的固有缺陷是「没想到的就漏」—— 磁盘上的路径无穷多，
#   而生成代码**没有任何正当理由**去写工程目录。所以写入侧翻转为白名单：
#   只放行沙箱自己的临时目录，其余一律拒绝。这样"漏"的方向是安全的（拒绝）。
#
#   读侧保持「默认放行 + 黑名单」，因为 GEE 工具链要读 matplotlib 字体、
#   certifi 证书、torch 权重等大量第三方路径，白名单维护不过来；
#   而读侧的敏感目标是**可枚举**的（凭据、密钥、数据库）。
_ALLOW_WRITE_ENV = "WB_SANDBOX_TMP"
_write_allow_roots: list[str] = []


def _set_write_roots() -> None:
    """初始化写入白名单根目录（沙箱临时目录 + 系统 temp）。

    在 `_block_sensitive_file_access()` 里调用，把 `tempfile.gettempdir()`
    与 `WB_SANDBOX_TMP` 指到的目录纳入。两者都规范化成小写正斜杠形式，
    与 `_sensitive_reason` 里的 `low` 保持同一坐标系。
    """
    roots: list[str] = []
    cands = [os.getenv(_ALLOW_WRITE_ENV), tempfile.gettempdir()]
    for c in cands:
        if not c:
            continue
        try:
            roots.append(os.path.realpath(str(c)).replace("\\", "/").lower().rstrip("/"))
        except (OSError, ValueError):
            pass
    _write_allow_roots[:] = roots


def _write_reason(path) -> str | None:
    """写入目标是否被允许。返回原因字符串表示**拒绝**，None 表示放行。

    判定顺序：
      1. 命中 `_ALLOW_PATH_FRAGMENTS`（GEE 凭据目录）→ 放行（工具链可能需要）
      2. 落在白名单根目录下 → 放行
      3. 其余 → 拒绝
    """
    if isinstance(path, int):  # os.open 允许传 fd
        return None
    try:
        real = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError, TypeError):
        return None
    low = real.replace("\\", "/").lower()
    for frag in _ALLOW_PATH_FRAGMENTS:
        if frag in low:
            return None
    for root in _write_allow_roots:
        if low == root or low.startswith(root + "/"):
            return None
    return "非沙箱临时目录"


def _allowed_sensitive_paths() -> set[str]:
    """GEE 初始化必须读到的具体文件 —— 显式放行，否则真实执行链路会直接挂。

    `GEE_SERVICE_ACCOUNT_JSON` 可以指向任意位置的任意文件名（例如用户放在
    `D:\\keys\\my.json`），只靠 `data/credentials` 这个片段放行是不够的，
    所以这里把环境变量指到的**确切路径**也算进来。
    """
    allow: set[str] = set()

    def add(p: str | None) -> None:
        if not p:
            return
        try:
            allow.add(os.path.realpath(os.path.expanduser(str(p))).replace("\\", "/").lower())
        except (OSError, ValueError):
            pass

    add(os.getenv("GEE_CREDENTIALS_ENC"))
    add(os.getenv("GEE_SERVICE_ACCOUNT_JSON"))
    add(os.getenv("GEE_OAUTH_CREDENTIALS"))
    return allow


_ALLOW_EXACT = _allowed_sensitive_paths()


# 原始 os.stat 的缓存。为什么必须缓存（2026-09-20 踩过，症状极难定位）：
#   `os.stat` 会被 `_block_sensitive_file_access()` 替换成 guard 版本，
#   而 guard 里要调 `_sensitive_reason()`，`_sensitive_reason()` 又要调
#   `_hardlink_reason()` 去 `stat` —— 如果那里用 `os.stat` 就是**无限递归**。
#   实测症状不是干脆的报错，而是：先出 `RecursionError`，
#   接着**沙箱子进程卡死到外层超时**，表现为「整个测试挂住不返回」。
_raw_stat = os.stat

# 敏感文件的 inode 指纹：(st_dev, st_ino) → 说明文字。
# 硬链接无法靠路径识别（硬链没有"目标"概念，两个名字等价、realpath 原样返回），
# 但它们在文件系统层面是**同一个 inode** —— 所以 inode 是唯一可靠的判据。
_sensitive_inodes: dict[tuple, str] = {}


def _collect_sensitive_inodes() -> None:
    """在加固前扫描项目内已知的敏感文件，记下它们的 inode。

    扫描范围刻意**收窄到项目目录 + 常见敏感名**，不做全盘遍历：
      * 全盘 stat 太慢（沙箱启动每次都跑，会拖垮任务耗时）；
      * 需要防的是「**本服务自己的**凭据/数据库被读走」——
        攻击者的目标是 `DEEPSEEK_API_KEY` 与 `app.db`，不是系统里任意文件。
    另外 `_ALLOW_EXACT`（GEE 凭据，工具链要读）里已放行的路径**不记入**，
    否则会把正常初始化也拦住。
    """
    import fnmatch

    cands: list[str] = []
    try:
        here = os.path.dirname(os.path.abspath(__file__))          # .../app/execution
        backend = os.path.dirname(os.path.dirname(here))            # .../backend
    except (OSError, ValueError):
        return

    # 项目目录下逐层找敏感文件（只走两层，够覆盖 .env / data/*.db / 凭据目录）
    for root, dirs, files in os.walk(backend):
        depth = root[len(backend):].count(os.sep)
        if depth > 2:
            dirs[:] = []
            continue
        for fn in files:
            low = fn.lower()
            hit = (low.startswith(".env")
                   or low.endswith(_DENY_SUFFIXES)
                   or any(fnmatch.fnmatch(low, g) for g in _DENY_BASENAME_GLOBS))
            if hit:
                cands.append(os.path.join(root, fn))

    for p in cands:
        try:
            rp = os.path.realpath(p)
            if rp.replace("\\", "/").lower() in _ALLOW_EXACT:
                continue
            st = _raw_stat(rp)
            # 用 (dev, ino) 做唯一键，天然去重：硬链接会指向同一 inode，
            # 这里覆盖写入即可，不会重复计数。
            _sensitive_inodes[(st.st_dev, st.st_ino)] = os.path.basename(p)
        except (OSError, ValueError):
            continue


def _hardlink_reason(real_path: str) -> str | None:
    """路径本身不敏感，但它的 inode 与某个敏感文件相同 → 是硬链接 → 拒绝。

    ⚠ 必须用 `_raw_stat` 而不是 `os.stat`（后者是 guard，会递归回本函数）。
    """
    if not _sensitive_inodes:
        return None
    try:
        st = _raw_stat(real_path)
    except (OSError, ValueError):
        return None
    name = _sensitive_inodes.get((st.st_dev, st.st_ino))
    if name:
        return f"敏感文件（疑似硬链接指向 {name}）"
    return None


def _sensitive_reason(path) -> str | None:
    """命中敏感路径返回原因字符串，否则 None。

    ⚠ 这里必须同时处理**软链和硬链**（2026-09-20 实测穿透）：
      * 软链：`os.path.realpath` 会解析到最终目标，`low` 自然是目标的路径 → 已被覆盖。
      * **硬链：`realpath` 解析不了**（硬链没有"目标"概念，两个名字等价，
        `realpath` 原样返回）。所以硬链必须**另用 inode 比对**：
        若该路径的 (st_dev, st_ino) 与任一敏感文件相同 → 就是同一份数据 → 拒绝。
    """
    if isinstance(path, int):  # os.open 允许传 fd
        return None
    try:
        real = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError, TypeError):
        return None
    low = real.replace("\\", "/").lower()
    if low in _ALLOW_EXACT:
        return None
    for frag in _ALLOW_PATH_FRAGMENTS:
        if frag in low:
            return None
    import fnmatch

    base = low.rsplit("/", 1)[-1]
    if base.startswith(".env"):
        return "环境变量文件"
    if base.endswith(_DENY_SUFFIXES):
        return "数据库或私钥文件"
    if any(fnmatch.fnmatch(base, g) for g in _DENY_BASENAME_GLOBS):
        return "服务账号密钥"
    parts = low.split("/")
    for d in _DENY_DIR_PARTS:
        if d in parts:
            return f"目录 {d}"

    # 硬链接检测：路径本身不敏感，但 inode 与敏感文件相同 → 同一份数据
    hard = _hardlink_reason(real)
    if hard:
        return hard

    return None


def _block_link_creation() -> None:
    """封禁链接创建 —— 这是「绕过路径检查」的经典手法。

    ⚠ 2026-09-20 对抗性测试实测的**真实穿透**，不是推演：

        os.link("<backend>/.env", "<temp>/x.txt")   # 硬链接成功
        open("<temp>/x.txt").read()                 # 读出了 .env 明文！
        → DEEPSEEK_API_KEY=sk-<已脱敏>... 完整泄露

    **为什么所有路径黑名单都拦不住它**：
    硬链接在文件系统层面与目标是**同一个 inode**（同一份数据、两个名字）。
    `_sensitive_reason()` 检查的是「传入的路径字符串」——
    `<temp>/x.txt` 这个名字完全无害，可它的内容就是 `.env`。
    同理 `os.symlink` 建的软链也有类似问题（`realpath` 能解析软链，
    但**建链接这一步本身**就该拦——没有理由让生成代码去建链接）。

    判定：**一律禁止创建链接**。生成代码做遥感分析没有任何理由需要 symlink/hardlink。
    """
    def _deny(name: str):
        def wrapper(*a, **k):
            raise PermissionError(
                f"沙箱安全策略禁止创建链接（os.{name}）：链接可绕过路径检查读取敏感文件"
            )

        return wrapper

    for _nm in ("symlink", "link", "symlinkat", "linkat"):
        if hasattr(os, _nm):
            setattr(os, _nm, _deny(_nm))


def _block_misc_escapes() -> None:
    """封禁剩余的越权能力：切目录 / 提权 / 改环境变量。

    2026-09-20 覆盖审计查出的漏项（`os.chdir`/`setuid`/`putenv`）：
      * `chdir`  —— 本身不直接泄密（相对路径仍会被 `_sensitive_reason` 按真实路径判），
        但它会**改变沙箱进程的全局状态**，让后续所有相对路径判定依赖当前目录，
        增加不确定性。没有正当理由，关掉。
      * `setuid`/`setgid`/`seteuid`/`setegid` —— 提权，必须在最外层就断掉。
      * `putenv`/`unsetenv` —— 改环境变量可能影响后续子进程行为
        （如伪造 `GEE_*` 或 `PYTHONPATH`）。
    """
    def _deny(name: str, why: str):
        def wrapper(*a, **k):
            raise PermissionError(f"沙箱安全策略禁止调用 os.{name}（{why}）")

        return wrapper

    for _nm, _why in (("chdir", "改变沙箱工作目录"),
                      ("chroot", "改变根目录"),
                      ("setuid", "提权"), ("seteuid", "提权"),
                      ("setgid", "提权"), ("setegid", "提权"),
                      ("setreuid", "提权"), ("setregid", "提权"),
                      ("putenv", "修改环境变量"),
                      ("unsetenv", "修改环境变量")):
        if hasattr(os, _nm):
            setattr(os, _nm, _deny(_nm, _why))


def _block_sensitive_file_access() -> None:
    """把 `open` / `io.FileIO` / `os.open` / `os.stat` / 目录列举换成带敏感路径判定的版本。

    与 `_block_process_creation`、socket 白名单同一思路：**封禁能力而不是模块**。

    ⚠ 只打一层 `builtins.open` 是**不够的** —— 这是对抗性测试实测出来的，不是推测。
    同一个文件在 CPython 里有好几条并列的入口，漏掉任何一条，前面的工作全部作废：

        builtins.open        ← 常规写法，也是 pathlib / json / shutil / codecs 的底层
        io.open / _io.open   ← `pathlib.Path.open()` 走这条
        io.FileIO            ← **裸的底层类，直接构造就绕过了上面所有包装**（实测可读 .env）
        io.open_code         ← 又一条独立入口（实测可读 .env）
        os.open              ← 系统调用入口，返回 fd（实测可读 app.db）
        os.stat              ← 只泄露元数据（大小 / 时间），但足以做存在性与变化探测

    所以每一条都要单独封。**改这里之后必须跑 `tools/test_sandbox_fs.py` 的第 4 组**
    （对抗性绕过用例），它把上面这一串逐个钉住。
    """
    import builtins
    import io as _io

    # 先把写入白名单根目录算出来（用到的 tempfile 必须在替换 open 之前导入，
    # 否则 tempfile 自己初始化时拿到的会是 guard 版本，可能自锁）
    _set_write_roots()
    # 记下敏感文件的 inode，供硬链接检测比对（必须在 open 被替换前做）
    _collect_sensitive_inodes()
    # 链接创建 / 切目录 / 提权 / 改环境变量 —— 全部在用户代码前关掉
    _block_link_creation()
    _block_misc_escapes()

    orig_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        why = _sensitive_reason(file)
        if why:
            raise PermissionError(
                f"沙箱安全策略禁止访问{why}：{os.path.basename(str(file))}"
            )
        # 写入路径另走白名单：黑名单拦不住"往工程目录写"这类没被枚举到的情况
        if any(ch in str(mode) for ch in ("w", "a", "x", "+")):
            wr = _write_reason(file)
            if wr:
                raise PermissionError(
                    f"沙箱安全策略禁止写入{wr}：{str(file)[:120]}"
                )
        return orig_open(file, mode, *a, **k)

    builtins.open = guarded_open  # type: ignore[assignment]
    _io.open = guarded_open  # type: ignore[assignment]

    # io.FileIO：底层类，必须用**子类**而不是函数替换，否则别处
    # `isinstance(f, io.FileIO)` 之类的判断会开始报错。
    orig_fileio = _io.FileIO

    class _GuardedFileIO(orig_fileio):  # type: ignore[misc,valid-type]
        def __init__(self, file, mode="r", *a, **k):
            why = _sensitive_reason(file)
            if why:
                raise PermissionError(
                    f"沙箱安全策略禁止访问{why}：{os.path.basename(str(file))}"
                )
            if any(ch in str(mode) for ch in ("w", "a", "x", "+")):
                wr = _write_reason(file)
                if wr:
                    raise PermissionError(
                        f"沙箱安全策略禁止写入{wr}：{str(file)[:120]}"
                    )
            super().__init__(file, mode, *a, **k)

    _io.FileIO = _GuardedFileIO  # type: ignore[assignment]
    io.FileIO = _GuardedFileIO  # type: ignore[attr-defined]

    for mod in (_io, io):
        if hasattr(mod, "open_code"):
            orig_open_code = mod.open_code

            def guarded_open_code(path, _o=orig_open_code):
                why = _sensitive_reason(path)
                if why:
                    raise PermissionError(
                        f"沙箱安全策略禁止访问{why}：{os.path.basename(str(path))}"
                    )
                return _o(path)

            mod.open_code = guarded_open_code  # type: ignore[attr-defined]

    orig_os_open = os.open
    # os.open 的写标志位：O_WRONLY/O_RDWR/O_CREAT/O_TRUNC/O_APPEND/O_EXCL
    _WRITE_FLAGS = (
        os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        | getattr(os, "O_EXCL", 0) | getattr(os, "O_TEMPORARY", 0)
    )

    def guarded_os_open(path, flags, *a, **k):
        why = _sensitive_reason(path)
        if why:
            raise PermissionError(
                f"沙箱安全策略禁止访问{why}：{os.path.basename(str(path))}"
            )
        if flags & _WRITE_FLAGS:
            wr = _write_reason(path)
            if wr:
                raise PermissionError(
                    f"沙箱安全策略禁止写入{wr}：{str(path)[:120]}"
                )
        return orig_os_open(path, flags, *a, **k)

    os.open = guarded_os_open  # type: ignore[assignment]

    # 元数据也封：os.stat 只泄露大小/时间，但足够做「文件是否存在 / 有没有变」的探测，
    # 而且没有任何正当理由让生成代码去 stat `.env`。
    for name in ("stat", "lstat"):

        def guarded_stat(path, *a, _name=name, **k):
            why = _sensitive_reason(path)
            if why:
                raise PermissionError(f"沙箱安全策略禁止访问{why}的元数据")
            return getattr(os, "_orig_" + _name)(path, *a, **k)

        setattr(os, "_orig_" + name, getattr(os, name))
        setattr(os, name, guarded_stat)

    # 目录列举：两层防护
    #   ① 目录本身敏感（.ssh/.git/…）→ 整个拒绝
    #   ② 目录本身不敏感，但**返回的条目里有敏感文件名** → 过滤掉这些条目
    # ②是 2026-09-20 对抗性测试补的：只做①时，`os.listdir(backend/)` 会把
    # `.env`、`app.db`、`.db_backup` 这些名字直接交出去。名字本身不等于内容，
    # 但它精确告诉攻击者"该读哪个文件"，是踩点的第一步，没有理由放给生成代码。
    orig_listdir = os.listdir

    def guarded_listdir(path="."):
        why = _sensitive_reason(path)
        if why:
            raise PermissionError(f"沙箱安全策略禁止列举{why}")
        names = orig_listdir(path)
        try:
            base = str(path)
        except Exception:  # noqa: BLE001
            return names
        kept = []
        for n in names:
            child = os.path.join(base, n)
            if _sensitive_reason(child) or _write_reason(child):
                continue
            kept.append(n)
        return kept

    os.listdir = guarded_listdir  # type: ignore[assignment]

    # os.scandir 返回 DirEntry 迭代器，同样要在迭代时过滤
    orig_scandir = os.scandir

    def guarded_scandir(path="."):
        why = _sensitive_reason(path)
        if why:
            raise PermissionError(f"沙箱安全策略禁止列举{why}")
        base = str(path)

        def gen():
            for e in orig_scandir(path):
                if _sensitive_reason(os.path.join(base, e.name)) or \
                        _write_reason(os.path.join(base, e.name)):
                    continue
                yield e

        return gen()

    os.scandir = guarded_scandir  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # 破坏性目录/文件操作：同样按**写侧白名单**判定
    # ------------------------------------------------------------------
    # 2026-09-20 补充。上面封了「写入内容」，但**删除 / 改名 / 建目录**
    # 是与「写」并列的另一组破坏性能力，当时整组漏掉了 ——
    # 也就是说虽然写不进新文件，却能把服务端的文件删掉、改名、或到处建目录。
    # 危害低于写文件（不能植入内容），但仍可破坏服务端目录结构、
    # 让服务崩溃或行为异常。判据与写入一致：只允许在沙箱临时目录里折腾。
    def _guard_destructive(name: str, fn):
        """把「单路径参数」的破坏性调用包一层白名单判定。

        `os.remove`/`unlink`/`rmdir`/`removedirs`/`mkdir`/`makedirs`/`rename`/`replace`
        的第一个参数，以及 `os.rename`/`replace` 的第二个参数，都是路径。
        """
        def wrapper(path, *a, **k):
            for p in (path,) + (a[:1] if name in ("rename", "replace") else ()):
                why = _sensitive_reason(p)
                if why:
                    raise PermissionError(
                        f"沙箱安全策略禁止访问{why}：{os.path.basename(str(p))}"
                    )
                wr = _write_reason(p)
                if wr:
                    raise PermissionError(
                        f"沙箱安全策略禁止对非沙箱临时目录执行 {name}：{str(p)[:110]}"
                    )
            return fn(path, *a, **k)

        return wrapper

    for _nm in ("remove", "unlink", "rmdir", "removedirs", "mkdir", "makedirs",
                "rename", "replace", "chmod", "chown", "truncate", "utime"):
        if hasattr(os, _nm):
            setattr(os, _nm, _guard_destructive(_nm, getattr(os, _nm)))

    # shutil 是独立入口（内部各函数会调用上面已封的 os 函数，但 shutil.rmtree
    # 有自己的一套实现，且 copyfile/move 会直接开文件），单独封。
    try:
        import shutil as _shutil

        def _guard_shutil(name: str):
            orig = getattr(_shutil, name)

            def wrapper(src, *a, **k):
                for p in (src,) + (a[:1] if name in ("copyfile", "copy", "copy2",
                                                    "copytree", "move") else ()):
                    if not isinstance(p, (str, bytes, os.PathLike)):
                        continue
                    wr = _write_reason(p)
                    if wr:
                        raise PermissionError(
                            f"沙箱安全策略禁止对非沙箱临时目录执行 shutil.{name}：{str(p)[:110]}"
                        )
                return orig(src, *a, **k)

            return wrapper

        for _nm in ("rmtree", "copyfile", "copy", "copy2", "copytree", "move"):
            if hasattr(_shutil, _nm):
                setattr(_shutil, _nm, _guard_shutil(_nm))
    except ImportError:  # pragma: no cover - shutil 必然存在，防御性
        pass


_NET_ALLOW = (".googleapis.com", ".google.com", ".gstatic.com", ".googleusercontent.com")
_NET_ALLOW_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _host_allowed(host: str) -> bool:
    h = str(host)
    if h in _NET_ALLOW_HOSTS:
        return True
    return any(h == d.lstrip(".") or h.endswith(d) for d in _NET_ALLOW)


def _harden() -> None:
    sys.meta_path.insert(0, _Blocker())
    sys.setrecursionlimit(3000)
    _block_process_creation()
    # ⚠ 文件系统那层**不在这里**加。原因：`python-dotenv`（`app.config` 依赖）在导入时
    #   就要读 `.env`，`gee_auth.initialize` 要读凭据文件 —— 这两个都发生在
    #   `_verify_toolchain()` / `_install_ee()` 里，属于**我们自己的可信代码**。
    #   在这之前把 .env 封了，会直接把整条链路打挂（实测报
    #   `PermissionError: 禁止访问环境变量文件：.env`，栈顶在 `_verify_toolchain`）。
    #   加固只需要赶在**用户代码 exec 之前**，见 main() 里的调用点。

    import socket

    orig_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *a, **k):
        if not _host_allowed(host):
            raise PermissionError(f"沙箱网络白名单拒绝访问：{host}")
        return orig_getaddrinfo(host, *a, **k)

    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]

    # 兜住直接用 IP / socket 发起连接、绕过 getaddrinfo 的路径
    orig_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        if not _host_allowed(host):
            raise PermissionError(f"沙箱网络白名单拒绝访问：{host}")
        return orig_connect(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[assignment]


class Reporter:
    """结果上报契约：大模型代码通过 WB 上报图层与图表，沙箱据此生成 ExecutionOutcome。"""

    def __init__(self, ee=None):
        self._ee = ee
        self.layers: list[dict] = []
        self.charts: list[dict] = []
        self.stats: dict = {}
        self.notes: list[str] = []

    # ---- 图层 ----
    def add_layer(self, name: str, geojson: dict | None = None, tile_url: str | None = None, legend: list | None = None):
        item = {"name": name, "kind": "geojson" if geojson else "tile", "legend": legend or []}
        if geojson:
            item["geojson"] = geojson
        if tile_url:
            item["tile_url"] = tile_url
        self.layers.append(item)
        return item

    def add_image_layer(self, name: str, image, vis_params: dict | None = None, legend: list | None = None):
        """把 ee.Image 转成前端可直接加载的瓦片图层（走 GEE getMapId）。"""
        tile_url = None
        try:
            map_id = image.getMapId(vis_params or {})
            tf = map_id.get("tile_fetcher")
            tile_url = getattr(tf, "url_format", None)
            if not tile_url and isinstance(map_id, dict) and map_id.get("mapid"):
                tile_url = (
                    "https://earthengine.googleapis.com/v1/"
                    f"{map_id.get('mapid')}/tiles/{{z}}/{{x}}/{{y}}"
                )
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"图层[{name}] 生成瓦片失败：{e}")
        return self.add_layer(name, tile_url=tile_url, legend=legend)

    def add_feature_layer(self, name: str, feature_or_collection, legend: list | None = None):
        """把 ee.Feature / ee.FeatureCollection 转成 GeoJSON 图层。"""
        geojson = None
        try:
            geojson = feature_or_collection.getInfo()
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"图层[{name}] 转 GeoJSON 失败：{e}")
        return self.add_layer(name, geojson=geojson, legend=legend)

    # ---- 图表 ----
    def add_chart(self, title: str, labels: list, series: list, kind: str = "line"):
        self.charts.append(
            {
                "title": title,
                "kind": kind,
                "labels": [str(x) for x in labels],
                "series": series if series and isinstance(series[0], dict) else [{"name": "值", "data": series}],
            }
        )

    # ---- 统计 ----
    def stat(self, key: str, value):
        self.stats[str(key)] = value

    def note(self, text: str):
        self.notes.append(str(text))

    def print(self, *args):
        print(*args)


def _mock_ee():
    """离线自检用的假 ee 模块（不联网），用于验证沙箱与结果契约链路。"""
    import types

    class Mock:
        def __init__(self, info=None):
            self._info = info

        def __call__(self, *a, **k):
            return Mock(self._info)

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name == "getInfo":
                return lambda *a, **k: self._info
            if name == "getMapId":
                return lambda *a, **k: {
                    "mapid": "mock",
                    "token": "mock",
                    "tile_fetcher": types.SimpleNamespace(
                        url_format="https://earthengine.googleapis.com/v1/mock/tiles/{z}/{x}/{y}"
                    ),
                }
            if name == "size":
                return lambda *a, **k: Mock(12)
            return lambda *a, **k: Mock(self._info)

        def __getitem__(self, k):
            return Mock(self._info)

    ee = types.ModuleType("ee")
    ee.Initialize = lambda *a, **k: None
    ee.Authenticate = lambda *a, **k: None
    ee.Geometry = Mock({"type": "Polygon", "coordinates": [[[120, 31], [120.3, 31], [120.3, 31.3], [120, 31.3]]]})
    ee.ImageCollection = lambda *a, **k: Mock(24)
    ee.Image = lambda *a, **k: Mock(0.42)
    ee.Feature = lambda *a, **k: Mock({"type": "Feature", "geometry": None, "properties": {}})
    ee.FeatureCollection = lambda *a, **k: Mock({"type": "FeatureCollection", "features": []})
    ee.Filter = Mock()
    ee.Reducer = Mock()
    ee.Number = lambda v: Mock(v)
    ee.Dictionary = lambda v: Mock(v)
    ee.Date = Mock()
    ee.List = lambda v: Mock(v)
    ee.Algorithms = Mock()
    ee.data = Mock()
    return ee


def _install_ee() -> object | None:
    """准备 ee 模块：mock 模式注入假模块；真实模式用服务端凭据预热并冻结 Initialize。"""
    if os.getenv("WB_GEE_MOCK") == "1":
        ee = _mock_ee()
        sys.modules["ee"] = ee
        return ee

    # 不要在这里写 `except Exception: from gee_auth import initialize` 之类的降级 import。
    # 那样会把真实失败原因（例如黑名单拦截、依赖缺失）掩盖成
    # `ModuleNotFoundError: No module named 'gee_auth'`，白白浪费排查时间。
    from app.gee_auth import initialize

    ee, info = initialize()
    if ee is None:
        raise RuntimeError(info.message)
    # 冻结：屏蔽代码内的重复初始化，避免自带 project 参数导致再次鉴权
    ee.Initialize = lambda *a, **k: None
    ee.Authenticate = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("沙箱内禁止交互式 Authenticate，请在服务端配置凭据")
    )
    return ee


def _auto_collect(ns: dict) -> tuple[list, list]:
    """兜底：大模型没调用 WB 时，从命名空间里捞可用的图层/图表变量。"""
    layers, charts = [], []
    img = ns.get("image")
    if img is not None and hasattr(img, "getMapId"):
        try:
            m = img.getMapId(ns.get("vis_params") or ns.get("vis") or {})
            tf = m.get("tile_fetcher")
            url = getattr(tf, "url_format", None)
            if url:
                layers.append({"name": ns.get("layer_name", "分析结果"), "kind": "tile", "tile_url": url, "legend": []})
        except Exception:  # noqa: BLE001
            pass
    gj = ns.get("geojson")
    if isinstance(gj, dict):
        layers.append({"name": ns.get("layer_name", "分析结果"), "kind": "geojson", "geojson": gj, "legend": []})
    for key in ("layers", "result_layers"):
        v = ns.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict) and ("geojson" in v[0] or "tile_url" in v[0]):
            layers.extend(v)
    for key in ("charts", "result_charts", "chart"):
        v = ns.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict) and "labels" in v[0]:
            charts.extend(v)
        elif isinstance(v, dict) and "labels" in v:
            charts.append(v)
    return layers, charts


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    code = payload.get("code", "")
    request = payload.get("request", {}) or {}

    report: dict = {"ok": False, "stdout": "", "layers": [], "charts": [], "error": None, "stats": {}}
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf  # 用户代码的 print 收进缓冲区，最后统一回传
    stats: dict = {}
    try:
        _harden()
        _verify_toolchain()  # 黑名单没误伤运行依赖（mock 模式同样执行，防止漏测）
        ee = _install_ee()
        wb = Reporter(ee)
        ns: dict = {
            "__name__": "__wb_sandbox__",
            "ee": ee,
            "WB": wb,
            "wb": wb,
            "region": request.get("region", ""),
            "start_date": request.get("start_date", ""),
            "end_date": request.get("end_date", ""),
            "cloud_threshold": request.get("cloud_threshold", 20),
        }
        # 敏感文件封禁放在**用户代码之前、GEE 初始化之后**：
        # 前面几步（导入 app.config → dotenv 读 .env；gee_auth 读凭据）都是可信代码，
        # 从这一行往下才轮到模型生成的代码。
        _block_sensitive_file_access()
        exec(compile(code, "<gee_code>", "exec"), ns)  # noqa: S102

        stats = getattr(wb, "stats", {})
        auto_layers, auto_charts = _auto_collect(ns)
        layers = wb.layers or auto_layers
        charts = wb.charts or auto_charts
        out = buf.getvalue()
        if stats:
            out += "\n[统计] " + json.dumps(stats, ensure_ascii=False)
        report |= {
            "ok": True,
            "stdout": out[:_MAX_OUTPUT_CHARS],
            "layers": layers,
            "charts": charts,
            # stats 同时保留在 stdout 文本里（向后兼容）**并且**结构化回传 ——
            # 结论摘要层需要结构化数据，去解析 stdout 文本太脆弱。
            "stats": stats or {},
        }
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=6)
        name = type(e).__name__
        text = f"{name}: {e}".lower()
        if isinstance(e, PermissionError):
            category = "sandbox"
        elif any(s in text for s in ("ssl", "unexpected_eof", "eof occurred", "connection reset",
                                     "connection aborted", "connection refused", "max retries",
                                     "timed out", "timeout", "remotedisconnected", "broken pipe",
                                     "temporarily unavailable")):
            category = "network"
        elif "auth" in text or "credential" in text or "permission" in text or "invalid_grant" in text:
            category = "auth"
        else:
            category = "gee"
        report |= {
            "ok": False,
            "stdout": (buf.getvalue() + "\n" + tb)[:_MAX_OUTPUT_CHARS],
            "error": {"category": category, "message": f"{name}: {e}"},
        }
    finally:
        sys.stdout = real_stdout

    blob = base64.b64encode(json.dumps(report, ensure_ascii=False, default=str).encode("utf-8")).decode("ascii")
    real_stdout.write(_SENTINEL + blob + _SENTINEL + "\n")


if __name__ == "__main__":
    main()
