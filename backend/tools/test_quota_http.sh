#!/usr/bin/env bash
# 任务配额 HTTP 层验证：全局并发、单用户并发、滑动窗口速率、429 语义，
# 以及「重启后僵尸任务被清理、并发名额被释放」。
#
# 为什么单开实例：线上实例不该被塞垃圾任务；而且这里要把配额调到很小才方便触发，
# 更需要独立配置。用 APP_DB_PATH + EXECUTION_BACKEND=offline 做到既隔离又快。
#
# 运行：bash tools/test_quota_http.sh
set -u

# Git Bash 的 pwd 给 /c/Users/... 形式，交给 Windows 上的 Python resolve() 会变成
# C:\c\Users\...，服务会在别处新建一个空库、脚本照样全绿却什么都没验到。pwd -W 给 Windows 风格。
BACKEND_DIR="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"
cd "$BACKEND_DIR" || exit 1

PORT="${PORT:-8014}"
BASE="http://127.0.0.1:$PORT"
WORK=".quota_tmp_$$"
DB="$BACKEND_DIR/$WORK/app.db"
LOG="$WORK/server.log"
JAR_A="$WORK/a.jar"
JAR_B="$WORK/b.jar"
JAR_C="$WORK/c.jar"
JAR_D="$WORK/d.jar"
PY="./.venv/Scripts/python.exe"
pass=0
fail=0

mkdir -p "$WORK"

# 配额故意设小，几秒内就能触发 429
LIM_ALL=2     # 全局并发
LIM_USER=1    # 单用户并发
LIM_HOUR=3    # 单用户每小时
LIM_DAY=100   # 单用户每天（放大，避免干扰小时用例）

start_server() {
  echo "===== 实例启动 $(date +%H:%M:%S) =====" >>"$LOG"
  APP_DB_PATH="$DB" \
  EXECUTION_BACKEND=offline \
  DEEPSEEK_API_KEY= \
  BACKEND_PORT="$PORT" \
  AUTH_ENABLED=true \
  ALLOW_REGISTRATION=true \
  ADMIN_USERNAME= \
  ADMIN_PASSWORD= \
  MAX_CONCURRENT_TASKS="$LIM_ALL" \
  MAX_CONCURRENT_TASKS_PER_USER="$LIM_USER" \
  TASK_RATE_LIMIT_PER_HOUR="$LIM_HOUR" \
  TASK_RATE_LIMIT_PER_DAY="$LIM_DAY" \
  "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 60); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 2 "$BASE/health")" = "200" ]; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

stop_server() {
  if [ -n "${SRV_PID:-}" ]; then
    kill "$SRV_PID" 2>/dev/null
    MSYS_NO_PATHCONV=1 taskkill /F /PID "$SRV_PID" >/dev/null 2>&1
    SRV_PID=""
  fi
  # 等端口真正释放。不等就删文件，Windows 上会撞上还开着的句柄：
  # app.db / server.log 删不掉，临时目录里的东西就一直在。
  for _ in $(seq 1 30); do
    netstat -ano 2>/dev/null | grep -qE ":$PORT .*LISTENING" || return 0
    sleep 0.5
  done
  echo "  WARN  :$PORT 仍被占用，后续清理可能受影响"
}

# 收尾清理。两个本机特性决定了它必须这么写：
#   1. 删除钩子遇到「被占用的文件」会 fail-closed —— 一条 rm 带上全部文件时，
#      只要有一个删不掉，整批都原封不动。所以必须逐个删、各自重试。
#   2. 进程被杀后句柄释放有延迟，所以要有重试等待。
# 失败时要说出来，不能静默（静默过一次：临时目录带着 8 个文件留在磁盘上）。
cleanup() {
  stop_server
  for f in "$JAR_A" "$JAR_B" "$JAR_C" "$JAR_D" "$LOG" \
           "$WORK/last.json" "$WORK/last.hdr" \
           "$WORK/app.db" "$WORK/app.db-wal" "$WORK/app.db-shm"; do
    [ -e "$f" ] || continue
    for _ in 1 2 3 4 5 6; do
      rm -f "$f" 2>/dev/null
      [ -e "$f" ] || break
      sleep 0.5
    done
    [ -e "$f" ] && echo "  WARN  删不掉：$BACKEND_DIR/$f"
  done
  rmdir "$WORK" 2>/dev/null
  if [ -d "$WORK" ]; then
    echo "  WARN  临时目录未清理干净：$BACKEND_DIR/$WORK"
    ls -la "$WORK" 2>/dev/null
  fi
  return 0
}
trap cleanup EXIT

echo "=== 0. 启动测试实例（:$PORT，临时库，offline 后端，配额 $LIM_ALL/$LIM_USER/$LIM_HOUR）==="
if ! start_server; then
  echo "  服务未起来，日志末尾："
  tail -20 "$LOG"
  exit 1
fi
echo "  PASS  实例已就绪 pid=$SRV_PID"

# 自检：确认服务真的在**预期位置**建了库，否则整轮验证都是对着空库跑的假绿
if [ ! -f "$WORK/app.db" ]; then
  echo "  FAIL  临时库没出现在预期位置：$DB"
  tail -5 "$LOG"
  exit 1
fi
echo "  PASS  临时库位置正确 $DB"

chk() {  # chk <名称> <期望> <实际>
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-46s %s\n' "$1" "$3"
    pass=$((pass + 1))
  else
    printf '  FAIL  %-46s 期望 %s 实际 %s\n' "$1" "$2" "$3"
    fail=$((fail + 1))
  fi
}

chkin() {  # chkin <名称> <应包含的子串> <实际内容>
  if printf '%s' "$3" | grep -q "$2"; then
    printf '  PASS  %-46s 命中「%s」\n' "$1" "$2"
    pass=$((pass + 1))
  else
    printf '  FAIL  %-46s 未包含「%s」，实际：%s\n' "$1" "$2" "$(printf '%s' "$3" | head -c 160)"
    fail=$((fail + 1))
  fi
}

code() { curl -s -o /dev/null -w '%{http_code}' -m 20 "$@"; }
body() { curl -s -m 20 "$@"; }

# 提交一个任务：响应头写 $WORK/last.hdr、响应体写 $WORK/last.json，回显 HTTP 码。
# 头和体必须同一次请求取，否则要发两次（第二次已经没有意义的重复请求）。
submit() {
  curl -s -m 20 -D "$WORK/last.hdr" -o "$WORK/last.json" -w '%{http_code}' -b "$1" \
    -X POST "$BASE/api/tasks" -H 'Content-Type: application/json' \
    -d '{"task_type":"ndvi","region":"太湖流域","start_date":"2024-06-01","end_date":"2024-08-31","cloud_threshold":20}'
}
last_detail() { grep -o '"detail":"[^"]*"' "$WORK/last.json" | cut -d'"' -f4; }
last_tid() { grep -o '"task_id":"[^"]*"' "$WORK/last.json" | cut -d'"' -f4; }
resp_hdr() { tr -d '\r' <"$WORK/last.hdr" | grep -i "^$1:" | cut -d' ' -f2; }

# 轮询到任务结束（offline 后端通常 1 秒内完成）
wait_done() {  # wait_done <jar> <tid>
  for _ in $(seq 1 60); do
    st="$(body -b "$1" "$BASE/api/tasks/$2" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)"
    case "$st" in
      succeeded | failed) return 0 ;;
    esac
    sleep 0.5
  done
  return 1
}

# 直接往库里塞一条 running 任务。用它模拟"有任务正在执行"，比依赖慢后端确定得多。
inject_running() {  # inject_running <用户名> <任务 ID>
  "$PY" -c '
import sqlite3, sys, time
db, username, tid = sys.argv[1], sys.argv[2], sys.argv[3]
c = sqlite3.connect(db, timeout=10)
row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
if row is None:
    sys.exit("no such user: " + username)
c.execute(
    "INSERT INTO tasks (task_id,status,task_type,region,start_date,end_date,cloud_threshold,"
    "code,result,logs,attempts,created_at,user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (tid, "running", "ndvi", "太湖流域", "2024-01-01", "2024-12-31", 20, "", None, "[]", 0,
     time.time(), row[0]),
)
c.commit()
c.close()
' "$DB" "$1" "$2"
}

clear_running() {  # 把所有 pending/running 清成 failed，模拟任务全部结束
  "$PY" -c '
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=10)
c.execute("UPDATE tasks SET status = ? WHERE status IN (?, ?)", ("failed", "pending", "running"))
c.commit()
c.close()
' "$DB"
}

echo
echo "=== 1. 建四个账号 ==="
chk "注册 A" 200 "$(code -c "$JAR_A" -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' -d '{"username":"quota_a","password":"password123"}')"
chk "注册 B" 200 "$(code -c "$JAR_B" -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' -d '{"username":"quota_b","password":"password123"}')"
chk "注册 C" 200 "$(code -c "$JAR_C" -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' -d '{"username":"quota_c","password":"password123"}')"
chk "注册 D" 200 "$(code -c "$JAR_D" -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' -d '{"username":"quota_d","password":"password123"}')"

echo
echo "=== 2. 匿名提交应仍是 401（配额不能把未登录变成 429）==="
chk "匿名 POST /api/tasks" 401 "$(code -X POST "$BASE/api/tasks" -H 'Content-Type: application/json' \
  -d '{"task_type":"ndvi","region":"太湖流域","start_date":"2024-06-01","end_date":"2024-08-31"}')"

echo
echo "=== 3. 正常提交不受影响 ==="
chk "A 首次提交" 200 "$(submit "$JAR_A")"
TID="$(last_tid)"
wait_done "$JAR_A" "$TID"
chk "A 的任务已结束" "succeeded" \
  "$(body -b "$JAR_A" "$BASE/api/tasks/$TID" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)"

echo
echo "=== 4. 单用户并发上限 ==="
inject_running quota_a fake_running_a
chk "A 已有 1 个在执行 → 再提交被拒" 429 "$(submit "$JAR_A")"
chkin "拒绝原因说明" "在执行" "$(last_detail)"
chk "带 Retry-After 头" "30" "$(resp_hdr Retry-After)"

echo
echo "=== 5. 并发额度按用户隔离（A 占用不挡 B）==="
chk "B 仍可提交" 200 "$(submit "$JAR_B")"
wait_done "$JAR_B" "$(last_tid)"

echo
echo "=== 6. 全局并发上限 ==="
inject_running quota_b fake_running_b
chk "A、B 各占 1（= 全局上限）→ C 提交被拒" 429 "$(submit "$JAR_C")"
chkin "拒绝原因说明" "服务繁忙" "$(last_detail)"

echo
echo "=== 7. 任务结束后名额立即释放 ==="
clear_running
chk "清掉在执行的任务后 C 可提交" 200 "$(submit "$JAR_C")"
wait_done "$JAR_C" "$(last_tid)"

echo
echo "=== 8. 滑动窗口速率上限（每用户 $LIM_HOUR 次/小时）==="
# C 在第 7 组已经成功提交 1 次，所以这里第 2、3 次放行，第 4 次应被拒
chk "C 第 2 次提交" 200 "$(submit "$JAR_C")"
wait_done "$JAR_C" "$(last_tid)"
chk "C 第 3 次提交" 200 "$(submit "$JAR_C")"
wait_done "$JAR_C" "$(last_tid)"
chk "C 第 4 次提交（超小时限额）" 429 "$(submit "$JAR_C")"
chkin "拒绝原因说明" "一小时内" "$(last_detail)"
chk "带 Retry-After 头" "600" "$(resp_hdr Retry-After)"
chk "429 不影响别人的额度" 200 "$(submit "$JAR_D")"
wait_done "$JAR_D" "$(last_tid)"

echo
echo "=== 9. 重启清理僵尸任务并释放名额 ==="
inject_running quota_d fake_running_d
chk "D 已有 1 个在执行 → 再提交被拒" 429 "$(submit "$JAR_D")"
stop_server
if start_server; then
  echo "  PASS  实例已重启 pid=$SRV_PID"
  pass=$((pass + 1))
else
  echo "  FAIL  重启失败"
  fail=$((fail + 1))
fi
chk "重启后 D 可提交（僵尸名额已释放）" 200 "$(submit "$JAR_D")"
wait_done "$JAR_D" "$(last_tid)"
ZOMBIE="$(body -b "$JAR_D" "$BASE/api/tasks/fake_running_d")"
chk "僵尸任务被标记为失败" "failed" \
  "$(printf '%s' "$ZOMBIE" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)"
chkin "僵尸任务日志有说明" "服务重启" "$ZOMBIE"
chk "库里已无 running 状态残留" "0" \
  "$(body -b "$JAR_D" "$BASE/api/tasks" | grep -o '"status":"running"' | wc -l | tr -d ' ')"

echo
echo "=== 10. 服务端无异常 ==="
# 别写 `grep -c ... || echo 0`：grep -c 计数为 0 时输出 0 且退出码 1，会再输出一个 0
ERRS="$(grep -ciE 'traceback|internal server error' "$LOG" 2>/dev/null)"
chk "后端日志无 Traceback" "0" "${ERRS:-0}"

cleanup
trap - EXIT

echo
echo "结果：$pass 通过 / $fail 失败"
[ "$fail" = "0" ] || exit 1
