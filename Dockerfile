FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISK_IMAGE=/data/disk.img \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# 数据卷：镜像文件独立于容器生命周期，重启/重建容器不丢状态。
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动时确保演示镜像存在（已存在则保留，崩溃后续作依赖它）。
CMD ["sh", "-c", "python -m app.seed \"$DISK_IMAGE\" || true; python -m app.web"]
