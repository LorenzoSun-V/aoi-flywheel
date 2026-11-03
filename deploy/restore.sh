#!/usr/bin/env bash
# 从备份恢复：拷回文件库，把数据库换成备份里的那份（原来的数据库改名保留），再核对一遍。
#
#   sudo bash deploy/restore.sh /backup/flywheel                 # 用最新的一份
#   sudo bash deploy/restore.sh /backup/flywheel fw-20261103-020000.dump
#
# 在新机器上恢复：先 sudo bash deploy/setup.sh（不用建管理员，账号和凭据都在备份里），再运行本脚本。
# 恢复期间平台服务会停一会儿；数据库失败时自动换回原来的数据库。
set -euo pipefail
cd "$(dirname "$0")"
set -a
. ./.env
set +a

SRC=${1:?用法：bash deploy/restore.sh <备份目录> [数据库导出文件名]}
DUMP=${2:-$(cat "$SRC/LATEST")}
[ -f "$SRC/db/$DUMP" ] || { echo "找不到 $SRC/db/$DUMP"; exit 1; }
start=$(date +%s)
old="fw_before_restore_$(date +%Y%m%d%H%M%S)"
sql() { docker compose exec -T db psql -U fw -d postgres -v ON_ERROR_STOP=1 -q "$@"; }

docker compose stop server
rsync -a --ignore-existing "$SRC/store/objects/" "$FW_STORE_DIR/objects/"
chown 10001:10001 "$FW_STORE_DIR" "$FW_STORE_DIR/objects"

sql -c "ALTER DATABASE fw RENAME TO $old"
sql -c "CREATE DATABASE fw"
if ! docker compose exec -T db pg_restore -U fw -d fw --no-owner --exit-on-error < "$SRC/db/$DUMP"; then
  echo "数据库恢复失败，换回原来的数据库"
  sql -c "DROP DATABASE fw"
  sql -c "ALTER DATABASE $old RENAME TO fw"
  docker compose start server
  exit 1
fi
docker compose start server

for i in $(seq 1 60); do
  docker compose exec -T server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5)" >/dev/null 2>&1 && break
  sleep 2
done
docker compose exec -T server fw-server verify
echo "恢复完成，用时 $(( $(date +%s) - start )) 秒。原来的数据库保留为 $old，确认无误后可以删："
echo "  docker compose -f deploy/docker-compose.yml exec db dropdb -U fw $old"
