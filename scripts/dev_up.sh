#!/usr/bin/env bash
# 本地一键启动：起依赖容器 -> 迁移 -> 消费者与模拟器 -> Web 服务
# 用法：bash scripts/dev_up.sh
# 等价于手开四个终端，调试时建议还是分开跑，日志更清楚

set -eu

PY=${PYTHON:-python}

echo "[1/5] 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "  没找到 docker，请先手动启动 MySQL 与 Redis" >&2
  exit 1
fi
docker compose up -d mysql redis

echo "[2/5] 等待 MySQL 健康检查通过"
for _ in $(seq 1 30); do
  status=$(docker inspect -f '{{.State.Health.Status}}' metro-mysql 2>/dev/null || echo starting)
  [ "$status" = "healthy" ] && break
  sleep 1
done

echo "[3/5] 应用数据库迁移"
"$PY" manage.py migrate --noinput

echo "[4/5] 启动消费者与数据模拟器"
"$PY" stream/consumer.py &
CONSUMER_PID=$!
"$PY" simulator/simulator.py --speed 3 &
PRODUCER_PID=$!

cleanup() {
  kill "$CONSUMER_PID" "$PRODUCER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[5/5] 打开 http://127.0.0.1:8000 ，Ctrl+C 结束全部进程"
"$PY" manage.py runserver 8000
