# fw

数据飞轮平台的命令行和 Python 客户端，给服务器和训练机用（人日常操作用网页）。Python 3.8 起，Windows 和 Linux 都能用。

```
pip install ./client
fw login --server http://<平台地址>:8000
fw dataset import SES-TX /data/SES/TX_v1
fw get SES-TX@1 --to /data/train/SES-TX@1
```

完整说明见仓库根目录的 README.md。
