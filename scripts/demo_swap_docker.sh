#!/usr/bin/env bash
# 可复现的“整理途中对调逻辑地址”演示（Docker Compose 版）。
#
# 场景：业务区 [0,3) 的数据要切换为备用区 [11,14) 里备好的数据。
#   1. 构建并启动网页容器（镜像在命名卷 disk-data 中，重启不丢状态）
#   2. 启动整理（MOVE_DELAY=0.5 放慢搬移），在整理途中提交对调
#   3. 整理按新的逻辑块号顺序继续压紧并截短
#   4. 校验：文件长度恰好 65536，读块按新映射取数
#   5. 重启容器：已确认成功的对调仍有效
#   6. 同一参数再次对调切回，重新整理后还原
#
# 用法：bash scripts/demo_swap_docker.sh
set -euo pipefail

cd "$(dirname "$0")/.."
BASE_URL='http://127.0.0.1:8000'

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1" >&2; exit 1; }; }
need docker
need curl

if docker compose version >/dev/null 2>&1; then
  DC='docker compose'
else
  need docker-compose
  DC='docker-compose'
fi

wait_ready() {
  for _ in $(seq 1 30); do
    curl -fsS "$BASE_URL/api/status" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo '网页未就绪' >&2; exit 1
}

wait_state() { # $1=期望状态
  for _ in $(seq 1 90); do
    state=$(curl -fsS "$BASE_URL/api/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
    [ "$state" = "$1" ] && return 0
    sleep 0.5
  done
  echo "状态未到达 $1（最后 $state）" >&2; exit 1
}

check_block() { # $1=lbn $2=内容来源（原逻辑块号）
  curl -fsS "$BASE_URL/api/blocks/$1" | python3 -c '
import json, sys
sys.path.insert(0, ".")
from app.seed import block_content
r = json.load(sys.stdin)
expect = block_content(int(sys.argv[2]))[:64]
assert bytes.fromhex(r["hex"]) == expect, (r["lbn"], sys.argv[2])
print("lbn %2s 读盘成功，内容为原 lbn %s 的块" % (r["lbn"], sys.argv[2]))
' "$1" "$2"
}

echo '== 0. 清理旧环境并构建启动 =='
$DC down -v >/dev/null 2>&1 || true
$DC up -d --build
wait_ready
curl -fsS "$BASE_URL/api/status" | python3 -c \
  'import json,sys; s=json.load(sys.stdin); print("初始 state=%s file_size=%s 在用块=%s" % (s["state"], s["file_size"], s["used_count"]))'

echo
echo '== 1. 启动整理，并在整理途中对调业务区 [0,3) ⇄ 备用区 [11,14) =='
curl -fsS -X POST "$BASE_URL/api/compact/start" >/dev/null
sleep 2   # MOVE_DELAY=0.5，此时整理正在搬移途中
curl -fsS -X POST "$BASE_URL/api/swap" \
  -H 'Content-Type: application/json' \
  -d '{"a": 0, "b": 11, "count": 3}' | python3 -c \
  'import json,sys; r=json.load(sys.stdin); print("对调响应:", r["message"])'

echo
echo '== 2. 等待整理按新映射压紧并截短 =='
wait_state idle
curl -fsS "$BASE_URL/api/status" | python3 -c '
import json, sys
s = json.load(sys.stdin)
expect = 8192 + 14 * 4096
assert s["state"] == "idle", s
assert s["file_size"] == expect, (s["file_size"], expect)
assert s["slots"][:14] == [i + 1 for i in range(14)], s["slots"]
print("OK 文件长度 %s 字节，前 14 槽按新逻辑块号顺序压紧" % expect)
'

echo
echo '== 3. 读块按新映射取数：业务地址读到备好的数据 =='
check_block 0 11
check_block 1 12
check_block 2 13
check_block 11 0
check_block 12 1
check_block 13 2
check_block 5 5

echo
echo '== 4. 重启容器：已确认成功的对调仍有效 =='
$DC restart >/dev/null
wait_ready
check_block 0 11
check_block 11 0
curl -fsS "$BASE_URL/api/status" | python3 -c \
  'import json,sys; s=json.load(sys.stdin); print("重启后 state=%s file_size=%s" % (s["state"], s["file_size"]))'

echo
echo '== 5. 同一参数再次对调切回，并重新整理 =='
curl -fsS -X POST "$BASE_URL/api/swap" \
  -H 'Content-Type: application/json' \
  -d '{"a": 0, "b": 11, "count": 3}' >/dev/null
curl -fsS -X POST "$BASE_URL/api/compact/start" >/dev/null
wait_state idle
check_block 0 0
check_block 11 11
curl -fsS "$BASE_URL/api/status" | python3 -c '
import json, sys
s = json.load(sys.stdin)
assert s["file_size"] == 8192 + 14 * 4096
assert s["slots"][:14] == [i + 1 for i in range(14)]
print("OK 切回后重新整理完成，布局与内容还原")
'

echo
echo '全部校验通过。网页地址：'"$BASE_URL"
