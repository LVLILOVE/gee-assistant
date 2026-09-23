"""辅助：读取 backend/.env 里的配置项。

为什么单独成文件：无障碍核验脚本原先各自从 os.environ 读 ADMIN_PASSWORD，
而脚本是独立进程、shell 里并没有 export 它 —— 于是拿到空密码、登录失败、
停在登录页，然后"检查了 1~5 个元素"就报通过。这是典型的**假绿**。

抽出来还有个作用：其它脚本可以 `from _env import _env` 复用，
而不会因为 import 触发了被 import 脚本的主体逻辑（那个脚本没有 __main__ 守卫，
直接 import 会让整个检查跑一遍 —— 踩过这个坑）。
"""
import os

_SENTINEL = object()


def _env(key, default=""):
    """凭据来源优先级：shell 环境变量 > backend/.env。

    搜索 .env 的候选路径覆盖两种调用方式：
      · 从 backend/ 目录跑（../backend/.env 与 backend/.env 都试）
      · 从任意目录跑（用 __file__ 定位到工程根）
    """
    if os.environ.get(key):
        return os.environ[key]
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in ("backend/.env", "../backend/.env",
                 os.path.join(here, "..", "backend", ".env"),
                 os.path.join(here, "..", "..", "backend", ".env")):
        try:
            with open(cand, encoding="utf-8") as f:
                for line in f:
                    if line.startswith(key + "="):
                        v = line.split("=", 1)[1].strip()
                        return v.strip('"').strip("'")
        except OSError:
            continue
    return default
