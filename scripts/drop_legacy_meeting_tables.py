"""删除旧会议体系遗留表。

用途：
1. 当项目已切换到 `meeting_domain` 新版表结构后，用于清理重构前遗留的旧表。
2. 只删除明确列在 `LEGACY_TABLES` 里的旧会议体系表，不触碰 meeting_domain 新表。

风险说明：
1. 这是破坏性脚本，执行后旧表及其数据不可恢复。
2. 适合在确认新表已经迁移完毕、旧接口已不再依赖旧表后执行。
"""

import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.database import SessionLocal


LEGACY_TABLES = [
    "local_meeting_audios",
    "volc_meeting_audios",
    "meeting_files",
    "meeting_summaries",
    "meeting_action_items",
    "meeting_decision_items",
]


def drop_legacy_meeting_tables() -> None:
    # 明确逐表 DROP，避免误删新表；这里不做自动探测，宁可显式维护名单。
    db = SessionLocal()
    try:
        for table in LEGACY_TABLES:
            db.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        db.commit()
        print("legacy tables dropped:")
        for table in LEGACY_TABLES:
            print(f"- {table}")
    finally:
        db.close()


if __name__ == "__main__":
    drop_legacy_meeting_tables()
