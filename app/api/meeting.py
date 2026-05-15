"""提供会议主表（meetings）的创建、列表、详情、更新与删除。

不负责音频上传、流式转写与纪要生成；删除会议时按顺序清理本地纪要、火山纪要、会议音频及对象存储中的文件，最后删除主表行。
列表依赖鉴权上下文中的 user_id，缺失时返回 401；主记录不存在时多为 404。
排障：先确认 meetings 是否有对应 id；删除失败时逐级查看 local/volc 纪要清理与 meeting_audio_service 日志。
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_audio_service import meeting_audio_service
from app.services.meeting_minute_local_service import local_meeting_minute_service
from app.services.meeting_minute_volc_service import volc_meeting_minute_service
from app.services.meeting_service import DuplicateMeetingError, meeting_service
from app.utils.auth import get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_api")

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


# 创建会议并落库，返回完整会议实体。
# 1. 从当前用户上下文取 creator_id（user_id）。
# 2. meeting_service 向 meetings 表插入一行。
# 3. 将 RETURNING 结果封装为 MeetingInDB 返回。
@router.post("", response_model=StandardResponse[schemas.MeetingInDB])
def create_meeting(
    meeting: schemas.MeetingCreate,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("user_id")
    logger.info("创建会议请求 title=%s creator_id=%s", meeting.title, user_id)
    try:
        result = meeting_service.create_meeting(db, meeting, creator_id=user_id)
    except DuplicateMeetingError as e:
        logger.warning("创建会议失败：重复 title=%s creator_id=%s", meeting.title, user_id)
        raise HTTPException(status_code=409, detail=e.message) from e
    logger.info("创建会议成功 meeting_id=%s creator_id=%s", result.id, user_id)
    return StandardResponse(success=True, data=result, message="会议创建成功")


# 列出当前登录用户作为创建者的全部会议。
# 1. 读取 user_id，未登录或无 user_id 时 401。
# 2. 按 creator_id 查询 meetings，按 date、id 倒序。
# 3. 返回 MeetingInDB 列表。
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


# 按主键查询单条会议详情。
# 1. meeting_service.get_meeting 按 id 查 meetings。
# 2. 无行则 404。
# 3. 有行则返回 MeetingInDB。
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


# 部分更新会议字段。
# 1. meeting_update 仅包含客户端 set 过的字段。
# 2. meeting_service 执行 UPDATE 并刷新 updated_at。
# 3. 无匹配行则 404；成功则返回最新 MeetingInDB。
@router.put("/{meeting_id}", response_model=StandardResponse[schemas.MeetingInDB])
def update_meeting(
    meeting_id: int,
    meeting: schemas.MeetingUpdate,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("更新会议请求 meeting_id=%s fields=%s", meeting_id, sorted(meeting.model_fields_set))
    try:
        db_meeting = meeting_service.update_meeting(db, meeting_id, meeting)
    except DuplicateMeetingError as e:
        logger.warning("更新会议失败：重复 meeting_id=%s", meeting_id)
        raise HTTPException(status_code=409, detail=e.message) from e
    if not db_meeting:
        logger.warning("更新会议失败：会议不存在 meeting_id=%s", meeting_id)
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info("更新会议成功 meeting_id=%s", meeting_id)
    return StandardResponse(success=True, data=db_meeting, message="会议更新成功")


# 删除会议及其关联的纪要数据、音频元数据与对象存储文件。
# 1. 确认 meetings 中存在该 id。
# 2. local_meeting_minute_service 删除本会相关本地纪要表数据。
# 3. volc_meeting_minute_service 删除本会相关火山纪要表数据。
# 4. meeting_audio_service 删除本会所有音频行并删 TOS 对象。
# 5. meeting_service 删除 meetings 行；失败则 404。
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
