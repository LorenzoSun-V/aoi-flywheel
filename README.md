# aoi-flywheel

数据飞轮平台的底座：数据集版本、模型版本、预标注（推理）。设计见[架构文档](docs/architecture.md)（[在线版](https://claude.ai/artifact/A8f2NKVHhLEF295TwomviL)）。

文档：[命令手册](docs/commands.md)（全部命令、选项、环境变量和常见问题）、[维护和开发说明](docs/development.md)（代码结构、不能破的规则、测试、怎么扩展）、[清单格式](docs/manifest-format.md)、[训练集快照的格式](docs/trainset-format.md)、[更新记录](CHANGELOG.md)。

分四步做，每步一个 PR：

1. **文件库、数据集版本、`fw` 命令行**，加部署、在线备份和恢复（已完成）
2. **训练集快照**，**模型版本、评估报告、发布登记、追溯链**（已写好，见下文；引擎由第三步从 ONNX 构建）
3. 推理任务、GPU worker、统一评估、预测集和预标注（已写好：用你们的 TRT 部署代码；写回批次等批次复核）
4. 网页，之后接上批次复核

## 组成

- 一个平台服务（`fw-server`，Python + FastAPI）、一个 PostgreSQL、一个按内容哈希存文件的文件库，用 docker compose 起两个容器。
- 所有机器都只通过 HTTP 找平台服务，跨网段只要开一个端口；只有平台服务碰数据库和文件库。
- `fw` 是一个 Python 包（Python 3.8 起，Windows 和 Linux 都能用），给服务器和训练机用：训练脚本取数据集、在服务器上直接导入大批数据、给连不上平台的机器导出。人日常操作用网页（第四步）。

```
client/      fw：命令行和 Python 客户端（fw.contract 里的清单格式和路径规则服务端也用）
server/      fw-server：接口、数据库表（numbered SQL 迁移）、文件库、账号和凭据
deploy/      Dockerfile、docker-compose.yml、setup.sh、backup.sh、restore.sh
tests/       client/（不需要服务，Windows 和 Python 3.8 也跑）、server/（真实服务 + PostgreSQL）
tools/       bench.py（规模测速）、deploy-drill.sh（部署、在线备份、异地恢复演练）
docs/        架构文档、清单格式
```

## 这一步保证什么

| 规则 | 怎么做到 |
|---|---|
| 冻结的版本永远不变 | 数据库触发器拒绝冻结版本清单项的新增、修改、删除，也拒绝改版本记录、解冻和删除；冻结那一刻由数据库按清单项重算清单哈希、文件数和大小，和登记的对不上就不冻结；没冻结的版本提交不了。 |
| 读任何版本都不经过父版本 | 每个版本存完整清单（路径 → SHA-256、大小），复制、改名、删除怎么组合都不影响旧版本。 |
| 数据库里出现的文件一定在盘上 | 上传先写到同一文件系统里的临时文件，边写边算哈希，对上了才放到最终文件名；建版本前对整个文件库做一次 `syncfs`，文件内容和目录项一起落盘，成功了才提交数据库。`syncfs` 出错时文件库停止接受写入：删掉文件库根目录的 `WRITE-OK`（删除不占空间，盘满也做得到）、写下 `WRITE-ERROR`，服务进程里也记着；`fw-server verify --rehash` 通过前不再接受写入；它把文件库里每个文件从盘上读出来核对，也包括导入中断时留下、还没有版本用到的文件。补传旧版本缺的文件、从备份修复之后，也先落盘再报告。断电或死机不报任何错，却会丢掉没落盘的写入：所以每次写入前先在文件库根目录记下 `UNSYNCED`（含本次开机的编号），确认它落了盘才放入文件，全部落盘后删掉；别的进程刚建、还没确认落盘的标记不算数，各进程自己再落盘确认一次，确认时出错就和写盘出错一样停止写入。重启后发现上次开机留下的 `UNSYNCED`，就和写盘出错一样停止写入，直到 `verify --rehash` 通过；删不掉这个标记时 verify 报错退出，不会显示已解除。 |
| 取回的就是存进去的 | 下载先核对 SHA-256 再进本机缓存；摆进目录的每个文件再按清单核对一次，对不上就重新下载，新的一份原子替换旧的。缓存从不因为索引记录对不上就删文件：几个 `fw` 进程共用一个缓存时，刚读到的记录可能已经过时。 |
| 改取回目录不影响缓存 | 默认 reflink（btrfs、XFS），文件系统不支持就复制（ext4、NTFS）。硬链接只在加 `--hardlink` 时用，只限 Linux：不多占盘，但文件只读、和缓存共用一份数据。 |
| 不会悄悄丢掉别人的修改 | 每个版本是一个目录的完整快照。两个人同时往一个数据集导入，后提交的会被拒绝，要以新的最新版本为父版本重新导入。 |
| 中途失败不留半个版本 | 上传断了、数据库提交失败、写盘出错，都不会留下缺文件的冻结版本；已经传上去的文件下次直接复用。 |
| 模型版本、评估、发布登记后不变 | 模型版本、评估报告、发布和模型都只能新增，数据库触发器拒绝修改和删除；模型版本、评估报告的文件和数据集版本一样按清单冻结。登记前文件必须已经在服务器上。标签（候选、弃用）是唯一能改的，每次改动多一条记录。 |
| 从发布查回训练数据 | 每个模型版本必须引用一个训练集快照；`fw model chain <发布>` 从发布一路打印到模型版本、快照、数据池版本，`--check` 核对链上每个文件都在。产线现在用的模型是部署时间最晚的发布，补登旧的不会顶掉新的。 |
| 训练集快照冻结的是结果 | 每张图分在 train、val 还是 test，类别表，以及建快照时转好的 YOLO、COCO 标注，都作为一个版本冻结；取回时原样摆出来，不重新划分、不重新转换。标注里有类别表以外的类别、转不成检测框的形状、图外的框，直接不建。同名再建一版时，原来的样本不换划分。 |

还没做：归档、清理（垃圾回收）。你们 intake.py 里按画面布局分线、标注口径、像素查重这些 SES 专用的检查，等批次入库那一步做成每个项目可配的检查。

## 部署

在平台服务器上（需要 Docker 和 docker compose；文件库默认放机械盘 `/data/flywheel/store`，数据库放固态盘 `/opt/flywheel/pg`）：

```
sudo git clone https://github.com/LorenzoSun-V/aoi-flywheel.git /opt/aoi-flywheel
sudo bash /opt/aoi-flywheel/deploy/setup.sh lorenzo
```

`setup.sh` 生成 `deploy/.env`（随机数据库密码、目录、端口），构建并启动两个容器，建管理员账号 `lorenzo` 并打印它的第一个凭据（只显示一次）。目录和端口要改，就在第一次运行前设环境变量 `FW_STORE_DIR`、`FW_DB_DIR`、`FW_PORT`。国内网络装 Python 包慢的话，在 `deploy/.env` 里加 `PIP_INDEX_URL=<镜像源>` 再重跑 `setup.sh`。

服务器不能联网时，在一台能联网、装了 Docker 的机器上，在仓库目录里：

```
docker build -f deploy/Dockerfile -t aoi-flywheel-server:local .
docker pull postgres:16.15-bookworm
docker save aoi-flywheel-server:local postgres:16.15-bookworm | gzip > fw-images.tar.gz
```

把 `fw-images.tar.gz` 和仓库一起拷到服务器上，`gunzip -c fw-images.tar.gz | sudo docker load`，再 `sudo FW_SKIP_BUILD=1 bash deploy/setup.sh lorenzo`。

升级：`git pull` 后先备份，再 `sudo docker compose -f deploy/docker-compose.yml up -d --build`；服务启动时会自动把数据库升到新结构（`fw-server migrate`），数据库比程序新时拒绝启动。

## 账号和凭据

| 角色 | 能做的 |
|---|---|
| `admin` 管理员 | 算法工程师和现场实施能做的都能做；管账号和凭据（网页「账号」页，或服务器上的 `fw-server`） |
| `algo` 算法工程师 | 新建和修改数据集、训练集快照、模型、评估报告、任务；给版本起版本号（`v0.1.0`，撤销只有管理员能做）；复核标注批次（通过、退回）并入库；看发布、追溯链和操作记录 |
| `field` 现场实施 | 看全部数据集和模型，下载它们的文件；上传标注批次、给它跑预标注、提交复核（只能改自己上传的批次）；部署成功后登记发布。不能复核、入库，不能新建或修改数据集 |
| `worker` GPU worker | 每台 GPU 机一个：登记推理环境（conda 环境），领取、运行和交回任务 |

每台机器一个凭据，能列出、能吊销；停用账号会立即吊销它的全部凭据。原来的「部署」角色并进了现场实施（升级时已有的部署账号自动变成现场实施）。谁在什么时候改了什么，记在网页的「操作记录」里。

```
cd /opt/aoi-flywheel/deploy
sudo docker compose exec server fw-server user add zhangsan --role algo
sudo docker compose exec server fw-server token create zhangsan --label 训练机-01
sudo docker compose exec server fw-server token list
sudo docker compose exec server fw-server token revoke 3
sudo docker compose exec server fw-server user disable zhangsan
```

## 网页

浏览器打开 `http://<服务器>:<端口>/`（和 `fw login --server` 是同一个地址），用自己的凭据登录（和 `fw login` 用的同一串，只存在这个浏览器里）。现在能：

- 看数据集和它的版本，按目录浏览版本里的图片（缩略图，绿点表示有 JSON 标注），点开看大图和标注框（矩形、多边形、点、线、圆；预标注显示分数），左右方向键翻图；
- 比较同一数据集的两个版本：新增、删除、修改、改名的文件，点开就是那张图；
- 看模型、版本、评估报告（两份比较），在模型版本页提交评估和预标注，在任务页看排队、实时日志、取消；
- 看预测集：每张图几个框，拖阈值，大图上实线是标注、虚线是预测。

- 新建数据池；往数据池里上传新增目录、去掉目录（和 `fw dataset add`、`fw dataset remove` 一样）；上传完整版本（和 `fw dataset import` 一样检查、去重、冻结；大批数据还是用命令行）。

网页由 fw-server 直接提供（`server/fwserver/web/`，原生 JS，不用构建），部署不加东西。

## fw 命令行

安装（在有仓库副本的机器上；没有的机器把 `client` 目录拷过去再装）：

```
pip install ./client
fw login --server http://<平台服务器>:8000             # 提示里输入凭据，不会留在命令历史里
```

| 命令 | 做什么 |
|---|---|
| `fw dataset create SES-TX --site SES --line TX` | 建数据集（一条产线的数据池，`--kind test` 测试集、`regression` 回归集） |
| `fw dataset import SES-TX /data/SES/TX_v2` | 检查目录，列出相比父版本的新增、修改、删除和改名，确认后只上传服务器上没有的文件，冻结成下一个版本。`--dry-run` 只检查；`--yes` 不问确认 |
| `fw dataset add SES-TX /data/20261104` | 往数据集里加目录：新版本 = 最新版本 + 这个目录，只上传这个目录。池里已有同名目录时拒绝（`--replace` 整个换掉）；和池里重复的图、标注不一致的重复图只提醒 |
| `fw dataset remove SES-TX 20261101` | 从数据集里去掉目录：新版本 = 最新版本去掉它，旧版本不变 |
| `fw dataset versions SES-TX` | 列出版本 |
| `fw dataset diff SES-TX@2` | 和父版本比；`fw dataset diff SES-TX@1 SES-TX@3 --list` 比任意两个版本并列出全部文件 |
| `fw get SES-TX@3 --to /data/train/SES-TX@3` | 取到一个空目录：只下载缓存里没有的，每个文件按清单核对 |
| `fw export SES-TX@3 --to /mnt/usb/SES-TX@3` | 导出给连不上平台的机器，到那边 `fw verify /mnt/usb/SES-TX@3` 核对 |
| `fw cache info` / `fw cache clear` | 本机缓存大小 / 清空（已经取出的目录不受影响） |
| `fw trainset create SES-TX-det --from SES-TX@4 --classes classes.txt --val 0.2 --test 0.1` | 建训练集快照：划分 train/val/test，转好 YOLO、COCO，冻结 |
| `fw model register SES-TX-detr --onnx best.onnx --weights best.pt --trainset SES-TX-det@2` | 上传 ONNX 和权重，登记模型的下一个版本 |
| `fw eval run SES-TX-detr@3 --data SES-TX@5 --dirs SES/TX/test_1015 --runner detr-pro` | 排一个评估任务：GPU 机从 ONNX 建 TensorRT 引擎推理，aoi-deteval 算指标，结果自动登记成评估报告 |
| `fw eval register SES-TX-detr@3 --testset SES-TX-det@2:test --deteval results/` | 登记 aoi-deteval 的评估结果 |
| `fw release register --model SES-TX-detr@3 --deploy-repo … --deploy-commit …` | 部署成功后登记发布（部署账号） |
| `fw model chain SES-TX-r3 --check` | 追溯链：发布 → 模型版本 → 训练集快照 → 数据池版本 |
| `fw predict run SES-TX-detr@3 --data SES-TX@6 --dirs SES/TX/new_1109 --runner detr-pro` | 排一个推理任务：GPU 机用 TensorRT 引擎跑选中的图，结果存成预测集（同样的请求直接复用） |
| `fw predict get 7 ~/label/new_1109` | 取出预测集的图片和预标注，LiteAnno 直接打开改 |
| `fw runner register detr-pro --env detr-pro --framework rfdetr --onnx2engine /opt/deploy/onnx2engine` | 在 GPU 机上登记推理环境（worker 账号） |
| `fw worker` | 在 GPU 机上常驻，领取并运行任务 |
| `fw job list` / `fw job show 12` / `fw job cancel 12` | 看任务队列、任务结果；取消任务 |

每条命令的全部选项、输出怎么看、缓存放在哪和怎么挪（`FW_CACHE`）、Windows 上的安装问题，见[命令手册](docs/commands.md)。

导入前的检查沿用试点的 01 号脚本：只差大小写的文件名、Windows 用不了的文件名、链接文件直接拒绝；同名不同扩展名的图片要加 `--force`，图片和标注以外的文件要加 `--allow-other-files`；没标注的图、没有图的标注、不同目录里的同名图片、内容完全相同的图片列出来提醒。

训练脚本里：

```python
import fw
ts = fw.get_trainset('SES-TX-det@2', '/data/train/SES-TX-det@2')   # 取训练集快照，默认平铺的 YOLO 目录（train、neg_train、val、test）
# ……训练，读 ts['data_yaml']……
mv = fw.register_model('SES-TX-detr', onnx='best.onnx', weights='best.pt', trainset=ts)
print(mv['ref'])                                                   # SES-TX-detr@3
```

## 备份、恢复和核对

定时任务（整段在服务器终端运行，建 `/etc/cron.d/aoi-flywheel`）：

```
sudo tee /etc/cron.d/aoi-flywheel >/dev/null <<'EOF'
0 2 * * *  root  bash /opt/aoi-flywheel/deploy/backup.sh /backup/flywheel >> /var/log/flywheel-backup.log 2>&1
0 4 * * 0  root  cd /opt/aoi-flywheel/deploy && docker compose exec -T server fw-server verify --rehash >> /var/log/flywheel-verify.log 2>&1
EOF
```

- 备份不停服务：先 `pg_dump`（一致的快照），再把文件库新增的文件 `rsync` 到备份目录。数据库只在文件落盘后才提交引用，所以先导出的数据库引用的文件这时都在文件库里。备份目录最好在另一块盘或另一台机器上。
- 恢复：`sudo bash deploy/restore.sh /backup/flywheel`。把数据库换成备份里的那份（原数据库改名保留），然后在不开端口的一次性容器里按这份数据库核对文件库：缺的和坏了的文件从备份补回，每个文件都从盘上读出来重算哈希，补回的文件落盘后才算完。全部通过才重新对外服务；任何一步失败（包括核对没通过）都换回原来的数据库，服务在原来的数据上继续运行。核对要把用到的每个文件读一遍，用时和文件库大小成正比：部署演练里 6000 个文件（89 MB）整个恢复约 13 秒，更大的文件库还没实测。在新机器上先 `sudo bash deploy/setup.sh`（不用建管理员，账号和凭据都在备份里）再恢复。
- 核对：`fw-server verify` 检查每个版本的清单项能重建登记的清单哈希、用到的每个文件都在且大小对；加 `--rehash` 逐个重算哈希，读之前先丢掉这个文件的页缓存，读到的是盘上真实的内容；还没有版本用到的文件（导入中断时留下的）也算，坏了的移到 `quarantine/`，不算问题，重新导入时会再上传；读不了的目录或文件算问题，核对不通过；全部通过后才解除写盘出错的标记。损坏的文件可以用 `--repair-from` 从备份取回（备份目录要挂进容器，命令见[命令手册](docs/commands.md#verify核对文件库)），取回的文件落盘后才报告；没有备份的移到 `quarantine/`，重新导入含它的目录就会补回。

## 第一步验收（试点服务器）

1. 部署：`setup.sh`，在笔记本上 `fw login`。
2. SES TX v1 原样导入；用 TX_v2_test 建 v2，并故意加进复制和改名的文件；`fw dataset diff` 的数字和预期一致。
3. 在笔记本或训练机上冷取回（`fw cache clear` 后 `fw get`），和源目录逐个文件比对一致。
4. 改取回目录里的文件（原地改写），再 `fw get` 到另一个目录，结果仍和版本一致，缓存没有被改。
5. 冻结后改不了：`docker compose exec db psql -U fw -c "INSERT INTO version_files ..."` 之类的语句都会被拒绝。
6. 中途失败：导入时拔网线或 Ctrl+C，平台上不出现新版本；重跑同一条命令只传剩下的文件。
7. 导入期间运行 `backup.sh`；在另一台机器上 `setup.sh` + `restore.sh`，再取回一致。`tools/deploy-drill.sh` 在一台机器上用两套部署自动跑一遍这个过程。
8. 规模：`FW_SERVER=... FW_TOKEN=... python tools/bench.py --pairs 100000 --work /data/tmp`，十万对（20 万个文件）入库、差异、冷取、热取的时间；`--pairs 500000` 是百万文件。

## 开发

代码结构、必须守住的规则和测试方法见[维护和开发说明](docs/development.md)。

```
python -m venv .venv && . .venv/bin/activate
pip install -c deploy/constraints.txt -e ./client -e ./server pytest import-linter
lint-imports                                     # 模块只能往下调：cli、app → datasets、auth → store、db、settings
FW_TEST_ADMIN_DSN=postgresql://fw@127.0.0.1:5432/postgres pytest -q tests
pytest -q tests/client                           # 不需要 PostgreSQL
FW_RANDOM_VERSIONS=60 FW_RANDOM_SEEDS=1,2,3,4,5 pytest -q tests/server/test_random_versions.py
```

服务端只支持 Linux（`syncfs`）。数据库结构的每次修改都是 `server/fwserver/migrations/` 里一个新的编号文件，应用过的文件不能再改（服务会拒绝启动）。真实现场数据不要提交到仓库。
