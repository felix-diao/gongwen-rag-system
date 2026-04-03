"""meeting_domain - 会议主对象服务。

这层只负责会议本身的数据库读写，不负责音频上传或纪要生成。

设计边界：
1. API 层负责鉴权和 HTTP 错误码转换。
2. 本 service 只做数据标准化和数据库操作。
3. 会议删除产生的级联清理由 API 层协调其他 service 一起完成。
"""

from sqlalchemy.orm import Session
from app.models.meeting_domain import database, schemas
from app.utils.logger import get_logger


logger = get_logger("meeting_service")

import pytz
from dateutil import parser
SH = pytz.timezone("Asia/Shanghai")

# 步骤说明（时间标准化）：
# 1) 支持 str / datetime 两种输入；
# 2) 若带时区则先转为北京时间再去掉 tzinfo；
# 3) 若无时区则按“已是北京时间”处理。
def to_beijing_naive(dt_val):
    """把任意输入时间归一化为“北京时间 naive datetime”。

    背景：
    当前 meeting_domain 沿用历史存储策略，`Meeting.date` 在库里保存为不带时区的北京时间。
    因此 create/update 时都必须统一走这里，避免出现混杂时区导致的排序和展示问题。
    """
    if dt_val is None:
        return None

    # str -> datetime
    if isinstance(dt_val, str):
        dt = parser.parse(dt_val)
    else:
        dt = dt_val  # datetime

    # 有 tz：先转上海再去 tz
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(SH).replace(tzinfo=None)

    # 无 tz：直接认为就是北京时间，保持不动
    return dt

class MeetingService:
    """会议主表服务。"""

    # 创建会议
    def create_meeting(self, db: Session, meeting: schemas.MeetingCreate, creator_id: str = None):
        # 创建时只做一件额外工作：把会议时间统一转成北京时间 naive datetime。
        data = meeting.dict()
        # ✅ 关键：修正 date
        if "date" in data:
            data["date"] = to_beijing_naive(data["date"])
        if creator_id:
            data["creator_id"] = creator_id
        db_meeting = database.Meeting(**data)
        db.add(db_meeting)
        db.commit()
        db.refresh(db_meeting)
        return db_meeting
    
    # 获取会议信息
    def get_meeting(self, db: Session, meeting_id: int):
        return db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
    
    # 更新会议信息
    def update_meeting(self, db: Session, meeting_id: int, meeting_update: schemas.MeetingUpdate):
        db_meeting = self.get_meeting(db, meeting_id)
        if db_meeting:
            for key, value in meeting_update.dict(exclude_unset=True).items():
                if key == "date":
                    value = to_beijing_naive(value)
                setattr(db_meeting, key, value)
            db.commit()
            db.refresh(db_meeting)
        return db_meeting
    
    # 删除会议
    def delete_meeting(self, db: Session, meeting_id: int):
        # 这里只删除主表记录；关联数据删除由更高一层协调，避免 service 间隐式耦合。
        db_meeting = self.get_meeting(db, meeting_id)
        if db_meeting:
            db.delete(db_meeting)
            db.commit()
            return True
        return False

    # 根据创建者ID获取该用户创建的所有会议
    def get_meetings_by_creator(self, db: Session, creator_id: str):
        return (
            db.query(database.Meeting)
            .filter(database.Meeting.creator_id == creator_id)
            .order_by(database.Meeting.date.desc(), database.Meeting.id.desc())
            .all()
        )

# 供 API 层直接复用的单例。
meeting_service = MeetingService()
