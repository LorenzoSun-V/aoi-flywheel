#!/usr/bin/env bash
# 从备份恢复：把数据库换成备份里的那份（原来的数据库改名保留），再按这份数据库核对文件库：
# 缺的和坏了的文件从备份补回，每个文件都从盘上读出来重算哈希。全部通过才重新对外服务；
# 中间任何一步失败（包括核对没通过），都换回原来的数据库，服务在原来的数据上继续运行。
#
#   sudo bash deploy/restore.sh /backup/flywheel                 # 用最新的一份
#   sudo bash deploy/restore.sh /backup/flywheel fw-20261103-020000.dump
#
# 在新机器上恢复：先 sudo bash deploy/setup.sh（不用建管理员，账号和凭据都在备份里），再运行本脚本。
# 恢复期间平台服务停着。核对要把文件库里每个文件读一遍，用时和文件库大小成正比（见 README）。
set -euo pipefail
cd "$(dirname "$0")"
set -a
. ./.env
set +a

SRC=$(cd "${1:?用法：bash deploy/restore.sh <备份目录> [数据库导出文件名]}" && pwd)
DUMP=${2:-$(cat "$SRC/LATEST")}
[ -f "$SRC/db/$DUMP" ] || { echo "找不到 $SRC/db/$DUMP"; exit 1; }
[ -d "$SRC/store/objects" ] || { echo "找不到 $SRC/store/objects"; exit 1; }
start=$(date +%s)
old="fw_before_restore_$(date +%Y%m%d%H%M%S)"
sql() { docker compose exec -T db psql -U fw -d postgres -v ON_ERROR_STOP=1 -q "$@"; }
healthy() {
  for i in $(seq 1 60); do
    if docker compose exec -T server python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

swapped=0   # the database called fw is the restored one, the original is $old
finished=0
finish() {
  status=$?
  if [ "$finished" = 1 ]; then exit "$status"; fi
  set +e
  echo
  if [ "$swapped" = 1 ]; then
    echo "恢复没有完成，换回原来的数据库"
    if ! { sql -c "DROP DATABASE IF EXISTS fw WITH (FORCE)" && sql -c "ALTER DATABASE $old RENAME TO fw"; }; then
      echo "换回失败：原来的数据库还在，名字是 $old。手动改回 fw 后再启动服务："
      echo "  docker compose -f deploy/docker-compose.yml exec db psql -U fw -d postgres -c 'ALTER DATABASE $old RENAME TO fw'"
      exit 1
    fi
  fi
  docker compose restart server
  if healthy; then
    echo "恢复失败，平台服务在原来的数据上继续运行；原因见上面的输出"
  else
    echo "恢复失败，平台服务也没有起来，看日志：docker compose -f deploy/docker-compose.yml logs server"
  fi
  exit 1
}

docker compose stop server
trap finish EXIT

sql -c "ALTER DATABASE fw RENAME TO $old"
swapped=1
sql -c "CREATE DATABASE fw"
docker compose exec -T db pg_restore -U fw -d fw --no-owner --exit-on-error < "$SRC/db/$DUMP"

# 服务停着，这个一次性容器不开端口：核对通过之前，没有人能读写恢复出来的数据。
# --repair-from 从备份补回缺的和坏了的文件（只拷这份数据库用到的），--rehash 把每个文件从盘上读出来核对，
# 通过后先让补回的文件落盘，再标记文件库可写。
docker compose run --rm --no-deps -T -v "$SRC/store:/backup:ro" server \
  sh -c 'fw-server migrate && fw-server verify --rehash --repair-from /backup'

docker compose start server
healthy || { echo "服务在恢复出来的数据库上起不来"; exit 1; }
finished=1
echo "恢复完成，用时 $(( $(date +%s) - start )) 秒。原来的数据库保留为 $old，确认无误后可以删："
echo "  docker compose -f deploy/docker-compose.yml exec db dropdb -U fw $old"
