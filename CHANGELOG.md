# 更新记录

## 0.3.0（未发布）

第三步之一：任务队列和 GPU worker。后面的评估、预标注、引擎构建都是在这上面排队的任务。

第三步之二：统一评估。

- `fw eval run <模型版本> --data <数据集版本 | 快照:划分> [--dirs <目录…>] --runner <推理环境>`：GPU 机用模型版本的 `model.onnx` 在本机建 TensorRT 引擎（onnx2engine，FP16，按 ONNX、程序、环境、显卡缓存，不上传不发布），用部署代码的推理模块跑测试图，写成 aoi-deteval 的预测文件，再跑 `deteval evaluate`，结果自动登记成评估报告，报告记下评估任务、推理环境、评了哪些目录和引擎信息（`files/engine.json`）。平台从不加载上传的权重。`--wait` 跟到跑完。
- 提交时定下评哪些图（清单存进文件库）。数据集版本读图旁边的 JSON 标注（按类别名），快照读它的 YOLO 标注；只有 YOLO txt 的图不收，先建快照。选的图和模型训练集 train、val 里的图内容相同时提醒，并写进报告备注。
- 推理环境登记时加 `--framework rfdetr --onnx2engine <程序>`（部署代码的推理模块和建引擎程序，记下程序的哈希）；评估前 worker 核对环境、源码和 onnx2engine 没变。
- 只收单文件 ONNX：`fw model register` 和 worker 建引擎前都拒绝把张量数据放在别的文件里的 ONNX（external data）。
- 引擎缓存先落盘再改名，旁边写 `.sha256`，用前核对，不对就重建；推理模块加载不了引擎时重建一次。
- 显卡按 `CUDA_VISIBLE_DEVICES` 的第一块，`CUDA_DEVICE_ORDER=PCI_BUS_ID`，和 `nvidia-smi` 编号一致。
- `engine.json` 记下推理计数；超出类别表的框比留下的还多时评估失败。
- 数据库迁移 0006（推理环境的框架、评估任务、评估报告的目录和任务）。

第三步之三：预测集和预标注。

- `fw predict run <模型版本> --data <数据集版本> [--dirs …] --runner <推理环境> [--conf 0.1]`：GPU 机用本机的 TensorRT 引擎跑选中的图，结果存成不可变的预测集；同样的模型版本、推理环境、图片和最低分数再提交直接复用，不再占 GPU。
- `fw predict get <编号> <目录> [--threshold 0.3] [--replace]`：取出图片，按阈值写 LiteAnno / X-AnyLabeling 预标注（矩形框带分数，`checked=false`，顶层 `flywheel` 记来源），已有标注默认保留，附 `classes.txt`；LiteAnno 直接打开。
- `fw predict list`、`fw predict show`；`fw job show` 显示预测集编号。
- 数据库迁移 0007（推理任务、预测集）。
- 复审后修正（合并前）：测试快照的类别表和模型不同（包括只是顺序不同）时不收；数据池里 `x.JSON` 这样大写扩展名的标注照常读，只有 `labels/x.TXT` 的图和只有 `.txt` 的一样不收；两个任务结果完全相同时各自登记报告；`FW_WORK_DIR` 可以写相对路径。
- 第二轮审查后修正：数据池上评估时，标注里有模型类别表里没有的类别名就不收。

- 新角色 `worker`（GPU worker）：每台 GPU 机一个账号，`fw-server user add gpu-01 --role worker`。
- 推理环境：`fw runner register <名称> --env <conda 环境> [--src <框架源码>]` 在 GPU 机上登记，记下 `conda list --explicit` 的包清单（传到平台）和源码的 git 提交号；变了再登记就是新版本。`fw runner list`、`show`；`fw runner check` 提交一个任务，在那台机器上检查环境和源码有没有变。
- 任务队列：`fw job list`、`show`、`log [-f]`、`cancel`。任务按提交顺序排队，只发给推理环境所在的机器。
- `fw worker`：在 GPU 机上常驻，领取、运行、交回任务，输出实时传回平台。每次领取有自己的令牌和 60 秒的租约（`FW_JOB_LEASE_SECONDS`），每 20 秒续一次；worker 掉线后任务自动回到队列，旧令牌的续租、日志和结果一律拒绝；领取 3 次都失联算失败。取消运行中的任务时，worker 杀掉正在跑的命令连同它的子进程。
- 平台上的模型只做 TensorRT 推理（NVIDIA GPU），不支持其他硬件（2026-11-09 定）。
- 数据库迁移 0005（任务、推理环境、worker 角色）。
- 复审后修正（合并前）：
  - 取消检查任务时，读包清单的 conda 和它的子进程也会被杀掉（以前读包清单不在可取消的进程组里）；命令结束后留下的子进程一起杀掉，不会占着输出拖住取消或任务结束。
  - worker 连不上平台时按自己的时钟算租约，到期就停下命令，不会和平台重新派出的领取并跑。
  - 等数据库行锁期间租约到期的续租、日志、交结果一律拒绝（到期按拿到锁之后的时间判断）。
  - 取消受理以后，worker 再交的完成或失败结果不会盖掉它，任务记成已取消。
  - 推理环境的包清单加上 `pip freeze --all`（pip 装的包）；源码有未提交的改动时记下改动内容的哈希，再改一次也看得出。包清单先落盘再登记，`fw-server verify` 也核对推理环境的包清单。
  - 日志：没收到回应的一段带原编号重发，后面的不会被当成重复丢掉；`fw job log -f` 跟到重新领取时从新一次的开头显示。
- 第二轮复审后修正：
  - 包清单不再带私有源的凭据：网址里的账号密码、`?` 后的参数（如 `?token=...`）和 conda 的 `/t/<token>/` 去掉再存，包的哈希保留。
  - 检查任务读源码的 git 也能被取消（以前卡住的 `git diff` 要等 5 分钟超时）。
  - 续租线程的日志请求迟迟不回时，任务结束不再并发重发同一段（以前会报错、结果没交上）。
  - 同一个结果同时交两次（网络重试），两次都返回完成，不再有一次报冲突。
  - 取消在结果之前受理时，worker 显示「已取消」，不再显示「完成」。
- 第三轮审查后修正：平台重启、网络断开时 `fw worker` 不再退出，等平台恢复后继续领任务；交结果连不上会一直重试并续租，算完的结果不会白算；平台一直返回 5xx 时最多重试 10 分钟，之后让任务回到队列，不会卡在「运行中」，期间收到取消就交「已取消」。文档补上 systemd 服务（`Restart=always`）。

## 0.2.3（未发布）

按 SES-TX 实测（`tx_v8`：`images/1.bmp` + `labels/1.txt`）修正。

- `fw get` 取回训练集快照时路径变短：去掉 `<现场>/<产线>/` 两层和数据池里的 YOLO `images/` 层，`images/train/SES/TX/images_/1.bmp` 变成 `images/train/1.bmp`（`labels/`、`--format coco`、`--format json` 同样）。快照里存的路径和样本标识不变（`--format all`、划分清单照旧）。去掉后有两张图路径相同（几个来源里同路径的图，或者池里同时有 `images/a.png` 和 `a.png`）时不建快照。新快照的 `rule` 里记 `"paths": "short"`，COCO 的 `file_name` 也是短路径；以前建的快照取回时仍是长路径。新服务器建的快照要用新版 `fw` 取，旧版 `fw` 会按长路径摆，COCO 对不上。
- `fw dataset import` 的扫描也按 `images/` ↔ `labels/` 配对，不再把配好的图和 txt 报成「没有标注的图片」「找不到图片的标注」。
- `fw get --format json` 把 LiteAnno JSON 放在每张图旁边（以前就是这样，手册补上说明：`--format all` 的 JSON 单独在 `json/` 下，LiteAnno 读不到）。
- 复审后修正（合并前）：
  - 去掉几层后只差扩展名的两张图（例如 `images/1.bmp` 有标注、`1.png` 没有）会共用一份标注（YOLO 的 `labels/train/1.txt`、LiteAnno 的 `1.json`），这样的快照也不建。只差大小写的（`A.bmp` 和 `a.png`，Windows 上是同一个标注文件）也算；检查一次分组完成，重名很多时预览不会变慢。
  - 导入扫描里 `labels/a.json` 不再因为旁边有配上的 `labels/a.txt` 就算作配上（YOLO 只认 txt），会报「找不到图片的标注」。
  - 0.2.3 以前建的快照用同样的输入续建时，只是取回布局变了也能建新版，不再报「完全一样」。

## 0.2.2（未发布）

按 SES 现场的真实数据池修正建训练集快照：数据池按 YOLO 目录放的标注能配上，标注里的类别可以在建快照时映射到快照的类别表。数据池里的标注都不改。

- 配对也认 YOLO 的位置：`batch/images/a.png` 配 `batch/labels/a.txt`（路径里最后一个 `images/` 换成 `labels/`，和 ultralytics、aoi-deteval 一样）。以前这些 txt 被当成孤立标注，快照建不了。
- `--label-source json|txt`：一张图同时有 JSON 和 txt 标注时用哪一种；不写时遇到这样的图就不建（以前同目录同时有两种也是不建）。同一种有两个也不建。
- `--label-classes <类别表>`：txt 标注按它自己的类别表读编号，快照里按 `--classes` 重新编号。
- `--map 源=目标`（可写多次）：几类合成一类（`DL1=DL`），或者去掉一类当背景（`others=`）。JSON 只改形状的类别、删掉去掉的形状，别的内容不动；txt 去掉的行删掉、按快照的类别表重新编号；没改到的标注原样冻结。框全被去掉的图当没有框的背景图收进来。预览列出每条映射去掉、并进了多少框；映射的源类别在标注里一个框也没有就不建。
- 复审后修正（合并前）：
  - 一个标注同时配到两张图时不建，以前同一份框会冻到两张图上（例如 `labels/a.txt` 旁边有 `labels/a.png`）。
  - 一张图有两个同一种的标注时，用 `--label-source` 选另一种也不建。
  - 接着上一版、新加或改了映射时，「标注改了」按这一版冻结的标注来比，不再漏报。
  - txt 重写时坐标原样保留、只换编号，极小的框不会写成宽 0。
  - JSON 重写时数字按原文写回，不经过数值转换（包括 `-0`、`NaN`）；含 `\udc80` 这类孤立代理码位的 JSON 映射时不再返回 500。
  - 和一份 JSON 字节相同的 txt 按 txt 读（以前会借用 JSON 的解析结果，绕过 txt 的格式检查）。
  - 「标注改了」比较这一版和上一版冻结的 `json/`、`labels/` 两个文件：换标注来源、映射、类别表或图片尺寸让冻结的文件变了都算，多了或少了 `json/` 也算。
- 这三项记在快照 `trainset.json` 和数据库里的 `rule`（`label_source`、`label_classes`、`class_map`），`fw trainset show` 显示「标注：…」一行。不需要数据库迁移；旧版 `fw` 不发这些参数，照常能用；新版 `fw` 带着这些参数连到旧服务器时直接报错（旧服务器会不声不响地忽略它们）。

## 0.2.1（未发布）

模型好坏以平台用 aoi-deteval 统一算的指标为准（第三步），登记模型时不再要求训练框架的指标和训练代码。

- `fw model register --weights best.pt`（`fw.register_model(weights=…)`）：权重登记在 `weights/` 下，一个文件，`fw model get` 一起取回。
- 训练代码改为选填：`--code-dir <目录>` 读那个目录的 git，或者直接写 `--code-commit`（和 `--code-repo`）；都不写就不记，显示「未记录」。**不再自动读当前目录的 git**：当前目录可能是别的仓库。
- `--metrics` 仍可用，只作参考。
- 没记训练代码时接口里 `code_commit` 是空字符串（数据库里是 NULL），旧版 `fw` 照常列出版本。
- 空的权重文件不登记（`fw` 和服务器都拒绝，数据库也有 CHECK）：冻结后会挡住用真权重重新登记同一个 ONNX。
- 读训练代码的 git 时不带任何 `GIT_*` 环境变量，`GIT_DIR`、`GIT_WORK_TREE` 不会把 `--code-dir` 指到别的仓库。
- 数据库迁移 `0004_optional_code.sql`：训练代码提交号可以为空；记了仓库地址或「有没提交的改动」就必须有提交号；`weights/` 下的文件不能为空、不能在子目录里。服务启动时自动应用，升级前照常备份。

## 0.2.0（未发布）

第二步：训练集快照、模型版本、评估、发布和追溯链（引擎构建放到第三步）。

- 新增训练集快照：`fw trainset create` 从一个或几个数据池版本建快照，每张图分到 train、val 或 test，连同类别表冻结；先预览每个划分的张数和每类框数，确认后才建。同名再建一版时默认接着上一版的划分（原来的样本不换划分，新样本进 test），可选 `--test-dirs`、`--merge-test`、`--new-to trainval`、`--split-file`、`--fresh`。标注里有类别表以外的类别、YOLO 编号超出范围、标注没有图片等情况直接拒绝。格式见[训练集快照的格式](docs/trainset-format.md)。
- 建快照时服务器把标注转成 YOLO 和 COCO 检测框，和图片、原始 LiteAnno JSON 一起冻结；`fw get <快照>` 默认摆成 YOLO 的目录并生成 `data.yaml`，`--format coco` 摆成 RF-DETR 读的 COCO 目录，`--format json` 摆成 LiteAnno 能直接打开的目录。类别编号都是 classes.txt 的行号。
- `fw trainset list`、`fw trainset show`；快照用 `fw get`、`fw export`、`fw verify`、`fw dataset diff` 取回、导出、核对和对比，和数据集版本一样。训练脚本用 `fw.get_trainset()` 取快照，返回 id、目录、类别表和 `data_yaml`。
- `fw dataset list` 里快照的类型显示为「训练集快照」；往快照里 `fw dataset import` 会被拒绝。
- 数据池里是 YOLO txt 标注的图，建快照时从框生成 LiteAnno JSON（矩形），`--format json` 也能取回、用 LiteAnno 打开。
- `--require-labels`：要求每张图都有标注，有没标注的图就不建（默认当负样本）。
- 数据库迁移 `0002_trainsets.sql`：服务启动时自动应用，不用手动操作；升级前照常备份。
- 审查后修正（合并前）：确认后冻结的是预览时定下的来源版本；`--split-file` 建的版本 `based_on` 为空；YOLO 目录里原路径中叫 `images` 的目录改名为 `images_`，ultralytics 和 aoi-deteval 才找得到标注；YOLO txt 的 UTF-8 BOM 冻结时去掉；同一现场和产线的两个来源有同名不同扩展名的图片时不建；没有 train 或 val 时预览和 `fw get` 明确提醒；名称和第一版一起建，失败不留空名称，同时建同名第一版返回 409 而不是 500；`fw dataset create` 同时建同名数据集也返回 409。
- 新增模型和模型版本：`fw model create` 建模型（现场、产线、任务类型），`fw model register` 或训练脚本里的 `fw.register_model()` 把 ONNX、指标、附件和训练集快照登记成下一个版本。文件先传到服务器再登记，`classes.txt` 从快照复制；训练代码的提交号默认从当前目录的 git 读，并记下有没有没提交的改动，仓库地址里的账号和令牌不记。同一个 ONNX 再登记返回已有的版本，内容不一样就拒绝。`fw model list/versions/show`，`fw model get` 和 `fw.get_model()` 把模型版本取到本地（和 `fw get` 共用缓存）。
- 标签：`fw model tag` 标候选、弃用，只追加，历史都在；`fw model compare` 并排对比几个版本的指标。
- 评估报告：`fw eval register` 登记一个模型版本在测试集（快照的某个划分或数据集版本）上的指标和报告文件，`--deteval` 直接读 aoi-deteval 的输出目录（没有定义的指标 `NaN` 登记为 `null`）；`fw eval list/show`。
- 发布：`fw release register` 由部署仓库在部署成功后登记，发布 ID 是「现场-产线-r序号」；不上传引擎，记下现场构建引擎用的 GPU、TensorRT 版本、精度和可选的引擎哈希。`fw release current` 按部署成功时间最晚的发布回答产线现在用的模型，补登旧发布不会顶掉新的。`fw release list/show`。
- 追溯链：`fw model chain <发布或模型版本>` 从发布一路显示到模型版本、评估、训练集快照和数据池版本，`--check` 核对链上每个文件都在文件库里。
- 部署账号现在能读模型、登记发布、下载模型版本的文件（取不了数据集和快照的文件）；现场实施账号仍然什么都碰不到。
- `fw-server verify` 也核对模型版本和评估报告的文件。
- 数据库迁移 `0003_models.sql`：服务启动时自动应用；模型、模型版本、评估报告、发布和标签记录登记后数据库拒绝修改和删除。
- 复审后修正（合并前）：登记评估和发布时先定下具体的模型版本，重试不会落到期间新登记的版本上（服务器也不收 `latest`）；`--deteval` 按集合名（`--deteval-set`）和图片数核对评的就是这个测试集；`--code-repo` 里的令牌不再打印；现场和产线不能有 `-`，两条产线不会撞发布编号；`fw model chain --check` 也核对评估用的测试集；附件目录里读不了的子目录直接报错；`1e309` 这样超出范围的指标返回 400；同时建同名模型返回 409；一个文件集只能属于一个模型版本或评估报告，直接写数据库也一样。仓库地址先去掉首尾空白再去令牌；现场和产线可用汉字、英文字母、数字、`_`、`.`（按码位写成同一条规则，服务器和数据库一致，不受数据库区域设置影响）；`--deteval` 先复制输出目录，指标和归档都来自这份复制。
- 部署演练（`tools/deploy-drill.sh`）加上训练集快照、模型登记、评估、发布，在另一台机器恢复后核对追溯链并取回 ONNX 比对。

## 0.1.0（未发布）

第一步：文件库、数据集版本、`fw` 命令行，加部署、在线备份和恢复。

- 平台服务 `fw-server`（FastAPI + PostgreSQL 16）和按 SHA-256 存文件的文件库，docker compose 部署（`deploy/setup.sh`）。
- 账号四种角色（管理员、算法工程师、现场实施、部署），每台机器一个凭据，能列出、吊销；停用账号立即吊销它的全部凭据。这一步只有管理员和算法工程师能用数据集。
- 数据集和版本：每个版本是一个目录的完整清单，冻结后数据库拒绝任何修改；版本之间的新增、删除、修改、改名对比。
- `fw` 命令行和 Python 包（Python 3.8 起，Windows、Linux）：`dataset create/list/versions/import/diff`、`get`、`export`、`verify`、`cache`。导入前检查目录（读不了的目录、链接和 Windows 目录联接、特殊文件都会拦下，不会悄悄漏掉）、预览相比父版本的变化并确认，只上传平台上没有的文件；取回时只下载本机缓存里没有的，每个文件按清单核对。几个 `fw` 进程共用一个缓存时互不干扰，不会删掉别的进程刚装好的文件；`--hardlink` 只限 Linux，清缓存不会让取出的文件变成可写。
- 新增[命令手册](docs/commands.md)和[维护和开发说明](docs/development.md)。`fw.ApiError` 可以直接从 `fw` 导入，训练脚本按手册接住它和 `fw.FwError`。
- `fw get` 先说本机缓存已有几个、要下载几个，结尾写「缓存命中 N 个，下载 M 个」（缓存里坏了、重新下载的不算命中）；原来缓存命中的也显示成「下载 N/N」。
- 导入或取回时按 Ctrl+C 立即生效：还没开始的上传、下载不再进行，命令一两秒内退出（退出码 130）；原来在 Windows 上要等全部传完才停下。
- 导入结束时打印的用时不再把等人确认的时间算进「上传」，另外单列「查服务器和对比」。
- 断电或死机时如果有没落盘的写入，重启后停止接受写入，直到核对通过（`UNSYNCED` 标记记着那次开机的编号，每个进程确认它落了盘才放入文件）；解除不了时 `verify --rehash` 报错退出。
- 写盘出错（syncfs）后停止接受写入，盘满写不下标记时也一样；`fw-server verify --rehash` 通过后才恢复，它也核对导入中断时留下、还没有版本用到的文件，读不了的目录或文件都算没通过。
- `fw-server verify [--rehash] [--repair-from 备份]` 核对文件库，`--rehash` 从盘上读出来重算（还没有版本用到的文件坏了就移到 `quarantine/`，重新导入时再上传），能从备份修复，修复的文件落盘后才报告。
- `deploy/backup.sh` 在线备份（不停服务）；`deploy/restore.sh` 恢复，按恢复的数据库逐个重算哈希、从备份补回缺的和坏的文件，全部通过才对外服务，任何一步失败都换回原数据库。
- `tools/bench.py` 规模测速，`tools/deploy-drill.sh` 部署和恢复演练。
- 小文件批量传输：导入和取回时小文件一次请求传几千个（16 MB 以上的文件仍然一个一次），每个文件照样按哈希核对，也和单个上传一样受 `FW_MAX_OBJECT_GB` 限制；网络断了只重传还没收到的。同一台机器上 20 万个小文件入库从 470 秒降到 53 秒，冷取回从 648 秒降到 136 秒。
