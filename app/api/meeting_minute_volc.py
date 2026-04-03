"""火山会议纪要 API。

该控制器负责火山链路的 HTTP 与 WebSocket 入口，包括：
1. 实时录音流式转写。
2. 离线妙记任务提交。
3. 当前纪要视图中的流式转写、精准转写、摘要、待办维护。

运维排障建议：
1. 实时录音阶段先看 live WebSocket 日志。
2. 妙记阶段重点看 submit 接口与后台轮询日志。
3. 人工修订阶段重点看 transcript / summary / todo 接口日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, WebSocket
from sqlalchemy.orm import Session

from app.models import database, schemas
from app.models.database import SessionLocal
from app.models.schemas import StandardResponse
from app.services.meeting_minute_volc_service import (
    LiveVolcAsrHandler,
    volc_meeting_minute_service,
)
from app.utils.auth import decode_access_token, get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_minute_volc_api")

router = APIRouter(prefix="/api/meetings/minutes/volc", tags=["meeting_volc_minutes"])


# 处理流程（火山实时录音 WebSocket）：
# 1. 校验 access token。
# 2. 创建独立数据库会话。
# 3. 交给 LiveVolcAsrHandler 执行“接收音频 -> 实时转写 -> 音频上传”。
# 4. 失败时关闭 WebSocket 并返回明确 reason。
@router.websocket("/{meeting_id}/live")
async def live_recording(websocket: WebSocket, meeting_id: int, token: str = Query(...)):
    logger.info("火山实时纪要 WS 连接尝试 meeting_id=%s client=%s", meeting_id, websocket.client)
    try:
        decode_access_token(token)
    except HTTPException:
        logger.warning("火山实时纪要 WS 鉴权失败 meeting_id=%s client=%s", meeting_id, websocket.client)
        await websocket.close(code=4001)
        return

    db = SessionLocal()
    try:
        handler = LiveVolcAsrHandler(websocket, db, meeting_id, volc_meeting_minute_service)
        logger.info("火山实时纪要 WS 开始处理 meeting_id=%s", meeting_id)
        await handler.run()
        logger.info("火山实时纪要 WS 处理完成 meeting_id=%s", meeting_id)
    except ValueError as exc:
        logger.warning("火山实时纪要 WS 业务失败 meeting_id=%s error=%s", meeting_id, exc)
        await websocket.close(code=4004, reason=str(exc))
    except Exception:
        logger.exception("火山实时纪要 WS 异常 meeting_id=%s", meeting_id)
        raise
    finally:
        db.close()
        logger.info("火山实时纪要 WS 数据库会话已关闭 meeting_id=%s", meeting_id)


# 处理流程（提交妙记任务）：
# 1. 校验 meeting/audio 归属关系。
# 2. 提交火山语音妙记离线任务。
# 3. 返回已提交的音频记录，后续由后台轮询更新状态。
@router.post(
    "/{meeting_id}/submit",
    response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB],
)
def submit_minutes(
    meeting_id: int,
    audio_id: int = Query(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("提交火山妙记请求 meeting_id=%s audio_id=%s", meeting_id, audio_id)
    try:
        record = volc_meeting_minute_service.submit_minutes(
            db=db,
            meeting_id=meeting_id,
            audio_id=audio_id,
        )
    except ValueError as exc:
        logger.warning("提交火山妙记失败 meeting_id=%s audio_id=%s error=%s", meeting_id, audio_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "提交火山妙记成功 meeting_id=%s audio_id=%s task_id=%s",
        meeting_id,
        audio_id,
        record.task_id,
    )
    return StandardResponse(
        success=True,
        data=schemas.MeetingAudioUnifiedInDB.model_validate(record),
        message="已提交语音妙记，后台处理中",
    )


# 处理流程（当前火山纪要视图）：
# 1. 聚合读取实时转写、精准转写、摘要、待办。
# 2. 不存在时返回 404。
# 3. 返回当前会议的纪要聚合结果。
@router.get(
    "/{meeting_id}",
    response_model=StandardResponse[schemas.VolcMeetingMinutesResponse],
)
def get_minutes(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取火山纪要请求 meeting_id=%s", meeting_id)
    try:
        data = volc_meeting_minute_service.get_minutes(db, meeting_id)
    except ValueError as exc:
        logger.warning("获取火山纪要失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("获取火山纪要成功 meeting_id=%s todo_count=%s", meeting_id, len(data.todos))
    return StandardResponse(success=True, data=data, message="获取会议纪要成功")


# 处理流程（更新流式转写）：
# 1. 定位当前最新流式 ASR 会话。
# 2. 覆盖实时转写文本。
# 3. 同步更新当前音频上的转写缓存。
@router.put(
    "/{meeting_id}/stream-transcript",
    response_model=StandardResponse[None],
)
def update_stream_transcript(
    meeting_id: int,
    payload: schemas.VolcTranscriptUpdate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(
        "更新火山流式转写请求 meeting_id=%s text_length=%s",
        meeting_id,
        len(payload.transcript_text or ""),
    )
    try:
        volc_meeting_minute_service.update_stream_transcript(db, meeting_id, payload.transcript_text)
    except ValueError as exc:
        logger.warning("更新火山流式转写失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("更新火山流式转写成功 meeting_id=%s", meeting_id)
    return StandardResponse(success=True, data=None, message="流式转写已更新")


# 处理流程（更新精准转写）：
# 1. 定位当前最新火山音频。
# 2. 覆盖精准转写并清理旧分段。
# 3. 返回更新后的音频记录。
@router.put(
    "/{meeting_id}/transcript",
    response_model=StandardResponse[schemas.MeetingAudioUnifiedInDB],
)
def update_transcript(
    meeting_id: int,
    payload: schemas.VolcTranscriptUpdate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(
        "更新火山精准转写请求 meeting_id=%s text_length=%s",
        meeting_id,
        len(payload.transcript_text or ""),
    )
    try:
        audio = volc_meeting_minute_service.update_precise_transcript(db, meeting_id, payload.transcript_text)
    except ValueError as exc:
        logger.warning("更新火山精准转写失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("更新火山精准转写成功 meeting_id=%s audio_id=%s", meeting_id, audio.id)
    return StandardResponse(
        success=True,
        data=schemas.MeetingAudioUnifiedInDB.model_validate(audio),
        message="精确转写已更新",
    )


# 处理流程（更新摘要）：
# 1. 校验会议存在。
# 2. 对当前摘要执行 upsert。
# 3. 返回最新摘要实体。
@router.put(
    "/{meeting_id}/summary",
    response_model=StandardResponse[schemas.VolcMeetingSummaryInDB],
)
def upsert_summary(
    meeting_id: int,
    payload: schemas.VolcMeetingSummaryCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("更新火山纪要摘要请求 meeting_id=%s source_audio_id=%s", meeting_id, payload.source_audio_id)
    try:
        summary = volc_meeting_minute_service.upsert_summary(db, meeting_id, payload)
    except ValueError as exc:
        logger.warning("更新火山纪要摘要失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("更新火山纪要摘要成功 meeting_id=%s summary_id=%s", meeting_id, summary.id)
    return StandardResponse(
        success=True,
        data=schemas.VolcMeetingSummaryInDB.model_validate(summary),
        message="会议摘要已更新",
    )


# 处理流程（新增待办）：
# 1. 校验会议存在。
# 2. 写入单条待办事项。
# 3. 返回新增后的待办实体。
@router.post(
    "/{meeting_id}/todos",
    response_model=StandardResponse[schemas.VolcMeetingTodoInDB],
)
def create_todo(
    meeting_id: int,
    payload: schemas.VolcMeetingTodoCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("新增火山纪要待办请求 meeting_id=%s source_audio_id=%s", meeting_id, payload.source_audio_id)
    try:
        todo = volc_meeting_minute_service.create_todo(db, meeting_id, payload)
    except ValueError as exc:
        logger.warning("新增火山纪要待办失败 meeting_id=%s error=%s", meeting_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("新增火山纪要待办成功 meeting_id=%s todo_id=%s", meeting_id, todo.id)
    return StandardResponse(
        success=True,
        data=schemas.VolcMeetingTodoInDB.model_validate(todo),
        message="待办事项已新增",
    )


# 处理流程（更新待办）：
# 1. 按 meeting_id + todo_id 定位待办记录。
# 2. 覆盖待办内容、执行人和执行时间。
# 3. 返回更新后的待办实体。
@router.put(
    "/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[schemas.VolcMeetingTodoInDB],
)
def update_todo(
    meeting_id: int,
    todo_id: int,
    payload: schemas.VolcMeetingTodoCreate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("更新火山纪要待办请求 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    todo = volc_meeting_minute_service.update_todo(db, meeting_id, todo_id, payload)
    if not todo:
        logger.warning("更新火山纪要待办失败：待办不存在 meeting_id=%s todo_id=%s", meeting_id, todo_id)
        raise HTTPException(status_code=404, detail="待办事项不存在")
    logger.info("更新火山纪要待办成功 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    return StandardResponse(
        success=True,
        data=schemas.VolcMeetingTodoInDB.model_validate(todo),
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
    logger.info("删除火山纪要待办请求 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    deleted = volc_meeting_minute_service.delete_todo(db, meeting_id, todo_id)
    if not deleted:
        logger.warning("删除火山纪要待办失败：待办不存在 meeting_id=%s todo_id=%s", meeting_id, todo_id)
        raise HTTPException(status_code=404, detail="待办事项不存在")
    logger.info("删除火山纪要待办成功 meeting_id=%s todo_id=%s", meeting_id, todo_id)
    return StandardResponse(success=True, data=None, message="待办事项已删除")
