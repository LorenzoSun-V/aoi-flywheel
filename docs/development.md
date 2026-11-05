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
  ops.py             每条命令真正做的事（import_folder、get、export、verify、diff、get_version、
                     create_trainset、get_trainset）
  cli.py             命令行参数和输出格式，只调 ops
  config.py          设置文件和缓存的位置
server/fwserver/     fw-server：只支持 Linux
  app.py             HTTP 接口 /api/v1（FastAPI）
  cli.py             fw-server 管理命令
  trainsets.py       训练集快照：配对图片和标注、核对类别、划分、摆出快照的目录，再用 datasets 建版本
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

- 服务端只能往下调：`cli`、`app` → `trainsets` → `datasets`、`auth` → `store`、`db`、`settings`。
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

`fw trainset create`（`ops.create_trainset`）先调预览接口，确认后再调建快照接口，两次的参数一样。服务端两次都走 `trainsets.plan`，建快照时 `trainsets.create` 再按同样的参数重算一遍，所以冻结的就是预览给人看的：

1. `_sources`：解析来源版本，拒绝快照做来源、没填现场或产线的数据集。
2. `_samples`：按同目录同名配对图片和标注，样本标识是 `<现场>/<产线>/<图片路径>`。
3. `_read_labels`：从文件库读每个标注（同一内容只读一次），核对类别。
4. `_latest_trainset`、`_previous_splits`：同名快照的最新版，从它的目录结构读回每个样本的划分。
5. `_assign`：划分清单、接着上一版、或重新划分；train/val 用 `_cut` 按 `sha256(种子\n标识)` 排序后按比例切。
6. `_tree`：摆出 `train|val|test/<现场>/<产线>/<路径>`，加上 `classes.txt`、`trainset.json`，核对路径规则。
7. `create`：先看最新版还是不是预览时那个（不是就 409），把 `classes.txt`、`trainset.json`、清单写进文件库，再调 `datasets.create_version(trainset=True, record=…)`：在建版本的同一个事务里、冻结之前写 `trainset_versions` 和 `trainset_sources` 两张表。

快照就是一个普通版本，取回、导出、核对、对比都不需要专门的代码。

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
| 快照只能用 `fw trainset create` 建；标注里的类别都在类别表里；冻结的就是预览的；预览后有人建了新版就不建 | `datasets.create_version` 的 `trainset` 参数、`trainsets.create` | `test_trainsets_are_made_only_by_trainset_create`、`test_bad_labels_are_refused`、`test_someone_else_made_one_meanwhile` |
| 接着上一版时，原来的样本不换划分；同样的样本、比例和种子，划分总是一样 | `trainsets._assign`、`_cut` | `test_cut_is_fixed_by_keys_ratio_and_seed`、`test_continue_keeps_every_split` |
| 恢复时核对全部通过才对外服务，失败换回原数据库 | `deploy/restore.sh` | CI 的部署演练（`tools/deploy-drill.sh`） |

## 数据库和迁移

- 表结构只通过 `server/fwserver/migrations/` 里的编号 SQL 文件改：`0002_xxx.sql`、`0003_xxx.sql`……服务启动时按顺序应用没应用过的。
- **应用过的迁移文件不能再改**：数据库记着每个文件的哈希，改了服务拒绝启动（`test_edited_or_newer_schema_is_refused`）。要改就再加一个新文件。
- 数据库比程序新时服务也拒绝启动（防止旧程序写新库）。
- 每个模块管自己的表，别的模块不直接读写它的表。
- 不用 ORM，SQL 写在模块里，用 psycopg 3 的参数绑定，不拼字符串。

## 格式和接口的版本

这些一旦有数据或有别的机器在用，就不能悄悄改：

- **清单格式 1**（`docs/manifest-format.md`）：清单哈希就是版本内容的标识，改格式会让所有旧版本的哈希对不上。要改只能出「清单格式 2」，两种都能读。
- **训练集快照格式 1**（`docs/trainset-format.md`，`trainset.json` 里的 `fw-trainset 1`）：训练代码按它读目录。可以往 `trainset.json` 加键；改目录结构或已有键的含义要出 `fw-trainset 2`。划分算法（`_cut`）改了不影响已有快照（它们冻结的是结果），但同样的参数会分得不一样，要在更新记录里写清楚。
- **fw-objects 1**（`contract.py` 的批量传输格式）：新旧 `fw` 和服务端要能互通；要改就出新的格式号，服务端同时支持一段时间。
- **HTTP 接口 `/api/v1`**：只加字段、加接口；改含义或删字段要出 `/api/v2`。
- **路径规则**（`contract.path_problem`、`collisions`）：放宽会让 Windows 上取不回；收紧会让旧版本在新规则下报错。改之前要想清楚旧数据。

## 测试

```
python -m venv .venv && . .venv/bin/activate
pip install -c deploy/constraints.txt -e ./client -e ./server pytest import-linter
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
