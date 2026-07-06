# ── DeepResearch Dockerfile ──────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.title="DeepResearch"
LABEL org.opencontainers.image.description="轻量级多 Agent 深度研究助手"

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 工作目录
WORKDIR /app

# 依赖安装（利用 Docker 缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 运行时数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 8764

# 启动命令
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8764"]
