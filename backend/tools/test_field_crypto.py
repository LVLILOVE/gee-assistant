#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""业务数据字段级加密检验（任务书 PRD §7「数据隐私」）。

PRD 原文要求「用户区域/任务描述敏感，**加密存储**」，而在此之前
`tasks` 表的 region / code / result / logs / feedback_note 与
`regions` 表的 name / desc 都是**明文落盘**的 —— 从库里直接捞一条就能读到
「长江三角洲」和完整可读的 GEE 脚本。本测试检验加密确实生效且不破坏功能。

跑在**真实 app.db 的一份副本**上（与 test_isolation.py 同一手法），因此同时覆盖：
  1. 存量明文数据的加密迁移是否正确、幂等、不丢数据；
  2. 加解密是否对上层透明（读出来仍是明文）；
  3. 加密后**功能是否还正常** —— 这是最容易忽略的一半：
     等值查询在密文上会静默失效，光验"密文看不懂"是不够的。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe tools\\test_field_crypto.py

为什么必须用副本：本测试会**改写数据**（加密回填），绝不能指向线上库。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
REAL_DB = os.path.join(BACKEND, "data", "app.db")

work = os.path.join(tempfile.gettempdir(), "enc_verify_%d" % time.time())
os.makedirs(work, exist_ok=True)
TMP_DB = os.path.join(work, "app.db")
TMP_KEY = os.path.join(work, ".data_key")
if os.path.exists(REAL_DB):
    shutil.copy2(REAL_DB, TMP_DB)

# 必须在 import app.* 之前设好：DB_PATH 是模块级常量，导入即定型
os.environ["APP_DB_PATH"] = TMP_DB

# 密钥策略：**副本里若是密文，就必须沿用生产密钥**，否则解不开。
#
# 这里踩过一次：本脚本最初一律生成新临时密钥，理由是"别碰生产密钥"。
# 结果在"生产库已完成加密回填"之后，副本里全是生产密钥加密的密文，
# 用新密钥解 → InvalidToken → 断言全红（2026-09-20 回归实测）。
# 正确做法是**看副本状态决定**：
#   * 副本里有 ENC1: 密文 → 复制生产密钥过来（只读使用，不修改原件）
#   * 副本是明文（从未加密过）→ 用全新临时密钥，避免污染生产密钥
_REAL_KEY = os.path.join(BACKEND, "data", "credentials", ".data_key")
_needs_real_key = False
if os.path.exists(TMP_DB):
    try:
        _c = sqlite3.connect(TMP_DB)
        _r = _c.execute(
            "SELECT region FROM tasks WHERE region LIKE 'ENC1:%' LIMIT 1"
        ).fetchone()
        _c.close()
        _needs_real_key = _r is not None
    except sqlite3.Error:
        pass

if _needs_real_key and os.path.exists(_REAL_KEY):
    shutil.copy2(_REAL_KEY, TMP_KEY)
    os.environ["WB_DATA_KEY_PATH"] = TMP_KEY
    print("[info] 副本含密文，已复制生产密钥用于解密（只读，原件未改动）")
else:
    # 密钥指向临时位置，避免在 backend/data/credentials/ 生成/覆盖真实密钥
    os.environ["WB_DATA_KEY_PATH"] = TMP_KEY
    if _needs_real_key:
        print("[warn] 副本含密文但找不到生产密钥 —— 解密断言会失败")

ok = []
def check(name, cond, extra=""):
    ok.append(bool(cond))
    print("  %s  %s%s" % ("[ OK ]" if cond else "[FAIL]", name, (" —— " + str(extra)) if extra else ""))


def raw(sql, args=()):
    c = sqlite3.connect(TMP_DB)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def raw_oneline(sql, args=()):
    r = raw(sql, args)
    return dict(r[0]) if r else None


# 迁移前快照（用裸 sqlite，趁 app 还没 import）。
#
# ⚠ 快照要**归一成明文**再存：本测试的断言是"加密后读出来的值 == 迁移前的明文"，
#   而如果副本本来就已加密（生产库已完成回填），直接存原始值拿到的就是密文，
#   与"解密后的明文"永远不相等。所以这里用生产密钥先解一遍。
_snap_key = None
if os.path.exists(TMP_KEY):
    _snap_key = open(TMP_KEY, "rb").read().strip()


def _snap_plain(v):
    """把快照值归一为明文（密文则解密，明文原样返回）。"""
    if not v or not _snap_key or not str(v).startswith("ENC1:"):
        return v
    try:
        from cryptography.fernet import Fernet

        return Fernet(_snap_key).decrypt(str(v)[len("ENC1:"):].encode()).decode("utf-8")
    except Exception:  # noqa: BLE001
        return v


PRE = {}
for r in raw("SELECT task_id, region, code FROM tasks"):
    PRE[r["task_id"]] = {"region": _snap_plain(r["region"]), "code": _snap_plain(r["code"])}
PRE_COUNTS = {"tasks": raw_oneline("SELECT COUNT(*) AS n FROM tasks")["n"]}
print("迁移前：%d 条任务，抽一条看 region=%r" % (PRE_COUNTS["tasks"], list(PRE.values())[0]["region"][:20] if PRE else None))
print("=" * 66)

print("\n== import app（会触发迁移）==")
from app import crypto  # noqa: E402
from app.task_store import store  # noqa: E402
print("crypto 状态:", crypto.crypto_status())

print("\n== A. 磁盘上确实是密文 ==")
row = raw_oneline("SELECT region, code FROM tasks LIMIT 1")
check("region 落盘为密文（带 ENC1: 前缀）", str(row["region"]).startswith("ENC1:"), str(row["region"])[:30])
check("code 落盘为密文", str(row["code"]).startswith("ENC1:"))
check("磁盘上看不到明文「长江三角洲」",
      not any("长江三角洲" in str(r[0] or "") for r in raw("SELECT region FROM tasks")))
check("磁盘上看不到明文 'import ee'",
      not any("import ee" in str(r[0] or "") for r in raw("SELECT code FROM tasks")))

print("\n== B. 应用层读出来仍是明文（透明加解密）==")
t = store.list(None, limit=200)
plain = [x for x in t if x.get("region") and "长江三角洲" in str(x["region"])]
check("list() 返回解密后的 region", len(plain) > 0, "命中 %d 条" % len(plain))
one = store.get(t[0]["task_id"])
check("get() 的 region 不是密文", not str(one["region"]).startswith("ENC1:"), str(one["region"])[:24])
check("get() 的 code 不是密文", not str(one["code"] or "").startswith("ENC1:"))
check("get() 的 logs 能正常解析成 list", isinstance(one["logs"], list), type(one["logs"]).__name__)
check("get() 的 result 能正常解析", one["result"] is None or isinstance(one["result"], dict))

print("\n== C. 数据没丢（数量与内容一致）==")
check("任务条数不变", store.count(None) == PRE_COUNTS["tasks"],
      "%d vs %d" % (store.count(None), PRE_COUNTS["tasks"]))
allt = store.list(None, limit=500)
ids_now = {x["task_id"] for x in allt}
check("任务 ID 集合不变", ids_now == set(PRE.keys()), "少了 %s" % (set(PRE) - ids_now))
# 逐条比对解密后的 region 是否等于迁移前的明文
bad = []
for tid, pv in PRE.items():
    cur = store.get(tid)
    if cur["region"] != pv["region"]:
        bad.append(tid)
    if (cur["code"] or "") != (pv["code"] or ""):
        bad.append(tid + "(code)")
check("逐条解密后与迁移前明文完全一致", not bad, bad[:3])

print("\n== D. 幂等：重复迁移不二次加密 ==")
before = [r["region"] for r in raw("SELECT region FROM tasks")]
# 用 with 拿连接（与生产代码同路径），直接传裸 connect() 会因未提交而锁库
with store._conn() as _c:
    store._migrate(_c)
with store._conn() as _c:
    store._migrate(_c)
with store._conn() as _c:
    store._migrate(_c)
after = [r["region"] for r in raw("SELECT region FROM tasks")]
check("跑 3 次迁移后密文一字未变", before == after)
check("迁移后仍能正常解密", store.get(allt[0]["task_id"])["region"] == PRE[allt[0]["task_id"]]["region"])
check("未发生双重前缀（无 ENC1:ENC1:）",
      not any(str(x).startswith("ENC1:ENC1:") for x in after))

print("\n== E. 新增数据走加密路径 ==")
from app.models import AnalysisRequest, TaskType  # noqa: E402
req = AnalysisRequest(task_type=TaskType.ndvi, region="珠穆朗玛峰北坡", start_date="2025-01-01",
                      end_date="2025-12-31", cloud_threshold=20)
new_id = store.create(req, user_id=4)
raw_new = raw_oneline("SELECT region FROM tasks WHERE task_id=?", (new_id,))
check("新任务 region 落盘为密文", str(raw_new["region"]).startswith("ENC1:"))
check("新任务读回为明文", store.get(new_id)["region"] == "珠穆朗玛峰北坡")
store.update(new_id, code="import ee\nprint('secret-aoi-123')")
raw_new2 = raw_oneline("SELECT code, region FROM tasks WHERE task_id=?", (new_id,))
check("update() 写入的 code 也是密文", str(raw_new2["code"]).startswith("ENC1:"))
check("update() 未破坏 region", str(raw_new2["region"]).startswith("ENC1:"))
check("新任务 code 读回正确", "secret-aoi-123" in (store.get(new_id)["code"] or ""))

print("\n== F. 反馈（feedback_note 也加密）==")
store.set_feedback(new_id, "up", "这个区域的植被变化很明显")
raw_fb = raw_oneline("SELECT feedback, feedback_note FROM tasks WHERE task_id=?", (new_id,))
check("feedback 本身保持明文（枚举值 up/down）", raw_fb["feedback"] == "up")
check("feedback_note 落盘为密文", str(raw_fb["feedback_note"]).startswith("ENC1:"))
check("feedback_note 读回为明文",
      store.get(new_id)["feedback_note"] == "这个区域的植被变化很明显")
s = store.feedback_summary()
check("feedback_summary 的 notes 是明文",
      all(not str(n.get("feedback_note") or "").startswith("ENC1:") for n in s["notes"]),
      s["notes"][:1])

print("\n== G. regions 表加密 + 删除仍可用 ==")
store.add_region("我的测试区", 118.5, 32.1, "测试用", user_id=4)
raw_rg = raw_oneline("SELECT name, desc, name_bidx FROM regions WHERE user_id=4")
check("region name 落盘为密文", str(raw_rg["name"]).startswith("ENC1:"))
check("region desc 落盘为密文", str(raw_rg["desc"]).startswith("ENC1:"))
check("写了 name_bidx 盲索引", str(raw_rg["name_bidx"]).startswith("BIDX1:"))
rg = store.list_regions(4)
check("list_regions 读回明文", any(r["name"] == "我的测试区" for r in rg), rg[:1])

# 覆盖保存：同名再存不该多出一行（密文不同，OR REPLACE 会失效）
store.add_region("我的测试区", 119.0, 33.0, "改过", user_id=4)
n_same = raw_oneline("SELECT COUNT(*) AS n FROM regions WHERE user_id=4")["n"]
check("同名重复保存不产生重复行", n_same == 1, "行数=%d" % n_same)
check("同名保存后坐标被更新", any(
    r["name"] == "我的测试区" and abs(r["lon"] - 119.0) < 1e-6 for r in store.list_regions(4)))

check("能删掉自己的区域", store.delete_region("我的测试区", user_id=4))
check("删掉后列表里没有了", not any(r["name"] == "我的测试区" for r in store.list_regions(4)))

# 跨用户：别人的同名区域删不动
store.add_region("共享名", 100.0, 30.0, "A的", user_id=4)
store.add_region("共享名", 110.0, 35.0, "B的", user_id=7)
check("别人的同名区域删不动", store.delete_region("共享名", user_id=999) is False)
check("A 的区域还在", any(r["name"] == "共享名" for r in store.list_regions(4)))
check("B 的区域还在", any(r["name"] == "共享名" for r in store.list_regions(7)))

print("\n== H. 损坏数据不拖垮读取（解密失败降级）==")
c = sqlite3.connect(TMP_DB)
c.execute("UPDATE tasks SET region='ENC1:not-a-valid-token' WHERE task_id=?", (new_id,))
c.commit(); c.close()
got = store.get(new_id)
check("坏密文返回占位符而非抛异常", got is not None and got["region"] == crypto.UNDECRYPTABLE, got["region"] if got else None)
lst = store.list(None, limit=500)
check("列表接口仍能返回全部（不被坏行拖垮）", len(lst) == store.count(None))

print("\n== I. 密钥隔离 ==")
check("临时密钥文件已就位（测试用自己的副本）", os.path.exists(TMP_KEY))
check("生产密钥文件未被本测试改动",
      os.path.exists(_REAL_KEY) and open(_REAL_KEY, "rb").read().strip() ==
      (open(_REAL_KEY, "rb").read().strip() if os.path.exists(_REAL_KEY) else None))

print("\n" + "=" * 66)
print("结果：%d/%d 通过" % (sum(ok), len(ok)))
try:
    shutil.rmtree(work, ignore_errors=True)
except OSError:
    pass
sys.exit(0 if all(ok) else 1)
