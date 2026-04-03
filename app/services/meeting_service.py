"""会议主对象服务。"""

import pytz
from datetime import datetime
from typing import Optional

from dateutil import parser
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

    def create_meeting(
        self,
        db: Session,
        meeting: schemas.MeetingCreate,
        creator_id: Optional[str] = None,
    ) -> database.Meeting:
        data = meeting.model_dump()
        if "date" in data:
            data["date"] = to_beijing_naive(data["date"])
        if creator_id:
            data["creator_id"] = creator_id
        db_meeting = database.Meeting(**data)
        db.add(db_meeting)
        db.commit()
        db.refresh(db_meeting)
        return db_meeting

    def get_meeting(self, db: Session, meeting_id: int) -> Optional[database.Meeting]:
        return db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()

    def update_meeting(
        self,
        db: Session,
        meeting_id: int,
        meeting_update: schemas.MeetingUpdate,
    ) -> Optional[database.Meeting]:
        db_meeting = self.get_meeting(db, meeting_id)
        if db_meeting:
            for key, value in meeting_update.model_dump(exclude_unset=True).items():
                if key == "date":
                    value = to_beijing_naive(value)
                setattr(db_meeting, key, value)
            db.commit()
            db.refresh(db_meeting)
        return db_meeting

    def delete_meeting(self, db: Session, meeting_id: int) -> bool:
        db_meeting = self.get_meeting(db, meeting_id)
        if db_meeting:
            db.delete(db_meeting)
            db.commit()
            return True
        return False

    def get_meetings_by_creator(self, db: Session, creator_id: str) -> list[database.Meeting]:
        return (
            db.query(database.Meeting)
            .filter(database.Meeting.creator_id == creator_id)
            .order_by(database.Meeting.date.desc(), database.Meeting.id.desc())
            .all()
        )


meeting_service = MeetingService()
