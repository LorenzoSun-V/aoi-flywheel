#!/usr/bin/env bash
# 在线备份，不停服务：先导出数据库（一个一致的快照），再把文件库里新增的文件拷到备份目录。
#
#   sudo bash deploy/backup.sh /backup/flywheel
#   每天 2 点（/etc/cron.d/aoi-flywheel，见 docs/commands.md）：0 2 * * * root bash /opt/aoi-flywheel/deploy/backup.sh /backup/flywheel >> /var/log/flywheel-backup.log 2>&1
#
# 顺序不能反：数据库只在文件落盘之后才提交引用它们的记录，所以先导出的数据库里引用的文件，
# 这时都已经在文件库里，后面的 rsync 一定会拷到。文件写进去以后不再改，所以只拷新文件。
# 第一步不做清理（垃圾回收）；以后加清理时，备份期间要暂停清理。
set -euo pipefail
cd "$(dirname "$0")"
set -a
. ./.env
set +a

DEST=${1:?用法：bash deploy/backup.sh <备份目录>}
KEEP=${FW_BACKUP_KEEP:-14}   # 保留最近几份数据库导出
mkdir -p "$DEST/db" "$DEST/store/objects"
start=$(date +%s)
stamp=$(date +%Y%m%d-%H%M%S)
dump="$DEST/db/fw-$stamp.dump"

docker compose exec -T db pg_dump -U fw -d fw -Fc > "$dump.part"
docker compose exec -T db pg_restore --list < "$dump.part" > /dev/null   # 导出的文件要能读
mv "$dump.part" "$dump"

rsync -a --ignore-existing "$FW_STORE_DIR/objects/" "$DEST/store/objects/"

ls -1t "$DEST"/db/fw-*.dump | tail -n +$((KEEP + 1)) | xargs -r rm --
basename "$dump" > "$DEST/LATEST"
echo "$(date '+%F %T') 备份完成：$dump，文件库已同步到 $DEST/store（用时 $(( $(date +%s) - start )) 秒）"
