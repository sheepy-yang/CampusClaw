# CampusClaw 迭代 1：Python 标准库 + SQLite，零第三方依赖
FROM python:3.12-slim

WORKDIR /app

# 仅复制运行所需文件；data/、.env、openspec/、.git 不入镜像（见 .dockerignore）
COPY app/ ./app/
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY run.py ./

# 运行期数据目录（被 docker-compose named volume 覆盖，跨容器持久化）
RUN mkdir -p /app/data/uploads/.tmp

# 容器内必须监听 0.0.0.0 才能从宿主访问；compose 的 environment 会覆盖 .env 中的同名值
ENV HOST=0.0.0.0
ENV PORT=5000
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# /health 只表存活、不查库（D9），与业务接口 503 失败路径分离
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=5).read()"

# 首次启动：DB 不存在则跑 init_db.py 建库与种子；已存在则跳过——
# 保证 docker compose down 再 up 不重置既有数据（D12 持久化语义）
CMD ["sh", "-c", "if [ ! -f /app/data/campusclaw.db ]; then python scripts/init_db.py; fi && exec python run.py"]
