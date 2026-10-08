"""CampusClaw 启动入口：python run.py  →  http://127.0.0.1:5000/"""
from __future__ import annotations

from app.config import Config, DB_PATH
from app.db import create_schema
from app.retrieval import backfill_unchunked, retry_failed_chunks
from app.server import build_server
from app.vector_store import VectorStore, VectorUnavailable


def main():
    config = Config.load()  # 缺 SESSION_SECRET 会直接退出
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在（{DB_PATH}）。请先运行：python scripts/init_db.py")
    create_schema()
    try:
        VectorStore(config).ensure_collection()
    except VectorUnavailable as exc:
        print(f"[QDRANT] 集合未就绪，向量检索将降级: {exc}")
    backfill_unchunked(config)
    try:
        retry_failed_chunks(config)
    except VectorUnavailable as exc:
        print(f"[QDRANT] 未能补嵌入，关键字以外的检索将降级: {exc}")
    httpd = build_server(config)
    print(f"CampusClaw 已启动: http://{config.host}:{config.port}/  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
