#!/bin/bash
# ============================================================================
# 一键回归 —— 把「改动后必跑」的套件按固定顺序跑完并汇总
#
# 为什么需要它：本项目的回归套件有 6 个（逻辑层），散在 tools/ 下。
# 手工逐个跑**一定会漏** —— 2026-09-17 就发生过一次：`test_isolation.py` 静默
# 失败成 74/79，而当时没人跑它，直到几小时后才发现。漏跑的最坏结果不是"少测了"，
# 是"以为测过了"。
#
# 用法（在 backend 目录下，或任意目录都行）：
#   bash tools/regress_all.sh                 # 跑 6 个逻辑层套件（默认，约几分钟）
#   bash tools/regress_all.sh --only isolation   # 只跑名字含 isolation 的
#   bash tools/regress_all.sh --with-doctor    # 额外跑 gee_doctor --self-test（沙箱链路）
#   bash tools/regress_all.sh --with-a11y      # 额外跑无障碍套件（需 :8010 在跑）
#   bash tools/regress_all.sh --http           # ⚠️ 额外跑 3 个 HTTP 套件（很慢，见下）
#   bash tools/regress_all.sh --help
#
# ⚠️ 关于 --with-a11y：需要后端实例在 :8010 跑着（脚本会先探活，没起就跳过）。
#    它用无头 Edge 登录页面，实测对比度与焦点可见性。改过 UI/配色后**必跑**。
#
# ⚠️ 关于 --http：3 个 HTTP 套件会**自起临时实例**（:8014 / :8015 等）并自造临时库，
#    不碰线上 data/app.db，所以是安全的；但它们**很慢** ——
#    `test_quota_http.sh` 实测要 **1 小时 18 分钟**（大部分时间在等限流窗口滑动）。
#    **答辩当天不要临时起意念加这个参数。**
#
# 安全性说明（这一点必须清楚）：
#   · 6 个逻辑层套件都跑在**临时库或线上库的副本**上，不写线上 data/app.db；
#     `test_isolation.py` 会**读取**线上库并复制一份出来操作（不是原地改）。
#   · 要碰线上只能跑 `tools/smoke_live.sh`（纯只读），本脚本不会碰。
# ============================================================================
set -u

cd "$(dirname "$0")/.." || { echo "无法进入 backend 目录"; exit 1; }

PY=".venv/Scripts/python.exe"
LOG_DIR="../.cache"
mkdir -p "$LOG_DIR"

# 每个套件单独限时，避免某个套件挂住把整轮拖死（本机确实出现过脚本假死）。
PER_TIMEOUT="${REGRESS_TIMEOUT:-900}"

# ---------------------------------------------------------------- 套件清单
SUITES=(
    test_auth.py
    test_isolation.py
    test_field_crypto.py
    test_sandbox_fs.py
    test_sa_route.py
    test_static_route.py
    test_no_stale_year.py
    test_task_echo.py
)
# demo_doctor 是纯规则自检（不碰服务/隧道/网络、秒级），所以放在默认清单里；
# gee_doctor 要装 ee 依赖、稍慢，放在 --with-doctor 后面。
DOCTOR_SUITES=(
    demo_doctor.py
)
# 无障碍套件要**真服务在跑**（它们用无头浏览器登录线上实例取真实计算样式）。
# 所以不能进默认清单 —— 回归可能在服务没起时跑，那样会全红，久了就没人看。
# 用 --with-a11y 显式开启，且服务不在跑时给明确提示而不是一堆报错。
A11Y_SUITES=(
    test_a11y_contrast.py
    test_a11y_kbd.py
)
HTTP_SUITES=(
    test_auth_http.sh
    test_isolation_http.sh
    test_quota_http.sh
)

WITH_DOCTOR=0
WITH_HTTP=0
WITH_A11Y=0
ONLY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --with-doctor) WITH_DOCTOR=1 ;;
        --with-a11y)   WITH_A11Y=1 ;;
        --http)        WITH_HTTP=1 ;;
        --only)        shift; ONLY="${1:-}" ;;
        --timeout)     shift; PER_TIMEOUT="${1:-900}" ;;
        --help|-h)     sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "未知参数：$1（--help 看用法）"; exit 2 ;;
    esac
    shift
done

if [ ! -x "$PY" ]; then
    echo "找不到虚拟环境解释器：$PY"
    echo "请确认在 backend 目录下、且 .venv 已创建。"
    exit 1
fi

if [ "$WITH_HTTP" = "1" ]; then
    SUITES+=("${HTTP_SUITES[@]}")
fi

if [ "$WITH_DOCTOR" = "1" ]; then
    SUITES+=("${DOCTOR_SUITES[@]}")
fi

# 无障碍套件需要线上实例在跑。先探活，没起就明确告知并跳过 ——
# 让整轮回归因为"服务没起"而变红，会训练出"红了也照过"的坏习惯。
#
# ⚠️ 探活的写法有坑，别改成 `curl -o /dev/null` + 看退出码：
#    本机（git-bash + 删除钩子）`-o /dev/null` 会返回 **退出码 23**
#    （写 /dev/null 被拦），于是 `if curl ...; then` 恒判失败 ——
#    明明服务健康（HTTP 200、netstat 显示 LISTENING），却被当成"没起"跳过。
#    所以这里**用 -w 取状态码**来判定，不依赖退出码、也不写文件。
#    （这坑很值钱：它会把"我检查过了"变成"我跳过了却以为检查过了"。）
_a11y_probe() {
    code="$(curl -s --noproxy '*' --max-time 5 -w '%{http_code}' \
            -o /dev/null "http://127.0.0.1:8010/health" 2>/dev/null)"
    [ "$code" = "200" ]
}
if [ "$WITH_A11Y" = "1" ]; then
    if _a11y_probe; then
        SUITES+=("${A11Y_SUITES[@]}")
    else
        echo "⚠️  --with-a11y 已指定，但 http://127.0.0.1:8010/health 不通 —— 跳过无障碍套件"
        echo "    （无障碍检查要用无头浏览器登录真实页面取计算样式，必须有实例在跑）"
        echo
        SKIPPED_LIST+=("${A11Y_SUITES[@]}")
    fi
fi

# `--only <子串>` 时，若目标只存在于需要开关才加载的清单里（如 demo_doctor 在
# DOCTOR_SUITES），上面那批没进来，过滤后会变成空清单、跑 0 个套件却报"0 失败"——
# 典型的假绿。这里显式把命中的套件补进清单，让 --only 始终能定位到目标。
# ---------------------------------------------------------------------------
# 无障碍套件（test_a11y_*.py）的特殊之处，改它们前先读：
#   · 需要在跑的服务：用无头 Edge 登录 :8010，从**浏览器计算样式**里读颜色，
#     再按 WCAG 公式算对比度 —— 因为手算会偏乐观（本项目的 ink-500 就是
#     手算以为 4.6:1、实测只有 4.24:1）。
#   · 焦点可见性用**截图逐像素比对**，不是读 outline 属性。两个原因：
#     ① antd 用 cssinjs 运行时注入 <style>，优先级在构建产物里查不到；
#     ② 按钮的焦点环其实是 box-shadow，outline 天生是 none，按属性判会误判。
#   · 排查这类脚本时最容易踩的两个坑（都已写进脚本注释）：
#     ✔ `document.body.focus()` **不能**清除焦点（body 不可聚焦，是个 no-op），
#       参照图里焦点环还在 → 差异恒为 0 → 把有环的元素全误报成无环。
#       必须用 `document.activeElement.blur()`。
#     ✔ 截图坐标要乘 devicePixelRatio（本机 125% 缩放），否则裁剪框错位。
# ---------------------------------------------------------------------------
if [ -n "$ONLY" ]; then
    for cand in "${DOCTOR_SUITES[@]}" "${HTTP_SUITES[@]}" "${A11Y_SUITES[@]}"; do
        case "$cand" in *"$ONLY"*)
            already=0
            for s in "${SUITES[@]}"; do [ "$s" = "$cand" ] && already=1; done
            [ "$already" = "0" ] && SUITES+=("$cand")
            ;;
        esac
    done
fi

echo "============================================================"
echo " 回归测试  $(date '+%F %T')"
echo " 套件数 ${#SUITES[@]}   单套件限时 ${PER_TIMEOUT}s"
[ "$WITH_HTTP" = "1" ] && echo " ⚠️ 含 HTTP 套件：本轮预计要 1 小时以上"
echo "============================================================"
echo

TOTAL_OK=0
TOTAL_ALL=0
FAILED_LIST=()
SKIPPED_LIST=()
START_ALL=$SECONDS

for f in "${SUITES[@]}"; do
    if [ -n "$ONLY" ]; then
        case "$f" in *"$ONLY"*) ;; *) continue ;; esac
    fi
    if [ ! -f "tools/$f" ]; then
        echo "── $f"
        echo "   ⚠️ 文件不存在，跳过"
        SKIPPED_LIST+=("$f")
        echo
        continue
    fi

    log="$LOG_DIR/regress_${f%.*}.log"
    echo "── $f"
    t0=$SECONDS

    case "$f" in
        *.sh) timeout "$PER_TIMEOUT" bash "tools/$f" > "$log" 2>&1 ;;
        demo_doctor.py)
            # 这个脚本默认做「真环境体检」（要服务在跑、会发网络请求），
            # 回归里要的是它的**判定规则自检**，必须显式带 --self-test。
            timeout "$PER_TIMEOUT" "$PY" "tools/$f" --self-test > "$log" 2>&1 ;;
        *)    timeout "$PER_TIMEOUT" "$PY" "tools/$f" > "$log" 2>&1 ;;
    esac
    rc=$?
    elapsed=$((SECONDS - t0))

    # 各套件统一以「N/M 通过」收尾（有 === 包裹的也在同一行内）
    summary="$(grep -oE '[0-9]+/[0-9]+ *通过' "$log" 2>/dev/null | tail -1)"

    if [ "$rc" = "124" ]; then
        echo "   ❌ 超时被杀（>${PER_TIMEOUT}s）—— 日志尾部："
        tail -5 "$log" | sed 's/^/      /'
        FAILED_LIST+=("$f")
    elif [ -z "$summary" ]; then
        echo "   ❌ 没解析到结果行（退出码 $rc）—— 日志尾部："
        tail -5 "$log" | sed 's/^/      /'
        FAILED_LIST+=("$f")
    else
        n="${summary%%/*}"
        rest="${summary#*/}"
        m="${rest%% *}"
        TOTAL_OK=$((TOTAL_OK + n))
        TOTAL_ALL=$((TOTAL_ALL + m))
        if [ "$n" = "$m" ]; then
            echo "   ✅ ${n}/${m} 通过            ${elapsed}s"
        else
            echo "   ❌ ${n}/${m} 通过（失败 $((m - n)) 项）  ${elapsed}s"
            echo "      失败明细："
            grep -E 'FAIL' "$log" | head -10 | sed 's/^/      /'
            FAILED_LIST+=("$f")
        fi
    fi
    echo "      日志：$log"
    echo
done

if [ "$WITH_DOCTOR" = "1" ]; then
    echo "── gee_doctor.py --self-test（沙箱链路，mock ee 无需凭据）"
    log="$LOG_DIR/regress_gee_doctor.log"
    t0=$SECONDS
    timeout "$PER_TIMEOUT" "$PY" tools/gee_doctor.py --self-test > "$log" 2>&1
    rc=$?
    elapsed=$((SECONDS - t0))
    # ⚠️ 这个工具**不用「N/M 通过」格式**，而是逐条打 `[ OK ]` / `[FAIL]` / `[SKIP]`，
    #    而且**失败时也不返回非零退出码** —— 所以退出码不能拿来判定，必须数标记。
    #    （第一版就是漏了这点，把满屏 [ OK ] 判成"未通过"。）
    # ⚠️ 这里**不能**写 `grep -c ... || echo 0` ——
    #    `grep -c` 在没有匹配时会**先输出一个 0、再返回非零退出码**，
    #    于是 `|| echo 0` 又补一个 0，变量变成 `"0\n0"`，参与算术运算就报
    #    `arithmetic syntax error (error token is "0")`。
    #    **计数一律用 `grep -c` 的 stdout，不要用退出码兜底。**
    dok=$(grep -c -F '[ OK ]' "$log" 2>/dev/null)
    dfail=$(grep -c -F '[FAIL]' "$log" 2>/dev/null)
    dskip=$(grep -c -F '[SKIP]' "$log" 2>/dev/null)
    [ -n "$dok" ]   || dok=0
    [ -n "$dfail" ] || dfail=0
    [ -n "$dskip" ] || dskip=0
    dtot=$((dok + dfail))
    if [ "$rc" = "124" ]; then
        echo "   ❌ 超时被杀（>${PER_TIMEOUT}s）"
        FAILED_LIST+=("gee_doctor --self-test")
    elif [ "$dtot" = "0" ]; then
        echo "   ❌ 没解析到任何 [ OK ]/[FAIL] 标记（退出码 $rc）—— 日志尾部："
        tail -8 "$log" | sed 's/^/      /'
        FAILED_LIST+=("gee_doctor --self-test")
    else
        TOTAL_OK=$((TOTAL_OK + dok)); TOTAL_ALL=$((TOTAL_ALL + dtot))
        if [ "$dfail" = "0" ]; then
            echo "   ✅ ${dok}/${dtot} 通过$([ "$dskip" != "0" ] && echo "（跳过 ${dskip} 项）")   ${elapsed}s"
        else
            echo "   ❌ ${dok}/${dtot} 通过（失败 ${dfail} 项）  ${elapsed}s"
            grep -F '[FAIL]' "$log" | head -10 | sed 's/^/      /'
            FAILED_LIST+=("gee_doctor --self-test")
        fi
    fi
    echo "      日志：$log"
    echo
fi

TOTAL_ELAPSED=$((SECONDS - START_ALL))

echo "============================================================"
if [ "${#FAILED_LIST[@]}" = "0" ]; then
    echo " 结果：${TOTAL_OK}/${TOTAL_ALL} 通过，0 失败"
else
    echo " 结果：${TOTAL_OK}/${TOTAL_ALL} 通过，${#FAILED_LIST[@]} 个套件没全绿"
    echo " 未通过："
    for x in "${FAILED_LIST[@]}"; do echo "   · $x"; done
fi
[ "${#SKIPPED_LIST[@]}" -gt 0 ] && echo " 已跳过：${SKIPPED_LIST[*]}"
echo " 总耗时：${TOTAL_ELAPSED}s（$(date '+%F %T')）"
echo " 日志目录：$LOG_DIR/regress_*.log"
echo "============================================================"

[ "${#FAILED_LIST[@]}" = "0" ] || exit 1
exit 0
