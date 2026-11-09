# 维护和开发说明

给以后改这份代码的人（包括我自己）：代码怎么分层、一次导入和一次取回在代码里怎么走、哪些规则绝不能破、怎么测、怎么加新功能。怎么用命令见 [命令手册](commands.md)，为什么这样设计见 [架构文档](architecture.md)。

## 文档地图

| 文档 | 写什么 | 什么时候要改 |
|---|---|---|
| `README.md` | 平台是什么、保证什么、怎么部署 | 保证的规则或部署方式变了 |
| `docs/commands.md` | 每条命令、选项、环境变量、常见问题 | 加命令、加选项、改输出或报错 |
| `docs/architecture.md` | 设计和取舍（在线设计文档的副本） | 设计变了 |
| `docs/manifest-format.md` | 清单格式 1 | 格式变了（只能出新版本，见下文） |
| `docs/trainset-format.md` | 训练集快照的目录、`classes.txt`、`trainset.json`、划分规则 | 快照格式或划分规则变了 |
| `docs/development.md` | 本文 | 分层、规则、测试方式变了 |
| `CHANGELOG.md` | 每个 PR 用户能看到的变化 | 每个 PR |

规则：**改了用户看得到的行为，就在同一个 PR 里改对应的文档。**

## 代码结构

```
client/fw/           fw：命令行 + 训练脚本用的 Python 包（Python 3.8 起，Windows、Linux）
  contract.py        客户端和服务端共用：路径规则、哈希、清单格式、fw-objects 批量传输格式、版本差异、
                     哪些扩展名算图片和标注
  checks.py          导入前对目录的检查（只读，不连平台）
  api.py             平台 HTTP 接口的客户端，所有请求从这里出去
  cache.py           本机缓存：按哈希存的文件 + SQLite 索引（大小、修改时间）
  layout.py          把缓存里的文件摆成版本的目录：reflink、复制、硬链接，每个文件摆完都核对哈希
  pool.py            并行跑一组任务（上传、下载）：出错或 Ctrl+C 时取消还没开始的，不等手上的
  worker.py          fw worker 和 fw runner：领任务、续租、传日志、在 conda 环境里跑命令、交结果；读 conda
                     包清单和源码提交号
  ops.py             每条命令真正做的事（import_folder、get、export、verify、diff、get_version、
                     create_trainset、get_trainset、register_model、get_model、register_eval、
                     register_release、show_chain）
  cli.py             命令行参数和输出格式，只调 ops
  config.py          设置文件和缓存的位置
server/fwserver/     fw-server：只支持 Linux
  app.py             HTTP 接口 /api/v1（FastAPI）
  cli.py             fw-server 管理命令
  jobs.py            任务队列（领取、租约令牌、续租、过期回队、取消、日志）和推理环境的登记
  models.py          模型、模型版本、评估报告、发布、标签、追溯链；部署账号能下载哪些文件
  trainsets.py       训练集快照：配对图片和标注、核对类别、划分、摆出快照的目录，再用 datasets 建版本
  formats.py         读 LiteAnno JSON 和 YOLO txt、读图片文件头的尺寸、写 YOLO 和 COCO（纯函数）
  datasets.py        数据集和版本：建版本、冻结、差异、核对（verify）
  auth.py            账号、角色、凭据（只存凭据的 sha256）
  store.py           文件库：写入、落盘（syncfs）、停写标记、核对时的读取和隔离
  db.py              数据库连接池和编号迁移
  settings.py        环境变量
  migrations/        0001_init.sql …，每次改表结构加一个新文件
deploy/              Dockerfile、docker-compose.yml、setup.sh、backup.sh、restore.sh
tools/               bench.py（规模测速）、deploy-drill.sh（部署和恢复演练）
tests/client/        不需要服务和数据库；Windows、Python 3.8 也跑
tests/server/        真实服务 + PostgreSQL 16 + 真实文件库
```

### 分层（`lint-imports` 检查）

- 服务端只能往下调：`cli`、`app` → `jobs`、`models` → `trainsets` → `datasets`、`auth` → `store`、`db`、`settings`、`formats`。
- 客户端永远不 import 服务端。
- `fw.contract` 不依赖 `fw` 里的任何其他模块：服务端也 import 它，它必须保持纯粹。

新模块要放进 `.importlinter` 的某一层。分层被打破时 CI 会失败，不要改规则来让它通过，先想清楚依赖方向。

## 一次导入在代码里怎么走

`fw dataset import`（`ops.import_folder`）：

1. `checks.scan_folder` 遍历目录，什么都不悄悄跳过：读不了的目录、链接、特殊文件都记下来；`FolderReport.blocking()` 给出拒绝的理由。
2. 每个文件算 SHA-256（算之前和之后各看一次大小和修改时间，变了就报错），得到清单。
3. `api.missing` 问服务器哪些内容还没有；和父版本的清单比出新增、修改、删除、改名，给人确认。
4. 小文件按 `fw-objects 1` 格式打包，一次请求几千个（`api.put_objects`），大文件一个一个传（`api.put_file`）。服务端 `store.put_verified` / `ObjectWriter` 先核对哈希再写临时文件、改名到最终位置。
5. `api.create_version`：服务端先 `store.flush()`（一次 `syncfs`，文件内容和目录项全部落盘），再在一个事务里插入版本和清单项并冻结；冻结时数据库按清单项重算清单哈希，对不上就不冻结。

## 一次取回在代码里怎么走

`fw get`（`ops.get`）：

1. 读版本记录和清单。
2. `cache.fetch_many`：先 `cached_many` 看缓存里有哪些（大小和修改时间和索引一致才算），其余的分批下载；每个下载的文件先核对哈希，再原子地放进缓存。
3. `Layout.place` 把每个文件从缓存摆到目标路径（reflink → 复制，或硬链接），摆完重算哈希；对不上就强制重新下载这个内容（`cache.fetch(force=True)`），第二次还不对就报错。

## 一次建训练集快照在代码里怎么走

`fw trainset create`（`ops.create_trainset`）先调预览接口，确认后再调建快照接口，两次的参数一样，只是来源换成预览解析出的具体版本（`SES-TX` → `SES-TX@4`；建快照接口不收没有序号的来源）。服务端两次都走 `trainsets.plan`，建快照时 `trainsets.create` 再按同样的参数重算一遍，所以冻结的就是预览给人看的：

1. `_sources`：解析来源版本，拒绝快照做来源、没填现场或产线的数据集。
2. `_samples`：配对图片和标注：同目录同名的 `.json` / `.txt`，或者 YOLO 找标注的位置（`contract.yolo_label_stem`，导入扫描 `checks.scan_folder` 也用它：路径里最后一个 `images/` 换成 `labels/` 的 `.txt`）。一张图配到 JSON 和 txt 两种时按 `label_source` 选，没给就拒绝；同一种有两个也拒绝（在按 `label_source` 筛之前查，选另一种盖不过去）；一个标注被两张图找到也拒绝；没有图认领的标注是孤立标注。样本标识是 `<现场>/<产线>/<图片路径>`；两个来源里去掉扩展名后相同的图片拒绝（标注路径会一样）。
3. `_read_labels`：从文件库读每个标注（同一内容只读一次，`_parse`），核对类别、形状和尺寸，把框裁到图片内。`_parse` 先做类别映射：JSON 在解析前用 `formats.remap_json` 改名、删掉映射成空的形状，文件里别的内容不动；txt 用 `label_classes`（没给就用类别表）读编号，再用 `formats.remap_txt` 改名、去掉行、按类别表重新编号。内容变了的标注放在 `Label.data`，`_tree` 冻结的是它，不是原文件；`_outputs` 在划分之后算好每个样本要冻结的 `labels/`、`json/`（`Sample.outputs`：转出来的内容，或者原样冻结的数据池文件），`_tree` 和 `_changes` 都用它：「标注改了」就是这两个文件（路径和哈希一起）和上一版这个样本的不一样，多了或少了 `json/` 也算（换了标注来源、映射、类别表或图片尺寸都算），只有标注转不了、快照反正建不了时才退回比较标注本身。解析结果按内容和格式（`.json` / `.txt`）缓存，同样字节的 txt 不会借用 JSON 的结果。txt 重写时坐标按原文保留（`Label.yolo_coords`），只换编号；JSON 重写时数字按原文写回（`json.loads` 的 `parse_float`、`parse_int`、`parse_constant` 换成占位符，写完再换回原文），孤立代理码位写回成 `\uXXXX`。映射的源类别在哪个标注里都没有就拒绝（多半是写错了）。
4. `_latest_trainset`、`_previous_splits`：同名快照的最新版，从它的目录结构读回每个样本的划分（按带扩展名的完整标识，标注按同划分同名找）。用划分清单或 `--fresh` 时不接着它。
5. `_assign`：划分清单、接着上一版、或重新划分；train/val 用 `_cut` 按 `sha256(种子\n标识)` 排序后按比例切。
6. `_formats`、`_tree`：看能生成哪些格式（COCO 要每张图的尺寸），摆出 `images/`、`labels/`、`json/`、`coco/`，加上 `classes.txt`、`trainset.json`；核对路径规则，并用 `contract.trainset_layout` 核对每种取回格式摆出来都不冲突。转出来的 YOLO 和 COCO 文件放在 `Plan.blobs` 里，建快照时写进文件库。
7. `create`：先看最新版还是不是预览时那个（不是就 409），把 `classes.txt`、`trainset.json`、清单写进文件库，再调 `datasets.create_version(trainset=True, record=…, new_dataset=…)`：第一版时在同一个事务里先插入数据集那一行（`ON CONFLICT DO NOTHING`，别人同时建了就 409），再在冻结之前写 `trainset_versions` 和 `trainset_sources` 两张表。所以第一版失败不留下空名称。

快照就是一个普通版本，导出、核对、对比都不需要专门的代码；取回时 `ops.get` 按 `contract.trainset_layout` 只改路径（YOLO 时再写 `data.yaml`，并用 `yolo_safe` 把原路径里叫 `images` 的目录改名，因为 ultralytics 和 aoi-deteval 把最后一个 `images/` 换成 `labels/` 找标注）。

## 一次登记模型版本在代码里怎么走

`fw model register`（`ops.register_model`）和 `fw.register_model`：

1. 本机先查 ONNX 的开头（ir_version）、指标文件是 JSON 对象、训练集快照存在而且是快照；训练代码选填：有 `code_dir` 时用 `git_info` 读那个目录的 HEAD、origin（`strip_credentials` 去掉账号和令牌）和有没有没提交的改动；从不自动读当前目录。
2. `_send_files`：算哈希、`api.missing`、只上传服务器上没有的。
3. `api.register_model_version`：服务端 `models.register_model_version` 核对每个文件都在文件库里且大小对，从文件库读 `metrics.json` 和 `model.onnx` 的开头（记下的是存着的文件，不是请求里说的），加上快照的 `classes.txt`，写好文件集的清单并 `store.flush()`；然后一个事务里锁住模型、看同一个 ONNX 是不是登记过（一样就返回已有的，不一样就 409）、插入文件集、冻结、插入模型版本。

评估报告（`register_eval`）走同一条路，文件集里是 `metrics.json` 和 `files/…`。发布不带文件：现场构建的引擎只记哈希和大小。

文件集（`filesets`、`fileset_files`）就是不属于数据集的「冻结的一组文件」：和数据集版本一样按清单哈希冻结，`fw model get` 用的是和 `fw get` 同一套缓存和摆放代码（`ops._lay_out`）。`fw-server verify` 通过 `models.verify_inputs` 也核对这些文件；`datasets.verify` 的 `more` 参数是必填的，免得哪里调用时漏掉模型文件（`--rehash` 时不认识的文件会被当成导入中断留下的）。

部署账号能下载的只有模型版本文件集里的文件和清单（`models.model_objects_only`，`app.may_download`），别的一律 403。

## 绝不能破的规则

每条后面是守住它的地方和测试。改到相关代码时，先跑这些测试；新的修复先写一个在旧代码上失败的复现测试。

| 规则 | 在哪里 | 测试 |
|---|---|---|
| 冻结的版本永远不变，不能解冻、不能删 | 数据库触发器（`0001_init.sql`） | `test_frozen.py` |
| 每个版本是完整清单，读任何版本不经过父版本 | `datasets.py`、`contract.py` | `test_random_versions.py`、`test_copy_rename_version_and_cold_fetch` |
| 数据库里引用的文件一定已经落盘 | `store.flush()` 在提交之前 | `test_failures.py` |
| 写盘出错（syncfs）后停止写入，盘满也拦得住，只有 `verify --rehash` 解除 | `store._mark_failed`、`WRITE-OK` / `WRITE-ERROR` | `test_store.py`、`test_disk_flush_error_blocks_writes_until_verified` 等 |
| 断电时有没落盘的写入，重启后停止写入；每个进程只信自己确认过落盘的 `UNSYNCED` | `store._writing`、`_make_durable` | `test_store.py` 里 `UNSYNCED` 相关、`test_power_loss_during_an_import_blocks_writes_until_verified` |
| 核对不完整（目录读不了）不能解除停写；解除失败时 verify 退出 1 | `store.iter_objects`、`datasets.verify`、`store.mark_verified` | `test_rehash_fails_when_a_folder_of_the_store_cannot_be_listed`、`test_verify_fails_when_*` |
| 导入前的扫描不悄悄跳过任何东西 | `checks._walk` | `test_checks.py` |
| 下载的文件核对哈希后才进缓存；摆出来的每个文件都再核对 | `cache._install`、`layout.py` | `test_download_with_a_wrong_hash_never_enters_the_cache`、`test_damaged_cache_object_is_caught_and_downloaded_again` |
| 缓存从不因为索引记录对不上就删文件（别的 fw 进程可能刚装好） | `cache.cached` / `cached_many` | `test_a_stale_index_row_never_deletes_a_fresh_copy*` |
| 改取出来的文件不影响缓存；硬链接只在明确要求时用，只限 Linux | `layout.py`、`ops.get` | `test_editing_a_fetched_folder_leaves_the_cache_alone`、`test_hardlink_*` |
| Ctrl+C 立即生效，不等还在排队的上传、下载（Windows 上无超时的等待接不到 Ctrl+C） | `pool.run_all`（单个任务也在线程里跑）、`cli._on_sigint` 不做清理直接 `os._exit(130)`、`Cache.close` 不等别的线程 | `test_pool.py`、`test_cache.py`、`test_ctrl_c_*` |
| 同一数据集同时导入，后提交的被拒，不悄悄覆盖 | `datasets.create_version` | `test_concurrent_import_into_same_dataset_is_refused`、`test_someone_else_added_a_version_meanwhile` |
| 训练集快照的记录（类别、规则、统计、来源）冻结后不能改、不能删；只有快照有这份记录，快照都有；有版本的数据集不能改类型 | 数据库触发器（`0002_trainsets.sql`） | `test_snapshot_record_cannot_change`、`test_only_trainset_versions_have_a_record` |
| 快照只能用 `fw trainset create` 建；标注里的类别都在类别表里；冻结的就是预览的（包括来源版本）；预览后有人建了新版就不建；同时建同名第一版只成一个，另一个 409 | `datasets.create_version` 的 `trainset`、`new_dataset` 参数、`trainsets.create` | `test_trainsets_are_made_only_by_trainset_create`、`test_bad_labels_are_refused`、`test_someone_else_made_one_meanwhile`、`test_what_was_previewed_is_what_is_frozen`、`test_two_first_versions_at_once` |
| 类别映射只改快照里的标注，数据池不变；没有改到的标注原样冻结；映射写错了（源类别没有框）就不建 | `formats.remap_json`、`remap_txt`、`trainsets._parse`、`_read_labels` | `test_class_map_drops_others_and_reads_txt_by_its_own_table`、`test_class_map_on_json_keeps_the_rest_of_the_file`、`test_class_map_snapshot_continues_without_relabeling`、`test_a_new_class_map_on_a_continued_snapshot_counts_as_relabeled`、`test_class_map_options_are_checked`、`test_remapping_keeps_numbers_and_coordinates_as_written`、`test_switching_the_label_source_counts_as_relabeled`、`test_a_txt_with_the_bytes_of_a_json_is_read_as_txt`、`test_a_class_map_on_a_json_with_a_lone_surrogate` |
| 数据池按 YOLO 的 `images/` ↔ `labels/` 放的标注能配上；一张图有 JSON 和 txt 两种时要明说用哪个 | `trainsets._samples`、`contract.yolo_label_stem` | `test_a_yolo_pool_pairs_images_with_labels`、`test_label_source_picks_json_or_txt`、`test_one_label_is_never_given_to_two_images` |
| YOLO 格式里每张图的标注就在 ultralytics 找的位置 | `contract.yolo_safe`、`trainsets._tree`（去掉 BOM） | `test_images_folders_inside_the_pool_path`、`test_yolo_label_with_a_bom`、`test_yolo_layout_renames_images_folders` |
| `fw get` 取回的路径去掉现场/产线和池里的 `images/` 层；去掉后重名的快照不建；旧快照仍是长路径 | `contract.short_paths`、`trainset_layout(short=)`、`trainsets._tree`、`ops.get`（看 `rule.paths`） | `test_short_paths_for_fw_get`、`test_short_paths_that_collide_are_refused`、`test_short_paths_never_share_a_label`、`test_shared_label_check_is_one_pass`、`test_an_old_layout_snapshot_can_be_made_again_with_short_paths`、`test_a_yolo_pool_pairs_images_with_labels` |
| 导入扫描按 `images/` ↔ `labels/` 配对，不误报「没有标注」「找不到图片」 | `checks.scan_folder` | `test_yolo_labels_pair_with_images` |
| 接着上一版时，原来的样本不换划分；同样的样本、比例和种子，划分总是一样 | `trainsets._assign`、`_cut` | `test_cut_is_fixed_by_keys_ratio_and_seed`、`test_continue_keeps_every_split` |
| 转好的 YOLO / COCO 标注随快照冻结，取回只改路径不改内容；转不了的形状、图外的框不悄悄丢掉，直接不建 | `formats.py`、`trainsets._tree`、`contract.trainset_layout` | `test_formats.py`、`test_first_snapshot_layout_and_formats`、`test_labels_that_cannot_be_detection_boxes` |
| 模型、模型版本、评估报告、发布、标签记录登记后不能改、不能删；文件集冻结后不能改；每个文件集只属于一个模型版本或评估报告（新建文件集时和插入模型版本、评估报告时都查）；模型版本只能引用训练集快照，ONNX 哈希就是文件集里的 `model.onnx`；现场和产线只能用汉字、英文字母、数字、`_`、`.`，和 `models.SITE_LINE` 是同一条按码位的规则 | 数据库触发器和 CHECK（`0003_models.sql`） | `test_records_cannot_change`、`test_what_the_database_refuses`、`test_a_file_set_has_one_owner`、`test_two_lines_never_share_a_release_ref`、`test_the_database_takes_the_names_the_server_takes` |
| 评估和发布登记在具体的模型版本上，重试不会换版本；`--deteval` 的集合就是声明的测试集（集合名、图片数） | `ops._pin`、`models._pinned`、`ops._deteval_metrics` | `test_a_retry_stays_on_the_version_it_meant`、`test_a_deteval_report_must_be_the_test_set` |
| 附件目录读不全就不登记；令牌不打印；指标只收标准 JSON 里的有限数 | `ops._folder_files`（`os.walk` 的 `onerror`）、`ops.register_model`、`models._no_nul` | `test_an_unreadable_folder_is_never_left_out`、`test_a_token_in_the_repository_is_never_printed`、`test_numbers_out_of_range` |
| 登记前文件已经在服务器上；登记的指标来自存着的文件；同样的东西再登记返回已有的，不一样就拒绝 | `models.register_model_version`、`register_eval`、`register_release` | `test_register_and_get_a_model_version`、`test_what_a_model_version_must_be`、`test_eval_reports` |
| 产线现在用的模型是部署时间最晚的发布，补登旧的不顶掉新的 | `models.current_release` | `test_releases_and_the_current_model` |
| 部署账号只能取模型版本的文件，取不了数据；现场实施什么都碰不到 | `app.may_download`、`models.model_objects_only` | `test_roles` |
| 仓库地址里的账号和令牌不会被记下 | `ops.strip_credentials`，数据库 CHECK 拒绝 `https://…@` | `test_code_commit_from_git`、`test_register_and_get_a_model_version` |
| 训练代码可以不记（数据库 NULL，接口给空字符串，旧客户端照常切片），但记了仓库或「有没提交的改动」就一定有提交号；`weights/` 下只有一个不为空的文件；读 git 时不带 `GIT_*` 环境变量 | `models.register_model_version`、`models._entries`、`ops._git`，CHECK `model_versions_code_needs_commit`、`fileset_files_weights_file`（`0004_optional_code.sql`） | `test_the_training_code_is_optional`、`test_no_training_code_reads_as_an_empty_string`、`test_empty_weights_are_refused`、`test_git_variables_never_redirect_the_code_dir`、`test_the_server_checks_weights_and_code`、`test_what_the_database_refuses` |
| `fw-server verify` 和追溯链 `--check` 也核对模型版本和评估报告的文件（`--check` 还核对评估用的测试集） | `models.verify_inputs`、`models._check_chain` | `test_chain_from_a_release_down_to_the_pool` |
| 一个任务同一时刻只在一次领取手里：领取用 `FOR UPDATE SKIP LOCKED`；每次领取一个新令牌（只存 sha256）；续租、日志、交结果都要这次领取的令牌而且租约没到期（拿到行锁之后按 `clock_timestamp()` 判断，等锁期间到期的也拒绝）；租约过期的领取再也改不了任务，任务回队（取消过的就结束，领取 3 次都失联就失败） | `jobs.claim`、`_held`、`expire_leases` | `test_two_workers_never_get_the_same_job`、`test_a_lost_lease_goes_back_to_the_queue_and_its_token_is_refused`、`test_tried_too_often_fails`、`test_cancelled_while_its_worker_is_gone`、`test_a_request_that_waited_for_the_lock_past_its_lease_is_refused` |
| 平台受理了取消，worker 后交的结果（完成或失败）不会盖掉它：任务记成已取消，失败原因保留；worker 按平台记下的结果显示（「已取消」） | `jobs.finish`、`worker.JobRun.execute` | `test_an_accepted_cancel_is_never_overwritten_by_a_result`、`test_the_worker_says_cancelled_when_the_cancel_won` |
| worker 连不上平台时按自己的时钟算租约（领取请求发出时起算，每次续租成功从那次请求发出时重算），到期就停下命令、作废这次领取，不会和平台重新派出的领取并跑 | `worker.JobRun.expired`、`_keep`、`_spawn` | `test_a_worker_cut_off_from_the_platform_stops_when_its_lease_runs_out` |
| 推理环境的包清单包括 conda 包和 `pip freeze --all`；源码有未提交的改动时记改动内容（`git diff HEAD` 和没被忽略的新文件）的哈希，再改一次也看得出；包清单先落盘再登记，`fw-server verify` 也核对它；包清单里网址的账号密码、`?` 后的参数（token、签名）和 conda 的 `/t/<token>/` 去掉再存（别的账号能读到包清单），`#` 后的哈希保留 | `worker.conda_listing`、`redact`、`source_state`、`jobs.register_runner`、`jobs.verify_inputs`、`cli.verify_inputs` | `test_a_runner_sees_a_second_uncommitted_change_and_pip_packages`、`test_a_runner_listing_is_durable_and_verified`、`test_a_runner_listing_keeps_no_credentials` |
| 结束的任务不再变，任务不能删；推理环境的版本只增不改 | 数据库触发器（`0005_jobs.sql`） | `test_finish_is_safe_to_repeat_and_final` |
| 交结果、发日志可以安全重发（同一次领取、同样的结束方式返回已有的，两个同时到也一样：先拿行锁再查；同一序号的日志只存一次）；日志每段编号一次，没收到回应就带原编号重发，下一段不会占用它；同一时刻只有一个线程发日志，任务结束时等续租线程还没回来的请求，不并发重发同一段；`fw job log -f` 遇到重新领取时从新一次的开头读 | `jobs.finish`、`add_log`，`Api.post_retried`，`worker.JobRun._send_log`，`cli.cmd_job_log` | `test_finish_is_safe_to_repeat_and_final`、`test_two_identical_finishes_at_once_both_get_the_job`、`test_the_end_of_a_job_waits_for_a_log_request_still_on_its_way`、`test_a_log_piece_whose_answer_was_lost_keeps_its_number`、`test_following_the_log_across_a_second_claim` |
| worker 收到取消或租约失效时，杀掉正在跑的命令和它启动的子进程（单独的进程组，读包清单的 conda、读源码的 git 也一样）；命令自己结束时，它留在组里的进程也杀掉，不会占着输出管道拖住任务；租约失效时不交结果 | `worker.JobRun._spawn`、`capture`、`_ended`、`_kill`、`execute` | `test_cancel`、`test_worker_stops_when_its_lease_is_taken_back`、`test_cancel_stops_a_real_check_with_its_children`、`test_what_a_command_leaves_running_never_holds_up_the_job`、`test_cancel_stops_a_check_whose_git_hangs` |
| 任务只发给推理环境所在的机器 | `jobs.claim` | `test_jobs_go_only_to_the_runners_machine` |
| 恢复时核对全部通过才对外服务，失败换回原数据库；恢复后追溯链一样完整 | `deploy/restore.sh` | CI 的部署演练（`tools/deploy-drill.sh`） |

## 数据库和迁移

- 表结构只通过 `server/fwserver/migrations/` 里的编号 SQL 文件改：`0002_xxx.sql`、`0003_xxx.sql`……（已经部署过的迁移不再改，改表结构就加下一个编号）服务启动时按顺序应用没应用过的。
- **应用过的迁移文件不能再改**：数据库记着每个文件的哈希，改了服务拒绝启动（`test_edited_or_newer_schema_is_refused`）。要改就再加一个新文件。
- 数据库比程序新时服务也拒绝启动（防止旧程序写新库）。
- 每个模块管自己的表，只有它写；上层模块可以读下层的表（例如 `models` 读快照的 `trainset_versions`），下层不读上层的（`datasets.verify` 要核对模型文件时由调用方传入 `models.verify_inputs`）。
- 不用 ORM，SQL 写在模块里，用 psycopg 3 的参数绑定，不拼字符串。

## 格式和接口的版本

这些一旦有数据或有别的机器在用，就不能悄悄改：

- **清单格式 1**（`docs/manifest-format.md`）：清单哈希就是版本内容的标识，改格式会让所有旧版本的哈希对不上。要改只能出「清单格式 2」，两种都能读。
- **训练集快照格式 1**（`docs/trainset-format.md`，`trainset.json` 里的 `fw-trainset 1`）：训练代码按它读目录。可以往 `trainset.json` 加键、加新的格式目录；改目录结构、已有键的含义或 `trainset_layout` 摆出来的路径要出 `fw-trainset 2`。划分算法（`_cut`）和标注转换（`formats.py`）改了不影响已有快照（它们冻结的是结果），但同样的参数会得到不一样的新快照，要在更新记录里写清楚。
- **fw-objects 1**（`contract.py` 的批量传输格式）：新旧 `fw` 和服务端要能互通；要改就出新的格式号，服务端同时支持一段时间。
- **HTTP 接口 `/api/v1`**：只加字段、加接口；改含义或删字段要出 `/api/v2`。
- **路径规则**（`contract.path_problem`、`collisions`）：放宽会让 Windows 上取不回；收紧会让旧版本在新规则下报错。改之前要想清楚旧数据。

## 测试

```
python -m venv .venv && . .venv/bin/activate
pip install -c deploy/constraints.txt -e ./client -e ./server pytest import-linter pillow   # pillow 只给测试用
lint-imports
pytest -q tests/client                                                  # 不需要数据库
FW_TEST_ADMIN_DSN=postgresql://fw@127.0.0.1:5432/postgres pytest -q tests
FW_RANDOM_VERSIONS=60 FW_RANDOM_SEEDS=1,2,3,4,5 pytest -q tests/server/test_random_versions.py
```

- `FW_TEST_ADMIN_DSN` 是一个能建库的 PostgreSQL 16 账号，测试会建临时库并删掉。
- `tests/fakeapi.py` 是内存里的假服务，客户端测试用它，不需要数据库。
- 服务端测试共用一个服务和文件库：**每个测试的数据要独一无二**（加随机盐，见 `test_failures.py` 的 `folder()`），不然一个测试弄坏文件会影响别人的版本。
- 故障用 `monkeypatch` 注入：替换 `os.fsync`、`os.unlink`、`os.scandir`、`syncfs`，或改 `store._boot_id` 模拟重启。不要依赖真的断电或真的拔盘。
- 以 root 运行时，`chmod 000` 之类的权限测试会跳过；要验证它们，用普通用户跑一遍。
- 不要依赖文件修改时间的精度（Windows 和 Linux 不一样）；要让缓存索引过时，改索引里的那一行。
- 真实现场数据**不要**提交到仓库，测试数据都是生成的。

CI（`.github/workflows/ci.yml`）有五个作业：服务端和全部测试（Linux + PostgreSQL 16 + `lint-imports`），`fw` 客户端（Ubuntu Python 3.8，Windows Python 3.8 和 3.12），部署演练（docker compose 部署、导入中在线备份、在另一套部署上恢复后取回）。不允许跳过或删掉测试来让 CI 变绿。

## 怎么加新功能

**加一条 `fw` 命令或选项**：逻辑写在 `ops.py`（训练脚本也能调），`cli.py` 只解析参数和打印；在 `tests/client/` 用假服务测；在 `docs/commands.md` 里写用途、例子和常见问题；`CHANGELOG.md` 记一笔。

**加一个接口**：在 `app.py` 加路由，业务逻辑放在 `datasets.py` 或新的同层模块，检查角色；`api.py` 加对应的方法；`tests/server/` 用真实服务测，包括没权限和出错的情况。

**加表或改表**：新的迁移文件；新表归哪个模块就写在哪个模块；冻结或不可变的数据用触发器守住，并写测试证明改不了。

**加导入检查**：写在 `checks.py`，分清「拒绝」（放进 `blocking()`）和「只提醒」；拒绝的要给出能照着做的办法和放行的选项。

**下一步（第二步其余部分）**：模型版本、引擎、评估报告、发布和追溯链，会是 `server/fwserver/` 里在 `trainsets.py` 之上的新模块和新的迁移，客户端加 `fw model …` 一组命令。模型版本引用训练集快照的版本 id 和它的类别表。

## 约定

- 给人看的输出、报错、命令帮助用中文，写成能照着做的话；代码、注释、提交说明里的技术描述用英文或中文都行，保持一个文件里一致。
- 报错时说清楚发生了什么和下一步怎么办，不打印 Python 堆栈（`FwError`、`DatasetError` 等会被命令行接住）。
- 一个 PR 做一件事；修复先写复现测试，确认它在旧代码上失败。
- 部署和升级不能丢数据：任何会动数据库或文件库的脚本，失败时要能回到原来的状态。
