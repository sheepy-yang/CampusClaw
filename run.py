"""CampusClaw 启动入口：python run.py  →  http://127.0.0.1:5000/"""
from __future__ import annotations

from app.config import Config, DB_PATH
from app.server import build_server


def main():
    config = Config.load()  # 缺 SESSION_SECRET 会直接退出
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在（{DB_PATH}）。请先运行：python scripts/init_db.py")
    httpd = build_server(config)
    print(f"CampusClaw 已启动: http://{config.host}:{config.port}/  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
