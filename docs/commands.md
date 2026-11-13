# 命令手册

平台的全部命令、选项和设置，每条附上用途、例子和容易踩的坑。命令行里 `-h` 只有一句提示，这里写清楚为什么、什么时候用。

- **`fw`**：在笔记本、训练机、服务器上都能用，导入、取回、对比、导出数据集版本，建训练集快照，登记模型版本、评估报告和发布，查追溯链。
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

序号是平台按顺序排的。你也可以给一个版本起**版本号**，写成 `v主.次.修订`，例如冷启动 `v0.0.1`、加了数据 `v0.1.0`、稳定了 `v1.0.0`。起了以后 `SES-TX@v0.1.0` 和 `SES-TX@3` 是同一个版本，凡是写版本的地方都能用（`fw get`、建快照的 `--from`、登记模型的 `--trainset`、评估、预标注、发布、网页地址）。数据集、训练集快照、模型版本都能起；见 [fw label](#fw-label版本号)。

每个版本是一个目录的**完整快照**：导入的目录里有什么，这个版本就是什么。冻结以后谁也改不了，也删不掉；新的导入只会多出一个版本。

其他记录的写法：

| 记录 | 写法 | 例子 |
|---|---|---|
| 训练集快照 | 和数据集版本一样 | `SES-TX-det@2` |
| 训练集快照的一个划分（评估用） | `快照@序号:划分` | `SES-TX-det@2:test` |
| 模型版本 | `模型名@序号` 或 `模型名@版本号`，只写名称就是最新版 | `SES-TX-detr@3`、`SES-TX-detr@v1.0.0` |
| 评估报告 | 编号 | `12`（显示为 `#12`） |
| 发布 | `现场-产线-r序号`，平台按产线依次编号 | `SES-TX-r3` |

模型版本、评估报告和发布登记以后都不能改、不能删；只有标签（候选、弃用）可以改，每次改动都留记录。

## fw 命令

### fw login

```
fw login --server http://192.168.66.27:8000
```

记下平台地址和凭据，之后的命令都用它们。凭据在提示里输入，不会显示，也不会留在命令历史里；`--token <凭据>` 可以直接写在命令里（会留在历史里，脚本里用）。登录成功会显示账号和角色。

凭据由管理员在服务器上用 `fw-server token create` 发，每台机器一个。设置存在哪里见[环境变量和文件位置](#环境变量和文件位置)。

### fw whoami

看当前用的是哪个账号、哪个凭据、哪台服务器。换了机器或怀疑登错时先运行它。

### fw site：现场和产线

```
fw site list                          # 登记的现场和产线，每条产线有多少数据集、快照、模型、在办批次、发布
fw site add SES/TX --name 贴片线       # 登记 SES 现场的 TX 产线（SES 还没登记就一起登记）
fw site add SES --name 苏州厂          # 只登记现场
fw site name SES/TX 贴片一线           # 改显示名；代号不变
```

- 数据池、模型、发布只能建在**登记过**的现场和产线下，没登记的会被拒绝并提示先 `fw site add`。这样各处写的都是同一个名字，列表才能按现场、产线分组。
- 代号（`SES`、`TX`）只能用汉字、英文字母、数字、`_` 和 `.`，最多 64 个字，**登记后不能改、不能删**：它在训练集快照的目录（`<现场>/<产线>/...`）和发布编号（`SES-TX-r3`）里，这些都是冻结的。想换个叫法就改显示名（`fw site name`），显示名只是给人看的。
- 不分大小写：`ses/tx` 和 `SES/TX` 是同一条产线，新建的记录一律存成登记时的写法。
- 升级到这一版时，已有数据池、模型、发布用过的现场和产线自动登记好了（只差大小写的并成一个，取最早用的那个写法）；已有记录里存的字不改。现场或产线里有 `-`、空格这类不能当代号的旧数据集不会登记，仍然能用，在 `fw site list` 最后一行「没有归到登记的现场产线下的」里计数。
- 管理员和算法工程师能登记和改显示名，现场实施只能看。
- 测试集、回归集可以不写现场产线（不属于哪条产线）；写了就必须是登记过的。数据池必须写。

### fw dataset list

```
fw dataset list
fw dataset list --site SES --line TX       # 只看这条产线的
fw dataset list --search det               # 名称里有 det 的
```

列出数据集：类型、现场、产线、最新版本、文件数、大小、更新时间。`--site`、`--line`、`--search`（`-s`）可以一起用；`fw trainset list`、`fw model list`、`fw batch list` 也有这三个选项。训练集快照按它取过数据的产线算，所以跨产线的快照在每条来源产线下都能看到。

### fw dataset create

```
fw dataset create SES-TX --site SES --line TX
```

| 选项 | 说明 |
|---|---|
| `名称` | 必填，例如 `SES-TX` |
| `--site` | 现场，例如 `SES`；要先登记（[fw site](#fw-site现场和产线)） |
| `--line` | 产线，例如 `TX`；数据池必须写现场和产线，测试集、回归集可以都不写 |
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

**想往数据集里加一批图**：用 [`fw dataset add`](#fw-dataset-add)，只上传新的那个目录。`fw dataset import` 是把一个目录当作新版本的全部内容，只导入新的那批图会让新版本只剩那几张（预览会显示大量删除，这时选 N）。要改池里已有的文件（改标注、删几张图）时才需要先 `fw get` 取出最新版、改好、再整个目录 `fw dataset import`。

**中途断了**：上传断网、Ctrl+C、服务器出错都不会留下半个版本；重新运行同一条命令，已经传上去的文件不再传。按 Ctrl+C 后，还没开始的上传不再进行，命令在一两秒内打印「已中断」并以 130 退出（正在传的那几批由服务端丢掉或留作下次用，不会成为版本的一部分）。两个人同时往一个数据集导入时，后提交的会被拒绝，以新的最新版为父版本重新导入即可。

### fw dataset add

```
fw dataset add SES-TX E:\data\20261104
fw dataset add SES-TX E:\data\20261104 E:\data\20261105 --note "两天的新图"
fw dataset add SES-TX E:\data\night --to night/20261104
fw dataset add SES-TX E:\data\20261104 --replace
```

往数据集里加目录：新版本 = 最新版本 + 这些目录。只读、只上传这几个目录，池不用在本机；新版本由服务器在最新版本的基础上拼出来，池里原有的文件一个不动。

| 选项 | 说明 |
|---|---|
| `名称` `目录…` | 必填，目录可以写多个（一次最多 100 个），一起成为一个新版本。每个目录在池里用它自己的目录名：`E:\data\20261104` 进池后是 `20261104/` |
| `--to` | 放在池里的哪个目录，例如 `20261104`、`night/20261104`；只加一个目录时可用 |
| `--replace` | 池里已有同名目录时，整个换成这一份（旧目录里的文件在新版本里都没有了）。不加就拒绝 |
| `--note` `--dry-run` `--yes` `--allow-other-files` `--force` `--workers` | 和 `fw dataset import` 一样。`--dry-run` 会上传目录的文件清单（很小，服务器要靠它预览），不上传图片 |

**会拒绝的**：

- 池里已经有这个目录：`池里已有 20261104/（SES-TX@6，1,203 个文件）`。确实是同一天重传就加 `--replace`，是另一批数据就用 `--to` 换个名称。只差大小写的目录（`ABC` 和 `abc`）也拒绝，在 Windows 上它们是同一个目录。
- `fw dataset import` 的那些检查，对这几个目录照样做：读不了的目录、链接、用不了的文件名、同名不同扩展名的图片（`--force`）、图片和标注以外的文件（`--allow-other-files`）。池里原有的文件不再重新检查。
- 一次写的几个目录互相重复或一个在另一个里面；结果和最新版本完全一样。

**只提醒、不拦的**：

- `有 12 张图片和池里已有的内容完全相同`：按 SHA-256 比的，是同一个文件换了名称或目录。文件库里只存一份，但建快照时各算一个样本。
- `新增的目录里有 3 组内容完全相同的图片`。
- `其中 2 对图片相同、标注不一样`：同一张图两份标注对不上，训练时会互相矛盾，列出两边各有几个目标。JSON 按类别和框比（不管顺序和排版），YOLO txt 按行比；一边是 JSON 一边是 txt 时只比目标个数。为了比较，这些图的标注文件会在确认前先上传（很小）。

**池不存在目录结构时**（文件都在最上层）也可以用，加进去的目录和原来的文件并存。数据集还没有版本时，加进去的目录就是第一个版本。

预览后、确认前别人建了新版本，会拒绝，重新运行即可（以新的最新版本为基础）。

### fw dataset remove

```
fw dataset remove SES-TX 20261101
fw dataset remove SES-TX night/20261104 20261102 --note "拍糊了"
```

从数据集里去掉目录：新版本 = 最新版本去掉这些目录。目录写版本里的相对路径（区分大小写），可以写多个。`--note`、`--dry-run`、`--yes` 同上。

旧版本不变：去掉的只是新版本里没有，`fw get SES-TX@6` 仍能取回，用旧版本建的训练集快照也不受影响。把目录全部去掉、版本变空会被拒绝。只想删目录里的几个文件，取出最新版、删掉、再 `fw dataset import`。

### fw batch：标注批次

批次是一个要进数据池的目录，从上传到入库的经过都记在它上面。状态：待提交复核 → 待复核 → 已退回 / 复核通过 → 已入库；不继续的可以关闭。批次的每次上传都冻结成一版，不进数据池，也不会被删除。

```
# 现场实施（或算法工程师）：上传、标注、提交
fw batch upload D:\data\20261104 --pool SES-TX --labeler 张三                # 新批次 SES-TX-20261104，第 1 版
fw batch upload D:\data\20261104 --pool SES-TX --labeler 张三 --name tx-1104 --to night/20261104 --reviewer lisi
fw batch update SES-TX-20261104 D:\data\20261104 --labeler 张三 --note "标完了"   # 新的一版（整个目录现在的样子）
fw batch prelabel SES-TX-20261104 --wait                                      # 让产线现在发布的模型先画框
fw batch get SES-TX-20261104 --prelabel --to D:\work\20261104                 # 连同预标注取到本地，用 LiteAnno 改
fw batch submit SES-TX-20261104 --reviewer lisi                               # 提交复核
fw batch list --mine
fw batch show SES-TX-20261104                                                 # 各版、经过、复核意见

# 复核人（算法工程师）
fw batch list --todo                                                          # 待我复核
fw batch get SES-TX-20261104 --to D:\review\20261104                          # 取到本地，用 LiteAnno 打开
fw batch diff SES-TX-20261104                                                 # 这一版相比上一版改了哪些图、几个框
fw batch reject SES-TX-20261104 -m "b.bmp 漏标划痕" --image b.bmp
fw batch approve SES-TX-20261104 -m "可以"
fw batch approve SES-TX-20261104 --with D:\review\20261104 --labeler 李四      # 我改过了：带上改好的目录一起通过

# 入库（算法工程师）
fw batch import SES-TX-20261104            # 预览，确认后数据池多一个版本
fw batch close SES-TX-20261104 -m "重拍"   # 不继续了
```

- `upload`：`--pool` 是复核通过后进哪个数据池；`--labeler` 标注人姓名，必填，和账号一起记在这一版上；`--name` 批次名称（默认 `<数据池>-<目录名>`，只能用字母、数字和 `. _ -`，目录名是中文时要自己起）；`--to` 入库后在池里的目录名（默认目录自己的名称）；`--reviewer` 上传后直接提交复核。原始图片或已标注的都行，检查和 `fw dataset import` 一样（`--allow-other-files`、`--force` 同义），目录里要有图片。同一个数据池的同一个目录，同时只能有一个没结束的批次。
- `update`：上传批次目录现在的样子作为新的一版；只有「待提交复核」和「已退回」的批次能传，只有上传它的账号（或管理员）能传。内容和上一版完全一样不建新版。`--reviewer <账号>` 传完直接提交，`--reviewer -` 是上次的复核人。传完会列出相比上一版改了什么。
- `prelabel`：让模型给最新的一版预标注，是一个在 GPU 机上排队的推理任务。`--model <模型>@<序号>` 不写就用这个数据池所在产线现在发布的模型（还没有发布就必须写）；`--runner` 推理环境，能推理的只有一个时不用写；`--conf` 保留的最低分数（默认 0.1）；`--wait` 等到跑完。只有上传批次的账号（或管理员）能跑，只有「待提交复核」「已退回」的批次能跑；现场实施账号可以。同一版上同样的模型、推理环境和最低分数再提交不会重跑；新的一版要再提交一次，图片没变（只改了标注）时直接用上一版的结果，不重跑。一个批次同时只能有一个没跑完的预标注，要换模型或分数等它跑完。失败的原因现场实施账号看不到原文，请算法工程师在任务页看。进度看 `fw batch show` 或网页批次页。
- `get --prelabel`：取最新一版和它最近一次跑完的预标注：没有标注的图写上分数不低于 `--threshold`（默认 0.3）的框，标成未确认；已有标注的图保留原样，`--replace` 才换成预标注。取出来的是普通文件，改好后 `fw batch update` 上传成新的一版（预标注本身不会自动变成新的一版）。
- `submit`：复核人必须是能复核的账号（算法工程师、管理员）；不写 `--reviewer` 就是上次的复核人。算法工程师可以指定自己，记成「自审」。已退回的批次要先 `update` 再提交。
- `get`：`<批次>` 或 `<批次>@<版次>`，取出来的是普通文件（复制），可以直接改；`fw get <批次>@<版次>` 也行。
- `diff`：`--old`、`--new` 选版次（默认最新一版和它前一版），`--limit` 最多列多少张。一张图的旧框和新框重叠一半以上算同一个框：类别不同是「改类别」，位置不同是「挪动」，其余是「加框」「删框」。只改了格式、顺序的标注不算变化。YOLO txt 只比较检测框（5 列）。
- `reject`、`approve`：只有被指定的复核人（或管理员）能做。`--revision <版次>` 写明看的是第几版，这期间有了新的一版就不受理。退回必须写意见；`--image` 列出有问题的图片（批次目录里的相对路径，可以写多次）。`approve --with <目录> --labeler <姓名>` 把复核人改好的整个目录作为新的一版并通过。通过以后、入库之前还能 `reject`。
- `import`：只有「复核通过」的批次能入库，要有新建或修改数据集的权限。和 `fw dataset add` 是同样的检查和提示，`--replace`、`--allow-other-files`、`--force`、`--note`、`--dry-run`、`--yes` 同义；文件已经在服务器上，不用再传。数据池新版本的来源是 `batch:<批次>@<版次>`。
- `close`：上传它的账号、被指定的复核人和管理员能关闭；关闭后不能再改，各版内容保留。
- `list`：`--mine` 我上传的，`--todo` 待我复核的，`--status`、`--pool` 筛选。
- 现场实施账号能上传批次、传新的一版、提交、关闭自己的批次；不能复核、入库。

### fw label：版本号

```
fw label add SES-TX-det@2 v0.1.0 -m 加了 11 月的数据     # 给快照的第 2 版起版本号
fw label add SES-TX-det --bump minor                    # 给最新版起下一个次版本号（平台接着最高的往下排）
fw label add SES-TX-detr@3 v1.0.0 --model               # 模型版本要加 --model
fw label list SES-TX-det                                # 全部版本号，和下一个可以起什么
fw label retire SES-TX-det v0.1.0 -m 起错版本了          # 撤销（管理员）
fw trainset create SES-TX-det --from SES-TX@v0.3.0 --classes classes.txt --label v0.2.0   # 建的时候就起
fw model register SES-TX-detr --onnx best.onnx --trainset SES-TX-det@v0.2.0 --label v0.2.0
```

- 版本号只能是 `v主.次.修订`：小写 v，三段数字，不带前导 0（`v0.1.0` 可以，`v1.0`、`V1.0.0`、`v01.0.0`、`v1.0.0-rc1` 不行）。
- 数据集（数据池、测试集）、训练集快照、模型版本都能起；标注批次的各版不起版本号。预测集和任务只有编号。
- 一个版本可以有几个版本号（同一份数据既是 `v0.3.0` 也是 `v1.0.0`）。什么时候起都行，不用在建版本的时候。
- **版本号定了就不能改到别的版本上，也不能删**：写在报告里的 `SES-TX-det@v0.1.0` 永远是同一份数据。起错了由管理员 `fw label retire` 撤销：撤销后它不再指任何版本，这个版本号以后也不能再用，换一个新的起。
- `--bump patch | minor | major`：不用自己数，接着这个数据集（或模型）现在最高的版本号往下排；还没有时分别是 `v0.0.1`、`v0.1.0`、`v1.0.0`。撤销过的也算在内。
- 同一个版本号在每个数据集、每个模型里各起各的，互不相干。
- 谁能做：管理员和算法工程师起版本号（模型版本要有登记模型的权限）；只有管理员能撤销；现场实施只能看和用。
- `fw dataset list`、`fw dataset versions`、`fw trainset list`、`fw model versions` 里都有一列版本号。平台内部的记录（快照的来源、模型的快照、发布的模型版本）仍然存序号，所以撤销一个版本号不影响任何已有的记录。

### fw dataset versions

```
fw dataset versions SES-TX
fw dataset versions SES-TX --source
```

列出版本：父版本、文件数、大小、目录变化、时间、操作人、备注。「目录变化」是服务器建这个版本时记下的：`+20261104/`（add）、`换 20261102/`（add --replace）、`-20261101/`（remove），`整份导入` 是 `fw dataset import` 或网页「上传完整版本」。`--source` 同时显示每个版本是从哪台机器、哪个目录导入的。

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
| `--format` | 只用于训练集快照：`yolo`（默认，平铺）、`yolo-tree`、`coco`、`json`、`all`，见下文 |
| `--neg-ratio` | 只用于整个取回快照的默认格式：写进 `data.yaml` 的 `negative_setting.neg_ratio`，默认 0。平台原样写，不解释它的含义 |
| `--fix-dataset-length` | 同上，写进 `negative_setting.fix_dataset_length`，默认 0 |
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

**取训练集快照**：默认摆成平铺的 YOLO 目录，另生成 `data.yaml`，交给训练代码（rf-detr-pro、ultralytics 都能读）：

```
train/images/  train/labels/     train 里有框的图和它们的标注
neg_train/                       train 里没有框的图（负样本），不带标注
val/images/    val/labels/       val 的全部图，有框没框放在一起；没有框的图是空的 txt
test/images/   test/labels/      test 同上
classes.txt  trainset.json  data.yaml
```

每个目录都只有一层，文件名就是图片原来的文件名，数据池里的目录不保留。没有 `neg_val`、`neg_test`。`data.yaml` 里有 `negative_setting`（`extra_neg_sources` 的 `neg_train` 写实际张数，`neg_ratio` 和 `fix_dataset_length` 默认 0，可以用 `--neg-ratio`、`--fix-dataset-length` 给）。两张图文件名相同（来自不同目录或不同来源）时平铺不了，`fw get` 会列出这些文件，改用 `--format yolo-tree`，或者给文件改名后重建快照。

```
fw get SES-TX-det@2 --to D:\train\det2                   # 平铺：train/images、train/labels、neg_train、val、test、data.yaml
fw get SES-TX-det@2 --to D:\train\det2 --neg-ratio 0.2 --fix-dataset-length 5000
fw get SES-TX-det@2 --to D:\train\det2-tree --format yolo-tree  # 0.3.0 以前的默认：images/train/…、labels/train/…，保留数据池的目录
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
fw trainset create SES-TX-det --from SES-TX@4 --classes classes4.txt --label-source txt \
    --label-classes classes5.txt --map others=        # txt 按 5 类编号，others 当背景，快照只要 4 类
```

从一个或几个数据池版本建训练集快照：每张图分到 train、val 或 test，连同类别表一起冻结。训练时取这个快照，模型登记时带上它，以后就能从模型查回训练用的每个文件。目录和文件的格式见[训练集快照的格式](trainset-format.md)。

先预览：每个划分多少张（其中多少张没有框，train 里没有框的取回时放在 `neg_train/`）、每个类别在每个划分里多少个框，接着上一版时还有新增、移除、换了划分、标注改了的个数，还有生成了哪几种标注格式。确认后才建。建的时候服务器把标注转成 YOLO 和 COCO 检测框，和图片、原始 JSON 一起冻结（转换规则见[训练集快照的格式](trainset-format.md#标注怎么转)）。同一个名称再运行一次，就是这个快照的下一版，默认**接着上一版的划分**：原来的样本不换划分，新样本进 test。

| 选项 | 说明 |
|---|---|
| `--from` | 必填，来源数据池版本，例如 `SES-TX@4`；几条产线合成一个快照就写几次。来源数据集要填了现场和产线 |
| `--classes` | 必填，类别表文件：一行一个类别名，行号（从 0 起）是类别编号，YOLO 和 COCO 都用它。标注里有表以外的类别，或者 YOLO 编号超出范围，就不建 |
| `--val` | 按比例划分时 val 的比例，默认 0.2 |
| `--test` | 按比例划分时 test 的比例，默认 0（不分 test）。7:2:1 就是 `--val 0.2 --test 0.1`。有框的图和没有框的图分别按比例分，所以 val、test 里也有相应比例的负样本。不能和 `--merge-test`、`--split-file` 一起用 |
| `--seed` | 种子，默认 0。同样的样本、比例和种子，分法总是一样 |
| `--test-dirs` | 这些目录下的新样本进 test，例如 `2026-10`，或带上现场和产线 `SES/TX/2026-10`；逗号分隔或写多次 |
| `--merge-test` | 上一版 test 里的样本重新分到 train/val（比如上一版的 test 已经测完了，要并进来训练） |
| `--new-to` | 接着上一版时新样本去哪：`test`（默认）或 `trainval` |
| `--split-file` | 用自己的划分清单：每行一个样本「`<现场>/<产线>/<图片路径>` 制表符或空格 `train`、`val` 或 `test`」，`#` 开头的行不算。清单要正好包括来源里的全部图片。用清单时不算接着上一版（`based_on` 是空的） |
| `--fresh` | 不接着上一版，全部重新划分 |
| `--require-labels` | 每张图都要有标注：有没标注的图就不建，预览里列出来。默认没有标注的图当负样本收进来 |
| `--label-source` | `json` 或 `txt`：一张图同时有 JSON 和 txt 标注时用哪一种（例如 `images/a.json` 和 `labels/a.txt`）。不写时遇到这样的图就不建 |
| `--label-classes` | txt 标注自己的类别表文件（行号是 txt 里的类别编号），和 `--classes` 不一样时用；不写就按 `--classes` 读 |
| `--map` | `源=目标`，可写多次：标注里的「源」类别算作类别表里的「目标」，例如 `DL1=DL`；目标留空就去掉这类框、当背景，例如 `others=`。只改快照里的标注，数据池不变 |
| `--note` | 备注 |
| `--dry-run` | 只预览，不建 |
| `--yes` | 不问确认 |

- 标注怎么配对：同目录同名的 `.json` / `.txt`，或者 YOLO 的 `images/` 对应 `labels/` 的 `.txt`（数据池按 YOLO 目录放的也能直接建）。详见[标注怎么配对](trainset-format.md#标注怎么配对)和[类别映射](trainset-format.md#类别映射)。
- 「标注改了」：这个样本这一版冻结的 `json/` 或 `labels/` 文件和上一版不一样，多了或少了一个也算（例如这一版因为有图读不出尺寸而没生成 LiteAnno JSON）。数据池里的标注改了、换了 `--label-source`、映射或类别表变了，只要冻结的文件变了都算。
- 用了 `--map` 时，预览里列出每条映射去掉或并进了多少个框；映射的源类别在标注里一个框也没有就不建（多半写错了，类别名区分大小写）。
- 名称和数据集共用：已经有叫这个名的数据集，就不能用来建快照。
- 快照只能用这条命令建，`fw dataset import` 往快照里导入会被拒绝。
- 和最新版完全一样（样本、划分、标注、类别表都没变）时不建。
- 这一版只做检测：`point`、`line`、`circle` 这类形状不能当检测框，有就不建；`polygon`、`rotation` 按外接矩形当检测框。
- 有图片读不出尺寸时（不是 PNG、JPEG、BMP、GIF、TIFF、WebP，又没有 JSON 标注）不生成 COCO；数据池里是 YOLO txt 标注的图，建快照时从框生成 LiteAnno JSON，这些图读不出尺寸时不生成 JSON 格式。预览里写着原因。
- `--from` 只写名称（或 `@latest`）时，预览时定下是哪个版本，确认后冻结的就是那个版本；预览和确认之间有人导入了新版本也不影响。
- 预览之后有人建了同名快照的新版本（或者同时有人建同名的第一版），就不建，重新运行一次即可。第一版没建成时名称也不会被占住。
- 同一现场和产线的两个来源里有同名不同扩展名的图片（例如 `same/a.png` 和 `same/a.jpg`）时不建：它们的 YOLO 标注会是同一个文件。
- 没有 train 或 val 样本时预览会提醒：这样的快照只能当测试集，不能直接训练。
- 有文件名在平铺后会重名时预览会提醒（照样能建）：这个快照取回时只能用 `--format yolo-tree`。
- 服务器要读每个标注文件核对类别，几十万张图时预览和建快照各要一些时间。

### fw trainset list 和 fw trainset show

```
fw trainset list                 # 全部快照和它们的最新版
fw trainset show SES-TX-det@2    # 来源、划分规则、读标注的规则（映射等）、每个划分的张数和每类框数
```

取回用 `fw get SES-TX-det@2 --to <目录>`，导出、核对、对比也和数据集版本一样（`fw export`、`fw verify`、`fw dataset diff`）。对比两个快照时，换了划分的样本显示成改名。`fw dataset list` 里也列出快照，类型是「训练集快照」。

### fw model create、list、versions、show

```
fw model create SES-TX-detr --site SES --line TX     # 一条产线上的一种模型；--task 现在只有 det（检测）
fw model list                                        # 全部模型和它们的最新版
fw model versions SES-TX-detr                        # 每个版本的快照、ONNX、代码提交号、标签
fw model show SES-TX-detr@3                          # 文件、类别、训练代码、验证指标、评估报告、发布
```

模型名称和数据集一样只能用字母、数字和 `. _ -`，不分大小写，不能重名。现场和产线要先登记（[fw site](#fw-site现场和产线)），发布登记到的现场产线也一样。现场和产线只能用汉字、英文字母、数字、`_` 和 `.`（如 `现场1`、`线_2.a`；汉字指 CJK 统一汉字及扩展 A 区，全角符号不算；数据库按同一条规则检查）：发布编号是 `<现场>-<产线>-r<序号>`，有 `-` 会撞号（`A-B`/`C` 和 `A`/`B-C`），有 `/`、`@`、空格等查不了。

### fw model register

```
fw model register SES-TX-detr --onnx runs/best.onnx --weights runs/best.pt --trainset SES-TX-det@2 \
  --file configs/train.yaml
```

把 ONNX 和权重传到服务器，登记成模型的下一个版本（`SES-TX-detr@3`）。模型好不好以平台用 aoi-deteval 统一算的指标为准（第三步由 GPU worker 用从 ONNX 构建的 TensorRT 引擎评估），不看各训练框架自己算的指标，所以指标和训练代码都是选填。先传文件、后登记，所以登记了的模型版本，文件一定在服务器上。

| 选项 | 说明 |
|---|---|
| `--onnx` | 必填，ONNX 文件，用来构建引擎。文件开头不像 ONNX 时拒绝（常见的是传成了 `.pt`）；有张量把数据放在别的文件里（external data）也拒绝，平台只收单个文件的 ONNX（2GB 以下的模型 `torch.onnx.export` 默认就是单文件） |
| `--weights` | 训练出的权重（`.pt`、`.pth`，一个文件，不能是空文件），登记在 `weights/` 下，只作存档（以后接着训练、对照用）；平台从不加载它，评估、推理都用 ONNX 建的引擎 |
| `--trainset` | 必填，训练用的训练集快照，例如 `SES-TX-det@2`。追溯链就是从这里接到数据的；类别表也用这个快照的，登记时一起放进模型版本 |
| `--metrics` | 选填：训练框架自己算的验证指标 JSON（一个 JSON 对象），只作参考；`fw model show`、`fw model compare` 会列出里面的数值 |
| `--file` | 附件：训练配置等，登记在 `files/` 下；写目录就带上整个目录；可写多次 |
| `--code-dir` | 选填：训练代码的 git 目录，记下它的 HEAD 和 origin；有没提交的改动时会提示，并记在这个版本上 |
| `--code-commit`、`--code-repo` | 选填：不用 `--code-dir` 时直接写提交号（和仓库地址）。只写仓库地址不写提交号会被拒绝。地址里带的账号和令牌（`https://user:token@…`）会去掉，不会记下来 |
| `--note` | 备注 |

- 同一个模型里，一个 ONNX 只登记一次。同样的 ONNX、快照、代码和文件再登记一次（脚本跑了两遍，网络断了重试），返回已有的那个版本，不会多出一个；ONNX 一样而别的不一样就拒绝。
- 训练代码三个选项都不写时不记，`fw model show` 和追溯链里显示「未记录」。不会自动读当前目录的 git：当前目录可能是别的仓库。
- 模型版本里的文件：`model.onnx`、`classes.txt`（快照的类别表）、`weights/<权重文件>`（有 `--weights` 时）、`metrics.json`（有 `--metrics` 时）、`files/…`（附件）。
- 引擎不在这里登记：上传的引擎不一定能在部署的机器上用，引擎要从 ONNX 在目标环境里构建（评估和预标注时 GPU worker 用登记的 onnx2engine 在本机建，只是那台机器的缓存；现场自己从同一个 ONNX 构建，发布时记下 GPU、TensorRT 版本和引擎的哈希，见 `fw release register`）。

### fw model get

```
fw model get SES-TX-detr@3 --to /opt/deploy/models/SES-TX-detr@3
```

把模型版本的文件取到一个空目录：`model.onnx`、`classes.txt`、`weights/…`、`metrics.json`、`files/…`（有哪些取哪些），另外生成 `fw-model.json`（不在模型版本里：版本编号、快照、类别表、ONNX 哈希、训练代码提交号）。和 `fw get` 一样走本机缓存，每个文件按清单核对；`--copy`、`--hardlink` 也一样。部署账号也能用，但只能取模型版本的文件，取不了数据集。

### fw model tag、fw model compare

```
fw model tag SES-TX-detr@3 candidate                      # 标成候选
fw model tag SES-TX-detr@2 deprecated --note '漏检多'      # 标成弃用
fw model tag SES-TX-detr@3 candidate --remove             # 去掉标签
fw model compare SES-TX-detr@2 SES-TX-detr@3              # 对比验证指标
fw model compare SES-TX-detr@2 SES-TX-detr@3 --testset SES-TX-det@2:test   # 对比在同一个测试集上的评估
```

- 标签只有 `candidate`（候选）和 `deprecated`（弃用）两种，不做「阶段」流转；「产线现在用哪个模型」看发布（`fw release current`）。每次加、去标签都留一条记录，`fw model chain` 里能看到。
- `compare` 把两边指标 JSON 里的数值摊平（`box.map50` 这样带点的键）逐项对比，最后一列是新减旧。`--testset` 时用两个版本在这个测试集上最近的一份评估报告。

### fw model chain

```
fw model chain SES-TX-r3 --check       # 从一次发布查起
fw model chain SES-TX-detr@3           # 从一个模型版本查起
```

追溯链一级一级打印：发布（部署仓库和提交号、现场怎么构建的引擎）→ 模型版本（ONNX 哈希、训练代码提交号、标签、评估报告）→ 训练集快照（清单哈希、类别、各划分张数）→ 数据池版本（清单哈希、从哪里导入、备注）。每一级都有编号、哈希、登记人和时间。

`--check` 顺便核对链上每个文件都还在文件库里、大小对（模型文件、评估报告的文件、快照和数据池版本的每个文件）；有问题时列出来，退出码 1。重算哈希用 `fw-server verify --rehash`。

### fw eval run

```
fw eval run SES-TX-detr@3 --data SES-TX@5 --dirs SES/TX/test_1015 SES/TX/test_1016 --runner detr-pro
fw eval run SES-TX-detr@3 --data SES-TX-det@2:test --runner detr-pro --wait
```

排一个评估任务：推理环境所在的 GPU 机用模型版本的 `model.onnx` 在本机建 TensorRT 引擎（onnx2engine，FP16），用部署代码的推理模块跑完全部测试图，写成 aoi-deteval 的预测文件，再跑 `deteval evaluate`，结果自动登记成评估报告（`fw eval show` 看）。指标以平台用 aoi-deteval 算的为准，量的就是部署时那条 TensorRT 推理。

| 选项 | 说明 |
|---|---|
| `模型版本` | 例如 `SES-TX-detr@3`；只写名称就是最新版，提交时定下。评的是它的 `model.onnx`（输入尺寸由 ONNX 定） |
| `--data` | 在哪个版本上评。数据集版本（数据池、测试集）直接写，例如 `SES-TX@5`；训练集快照写划分，例如 `SES-TX-det@2:test` |
| `--dirs` | 只评数据集版本里的这几个目录（连同下面所有子目录），写版本里的相对路径；不写就是整个版本。快照按划分评，不用写 |
| `--runner` | 推理环境，例如 `detr-pro` 或 `detr-pro@2`；登记时要写了 `--framework` 和 `--onnx2engine` |
| `--wait` | 等任务跑完，跟着显示输出，最后给出评估报告编号 |

- 读哪些标注：快照用它的 YOLO 标注，类别编号按快照的类别表；快照的类别表要和模型的完全一样（顺序也一样），不一样就不收（否则对的框会被算成错的类别）。数据集版本用每张图旁边的 LiteAnno / X-AnyLabeling JSON，按类别名对到模型的类别表，有模型类别表里没有的类别名（包括只差大小写的）就不收（扩展名大小写都认，`x.JSON` 摆成 `x.json` 给 aoi-deteval）；没有标注的图算正常图（没有缺陷）。只有 YOLO txt（`.txt`、`.TXT` 都算）的图不收（txt 的类别编号是数据池自己的，不一定和模型的一样），先用 `fw trainset create --label-classes` 建快照，再评快照的划分。同一个目录里同名不同扩展名的两张图（`a.png`、`a.jpg`）会共用一份标注，也不收。
- 提交时就定下要评哪些图：图片清单存进文件库，任务重试、版本以后再变都还是这些图。评完的张数和清单对不上（有图没读到），不登记，任务算失败。
- 选的图里有和模型训练集快照 train、val 里内容相同的图（按文件哈希，不看路径），提交时提醒，并写进报告的备注：这些图的成绩会偏好。
- worker 先核对推理环境还是登记时那样（包清单、源码、onnx2engine 程序），变了就不评；确认没问题后在 GPU 机上重新 `fw runner register`，再提交。
- 本机的引擎只是缓存：按 ONNX、onnx2engine 程序、环境、显卡和驱动存在 fw 缓存目录的 `engines/` 下，同一台机器同一个 ONNX 只建一次；不上传、不发布。发布到现场的是 ONNX，现场用同一个 onnx2engine 自己建引擎（本机建的引擎换了显卡或 TensorRT 版本不一定能用；同样的显卡和版本，TensorRT 每次建 FP16 引擎挑的算子实现也可能不同，所以现场的引擎和本机的结果会有很小的差别）。报告的 `files/engine.json` 记下引擎的哈希、显卡和驱动、TensorRT 版本、精度（精度是 onnx2engine 自己设的 FP16，平台没有从引擎里读出来核对，所以记成 `precision_from: onnx2engine`）和推理计数（`predict`：图数、留下的框、去掉的框）。
- 引擎写进缓存前先落盘（fsync）再改名，最后写旁边的 `<名字>.engine.sha256`；用缓存前核对，对不上（比如断电留下的空文件、被改过）就删掉重建。推理模块加载不了引擎时也删掉重建一次再试。
- 只收单文件 ONNX：worker 建引擎前再查一遍 ONNX 里有没有 external data（TensorRT 解析 ONNX 时会按模型里写的路径去读那个文件），有就不建；ONNX 单独复制到一个空目录里给 onnx2engine。
- 缓存不会自己清：`engines/` 下的文件随时可以删（删了下次评估再建），换显卡、换环境以后旧引擎就用不上了，可以定期清掉。
- 用哪块显卡：`CUDA_VISIBLE_DEVICES` 的第一块，没设就是 0 号；worker 跑 onnx2engine 和推理时设 `CUDA_DEVICE_ORDER=PCI_BUS_ID`，这样 CUDA 和 `nvidia-smi` 的编号一致，引擎缓存按的显卡就是实际跑的那块。
- 推理时置信度下限 0.001（算 mAP 要几乎所有框，和 `deteval predict` 一样）；引擎输出的类别超出模型类别表的框不计入，日志和 `engine.json` 里写去掉了多少个；去掉的比留下的还多时任务失败（ONNX 和登记的类别表多半对不上），不登记报告。
- aoi-deteval 一个集合只读一层目录，按文件名对应预测和真值，所以 worker 把每个有图的目录作为一个集合（`d1`、`d2`…），再把它们合成一组 `eval`；登记的指标是 `eval` 这一组的（只有一个目录时就是那个目录）。工作点阈值（f1 等）也是在这一组上定的，看 mAP 不受影响，看阈值类指标时要知道这一点。
- 报告的文件：`metrics.json`，`files/` 下是 aoi-deteval 的整个输出目录、用的集合配置（`eval_sets.yaml`）、预测 JSON（`preds/`）和引擎信息（`engine.json`）。`fw eval show` 会写明是哪个评估任务、哪个推理环境算的。
- 取消、租约失效的评估不登记报告。两个任务算出的结果一字不差时，各自登记一份报告（文件只存一份），报告里各记各的任务和推理环境。
- 平台从不加载上传的权重（`.pt`、`.pth` 是 pickle，加载时会执行里面的代码）：worker 只拿到 ONNX，ONNX 里没有能执行的代码。

### fw eval register、list、show

```
fw eval register SES-TX-detr@3 --testset SES-TX-det@2:test --deteval results/
fw eval register SES-TX-detr@3 --testset SES-TX-det@2:test --deteval results/ --deteval-model detr_s0
fw eval register SES-TX-detr@3 --testset SES-TX-test@1 --metrics my_eval.json --tool mytool --tool-version 1.0
fw eval list SES-TX-detr
fw eval show 12
```

登记一个模型版本在测试集上的评估结果。平台这一步不自己算分，记下的是你们评估得到的结果和文件。

| 选项 | 说明 |
|---|---|
| `--testset` | 必填，测试集版本。训练集快照要写明划分，例如 `SES-TX-det@2:test`；别的数据集版本（例如测试集 `SES-TX-test@1`）不写划分 |
| `--deteval` | aoi-deteval `evaluate` 的输出目录：指标取自 `summary.json` 里这个模型在**这个测试集**上的那一行（加上阈值等），工具版本取自 `run_info.json`，整个目录（`metrics.csv`、`per_image.csv`、`report.txt` 等）都登记在 `files/` 下。`summary.json` 里没有定义的指标（aoi-deteval 写成 `NaN`，例如测试集里没有某个类别）登记为 `null`，原文件不改 |
| `--deteval-model` | `summary.json` 里有几个模型时，这个模型版本对应哪一个（记在备注里） |
| `--deteval-set` | aoi-deteval 结果里哪个集合就是这个测试集（记在备注里）。不写时：快照取和划分同名的集合（`:test` 就是 `test`）；数据集版本取结果里唯一的那个集合 |
| `--metrics` | 不用 aoi-deteval 时，指标 JSON（一个 JSON 对象，标准 JSON：没有定义的值写 `null`，不能写 `NaN`）；和 `--deteval` 二选一 |
| `--file` | 另外附带的报告文件或目录；可写多次 |
| `--tool`、`--tool-version` | 评估工具和版本；用 `--deteval` 时自动填 |
| `--note` | 备注 |

- 同样的模型版本、测试集和文件再登记一次，返回已有的那份报告。
- 模型版本只写名称时，先定下是哪个版本（例如 `SES-TX-detr@3`）再登记；网络断了重试，也还是登记在这个版本上。
- 用 `--deteval` 时，先把整个输出目录复制一份，指标从这份复制里读，归档的也是这份：登记途中评估程序再改、删、加文件都不影响这次登记。
- 用 `--deteval` 时，选中的集合评的图片数要和测试集一样（快照数 `images/<划分>/` 下的图，数据集版本数全部图片），不一样就不登记：val 的报告不会被登记成 test 的成绩。只核对张数，不核对是哪几张：用 `fw get SES-TX-det@2 --to <目录>` 取出来再评，评的就是它。aoi-deteval 只读一层目录（`list_images` 不递归），而且按文件名（不带目录）对应预测和真值；默认的平铺格式里 `test/images/` 正好是一层、文件名不重复。

### fw predict run、list、show、get

```
fw predict run SES-TX-detr@3 --data SES-TX@6 --dirs SES/TX/new_1109 --runner detr-pro --wait
fw predict get 7 ~/label/new_1109                   # 图片 + 预标注，LiteAnno 直接打开
fw predict get 7 ~/label/new_1109_t5 --threshold 0.5 --replace
```

预标注分两步：`fw predict run` 排一个推理任务，GPU 机用模型版本的 TensorRT 引擎（和评估同一套：本机从 ONNX 建、缓存）跑选中目录里的全部图片，结果存成不可变的「预测集」；`fw predict get` 把预测集按分数阈值写成 LiteAnno / X-AnyLabeling 的 JSON，和图片一起放到一个目录里，用 LiteAnno 打开就能改。

| 选项 | 说明 |
|---|---|
| `模型版本` | 例如 `SES-TX-detr@3`，推理的是它的 `model.onnx` |
| `--data` | 数据集版本（数据池等），例如 `SES-TX@6`；训练集快照不收 |
| `--dirs` | 只推理这几个目录（连同子目录）；不写就是整个版本 |
| `--runner` | 推理环境，要登记过 `--framework` 和 `--onnx2engine` |
| `--conf` | 预测集里保留的最低分数，默认 0.1；以后预标注的阈值只能在它之上调，要更低就重新推理 |
| `--wait` | 等任务跑完，最后给出预测集编号 |

- 同一个模型版本、同一个推理环境版本、同样的图片（按清单哈希）和同样的 `--conf` 再提交，直接返回已有的任务（排队中、运行中或已完成），不再占 GPU；任务失败、取消或正在取消才会重新排。复用不看引擎哈希：提交时引擎还没建，同一推理环境版本在同一台机器上，只有换显卡、换驱动或缓存重建时引擎才会不同；要强制重跑就换一个 `--conf`（以后网页上加「重新推理」）。
- 部署代码自己按 conf 过滤的方式不一定和平台一样（比如用 `>`，或 FP16 下分数有舍入），`trt_predict.py` 写出前再按「分数不低于 conf」过滤一遍。
- worker 交回 `predictions.json`（每张图在版本里的路径、宽高和框 `[x1, y1, x2, y2, 分数, 类别]`）和 `engine.json`；平台核对每张图都在、只有这些图、类别在模型的类别表里、分数不低于 `--conf`，都对才登记预测集。
- 同一个目录里同名不同扩展名的两张图（`a.png`、`a.jpg`）会写进同一个 `a.json`，不收。
- `fw predict list [模型]`、`fw predict show <编号>` 看预测集。

`fw predict get <编号> <目录>`：

| 选项 | 说明 |
|---|---|
| `--threshold` | 分数阈值，默认 0.3；不能低于预测集的 `--conf` |
| `--replace` | 已有 JSON 标注的图也写预标注（默认保留已有标注原样）；模型在这张图上没有够阈值的框时，仍保留原来的标注，不会让它消失 |
| `--link`、`--workers` | 同 `fw get` |

- 目录里是预测集的全部图片（保持版本里的目录结构）。有框的图旁边写一个 JSON：矩形框、`score` 是分数、`checked` 为 `false`（未确认，LiteAnno 里由人确认），顶层的 `flywheel` 记下预测集编号、模型版本、推理环境和阈值（LiteAnno 和 X-AnyLabeling 都会原样保留不认识的顶层字段）。框裁到图片范围内。
- 已有标注的图默认保留原来的 JSON，不写预标注；`--replace` 时换成预标注（原来的 `x.JSON` 之类会删掉，一张图只留一个 JSON），没有够阈值的框就保留原来的。没有标注也没有框的图不写 JSON。输出的统计分开列出写了预标注、换掉原标注、保留原标注的张数。
- 顶层写一个 `classes.txt`（模型的类别表，一行一个），LiteAnno 的普通文件夹模式导出时用；版本里已有 `classes.txt` 就不写。
- 改好的标注再用 `fw dataset import` 导入成数据池的新版本。批次复核做好以后，预标注会直接写回批次。

### fw release register、list、current、show

```
fw release register --model SES-TX-detr@3 --deploy-repo git@git.example.com:deploy/trt.git \
  --deploy-commit 89abcdef --deployed-at 2026-11-20T10:30+08:00 --gpu RTX3070 --trt 10.3 --precision fp16 \
  --engine-file /opt/deploy/engines/model_fp16.engine
fw release list --site SES --line TX       # 按部署时间排
fw release current --site SES --line TX    # 这条产线现在用的是哪个模型
fw release show SES-TX-r3                  # 这次发布和它的追溯链
```

部署成功后由部署仓库（用现场实施账号）登记一次发布，平台给它一个编号 `SES-TX-r3`。

| 选项 | 说明 |
|---|---|
| `--model` | 必填，部署的模型版本 |
| `--site`、`--line` | 现场和产线；不写就是模型的 |
| `--deploy-repo`、`--deploy-commit` | 必填，部署仓库地址和 git 提交号。地址里不能带账号和令牌 |
| `--deployed-at` | 部署成功的时间，例如 `2026-11-20T10:30+08:00`；不写时区按这台机器的时区，不写就是现在。不能比服务器的时间晚 |
| `--gpu`、`--trt`、`--precision` | 现场从 ONNX 构建引擎用的 GPU、TensorRT 版本、精度（`fp32` 或 `fp16`） |
| `--engine-file` | 现场构建的引擎文件：只记它的 sha256 和大小，文件不上传（引擎留在现场） |
| `--note` | 备注 |

- 「产线现在用哪个模型」是**部署时间最晚**的那次发布，不是最后登记的那次：补登一次以前的发布，不会顶掉后来真正部署的那次。
- 同一条产线、同一个模型版本、同一个部署提交号和部署时间再登记一次（网络断了重试），返回已有的那次；这些相同而 GPU、引擎等不一样就拒绝。
- `--model` 只写名称时，先定下是哪个版本再登记，重试也不会换成期间新登记的版本。
- 现场和产线只能用汉字、英文字母、数字、`_` 和 `.`（见 `fw model create`）。
- 只有现场实施账号和管理员能登记发布；算法工程师能看。

### fw runner register、list、show、check

推理环境就是 GPU 机上搭好的 conda 环境，一个框架一个（例如 `yolo26`、`detr-pro`）。平台只做 TensorRT 推理，推理环境都在 NVIDIA GPU 机上。评估（`fw eval run`）和推理任务（`fw predict run`）在提交时指定的推理环境里跑。

```
fw runner register detr-pro --env detr-pro --framework rfdetr --onnx2engine /opt/deploy/onnx2engine   # 在 GPU 机上，用 worker 账号
fw runner list
fw runner show detr-pro
fw runner check detr-pro                                              # 算法工程师：提交一个检查任务
```

| 选项 | 说明 |
|---|---|
| `名称` | 推理环境名：字母、数字和 `. _ -`，全平台唯一 |
| `--env` | conda 环境名 |
| `--src` | 框架源码目录（git 仓库），要记下哪个提交时写；不写也行 |
| `--framework` | 用哪个部署代码的推理模块跑 TensorRT 引擎，现在只有 `rfdetr`（`py_rfdetr`）。要用来评估的环境必须写；改了也是新版本 |
| `--onnx2engine` | 部署代码的 onnx2engine 程序：只带一个参数（ONNX 路径）运行，在 ONNX 旁边写出同名的 `.bin` 或 `.engine`（两个都有或都没有算失败）。RF-DETR 部署代码里登记静态的 `onnx2engine_st`；`onnx2engine_dy` 还要一个 batch 参数，用不了。和 `--framework` 一起写；登记它的路径和文件哈希，程序换了（重新编译）就是新版本。建议放在 `--src` 指的部署仓库里编译：哈希只管程序文件本身，它用到的动态库（TensorRT、自家的 .so）在环境或仓库里，这样 `fw runner check` 能一起看出变化 |

评估用的环境里要有：OpenCV（`cv2`）、部署代码的推理模块（`py_rfdetr`，能 `import`）、aoi-deteval（只用 `deteval evaluate`）。onnx2engine 在这个环境里运行（`conda run`），所以它找得到环境里的 TensorRT 库；机器上要有 `nvidia-smi`。TensorRT 版本（进引擎缓存的键和 `engine.json`）取 onnx2engine 实际链接的 libnvinfer 的文件名（`ldd` 看到的 `libnvinfer.so.10` 指向的 `libnvinfer.so.10.x.y`）；读不出来（静态链接、没有 `ldd`）才用环境里 `tensorrt` 这个 Python 包的版本，这时要装和 libnvinfer 同版本的；两样都没有任务失败并说明。任务目录里的图片在 Linux 上是缓存的硬链接（文件系统支持 reflink 就用 reflink，跨盘才复制），不多占一份空间；任务跑完目录就删。

- 登记时 fw 运行 `conda list -n <环境> --explicit`（conda 装的包）和在环境里运行 `python -m pip freeze --all`（pip 装的包，conda 的清单里没有），把这份包清单传到平台、记下它的哈希（包清单里网址带的账号密码、`?token=...` 这类参数和 conda 的 `/t/<token>/` 先去掉再传，私有源的凭据不会被别的账号看到；包的哈希保留）；写了 `--src` 就记下源码的 git 提交号，有没提交的改动时再记下改动内容的哈希（`git diff HEAD` 加上没被 `.gitignore` 忽略的新文件），所以已经有改动的源码再改一次也看得出来。找 conda 的顺序：环境变量 `FW_CONDA`、PATH 里的 `conda`、`CONDA_EXE`。
- 用 `pip install -e` 装的包，pip 的清单只记它的位置，不记它的代码；这样装的框架源码用 `--src` 登记，改动才看得出来。
- 包清单、源码提交号或改动、所在机器有任何一样和最新一版不同，就登记成新版本（`detr-pro@2`）；完全一样就说「没有新版本」。版本只增不改。
- 迁到别的 GPU 机：在那台机器装好同名环境，用它的 worker 账号再登记一次，新版本记的是新机器；以后的任务都发给那台机器。
- `fw runner check` 提交一个任务：那台 GPU 机上的 worker 重新读包清单和源码，和登记的比（onnx2engine 程序算在环境里），结果用 `fw job show` 看（「包清单没变 / 变了」「源码没变 / 变了」）。有人在环境里装了包、改了源码，这样就能看出来；要以新状态为准就再 `register` 一次。

### fw job list、show、log、cancel

```
fw job list                      # 最新的在前；--status queued|running|done|failed|cancelled
fw job show 12                   # 状态、每次领取（哪台机器、怎么结束的）、结果或失败原因
fw job log 12 -f                 # 看输出，-f 一直跟到任务结束；--attempt 2 看第 2 次领取的输出
fw job cancel 12
```

- 任务按提交顺序排队，绑定了推理环境的任务只发给那个环境所在的机器。`fw job list` 里排队的会写「前面 N 个」。
- 取消：排队中的立即取消；运行中的，worker 在下次续租时（默认 20 秒内）停下正在跑的命令（连同它启动的子进程），报告「已取消」。取消受理以后，即使命令恰好在这之前跑完、worker 交了结果，任务也记成已取消。
- `fw job log -f` 跟到任务被重新领取时，接着显示新一次领取的输出（前面有一行「第 N 次领取」）。
- 领取次数：worker 掉线、死机或被停掉，租约（默认 60 秒）到期后任务自动回到队列，换一次领取；同一个任务领取 3 次都失联就算失败。worker 自己报告的失败（命令出错）不重试，看 `fw job show` 的原因，改好再提交。
- 状态：排队、运行中、完成、失败、已取消。结束的任务不会再变。

### fw worker

在 GPU 机上常驻，一个接一个领任务、运行、交回结果：

```
fw login --server http://<平台服务器>:8000          # 用 worker 账号的凭据
fw worker
```

| 选项 | 说明 |
|---|---|
| `--poll` | 没有任务时隔几秒问一次，默认 5 |
| `--once` | 把这台机器能做的任务做完就退出（测试、手动跑时用） |

- 要用 worker 账号（`fw-server user add gpu-01 --role worker`）。worker 只领任务、续租、传日志、交结果，不能提交任务。
- 每次领取有自己的令牌和租约；worker 每 20 秒续一次。平台说租约已收回，或者连不上平台、按这台机器的时钟租约已经到期，worker 就停下正在跑的命令（连同子进程），这次的结果不交；平台那边同时把任务放回队列，所以同一个任务不会在两处同时跑。
- 命令自己结束时，它留下还在跑的子进程也一起杀掉，任务不会被它们拖住。
- 平台重启、网络断开时 worker 不退出：领任务连不上就每隔 `--poll` 秒再试；交结果连不上或平台返回 5xx 就重试并续租，最多 10 分钟，交不上就不再续租，租约到期后任务回到队列（重试期间收到取消就改交「已取消」）。只有凭据失效这类问题才退出。
- Ctrl+C 立即退出；正在跑的任务在租约到期后回到队列。

常驻运行用 systemd（以 worker 账号 `fwworker` 为例，写到 `/etc/systemd/system/fw-worker.service`）：

```
[Unit]
Description=fw worker
After=network-online.target
Wants=network-online.target

[Service]
User=fwworker
ExecStart=/home/fwworker/.local/bin/fw worker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`sudo systemctl daemon-reload && sudo systemctl enable --now fw-worker`；看输出用 `journalctl -u fw-worker -f`。`ExecStart` 写 `which fw` 给出的路径。`Restart=always` 保证 worker 万一退出（升级、出错）也会重新起来。

## 在训练脚本里用

```python
import fw

ts = fw.get_trainset('SES-TX-det@2', '/data/train/SES-TX-det@2')   # 取训练集快照
print(ts['id'], ts['path'], ts['classes'])   # 登记模型时带上 ts['id']；classes 的下标就是类别编号

v = fw.get_version('SES-TX@3', '/data/train/SES-TX@3')   # 取任意一个版本（目录要不存在或为空），返回版本记录
print(v['id'], v['path'])
```

和 `fw get` 一样用缓存、逐个核对；进度和结果打印到 stderr。要先在这台机器上 `fw login`，或者设好 `FW_SERVER`、`FW_TOKEN`。

训练完登记模型版本：

```python
mv = fw.register_model('SES-TX-detr', onnx='runs/best.onnx', weights='runs/best.pt', trainset=ts)
print(mv['ref'])   # SES-TX-detr@3
```

`trainset` 可以是 `fw.get_trainset` 返回的记录、它的 `id`，或者 `'SES-TX-det@2'`。参数和 `fw model register` 一样（`weights`、`metrics`、`files`、`code_dir`、`code_commit`、`code_repo`、`note`）；训练代码不写就不记。同样的东西再登记一次返回已有的版本，`mv['existing']` 为 `True`。

部署脚本取模型版本：

```python
m = fw.get_model('SES-TX-detr@3', '/opt/deploy/models/SES-TX-detr@3')
print(m['path'], m['onnx_sha256'], m['classes'])
```

`fw.get_trainset(ref, dest, fmt='yolo', neg_ratio=None, fix_dataset_length=None)`：`fmt` 和 `fw get --format` 一样，后两个和 `--neg-ratio`、`--fix-dataset-length` 一样。返回的记录里另有 `format`、`data_yaml`（`yolo` 和 `yolo-tree` 时，交给训练代码）、`neg_train`（`yolo` 时，`neg_train/` 里的张数）、`classes`（类别名列表）、`counts`（每个划分的张数和每类框数）、`formats`（快照里有的格式）、`sources`（来源版本）、`based_on`（接着哪个快照）和 `rule`（划分规则）；不是快照的名称会报错（`fw.ApiError`，`.status` 是 400）。

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
fw-server user role zhangsan field     # 改角色：下一次请求起生效，凭据不用重发
fw-server user disable zhangsan        # 立即吊销这个账号的全部凭据
fw-server user enable zhangsan         # 恢复账号；旧凭据仍然作废，要重新发
```

| 角色 | 能做的 |
|---|---|
| `admin` 管理员 | 算法工程师和现场实施能做的都能做（不能领任务，那是 worker 的事）；管账号和凭据 |
| `algo` 算法工程师 | 建数据集、导入、加目录、取回、对比、导出；建训练集快照；建模型、登记模型版本和评估报告、打标签；提交和取消任务；看发布、追溯链和操作记录 |
| `field` 现场实施 | 看全部数据集、训练集快照、预测集、模型、评估和发布，取它们的文件（`fw get`、`fw model get`）；登记发布；看追溯链。不能新建或修改，也不能上传文件（上传批次在后面的步骤） |
| `worker` GPU worker | 一台 GPU 机一个：登记推理环境、领取和运行任务、上传结果；为了任务能取数据集和模型的文件；不能提交任务、不能建数据集和模型 |

人的角色就这三个，一人一个账号。原来的 `deploy`（部署）并进了现场实施：升级时已有的部署账号自动变成现场实施，凭据不用重发。每个角色能做什么在代码里是一张表（`server/fwserver/auth.py` 的 `CAPS`），接口只查这张表。

算法工程师和管理员提交、取消任务；算法工程师、管理员和 worker 能看任务和推理环境。

### 凭据

```
fw-server token create zhangsan --label 训练机-01   # 打印新凭据，只显示这一次
fw-server token list [账号]
fw-server token revoke 3                          # 按 list 里的编号吊销
```

每台机器一个凭据，`--label` 写清楚给谁、哪台机器，丢了或者机器报废就吊销那一个。不要把凭据发到聊天群或写进仓库。

**第一个管理员账号**只能在服务器上用上面的命令建。之后账号都可以在网页的「账号」页管（管理员）：新建账号、改角色、停用和启用、发凭据（只显示一次）、吊销凭据。每个人在网页右上角点自己的名字能看到自己的凭据，吊销不再用的。

- 改角色：worker 账号是 GPU 机用的（推理环境登记在它名下），不和人的角色互换，要换就另建一个账号。
- 唯一在用的管理员不能停用、不能改成别的角色，先建好另一个管理员。
- 账号不能删：它建过的版本、模型都记着它。不用了就停用。

### 操作记录

谁在什么时候改了什么都记在 `events` 表里，和改动本身在同一个事务里写：改动回滚了记录也没有，没有记录的改动提交不了。记录只增不改，数据库触发器拒绝修改和删除。网页「操作记录」页（管理员、算法工程师）能按操作、账号、对象筛选；接口是 `GET /api/v1/events?limit=&before=&actor=&action=&q=`。

| 记下的操作 | 对象 |
|---|---|
| 新建数据集、建版本（导入、加目录、去掉目录、建快照都算）、起版本号、撤销版本号 | `SES-TX`、`SES-TX@3` |
| 新建模型、登记模型版本、改模型标签、给模型版本起或撤销版本号、登记评估报告、登记发布 | `SES-TX-detr@3`、`评估 12`、`SES-TX-r3` |
| 登记推理环境、提交任务、取消任务、登记预测集（预标注任务跑完时，记在提交任务的账号名下） | `rfdetr@2`、`任务 17`、`预测集 5` |
| 登记现场、登记产线、改现场或产线的显示名 | `SES`、`SES/TX` |
| 新建账号、改角色、停用、启用、发凭据、吊销凭据 | 账号名 |

在服务器上用 `fw-server` 做的（建账号、发凭据）记成「服务器命令行」，没有账号。凭据本身从不写进记录，只记编号和说明。worker 领任务、续租、交结果是机器的日常动作，不记在这里，在任务页看。

### verify：核对文件库

```
fw-server verify                                    # 快：每个版本（包括模型版本、评估报告）的清单能对上，用到的文件都在、大小对
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

## 网页

`http://<服务器>:<端口>/`，用账号凭据登录（`fw-server token create` 给的那串）。凭据存在浏览器的 localStorage 里，点「退出」删掉；共用电脑用完要退出。各角色能看的和 API 一样；没权限做的事，页面上不出现对应的按钮（现场实施账号看得到数据集，看不到数据集的上传；它上传的是标注批次）。

| 页面 | 地址 | 内容 |
|---|---|---|
| 数据集 | `#/` | 全部数据集、最新版本、大小；新建数据池（现场和产线从登记过的里面选）；登记现场 / 产线 |
| 数据集 | `#/d/<名称>` | 各个版本（序号、版本号、目录变化、来源）、比较两个版本；管理员和算法工程师在这里给版本起版本号（平台给出下一个修订、次、主版本号，点一下填上） |
| 版本 | `#/v/<名称>/<序号或版本号>?dir=<目录>` | 按目录浏览：子目录（含多少张图）、这一层的图片缩略图（每页 60 张，绿点 = 有 JSON 标注） |
| 图片 | `#/img/<名称>/<序号>/<路径>` | 大图和标注框，尺寸、大小、sha256、是否已确认、各类别框数；← → 翻同一目录的图 |
| 比较 | `#/diff/<名称>?old=<序号>&new=<序号>` | 新增、删除、修改、改名（各列前 500 个，全部用 `fw dataset diff --list`） |
| 模型 | `#/models`、`#/m/<名称>` | 全部模型；一个模型的版本（序号、版本号）和评估报告，勾两份报告比较；给模型版本起版本号 |
| 模型版本 | `#/mv/<名称>/<序号>` | 训练集快照、来源数据池、类别、ONNX、训练代码、发布、文件（可下载）、评估、预测集；管理员和算法工程师还能在这里提交评估和预标注（选推理环境、数据、目录、conf） |
| 评估 | `#/eval/<编号>`、`#/evals?a=<编号>&b=<编号>` | 一份报告的全部数值指标和文件；两份报告逐项比较（测试集或目录不同时提醒） |
| 任务 | `#/jobs?status=<状态>`、`#/job/<编号>` | 任务队列（前面还有几个，有没跑完的每 10 秒刷新）；一个任务的状态、结果链接（评估报告、预测集）、实时日志（3 秒刷新），可以取消 |
| 预测集 | `#/predictions`、`#/p/<编号>?thr=<阈值>` | 全部预测集；一个预测集的图片（每张几个框）、拖阈值看框数变化、下载命令 |
| 标注批次 | `#/batches?view=todo\|mine\|all`、`#/b/<批次>?old=<版次>&new=<版次>` | 待我复核、我上传的、全部；一个批次的状态、各版、两版之间逐图的框变化、经过和复核意见；各次预标注的状态和预测集；按账号能做的出现按钮：上传新的一版、跑预标注、提交复核、通过、退回、预览入库、关闭 |
| 上传批次 | `#/batch-new`、`#/batch-up/<批次>` | 从浏览器选目录上传新批次或新的一版（标注人姓名必填，可以同时选复核人）；复核人「我改过了」也在这里上传 |
| 上传 | `#/upload/<名称>` | 从浏览器选一个目录建数据池的新版本（见下） |
| 图片叠预测 | `#/img/…?pred=<预测集>&thr=<阈值>` | 标注是实线，预测是虚线带分数；← → 翻这个预测集的图 |

- 网页读的是同一套 `/api/v1`，图片按哈希从文件库取，浏览器会缓存（内容不会变）。
- 版本的文件清单整个读进浏览器（二十万个文件约 20 MB），大版本第一次打开要等几秒。
- 上传新增目录、去掉目录（管理员、算法工程师）：数据集详情页的「上传新增目录」「去掉目录」，和 `fw dataset add`、`fw dataset remove` 是同一件事、同一个服务端接口（`POST /api/v1/datasets/<名称>/changes/preview` 预览，`/changes` 建版本）。上传新增目录：选一个目录，「放在池里的目录」自动填目录名（可改，相当于 `--to`），「替换已有目录」相当于 `--replace`；先按 `fw dataset import` 的规则检查这个目录，算哈希后服务器给出预览（新增或替换哪个目录、和池里重复的图片、标注不一致的重复图片、会少掉的文件），能建才出现「上传并建」。去掉目录：勾选最上层的目录（更里面的目录手写），预览后建版本。来源记成「网页上传新增目录 <目录>」「网页去掉目录」。
- 上传完整版本（管理员、算法工程师）：数据集页「新建数据池」或数据集详情页「上传完整版本」（原来叫「从浏览器上传新版本」）。和 `fw dataset import` 一样：服务端用同一套规则检查（`POST /api/v1/folder-check`，系统文件 Thumbs.db 等不入库，有问题不收；「其他文件」「同名不同扩展名」对应 `--allow-other-files`、`--force` 两个勾选），浏览器里算 SHA-256（局域网 http 没有 crypto.subtle，用 `web/sha256.js`，几个 worker 并行），列出和最新版本比新增、删除、修改，确认后只传服务器没有的文件，最后建版本并冻结。父版本是最新版本，新版本只含这个目录里的文件；期间别人建了新版本就拒绝，重来一次。来源记成「网页上传 <目录名>」。几万张的大批数据还是在服务器上用 `fw dataset import` 快（网页一次最多检查 30 万个文件）。浏览器看不到读不了的子目录和链接，命令行会拦下这些，网页只能靠「会删掉 N 个文件」的提示发现。检查、算哈希、上传期间改选目录或勾选，旧的检查结果作废；算哈希和上传时目录和勾选框锁住。
- 菜单按账号能做的事显示（`/api/v1/me` 返回的 `caps`）：现场实施看数据集、标注批次、模型、预测集，worker 账号只看任务，「操作记录」给管理员和算法工程师，「账号」只给管理员；首页跳到能看的页面。
- 「账号」页（管理员）：账号列表，新建账号，下拉改角色，停用和启用，每个账号的凭据列表、发新凭据（只显示这一次）、吊销。右上角点自己的名字是「我的凭据」：看自己的凭据、吊销不再用的。
- 「操作记录」页（管理员、算法工程师）：时间、账号、操作、对象、细节，按操作类别、账号、对象里的字筛选，「更早的」翻页。
- 浏览器显示不了 TIFF：缩略图写「TIFF（浏览器显示不了）」，看图页只画框（尺寸取预测集或标注里的）。
- 打开过的图片最多留 150 张在浏览器内存里，多了丢掉最早的。缩略图现在拿的是原图，一页 60 张大图要下载一阵；以后加服务端缩略图。
- 安全：页面只加载自己的脚本和样式（Content-Security-Policy），平台里的文字（文件名、备注、标注类别）一律当文字显示，不当 HTML。

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
| `FW_CONDA` | `fw runner`、`fw worker` 用的 conda 可执行文件；不设就找 PATH 里的 `conda`，再找 `CONDA_EXE` |
| `FW_WORK_DIR` | `fw worker` 摆放任务文件（权重、图片、输出）的目录，默认是缓存目录下的 `jobs/`；相对路径按启动 worker 时的当前目录算；每个任务一个子目录，跑完删掉 |
| `FW_KEEP_WORK` | 设了（例如 `1`）就不删跑完的任务目录，查问题时用 |

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
| `FW_JOB_LEASE_SECONDS` | `60` | 任务租约：worker 这么久没续租，任务回到队列 |

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
