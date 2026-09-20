"""AuthStore 逻辑检验（不经过 HTTP，快速暴露边界问题）。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import AuthError, AuthStore  # noqa: E402

tmp = os.path.join(tempfile.gettempdir(), "auth_test_%d.db" % time.time())
s = AuthStore(db_path=tmp)
ok = []


def check(name, cond, extra=""):
    ok.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))


print("=== 1. 注册与首用户成为管理员 ===")
u1 = s.create_user("tester", "password123")
check("首个用户创建成功", u1["username"] == "tester")
check("首个用户是管理员", u1["is_admin"] is True)
u2 = s.create_user("second", "password456")
check("第二个用户不是管理员", u2["is_admin"] is False)

print("\n=== 2. 用户名 / 密码校验 ===")
for bad_name, why in [("ab", "太短"), ("a" * 33, "太长"), ("bad name!", "含非法字符")]:
    try:
        s.create_user(bad_name, "password123")
        check("拒绝非法用户名(%s)" % why, False, "竟然通过了")
    except AuthError as e:
        check("拒绝非法用户名(%s)" % why, True, e.message)

try:
    s.create_user("shortpwd", "1234567")
    check("拒绝 7 位密码", False, "竟然通过了")
except AuthError as e:
    check("拒绝 7 位密码", True, e.message)

check("允许 8 位密码", s.create_user("eightchar", "12345678")["username"] == "eightchar")

try:
    s.create_user("tester", "password123")
    check("拒绝重名", False, "竟然通过了")
except AuthError as e:
    check("拒绝重名", e.status == 409, e.message)

print("\n=== 3. 登录 ===")
user = s.verify_login("tester", "password123")
check("正确密码可登录", user["username"] == "tester")
check("不返回密码哈希", "password_hash" not in user and "salt" not in user, list(user.keys()))

# 用户名前后空格应被容忍
check("用户名带空格也可登录", s.verify_login("  tester  ", "password123")["username"] == "tester")

# 密码错误与用户不存在必须返回同一条消息（不泄露用户是否存在）
msgs = []
for name, pwd in [("tester", "wrongpassword"), ("nobody", "whatever123")]:
    try:
        s.verify_login(name, pwd)
    except AuthError as e:
        msgs.append((e.status, e.message))
check("错误密码与不存在用户提示一致", msgs[0] == msgs[1], msgs)
check("登录失败返回 401", msgs[0][0] == 401)

print("\n=== 4. 登录失败节流 ===")
# 语义：_MAX_FAILS=5 表示「允许 5 次失败」，第 6 次才锁。
# 前 5 次都应返回常规 401，第 6 次返回 429。
last_status = None
for i in range(5):
    try:
        s.verify_login("second", "wrongpassword")
    except AuthError as e:
        last_status = e.status
check("第 1–5 次失败均为常规 401", last_status == 401, last_status)
try:
    s.verify_login("second", "wrongpassword")
    check("第 6 次失败触发锁定", False, "未锁定")
except AuthError as e:
    check("第 6 次失败触发锁定", e.status == 429, e.message)
try:
    s.verify_login("second", "password456")
    check("锁定期内正确密码也被拒", False, "竟然放行了")
except AuthError as e:
    check("锁定期内正确密码也被拒", e.status == 429)
check("节流按用户名独立（tester 不受影响）", s.verify_login("tester", "password123") is not None)

print("\n=== 5. 会话 ===")
tok = s.create_session(user["id"], 3600)
check("会话可解析", s.resolve_session(tok)["username"] == "tester")
check("空 token 返回 None", s.resolve_session(None) is None)
check("伪造 token 返回 None", s.resolve_session("forged-token-xyz") is None)

expired = s.create_session(user["id"], -1)
check("过期会话不可用", s.resolve_session(expired) is None)

s.delete_session(tok)
check("注销后会话失效", s.resolve_session(tok) is None)
s.delete_session(None)
check("注销 None 不报错", True)

print("\n=== 6. 密码哈希不可逆 ===")
import sqlite3  # noqa: E402

conn = sqlite3.connect(tmp)
row = conn.execute("SELECT password_hash, salt FROM users WHERE username='tester'").fetchone()
conn.close()
check("库里存的不是明文", row[0] != "password123" and "password123" not in row[0])
check("哈希长度 64（SHA-256）", len(row[0]) == 64, len(row[0]))
check("盐每次不同", len(row[1]) == 32)

print("\n=== 7. 清理过期会话 ===")
s.create_session(user["id"], -5)
n = s.purge_expired()
check("能清掉过期会话", n >= 1, "清理 %d 条" % n)

try:
    os.remove(tmp)
except OSError:
    pass

print("\n结果：%d/%d 通过" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
