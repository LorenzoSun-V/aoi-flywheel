#!/usr/bin/env bash
# 部署演练：在一台机器上用两套部署走一遍“部署 → 导入 → 导入期间在线备份 → 换一台机器恢复 → 取回核对”。
#
#   sudo bash tools/deploy-drill.sh /data/tmp/fw-drill          # 工作目录要不存在或为空
#   sudo bash tools/deploy-drill.sh /data/tmp/fw-drill --keep   # 跑完保留两套部署，方便自己再看
#
# 需要：Docker（带 compose）、rsync、python3，以及装好的 fw（pip install ./client）。
# sudo 下找不到 fw 时：sudo FW_BIN=$(command -v fw) bash tools/deploy-drill.sh ...
# 占用端口 18000、18001，compose 项目名 fwdrilla、fwdrillb；不碰正式部署（/data/flywheel、/opt/flywheel、端口 8000）。
# fw 的登录信息和缓存放在工作目录里，不影响本机已有的 fw login。
# 服务器不能联网时先 docker load 镜像，再加 FW_SKIP_BUILD=1 运行。
set -euo pipefail

W=${1:?用法：sudo bash tools/deploy-drill.sh <工作目录> [--keep]}
KEEP=${2:-}
REPO=$(cd "$(dirname "$0")/.." && pwd)
FW=${FW_BIN:-fw}
for c in docker rsync python3 "$FW"; do
  command -v "$c" >/dev/null || { echo "缺少 $c"; exit 1; }
done
if [ -e "$W" ] && [ -n "$(ls -A "$W")" ]; then echo "$W 不是空目录"; exit 1; fi
mkdir -p "$W"
W=$(cd "$W" && pwd)
export FW_CONFIG=$W/fwconfig.json FW_CACHE=$W/fwcache
step() { echo; echo "== $*"; }

down() {
  for n in a b; do
    (cd "$W/$n/deploy" 2>/dev/null && COMPOSE_PROJECT_NAME=fwdrill$n docker compose down >/dev/null 2>&1) || true
  done
  rm -rf "$W"
}
finish() {
  status=$?
  if [ $status -ne 0 ]; then
    echo
    echo "演练失败（退出码 $status）。$W 和两套部署都留着方便查看，看完后清理："
    echo "  for n in a b; do (cd $W/\$n/deploy && COMPOSE_PROJECT_NAME=fwdrill\$n docker compose down); done; rm -rf $W"
  elif [ "$KEEP" = --keep ]; then
    echo "两套部署留着：A http://127.0.0.1:18000，B http://127.0.0.1:18001（FW_CONFIG=$FW_CONFIG）"
  else
    down
  fi
}
trap finish EXIT

step '准备：两份仓库副本，和两个版本的示例数据（v1 3000 对；v2 改 30 个标注、加 30 对、删 30 对、改名 20 对、复制 20 对）'
for n in a b; do   # 部署只用到这三个目录（镜像从 client、server 构建）
  mkdir -p "$W/$n"
  rsync -a --exclude .env --exclude '*.egg-info' --exclude __pycache__ "$REPO/client" "$REPO/server" "$REPO/deploy" "$W/$n/"
done
python3 - "$W" <<'PY'
import json, random, shutil, sys
from pathlib import Path
w = Path(sys.argv[1])
rng = random.Random(1)
def pair(d, stem):
    d.mkdir(parents=True, exist_ok=True)
    n = rng.randint(2000, 60000)
    (d / f'{stem}.bmp').write_bytes(rng.getrandbits(8 * n).to_bytes(n, 'little'))
    (d / f'{stem}.json').write_text(json.dumps({'shapes': [rng.randint(0, 9)], 'imagePath': f'{stem}.bmp'}))
v1, v2 = w / 'v1', w / 'v2'
for i in range(3000):
    pair(v1 / f'chip{i % 20:02d}', f'img_{i:05d}')
shutil.copytree(v1, v2)
p = lambda i, ext: v2 / f'chip{i % 20:02d}' / f'img_{i:05d}.{ext}'
ids = rng.sample(range(3000), 100)
for i in ids[:30]:
    p(i, 'json').write_text(json.dumps({'shapes': [99], 'imagePath': p(i, 'bmp').name}))
for i in range(3000, 3030):
    pair(v2 / f'chip{i % 20:02d}', f'img_{i:05d}')
for i in ids[30:60]:
    p(i, 'bmp').unlink(); p(i, 'json').unlink()
for i in ids[60:80]:
    for ext in ('bmp', 'json'):
        p(i, ext).rename(v2 / f'chip{(i + 1) % 20:02d}' / f'moved_{i:05d}.{ext}')
for i in ids[80:100]:
    for ext in ('bmp', 'json'):
        shutil.copy(p(i, ext), v2 / f'chip{(i + 2) % 20:02d}' / f'copy_{i:05d}.{ext}')
PY
echo "v1 $(find "$W/v1" -type f | wc -l) 个文件，v2 $(find "$W/v2" -type f | wc -l) 个文件"

step 'A：首次部署，建管理员和数据集'
cd "$W/a/deploy"
export COMPOSE_PROJECT_NAME=fwdrilla FW_STORE_DIR=$W/a-data/store FW_DB_DIR=$W/a-data/pg FW_PORT=18000
time bash setup.sh drill-admin > "$W/setup-a.log" 2>&1 || { cat "$W/setup-a.log"; exit 1; }
TOKEN=$(grep '^fw_' "$W/setup-a.log")
"$FW" login --server http://127.0.0.1:18000 --token "$TOKEN"
"$FW" dataset create DRILL --site drill --note 'tools/deploy-drill.sh'

step 'A：导入 v1'
time "$FW" dataset import DRILL "$W/v1" --yes --note v1

step 'A：后台导入 v2，同时在线备份'
"$FW" dataset import DRILL "$W/v2" --yes --note 'v2：修改、新增、删除、改名、复制' > "$W/v2.log" 2>&1 &
pid=$!
sleep 1
time bash backup.sh "$W/backup"
cp "$W/backup/LATEST" "$W/backup/MID"
wait $pid || { cat "$W/v2.log"; exit 1; }
tail -n 3 "$W/v2.log"
"$FW" dataset diff DRILL@2

step 'A：建训练集快照，登记模型版本、评估和发布（恢复后追溯链要一样完整）'
python3 - "$W/m" <<'PY'
import json, os, struct, sys, zlib
root = sys.argv[1]


def png(w, h, seed):
    raw = b''.join(b'\x00' + bytes((seed + x + y) % 256 for x in range(w)) for y in range(h))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


os.makedirs(f'{root}/pool/2026-10')
for i in range(10):
    with open(f'{root}/pool/2026-10/{i:03}.png', 'wb') as f:
        f.write(png(32, 32, i))
    label = {'version': '3.1', 'flags': {}, 'imagePath': f'{i:03}.png', 'imageData': None, 'imageWidth': 32,
             'imageHeight': 32, 'shapes': [{'label': 'defect', 'points': [[2, 2], [10, 10]], 'shape_type': 'rectangle'}]}
    with open(f'{root}/pool/2026-10/{i:03}.json', 'w') as f:
        json.dump(label, f)
with open(f'{root}/classes.txt', 'w') as f:
    f.write('defect\n')
with open(f'{root}/best.onnx', 'wb') as f:
    f.write(b'\x08\x07\x12\x05drill')  # starts like an ONNX file (ir_version); nothing runs it
with open(f'{root}/val.json', 'w') as f:
    json.dump({'mAP50': 0.5}, f)
PY
"$FW" dataset create DRILL-M --site drill --line L1
"$FW" dataset import DRILL-M "$W/m/pool" --yes
"$FW" trainset create DRILL-det --from DRILL-M@1 --classes "$W/m/classes.txt" --yes
"$FW" model create DRILL-detr --site drill --line L1
"$FW" model register DRILL-detr --onnx "$W/m/best.onnx" --trainset DRILL-det@1 --metrics "$W/m/val.json" \
  --code-commit 0123456789abcdef0123456789abcdef01234567
"$FW" eval register DRILL-detr@1 --testset DRILL-det@1:val --metrics "$W/m/val.json"
"$FW" release register --model DRILL-detr@1 --deploy-repo git@git.example.com:deploy/trt.git \
  --deploy-commit 89abcdef --deployed-at 2026-01-01T08:00+08:00 --gpu RTX3070 --trt 10.3 --precision fp16
"$FW" model chain drill-L1-r1 --check

step 'A：导入完成后再备份一次，全部重算哈希核对'
sleep 1   # 两次备份的文件名精确到秒
time bash backup.sh "$W/backup"
docker compose exec -T server fw-server verify --rehash

step 'B：另一台“新机器”，部署时不建管理员，恢复导入期间那份备份'
cd "$W/b/deploy"
export COMPOSE_PROJECT_NAME=fwdrillb FW_STORE_DIR=$W/b-data/store FW_DB_DIR=$W/b-data/pg FW_PORT=18001
time bash setup.sh > "$W/setup-b.log" 2>&1 || { cat "$W/setup-b.log"; exit 1; }
time bash restore.sh "$W/backup" "$(cat "$W/backup/MID")"
FW_SERVER=http://127.0.0.1:18001 "$FW" dataset versions DRILL

step 'B：备份里也没有的文件让核对失败：恢复要换回原来的数据库，服务照常'
before=$(FW_SERVER=http://127.0.0.1:18001 "$FW" dataset versions DRILL)
lost=$(sha256sum "$W/v2/chip00/img_03000.bmp" | cut -c1-64)   # v2 新加的文件
lost_path="objects/${lost:0:2}/${lost:2:2}/$lost"
mv "$W/backup/store/$lost_path" "$W/lost-object"
rm -f "$W/b-data/store/$lost_path"
if bash restore.sh "$W/backup" > "$W/restore-fail.log" 2>&1; then
  cat "$W/restore-fail.log"; echo '缺文件的恢复应该失败'; exit 1
fi
grep -E '缺失|换回|继续运行' "$W/restore-fail.log"
grep -q '平台服务在原来的数据上继续运行' "$W/restore-fail.log" || { cat "$W/restore-fail.log"; exit 1; }
after=$(FW_SERVER=http://127.0.0.1:18001 "$FW" dataset versions DRILL)
[ "$after" = "$before" ] || { echo '换回原来的数据库后，版本列表变了'; echo "$before"; echo "$after"; exit 1; }
mv "$W/lost-object" "$W/backup/store/$lost_path"

step 'B：文件库里一个文件坏了但大小没变；恢复最新的备份时要从备份把它换回来'
bad=$(sha256sum "$W/v1/chip00/img_00000.bmp" | cut -c1-64)
python3 - "$W/b-data/store/objects/${bad:0:2}/${bad:2:2}/$bad" <<'PY'
import os, sys
p = sys.argv[1]
data = bytearray(open(p, 'rb').read())
data[0] ^= 0xff
os.chmod(p, 0o644)
with open(p, 'wb') as f:
    f.write(data)
os.chmod(p, 0o444)
PY

step 'B：恢复最新的备份，清空本机缓存后从 B 取回两个版本，和源目录逐个文件比对'
time bash restore.sh "$W/backup" | tee "$W/restore-latest.log"
grep -q '从备份修复' "$W/restore-latest.log"
FW_SERVER=http://127.0.0.1:18001 "$FW" dataset versions DRILL
"$FW" cache clear
for n in 1 2; do
  FW_SERVER=http://127.0.0.1:18001 "$FW" get "DRILL@$n" --to "$W/outB$n"
  diff -r "$W/v$n" "$W/outB$n"
  echo "DRILL@$n 从 B 取回：和源目录完全一致"
done
FW_SERVER=http://127.0.0.1:18001 "$FW" model chain drill-L1-r1 --check | tee "$W/chain-b.log"
grep -q '都在文件库里' "$W/chain-b.log"
FW_SERVER=http://127.0.0.1:18001 "$FW" model get DRILL-detr@1 --to "$W/modelB"
cmp "$W/m/best.onnx" "$W/modelB/model.onnx"
echo 'DRILL-detr@1 从 B 取回：ONNX 一致，追溯链完整'
docker compose exec -T server fw-server verify --rehash

step '内存占用'
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' \
  fwdrilla-server-1 fwdrilla-db-1 fwdrillb-server-1 fwdrillb-db-1
echo
echo '演练通过'
