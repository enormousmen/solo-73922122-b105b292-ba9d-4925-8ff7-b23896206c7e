#!/usr/bin/env bash
# 故障注入演示：搬移报错 / 截短报错后数据仍可读，再续跑完成。
# 需要先启动网页：python -m app.web （或 docker compose up）
set -euo pipefail
BASE_URL="${1:-http://127.0.0.1:8000}"

echo '== 注入“下一次搬移报错”并启动整理 =='
curl -fsS -X POST "$BASE_URL/api/fault/move" >/dev/null
curl -fsS -X POST "$BASE_URL/api/compact/start" >/dev/null
for _ in $(seq 1 30); do
  state=$(curl -fsS "$BASE_URL/api/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$state" = 'error' ] && break
  sleep 0.5
done
echo "状态: $state（应为 error）"
echo '== 错误态下读盘仍成功 =='
curl -fsS "$BASE_URL/api/blocks/0" | python3 -c 'import json,sys; r=json.load(sys.stdin); print("lbn", r["lbn"], "可读,", r["size"], "字节")'

echo '== 续跑至完成 =='
curl -fsS -X POST "$BASE_URL/api/compact/start" >/dev/null
for _ in $(seq 1 60); do
  state=$(curl -fsS "$BASE_URL/api/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$state" = 'idle' ] && break
  sleep 0.5
done
curl -fsS "$BASE_URL/api/status" | python3 -c '
import json, sys
s = json.load(sys.stdin)
assert s["state"] == "idle"
assert s["file_size"] == 8192 + 14 * 4096
print("OK 续作完成，file_size=%s errors=%s" % (s["file_size"], s["errors"]))
'
