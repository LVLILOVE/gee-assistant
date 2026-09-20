import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    def __init__(self):
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self.execution_backend = os.getenv("EXECUTION_BACKEND", "offline")
        self.max_retries = int(os.getenv("MAX_RETRIES", "3"))
        self.backend_port = int(os.getenv("BACKEND_PORT", "8010"))

        # ---- GEE 凭据配置（三种来源，优先级从高到低）----
        # 1) 服务账号 JSON 文件路径（推荐，适合服务器/对外部署）
        self.gee_service_account_json = os.getenv("GEE_SERVICE_ACCOUNT_JSON", "").strip()
        # 2) 加密凭据库：由 tools/gee_doctor.py --encrypt 生成，凭据加密落盘
        self.gee_credentials_enc = os.getenv("GEE_CREDENTIALS_ENC", "data/credentials/gee.enc").strip()
        # 3) 个人 OAuth（earthengine authenticate 生成），默认走官方路径
        self.gee_oauth_credentials = os.getenv("GEE_OAUTH_CREDENTIALS", "").strip()
        # GEE 云项目 ID（注册 Earth Engine 时创建的那个项目）
        self.gee_project = os.getenv("GEE_PROJECT", "").strip()
        # 出网代理（GEE 全站被墙时必须；留空表示直连）
        self.gee_proxy = os.getenv("GEE_PROXY", "").strip()
        # 单次执行的硬超时（秒）
        # 真实 GEE 任务的云端计算（Sentinel-2 全年 median + reduceRegion）通常需要数分钟，
        # 120s 会稳定超时；默认放宽到 420s。
        self.gee_timeout = int(os.getenv("GEE_TIMEOUT", "420"))
        # 网络类错误（代理抖动/SSL 中断）的自动重试次数
        self.gee_net_retries = int(os.getenv("GEE_NET_RETRIES", "3"))

        # ---- 访问控制 ----
        # 服务通过 cpolar 暴露在公网，必须挡住未登录访问，否则任何人拿到 URL
        # 都能消耗 DeepSeek 与 Earth Engine 的额度。本地调试可设 AUTH_ENABLED=false。
        self.auth_enabled = os.getenv("AUTH_ENABLED", "true").strip().lower() not in ("0", "false", "no")
        # 是否允许自助注册。单人使用时可关掉，避免公网上被随意注册。
        self.allow_registration = os.getenv("ALLOW_REGISTRATION", "true").strip().lower() not in ("0", "false", "no")
        # 会话有效期（小时），默认 7 天
        self.session_ttl_hours = int(os.getenv("SESSION_TTL_HOURS", "168"))
        # 预置管理员账号（幂等）。服务在公网，靠"谁先注册谁是管理员"会留下竞态：
        # 第一个访问者就成了管理员。用 .env 预置可彻底消除这个窗口。
        self.admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        self.admin_password = os.getenv("ADMIN_PASSWORD", "").strip()

        # ---- 任务配额（防公网滥用与烧额度）----
        # 每条任务都要烧 DeepSeek token + 消耗 GEE 云端算力，而服务暴露在公网、
        # 自助注册默认开着。没有这层限制时，任何注册用户都能无限提交。
        # 全局并发上限：保护本机。MX450 只有 2GB 显存，同时跑多个 GEE 沙箱没意义。
        self.max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))
        # 单用户并发上限（防一个人排队占满全局额度）
        self.max_concurrent_tasks_per_user = int(os.getenv("MAX_CONCURRENT_TASKS_PER_USER", "2"))
        # 单用户滑动窗口提交上限。设 0 表示不限制
        self.task_rate_limit_per_hour = int(os.getenv("TASK_RATE_LIMIT_PER_HOUR", "20"))
        self.task_rate_limit_per_day = int(os.getenv("TASK_RATE_LIMIT_PER_DAY", "60"))

        # ---- CORS 允许来源 ----
        # 生产环境前端由本服务托管（同源），根本不需要 CORS。
        # 这里列出的是「开发时跑在别的端口的前端」，默认只有 Vite 的 5173。
        #
        # 之前这里是 allow_origins=["*"] + allow_credentials=True：实测任意 Origin
        # 都会被原样回显、并带上 Allow-Credentials，等于把「任意网站可带 cookie 读接口」
        # 的门开着。目前靠 Cookie 的 SameSite=Lax 兜住了（跨站 fetch 不带 cookie），
        # 但只要哪天改成 SameSite=None 就会立刻变成可读别人任务列表的漏洞。
        # 更关键的是：宽 CORS 对本项目毫无用处——生产是同源。
        #
        # 需要额外来源（局域网调试、独立前端）就写 CORS_ORIGINS，逗号分隔。
        default_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
        extra = os.getenv("CORS_ORIGINS", "").strip()
        self.cors_origins = default_origins + [
            o.strip().rstrip("/") for o in extra.split(",") if o.strip()
        ]

    @property
    def gee_enc_path(self) -> Path:
        p = Path(self.gee_credentials_enc)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def gee_sa_path(self) -> Path | None:
        if not self.gee_service_account_json:
            return None
        p = Path(self.gee_service_account_json)
        return p if p.is_absolute() else BASE_DIR / p


settings = Settings()
