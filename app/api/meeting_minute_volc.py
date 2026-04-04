"""火山会议纪要 API。

该控制器负责火山链路的 HTTP 与 WebSocket 入口，包括：
1. 实时录音流式转写。
2. 离线妙记任务提交。
3. 当前纪要视图只提供读取，不提供直接增删改。

运维排障建议：
1. 实时录音阶段先看 live WebSocket 日志。
2. 妙记阶段重点看 submit 接口与后台轮询日志。
3. 人工修订统一通过 session 控制器处理。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
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
        payload = decode_access_token(token)
    except HTTPException:
        logger.warning("火山实时纪要 WS 鉴权失败 meeting_id=%s client=%s", meeting_id, websocket.client)
        await websocket.close(code=4001)
        return
    creator_id = payload.get("sub")

    db = SessionLocal()
    try:
        handler = LiveVolcAsrHandler(
            websocket,
            db,
            meeting_id,
            volc_meeting_minute_service,
            creator_id=creator_id,
        )
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
# 3. 返回已提交的离线任务记录，后续由后台轮询更新状态。
@router.post(
    "/{meeting_id}/submit",
    response_model=StandardResponse[schemas.VolcMinutesJobInDB],
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
        record.volc_task_id,
    )
    return StandardResponse(
        success=True,
        data=schemas.VolcMinutesJobInDB.model_validate(record),
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
