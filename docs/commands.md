# 命令手册

平台的全部命令、选项和设置，每条附上用途、例子和容易踩的坑。命令行里 `-h` 只有一句提示，这里写清楚为什么、什么时候用。

- **`fw`**：在笔记本、训练机、服务器上都能用，导入、取回、对比、导出数据集版本，建训练集快照。
- **`fw-server`**：只在平台服务器上用，管账号和凭据、核对文件库。它在服务容器里运行，命令前面都要加 `sudo docker compose exec server`（先 `cd /opt/aoi-flywheel/deploy`）。
- **部署脚本**：`deploy/` 下的 `setup.sh`、`backup.sh`、`restore.sh`，在平台服务器上用 `sudo bash` 运行。
- **工具**：`tools/` 下的测速和演练脚本。

命令输出和报错都是中文；退出码 0 表示成功，1 表示出错、被拒绝或没有确认，130 表示按了 Ctrl+C。

## 目录

1. [安装 fw](#安装-fw)
2. [版本的写法](#版本的写法)
3. [fw 命令](#fw-命令)
4. [在训练脚本里用](#在训练脚本里用)
5. [fw-server 命令](#fw-server-命令)
6. [部署、备份、恢复](#部署备份恢复)
7. [工具](#工具)
8. [环境变量和文件位置](#环境变量和文件位置)
9. [常见问题](#常见问题)

## 安装 fw

需要 Python 3.8 或更新的版本，Windows 和 Linux 都行。在有仓库副本的机器上：

```
cd <仓库目录>
python -m pip install ./client          # Windows 写 .\client
fw --version
```

- 一定要写 `./client`（带路径）。只写 `client` 的话，pip 会去网上找一个同名的包。
- 用 `python -m pip`，不要直接用 `pip`：conda 环境里的 `pip.exe` 有时还记着旧的 Python 路径，会报 `Can't find a default Python`。
- 更新代码后（`git pull`）要再装一次，新的 `fw` 才生效。服务器上的平台服务和 `fw` 是分开的，只改了 `fw` 时服务器不用重启。
- Windows 用 Anaconda 时，在开始菜单打开 Anaconda Prompt，`conda activate <环境名>` 之后再运行。那是 cmd 窗口：换盘要写 `cd /d D:\code\aoi-flywheel`，或者先输入 `D:` 再 `cd`。
- 平台服务器上给本机用的 `fw` 建议装在独立的虚拟环境里，例如 `sudo python3 -m venv /opt/fw-venv && sudo /opt/fw-venv/bin/pip install /opt/aoi-flywheel/client`。

## 版本的写法

`数据集名@序号`，例如 `SES-TX@3`。只写名称（`SES-TX`）就是最新版。数据集名只能用字母、数字和 `. _ -`。

每个版本是一个目录的**完整快照**：导入的目录里有什么，这个版本就是什么。冻结以后谁也改不了，也删不掉；新的导入只会多出一个版本。

## fw 命令

### fw login

```
fw login --server http://192.168.66.27:8000
```

记下平台地址和凭据，之后的命令都用它们。凭据在提示里输入，不会显示，也不会留在命令历史里；`--token <凭据>` 可以直接写在命令里（会留在历史里，脚本里用）。登录成功会显示账号和角色。

凭据由管理员在服务器上用 `fw-server token create` 发，每台机器一个。设置存在哪里见[环境变量和文件位置](#环境变量和文件位置)。

### fw whoami

看当前用的是哪个账号、哪个凭据、哪台服务器。换了机器或怀疑登错时先运行它。

### fw dataset list

列出全部数据集：类型、现场、产线、最新版本、文件数、大小、更新时间。

### fw dataset create

```
fw dataset create SES-TX --site SES --line TX
```

| 选项 | 说明 |
|---|---|
| `名称` | 必填，例如 `SES-TX` |
| `--site` | 现场，例如 `SES` |
| `--line` | 产线，例如 `TX` |
| `--kind` | `pool` 数据池（默认）、`test` 测试集、`regression` 回归集、`other` 其他 |
| `--note` | 备注 |

建好后是空的，用 `fw dataset import` 导入第一个版本。

### fw dataset import

```
fw dataset import SES-TX E:\data\SES\SES_SurfaceHigh_TX
```

检查一个目录，预览它相比父版本的变化，确认后上传服务器上还没有的文件，冻结成下一个版本。

| 选项 | 说明 |
|---|---|
| `名称` `目录` | 必填。**整个目录就是新版本的全部内容**：目录里没有的文件，在新版本里就算删除 |
| `--parent` | 父版本：`latest`（默认，最新版）、版本序号（从某个旧版本分出来）、`none`（不接父版本，开一条新的版本线） |
| `--note` | 备注，例如这一版改了什么，`fw dataset versions` 里能看到 |
| `--dry-run` | 只检查和预览，不上传、不建版本。不确定时先加它跑一遍 |
| `--yes` | 不问确认，直接建版本（脚本里用；不在终端里运行时必须加） |
| `--allow-other-files` | 图片和标注以外的文件也一起入库 |
| `--force` | 有同名不同扩展名的图片（如 `a.bmp` 和 `a.png`）也入库 |
| `--workers` | 并行数，默认 8 |

**检查**：下面这些会直接拒绝，先改好再导入：

- 有目录读不了（漏掉的文件在新版本里会变成删除）；
- 有链接文件、Windows 目录联接或特殊文件（只收普通文件和目录）；
- 有在 Windows 或 Linux 上用不了的文件名，或者只差大小写的文件名（Windows 上会互相覆盖）；
- 有同名不同扩展名的图片，除非加 `--force`；
- 有图片和标注以外的文件（比如 `labels.cache`、`manifest.csv`），除非加 `--allow-other-files`。

下面这些只提醒，不拦：没有标注的图片（负样本正常）、找不到图片的标注、不同目录里的同名图片（训练时把目录摊平会互相覆盖）、内容完全相同的图片（只存一份）。

文件类型：图片是 `.bmp .png .jpg .jpeg .tif .tiff .webp`；标注是 `.json`（LiteAnno、X-AnyLabeling）和 `.txt`（YOLO）；说明文件是 `classes.txt`、`classes.names`、`obj.names`、`data.yaml`、`dataset.yaml`、`mask_colors.json`，会一起入库。`Thumbs.db`、`desktop.ini`、`.DS_Store` 自动忽略。

**输出怎么看**：

- `相比 SES-TX@2：新增 3、修改 0、删除 0、改名 0`：按路径比较。内容没变只是换了路径算改名；同样的内容出现在新路径算新增。
- `要上传 1291 个文件，其余服务器上已有`：服务器按内容存，同样的内容只存一份。目录内部重复的文件、以前传过的文件都不再上传。
- `用时：算哈希 1.3 秒，查服务器和对比 0.1 秒，上传 0.1 秒，建版本 0.1 秒`：不含你看预览、输入 y 的时间。

**想往数据集里加一批图**：先把最新版取出来，把新图放进去，再整个目录导入：

```
fw get SES-TX --to D:\work\SES-TX
（把新的图片和标注拷进 D:\work\SES-TX）
fw dataset import SES-TX D:\work\SES-TX --note "加 30 张"
```

只导入新的那批图会让新版本只剩那几张（预览会显示大量删除，这时选 N）。

**中途断了**：上传断网、Ctrl+C、服务器出错都不会留下半个版本；重新运行同一条命令，已经传上去的文件不再传。按 Ctrl+C 后，还没开始的上传不再进行，命令在一两秒内打印「已中断」并以 130 退出（正在传的那几批由服务端丢掉或留作下次用，不会成为版本的一部分）。两个人同时往一个数据集导入时，后提交的会被拒绝，以新的最新版为父版本重新导入即可。

### fw dataset versions

```
fw dataset versions SES-TX
fw dataset versions SES-TX --source
```

列出版本：父版本、文件数、大小、时间、操作人、备注。`--source` 同时显示每个版本是从哪台机器、哪个目录导入的。

### fw dataset diff

```
fw dataset diff SES-TX@3                  # 和它的父版本比
fw dataset diff SES-TX@1 SES-TX@3         # 任意两个版本，旧的写前面
fw dataset diff SES-TX@1 SES-TX@3 --list  # 列出全部变化的文件，制表符分隔，可以存成文件用表格打开
```

### fw get

```
fw get SES-TX@3 --to D:\fw\v3
```

把一个版本取到本地的空目录（目录不存在会自动建）。目录结构和文件名和导入时一样，每个文件都按清单核对过哈希。

| 选项 | 说明 |
|---|---|
| `--to` | 必填，目标目录：要不存在或是空的 |
| `--subpath` | 只取版本里的一个子目录，例如 `--subpath pos/train`；训练集快照按 `--format` 摆好以后的路径算 |
| `--format` | 只用于训练集快照：`yolo`（默认）、`coco`、`json`、`all`，见下文 |
| `--copy` | 总是复制 |
| `--hardlink` | 硬链接，只限 Linux：不多占盘，但取出的文件只读，和缓存共用一份数据，不能改 |
| `--workers` | 并行数，默认 8 |

**本机缓存**：`fw` 在本机有一份按内容存的缓存（位置见[环境变量和文件位置](#环境变量和文件位置)）。缓存里存的是文件内容本身，按哈希命名，不是只有哈希。取回时：

1. 按版本清单列出要的内容，先看缓存里有没有；
2. 没有的从服务器下载，核对哈希后放进缓存；
3. 再按清单把每个文件从缓存复制（或 reflink）到目标目录的对应路径，复制完再核对一次哈希。

所以同一张图在几个目录、几个版本里出现，只下载一次、只在缓存里占一份。输出里会先说 `2045 种不同内容：本机缓存已有 0 个，要下载 2045 个`，最后一行写 `缓存命中 N 个，下载 M 个`。

- **冷取回**：缓存是空的（比如刚 `fw cache clear`），要下载的都得下载。
- **热取回**：内容都在缓存里，不下载，只摆放和核对，几秒完成。

方式默认先试 reflink（btrfs、XFS 上不多占盘，改文件也不影响缓存），不支持就复制（NTFS、ext4）。改取出来的文件不会影响缓存，也不会影响任何版本。

**取训练集快照**：默认摆成 YOLO 的目录，另生成 `data.yaml`，可以直接 `yolo train data=<目录>/data.yaml`。

```
fw get SES-TX-det@2 --to D:\train\det2                   # YOLO：images/ labels/ data.yaml
fw get SES-TX-det@2 --to D:\train\det2-coco --format coco  # COCO：train/ valid/ test/ 各有 _annotations.coco.json
fw get SES-TX-det@2 --to D:\look\det2 --format json       # 图片和 LiteAnno JSON 放在一起，用 LiteAnno 打开看
```

每种格式的目录、类别编号、`data.yaml` 的内容见[训练集快照的格式](trainset-format.md#fw-get-按格式摆出来)。同一张图只下载一次，取几种格式都不重复下载。

### fw export 和 fw verify

```
fw export SES-TX@3 --to E:\usb\SES-TX@3       # 在能连平台的机器上
fw verify E:\usb\SES-TX@3                     # 拷到连不上平台的机器后核对
```

导出一个能独立使用的目录：`data/` 是版本的文件，`fw-manifest.txt` 是清单，`fw-version.json` 记着版本号和清单哈希。训练集快照按快照里存的样子导出（`images/`、`labels/`、`json/`、`coco/` 都在）。`fw verify` 不用连平台，按清单检查缺少的、多出来的、内容不一致的文件，全部一致才退出 0。

### fw cache

```
fw cache info     # 缓存在哪、多少个文件、多大
fw cache clear    # 清空；已经取出的目录不受影响（--hardlink 取出的也一样）
```

缓存大小约等于你取回过的所有不同内容的总大小，不会自动清理。C 盘紧张就把缓存挪到别的盘，见 `FW_CACHE`。

### fw trainset create

```
fw trainset create SES-TX-det --from SES-TX@4 --classes classes.txt --test-dirs 2026-10 --dry-run
fw trainset create SES-TX-det --from SES-TX@4 --classes classes.txt --test-dirs 2026-10
```

从一个或几个数据池版本建训练集快照：每张图分到 train、val 或 test，连同类别表一起冻结。训练时取这个快照，模型登记时带上它，以后就能从模型查回训练用的每个文件。目录和文件的格式见[训练集快照的格式](trainset-format.md)。

先预览：每个划分多少张（其中多少张没有标注）、每个类别在每个划分里多少个框，接着上一版时还有新增、移除、换了划分、标注改了的个数，还有生成了哪几种标注格式。确认后才建。建的时候服务器把标注转成 YOLO 和 COCO 检测框，和图片、原始 JSON 一起冻结（转换规则见[训练集快照的格式](trainset-format.md#标注怎么转)）。同一个名称再运行一次，就是这个快照的下一版，默认**接着上一版的划分**：原来的样本不换划分，新样本进 test。

| 选项 | 说明 |
|---|---|
| `--from` | 必填，来源数据池版本，例如 `SES-TX@4`；几条产线合成一个快照就写几次。来源数据集要填了现场和产线 |
| `--classes` | 必填，类别表文件：一行一个类别名，行号（从 0 起）是类别编号，YOLO 和 COCO 都用它。标注里有表以外的类别，或者 YOLO 编号超出范围，就不建 |
| `--val` | train/val 里 val 的比例，默认 0.2 |
| `--seed` | 种子，默认 0。同样的样本、比例和种子，分法总是一样 |
| `--test-dirs` | 这些目录下的新样本进 test，例如 `2026-10`，或带上现场和产线 `SES/TX/2026-10`；逗号分隔或写多次 |
| `--merge-test` | 上一版 test 里的样本重新分到 train/val（比如上一版的 test 已经测完了，要并进来训练） |
| `--new-to` | 接着上一版时新样本去哪：`test`（默认）或 `trainval` |
| `--split-file` | 用自己的划分清单：每行一个样本「`<现场>/<产线>/<图片路径>` 制表符或空格 `train`、`val` 或 `test`」，`#` 开头的行不算。清单要正好包括来源里的全部图片。用清单时不算接着上一版（`based_on` 是空的） |
| `--fresh` | 不接着上一版，全部重新划分 |
| `--require-labels` | 每张图都要有标注：有没标注的图就不建，预览里列出来。默认没有标注的图当负样本收进来 |
| `--note` | 备注 |
| `--dry-run` | 只预览，不建 |
| `--yes` | 不问确认 |

- 名称和数据集共用：已经有叫这个名的数据集，就不能用来建快照。
- 快照只能用这条命令建，`fw dataset import` 往快照里导入会被拒绝。
- 和最新版完全一样（样本、划分、标注、类别表都没变）时不建。
- 这一版只做检测：`point`、`line`、`circle` 这类形状不能当检测框，有就不建；`polygon`、`rotation` 按外接矩形当检测框。
- 有图片读不出尺寸时（不是 PNG、JPEG、BMP、GIF、TIFF、WebP，又没有 JSON 标注）不生成 COCO；快照里有 YOLO txt 标注时不生成 JSON 格式。预览里写着原因。
- `--from` 只写名称（或 `@latest`）时，预览时定下是哪个版本，确认后冻结的就是那个版本；预览和确认之间有人导入了新版本也不影响。
- 预览之后有人建了同名快照的新版本（或者同时有人建同名的第一版），就不建，重新运行一次即可。第一版没建成时名称也不会被占住。
- 同一现场和产线的两个来源里有同名不同扩展名的图片（例如 `same/a.png` 和 `same/a.jpg`）时不建：它们的 YOLO 标注会是同一个文件。
- 没有 train 或 val 样本时预览会提醒：这样的快照只能当测试集，不能直接训练。
- 服务器要读每个标注文件核对类别，几十万张图时预览和建快照各要一些时间。

### fw trainset list 和 fw trainset show

```
fw trainset list                 # 全部快照和它们的最新版
fw trainset show SES-TX-det@2    # 来源、划分规则、每个划分的张数和每类框数
```

取回用 `fw get SES-TX-det@2 --to <目录>`，导出、核对、对比也和数据集版本一样（`fw export`、`fw verify`、`fw dataset diff`）。对比两个快照时，换了划分的样本显示成改名。`fw dataset list` 里也列出快照，类型是「训练集快照」。

## 在训练脚本里用

```python
import fw

ts = fw.get_trainset('SES-TX-det@2', '/data/train/SES-TX-det@2')   # 取训练集快照
print(ts['id'], ts['path'], ts['classes'])   # 登记模型时带上 ts['id']；classes 的下标就是类别编号

v = fw.get_version('SES-TX@3', '/data/train/SES-TX@3')   # 取任意一个版本（目录要不存在或为空），返回版本记录
print(v['id'], v['path'])
```

和 `fw get` 一样用缓存、逐个核对；进度和结果打印到 stderr。要先在这台机器上 `fw login`，或者设好 `FW_SERVER`、`FW_TOKEN`。

`fw.get_trainset(ref, dest, fmt='yolo')`：`fmt` 和 `fw get --format` 一样。返回的记录里另有 `format`、`data_yaml`（YOLO 时，给 ultralytics 的 `data=`）、`classes`（类别名列表）、`counts`（每个划分的张数和每类框数）、`formats`（快照里有的格式）、`sources`（来源版本）、`based_on`（接着哪个快照）和 `rule`（划分规则）；不是快照的名称会报错（`fw.ApiError`，`.status` 是 400）。

```python
from ultralytics import YOLO
ts = fw.get_trainset('SES-TX-det@2', '/data/train/SES-TX-det@2')
YOLO('yolov8s.pt').train(data=ts['data_yaml'])
```

出错时可能抛三种异常，训练脚本要都接住：

| 异常 | 什么时候 |
|---|---|
| `fw.ApiError` | 平台拒绝了请求：版本不存在（`.status` 是 404）、凭据失效（401）、没有权限（403）、文件库停写（503）等；`.status` 是 HTTP 状态码，`.message` 是中文说明 |
| `fw.FwError` | 本机的问题：版本写法不对、目标目录不是空的、缓存里的文件重新下载后仍对不上等 |
| `httpx.TransportError` | 连不上平台（地址不对、网络断了、服务没起） |

```python
import httpx
import fw

try:
    v = fw.get_version('SES-TX@3', '/data/train/SES-TX@3')
except (fw.ApiError, fw.FwError, httpx.TransportError) as e:
    raise SystemExit(f'取数据失败：{e}')
```

## fw-server 命令

在平台服务器上：

```
cd /opt/aoi-flywheel/deploy
sudo docker compose exec server fw-server <命令>
```

### 账号

```
fw-server user add zhangsan --role algo
fw-server user list
fw-server user disable zhangsan        # 立即吊销这个账号的全部凭据
fw-server user enable zhangsan         # 恢复账号；旧凭据仍然作废，要重新发
```

| 角色 | 第一步能做的 |
|---|---|
| `admin` 管理员 | 和算法工程师一样 |
| `algo` 算法工程师 | 建数据集、导入、取回、对比、导出 |
| `field` 现场实施 | 第一步还不能碰数据集 |
| `deploy` 部署 | 第一步还不能碰数据集 |

### 凭据

```
fw-server token create zhangsan --label 训练机-01   # 打印新凭据，只显示这一次
fw-server token list [账号]
fw-server token revoke 3                          # 按 list 里的编号吊销
```

每台机器一个凭据，`--label` 写清楚给谁、哪台机器，丢了或者机器报废就吊销那一个。不要把凭据发到聊天群或写进仓库。

### verify：核对文件库

```
fw-server verify                                    # 快：每个版本的清单能对上，用到的文件都在、大小对
fw-server verify --rehash                           # 慢：把文件库里每个文件从盘上读出来重算哈希
```

坏的、缺的文件要从备份取回时，备份目录得先挂进容器：`fw-server` 在容器里运行，只看得到容器里的路径，宿主机上的 `/backup/flywheel` 它看不到（看不到就修不了，只会把坏文件隔离、核对不通过）。用一个挂上备份目录的临时容器：

```
cd /opt/aoi-flywheel/deploy
sudo docker compose run --rm --no-deps -v /backup/flywheel/store:/backup:ro server \
  fw-server verify --rehash --repair-from /backup
```

`-v 宿主机目录:/backup:ro` 把备份只读挂到容器的 `/backup`，`--repair-from` 后面写容器里的这个路径。`restore.sh` 也是这样做的。

- 报「问题 0 个」并退出 0 才算通过。
- 文件库停止写入时（写盘出错，或者断电时有没落盘的写入），导入会被拒绝（503），只有 `--rehash` 通过才会解除：最后一行显示「全部核对通过，已解除：…」，`echo $?` 为 0。如果显示「核对通过了，但没能解除写入阻断」或退出码不是 0，就是还在停写，先检查磁盘。
- 还没有版本用到的文件（导入中断留下的）坏了会移到文件库的 `quarantine/`，不算问题，重新导入时会再上传。
- 读不了的目录或文件算问题，核对不通过。
- 建议每周空闲时跑一次 `--rehash`（见[定时任务](#定时任务)）。

### migrate、serve

`fw-server migrate` 把数据库升到程序需要的结构；服务启动时会自动执行，平时不用手动跑。数据库比程序新时服务拒绝启动。`fw-server serve` 是容器里启动服务的命令，不用手动运行。

## 部署、备份、恢复

都在平台服务器上，在仓库目录里运行。

### 第一次部署

```
sudo git clone https://github.com/LorenzoSun-V/aoi-flywheel.git /opt/aoi-flywheel
sudo bash /opt/aoi-flywheel/deploy/setup.sh lorenzo
```

生成 `deploy/.env`（随机数据库密码、目录、端口，只有 root 能读），建目录，构建并启动两个容器（数据库和平台服务），建管理员账号 `lorenzo` 并打印它的第一个凭据（只显示一次）。不写账号名就不建管理员（在新机器上恢复备份时就这样）。

第一次运行前可以用环境变量改默认值，例如 `sudo FW_PORT=8100 bash deploy/setup.sh lorenzo`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FW_STORE_DIR` | `/data/flywheel/store` | 文件库，放大容量的机械盘 |
| `FW_DB_DIR` | `/opt/flywheel/pg` | 数据库，放固态盘 |
| `FW_PORT` | `8000` | 对外端口，`fw login --server` 里写的那个 |
| `FW_WORKERS` | `4` | 服务进程数 |
| `FW_SKIP_BUILD` | 不设 | 设为 1 就不构建镜像，用已经 `docker load` 的（服务器不能联网时） |

已经部署过再改，就改 `deploy/.env` 后 `sudo docker compose up -d`。装 Python 包慢时在 `deploy/.env` 里加 `PIP_INDEX_URL=<镜像源>` 再重跑 `setup.sh`。服务器不能联网时的离线镜像做法见 README。

### 日常运维

```
cd /opt/aoi-flywheel/deploy
sudo docker compose ps                  # 两个容器是否在跑
sudo docker compose logs --tail 100 server
sudo docker compose stop                # 停服务（数据不动；重启机器后也不会自己起来）
sudo docker compose start               # 再启动
```

升级：先备份，再 `git pull`，然后 `sudo docker compose up -d --build`。服务启动时自动升级数据库结构。只改了 `fw` 客户端的更新不用重启服务。

### 备份

```
sudo bash /opt/aoi-flywheel/deploy/backup.sh /backup/flywheel
```

不停服务：先导出数据库，再把文件库里新增的文件拷到备份目录（文件写进去以后不再改，所以只拷新的）。备份目录最好在另一块盘或另一台机器上。数据库导出默认保留最近 14 份，用 `FW_BACKUP_KEEP` 改。

### 恢复

```
sudo bash /opt/aoi-flywheel/deploy/restore.sh /backup/flywheel                        # 用最新一份
sudo bash /opt/aoi-flywheel/deploy/restore.sh /backup/flywheel fw-20261103-020000.dump # 指定一份
```

把数据库换成备份里的那份（原来的改名保留），再按它核对文件库：缺的、坏的从备份补回，每个文件重算哈希。全部通过才重新对外服务；任何一步失败都换回原来的数据库。恢复期间服务停着，用时和文件库大小成正比。在新机器上恢复：先 `sudo bash deploy/setup.sh`（不写账号名），再运行 `restore.sh`；账号和凭据都在备份里。

### 定时任务

整段复制到服务器终端运行（不要把里面的两行单独粘进终端）：

```
sudo tee /etc/cron.d/aoi-flywheel >/dev/null <<'EOF'
0 2 * * *  root  bash /opt/aoi-flywheel/deploy/backup.sh /backup/flywheel >> /var/log/flywheel-backup.log 2>&1
0 4 * * 0  root  cd /opt/aoi-flywheel/deploy && docker compose exec -T server fw-server verify --rehash >> /var/log/flywheel-verify.log 2>&1
EOF
sudo mkdir -p /backup/flywheel
```

每天 2 点备份，每周日 4 点全库核对。结果看 `/var/log/flywheel-backup.log` 和 `/var/log/flywheel-verify.log`。

## 工具

### tools/deploy-drill.sh：部署和恢复演练

```
sudo FW_BIN=$(command -v fw) bash tools/deploy-drill.sh /data/tmp/fw-drill          # 跑完自动拆掉
sudo FW_BIN=$(command -v fw) bash tools/deploy-drill.sh /data/tmp/fw-drill --keep   # 保留两套部署
```

在一台机器上临时起两套独立的部署（端口 18000、18001，compose 项目名 `fwdrilla`、`fwdrillb`），走一遍「部署 → 导入 → 导入期间在线备份 → 在另一套上恢复 → 取回核对」。不碰正式部署（`/data/flywheel`、`/opt/flywheel`、端口 8000），`fw` 的登录信息和缓存放在工作目录里。

### tools/bench.py：规模测速

```
FW_SERVER=http://<平台>:8000 FW_TOKEN=<凭据> python tools/bench.py --pairs 100000 --work /data/tmp
```

生成 N 对图片和标注，测 v1 导入、v2 导入（改 1% 标注、加 1%、删 1%、改名和复制各 100 对）、对比、冷取回、热取回，每次取回都和源目录逐个比对，输出 JSON。会建一个名为 `bench-<时间>` 的数据集。`--img-kb` 每张图大小（默认 8 KB），`--work` 临时目录（要有数据量 5 倍左右的空间），`--keep` 保留生成的文件。

测速要用**空的文件库**：同样的内容第二次导入不会再上传，量不出上传速度。可以用 `deploy-drill.sh --keep` 起的那套临时部署（端口 18000）来测。

## 环境变量和文件位置

### fw（客户端）

| 变量 | 说明 |
|---|---|
| `FW_SERVER` | 平台地址，设了就不用 `fw login` 里存的那个 |
| `FW_TOKEN` | 凭据，同上。脚本、CI 里用；注意不要打印到日志 |
| `FW_CONFIG` | 设置文件的位置 |
| `FW_CACHE` | 本机缓存的位置 |

默认位置：

| | Windows | Linux |
|---|---|---|
| 设置（地址和凭据） | `%APPDATA%\fw\config.json` | `~/.config/fw/config.json`（只有自己能读） |
| 缓存 | `%LOCALAPPDATA%\fw` | `~/.cache/fw` |

把缓存挪到 D 盘：Windows 运行 `setx FW_CACHE D:\fw-cache`，然后**重新打开**命令窗口；Linux 在 `~/.bashrc` 里加 `export FW_CACHE=/data/fw-cache`。旧位置的缓存可以先 `fw cache clear` 清掉。

### fw-server（服务端，容器里已设好）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FW_DSN` | 必填 | 数据库地址 |
| `FW_STORE` | 必填 | 文件库根目录 |
| `FW_HOST` / `FW_PORT` | `0.0.0.0` / `8000` | 服务监听的地址和端口（容器内） |
| `FW_WORKERS` | `4` | 服务进程数 |
| `FW_POOL_SIZE` | `8` | 每个进程的数据库连接数 |
| `FW_MAX_OBJECT_GB` | `64` | 单个文件大小上限 |

部署时这些由 `deploy/docker-compose.yml` 和 `deploy/.env` 给出；`FW_POOL_SIZE`、`FW_MAX_OBJECT_GB` 要改的话，在 `docker-compose.yml` 的 `server.environment` 里加上。

### 文件库的目录

`objects/ab/cd/<sha256>` 是文件内容（按哈希命名，只读）；`tmp/` 是上传中的临时文件；`quarantine/` 是核对时移出来的坏文件；`WRITE-OK`、`WRITE-ERROR`、`UNSYNCED` 是写入状态标记，不要手动改。直接打开文件库看不出原来的文件名，要看图就用 `fw get` 取出来。

## 常见问题

| 现象 | 原因和办法 |
|---|---|
| `Python was not found; run without arguments to install from the Microsoft Store` | Windows 自带的应用商店快捷方式，不是真的 Python。装 Python，或用 Anaconda Prompt |
| `Can't find a default Python` | `pip.exe` 记着失效的 Python 路径。改用 `python -m pip install ./client` |
| `fw` 不是内部或外部命令 | 没装，或 Scripts 目录不在 PATH 里。用 `python -m fw.cli` 代替 `fw` |
| cmd 里 `cd D:\...` 进不去 | cmd 换盘要 `cd /d D:\...` |
| `需要凭据：先运行 fw login` / `凭据无效、已吊销，或者账号已停用` | 重新 `fw login`；凭据被吊销就找管理员重新发 |
| `…账号不能查看或修改数据集` | 第一步只有 `admin`、`algo` 能用数据集 |
| `连不上平台服务` | 地址或端口不对、服务没起、网络不通。`fw whoami` 看地址；服务器上 `sudo docker compose ps` |
| 导入报 503，提示运行 `verify --rehash` | 文件库停止写入了（写盘出错或断电）。先检查磁盘，再在服务器上 `fw-server verify --rehash` |
| 导入被拒绝：`…在这期间有了变化（最新是 …）` | 导入期间别人建了新版本，后提交的被拒。重新运行导入，会以新的最新版为父版本 |
| `已存在且不是空目录` | `fw get`、`fw export` 的目标目录要不存在或为空 |
| `需要确认才能继续：在终端里运行，或者加 --yes` | 不是在终端里运行（脚本、管道），加 `--yes` |
| 预览里删除特别多 | 导入的目录不是完整的数据集。按[想往数据集里加一批图](#fw-dataset-import)的做法来，这次先选 N |
