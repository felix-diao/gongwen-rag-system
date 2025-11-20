import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session

from app.models import schemas2
from app.services.meeting_minute_service import minutes_service
from app.services.meeting_service import MeetingService
from app.models.database import get_db

# 路由
router = APIRouter(prefix="/api/minutes", tags=["meeting_minutes"])

logger = logging.getLogger(__name__)

meeting_service = MeetingService()


# 生成会议纪要接口（基于会议内容文本）
@router.post("/{meeting_id}/generate", response_model=schemas2.MeetingMinutesInDB)
def generate_meeting_minutes(meeting_id: int, file_ids: Optional[List[int]] = Body(None), db: Session = Depends(get_db)):
    logger.info(f"生成会议纪要，会议ID: {meeting_id}，所选文件ids: {file_ids}")

    # 验证会议是否存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        logger.warning(f"生成纪要时未找到会议，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")

    # 调用 MinutesService 生成纪要（会合并会议文本与文件并调用 LLM）
    result = minutes_service.generate_minutes(db, meeting_id, selected_file_ids=file_ids)
    if not result:
        raise HTTPException(status_code=500, detail="生成纪要失败")
    return result


# 获取会议纪要
@router.get("/{meeting_id}", response_model=schemas2.MeetingMinutesInDB)
def get_meeting_minutes(meeting_id: int, db: Session = Depends(get_db)):
    logger.info(f"获取会议纪要，会议ID: {meeting_id}")
    minutes = minutes_service.get_minutes_by_meeting(db, meeting_id)
    if not minutes:
        raise HTTPException(status_code=404, detail="会议纪要未找到")
    return minutes


# 更新会议纪要
@router.put("/{meeting_id}", response_model=schemas2.MeetingMinutesInDB)
def update_meeting_minutes(meeting_id: int, minutes_update: schemas2.MeetingMinutesUpdate, db: Session = Depends(get_db)):
    logger.info(f"更新会议纪要，会议ID: {meeting_id}")
    updated = minutes_service.update_minutes(db, meeting_id, minutes_update)
    if not updated:
        raise HTTPException(status_code=404, detail="会议纪要未找到")
    logger.info(f"成功更新会议纪要，会议ID: {meeting_id}")
    return updated


# 删除会议纪要
@router.delete("/{meeting_id}")
def delete_meeting_minutes(meeting_id: int, db: Session = Depends(get_db)):
    logger.info(f"删除会议纪要，会议ID: {meeting_id}")
    success = minutes_service.delete_minutes(db, meeting_id)
    if not success:
        raise HTTPException(status_code=404, detail="会议纪要未找到")
    logger.info(f"已删除会议纪要，会议ID: {meeting_id}")
    return {"message": "会议纪要已删除"}


# 重新生成会议纪要（可选择部分文件）
@router.post("/{meeting_id}/regenerate", response_model=schemas2.MeetingMinutesInDB)
def regenerate_meeting_minutes(meeting_id: int, file_ids: Optional[List[int]] = Body(None), db: Session = Depends(get_db)):
    logger.info(f"重新生成会议纪要，会议ID: {meeting_id}，所选文件ids: {file_ids}")
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")
    result = minutes_service.generate_minutes(db, meeting_id, selected_file_ids=file_ids)
    if not result:
        raise HTTPException(status_code=500, detail="生成纪要失败")
    return result
