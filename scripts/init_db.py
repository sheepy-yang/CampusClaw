"""
初始化数据库并写入种子数据（双班、双角色、可区分材料）。
运行:  python scripts/init_db.py
口令来自环境变量（.env）：SEED_TEACHER_PASSWORD / SEED_STUDENT_PASSWORD。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import DB_PATH, UPLOAD_DIR, TMP_DIR  # noqa: E402
from app.db import create_schema, get_connection  # noqa: E402
from app.security import hash_password  # noqa: E402
from corpus import EXTRA_MATERIALS  # noqa: E402

CLASSES = [(1, "高一(3)班"), (2, "高一(4)班")]

# (username, role, class_id, 口令环境变量)
USERS = [
    ("teacher3", "teacher", 1, "SEED_TEACHER_PASSWORD"),
    ("student3", "student", 1, "SEED_STUDENT_PASSWORD"),
    ("teacher4", "teacher", 2, "SEED_TEACHER_PASSWORD"),
    ("student4", "student", 2, "SEED_STUDENT_PASSWORD"),
]

# (class_id, uploader_username, 文件名, 标题, 正文)
MATERIALS = [
    (1, "teacher3", "高一3班-语文-荷塘月色教案.md", "高一3班-语文-荷塘月色教案",
     "# 《荷塘月色》教案（高一3班·语文）\n\n## 教学目标\n- 体会散文的意境美与语言美\n- 学习通感、叠词等修辞手法\n\n## 教学过程\n1. 导入：月夜意象讨论\n2. 朗读全文，圈画叠词\n3. 品读第四、五段\n\n## 作业\n- 背诵第四段；摘抄 5 个叠词并造句。\n"),
    (1, "teacher3", "高一3班-数学-集合入门知识点.txt", "高一3班-数学-集合入门知识点",
     "集合入门知识点（高一3班·数学）\n\n一、集合的概念\n  一定范围内某些确定的、不同的对象的全体构成一个集合。\n\n二、集合的表示\n  列举法：{1, 2, 3}\n  描述法：{x | x > 0}\n\n三、集合间关系\n  子集、真子集、相等；空集是任何集合的子集。\n\n练习：写出集合 {a,b} 的全部子集。\n"),
    (2, "teacher4", "高一4班-语文-故都的秋教案.md", "高一4班-语文-故都的秋教案",
     "# 《故都的秋》教案（高一4班·语文）\n\n## 教学目标\n- 把握文章\"清、静、悲凉\"的感情基调\n- 体会以情驭景、以景显情的写法\n\n## 教学过程\n1. 导入：南北之秋对比\n2. 精读五幅秋景图\n3. 讨论结尾直抒胸臆段\n\n## 作业\n- 仿写一段 200 字的校园秋色。\n"),
    (2, "teacher4", "高一4班-数学-函数概念知识点.txt", "高一4班-数学-函数概念知识点",
     "函数概念知识点（高一4班·数学）\n\n一、函数的定义\n  设 A、B 是非空数集，若按某种对应关系 f，对 A 中任意 x，\n  B 中都有唯一确定的 y 与之对应，则称 f: A→B 为函数。\n\n二、三要素\n  定义域、对应关系、值域。\n\n三、区间表示\n  开区间 (a,b)、闭区间 [a,b]、半开区间 [a,b)。\n\n练习：求 f(x)=1/(x-1) 的定义域。\n"),
]
MATERIALS.extend(EXTRA_MATERIALS)


def main():
    # 复用服务端的 .env 加载与必填校验
    from app.config import Config
    Config.load()  # SESSION_SECRET 必填

    teacher_pw = os.environ.get("SEED_TEACHER_PASSWORD", "").strip()
    student_pw = os.environ.get("SEED_STUDENT_PASSWORD", "").strip()
    if not teacher_pw or not student_pw:
        raise SystemExit(
            "配置错误：请在 .env 或环境变量中设置 SEED_TEACHER_PASSWORD 与 SEED_STUDENT_PASSWORD。"
        )

    # 重置：删库 + 清空上传目录，保证种子可重复生成
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        p.unlink(missing_ok=True)
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    create_schema()
    conn = get_connection()
    try:
        conn.executemany("INSERT INTO classes (id, name) VALUES (?,?)", CLASSES)

        pw_by_role = {"teacher": teacher_pw, "student": student_pw}
        username_id: dict[str, int] = {}
        for username, role, class_id, _env in USERS:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, class_id) VALUES (?,?,?,?)",
                (username, hash_password(pw_by_role[role]), role, class_id),
            )
            username_id[username] = cur.lastrowid

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        insert_materials(conn, username_id, now, MATERIALS)
    finally:
        conn.close()

    from app.retrieval import backfill_unchunked
    backfill_unchunked(Config.load())

    print(f"✓ 初始化完成: {DB_PATH}")
    print("  班级: 高一(3)班 / 高一(4)班")
    print("  账号: teacher3 / student3 / teacher4 / student4")
    print(f"  材料: {len(MATERIALS)} 份（标题含班级，可按班区分）")
    print("  启动: python run.py")


def insert_materials(conn, username_id: dict[str, int], now: str, materials) -> int:
    """两表同行并落盘。返回新插入的份数。"""
    added = 0
    for class_id, uploader, filename, title, content in materials:
        ext = Path(filename).suffix
        cur = conn.execute(
            """INSERT INTO materials
               (class_id, uploader_id, title, original_name, stored_name,
                size_bytes, content_type, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (class_id, username_id[uploader], title, filename, "",
             len(content.encode("utf-8")),
             "text/markdown; charset=utf-8" if ext == ".md" else "text/plain; charset=utf-8",
             now),
        )
        mid = cur.lastrowid
        stored = f"{mid}{ext}"
        conn.execute("UPDATE materials SET stored_name=? WHERE id=?", (stored, mid))
        conn.execute(
            "INSERT INTO knowledge_entries (material_id, class_id, content, created_at) VALUES (?,?,?,?)",
            (mid, class_id, content, now),
        )
        (UPLOAD_DIR / stored).write_text(content, encoding="utf-8")
        added += 1
    return added


def expand_existing() -> None:
    """把种子中尚未入库的材料补进现有库，不删除账号、会话和已有材料。"""
    from datetime import datetime, timezone

    from app.config import Config
    from app.retrieval import backfill_unchunked, retry_failed_chunks

    Config.load()
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在（{DB_PATH}）。请先运行：python scripts/init_db.py")
    create_schema()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        have = {row["original_name"] for row in conn.execute("SELECT original_name FROM materials")}
        users = {
            row["username"]: row["id"]
            for row in conn.execute("SELECT id, username FROM users")
        }
        missing = [item for item in MATERIALS if item[2] not in have]
        now = datetime.now(timezone.utc).isoformat()
        added = insert_materials(conn, users, now, missing) if missing else 0
    finally:
        conn.close()
    from app.vector_store import VectorStore, VectorUnavailable

    config = Config.load()
    try:
        VectorStore(config).ensure_collection()
    except VectorUnavailable as exc:
        print(f"[QDRANT] 集合未就绪，新切片会保持 failed: {exc}")
    backfill_unchunked(config)
    ready = retry_failed_chunks(config)
    print(f"✓ 已补入 {added} 份材料，重新嵌入成功 {ready} 个切片")


if __name__ == "__main__":
    if "--expand" in sys.argv:
        expand_existing()
    else:
        main()
