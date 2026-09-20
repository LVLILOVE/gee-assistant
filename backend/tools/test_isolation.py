"""多用户隔离逻辑检验（不经过 HTTP）。

跑在**真实 app.db 的一份副本**上，因此同时覆盖了两件事：
  1. 旧库 → 新结构的自动迁移是否正确、幂等、不丢数据；
  2. tasks / regions / user_settings 三张表是否真的按用户隔离。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe tools\\test_isolation.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DB = os.path.join(BACKEND, "data", "app.db")

workdir = os.path.join(tempfile.gettempdir(), "iso_test_%d" % time.time())
os.makedirs(workdir, exist_ok=True)
TMP_DB = os.path.join(workdir, "app.db")

had_real = os.path.exists(REAL_DB)
if had_real:
    shutil.copy2(REAL_DB, TMP_DB)

# 必须在 import app.* 之前设置：DB_PATH 是模块级常量，导入即定型
os.environ["APP_DB_PATH"] = TMP_DB
# 密钥也指向临时位置：否则测试会拿**生产密钥**去解密生产副本，
# 而且会在 backend/data/credentials/ 留下/覆盖真实密钥（绝不该发生）。
# 用临时密钥后，副本里的密文对本测试是"解不开的"——但本测试断言的是
# 表结构/归属/盲索引，不依赖解出明文，所以正好也顺带验证了"换密钥时降级不炸"。
os.environ["WB_DATA_KEY_PATH"] = os.path.join(workdir, ".data_key")


def _snapshot(path):
    """在导入 app.* 之前，用裸 sqlite3 记录副本的"迁移前"样子。

    必须在 import 之前做：`app.task_store` 模块级就有 `store = TaskStore()`，
    一 import 就会把库迁移掉，之后再读就只能看到新结构了。
    """
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return {
            "tasks": c.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"],
            "settings": [dict(r) for r in c.execute("SELECT key, user_id, value FROM user_settings")]
            if "user_id" in [r[1] for r in c.execute("PRAGMA table_info(user_settings)")]
            else [dict(r) for r in c.execute("SELECT key, value FROM user_settings")],
            "task_cols": [r[1] for r in c.execute("PRAGMA table_info(tasks)")],
        }
    finally:
        c.close()


PRE_REAL = _snapshot(TMP_DB)

# ⚠ 这一行（以及下面的 from app.*）会立刻触发迁移
from app.auth import AuthStore  # noqa: E402
from app.preferences import (  # noqa: E402
    PREF_KEY,
    build_preference_note,
    get_preferences,
    get_profile,
    save_preferences,
)
from app.task_store import TaskStore  # noqa: E402

ok = []


def check(name, cond, extra=""):
    ok.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))


def bidx(name: str) -> str:
    """算 regions.name 的盲索引。

    `name` 自 2026-09-20 起是密文（Fernet 带随机 IV，同明文两次加密结果不同），
    所以库里**无法**用 `WHERE name=?` 查明文 —— 必须走 `name_bidx` 盲索引列。
    本测试底下几个断言原本直接查明文，加密上线后立刻失效（实测 2 个套件挂）。
    """
    from app import crypto

    return crypto.blind_index(name)


def raw(sql, args=()):
    return raw_db(TMP_DB, sql, args)


def raw_db(path, sql, args=()):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def raw_plain(sql, args=()):
    """查库并把指定列解密后再比 —— 用于断言"语义"而不是"落盘形态"。

    `desc` / `name` 现在是密文，直接拿原始值比明文必然失败（2026-09-20 实测）。
    """
    from app import crypto

    rows = raw(sql, args)
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, str) and crypto.is_encrypted(v):
                d[k] = crypto.decrypt_field(v)
        out.append(d)
    return out


def cols_of(table):
    return cols_of_db(TMP_DB, table)


def cols_of_db(path, table):
    return [r[1] for r in raw_db(path, "PRAGMA table_info(%s)" % table)]


def pk_of_db(path, table):
    """返回主键包含的列名。PRAGMA table_info 的第 6 列(pk) 是主键内序号，0 表示不是主键。"""
    return [r[1] for r in raw_db(path, "PRAGMA table_info(%s)" % table) if r[5]]


# 模块级 singleton 已经建在 TMP_DB 上（见上面的 import），这里显式再取一份引用
store = TaskStore(db_path=TMP_DB)
auths = AuthStore(db_path=TMP_DB)
check("真实副本已迁移出 tasks.user_id", "user_id" in cols_of("tasks"))
check("真实副本已迁移出 regions.user_id", "user_id" in cols_of("regions"))
check("真实副本已迁移出 user_settings.user_id", "user_id" in cols_of("user_settings"))
check("建了 user 索引", any(r[1] == "idx_tasks_user" for r in raw("PRAGMA index_list(tasks)")))


# ---------------------------------------------------------------- 迁移
print("=== 1a. 旧结构库迁移（合成旧库，结果确定）===")
OLD_DB = os.path.join(workdir, "old_schema.db")
c = sqlite3.connect(OLD_DB)
c.executescript(
    """
    CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, status TEXT, task_type TEXT, region TEXT,
        start_date TEXT, end_date TEXT, cloud_threshold REAL, code TEXT,
        result TEXT, logs TEXT, attempts INTEGER, created_at REAL
    );
    CREATE TABLE regions (
        name TEXT PRIMARY KEY, lon REAL, lat REAL, desc TEXT, created_at REAL
    );
    CREATE TABLE user_settings (
        key TEXT PRIMARY KEY, value TEXT, updated_at REAL
    );
    INSERT INTO tasks VALUES
        ('old000000001','succeeded','ndvi','太湖流域','2024-06-01','2024-08-31',20,'code-a',NULL,'[]',1,1.0),
        ('old000000002','failed','water','洞庭湖','2024-05-01','2024-09-30',30,'code-b',NULL,'[]',2,2.0);
    INSERT INTO regions VALUES ('老区域', 118.0, 30.0, '迁移前建的', 1.0);
    INSERT INTO user_settings VALUES ('analysis_preferences', '{"instructions":"迁移前的偏好"}', 1.0);
    """
)
c.commit()
c.close()

check("合成旧库确实没有 user_id", "user_id" not in _snapshot(OLD_DB)["task_cols"])
check("合成旧库 regions 主键只有 name", pk_of_db(OLD_DB, "regions") == ["name"],
      pk_of_db(OLD_DB, "regions"))

old_store = TaskStore(db_path=OLD_DB)  # 构造即迁移
check("旧库迁移出 tasks.user_id", "user_id" in cols_of_db(OLD_DB, "tasks"))
check("旧库迁移出 regions.user_id", "user_id" in cols_of_db(OLD_DB, "regions"))
check("旧库迁移出 user_settings.user_id", "user_id" in cols_of_db(OLD_DB, "user_settings"))
check("regions 主键升级为 (name, user_id)", sorted(pk_of_db(OLD_DB, "regions")) == ["name", "user_id"],
      pk_of_db(OLD_DB, "regions"))
check("旧任务一条没丢", old_store.list(None) and len(old_store.list(None)) == 2)
old_ids = {r["task_id"] for r in old_store.list(None)}
check("旧任务 ID 保持不变", old_ids == {"old000000001", "old000000002"})
check("旧数据归属归一为 0（不是 NULL）", old_store.count_unowned() == 3,
      "无主 %d 条（含 1 个区域）" % old_store.count_unowned())
check("库里查不到 NULL 归属",
      raw_db(OLD_DB, "SELECT COUNT(*) AS n FROM tasks WHERE user_id IS NULL")[0]["n"] == 0
      and raw_db(OLD_DB, "SELECT COUNT(*) AS n FROM regions WHERE user_id IS NULL")[0]["n"] == 0)
check("旧区域坐标迁移后不变",
      raw_db(OLD_DB, "SELECT user_id, lon, lat FROM regions")[0]["lon"] == 118.0
      and raw_db(OLD_DB, "SELECT user_id, lon, lat FROM regions")[0]["lat"] == 30.0
      and raw_db(OLD_DB, "SELECT user_id, lon, lat FROM regions")[0]["user_id"] == 0)
# 名称本身也要还在（加密了，所以要解密后再比；直接比明文会永远失败）
check("旧区域名称迁移后可按盲索引找回",
      len(raw_db(OLD_DB, "SELECT * FROM regions WHERE name_bidx=?", (bidx("老区域"),))) == 1)
check("旧区域名称已加密（不再是明文）",
      raw_db(OLD_DB, "SELECT name FROM regions")[0]["name"].startswith("ENC1:"))

old_pref = old_store.get_settings(None).get("analysis_preferences") or {}
check("旧设置迁为全局并保值", old_pref.get("instructions") == "迁移前的偏好", old_pref.get("instructions"))

old_store2 = TaskStore(db_path=OLD_DB)  # 再来一次，验证幂等
check("重复迁移不报错且任务数不变", len(old_store2.list(None)) == 2)
check("重复迁移不产生重复设置行", len(raw_db(OLD_DB, "SELECT * FROM user_settings")) == 1)
check("重复迁移后 user_settings 结构仍为 4 列", len(cols_of_db(OLD_DB, "user_settings")) == 4)
check("重复迁移后 regions 主键没被再改坏", sorted(pk_of_db(OLD_DB, "regions")) == ["name", "user_id"])
check("重复迁移后区域行数不变", len(raw_db(OLD_DB, "SELECT * FROM regions")) == 1)

# 接管逻辑也要能在合成旧库上跑通
oc = AuthStore(db_path=OLD_DB)
O1 = oc.create_user("old_admin", "password123")
moved_old = old_store2.backfill_owner(O1["id"])
check("合成旧库：接管 2 条任务", moved_old == 2, "改了 %d 条" % moved_old)
check("合成旧库：接管后无主清零", old_store2.count_unowned() == 0)
check("合成旧库：老区域归 O1", {r["name"] for r in old_store2.list_regions(O1["id"])} == {"老区域"})
check("合成旧库：接管后区域 user_id 变成 O1", 
      raw_db(OLD_DB, "SELECT user_id FROM regions")[0]["user_id"] == O1["id"])

# ---------------------------------------------------------------- 真实库副本
print("\n=== 1b. 真实库副本：迁移无损 ===")
if PRE_REAL:
    print("  （迁移前：%d 条任务、%d 条设置）" % (PRE_REAL["tasks"], len(PRE_REAL["settings"])))
    if "user_id" in PRE_REAL["task_cols"]:
        print("  （注意：真实库此前已迁移过，本次不是首次迁移）")
    after_tasks = raw("SELECT COUNT(*) AS n FROM tasks")[0]["n"]
    check("真实库迁移后任务数不变", after_tasks == PRE_REAL["tasks"],
          "%d -> %d" % (PRE_REAL["tasks"], after_tasks))
    check("真实库设置行数不变",
          len(raw("SELECT * FROM user_settings")) == len(PRE_REAL["settings"]))
else:
    print("  （未找到真实库，跳过）")

# ---------------------------------------------------------------- 历史数据接管
print("\n=== 2. 历史无主数据划归管理员 ===")
# ⚠️ 2026-09-17 修正：本节原先直接依赖真实库副本的「库里还没有用户 + 有 N 条
# 无主历史任务」这个状态来断言接管逻辑。线上库收尾（预置管理员、把 22 条历史
# 任务全部划归它）之后，该前置条件不再成立，本节 5 项断言随之失败——
# **失败原因是测试与生产数据状态耦合，不是接管逻辑坏了**。
#
# 教训：测试的前置条件必须由测试自己造，不能指望生产库长成某个样子；否则生产
# 状态一变（这恰恰是好事），测试就红，久而久之就没人信它了。
# 所以这里改成：复制一份库 → 清空用户 → 把数据打回无主 → 再造用户验证接管。
ADOPT_DB = os.path.join(workdir, "adopt.db")
shutil.copy2(TMP_DB, ADOPT_DB)
_seed = sqlite3.connect(ADOPT_DB)
_seed.execute("DELETE FROM users")
_seed.execute("DELETE FROM sessions")
_seed.execute("UPDATE tasks SET user_id = 0")
_seed.execute("UPDATE regions SET user_id = 0")
_seed.commit()
_seed.close()

# 期望值从库里现算，不写死条数——生产库任务数变化不该把测试带崩
EXPECT_TASKS = raw_db(ADOPT_DB, "SELECT COUNT(*) AS n FROM tasks")[0]["n"]
EXPECT_REGIONS = raw_db(ADOPT_DB, "SELECT COUNT(*) AS n FROM regions")[0]["n"]

adopt_store = TaskStore(db_path=ADOPT_DB)
adopt_auths = AuthStore(db_path=ADOPT_DB)

check("尚无用户时 first_admin_id 为空", adopt_auths.first_admin_id() is None)
unowned_before = adopt_store.count_unowned()
check("库里有历史无主数据",
      unowned_before == EXPECT_TASKS + EXPECT_REGIONS,
      "无主 %d 条（任务 %d + 区域 %d）" % (unowned_before, EXPECT_TASKS, EXPECT_REGIONS))

A = adopt_auths.create_user("iso_admin", "password123")
B = adopt_auths.create_user("iso_user", "password456")
check("第二用户不是管理员", B["is_admin"] is False)
check("first_admin_id 是首个用户", adopt_auths.first_admin_id() == A["id"])

moved = adopt_store.backfill_owner(A["id"])
check("接管返回改动任务数", moved == EXPECT_TASKS, "改了 %d 条" % moved)
check("接管后无主数据清零", adopt_store.count_unowned() == 0)
check("接管幂等（再次为 0）", adopt_store.backfill_owner(A["id"]) == 0)

la = adopt_store.list(A["id"], limit=EXPECT_TASKS + 10)
lb = adopt_store.list(B["id"], limit=EXPECT_TASKS + 10)
check("老数据全部归 A", len(la) == EXPECT_TASKS, "A 看到 %d 条" % len(la))
check("老数据对 B 不可见", len(lb) == 0, "B 看到 %d 条" % len(lb))
# 分页契约：`list()` 默认只取 50 条。上面显式传了大 limit，所以这条断言查的是
# **默认值本身有没有被静默用于"我拿全了"的假设**——2026-09-17 实测：
# `/api/tasks` 忘传 limit，任务涨到 51 条时接口只回 50 条且不给 total，
# 而本测试当时恰好库里 ≤50 条，于是**偶然通过**、从没真正验证过"全部归 A"。
check("count() 与完整列表长度一致（分页不会被误当全量）",
      adopt_store.count(A["id"]) == EXPECT_TASKS,
      "count=%d 期望=%d" % (adopt_store.count(A["id"]), EXPECT_TASKS))

# 第 3、4 节的隔离验证跑在真实库副本（TMP_DB）上，需要两个独立用户。
# 注意：真实库里已存在预置管理员，所以他们都不是"首个管理员"——上面那组
# 与管理员身份相关的断言已经在独立的 ADOPT_DB 上验证过了。
A = auths.create_user("iso_admin", "password123")
B = auths.create_user("iso_user", "password456")

# ---------------------------------------------------------------- 任务隔离
print("\n=== 3. 任务隔离 ===")
from app.models import AnalysisRequest, TaskType  # noqa: E402


def mk(region):
    return AnalysisRequest(
        task_type=TaskType.ndvi,
        region=region,
        start_date="2024-06-01",
        end_date="2024-08-31",
        cloud_threshold=20,
    )


tid_a = store.create(mk("A 的太湖"), A["id"])
tid_b = store.create(mk("B 的鄱阳湖"), B["id"])

ids_a = {r["task_id"] for r in store.list(A["id"])}
ids_b = {r["task_id"] for r in store.list(B["id"])}
check("A 能列出自己的新任务", tid_a in ids_a)
check("A 看不到 B 的任务", tid_b not in ids_a)
check("B 能列出自己的新任务", tid_b in ids_b)
check("B 看不到 A 的任务", tid_a not in ids_b)

row_a = store.get(tid_a)
check("新任务写入了归属", row_a["user_id"] == A["id"], "user_id=%s" % row_a["user_id"])
check("按 ID 取任务不做过滤(由端点校验)", store.get(tid_b)["user_id"] == B["id"])

all_ids = {r["task_id"] for r in store.list(None)}
check("鉴权关闭时不按用户过滤", {tid_a, tid_b} <= all_ids, "共 %d 条" % len(all_ids))

# ---------------------------------------------------------------- 区域隔离
print("\n=== 4. 自定义区域隔离 ===")
store.add_region("甲地", 120.0, 31.0, "A 定义的", A["id"])
store.add_region("乙地", 116.0, 29.0, "B 定义的", B["id"])

names_a = {r["name"] for r in store.list_regions(A["id"])}
names_b = {r["name"] for r in store.list_regions(B["id"])}
check("A 只看到自己的区域", names_a == {"甲地"}, names_a)
check("B 只看到自己的区域", names_b == {"乙地"}, names_b)

check("B 删不掉 A 的区域", store.delete_region("甲地", B["id"]) is False)
check("B 的区域未被误删", {r["name"] for r in store.list_regions(B["id"])} == {"乙地"})
check("A 能删自己的区域", store.delete_region("甲地", A["id"]) is True)
check("删除后 A 的区域为空", store.list_regions(A["id"]) == [])
check("同名区域可以分属不同人（重名不互相覆盖）", store.delete_region("乙地", B["id"]) is True)

# ---- 同名区域共存（联合主键的核心价值：B 不能顶掉 A 的同名区域）----
print("\n=== 4b. 同名区域共存（跨用户覆盖防护）===")
SAME = "共同区域名"
store.add_region(SAME, 120.0, 31.0, "A 的坐标", A["id"])
store.add_region(SAME, 100.0, 20.0, "B 的坐标", B["id"])

ra = [r for r in store.list_regions(A["id"]) if r["name"] == SAME]
rb = [r for r in store.list_regions(B["id"]) if r["name"] == SAME]
check("A 仍有自己的同名区域", len(ra) == 1, "A 命中 %d 条" % len(ra))
check("B 也拿到自己的同名区域", len(rb) == 1, "B 命中 %d 条" % len(rb))
check("A 的坐标没被 B 顶掉", ra and (ra[0]["lon"], ra[0]["lat"]) == (120.0, 31.0),
      (ra[0]["lon"], ra[0]["lat"]) if ra else None)
check("B 的坐标是自己的", rb and (rb[0]["lon"], rb[0]["lat"]) == (100.0, 20.0))
check("库里两行同名但归属不同",
      len(raw("SELECT * FROM regions WHERE name_bidx=?", (bidx(SAME),))) == 2)

check("B 删自己的同名区域成功", store.delete_region(SAME, B["id"]) is True)
check("删掉 B 的之后 A 的仍在", len([r for r in store.list_regions(A["id"]) if r["name"] == SAME]) == 1)
check("A 的坐标依然没变",
      raw("SELECT lon, lat FROM regions WHERE name_bidx=? AND user_id=?",
          (bidx(SAME), A["id"]))[0]["lon"] == 120.0)
check("B 现在删不到 A 的同名区域", store.delete_region(SAME, B["id"]) is False)
check("A 能删自己的同名区域", store.delete_region(SAME, A["id"]) is True)

# 同一用户重复保存同名区域应当是"更新"，不是插重复行
store.add_region("重复保存", 1.0, 1.0, "第一次", A["id"])
store.add_region("重复保存", 2.0, 2.0, "第二次", A["id"])
dup = raw_plain("SELECT lon, desc FROM regions WHERE name_bidx=? AND user_id=?",
                (bidx("重复保存"), A["id"]))
check("同用户重复保存=更新而非插重复行", len(dup) == 1 and dup[0]["lon"] == 2.0,
      "行数=%d" % len(dup))
check("重复保存后 desc 也被覆盖", len(dup) == 1 and dup[0]["desc"] == "第二次",
      dup[0]["desc"] if dup else None)

# 鉴权关闭场景（user_id 传 None → 落 0）：反复保存也不该插出重复行
store.add_region("无主区域", 5.0, 5.0, "v1", None)
store.add_region("无主区域", 6.0, 6.0, "v2", None)
unowned = raw("SELECT lon FROM regions WHERE name_bidx=?", (bidx("无主区域"),))
check("无主区域归一到 user_id=0",
      unowned and raw_db(TMP_DB, "SELECT user_id FROM regions WHERE name_bidx=?",
                         (bidx("无主区域"),))[0]["user_id"] == 0)
check("无主区域重复保存不产生重复行", len(unowned) == 1 and unowned[0]["lon"] == 6.0,
      "行数=%d" % len(unowned))

# ---------------------------------------------------------------- 偏好隔离
print("\n=== 5. 分析偏好隔离 ===")
check("A 的偏好为默认值", get_preferences(A["id"])["instructions"] == "")

save_preferences({"instructions": "只分析太湖水体的浊度"}, A["id"])
save_preferences({"instructions": "只关注植被覆盖度", "agent_profile": "mentor"}, B["id"])

check("A 读到自己的指令", get_preferences(A["id"])["instructions"] == "只分析太湖水体的浊度")
check("B 读到自己的指令", get_preferences(B["id"])["instructions"] == "只关注植被覆盖度")
check("A 不会读到 B 的指令", "植被" not in get_preferences(A["id"])["instructions"])
check("B 不会读到 A 的指令", "浊度" not in get_preferences(B["id"])["instructions"])
check("全局默认未被污染", get_preferences(None)["instructions"] == "", repr(get_preferences(None)["instructions"]))

C = auths.create_user("iso_third", "password789")
check("新用户拿到的是默认值", get_preferences(C["id"])["instructions"] == "")
store.set_setting(PREF_KEY, {"data_source": "landsat", "output_language": "zh"}, None)
check("新用户能继承全局默认", get_preferences(C["id"])["data_source"] == "landsat")
check("已设私有值的用户不受全局影响", get_preferences(B["id"])["data_source"] == "auto")

note_a = build_preference_note(user_id=A["id"])
note_b = build_preference_note(user_id=B["id"])
check("注入 prompt 的偏好是 A 的", "浊度" in note_a and "植被" not in note_a)
check("注入 prompt 的偏好是 B 的", "植被" in note_b and "浊度" not in note_b)
check("A 画像是分析师", get_profile(A["id"])["label"] == "分析师（默认）")
check("B 画像是导师", get_profile(B["id"])["label"] == "导师")

priv = {
    r["user_id"]: r["value"]
    for r in raw("SELECT user_id, value FROM user_settings WHERE key=?", (PREF_KEY,))
}
check("库里按 user_id 分行存储", A["id"] in priv and B["id"] in priv, "行数=%d" % len(priv))
check("A 的存储行不含 B 的文本", "植被" not in (priv.get(A["id"]) or ""))
check("全局行独立于用户行", 0 in priv)

# ---------------------------------------------------------------- 清理
try:
    shutil.rmtree(workdir, ignore_errors=True)
except OSError:
    pass

print("\n结果：%d/%d 通过" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
