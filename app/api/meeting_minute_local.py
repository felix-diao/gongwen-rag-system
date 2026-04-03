"""
meeting_domain - 本地 AI 会议纪要 API。

核心能力：
1) WebSocket 实时录音转写（流式返回转写）
2) 基于流式转写生成纪要（摘要/待办）
3) 三个核心部分（流式转写、摘要、待办）的增删改查

接口约定：
- 本地 AI 纪要不区分“流式转写”和“精确转写”；
- 流式转写文本存储在 LocalAsrSession.transcript_text。

阅读提示：
1. 这个文件只负责 API 编排，不在这里写 LLM 或 ASR 细节。
2. 真正的状态迁移、落库和第三方调用都在 `meeting_minute_local_service.py`。
3. 若要排查“为什么前端看不到纪要”，优先看 generate / get / sessions 三条链路是否连贯。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, WebSocket
from sqlalchemy.orm import Session

from app.models.meeting_domain import database, schemas
from app.models.schemas import StandardResponse
from app.services.meeting_domain.meeting_minute_local_service import (
    LiveLocalAsrHandler,
    local_meeting_minute_service,
)
from app.utils.auth import decode_access_token, get_current_user

router = APIRouter(prefix="/api/meetings/minutes/local", tags=["meeting_domain_local_minutes"])


@router.websocket("/{meeting_id}/live")
async def live_recording(websocket: WebSocket, meeting_id: int, token: str = Query(...)):
    try:
        decode_access_token(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    from app.models.meeting_domain.database import SessionLocal

    db = SessionLocal()
    try:
        exists = db.query(database.Meeting.id).filter(database.Meeting.id == meeting_id).first()
        if not exists:
            raise ValueError("会议不存在")
        handler = LiveLocalAsrHandler(websocket, db, meeting_id, local_meeting_minute_service)
        await handler.run()
    except ValueError as exc:
        await websocket.close(code=4004, reason=str(exc))
    finally:
        db.close()


@router.post(
    "/{meeting_id}/generate",
    response_model=StandardResponse[schemas.LocalMeetingMinutesResponse],
)
def generate_minutes(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = local_meeting_minute_service.generate_minutes(db, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return StandardResponse(success=True, data=data, message="会议纪要生成成功")


@router.get(
    "/{meeting_id}",
    response_model=StandardResponse[schemas.LocalMeetingMinutesResponse],
)
def get_minutes(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        data = local_meeting_minute_service.get_minutes(db, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StandardResponse(success=True, data=data, message="获取会议纪要成功")


@router.put(
    "/{meeting_id}/stream-transcript",
    response_model=StandardResponse[None],
)
def update_stream_transcript(
    meeting_id: int,
    payload: schemas.LocalStreamTranscriptUpdate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        local_meeting_minute_service.update_stream_transcript(
            db, meeting_id, payload.stream_transcript_text
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(success=True, data=None, message="流式转写已更新")


@router.put(
    "/{meeting_id}/summary",
    response_model=StandardResponse[schemas.LocalMeetingSummaryInDB],
)
def upsert_summary(
    meeting_id: int,
    payload: schemas.LocalMeetingSummaryCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        summary = local_meeting_minute_service.upsert_summary(db, meeting_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(
        success=True,
        data=schemas.LocalMeetingSummaryInDB.model_validate(summary),
        message="会议摘要已更新",
    )


@router.post(
    "/{meeting_id}/todos",
    response_model=StandardResponse[schemas.LocalMeetingTodoInDB],
)
def create_todo(
    meeting_id: int,
    payload: schemas.LocalMeetingTodoCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        todo = local_meeting_minute_service.create_todo(db, meeting_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(
        success=True,
        data=schemas.LocalMeetingTodoInDB.model_validate(todo),
        message="待办事项已新增",
    )


@router.put(
    "/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[schemas.LocalMeetingTodoInDB],
)
def update_todo(
    meeting_id: int,
    todo_id: int,
    payload: schemas.LocalMeetingTodoCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    todo = local_meeting_minute_service.update_todo(db, meeting_id, todo_id, payload)
    if not todo:
        raise HTTPException(status_code=404, detail="待办事项不存在")
    return StandardResponse(
        success=True,
        data=schemas.LocalMeetingTodoInDB.model_validate(todo),
        message="待办事项已更新",
    )


@router.delete(
    "/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[None],
)
def delete_todo(
    meeting_id: int,
    todo_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    deleted = local_meeting_minute_service.delete_todo(db, meeting_id, todo_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="待办事项不存在")
    return StandardResponse(success=True, data=None, message="待办事项已删除")
