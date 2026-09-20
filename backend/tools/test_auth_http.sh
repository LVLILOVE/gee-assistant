#!/usr/bin/env bash
# 鉴权 HTTP 层验证。用 curl 直接打接口，覆盖「未登录必须被挡」与「登录后可用」两侧。
#
#   bash tools/test_auth_http.sh
#
# ⚠️ 本脚本**会注册账号、会写库**，所以它必须跑在自己的临时实例上。
#
# 这个约束是用一次真实事故换来的：脚本原先写死 `BASE=http://127.0.0.1:8010`，
# 也就是**直接打线上库**。它注册的固定用户名成了库里的「首个用户」，于是
#   1) 自动拿到 is_admin=1；
#   2) 触发 _adopt_legacy_data()，把 22 条历史任务全部划归到它名下；
#   3) 而那个账号的密码就硬编码在本文件里 —— 等于把历史任务对任何读过仓库的人开放。
# 更糟的是它**不可重复运行**：第二次跑时用户已存在，注册断言直接 409，
# 后面带 cookie 的断言全部级联失败，看起来像产品坏了。
#
# 所以：跑自己的实例 + 自己的临时库 + 每次全新。想验证**线上**部署，
# 用只读的 `tools/smoke_live.sh`，别用这个。
set -u

# Git Bash 的 pwd 给 /c/Users/... 形式，交给 Windows 上的 Python resolve() 会变成
# C:\c\Users\...，服务会在别处新建一个空库、脚本照样全绿却什么都没验到。pwd -W 给 Windows 风格。
BACKEND_DIR="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"
cd "$BACKEND_DIR" || exit 1

PORT="${PORT:-8015}"
BASE="http://127.0.0.1:$PORT"
# 不用 mktemp：Git Bash 会给出 /c/... 路径，shell 的 rm 与本机删除钩子对该组合会失败。
# 也不用 rm -rf：本机删除钩子对目录递归删除会挂住（实测超时）。
WORK=".auth_tmp_$$"
DB="$BACKEND_DIR/$WORK/app.db"
LOG="$WORK/server.log"
JAR="$WORK/cookies.txt"
HDR="$WORK/register.hdr"   # 注册响应的原始头（cookie jar 不保留 SameSite）
PY="./.venv/Scripts/python.exe"
USER="chktest_user"
PASS="chkpass12345"
pass=0
fail=0

mkdir -p "$WORK"

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
  GEE_PROXY= \
  GEE_PROJECT= \
  "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 60); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 2 "$BASE/health")" = "200" ]; then
      # 光有 200 不够：万一路径拼错，服务会在别处建个空库，测试照样"全绿"。
      # 断言临时库确实出现在预期位置。
      if [ -f "$DB" ]; then
        return 0
      fi
      echo "  FAIL  实例起来了，但临时库不在预期位置：$DB"
      return 1
    fi
    sleep 0.5
  done
  return 1
}

cleanup() {
  if [ -n "${SRV_PID:-}" ]; then
    kill "$SRV_PID" 2>/dev/null
    MSYS_NO_PATHCONV=1 taskkill /F /PID "$SRV_PID" >/dev/null 2>&1
    SRV_PID=""
  fi
  # 等端口释放再删文件，否则会撞上还开着的句柄（app.db / server.log）。
  for _ in $(seq 1 30); do
    netstat -ano 2>/dev/null | grep -qE ":$PORT .*LISTENING" || break
    sleep 0.5
  done
  # 逐个删 + 重试：本机删除钩子遇到「被占用的文件」会 fail-closed，
  # 一条 rm 带上全部文件时只要一个删不掉，整批都原封不动。
  for f in "$JAR" "$HDR" "$LOG" "$DB" "$DB-wal" "$DB-shm"; do
    [ -e "$f" ] || continue
    for _ in 1 2 3 4 5 6; do
      rm -f "$f" 2>/dev/null
      [ -e "$f" ] || break
      sleep 0.5
    done
    [ -e "$f" ] && echo "  WARN  删不掉：$f"
  done
  rmdir "$WORK" 2>/dev/null
  [ -d "$WORK" ] && echo "  WARN  临时目录未清理干净：$BACKEND_DIR/$WORK"
  return 0
}
trap cleanup EXIT

chk() {  # chk <名称> <期望码> <实际码>
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-46s %s\n' "$1" "$3"
    pass=$((pass + 1))
  else
    printf '  FAIL  %-46s 期望 %s 实际 %s\n' "$1" "$2" "$3"
    fail=$((fail + 1))
  fi
}

# 明确区分「跳过」与「失败」。网络依赖类断言在本机不通时不该记成产品缺陷，
# 但也不能悄悄放过 —— 所以要打印出来。
skipped=0
skip() { printf '  SKIP  %-46s %s\n' "$1" "$2"; skipped=$((skipped + 1)); }

code() { curl -s -o /dev/null -w '%{http_code}' -m 20 "$@"; }

echo "=== 0. 启动测试实例（:$PORT，临时库，offline 后端）==="
if ! start_server; then
  echo "  服务未就绪，日志末尾："
  tail -20 "$LOG"
  exit 1
fi
echo "  PASS  实例已就绪 pid=$SRV_PID（临时库：$WORK/app.db）"

echo
echo "=== 1. 公开端点（不需要登录）==="
chk "GET /health"                    200 "$(code "$BASE/health")"
chk "GET /api/auth/me"               200 "$(code "$BASE/api/auth/me")"
chk "GET /api/tasks/types"           200 "$(code "$BASE/api/tasks/types")"
chk "GET /api/knowledge"             200 "$(code "$BASE/api/knowledge")"
chk "GET /api/regions"               200 "$(code "$BASE/api/regions")"

echo
echo "=== 2. 未登录访问受保护端点，必须 401 ==="
chk "GET  /api/tasks"                401 "$(code "$BASE/api/tasks")"
chk "GET  /api/gee/status"           401 "$(code "$BASE/api/gee/status")"
chk "GET  /api/preferences"          401 "$(code "$BASE/api/preferences")"
chk "GET  /api/profiles"             401 "$(code "$BASE/api/profiles")"
chk "GET  /api/tasks/e70b9f3440bf"   401 "$(code "$BASE/api/tasks/e70b9f3440bf")"
chk "GET  /api/tasks/e70b9f3440bf/report" 401 "$(code "$BASE/api/tasks/e70b9f3440bf/report")"
chk "POST /api/parse"                401 "$(code -X POST "$BASE/api/parse" -H 'Content-Type: application/json' -d '{"text":"太湖"}')"
chk "POST /api/ask"                  401 "$(code -X POST "$BASE/api/ask" -H 'Content-Type: application/json' -d '{"question":"hi"}')"
chk "POST /api/plan"                 401 "$(code -X POST "$BASE/api/plan" -H 'Content-Type: application/json' -d '{"text":"太湖"}')"
chk "POST /api/tasks (提交任务)"      401 "$(code -X POST "$BASE/api/tasks" -H 'Content-Type: application/json' -d '{"task_type":"ndvi","region":"太湖流域","start_date":"2024-01-01","end_date":"2024-12-31"}')"
chk "PUT  /api/preferences"          401 "$(code -X PUT "$BASE/api/preferences" -H 'Content-Type: application/json' -d '{"data_source":"auto"}')"
chk "POST /api/regions"              401 "$(code -X POST "$BASE/api/regions" -H 'Content-Type: application/json' -d '{"name":"x","lon":1,"lat":1}')"

echo
echo "=== 3. 未登录时 /api/auth/me 的响应体 ==="
echo "  $(curl -s -m 10 "$BASE/api/auth/me")"

echo
echo "=== 4. 注册 ==="
chk "弱密码被拒(7位)"                400 "$(code -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"1234567\"}")"
chk "非法用户名被拒"                 400 "$(code -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' -d '{"username":"a","password":"12345678"}')"
# 同时落一份原始响应头：cookie jar（Netscape 格式）只保留 HttpOnly 标记，
# **不保留 SameSite**，所以下面第 5 组必须读原始头而不是读 jar。
REG_CODE=$(curl -s -m 20 -c "$JAR" -D "$HDR" -o /dev/null -w '%{http_code}' \
  -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$PASS\"}")
chk "正常注册"                       200 "$REG_CODE"
chk "重名注册被拒"                   409 "$(code -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"$PASS\"}")"

echo
echo "=== 5. cookie 属性（读原始 Set-Cookie）==="
# 注意大小写：Starlette 写出来的是 SameSite=lax（小写 l），不是规范里的 Lax。
for attr in HttpOnly "SameSite=lax"; do
  if tr -d '\r' <"$HDR" | grep -qi "$attr"; then
    printf '  PASS  %-46s\n' "Set-Cookie 带 $attr"
    pass=$((pass + 1))
  else
    printf '  FAIL  %-46s\n' "Set-Cookie 缺少 $attr"
    tr -d '\r' <"$HDR" | grep -i '^set-cookie' | sed 's/^/    /'
    fail=$((fail + 1))
  fi
done

echo
echo "=== 6. 已登录访问受保护端点 ==="
chk "GET  /api/tasks"                200 "$(code -b "$JAR" "$BASE/api/tasks")"
chk "GET  /api/preferences"          200 "$(code -b "$JAR" "$BASE/api/preferences")"
chk "GET  /api/profiles"             200 "$(code -b "$JAR" "$BASE/api/profiles")"
# 已登录但任务不存在 -> 404。这条把"没登录(401)"和"没这个任务(404)"区分开，
# 也就是越权语义：不存在的 ID 与别人的 ID 都只给 404，不泄露是否存在。
chk "GET  不存在的任务 -> 404"        404 "$(code -b "$JAR" "$BASE/api/tasks/e70b9f3440bf")"
chk "GET  不存在任务的报告 -> 404"    404 "$(code -b "$JAR" "$BASE/api/tasks/e70b9f3440bf/report")"

# /api/gee/status 会去探 GEE 连通性，本机不通时可能一直等（实测 20s 超时 -> 000）。
# 它同时是「最可能回显凭据的端点」，所以保留检查，但不让网络抖动变成假失败。
GEE_CODE="$(code -b "$JAR" "$BASE/api/gee/status")"
if [ "$GEE_CODE" = "000" ]; then
  skip "GET  /api/gee/status" "无响应（本机连不通 GEE），跳过"
elif [ "$GEE_CODE" = "200" ]; then
  chk "GET  /api/gee/status"         200 "$GEE_CODE"
else
  skip "GET  /api/gee/status" "返回 $GEE_CODE（GEE 未就绪），此处不判失败"
fi

echo
echo "=== 7. 登录失败与节流 ==="
chk "错误密码 -> 401"                401 "$(code -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"wrongpass123\"}")"
chk "不存在用户 -> 401"              401 "$(code -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d '{"username":"no_such_user","password":"wrongpass123"}')"
chk "正确密码 -> 200"                200 "$(code -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"$PASS\"}")"

echo
echo "=== 8. Bearer 头方式（便于自动化）==="
TOK=$(curl -s -m 10 -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"$PASS\"}" -D - -o /dev/null | grep -i "set-cookie" | sed 's/.*gee_session=//' | cut -d';' -f1)
if [ -n "$TOK" ]; then
  chk "带 Bearer 可访问 /api/tasks"  200 "$(code -H "Authorization: Bearer $TOK" "$BASE/api/tasks")"
  chk "伪造 Bearer 被拒"             401 "$(code -H "Authorization: Bearer forged-token" "$BASE/api/tasks")"
else
  printf '  FAIL  %-46s\n' "未能提取会话 token"
  fail=$((fail + 1))
fi

echo
echo "=== 9. 注销后立即失效 ==="
chk "POST /api/auth/logout"          200 "$(code -b "$JAR" -c "$JAR" -X POST "$BASE/api/auth/logout")"
chk "注销后 /api/tasks -> 401"       401 "$(code -b "$JAR" "$BASE/api/tasks")"

echo
echo "=== 10. 安全性：错误信息不泄露账号是否存在 ==="
M1=$(curl -s -m 10 -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"wrongpass123\"}")
M2=$(curl -s -m 10 -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d '{"username":"no_such_user","password":"wrongpass123"}')
if [ "$M1" = "$M2" ]; then
  printf '  PASS  %-46s %s\n' "两者返回一致" "$M1"
  pass=$((pass + 1))
else
  printf '  FAIL  %-46s\n    已存在用户: %s\n    不存在用户: %s\n' "两者返回不一致" "$M1" "$M2"
  fail=$((fail + 1))
fi

echo
echo "=== 11. CORS：不允许陌生来源 ==="
# 这组断言防的是一次真实发现的回归：middleware 曾写成 allow_origins=["*"] +
# allow_credentials=True。实测随便一个 Origin 都会被原样回显并带 Allow-Credentials，
# 等于把「任意网站可带 cookie 读接口」的门敞开——当时靠 SameSite=Lax 才没被打穿。
# 生产前端同源托管，本就不需要 CORS。
#
# acao <origin>：取响应里的 Access-Control-Allow-Origin（不匹配时应为空）
acao() {
  curl -s -D - -o /dev/null -m 15 -H "Origin: $1" "$BASE/api/regions" 2>/dev/null \
    | tr -d '\r' | grep -i '^access-control-allow-origin:' | cut -d' ' -f2
}
chk "陌生 Origin 不被回显" "" "$(acao https://evil.example.com)"
chk "陌生 Origin 不变通为 *" "" "$(acao http://attacker.local)"
chk "localhost:5173 开发来源可用" "http://localhost:5173" "$(acao http://localhost:5173)"
PRE_ACAO=$(curl -s -D - -o /dev/null -m 15 -X OPTIONS \
  -H "Origin: https://evil.example.com" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" \
  "$BASE/api/tasks" 2>/dev/null | tr -d '\r' | grep -i '^access-control-allow-origin:' | cut -d' ' -f2)
chk "陌生 Origin 预检也不放行" "" "$PRE_ACAO"

echo
echo "=== 12. 受保护端点响应体不含密钥 ==="
BODY=$(curl -s --http1.1 -m 40 -H "Authorization: Bearer $TOK" "$BASE/api/gee/status" 2>/dev/null)
if [ -z "$BODY" ]; then
  # 拿不到响应体就不能宣称"没泄漏" —— 空响应通过等于自己骗自己。
  skip "gee/status 响应体不含密钥" "无响应体，无法判定"
elif printf '%s' "$BODY" | grep -qiE 'AIza|-----BEGIN|service_account|"key"'; then
  printf '  FAIL  %-46s\n' "/api/gee/status 疑似回显了密钥"
  printf '    %s\n' "$(printf '%s' "$BODY" | head -c 200)"
  fail=$((fail + 1))
else
  printf '  PASS  %-46s\n' "/api/gee/status 未回显密钥"
  pass=$((pass + 1))
fi

echo
echo "=== 13. 服务端无异常 ==="
# 别写 `grep -c ... || echo 0`：grep -c 计数为 0 时输出 0 且退出码 1，会再输出一个 0
ERRS="$(grep -ciE 'traceback|internal server error' "$LOG" 2>/dev/null)"
chk "后端日志无 Traceback" "0" "${ERRS:-0}"

cleanup
trap - EXIT

echo
echo "结果：$pass 通过，$fail 失败，$skipped 跳过"
[ "$fail" -eq 0 ] || exit 1
