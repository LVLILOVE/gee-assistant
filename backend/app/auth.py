"""注册登录与会话管理。

为什么需要它：服务已通过 cpolar 暴露到公网，如果不加访问控制，任何拿到 URL 的人
都能消耗 DeepSeek 与 Earth Engine 的额度。任务书也要求凭据与访问由服务端把关。

实现取向（有意为之的取舍）：
  * 密码用 **PBKDF2-HMAC-SHA256**（标准库），不引入 bcrypt/passlib——
    本机装包链路很不稳定，为一个哈希函数去赌一次 pip 不值当。
  * 会话用**随机 token + 服务端表**，不放 JWT、不放 localStorage。
    理由：token 存服务端才能"立即失效"，注销才是真的注销。
  * cookie 一律 **HttpOnly + SameSite=Lax**，脚本读不到，降低 XSS 窃取面。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from .task_store import DB_DIR, DB_PATH

PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16
_TOKEN_BYTES = 32

USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.-]{3,32}$")
MIN_PASSWORD_LEN = 8

# 登录失败节流。进程内字典足够挡住脚本暴力破解；
# 若将来跑多副本，这里需要换成 Redis 之类的共享存储。
_MAX_FAILS = 5
_LOCK_SECONDS = 60


class AuthError(Exception):
    """带 HTTP 语义的鉴权错误（由 main 层翻译成响应）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def _validate(username: str, password: str) -> tuple[str, str]:
    username = (username or "").strip()
    password = password or ""
    if not USERNAME_RE.match(username):
        raise AuthError("用户名需 3–32 位，可用中英文、数字、下划线、点或短横线")
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > 128:
        raise AuthError("密码过长")
    return username, password


class AuthStore:
    def __init__(self, db_path=DB_PATH):
        self._db = str(db_path)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fails: dict[str, list] = {}
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    is_admin INTEGER DEFAULT 0,
                    created_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at REAL,
                    expires_at REAL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")

    # ---- 用户 ----

    def count_users(self) -> int:
        with self._lock, self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def first_admin_id(self) -> int | None:
        """最早注册的管理员 ID。用于把历史无主数据（任务/区域）划归给他。"""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

    def create_user(self, username: str, password: str) -> dict:
        username, password = _validate(username, password)
        salt = secrets.token_bytes(_SALT_BYTES)
        pwd_hash = _hash_password(password, salt)
        first = self.count_users() == 0
        with self._lock, self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if exists:
                raise AuthError("该用户名已被占用", status=409)
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, salt, is_admin, created_at) VALUES (?,?,?,?,?)",
                (username, pwd_hash, salt.hex(), 1 if first else 0, time.time()),
            )
            uid = cur.lastrowid
        return {"id": uid, "username": username, "is_admin": first}

    def _user_row(self, username: str):
        with self._lock, self._conn() as conn:
            return conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()

    def verify_login(self, username: str, password: str) -> dict:
        """校验用户名密码。失败一律抛同一条消息，不区分"用户不存在/密码错"。"""
        key = (username or "").strip().lower()
        self._check_lock(key)

        row = self._user_row(username or "")
        ok = False
        if row is not None:
            expected = row["password_hash"]
            got = _hash_password(password or "", bytes.fromhex(row["salt"]))
            # compare_digest 防时序侧信道；两边都是十六进制串，等长
            ok = hmac.compare_digest(expected, got)

        if not ok:
            self._note_failure(key)
            raise AuthError("用户名或密码不正确", status=401)

        self._clear_failures(key)
        return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}

    # ---- 登录失败节流 ----

    def _check_lock(self, key: str) -> None:
        rec = self._fails.get(key)
        if not rec:
            return
        count, until = rec
        if count >= _MAX_FAILS and time.time() < until:
            raise AuthError(f"失败次数过多，请 {int(until - time.time()) + 1} 秒后再试", status=429)

    def _note_failure(self, key: str) -> None:
        count, _ = self._fails.get(key, (0, 0.0))
        self._fails[key] = (count + 1, time.time() + _LOCK_SECONDS)

    def _clear_failures(self, key: str) -> None:
        self._fails.pop(key, None)

    # ---- 会话 ----

    def create_session(self, user_id: int, ttl_seconds: int) -> str:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (token, user_id, now, now + ttl_seconds),
            )
        return token

    def resolve_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = time.time()
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT s.expires_at, u.id, u.username, u.is_admin FROM sessions s"
                " JOIN users u ON u.id = s.user_id WHERE s.token=?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] < now:
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                return None
        return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))

    def purge_expired(self) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            return cur.rowcount

    # ---- 密码修改 ----

    def change_password(self, user_id: int, old_password: str, new_password: str) -> None:
        """改密码。必须验旧密码；改完踢掉该用户所有会话（旧会话可能已泄露）。"""
        if len(new_password or "") < MIN_PASSWORD_LEN:
            raise AuthError(f"新密码至少 {MIN_PASSWORD_LEN} 位")
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise AuthError("用户不存在", status=404)
            got = _hash_password(old_password or "", bytes.fromhex(row["salt"]))
            if not hmac.compare_digest(row["password_hash"], got):
                raise AuthError("原密码不正确", status=401)
            salt = secrets.token_bytes(_SALT_BYTES)
            new_hash = _hash_password(new_password, salt)
            conn.execute(
                "UPDATE users SET password_hash=?, salt=? WHERE id=?",
                (new_hash, salt.hex(), user_id),
            )
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    # ---- 管理员种子账号 ----

    def ensure_admin(self, username: str, password: str) -> str:
        """按环境变量补一个管理员账号（幂等）。

        为什么需要它：服务在公网，如果只靠"谁先注册谁是管理员"，第一个访问者
        就成了管理员。由部署方通过 .env 预置账号可以彻底消掉这个竞态。
        """
        username = (username or "").strip()
        if not username or not password:
            return "skipped"
        row = self._user_row(username)
        if row is not None:
            if not row["is_admin"]:
                with self._lock, self._conn() as conn:
                    conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
                return "promoted"
            return "exists"
        try:
            self.create_user(username, password)
        except AuthError as e:
            return f"failed: {e.message}"
        return "created"


store = AuthStore()
