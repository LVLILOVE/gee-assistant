#!/usr/bin/env bash
# 线上部署冒烟检查。**严格只读**：不注册、不登录、不写库、不提交任务。
#
#   bash tools/smoke_live.sh
#   BASE=https://xxxx.r31.cpolar.top bash tools/smoke_live.sh   # 连公网地址一起验
#
# 为什么单独有这么一个脚本：`test_auth_http.sh` 需要注册账号才能验登录态，
# 而它曾经直接打线上库 —— 注册出的固定用户名成了「首个用户」，自动拿到管理员，
# 还把历史任务划归到自己名下，而密码就写在仓库里。会写库的测试绝不能指向生产。
# 想验证生产，用这个只读脚本。
set -u

BASE="${BASE:-http://127.0.0.1:8010}"
pass=0
fail=0

chk() {  # chk <名称> <期望码> <实际码>
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-46s %s\n' "$1" "$3"
    pass=$((pass + 1))
  else
    printf '  FAIL  %-46s 期望 %s 实际 %s\n' "$1" "$2" "$3"
    fail=$((fail + 1))
  fi
}

# 线上走隧道时要用 http1.1，并且 -o 要给真实可写路径：
# 直接 -o /dev/null 会偶发 curl: (23) write error，输出 000 的假失败。
#
# 另外隧道本身不稳定，实测同一端点连打 3 次会出现「200 / 超时 / 200」，
# 甚至 30 秒后返回 502（那是 cpolar 代理到你本机这一段超时，不是应用的 502，
# 应用只在 /api/tile 失败时才返 502，而这里不打那个端点）。
# 所以把 **传输级失败**（000 / 502 / 504）纳入重试；连试多次仍失败才算 FAIL ——
# 那才是「当前这份部署确实不可靠」的结论。
code() {
  local i out
  for i in 1 2 3 4 5 6; do
    out=$(curl -sS --http1.1 -o /dev/null -w '%{http_code}' -m 30 "$@" 2>/dev/null)
    case "$out" in
      ""|000|502|504) sleep 1 ;;
      *) printf '%s' "$out"; return ;;
    esac
  done
  printf '%s' "${out:-000}"
}

echo "=== 目标：$BASE ==="
echo

echo "=== 1. 存活与公开端点 ==="
chk "GET /health"                 200 "$(code "$BASE/health")"
chk "GET /"                       200 "$(code "$BASE/")"
chk "GET /api/auth/me"            200 "$(code "$BASE/api/auth/me")"
chk "GET /api/tasks/types"        200 "$(code "$BASE/api/tasks/types")"
chk "GET /api/knowledge"          200 "$(code "$BASE/api/knowledge")"
chk "GET /api/regions"            200 "$(code "$BASE/api/regions")"

echo
echo "=== 2. 受保护端点对匿名必须 401 ==="
chk "GET  /api/tasks"             401 "$(code "$BASE/api/tasks")"
chk "GET  /api/gee/status"        401 "$(code "$BASE/api/gee/status")"
chk "GET  /api/preferences"       401 "$(code "$BASE/api/preferences")"
chk "GET  /api/profiles"          401 "$(code "$BASE/api/profiles")"
chk "POST /api/ask"               401 "$(code -X POST "$BASE/api/ask" -H 'Content-Type: application/json' -d '{"question":"hi"}')"

echo
echo "=== 3. 匿名响应体里不能有密钥或任务数据 ==="
BODY=$(curl -sS --http1.1 -m 30 "$BASE/api/regions" 2>/dev/null)
if printf '%s' "$BODY" | grep -qiE 'AIza|-----BEGIN|service_account|deepseek'; then
  printf '  FAIL  %-46s\n' "/api/regions 疑似泄漏密钥"
  fail=$((fail + 1))
else
  printf '  PASS  %-46s\n' "/api/regions 无密钥"
  pass=$((pass + 1))
fi
ANON=$(curl -sS --http1.1 -m 30 "$BASE/api/tasks" 2>/dev/null)
if printf '%s' "$ANON" | grep -q '"task_id"'; then
  printf '  FAIL  %-46s\n' "匿名 /api/tasks 泄漏了任务数据"
  fail=$((fail + 1))
else
  printf '  PASS  %-46s\n' "匿名 /api/tasks 无任务数据"
  pass=$((pass + 1))
fi

echo
echo "=== 4. CORS 只放行白名单来源 ==="
acao() {
  local i hdrs
  for i in 1 2 3 4; do
    hdrs=$(curl -sS --http1.1 -D - -o /dev/null -m 30 -H "Origin: $1" "$BASE/api/regions" 2>/dev/null | tr -d '\r')
    [ -n "$hdrs" ] && break
    sleep 1
  done
  printf '%s' "$hdrs" | grep -i '^access-control-allow-origin:' | cut -d' ' -f2
}
chk "陌生 Origin 不被回显" "" "$(acao https://evil.example.com)"
chk "陌生 Origin 不变通为 *" "" "$(acao http://attacker.local)"
chk "localhost:5173 开发来源可用" "http://localhost:5173" "$(acao http://localhost:5173)"

echo
echo "=== 5. 路径穿越必须被拦（这个洞真的能匿名读走 .env 和数据库）==="
# 回归用例。历史问题：SPA 兜底路由 `@app.get("/{full_path:path}")` 只判了
# is_file()，没判「resolve() 之后仍在 dist 之内」，而 uvicorn 会把 %2e 解码成
# '.'，于是 /%2e%2e/%2e%2e/backend/.env 直接把 .env（含 LLM 密钥）、后端源码、
# data/app.db（含全部用户与密码哈希）发给了**不需要登录**的任何人。服务挂在公网上。
#
# 两个细节不能省：
#   ① --path-as-is —— curl 默认会自己把 .. 归一化掉，请求根本发不出去，
#      那样跑出来全绿却什么都没验到；
#   ② 用 %2e 编码 —— 有些中间层只看字面 '..'，编码形式才是真正常用的打法。
OUT="${TMPDIR:-/tmp}/smoke_live_$$.out"
cleanup_out() { rm -f "$OUT" 2>/dev/null; return 0; }
trap cleanup_out EXIT

tcode() {  # 同 code()，但加 --path-as-is（路径里的 .. 原样发出）
  local i out
  for i in 1 2 3 4 5 6; do
    out=$(curl -sS --http1.1 --path-as-is -o "$OUT" -w '%{http_code}' -m 30 "$@" 2>/dev/null)
    case "$out" in
      ""|000|502|504) sleep 1 ;;
      *) printf '%s' "$out"; return ;;
    esac
  done
  printf '%s' "${out:-000}"
}

for p in "/%2e%2e/%2e%2e/backend/.env" \
         "/%2e%2e/%2e%2e/backend/app/config.py" \
         "/%2e%2e/%2e%2e/backend/data/app.db" \
         "/..%2f..%2fbackend%2f.env" \
         "/%2e%2e/%2e%2e/backend/.db_backup/app.db" \
         "/%2e%2e/%2e%2e/%2e%2e/.workbuddy/MEMORY.md"; do
  chk "穿越 $p" 404 "$(tcode "$BASE$p")"
done

# 光看状态码不够：万一拦漏了、但恰好返回 200 并带上文件内容，状态码断言会漏掉。
# 这里直接抓一次响应体，确认里面没有密钥名、没有 SQLite 文件头。
tcode "$BASE/%2e%2e/%2e%2e/backend/.env" >/dev/null 2>&1
if grep -qiE 'DEEPSEEK_API_KEY|ADMIN_PASSWORD|SQLite format 3|from pathlib import Path' "$OUT" 2>/dev/null; then
  printf '  FAIL  %-46s\n' "穿越响应体泄漏了配置或数据"
  fail=$((fail + 1))
else
  printf '  PASS  %-46s\n' "穿越响应体无密钥 / 无数据库内容"
  pass=$((pass + 1))
fi

echo
echo "=== 6. 接口打错路径要 JSON 404，不能拿 200 + HTML 骗过调用方 ==="
chk "GET /api/nope-xyz" 404 "$(code "$BASE/api/nope-xyz")"
tcode "$BASE/api/nope-xyz" >/dev/null 2>&1
if grep -q 'id="root"' "$OUT" 2>/dev/null; then
  printf '  FAIL  %-46s\n' "未知接口返回了前端外壳 HTML"
  fail=$((fail + 1))
else
  printf '  PASS  %-46s\n' "未知接口没回退到前端外壳"
  pass=$((pass + 1))
fi

echo
echo "结果：$pass 通过，$fail 失败"
[ "$fail" -eq 0 ] || exit 1
