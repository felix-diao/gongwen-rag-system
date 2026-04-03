"""meeting_domain - 会议主对象 API。

这个模块只处理“会议”本身的生命周期，不直接承担纪要生成职责。

职责边界：
1. 创建、查询、更新、删除会议主记录。
2. 删除会议时负责串起音频和纪要关联数据的级联清理。
3. 会议相关的音频、实时转写、纪要内容都由其他路由模块负责。
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.meeting_domain import database, schemas
from app.services.meeting_domain.meeting_service import MeetingService
from app.utils.auth import get_current_user
from app.services.meeting_domain.meeting_audio_service import meeting_audio_service
from app.services.meeting_domain.meeting_minute_local_service import local_meeting_minute_service
from app.services.meeting_domain.meeting_minute_volc_service import volc_meeting_minute_service
from app.models.schemas import StandardResponse
from app.utils.logger import get_logger

logger = get_logger("meeting_api")
meeting_service = MeetingService()

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


# 步骤说明（创建会议）：
# 1) 从登录上下文提取创建者 user_id；
# 2) 将会议基础信息交给 service 落库；
# 3) 返回创建后的会议实体。
@router.post("", response_model=StandardResponse[schemas.MeetingInDB])
def create_meeting(
    meeting: schemas.MeetingCreate,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("user_id")
    logger.info(f"创建新会议，标题: {meeting.title}，创建者: {user_id}")
    # 将创建者 user_id 传入服务层保存到 Meeting.creator_id
    result = meeting_service.create_meeting(db, meeting, creator_id=user_id)
    logger.info(f"成功创建会议，ID: {result.id}")
    return StandardResponse(success=True, data=result, message="会议创建成功")


# 步骤说明（当前用户会议列表）：
# 1) 从鉴权上下文提取 user_id；
# 2) 若缺失 user_id，直接返回 401；
# 3) 按创建者筛选会议并按时间倒序返回。
@router.get("", response_model=StandardResponse[List[schemas.MeetingInDB]])
def get_all_meetings(
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("user_id")
    logger.info("获取会议列表，请求用户: %s", user_id)
    if not user_id:
        logger.warning("当前会话缺少 user_id，拒绝访问会议列表")
        raise HTTPException(status_code=401, detail="未认证用户")
    meetings = meeting_service.get_meetings_by_creator(db, user_id)
    return StandardResponse(success=True, data=meetings, message="获取会议列表成功")


# 步骤说明（会议详情）：
# 1) 按 meeting_id 查询会议；
# 2) 不存在返回 404；
# 3) 返回会议完整信息。
@router.get("/{meeting_id}", response_model=StandardResponse[schemas.MeetingInDB])
def get_meeting(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(f"获取会议详情，会议ID: {meeting_id}")
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        logger.warning(f"未找到会议，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info(f"成功获取会议详情，会议ID: {meeting_id}")
    return StandardResponse(success=True, data=db_meeting, message="获取会议详情成功")


# 步骤说明（会议更新）：
# 1) 按 meeting_id 定位记录；
# 2) 仅更新前端传入字段；
# 3) 返回更新后的会议实体。
@router.put("/{meeting_id}", response_model=StandardResponse[schemas.MeetingInDB])
def update_meeting(
    meeting_id: int,
    meeting: schemas.MeetingUpdate,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(f"更新会议信息，会议ID: {meeting_id}")
    db_meeting = meeting_service.update_meeting(db, meeting_id, meeting)
    if not db_meeting:
        logger.warning(f"未找到会议进行更新，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info(f"成功更新会议信息，会议ID: {meeting_id}")
    return StandardResponse(success=True, data=db_meeting, message="会议更新成功")


# 步骤说明（会议删除）：
# 1) 先校验会议存在；
# 2) 清理 local/volc 纪要关联表；
# 3) 删除会议关联音频（对象存储 + 数据库）；
# 4) 再删会议主记录；
# 5) 返回空数据表示删除完成。
@router.delete("/{meeting_id}", response_model=StandardResponse[None])
def delete_meeting(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(f"删除会议，会议ID: {meeting_id}")
    meeting = meeting_service.get_meeting(db, meeting_id)
    if not meeting:
        logger.warning(f"未找到会议进行删除，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")

    local_meeting_minute_service.delete_meeting_minutes_data(db, meeting_id)
    volc_meeting_minute_service.delete_meeting_minutes_data(db, meeting_id)

    # 删除相关音频记录与对象存储文件
    meeting_audio_service.delete_all_audio_by_meeting(db, meeting_id)

    # 删除数据库中的会议记录
    success = meeting_service.delete_meeting(db, meeting_id)
    if not success:
        logger.warning(f"未找到会议进行删除，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")
    logger.info(f"成功删除会议，会议ID: {meeting_id}")
    return StandardResponse(success=True, data=None, message="会议及相关音频已删除")
