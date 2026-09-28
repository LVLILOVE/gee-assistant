#!/usr/bin/env bash
# 前端单测（vitest + jsdom）。作为 regress_all.sh 的一个套件运行。
#
# 【为什么它进**默认**清单（而不是像 --with-a11y 那样 opt-in）】
# 判据是"有没有环境前置条件"：
#   · a11y / http 套件需要真实服务在 :8010 跑着、还要无头浏览器 → 只能 opt-in，
#     否则回归在不该它跑的场合会全红，久了没人看。
#   · 这个套件是**自足的**：只需要 frontend/node_modules（任何前端构建本来就要装），
#     不连网、不连服务、不碰数据库 → 结果确定、可重复。没有理由藏起来不跑。
# 唯一的例外是"依赖没装"（全新 clone / 未 npm install），那就明确跳过并说清楚，
# **绝不允许悄悄变成 0/0 的假绿**。
#
# 【为什么判据要长得像别的套件】
# regress_all.sh 用 `grep -oE '[0-9]+/[0-9]+ *通过'` 取汇总行。
# 第一巡的 T2 就是"汇总行解析不到造成的假红"：零失败也会被报成没全绿，
# 久了人会开始无视红色。所以这里**必须**输出同款格式，不能自创一套。
set -u

cd "$(dirname "$0")/../.." || exit 1
FRONT="frontend"

# ---- 找 node：本机是隔离环境，PATH 里通常没有 ----
# 顺序：显式指定 → 托管版本（偏好优先，再扫目录兜底）→ PATH 里的
# ⚠️ **不要把版本号写成唯一出路**：本仓库在 T1 上吃过这个亏 ——
#    `...\Edge\Application\153.0.4234.48\msedge.exe` 写死在代码里，Edge 升级后该目录
#    变成 `153.0.4234.48.deleting` → 整套无障碍校验**静默停跑**（不是失败，是压根没跑）。
#    这里与 tools/browser_util.py 的 find_edge() 同一路数：偏好写前面，兜底扫目录。
find_node() {
    if [ -n "${FRONT_NODE:-}" ] && [ -x "${FRONT_NODE}" ]; then echo "${FRONT_NODE}"; return 0; fi

    local root="C:/Users/Administrator/.workbuddy/binaries/node/versions"
    local cand
    # ① 已知可用的托管版本（偏好，不是唯一）
    for cand in "$root/22.22.2-2/node.exe" "$root/22.22.2/node.exe"; do
        [ -x "$cand" ] && { echo "$cand"; return 0; }
    done
    # ② 偏好不存在 → 扫目录兜底（跳过升级中的 .deleting，同 find_edge）
    for cand in "$root"/*/node.exe; do
        case "$cand" in *".deleting"*) continue ;; esac
        [ -x "$cand" ] && { echo "$cand"; return 0; }
    done
    # ③ 最后才用 PATH 里的
    if command -v node >/dev/null 2>&1; then command -v node; return 0; fi
    return 1
}

NODE_BIN="$(find_node)" || {
    echo "跳过：找不到 node 可执行文件（可用 FRONT_NODE=<路径> 指定）"
    exit 0
}
export PATH="$(dirname "$NODE_BIN"):$PATH"

if [ ! -x "$FRONT/node_modules/.bin/vitest" ]; then
    echo "跳过：前端测试依赖未安装 —— 在 frontend/ 下执行 npm install 后即可纳入回归"
    exit 0
fi

echo "node：$NODE_BIN"
echo "测试文件：$(ls "$FRONT"/test/*.test.jsx 2>/dev/null | wc -l) 个"
echo

cd "$FRONT" || exit 1
rm -f .test-report.json

# ⚠️ 只跑一次。先前版本为了"拿干净数字"额外又跑了两遍，纯属浪费（每次约 45s）。
# ⚠️ 必须带 `--no-color`：vitest 在**被管道接走**时仍会输出 ANSI 转义，
#    汇总行实际是 `\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m10 passed\x1b[39m` ——
#    于是 `grep -oE 'Tests  [0-9]+ passed'` **永远匹配不到**，计数会静默变成 0/0。
#    实测踩到：反向验证脚本就因为这一条，把"修复前该红 2 条"报成了 0/0。
#    除了加 --no-color，下面还额外剥一遍转义（防将来某个版本忽略该开关）。
out="$(env -u PYTHONPATH ./node_modules/.bin/vitest run --no-color 2>&1)"
rc=$?
printf '%s\n' "$out"

ESC=$(printf '\033')
plain=$(printf '%s' "$out" | sed -e "s/${ESC}\[[0-9;]*m//g")
passed=$(printf '%s' "$plain" | grep -oE 'Tests  [0-9]+ passed' | grep -oE '[0-9]+' | tail -1)
failed=$(printf '%s' "$plain" | grep -oE 'Tests  [0-9]+ failed' | grep -oE '[0-9]+' | tail -1)
passed=${passed:-0}; failed=${failed:-0}

echo
# 收集阶段就挂掉时（例如某个测试文件语法错）会一条 Tests 行都没有。
# 那种情况下绝不能靠 "0/0" 蒙过去 —— 必须显式报错，让 regress_all 判成未通过。
if [ "$((passed + failed))" = "0" ]; then
    echo "❌ 没解析到任何用例计数（vitest 退出码 $rc）—— 大概是测试文件收集失败"
    exit 1
fi

if [ "$failed" = "0" ] && [ "$rc" != "0" ]; then
    echo "❌ 用例全通过但 vitest 退出码为 $rc —— 别当成绿"
    exit 1
fi

echo "结果：${passed}/$((passed + failed)) 通过"
[ "$failed" = "0" ] && exit 0 || exit 1
