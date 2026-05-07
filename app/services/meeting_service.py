"""会议主对象服务。"""

import pytz
from datetime import datetime
from typing import Optional

from dateutil import parser
from sqlalchemy import case
from sqlalchemy.orm import Session

from app.models import database, schemas

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

    @staticmethod
    def _to_schema(row: database.Meeting) -> schemas.MeetingInDB:
        return schemas.MeetingInDB.model_validate(row)

    def create_meeting(
        self,
        db: Session,
        meeting: schemas.MeetingCreate,
        creator_id: Optional[str] = None,
    ) -> schemas.MeetingInDB:
        data = meeting.model_dump()
        data["date"] = to_beijing_naive(data["date"])
        row = database.Meeting(creator_id=creator_id, **data)
        db.add(row)
        db.commit()
        db.refresh(row)
        return self._to_schema(row)

    def get_meeting(self, db: Session, meeting_id: int) -> Optional[schemas.MeetingInDB]:
        row = (
            db.query(database.Meeting)
            .filter(database.Meeting.id == meeting_id)
            .first()
        )
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

        row = (
            db.query(database.Meeting)
            .filter(database.Meeting.id == meeting_id)
            .first()
        )
        if not row:
            return None

        if "date" in data:
            data["date"] = to_beijing_naive(data["date"])
        for key, value in data.items():
            setattr(row, key, value)

        db.commit()
        db.refresh(row)
        return self._to_schema(row)

    def delete_meeting(self, db: Session, meeting_id: int) -> bool:
        row = (
            db.query(database.Meeting)
            .filter(database.Meeting.id == meeting_id)
            .first()
        )
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    def get_meetings_by_creator(self, db: Session, creator_id: str) -> list[schemas.MeetingInDB]:
        rows = (
            db.query(database.Meeting)
            .filter(database.Meeting.creator_id == creator_id)
            .order_by(
                case((database.Meeting.date.is_(None), 1), else_=0),
                database.Meeting.date.desc(),
                database.Meeting.id.desc(),
            )
            .all()
        )
        return [self._to_schema(r) for r in rows]


meeting_service = MeetingService()
