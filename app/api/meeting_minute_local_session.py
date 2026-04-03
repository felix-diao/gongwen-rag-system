"""本地 AI 会议纪要历史会话 API。

这里的 session 指“某次生成纪要后的历史快照”，不是实时 WebSocket 连接。
控制器负责历史快照的查询、查看、编辑和删除。

运维排障建议：
1. 当前纪要和历史快照不一致时，重点看 update session 日志。
2. 删除历史记录后页面未刷新时，先确认控制器是否返回成功。
3. 历史列表为空时，优先确认 generate 链路是否已生成快照。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_minute_local_service import local_meeting_minute_service
from app.utils.auth import get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_minute_local_session_api")

router = APIRouter(prefix="/api/meetings/minutes/local", tags=["meeting_local_minutes_session"])


# 处理流程（历史快照列表）：
# 1. 校验会议存在。
# 2. 查询该会议下全部本地纪要快照。
# 3. 返回历史快照列表。
@router.get(
    "/{meeting_id}/sessions",
    response_model=StandardResponse[list[schemas.LocalMeetingMinutesSessionInDB]],
)
def list_minutes_sessions(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取本地纪要历史列表请求 meeting_id=%s", meeting_id)
    try:
        data = local_meeting_minute_service.list_minutes_sessions(db, meeting_id)
    except ValueError as exc:
        logger.warning("获取本地纪要历史列表失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("获取本地纪要历史列表成功 meeting_id=%s count=%s", meeting_id, len(data))
    return StandardResponse(success=True, data=data, message="获取会话历史成功")


# 处理流程（历史快照详情）：
# 1. 校验会议存在。
# 2. 校验 session_id 属于当前会议。
# 3. 返回单个历史快照详情。
@router.get(
    "/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[schemas.LocalMeetingMinutesSessionInDB],
)
def get_minutes_session(
    meeting_id: int,
    session_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取本地纪要历史详情请求 meeting_id=%s session_id=%s", meeting_id, session_id)
    try:
        data = local_meeting_minute_service.get_minutes_session(db, meeting_id, session_id)
    except ValueError as exc:
        logger.warning(
            "获取本地纪要历史详情失败 meeting_id=%s session_id=%s error=%s",
            meeting_id,
            session_id,
            exc,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not data:
        logger.warning("获取本地纪要历史详情失败：会话不存在 meeting_id=%s session_id=%s", meeting_id, session_id)
        raise HTTPException(status_code=404, detail="会话历史不存在")
    logger.info("获取本地纪要历史详情成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=data, message="获取会话历史详情成功")


# 处理流程（更新历史快照）：
# 1. 校验会议与会话归属关系。
# 2. 更新历史快照字段。
# 3. 若当前会话是最新版本，则同步覆盖当前纪要视图。
@router.put(
    "/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[schemas.LocalMeetingMinutesSessionInDB],
)
def update_minutes_session(
    meeting_id: int,
    session_id: int,
    payload: schemas.LocalMeetingMinutesSessionUpdate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(
        "更新本地纪要历史会话请求 meeting_id=%s session_id=%s fields=%s",
        meeting_id,
        session_id,
        sorted(payload.model_fields_set),
    )
    try:
        data = local_meeting_minute_service.update_minutes_session(db, meeting_id, session_id, payload)
    except ValueError as exc:
        logger.warning(
            "更新本地纪要历史会话失败 meeting_id=%s session_id=%s error=%s",
            meeting_id,
            session_id,
            exc,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not data:
        logger.warning("更新本地纪要历史会话失败：会话不存在 meeting_id=%s session_id=%s", meeting_id, session_id)
        raise HTTPException(status_code=404, detail="会话历史不存在")
    logger.info("更新本地纪要历史会话成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=data, message="会话历史已更新")


# 处理流程（删除历史快照）：
# 1. 校验会议存在。
# 2. 校验历史快照归属。
# 3. 删除指定历史快照。
@router.delete(
    "/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[None],
)
def delete_minutes_session(
    meeting_id: int,
    session_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("删除本地纪要历史会话请求 meeting_id=%s session_id=%s", meeting_id, session_id)
    try:
        deleted = local_meeting_minute_service.delete_minutes_session(db, meeting_id, session_id)
    except ValueError as exc:
        logger.warning(
            "删除本地纪要历史会话失败 meeting_id=%s session_id=%s error=%s",
            meeting_id,
            session_id,
            exc,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not deleted:
        logger.warning("删除本地纪要历史会话失败：会话不存在 meeting_id=%s session_id=%s", meeting_id, session_id)
        raise HTTPException(status_code=404, detail="会话历史不存在")
    logger.info("删除本地纪要历史会话成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=None, message="会话历史已删除")
