# aoi-flywheel 平台服务镜像。由 deploy/docker-compose.yml 构建（上下文是仓库根目录）。
FROM python:3.12.15-slim-bookworm

# 国内网络可以在 deploy/.env 里设 PIP_INDEX_URL 换成镜像源
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 固定的 uid/gid 10001：宿主机上的文件库目录归它所有（setup.sh 会 chown）
RUN groupadd --system --gid 10001 fw \
 && useradd --system --uid 10001 --gid fw --home-dir /srv --no-create-home fw

COPY deploy/constraints.txt /src/constraints.txt
COPY client /src/client
COPY server /src/server
RUN pip install -c /src/constraints.txt /src/client /src/server && rm -rf /src

USER fw
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=8)"
CMD ["sh", "-c", "fw-server migrate && exec fw-server serve"]
