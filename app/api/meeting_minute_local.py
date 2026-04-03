"""本地 AI 会议纪要 API。

该控制器负责本地实时纪要链路的 HTTP 与 WebSocket 入口，包括：
1. 实时录音转写。
2. 基于流式转写生成纪要。
3. 当前纪要视图的摘要、待办、流式文本维护。

运维排障建议：
1. 实时录音问题优先看 live WebSocket 的连接和关闭日志。
2. 纪要生成问题优先看 generate 接口和 service 中的 LLM 日志。
3. 人工修订问题优先看 summary / todo / stream-transcript 三类接口日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, WebSocket
from sqlalchemy.orm import Session

from app.models import database, schemas
from app.models.database import SessionLocal
from app.models.schemas import StandardResponse
from app.services.meeting_minute_local_service import (
    LiveLocalAsrHandler,
    local_meeting_minute_service,
)
from app.utils.auth import decode_access_token, get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_minute_local_api")

router = APIRouter(prefix="/api/meetings/minutes/local", tags=["meeting_local_minutes"])


# 处理流程（本地实时录音 WebSocket）：
# 1. 校验 access token。
# 2. 创建独立数据库会话。
# 3. 交给 LiveLocalAsrHandler 执行“接收音频 -> 实时转写 -> 音频上传”全链路。
# 4. 失败时关闭 WebSocket 并返回明确 reason。
@router.websocket("/{meeting_id}/live")
async def live_recording(websocket: WebSocket, meeting_id: int, token: str = Query(...)):
    logger.info("本地实时纪要 WS 连接尝试 meeting_id=%s client=%s", meeting_id, websocket.client)
    try:
        decode_access_token(token)
    except HTTPException:
        logger.warning("本地实时纪要 WS 鉴权失败 meeting_id=%s client=%s", meeting_id, websocket.client)
        await websocket.close(code=4001)
        return

    db = SessionLocal()
    try:
        handler = LiveLocalAsrHandler(websocket, db, meeting_id, local_meeting_minute_service)
        logger.info("本地实时纪要 WS 开始处理 meeting_id=%s", meeting_id)
        await handler.run()
        logger.info("本地实时纪要 WS 处理完成 meeting_id=%s", meeting_id)
    except ValueError as exc:
        logger.warning("本地实时纪要 WS 业务失败 meeting_id=%s error=%s", meeting_id, exc)
        await websocket.close(code=4004, reason=str(exc))
    except Exception:
        logger.exception("本地实时纪要 WS 异常 meeting_id=%s", meeting_id)
        raise
    finally:
        db.close()
        logger.info("本地实时纪要 WS 数据库会话已关闭 meeting_id=%s", meeting_id)


# 处理流程（生成本地纪要）：
# 1. 读取最新流式转写文本。
# 2. 调用 service 生成当前纪要视图与历史快照。
# 3. 返回摘要、待办和转写结果。
@router.post(
    "/{meeting_id}/generate",
    response_model=StandardResponse[schemas.LocalMeetingMinutesResponse],
)
def generate_minutes(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("生成本地纪要请求 meeting_id=%s", meeting_id)
    try:
        data = local_meeting_minute_service.generate_minutes(db, meeting_id)
    except ValueError as exc:
        logger.warning("生成本地纪要失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("生成本地纪要失败：下游依赖异常 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("生成本地纪要成功 meeting_id=%s todo_count=%s", meeting_id, len(data.todos))
    return StandardResponse(success=True, data=data, message="会议纪要生成成功")


# 处理流程（当前本地纪要视图）：
# 1. 读取当前会议的最新纪要聚合结果。
# 2. 不存在时返回 404。
# 3. 返回当前摘要、待办和转写内容。
@router.get(
    "/{meeting_id}",
    response_model=StandardResponse[schemas.LocalMeetingMinutesResponse],
)
def get_minutes(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取本地纪要请求 meeting_id=%s", meeting_id)
    try:
        data = local_meeting_minute_service.get_minutes(db, meeting_id)
    except ValueError as exc:
        logger.warning("获取本地纪要失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("获取本地纪要成功 meeting_id=%s", meeting_id)
    return StandardResponse(success=True, data=data, message="获取会议纪要成功")


# 处理流程（更新流式转写）：
# 1. 定位当前会议最新流式 ASR 会话。
# 2. 覆盖 stream_transcript_text。
# 3. 同步更新当前音频上的转写缓存。
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
    logger.info(
        "更新本地流式转写请求 meeting_id=%s text_length=%s",
        meeting_id,
        len(payload.stream_transcript_text or ""),
    )
    try:
        local_meeting_minute_service.update_stream_transcript(
            db, meeting_id, payload.stream_transcript_text
        )
    except ValueError as exc:
        logger.warning("更新本地流式转写失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("更新本地流式转写成功 meeting_id=%s", meeting_id)
    return StandardResponse(success=True, data=None, message="流式转写已更新")


# 处理流程（更新当前摘要）：
# 1. 校验会议存在。
# 2. 对当前摘要执行 upsert。
# 3. 返回最新摘要实体。
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
    logger.info("更新本地纪要摘要请求 meeting_id=%s source_audio_id=%s", meeting_id, payload.source_audio_id)
    try:
        summary = local_meeting_minute_service.upsert_summary(db, meeting_id, payload)
    except ValueError as exc:
        logger.warning("更新本地纪要摘要失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("更新本地纪要摘要成功 meeting_id=%s summary_id=%s", meeting_id, summary.id)
    return StandardResponse(
        success=True,
        data=schemas.LocalMeetingSummaryInDB.model_validate(summary),
        message="会议摘要已更新",
    )


# 处理流程（新增待办）：
# 1. 校验会议存在。
# 2. 写入单条待办事项。
# 3. 返回新增后的待办实体。
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
    logger.info("新增本地纪要待办请求 meeting_id=%s source_audio_id=%s", meeting_id, payload.source_audio_id)
    try:
        todo = local_meeting_minute_service.create_todo(db, meeting_id, payload)
    except ValueError as exc:
        logger.warning("新增本地纪要待办失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("新增本地纪要待办成功 meeting_id=%s todo_id=%s", meeting_id, todo.id)
    return StandardResponse(
        success=True,
        data=schemas.LocalMeetingTodoInDB.model_validate(todo),
        message="待办事项已新增",
    )


# 处理流程（更新待办）：
# 1. 按 meeting_id + todo_id 定位待办记录。
# 2. 覆盖待办内容、执行人和执行时间。
# 3. 返回更新后的待办实体。
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
    logger.info("更新本地纪要待办请求 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    todo = local_meeting_minute_service.update_todo(db, meeting_id, todo_id, payload)
    if not todo:
        logger.warning("更新本地纪要待办失败：待办不存在 meeting_id=%s todo_id=%s", meeting_id, todo_id)
        raise HTTPException(status_code=404, detail="待办事项不存在")
    logger.info("更新本地纪要待办成功 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    return StandardResponse(
        success=True,
        data=schemas.LocalMeetingTodoInDB.model_validate(todo),
        message="待办事项已更新",
    )


# 处理流程（删除待办）：
# 1. 按 meeting_id + todo_id 定位待办记录。
# 2. 删除记录。
# 3. 返回空响应表示删除成功。
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
    logger.info("删除本地纪要待办请求 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    deleted = local_meeting_minute_service.delete_todo(db, meeting_id, todo_id)
    if not deleted:
        logger.warning("删除本地纪要待办失败：待办不存在 meeting_id=%s todo_id=%s", meeting_id, todo_id)
        raise HTTPException(status_code=404, detail="待办事项不存在")
    logger.info("删除本地纪要待办成功 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    return StandardResponse(success=True, data=None, message="待办事项已删除")
