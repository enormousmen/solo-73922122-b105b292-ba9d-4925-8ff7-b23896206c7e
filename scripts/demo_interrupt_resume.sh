#!/usr/bin/env bash
# 可复现的“中断续作”演示（Docker Compose 版，使用真实 SIGKILL）。
#
# 流程：
#   1. 构建并启动网页容器（镜像在命名卷 disk-data 中，容器重启不丢状态）
#   2. 启动整理，随即 docker kill -9 强杀容器
#   3. 重新启动容器：网页自动识别 compacting 态并续作
#   4. 轮询状态直到 idle，校验文件长度恰好 = 8192 + 14*4096 = 65536
#   5. 校验全部 14 个逻辑块内容与种子逐字节一致
#
# 用法：bash scripts/demo_interrupt_resume.sh
set -euo pipefail

cd "$(dirname "$0")/.."
IMG_REF='disk-compactor:demo'
BASE_URL='http://127.0.0.1:8000'

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1" >&2; exit 1; }; }
need docker

if docker compose version >/dev/null 2>&1; then
  DC='docker compose'
else
  need docker-compose
  DC='docker-compose'
fi

echo '== 0. 清理旧环境并构建 =='
$DC down -v >/dev/null 2>&1 || true
$DC up -d --build
echo '等待网页就绪...'
for _ in $(seq 1 30); do
  curl -fsS "$BASE_URL/api/status" >/dev/null 2>&1 && break
  sleep 1
done

echo
echo '== 1. 初始状态（尾部空槽仍撑着文件） =='
curl -fsS "$BASE_URL/api/status" | python3 -c \
  'import json,sys; s=json.load(sys.stdin); print("state=%s file_size=%s 目标=%s 在用块=%s" % (s["state"], s["file_size"], s["expected_compact_size"], s["used_count"]))'

echo
echo '== 2. 启动整理并立即强杀容器（SIGKILL） =='
curl -fsS -X POST "$BASE_URL/api/compact/start" >/dev/null
sleep 1
docker kill --signal=KILL disk-compactor
echo '容器已被 SIGKILL，镜像文件保留在卷中。'

echo
echo '== 3. 重启容器，自动从持久化游标续作 =='
$DC up -d
for _ in $(seq 1 30); do
  curl -fsS "$BASE_URL/api/status" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$BASE_URL/api/status" | python3 -c \
  'import json,sys; s=json.load(sys.stdin); print("重启后 state=%s cursor=%s moves=%s" % (s["state"], s["cursor"], s["moves"]))'

echo
echo '== 4. 等待整理完成 =='
for _ in $(seq 1 60); do
  state=$(curl -fsS "$BASE_URL/api/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$state" = 'idle' ] && break
  sleep 1
done

echo
echo '== 5. 核对文件长度与各逻辑块内容 =='
curl -fsS "$BASE_URL/api/status" | python3 -c '
import json, sys
s = json.load(sys.stdin)
assert s["state"] == "idle", s
expect = 8192 + 14 * 4096
assert s["file_size"] == expect, (s["file_size"], expect)
print("OK 文件长度恰好 %s 字节，仅容纳 2 个头部块 + 14 个在用块" % expect)
'
ok=1
for lbn in $(seq 0 13); do
  curl -fsS "$BASE_URL/api/blocks/$lbn" | python3 -c '
import json, sys
sys.path.insert(0, ".")
from app.seed import block_content
r = json.load(sys.stdin)
got = bytes.fromhex(r["hex"])
assert got == block_content(r["lbn"])[:64], r["lbn"]
print("lbn %2s 读盘成功，前64字节与种子一致" % r["lbn"])
'
done
echo
echo '全部校验通过。网页地址：'"$BASE_URL"
