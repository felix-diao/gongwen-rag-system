"""会议主对象 API。

该控制器只负责会议主记录的生命周期管理，不直接处理音频上传、
实时转写或纪要生成。会议删除时会协调会议相关的音频与纪要数据清理。

运维排障建议：
1. 先看这里的会议主记录是否存在。
2. 再看音频控制器和纪要控制器是否返回对应数据。
3. 删除失败时重点看级联清理阶段的日志。
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_audio_service import meeting_audio_service
from app.services.meeting_minute_local_service import local_meeting_minute_service
from app.services.meeting_minute_volc_service import volc_meeting_minute_service
from app.services.meeting_service import meeting_service
from app.utils.auth import get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_api")

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


# 处理流程（创建会议）：
# 1. 从登录上下文提取创建者 user_id。
# 2. 调用 meeting_service 创建会议主记录。
# 3. 返回创建后的会议实体。
@router.post("", response_model=StandardResponse[schemas.MeetingInDB])
def create_meeting(
    meeting: schemas.MeetingCreate,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("user_id")
    logger.info("创建会议请求 title=%s creator_id=%s", meeting.title, user_id)
    result = meeting_service.create_meeting(db, meeting, creator_id=user_id)
    logger.info("创建会议成功 meeting_id=%s creator_id=%s", result.id, user_id)
    return StandardResponse(success=True, data=result, message="会议创建成功")


# 处理流程（会议列表）：
# 1. 从鉴权上下文提取 user_id。
# 2. 按创建者查询会议列表。
# 3. 返回按时间倒序排列的会议集合。
@router.get("", response_model=StandardResponse[List[schemas.MeetingInDB]])
def get_all_meetings(
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("user_id")
    logger.info("获取会议列表请求 user_id=%s", user_id)
    if not user_id:
        logger.warning("获取会议列表失败：当前会话缺少 user_id")
        raise HTTPException(status_code=401, detail="未认证用户")
    meetings = meeting_service.get_meetings_by_creator(db, user_id)
    logger.info("获取会议列表成功 user_id=%s count=%s", user_id, len(meetings))
    return StandardResponse(success=True, data=meetings, message="获取会议列表成功")


# 处理流程（会议详情）：
# 1. 按 meeting_id 查询会议主记录。
# 2. 不存在则返回 404。
# 3. 返回单个会议详情。
@router.get("/{meeting_id}", response_model=StandardResponse[schemas.MeetingInDB])
def get_meeting(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取会议详情请求 meeting_id=%s", meeting_id)
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        logger.warning("获取会议详情失败：会议不存在 meeting_id=%s", meeting_id)
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info("获取会议详情成功 meeting_id=%s", meeting_id)
    return StandardResponse(success=True, data=db_meeting, message="获取会议详情成功")


# 处理流程（更新会议）：
# 1. 按 meeting_id 定位会议主记录。
# 2. 仅更新前端显式传入的字段。
# 3. 返回更新后的会议实体。
@router.put("/{meeting_id}", response_model=StandardResponse[schemas.MeetingInDB])
def update_meeting(
    meeting_id: int,
    meeting: schemas.MeetingUpdate,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("更新会议请求 meeting_id=%s fields=%s", meeting_id, sorted(meeting.model_fields_set))
    db_meeting = meeting_service.update_meeting(db, meeting_id, meeting)
    if not db_meeting:
        logger.warning("更新会议失败：会议不存在 meeting_id=%s", meeting_id)
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info("更新会议成功 meeting_id=%s", meeting_id)
    return StandardResponse(success=True, data=db_meeting, message="会议更新成功")


# 处理流程（删除会议）：
# 1. 校验会议主记录存在。
# 2. 清理本地纪要与火山纪要关联数据。
# 3. 清理会议音频及对象存储文件。
# 4. 删除会议主记录。
# 5. 返回删除完成结果。
@router.delete("/{meeting_id}", response_model=StandardResponse[None])
def delete_meeting(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("删除会议请求 meeting_id=%s", meeting_id)
    meeting = meeting_service.get_meeting(db, meeting_id)
    if not meeting:
        logger.warning("删除会议失败：会议不存在 meeting_id=%s", meeting_id)
        raise HTTPException(status_code=404, detail="会议未找到")

    logger.info("开始清理本地纪要数据 meeting_id=%s", meeting_id)
    local_meeting_minute_service.delete_meeting_minutes_data(db, meeting_id)
    logger.info("开始清理火山纪要数据 meeting_id=%s", meeting_id)
    volc_meeting_minute_service.delete_meeting_minutes_data(db, meeting_id)
    logger.info("开始清理会议音频数据 meeting_id=%s", meeting_id)
    meeting_audio_service.delete_all_audio_by_meeting(db, meeting_id)

    success = meeting_service.delete_meeting(db, meeting_id)
    if not success:
        logger.warning("删除会议失败：主记录删除未完成 meeting_id=%s", meeting_id)
        raise HTTPException(status_code=404, detail="会议未找到")

    logger.info("删除会议成功 meeting_id=%s", meeting_id)
    return StandardResponse(success=True, data=None, message="会议及相关音频已删除")
