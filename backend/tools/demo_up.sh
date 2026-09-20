#!/bin/bash
# ============================================================================
# 演示环境三层预案 —— 一键把「本地服务 + 公网隧道」拉到可用状态，并逐层自检
#
#   L1 本地直连   http://127.0.0.1:8010        永远可用的最后手段，不依赖任何外网
#   L2 公网隧道   https://<7位hex>.r31.cpolar.top   cpolar 临时隧道
#   L3 离线兜底   EXECUTION_BACKEND=offline     GEE 不可用时仍能走通全流程
#
# 为什么需要这个脚本（都是实测踩过的坑，不是假想风险）：
#   ① cpolar 免费版**没有预留 8010 域名**（`~/.cpolar/cpolar.yml` 里只有 8001/3389/8080），
#      只能走临时隧道 —— **每次重启域名都变**，上一秒还能打开的链接下一秒就 404。
#   ② 不加 `-log stdout` 的话 cpolar 不把隧道地址打到控制台，拿不到域名（等于白开）。
#   ③ 本机 `HTTP_PROXY` 指向 127.0.0.1:60722。服务**没起来**时本地 curl 会被代理
#      转成 **502**（而不是 000/连接拒绝），看起来像"网关问题"而不是"进程挂了"。
#      所以本脚本所有本地探测一律加 `--noproxy '*'`。
#   ④ 服务进程要在**真实终端窗口**里起。`nohup ... &` 在「命令执行完就回收子进程」的
#      自动化壳里会被连带终止（实测：起来 20 秒后再查已无监听，且日志无任何报错——
#      不是崩溃，是被回收）。在 PyCharm Terminal / cmd 里手跑本脚本不受影响。
#      本机 `cmd /c start /b` 被安全策略拦截，脚本不依赖它。
#
# 用法（在 backend 目录下）：
#   bash tools/demo_up.sh              # 拉起并自检本地 + 公网 + L4-L6 深度自检
#   bash tools/demo_up.sh --local-only # 只保证本地可用（公网用不上时）
#   bash tools/demo_up.sh --status     # 只看状态，什么都不改
#   bash tools/demo_up.sh --no-llm     # 跳过 L4 那次真实联网调用（不想消耗额度时加）
#   bash tools/demo_up.sh --self-test  # 只验自检规则本身（不碰服务/隧道/网络）#
# 自检分层：
#   L1 本地服务 / L2 公网隧道 / L3 离线兜底   —— 本脚本内置
#   L4 LLM 密钥是否真能调通 / L5 前端产物是否比源码新 / L6 库状态
#                                             —— 交给 tools/demo_doctor.py
# ============================================================================
set -u

cd "$(dirname "$0")/.." || exit 1                      # → backend/
BACKEND="$(pwd)"
# 这里不用 `ROOT="$(cd .. && pwd)"`：已经拿到 BACKEND 了，再开一次「子 shell + cd + pwd」
# 是多余的一跳，去掉更简单。
# ⚠️ 但要注意：**本脚本在「命令跑完就回收子进程」的自动化壳里跑不完**，与本行无关 ——
#    实测那种壳里连 `$(dirname ...)` 这种简单命令替换都会挂住（`bash -x` 会停在这一步，
#    看起来像脚本 bug，其实是执行环境不同）。**它的自检结果请在真实终端里取**
#    （PyCharm Terminal / cmd）。参见 tools/demo_doctor.py：那部分用 Python 写，
#    不依赖 shell 命令替换，自动化和终端里都能跑。
ROOT="$BACKEND/.."                                     # → gee-assistant/
PY="$BACKEND/.venv/Scripts/python.exe"
PORT="${BACKEND_PORT:-8010}"
LOCAL="http://127.0.0.1:$PORT"
CPOLAR_EXE="/c/Users/Administrator/cpolar/portable/cpolar.exe"
URL_FILE="$ROOT/.cache/tunnel_url.txt"                 # 最新公网地址落盘处
CPOLAR_LOG="$ROOT/.cache/cpolar_demo.log"
mkdir -p "$(dirname "$URL_FILE")"                      # 目录可能还不存在，先建

MODE="${1:-}"
# `--no-llm`：跳过 demo_doctor 里那次真实联网调用（没网/不想消耗额度时用）
SKIP_LLM=0
for a in "$@"; do [ "$a" = "--no-llm" ] && SKIP_LLM=1; done

ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; }
warn() { echo "  ⚠️  $*"; }

# ---------------------------------------------------------------- 探测工具
# 一律 --noproxy '*'：否则本地请求走 127.0.0.1:60722 代理，
# 服务没起来时会拿到 502 而不是连接失败，掩盖真实原因。
local_code() { curl -s -m 6 --noproxy '*' -o /dev/null -w '%{http_code}' "$LOCAL$1" 2>/dev/null; }
public_code() { curl -s -m 20 -o /dev/null -w '%{http_code}' "$1$2" 2>/dev/null; }

local_up() { [ "$(local_code /health)" = "200" ]; }

find_pid_on_port() {
    netstat -ano 2>/dev/null | grep LISTENING | grep ":$PORT " | awk '{print $5}' | head -1
}

# ---------------------------------------------------------------- L1 本地服务
start_local() {
    echo "[L1] 本地服务"
    if local_up; then
        ok "已在监听 $LOCAL（pid=$(find_pid_on_port)）"
        return 0
    fi
    warn "未监听，启动中…"
    ( cd "$BACKEND" && nohup "$PY" -u run.py >> "$BACKEND/logs/backend.log" 2>&1 & ) 
    for _ in $(seq 1 20); do
        sleep 2
        if local_up; then
            ok "已启动（pid=$(find_pid_on_port)）"
            return 0
        fi
    done
    bad "启动后 40s 内 /health 仍不可用，日志尾部："
    tail -12 "$BACKEND/logs/backend.log" | sed 's/^/       /'
    return 1
}

# ---------------------------------------------------------------- L2 公网隧道
current_url() { cat "$URL_FILE" 2>/dev/null | tr -d '\r\n'; }

tunnel_live() {
    local u="$1"
    [ -n "$u" ] || return 1
    [ "$(public_code "$u" /health)" = "200" ]
}

start_tunnel() {
    echo "[L2] 公网隧道（cpolar 临时域名）"
    local u; u="$(current_url)"
    if tunnel_live "$u"; then
        ok "隧道仍有效：$u"
        return 0
    fi
    [ -n "$u" ] && warn "旧地址已失效：$u（cpolar 免费版重启即换域名）"

    # 清掉残留进程，避免两条隧道并存（分不清哪个域名是活的）
    MSYS_NO_PATHCONV=1 tasklist /FI "IMAGENAME eq cpolar.exe" 2>/dev/null \
        | awk '/cpolar\.exe/ {print $2}' | while read -r p; do
            [ -n "$p" ] && MSYS_NO_PATHCONV=1 taskkill /F /PID "$p" >/dev/null 2>&1
        done
    sleep 2

    mkdir -p "$(dirname "$CPOLAR_LOG")"
    : > "$CPOLAR_LOG"
    # `-log stdout` 是必须的：没有它 cpolar 不打印隧道地址，脚本就拿不到域名。
    # 同时清掉代理变量 —— cpolar 走系统代理时会报 502 Bad Gateway。
    ( cd /c/Users/Administrator/cpolar && \
      env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
      nohup "$CPOLAR_EXE" http "$PORT" -region cn_top -log stdout >> "$CPOLAR_LOG" 2>&1 & )

    local url="" i
    for i in $(seq 1 30); do
        sleep 2
        url="$(grep -oE 'https://[a-z0-9]+\.r[0-9]+\.cpolar\.top' "$CPOLAR_LOG" 2>/dev/null | head -1)"
        [ -n "$url" ] && break
    done
    if [ -z "$url" ]; then
        bad "60s 内未取到隧道地址，cpolar 日志尾部："
        tail -12 "$CPOLAR_LOG" | sed 's/^/       /'
        return 1
    fi
    printf '%s' "$url" > "$URL_FILE"
    # 隧道刚建立时后端还要几秒才握手完，这里等一等再判定，避免"刚起来就报失败"
    for i in $(seq 1 8); do
        sleep 3
        if tunnel_live "$url"; then ok "新隧道可用：$url"; return 0; fi
    done
    bad "隧道已建立但公网 /health 不通：$url"
    return 1
}

# ---------------------------------------------------------------- L3 离线兜底
check_offline_fallback() {
    echo "[L3] 离线兜底（GEE 不可用时的演示保底）"
    local cur; cur="$(grep -E '^EXECUTION_BACKEND=' "$BACKEND/.env" 2>/dev/null | cut -d= -f2 | tr -d '\r')"
    echo "       当前 EXECUTION_BACKEND = ${cur:-offline(默认)}"
    if [ "$(local_code /health)" != "200" ]; then
        warn "服务未运行，跳过（L1 先起来才能判断）"
        return 0
    fi
    local g; g="$("$PY" - <<'PY' 2>/dev/null
import os, json, urllib.request
try:
    body = json.dumps({"username": os.getenv("ADMIN_USERNAME", "admin"),
                       "password": os.getenv("ADMIN_PASSWORD", "")}).encode()
    req = urllib.request.Request("http://127.0.0.1:8010/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    tok = json.load(urllib.request.urlopen(req, timeout=8)).get("token", "")
    req = urllib.request.Request("http://127.0.0.1:8010/api/gee/status",
                                 headers={"Authorization": "Bearer " + tok})
    print(json.load(urllib.request.urlopen(req, timeout=15)).get("ok"))
except Exception:
    print("unknown")
PY
)"
    case "$g" in
        True)  ok "GEE 连通正常 —— 演示走真实 Earth Engine，无需兜底" ;;
        False) warn "GEE 当前不通 —— 演示前请把 .env 的 EXECUTION_BACKEND 改成 offline 并重启服务" ;;
        *)     warn "GEE 状态无法判定（多为未登录/超时）—— 演示前人工确认一次" ;;
    esac
}

# ---------------------------------------------------------- L4-L6 深度自检
# L1-L3 只回答"服务/隧道/GEE 通不通"，答不了三件同样会让演示翻车的事：
#   ① LLM 密钥配了但**还能不能用**（/health 只回 has_key，答不了这个）
#   ② 前端产物**是否比源码新**（改了 src 忘了 build → 演示看到旧界面，后端测试还不报错）
#   ③ 库状态（答辩当天可能被人当场翻看：有没有管理员、任务归属对不对）
# 这三项交给 tools/demo_doctor.py（Python 写，避开 shell 的路径坑）。
check_doctor() {
    echo "[L4-L6] 深度自检（LLM 密钥 / 前端产物 / 库状态）"
    if [ ! -f "$BACKEND/tools/demo_doctor.py" ]; then
        warn "找不到 tools/demo_doctor.py，跳过"
        return 0
    fi
    if [ "$SKIP_LLM" = "1" ]; then
        "$PY" "$BACKEND/tools/demo_doctor.py" --no-llm || warn "深度自检有 FAIL 项（见上）"
    else
        "$PY" "$BACKEND/tools/demo_doctor.py" || warn "深度自检有 FAIL 项（见上）"
    fi
}

# ---------------------------------------------------------------- 主流程
echo "============================================================"
echo " 演示环境三层预案自检        $(date '+%F %T')"
echo "============================================================"

# --self-test：只验 demo_doctor 的判定规则，不碰服务/隧道/网络（秒级返回）
if [ "$MODE" = "--self-test" ]; then
    "$PY" "$BACKEND/tools/demo_doctor.py" --self-test
    exit $?
fi

if [ "$MODE" = "--status" ]; then
    echo "[L1] 本地 $(find_pid_on_port) → /health = $(local_code /health)"
    echo "[L2] 落盘地址 $(current_url) → /health = $(public_code "$(current_url)" /health)"
    check_offline_fallback
    echo
    check_doctor
    exit 0
fi

start_local || exit 1
echo
if [ "$MODE" = "--local-only" ]; then
    echo "[L2] 已跳过（--local-only）"
else
    start_tunnel
fi
echo
check_offline_fallback
echo
check_doctor
echo
echo "============================================================"
echo " 演示入口"
echo "   ★ 主路径（本地直连，最稳）  $LOCAL"
[ "$MODE" = "--local-only" ] || echo "     备选（当天证明「外网可访问」用，会断、会换域名）"
[ "$MODE" = "--local-only" ] || echo "                              $(current_url)"
echo "     域名落盘  $URL_FILE"
echo
echo " 判断依据：cpolar 免费隧道 2026-09-17 一天内断了 2 次、域名换了 3 次。"
echo " 答辩现场以 L1 为准；L2 只在需要证明「外网能访问」时展示，不要当演示主路径。"
echo "============================================================"
