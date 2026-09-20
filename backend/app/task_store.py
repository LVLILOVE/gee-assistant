"""任务存储：sqlite3 持久化（任务代码 / 执行结果元数据 / 自定义区域 / 用户设置）。

多用户隔离说明
--------------
`tasks` / `regions` / `user_settings` 三张表都带 `user_id`，语义统一：

| 值 | 含义 |
|---|---|
| `0` | **无主**：历史数据，或鉴权关闭（单人自用）时创建的数据 |
| `> 0` | 属于 `users.id` 对应用户 |
| 查询参数传 `None` | 不过滤（仅测试/管理用；生产代码总传具体 id） |

**为什么统一用 `0` 而不是 `NULL` 表示无主**：`regions` 的主键是 `(name, user_id)`
联合主键 —— 而 SQLite 里主键列**允许 NULL**，`(名称, NULL)` 不会触发唯一冲突，
于是同一用户反复保存同名区域会插出重复行，`INSERT OR REPLACE` 也替换不掉。
`user_settings` 同理（`(key, user_id)` 主键），`0` 表示全局默认值。

`backfill_owner()` 把无主数据（`user_id = 0`）一次性划给管理员。
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import crypto
from .models import AnalysisRequest


def _resolve_db_path() -> Path:
    """数据库位置。默认 backend/data/app.db，可用 `APP_DB_PATH` 覆盖。

    留这个开关是为了让自动化测试能在**临时库**上跑真实迁移与隔离逻辑，
    而不必去动线上那份 app.db。
    """
    override = os.getenv("APP_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "data" / "app.db"


DB_PATH = _resolve_db_path()
DB_DIR = DB_PATH.parent

#: 表示"不属于任何用户"（历史数据 / 鉴权关闭时的数据）与"全局默认值"的伪用户 ID
GLOBAL_USER_ID = 0


def _norm_uid(user_id: int | None) -> int:
    """写入前归一：`None` 一律落 `0`，避免库里同时存在 NULL 与 0 两种"无主"。"""
    return GLOBAL_USER_ID if user_id is None else int(user_id)


def _settings_ddl(table: str) -> str:
    """user_settings 的目标结构。建新表与迁移旧表共用同一段 DDL（迁移时换个表名）。"""
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
        key TEXT NOT NULL,
        user_id INTEGER NOT NULL DEFAULT 0,
        value TEXT,
        updated_at REAL,
        PRIMARY KEY (key, user_id)
    )
    """


def _regions_ddl(table: str) -> str:
    """regions 的目标结构：**联合主键 `(name, user_id)`**。

    对照历史结构 `name` 单列主键：那种设计下 `INSERT OR REPLACE` 会让
    「B 保存一个与 A 同名的区域」直接把 A 那一行替换掉 —— 坐标变 B 的、
    `user_id` 也变成 B，A 的区域凭空消失且再也删不掉。这是真实的跨用户数据破坏，
    所以必须改联合主键让同名区域能各自共存。

    `name` 自 2026-09-20 起是**密文**（Fernet 带随机 IV，同明文两次加密结果不同），
    因此另存一列 `name_bidx`（盲索引）用于等值查询 ——
    `delete_region()` / `INSERT OR REPLACE` 都要靠它定位行，
    直接拿密文比对永远匹配不上。
    """
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
        name TEXT NOT NULL,
        user_id INTEGER NOT NULL DEFAULT 0,
        lon REAL,
        lat REAL,
        desc TEXT,
        created_at REAL,
        name_bidx TEXT,
        PRIMARY KEY (name, user_id)
    )
    """


class TaskStore:
    def __init__(self, db_path=DB_PATH):
        self._db = str(db_path)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 敏感字段加解密（任务书 PRD §7 数据隐私）----
    # 加解密统一下沉到这一层，而不是散落到 API / orchestrator：
    # 存储层是**唯一**的读写咽喉（全项目只有这里的 sqlite3.connect 碰 tasks 表），
    # 把边界放在这里，新增调用方就不可能忘记加密。
    @staticmethod
    def _enc_task_row(values: dict) -> dict:
        """写入前加密。未知字段原样通过，键集合不变（调用方按位置传参，不能改顺序）。"""
        return {
            k: (crypto.encrypt_field(v) if k in crypto.ENCRYPTED_TASK_FIELDS else v)
            for k, v in values.items()
        }

    @staticmethod
    def _dec_row(d: dict) -> dict:
        """读出后解密。加解密都是幂等的，所以半新半旧的库也能正确读。

        按**字段名**判断而不是按表名：`notes` 查询会混着取 `task_id`/`feedback`（明文）
        和 `feedback_note`（密文），传进来的行形状不固定。
        所以这里对"任何一个可能被加密的字段名"都尝试解密，
        交给 `decrypt_field()` 的前缀判断决定要不要真的解 —— 比区分表更不容易漏。
        """
        fields = set(crypto.ENCRYPTED_TASK_FIELDS) | set(crypto.ENCRYPTED_REGION_FIELDS)
        for f in fields:
            if f in d and d[f] is not None:
                d[f] = crypto.decrypt_field(d[f])
        return d

    # ---- 建表 / 迁移 ----
    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT,
                    task_type TEXT,
                    region TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    cloud_threshold REAL,
                    code TEXT,
                    result TEXT,
                    logs TEXT,
                    attempts INTEGER,
                    created_at REAL,
                    user_id INTEGER,
                    started_at REAL,
                    finished_at REAL,
                    feedback TEXT,
                    feedback_note TEXT,
                    feedback_at REAL
                )
                """
            )
            conn.execute(_regions_ddl("regions"))
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把旧库补齐到当前结构。幂等，可重复执行。

        处理顺序很重要：先把 regions 的主键改对、再把 NULL 归一成 0，
        否则后一步的比较条件会漏掉还没归一的 NULL 行。
        """
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        # 1) tasks 补 user_id 列（2026-09 多用户隔离）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created_at DESC)")

        # 2) tasks 补耗时字段（2026-09-17）。
        #    背景：任务书要求「典型任务端到端响应 ≤60s」这一量化指标，但原先只存了
        #    created_at，架构上就测不出耗时——指标不是"没测"，是"测不了"。
        #    分两个字段是为了区分**排队等待**与**真实执行**：
        #    created_at→started_at 是在队列里等（受并发配额影响），
        #    started_at→finished_at 才是任务本身的耗时。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        for col in ("started_at", "finished_at"):
            if col not in cols:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} REAL")

        # 2.5) tasks 补用户反馈字段（2026-09-20）。
        #      背景：项目到了验收阶段仍然 **0 真实用户**，而"产品价值"这一条线
        #      只有用户能证明。访谈成本高且要等人，所以给结果区加一行极简反馈
        #      （👍/👎 + 可选一句话）直接落进任务表 —— 把"用户验证"从
        #      "一件要安排的事"变成"默认会发生的事"。
        #      `feedback` 只存 'up' / 'down'，用 CHECK 约束不写（SQLite 的 ALTER
        #      加不了 CHECK），改为在 set_feedback() 里校验取值。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        for col, typ in (("feedback", "TEXT"), ("feedback_note", "TEXT"), ("feedback_at", "REAL")):
            if col not in cols:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")

        # 2) regions：补列 + 把主键从 `name` 单列改成 `(name, user_id)` 联合主键。
        #    旧结构下 `INSERT OR REPLACE` 会让 B 保存同名区域时直接顶掉 A 的那一行，
        #    属于跨用户数据破坏，必须重建表才能修（主键无法 ALTER）。
        if "regions" in tables:
            info = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(regions)")}
            if "user_id" not in info:
                conn.execute("ALTER TABLE regions ADD COLUMN user_id INTEGER")
                info["user_id"] = 0
            if info.get("user_id", 0) == 0:  # pk 序号 0 = 不在主键里 → 需要重建
                conn.execute(_regions_ddl("regions_new"))
                conn.execute(
                    "INSERT OR IGNORE INTO regions_new (name, user_id, lon, lat, desc, created_at)"
                    " SELECT name, COALESCE(user_id, 0), lon, lat, desc, created_at FROM regions"
                )
                conn.execute("DROP TABLE regions")
                conn.execute("ALTER TABLE regions_new RENAME TO regions")

        # 3) user_settings 重建为 (key, user_id) 联合主键
        #    三种情况：全新库（表都没有）/ 旧库（有表无 user_id）/ 已迁移过
        if "user_settings" not in tables:
            conn.execute(_settings_ddl("user_settings"))
        else:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(user_settings)")}
            if "user_id" not in cols:
                conn.execute(_settings_ddl("user_settings_new"))
                # 旧行（无归属）视为全局默认值
                conn.execute(
                    "INSERT OR IGNORE INTO user_settings_new (key, user_id, value, updated_at)"
                    " SELECT key, 0, value, updated_at FROM user_settings"
                )
                conn.execute("DROP TABLE user_settings")
                conn.execute("ALTER TABLE user_settings_new RENAME TO user_settings")

        # 3.5) regions 补 name_bidx 盲索引列（2026-09-20 字段加密配套）。
        #      `name` 变密文后无法用等值查询定位行，删除/替换会静默失效，
        #      所以必须补一列确定性的盲索引。回填在 _encrypt_backfill() 里做。
        if "regions" in tables:
            rcols = {r[1] for r in conn.execute("PRAGMA table_info(regions)")}
            if "name_bidx" not in rcols:
                conn.execute("ALTER TABLE regions ADD COLUMN name_bidx TEXT")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_regions_bidx ON regions(user_id, name_bidx)"
                )

        # 4) 把残留的 NULL 归属归一成 0（上一版迁移曾用 NULL 表示无主）
        conn.execute("UPDATE tasks SET user_id=0 WHERE user_id IS NULL")
        conn.execute("UPDATE regions SET user_id=0 WHERE user_id IS NULL")

        # 5) 敏感字段加密回填（2026-09-20，任务书 PRD §7「数据隐私」）。
        #    PRD 原文要求「用户区域/任务描述敏感，加密存储」，而此前这些字段是明文落盘。
        #    这一步把存量明文行原地加密。
        #
        #    **幂等性靠 `crypto.ENCRYPTED_TASK_FIELDS` 的 `ENC1:` 前缀保证**：
        #    encrypt_field() 遇到已带前缀的值原样返回，所以重复执行不会二次加密
        #    （二次加密 = 数据永久损坏，且不可逆 —— 这是本步最大的风险，必须靠
        #     前缀判断挡住，绝不能靠"记得只跑一次"）。
        self._encrypt_backfill(conn)

    def _encrypt_backfill(self, conn: sqlite3.Connection) -> None:
        """把存量明文敏感字段就地加密。幂等，可重复执行。"""
        # ---- tasks ----
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        todo = [f for f in crypto.ENCRYPTED_TASK_FIELDS if f in task_cols]
        if todo:
            rows = conn.execute(
                "SELECT task_id, %s FROM tasks" % ", ".join(todo)
            ).fetchall()
            updated = 0
            for r in rows:
                changed = {}
                for f in todo:
                    v = r[f]
                    if v is None or v == "":
                        continue
                    if crypto.is_encrypted(v):
                        continue  # 已加密，跳过（幂等的关键）
                    changed[f] = crypto.encrypt_field(v)
                if changed:
                    conn.execute(
                        "UPDATE tasks SET %s WHERE task_id=?"
                        % ", ".join("%s=?" % k for k in changed),
                        list(changed.values()) + [r["task_id"]],
                    )
                    updated += 1
            if updated:
                print("[migrate] 已加密回填 %d 条历史任务" % updated)

        # ---- regions ----
        # 注意：regions 的 name 是**联合主键的一部分**。加密后同一明文的密文各不相同
        # （Fernet 带随机 IV），所以除了加密还要同时写 `name_bidx` 盲索引，
        # 否则 `WHERE name=?` 永远匹配不上，删除/替换功能静默失效。
        reg_cols = {r[1] for r in conn.execute("PRAGMA table_info(regions)")}
        rtodo = [f for f in crypto.ENCRYPTED_REGION_FIELDS if f in reg_cols]
        has_bidx = "name_bidx" in reg_cols
        sel = list(rtodo) + (["name_bidx"] if has_bidx else [])
        rows = conn.execute(
            "SELECT rowid, user_id, %s FROM regions" % ", ".join(sel)
        ).fetchall()
        updated = 0
        for r in rows:
            changed = {}
            for f in rtodo:
                v = r[f]
                if v is None or v == "":
                    continue
                if crypto.is_encrypted(v):
                    continue
                changed[f] = crypto.encrypt_field(v)
            # 盲索引：缺失或算出不一致都重算。
            # 用**解密后**的明文算，这样"已加密但没盲索引"的中间态也能补齐。
            if has_bidx:
                plain_name = crypto.decrypt_field(r["name"]) if "name" in rtodo else None
                want = crypto.blind_index(plain_name)
                if want and r["name_bidx"] != want:
                    changed["name_bidx"] = want
            if changed:
                conn.execute(
                    "UPDATE regions SET %s WHERE rowid=?"
                    % ", ".join("%s=?" % k for k in changed),
                    list(changed.values()) + [r["rowid"]],
                )
                updated += 1
        if updated:
            print("[migrate] 已加密回填 %d 条自定义区域" % updated)

    # ---- 任务 ----
    def create(self, request: AnalysisRequest, user_id: int | None = None) -> str:
        tid = uuid.uuid4().hex[:12]
        values = {
            "task_id": tid,
            "status": "pending",
            "task_type": request.task_type.value,
            "region": request.region,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "cloud_threshold": request.cloud_threshold,
            "code": "",
            "result": None,
            "logs": json.dumps([]),
            "attempts": 0,
            "created_at": time.time(),
            "user_id": _norm_uid(user_id),
        }
        values = self._enc_task_row(values)
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, status, task_type, region, start_date, end_date,"
                " cloud_threshold, code, result, logs, attempts, created_at, user_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(values.values()),
            )
        return tid

    def update(self, tid: str, **kwargs) -> None:
        allowed = {"status", "code", "result", "logs", "attempts", "started_at", "finished_at"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"].model_dump())
        if "logs" in fields:
            fields["logs"] = json.dumps(fields["logs"])
        if not fields:
            return
        fields = self._enc_task_row(fields)
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [tid]
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", vals)

    def get(self, tid: str) -> dict | None:
        """按 ID 取任务。**不做归属校验**，调用方必须自行比对 user_id。"""
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, user_id: int | None = None, limit: int = 50,
             offset: int = 0) -> list[dict]:
        """列出任务。`user_id=None` 表示不按用户过滤。

        ⚠ `limit` 是**分页**参数，调用方必须自己决定要不要告诉用户"还有更多"。
        2026-09-17 实测踩过：`/api/tasks` 忘了传 limit，于是接口静默只返回最近 50 条 ——
        不报错、不给总数、翻不了页。任务表涨到 51 条时，用户看到 50 条且无法知道少了什么。
        症状和「冻结年份」是同一类：**当时看着是对的，数据一长过阈值就悄悄错**。
        所以配套加了 `count()`，接口把 `total` / `has_more` 一起返回。
        """
        # cloud_threshold 于 2026-09-20 补入：列表页也要能回显云量阈值，
        # 且前端「批量重跑」会读它（原先取不到 → 一律退回默认 20）。
        sql = (
            "SELECT task_id, status, task_type, region, start_date, end_date, "
            "cloud_threshold, created_at, attempts, user_id FROM tasks"
        )
        args: list = []
        if user_id is not None:
            sql += " WHERE user_id = ?"
            args.append(_norm_uid(user_id))
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args.extend([limit, max(0, offset)])
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        # list 查了 region（列表上要显示），所以也要过一遍解密
        return [self._dec_row(dict(r)) for r in rows]

    def count(self, user_id: int | None = None) -> int:
        """任务总数（与 `list()` 同口径）。用来判断分页是否还有下一页。"""
        sql = "SELECT COUNT(*) AS n FROM tasks"
        args: list = []
        if user_id is not None:
            sql += " WHERE user_id = ?"
            args.append(_norm_uid(user_id))
        with self._lock, self._conn() as conn:
            return int(conn.execute(sql, args).fetchone()["n"])

    # ---- 用户反馈 ----
    #: 合法的反馈取值。用常量而非裸字符串：`feedback` 列在 SQLite 里加不了 CHECK
    #: （ALTER TABLE 不支持），校验只能放在这一层。
    FEEDBACK_VALUES = ("up", "down")

    def set_feedback(self, tid: str, value: str, note: str = "") -> bool:
        """记录用户对某次结果的反馈。返回是否写入成功。

        反馈允许多次修改（用户改主意很正常，最新一次为准），所以是 UPDATE 而非 INSERT。

        **归属校验不在这里做** —— 调用方（API 层）必须先确认任务属于当前用户，
        否则会变成「知道别人的 task_id 就能给他打差评」。
        """
        if value not in self.FEEDBACK_VALUES:
            return False
        note = (note or "").strip()[:500]  # 限长：这是自由文本，不设上限就是无界写入
        note = crypto.encrypt_field(note)  # 自由文本可能含个人信息，同样加密
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET feedback=?, feedback_note=?, feedback_at=? WHERE task_id=?",
                (value, note, time.time(), tid),
            )
            return cur.rowcount > 0

    def feedback_summary(self, user_id: int | None = None) -> dict:
        """反馈汇总：用于"0 用户"这条线的最低成本度量。

        刻意同时返回**已反馈数**与**任务总数** —— 只报"3 个赞"没有意义，
        得知道分母（是 3/5 还是 3/500），否则又变成"弱信号漂亮"。
        """
        where, args = "", []
        if user_id is not None:
            where = " WHERE user_id = ?"
            args.append(_norm_uid(user_id))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                f"SELECT feedback, COUNT(*) AS n FROM tasks{where}"
                + (" AND" if where else " WHERE")
                + " feedback IS NOT NULL GROUP BY feedback",
                args,
            ).fetchall()
            total = int(
                conn.execute(f"SELECT COUNT(*) AS n FROM tasks{where}", args).fetchone()["n"]
            )
            notes = conn.execute(
                f"SELECT task_id, feedback, feedback_note FROM tasks{where}"
                + (" AND" if where else " WHERE")
                + " feedback IS NOT NULL AND feedback_note IS NOT NULL AND feedback_note != ''"
                + " ORDER BY feedback_at DESC LIMIT 50",
                args,
            ).fetchall()
        counts = {r["feedback"]: int(r["n"]) for r in rows}
        n_up, n_down = counts.get("up", 0), counts.get("down", 0)
        rated = n_up + n_down
        return {
            "total_tasks": total,
            "rated": rated,
            "up": n_up,
            "down": n_down,
            # 分母为 0 时返回 None 而不是 0.0 —— 0.0 会被读成"满意度为 0"，
            # 而真相是"还没有人评过"，这是两件事。
            "satisfaction": round(n_up / rated, 3) if rated else None,
            "coverage": round(rated / total, 3) if total else None,
            # notes 里的 feedback_note 是加密存的，这里要解出来再给调用方
            "notes": [self._dec_row(dict(r)) for r in notes],
        }

    def backfill_owner(self, user_id: int) -> int:
        """把无主数据（`user_id = 0`）划给指定用户。返回改动的任务行数（区域另计）。"""
        uid = _norm_uid(user_id)
        with self._lock, self._conn() as conn:
            cur = conn.execute("UPDATE tasks SET user_id=? WHERE user_id = 0", (uid,))
            changed = cur.rowcount
            conn.execute("UPDATE regions SET user_id=? WHERE user_id = 0", (uid,))
        return changed

    def count_unowned(self) -> int:
        with self._lock, self._conn() as conn:
            return conn.execute(
                "SELECT (SELECT COUNT(*) FROM tasks WHERE user_id = 0)"
                " + (SELECT COUNT(*) FROM regions WHERE user_id = 0)"
            ).fetchone()[0]

    # ---- 配额统计 ----
    def count_active(self, user_id: int | None = None) -> int:
        """统计未结束（pending / running）的任务数。`user_id=None` 表示全库。

        用于并发配额：这两个状态的任务都在等或占着执行资源。
        """
        sql = "SELECT COUNT(*) FROM tasks WHERE status IN ('pending','running')"
        args: list = []
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(_norm_uid(user_id))
        with self._lock, self._conn() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def count_since(self, since: float, user_id: int | None = None) -> int:
        """统计 `created_at >= since` 的任务数，供滑动窗口限流使用。"""
        sql = "SELECT COUNT(*) FROM tasks WHERE created_at >= ?"
        args: list = [since]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(_norm_uid(user_id))
        with self._lock, self._conn() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def fail_orphans(self) -> int:
        """把上个进程遗留的 pending / running 任务标记为失败，返回处理条数。

        为什么必须在启动时做一次：服务重启后不存在任何后台执行线程，这些行会永远
        停在"执行中" —— 前端一直转圈，而且会**永久占住并发配额**（配额按状态计数，
        僵尸行不清掉，用户就再也提交不了新任务）。
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT task_id, logs FROM tasks WHERE status IN ('pending','running')"
            ).fetchall()
            for r in rows:
                try:
                    logs = json.loads(r["logs"]) if r["logs"] else []
                except Exception:  # noqa: BLE001
                    logs = []
                logs.append("服务重启：上次执行被中断，已标记为失败，请重新提交")
                conn.execute(
                    "UPDATE tasks SET status='failed', logs=? WHERE task_id=?",
                    (json.dumps(logs, ensure_ascii=False), r["task_id"]),
                )
        return len(rows)

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        # ⚠ 顺序很关键：先解密，再 json.loads。
        # result / logs 是"JSON 文本先加密、再落盘"，所以从库里取出的是**密文**，
        # 拿密文调 json.loads 会直接抛 JSONDecodeError。
        # 反过来也不行：解密必须在解析之前。
        d = TaskStore._dec_row(d)
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        d["logs"] = json.loads(d["logs"]) if d.get("logs") else []
        return d

    # ---- 自定义常用区域 ----
    def add_region(
        self, name: str, lon: float, lat: float, desc: str = "", user_id: int | None = None
    ) -> None:
        """保存自定义区域。同名（同一用户下）覆盖。

        ⚠ 不能再用 `INSERT OR REPLACE`：主键是 `(name, user_id)`，而 `name` 现在是
        **密文**且每次加密结果不同 —— 同一个区域名每次存进去都是一串新密文，
        `REPLACE` 永远匹配不到旧行，结果是**每保存一次就多一行**。
        所以改成"先按盲索引定位并删除旧行，再插入"。
        """
        uid = _norm_uid(user_id)
        enc_name = crypto.encrypt_field(name)
        bidx = crypto.blind_index(name)
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM regions WHERE user_id=? AND name_bidx=?", (uid, bidx)
            )
            conn.execute(
                "INSERT INTO regions (name, user_id, lon, lat, desc, created_at, name_bidx)"
                " VALUES (?,?,?,?,?,?,?)",
                (enc_name, uid, lon, lat, crypto.encrypt_field(desc), time.time(), bidx),
            )

    def list_regions(self, user_id: int | None = None) -> list:
        sql = "SELECT name, lon, lat, desc FROM regions"
        args: list = []
        if user_id is not None:
            sql += " WHERE user_id = ?"
            args.append(_norm_uid(user_id))
        sql += " ORDER BY created_at DESC"
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._dec_row(dict(r)) for r in rows]

    def delete_region(self, name: str, user_id: int | None = None) -> bool:
        """删除区域。带 user_id 时只删自己名下的，别人的同名区域删不动（返回 False）。

        联合主键 `(name, user_id)` 让同名区域能各自共存，所以这里的 `AND user_id = ?`
        是**必需**的 —— 少了它会把所有同名区域一起删掉。

        ⚠ `name` 是密文存储的，必须用**盲索引**定位：
        Fernet 每次加密带新随机 IV，同一明文两次加密结果不同，
        拿明文重新加密的密文与库里的比对**永远不会相等**（2026-09-20 实测踩过）。
        """
        bidx = crypto.blind_index(name)
        sql = "DELETE FROM regions WHERE name_bidx=?"
        args: list = [bidx]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(_norm_uid(user_id))
        with self._lock, self._conn() as conn:
            cur = conn.execute(sql, args)
            if cur.rowcount > 0:
                return True
            # 回退：迁移可能还没回填出 name_bidx（半新半旧的库），
            # 这时按明文再试一次，避免"迁移没跑到就删不掉"。
            fb = "DELETE FROM regions WHERE name=?"
            fargs: list = [name]
            if user_id is not None:
                fb += " AND user_id = ?"
                fargs.append(_norm_uid(user_id))
            return conn.execute(fb, fargs).rowcount > 0

    # ---- 用户设置 / 分析偏好 ----
    def get_settings(self, user_id: int | None = None) -> dict:
        """返回 `{key: value}`。先取全局默认值，再用该用户的私有值覆盖（私有优先）。"""
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT key, value, user_id FROM user_settings").fetchall()
        out: dict = {}
        for r in rows:
            if r["user_id"] != GLOBAL_USER_ID:
                continue
            out[r["key"]] = self._decode(r["value"])
        if user_id is not None:
            for r in rows:
                if r["user_id"] == user_id:
                    out[r["key"]] = self._decode(r["value"])
        return out

    def set_setting(self, key: str, value, user_id: int | None = None) -> None:
        uid = _norm_uid(user_id)
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_settings (key, user_id, value, updated_at)"
                " VALUES (?,?,?,?)",
                (key, uid, json.dumps(value, ensure_ascii=False), time.time()),
            )

    @staticmethod
    def _decode(raw):
        try:
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            return raw


store = TaskStore()
