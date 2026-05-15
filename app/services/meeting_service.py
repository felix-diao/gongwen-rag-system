"""会议主对象服务。"""

import pytz
from datetime import datetime, timedelta
from typing import Optional

from dateutil import parser
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.models import database, schemas

SH = pytz.timezone("Asia/Shanghai")


class DuplicateMeetingError(Exception):
    """同一创建者下已存在相同标题与会议时间的会议。"""

    def __init__(self, message: str = "已存在相同名称和会议时间的会议"):
        self.message = message
        super().__init__(message)


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


def to_beijing_naive_trunc_minute(dt_val: str | datetime | None) -> Optional[datetime]:
    """北京时间 naive，秒与微秒归零，用于「同一分钟」维度比对。"""
    d = to_beijing_naive(dt_val)
    if d is None:
        return None
    return d.replace(second=0, microsecond=0)


class MeetingService:
    """会议主表服务。"""

    @staticmethod
    def _to_schema(row: database.Meeting) -> schemas.MeetingInDB:
        return schemas.MeetingInDB.model_validate(row)

    @staticmethod
    def _normalize_title(title: str) -> str:
        return title.strip()

    def _existing_same_title_and_date(
        self,
        db: Session,
        *,
        creator_id: Optional[str],
        title: str,
        date_val: Optional[datetime],
        exclude_meeting_id: Optional[int] = None,
    ) -> Optional[database.Meeting]:
        """同一 creator 下是否存在相同标题（trim）且会议时间落在同一分钟内的记录。"""
        if date_val is None:
            return None
        nt = self._normalize_title(title)
        if not nt:
            return None
        minute_start = to_beijing_naive_trunc_minute(date_val)
        if minute_start is None:
            return None
        minute_end = minute_start + timedelta(minutes=1)
        conditions = [
            func.trim(database.Meeting.title) == nt,
            database.Meeting.date >= minute_start,
            database.Meeting.date < minute_end,
        ]
        if creator_id is not None:
            conditions.append(database.Meeting.creator_id == creator_id)
        else:
            conditions.append(database.Meeting.creator_id.is_(None))
        if exclude_meeting_id is not None:
            conditions.append(database.Meeting.id != exclude_meeting_id)
        return db.query(database.Meeting).filter(and_(*conditions)).first()

    def create_meeting(
        self,
        db: Session,
        meeting: schemas.MeetingCreate,
        creator_id: Optional[str] = None,
    ) -> schemas.MeetingInDB:
        data = meeting.model_dump()
        data["date"] = to_beijing_naive(data["date"])
        if self._existing_same_title_and_date(
            db,
            creator_id=creator_id,
            title=data["title"],
            date_val=data["date"],
        ):
            raise DuplicateMeetingError()
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

        if "title" in data or "date" in data:
            eff_title = data["title"] if "title" in data else row.title
            eff_date = data["date"] if "date" in data else row.date
            if self._existing_same_title_and_date(
                db,
                creator_id=row.creator_id,
                title=eff_title,
                date_val=eff_date,
                exclude_meeting_id=meeting_id,
            ):
                raise DuplicateMeetingError()

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
