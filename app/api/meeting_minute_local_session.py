"""meeting_domain - 本地 AI 会议纪要会话历史 API。

这里的 “session” 指的是纪要生成后的历史快照，不是 WebSocket 会话。

作用：
1. 查询某个会议下所有本地纪要历史快照。
2. 查看指定历史快照详情。
3. 编辑历史快照；若编辑的是最新快照，会同步覆盖“当前纪要视图”中的摘要/待办/流式转写。
4. 删除历史快照。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.meeting_domain import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_domain.meeting_minute_local_service import local_meeting_minute_service
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/meetings/minutes/local", tags=["meeting_domain_local_minutes_session"])


# 步骤说明（本地纪要历史列表）：
# 1) 校验会议存在；
# 2) 读取所有本地纪要快照；
# 3) 返回按创建顺序排序的历史快照。
@router.get(
    "/{meeting_id}/sessions",
    response_model=StandardResponse[list[schemas.LocalMeetingMinutesSessionInDB]],
)
def list_minutes_sessions(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = local_meeting_minute_service.list_minutes_sessions(db, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StandardResponse(success=True, data=data, message="获取会话历史成功")


# 步骤说明（本地纪要历史详情）：
# 1) 校验会议存在；
# 2) 校验 session_id 属于该会议；
# 3) 返回单个历史快照。
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
    try:
        data = local_meeting_minute_service.get_minutes_session(db, meeting_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not data:
        raise HTTPException(status_code=404, detail="会话历史不存在")
    return StandardResponse(success=True, data=data, message="获取会话历史详情成功")


# 步骤说明（本地纪要历史更新）：
# 1) 校验会议与历史快照归属；
# 2) 覆盖历史快照字段；
# 3) 若当前修改的是最新版本，则同步更新“当前纪要视图”。
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
    try:
        data = local_meeting_minute_service.update_minutes_session(db, meeting_id, session_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not data:
        raise HTTPException(status_code=404, detail="会话历史不存在")
    return StandardResponse(success=True, data=data, message="会话历史已更新")


# 步骤说明（本地纪要历史删除）：
# 1) 校验会议存在；
# 2) 校验历史快照归属；
# 3) 删除历史快照，不直接改动其他历史快照。
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
    try:
        deleted = local_meeting_minute_service.delete_minutes_session(db, meeting_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="会话历史不存在")
    return StandardResponse(success=True, data=None, message="会话历史已删除")
