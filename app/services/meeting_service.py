"""会议主对象服务。"""

import pytz
from datetime import datetime
from typing import Any, Optional

from dateutil import parser
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import schemas

SH = pytz.timezone("Asia/Shanghai")


def to_beijing_naive(dt_val: str | datetime | None) -> Optional[datetime]:
    """把时间标准化为不带时区的北京时间。"""
    if dt_val is None:
        return None

    if isinstance(dt_val, str):
        dt = parser.parse(dt_val)
    else:
        dt = dt_val

    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(SH).replace(tzinfo=None)

    return dt


class MeetingService:
    """会议主表服务。"""

    _SELECT_COLUMNS = """
        id,
        title,
        date,
        location,
        host,
        participants,
        content_text,
        meeting_url,
        status,
        creator_id,
        created_at,
        updated_at
    """

    @classmethod
    def _to_schema(cls, row: Any) -> schemas.MeetingInDB:
        if row is None:
            raise ValueError("会议记录不存在")
        mapping = row._mapping if hasattr(row, "_mapping") else row
        return schemas.MeetingInDB.model_validate(dict(mapping))

    def create_meeting(
        self,
        db: Session,
        meeting: schemas.MeetingCreate,
        creator_id: Optional[str] = None,
    ) -> schemas.MeetingInDB:
        data = meeting.model_dump()
        if "date" in data:
            data["date"] = to_beijing_naive(data["date"])
        data["creator_id"] = creator_id

        row = db.execute(
            text(
                f"""
                INSERT INTO meetings (
                    title,
                    date,
                    location,
                    host,
                    participants,
                    content_text,
                    meeting_url,
                    status,
                    creator_id
                )
                VALUES (
                    :title,
                    :date,
                    :location,
                    :host,
                    :participants,
                    :content_text,
                    :meeting_url,
                    :status,
                    :creator_id
                )
                RETURNING {self._SELECT_COLUMNS}
                """
            ),
            data,
        ).first()
        db.commit()
        return self._to_schema(row)

    def get_meeting(self, db: Session, meeting_id: int) -> Optional[schemas.MeetingInDB]:
        row = db.execute(
            text(
                f"""
                SELECT {self._SELECT_COLUMNS}
                FROM meetings
                WHERE id = :meeting_id
                LIMIT 1
                """
            ),
            {"meeting_id": meeting_id},
        ).first()
        return self._to_schema(row) if row else None

    def update_meeting(
        self,
        db: Session,
        meeting_id: int,
        meeting_update: schemas.MeetingUpdate,
    ) -> Optional[schemas.MeetingInDB]:
        data = meeting_update.model_dump(exclude_unset=True)
        if not data:
            return self.get_meeting(db, meeting_id)

        if "date" in data:
            data["date"] = to_beijing_naive(data["date"])

        assignments = ", ".join(f"{key} = :{key}" for key in data)
        params = {"meeting_id": meeting_id, **data}
        row = db.execute(
            text(
                f"""
                UPDATE meetings
                SET {assignments}, updated_at = NOW()
                WHERE id = :meeting_id
                RETURNING {self._SELECT_COLUMNS}
                """
            ),
            params,
        ).first()
        db.commit()
        return self._to_schema(row) if row else None

    def delete_meeting(self, db: Session, meeting_id: int) -> bool:
        result = db.execute(
            text("DELETE FROM meetings WHERE id = :meeting_id"),
            {"meeting_id": meeting_id},
        )
        db.commit()
        return result.rowcount > 0

    def get_meetings_by_creator(self, db: Session, creator_id: str) -> list[schemas.MeetingInDB]:
        rows = db.execute(
            text(
                f"""
                SELECT {self._SELECT_COLUMNS}
                FROM meetings
                WHERE creator_id = :creator_id
                ORDER BY CASE WHEN date IS NULL THEN 1 ELSE 0 END, date DESC, id DESC
                """
            ),
            {"creator_id": creator_id},
        ).all()
        return [self._to_schema(row) for row in rows]


meeting_service = MeetingService()
