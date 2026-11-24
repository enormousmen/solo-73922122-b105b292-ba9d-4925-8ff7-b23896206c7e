#!/usr/bin/env bash
# 无 Docker 环境下的“中断续作”演示（纯 Python，任何机器可跑）。
# 用 os._exit 模拟 SIGKILL：不执行 finally、不刷新缓冲、不关 fd，
# 恢复只能依赖已 fsync 的盘上双份头。
#
# 用法：bash scripts/demo_local.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY=python3
IMG="$(pwd)/data/demo_local.img"
mkdir -p data

echo '== 0. 重建演示镜像 =='
$PY -m app.cli seed "$IMG"

echo
echo '== 1. 第一次中断：推进一步后硬退出 =='
$PY -m app.cli hard-exit "$IMG" || true

echo
echo '== 2. 中断后立刻读盘：状态 compacting，14 个块全部可读且内容正确 =='
$PY -m app.cli verify "$IMG"

echo
echo '== 3. 第二次中断（验证可多次中断、多次续作） =='
$PY -m app.cli hard-exit "$IMG" || true
$PY -m app.cli status "$IMG" | python3 -c 'import json,sys; s=json.load(sys.stdin); print("state=%s cursor=%s file_size=%s" % (s["state"], s["cursor"], s["file_size"]))'

echo
echo '== 4. 重启续作直到完成 =='
$PY -m app.cli resume "$IMG"

echo
echo '== 5. 终态校验 =='
$PY -m app.cli verify "$IMG"
$PY -m app.cli status "$IMG" | python3 -c '
import json, sys
s = json.load(sys.stdin)
expect = 8192 + 14 * 4096
assert s["state"] == "idle"
assert s["file_size"] == expect, (s["file_size"], expect)
assert s["used_lbns"] == list(range(14))
print("OK 终态文件长度 %s 字节，逻辑块 0..13 映射与内容保持原样" % expect)
'
