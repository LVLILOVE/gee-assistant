#!/usr/bin/env bash
# 多用户隔离 HTTP 层验证：起一个**独立端口 + 独立临时库 + 离线执行后端**的实例，
# 用两个账号交叉访问，验证「看不到、删不掉、改不了别人的东西」。
#
# 为什么单开实例：线上实例用的是真实 app.db 与 GEE 后端，测试数据会污染它，
# 而且真实 GEE 任务要跑几分钟。这里用 APP_DB_PATH + EXECUTION_BACKEND=offline
# 让结果既隔离又快又确定。
#
# 运行：bash tools/test_isolation_http.sh
set -u

# Git Bash 的 pwd 给的是 /c/Users/... 形式。这种路径一旦交给 Windows 上的 Python
# 去 resolve()，会被当成「当前盘符根目录下的相对路径」→ C:\c\Users\...。
# 后果很隐蔽：服务会在别处新建一个**空库**，脚本照样跑得"全绿"，但读的是空数据。
# pwd -W 输出 Windows 风格（C:/Users/...）；非 Git Bash 环境没有该选项，回退 pwd。
BACKEND_DIR="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"
cd "$BACKEND_DIR" || exit 1

PORT="${PORT:-8011}"
BASE="http://127.0.0.1:$PORT"
# 不用 mktemp：Git Bash 会给出 /c/... 路径，shell 的 rm 与本机删除钩子对该组合会失败。
# 也不用 rm -rf：本机的删除钩子对目录递归删除会挂住（实测超时），
# 所以每次用唯一目录名，收尾只 rm 单文件 + rmdir 空目录。
WORK=".iso_http_tmp_$$"
JAR_A="$WORK/a.jar"
JAR_B="$WORK/b.jar"
LOG="$WORK/server.log"
pass=0
fail=0

mkdir -p "$WORK"

cleanup() {
  if [ -n "${SRV_PID:-}" ]; then
    kill "$SRV_PID" 2>/dev/null
    MSYS_NO_PATHCONV=1 taskkill /F /PID "$SRV_PID" >/dev/null 2>&1
    SRV_PID=""
  fi
  # 等端口真正释放。不等就删文件，Windows 上会撞上还开着的句柄：
  # app.db / server.log 删不掉，临时目录里的东西就一直在。
  for _ in $(seq 1 30); do
    netstat -ano 2>/dev/null | grep -qE ":$PORT .*LISTENING" || break
    sleep 0.5
  done
  # 逐个删 + 重试：本机删除钩子遇到「被占用的文件」会 fail-closed，
  # 一条 rm 带上全部文件时只要一个删不掉，整批都原封不动。
  for f in "$JAR_A" "$JAR_B" "$LOG" \
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

echo "=== 0. 启动隔离测试实例（:$PORT，临时库，offline 后端）==="
# 空值 DEEPSEEK_API_KEY：load_dotenv 不覆盖已存在的环境变量，于是代码生成走 fallback，
# 提交的任务会立刻跑完，避免测试等几分钟。
APP_DB_PATH="$BACKEND_DIR/$WORK/app.db" \
EXECUTION_BACKEND=offline \
DEEPSEEK_API_KEY= \
BACKEND_PORT="$PORT" \
AUTH_ENABLED=true \
ALLOW_REGISTRATION=true \
ADMIN_USERNAME= \
ADMIN_PASSWORD= \
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  >"$LOG" 2>&1 &
SRV_PID=$!

up=0
for _ in $(seq 1 40); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 2 "$BASE/health")" = "200" ]; then
    up=1
    break
  fi
  sleep 0.5
done

if [ "$up" != "1" ]; then
  echo "  服务未起来，日志末尾："
  tail -20 "$LOG"
  exit 1
fi
echo "  PASS  实例已就绪 pid=$SRV_PID"

# 自检：确认服务真的在**预期位置**建了库。
# 少了这一步，上面注释里那个路径坑会让整个脚本"用空库跑完全绿"——
# 断言全过，但验的是一份空数据，等于没验。
if [ ! -f "$WORK/app.db" ]; then
  echo "  FAIL  临时库没出现在预期位置：$BACKEND_DIR/$WORK/app.db"
  echo "        多为路径被解析到别处（Git Bash 的 /c/... 被当成 C:\\c\\...）。"
  tail -5 "$LOG"
  exit 1
fi
echo "  PASS  临时库位置正确 $BACKEND_DIR/$WORK/app.db"

chk() {  # chk <名称> <期望> <实际>
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-44s %s\n' "$1" "$3"
    pass=$((pass + 1))
  else
    printf '  FAIL  %-44s 期望 %s 实际 %s\n' "$1" "$2" "$3"
    fail=$((fail + 1))
  fi
}

chknot() {  # chknot <名称> <不应出现的子串> <实际内容>
  if printf '%s' "$3" | grep -q "$2"; then
    printf '  FAIL  %-44s 不该出现 %s\n' "$1" "$2"
    fail=$((fail + 1))
  else
    printf '  PASS  %-44s 未泄露\n' "$1"
    pass=$((pass + 1))
  fi
}

code() { curl -s -o /dev/null -w '%{http_code}' -m 20 "$@"; }
body() { curl -s -m 20 "$@"; }

echo
echo "=== 1. 建两个账号 ==="
chk "注册 A" 200 "$(code -c "$JAR_A" -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' -d '{"username":"iso_http_a","password":"password123"}')"
chk "注册 B" 200 "$(code -c "$JAR_B" -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' -d '{"username":"iso_http_b","password":"password456"}')"
chk "A 身份正确" 200 "$(code -b "$JAR_A" "$BASE/api/auth/me")"
chk "A 是管理员" "true" "$(body -b "$JAR_A" "$BASE/api/auth/me" | grep -o '"is_admin":[a-z]*' | cut -d: -f2)"
chk "B 不是管理员" "false" "$(body -b "$JAR_B" "$BASE/api/auth/me" | grep -o '"is_admin":[a-z]*' | cut -d: -f2)"
chk "A 初始任务列表为空" '"tasks":[]' "$(body -b "$JAR_A" "$BASE/api/tasks" | tr -d ' ' | grep -o '"tasks":\[\]')"
chk "B 初始任务列表为空" '"tasks":[]' "$(body -b "$JAR_B" "$BASE/api/tasks" | tr -d ' ' | grep -o '"tasks":\[\]')"
# 分页契约：接口必须给出 total / has_more，否则超过一页时调用方无法判断"是不是拿全了"。
# 2026-09-17 实测过早期版本静默截断 50 条（不报错、不给总数），这里锁住不再退化。
chk "列表接口返回 total"    '"total":0'      "$(body -b "$JAR_A" "$BASE/api/tasks" | tr -d ' ' | grep -o '"total":0')"
chk "列表接口返回 has_more" '"has_more":false' "$(body -b "$JAR_A" "$BASE/api/tasks" | tr -d ' ' | grep -o '"has_more":false')"

echo
echo "=== 2. A 提交任务，B 不得可见 ==="
TID="$(body -b "$JAR_A" -X POST "$BASE/api/tasks" -H 'Content-Type: application/json' \
  -d '{"task_type":"ndvi","region":"太湖流域","start_date":"2024-06-01","end_date":"2024-08-31","cloud_threshold":20}' \
  | grep -o '"task_id":"[^"]*"' | cut -d'"' -f4)"
if [ -n "$TID" ]; then
  chk "拿到任务 ID" "yes" "yes"
else
  chk "拿到任务 ID" "yes" "空"
fi

# 等离线后端跑完
st=""
for _ in $(seq 1 40); do
  st="$(body -b "$JAR_A" "$BASE/api/tasks/$TID" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)"
  if [ "$st" = "succeeded" ] || [ "$st" = "failed" ]; then break; fi
  sleep 0.5
done
echo "  （任务状态：${st:-未知}）"

chk "A 能看到自己的任务"    200 "$(code -b "$JAR_A" "$BASE/api/tasks/$TID")"
chk "A 能拿到自己的报告"    200 "$(code -b "$JAR_A" "$BASE/api/tasks/$TID/report")"
chk "B 看 A 的任务详情=404" 404 "$(code -b "$JAR_B" "$BASE/api/tasks/$TID")"
chk "B 下 A 的报告=404"     404 "$(code -b "$JAR_B" "$BASE/api/tasks/$TID/report")"
chk "A 的任务列表含自己的任务" "$TID" \
  "$(body -b "$JAR_A" "$BASE/api/tasks" | grep -o "$TID" | head -1)"
chknot "B 的任务列表不含 A 的任务" "$TID" "$(body -b "$JAR_B" "$BASE/api/tasks")"

echo
echo "=== 3. 越权返回 404 而非 403（不暴露任务是否存在）==="
chk "B 查不存在的 ID 也是 404" 404 "$(code -b "$JAR_B" "$BASE/api/tasks/deadbeef0000")"
chk "A 查不存在的 ID 也是 404" 404 "$(code -b "$JAR_A" "$BASE/api/tasks/deadbeef0000")"

echo
echo "=== 4. 自定义区域隔离 ==="
chk "A 建区域" 200 "$(code -b "$JAR_A" -X POST "$BASE/api/regions" \
  -H 'Content-Type: application/json' -d '{"name":"zone-alpha","lon":120.1,"lat":31.2,"desc":"A 的"}')"
chk "B 建区域" 200 "$(code -b "$JAR_B" -X POST "$BASE/api/regions" \
  -H 'Content-Type: application/json' -d '{"name":"zone-beta","lon":116.1,"lat":29.2,"desc":"B 的"}')"

REG_A="$(body -b "$JAR_A" "$BASE/api/regions")"
REG_B="$(body -b "$JAR_B" "$BASE/api/regions")"
chknot "A 的区域列表不含 B 的区域" "zone-beta"  "$REG_A"
chknot "B 的区域列表不含 A 的区域" "zone-alpha" "$REG_B"
chknot "匿名看不到 A 的区域"       "zone-alpha" "$(body "$BASE/api/regions")"
chk   "匿名仍能拿到内置区域" 200 "$(code "$BASE/api/regions")"

chk "B 删 A 的区域=404" 404 "$(code -b "$JAR_B" -X DELETE "$BASE/api/regions/zone-alpha")"
chk "删失败后 A 的区域还在" "zone-alpha" \
  "$(body -b "$JAR_A" "$BASE/api/regions" | grep -o 'zone-alpha' | head -1)"
chk "A 删自己的区域=200" 200 "$(code -b "$JAR_A" -X DELETE "$BASE/api/regions/zone-alpha")"
chknot "删除后 A 的区域已消失" "zone-alpha" "$(body -b "$JAR_A" "$BASE/api/regions")"

echo
echo "=== 4b. 同名区域：B 不能顶掉 A 的（跨用户覆盖防护）==="
chk "A 建同名区域(lon=120.5)" 200 "$(code -b "$JAR_A" -X POST "$BASE/api/regions" \
  -H 'Content-Type: application/json' -d '{"name":"same-name-zone","lon":120.5,"lat":31.5,"desc":"A 的"}')"
chk "B 建同名区域(lon=100.5)" 200 "$(code -b "$JAR_B" -X POST "$BASE/api/regions" \
  -H 'Content-Type: application/json' -d '{"name":"same-name-zone","lon":100.5,"lat":20.5,"desc":"B 的"}')"

# A 的坐标必须还是 120.5 —— 旧结构下这里会变成 B 的 100.5
chk "A 的坐标没被 B 顶掉" "120.5" \
  "$(body -b "$JAR_A" "$BASE/api/regions" | grep -o 'same-name-zone","lon":[0-9.]*' | grep -o '[0-9.]*$')"
chk "B 的坐标是自己的" "100.5" \
  "$(body -b "$JAR_B" "$BASE/api/regions" | grep -o 'same-name-zone","lon":[0-9.]*' | grep -o '[0-9.]*$')"
chk "B 删同名区域删的是自己的" 200 "$(code -b "$JAR_B" -X DELETE "$BASE/api/regions/same-name-zone")"
chk "B 删完后 A 的同名区域仍在" "120.5" \
  "$(body -b "$JAR_A" "$BASE/api/regions" | grep -o 'same-name-zone","lon":[0-9.]*' | grep -o '[0-9.]*$')"
chk "B 现在删不到 A 的了=404" 404 "$(code -b "$JAR_B" -X DELETE "$BASE/api/regions/same-name-zone")"
chk "A 自删同名区域=200" 200 "$(code -b "$JAR_A" -X DELETE "$BASE/api/regions/same-name-zone")"

echo
echo "=== 5. 分析偏好 / 画像隔离 ==="
MARK_A="MARKER_ALPHA_9157"
MARK_B="MARKER_BETA_2604"
chk "A 写偏好" 200 "$(code -b "$JAR_A" -X PUT "$BASE/api/preferences" \
  -H 'Content-Type: application/json' -d "{\"instructions\":\"$MARK_A\"}")"
chk "B 写偏好" 200 "$(code -b "$JAR_B" -X PUT "$BASE/api/preferences" \
  -H 'Content-Type: application/json' -d "{\"instructions\":\"$MARK_B\",\"agent_profile\":\"mentor\"}")"

chknot "B 读不到 A 的自定义指令" "$MARK_A" "$(body -b "$JAR_B" "$BASE/api/preferences")"
chknot "A 读不到 B 的自定义指令" "$MARK_B" "$(body -b "$JAR_A" "$BASE/api/preferences")"
chknot "匿名读不到任何自定义指令" "$MARK_A" "$(body "$BASE/api/preferences")"
chk "A 拿到的还是自己的指令" "yes" "$(body -b "$JAR_A" "$BASE/api/preferences" | grep -q "$MARK_A" && echo yes || echo no)"
chk "A 画像是 analyst" "analyst" "$(body -b "$JAR_A" "$BASE/api/profiles" | grep -o '"current":"[^"]*"' | cut -d'"' -f4)"
chk "B 画像是 mentor"  "mentor"  "$(body -b "$JAR_B" "$BASE/api/profiles" | grep -o '"current":"[^"]*"' | cut -d'"' -f4)"

echo
echo "=== 6. 注销只影响自己 ==="
chk "A 注销" 200 "$(code -b "$JAR_A" -X POST "$BASE/api/auth/logout")"
chk "A 注销后确实被挡" 401 "$(code -b "$JAR_A" "$BASE/api/tasks")"
chk "B 未受影响" 200 "$(code -b "$JAR_B" "$BASE/api/tasks")"
chk "B 的偏好未被 A 的注销清掉" "$MARK_B" \
  "$(body -b "$JAR_B" "$BASE/api/preferences" | grep -o "$MARK_B" | head -1)"

echo
echo "=== 7. 服务端无异常 ==="
# 注意别写 `grep -c ... || echo 0`：grep -c 在计数为 0 时会输出 0 并返回 1，
# 于是 || 分支再输出一个 0，拿到的就是 "0\n0" 而不是 "0"。
ERRS="$(grep -ciE 'traceback|internal server error' "$LOG" 2>/dev/null)"
chk "后端日志无 Traceback" "0" "${ERRS:-0}"

cleanup
trap - EXIT

echo
echo "结果：$pass 通过 / $fail 失败"
[ "$fail" = "0" ] || exit 1
