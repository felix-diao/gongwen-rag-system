"""删除并重建 meeting_domain 相关表。

用途：
1. 在重构期快速重建 meeting_domain 的所有 ORM 表。
2. 适合本地开发或测试环境，不适合直接在生产环境执行。

风险说明：
1. 该脚本会先 `drop_all` 再 `create_all`，属于破坏性重建。
2. 若数据库中已有正式业务数据，请改用显式 migration，而不是直接运行本脚本。
"""

import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.database import MEETING_DOMAIN_TABLES, engine, SessionLocal


def recreate_meeting_domain_tables() -> None:
    # 先打印即将处理的表，避免误以为脚本会影响全库。
    table_names = sorted(table.name for table in MEETING_DOMAIN_TABLES)
    print("meeting_domain tables:")
    for name in table_names:
        print(f"- {name}")

    # 真实模型已经并入 app.models.database，因此这里只能显式针对 meeting_domain 表做 DDL。
    for table in reversed(MEETING_DOMAIN_TABLES):
        table.drop(bind=engine, checkfirst=True)
    for table in MEETING_DOMAIN_TABLES:
        table.create(bind=engine, checkfirst=True)

    db = SessionLocal()
    try:
        # 重建完成后回查当前 public schema，便于确认操作结果。
        result = db.execute(
            text(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
        )
        current_tables = [row[0] for row in result.fetchall()]
    finally:
        db.close()

    print("recreated successfully, current public tables:")
    for name in current_tables:
        print(f"- {name}")


if __name__ == "__main__":
    recreate_meeting_domain_tables()
