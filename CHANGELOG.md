# 更新记录

## 0.1.0（未发布）

第一步：文件库、数据集版本、`fw` 命令行，加部署、在线备份和恢复。

- 平台服务 `fw-server`（FastAPI + PostgreSQL 16）和按 SHA-256 存文件的文件库，docker compose 部署（`deploy/setup.sh`）。
- 账号四种角色（管理员、算法工程师、现场实施、部署），每台机器一个凭据，能列出、吊销；停用账号立即吊销它的全部凭据。这一步只有管理员和算法工程师能用数据集。
- 数据集和版本：每个版本是一个目录的完整清单，冻结后数据库拒绝任何修改；版本之间的新增、删除、修改、改名对比。
- `fw` 命令行和 Python 包（Python 3.8 起，Windows、Linux）：`dataset create/list/versions/import/diff`、`get`、`export`、`verify`、`cache`。导入前检查目录（读不了的目录、链接和 Windows 目录联接、特殊文件都会拦下，不会悄悄漏掉）、预览相比父版本的变化并确认，只上传平台上没有的文件；取回时只下载本机缓存里没有的，每个文件按清单核对。几个 `fw` 进程共用一个缓存时互不干扰，不会删掉别的进程刚装好的文件；`--hardlink` 只限 Linux，清缓存不会让取出的文件变成可写。
- 写盘出错（syncfs）后停止接受写入，盘满写不下标记时也一样；`fw-server verify --rehash` 通过后才恢复，它也核对导入中断时留下、还没有版本用到的文件。
- `fw-server verify [--rehash] [--repair-from 备份]` 核对文件库，`--rehash` 从盘上读出来重算（还没有版本用到的文件坏了就移到 `quarantine/`，重新导入时再上传），能从备份修复，修复的文件落盘后才报告。
- `deploy/backup.sh` 在线备份（不停服务）；`deploy/restore.sh` 恢复，按恢复的数据库逐个重算哈希、从备份补回缺的和坏的文件，全部通过才对外服务，任何一步失败都换回原数据库。
- `tools/bench.py` 规模测速，`tools/deploy-drill.sh` 部署和恢复演练。
