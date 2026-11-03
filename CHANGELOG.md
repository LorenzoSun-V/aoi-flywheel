# 更新记录

## 0.1.0（未发布）

第一步：文件库、数据集版本、`fw` 命令行，加部署、在线备份和恢复。

- 平台服务 `fw-server`（FastAPI + PostgreSQL 16）和按 SHA-256 存文件的文件库，docker compose 部署（`deploy/setup.sh`）。
- 账号四种角色（管理员、算法工程师、现场实施、部署），每台机器一个凭据，能列出、吊销；停用账号立即吊销它的全部凭据。这一步只有管理员和算法工程师能用数据集。
- 数据集和版本：每个版本是一个目录的完整清单，冻结后数据库拒绝任何修改；版本之间的新增、删除、修改、改名对比。
- `fw` 命令行和 Python 包（Python 3.8 起，Windows、Linux）：`dataset create/list/versions/import/diff`、`get`、`export`、`verify`、`cache`。导入前检查目录、预览相比父版本的变化并确认，只上传平台上没有的文件；取回时只下载本机缓存里没有的，每个文件按清单核对。
- `fw-server verify [--rehash] [--repair-from 备份]` 核对文件库，能从备份修复。
- `deploy/backup.sh` 在线备份（不停服务），`deploy/restore.sh` 恢复（失败时换回原数据库）。
- `tools/bench.py` 规模测速，`tools/deploy-drill.sh` 部署和恢复演练。
- 小文件批量传输：导入和取回时小文件一次请求传几千个（16 MB 以上的文件仍然一个一次），每个文件照样按哈希核对；网络断了只重传还没收到的。同一台机器上 20 万个小文件入库从 470 秒降到 53 秒，冷取回从 648 秒降到 136 秒。
