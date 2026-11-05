# 更新记录

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
- 复审后修正（合并前）：登记评估和发布时先定下具体的模型版本，重试不会落到期间新登记的版本上（服务器也不收 `latest`）；`--deteval` 按集合名（`--deteval-set`）和图片数核对评的就是这个测试集；`--code-repo` 里的令牌不再打印；现场和产线不能有 `-`，两条产线不会撞发布编号；`fw model chain --check` 也核对评估用的测试集；附件目录里读不了的子目录直接报错；`1e309` 这样超出范围的指标返回 400；同时建同名模型返回 409；一个文件集只能属于一个模型版本或评估报告，直接写数据库也一样。仓库地址先去掉首尾空白再去令牌；现场和产线只能用字母、数字、`_`、`.`；`--deteval` 登记途中报告被改了就不登记。
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
