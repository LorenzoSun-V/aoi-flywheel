#!/usr/bin/env bash
# 首次部署：生成 deploy/.env（含随机数据库密码）、建目录、构建并启动两个容器。
# 写了账号名就再建一个管理员账号并发它的第一个凭据（只显示一次）。
#
#   sudo bash deploy/setup.sh lorenzo
#
# 目录默认：文件库 /data/flywheel/store（机械盘），数据库 /opt/flywheel/pg（固态盘）。
# 要改就在第一次运行前设环境变量 FW_STORE_DIR、FW_DB_DIR、FW_PORT，或者事后改 deploy/.env。
# 服务器不能联网时先 docker load 两个镜像，再 FW_SKIP_BUILD=1 运行本脚本（见 README）。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  password=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
  (umask 077 && cat > .env <<ENV
FW_DB_PASSWORD=$password
FW_STORE_DIR=${FW_STORE_DIR:-/data/flywheel/store}
FW_DB_DIR=${FW_DB_DIR:-/opt/flywheel/pg}
FW_PORT=${FW_PORT:-8000}
FW_WORKERS=${FW_WORKERS:-4}
ENV
  )
  echo "已生成 deploy/.env（里面有数据库密码，只有 root 能读）"
fi
set -a
. ./.env
set +a

mkdir -p "$FW_STORE_DIR" "$FW_DB_DIR"
chown 10001:10001 "$FW_STORE_DIR"   # 服务容器里的 fw 用户

if [ "${FW_SKIP_BUILD:-0}" = 1 ]; then
  docker compose up -d --no-build   # 用已经 docker load 进来的镜像（服务器不能联网时，见 README）
else
  docker compose up -d --build
fi

printf '等服务启动'
for i in $(seq 1 90); do
  if docker compose exec -T server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5)" >/dev/null 2>&1; then
    echo ' 好了'
    break
  fi
  if [ "$i" = 90 ]; then
    echo
    echo '服务没有起来，看日志：docker compose -f deploy/docker-compose.yml logs server'
    exit 1
  fi
  printf '.'
  sleep 2
done

if [ $# -ge 1 ]; then
  docker compose exec -T server fw-server user add "$1" --role admin
  docker compose exec -T server fw-server token create "$1" --label setup
fi
echo "平台地址：http://$(hostname -I 2>/dev/null | awk '{print $1}'):$FW_PORT"
