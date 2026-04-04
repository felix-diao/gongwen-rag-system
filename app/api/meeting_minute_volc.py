"""火山会议纪要：实时转写 WebSocket、提交妙记离线任务、当前聚合视图与历史快照 CRUD。

WebSocket 走火山实时 ASR 并上传音频；submit 创建 volc_minutes_jobs 由后台轮询妙记结果，驱动精准转写与摘要待办。
GET 聚合流式稿、精准转写、说话人分段、摘要、待办与任务状态；sessions 为妙记成功等节点落地的历史快照，可编辑并回写当前视图。
排障：live、submit/轮询、PUT sessions 三类日志分区查看。
"""

from __future__ import annotations

from typing import List

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


def _http_from_volc_minutes_value_error(exc: ValueError) -> HTTPException:
    detail = str(exc)
    if detail == "会议不存在":
        return HTTPException(status_code=404, detail=detail)
    if detail == "会话历史不存在":
        return HTTPException(status_code=404, detail=detail)
    return HTTPException(status_code=400, detail=detail)


# 实时录音并流式转写（火山 ASR），结束后上传本会话对应音频。
# 1. Query token 鉴权，失败关连接。
# 2. SessionLocal + LiveVolcAsrHandler：建 volc_asr_sessions、连火山实时接口、转发 PCM。
# 3. 收尾更新转写与时长，上传音频并回填 source_audio_id。
# 4. 异常时更新会话 failed 并向前端报错。
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


# 为指定会议音频提交火山语音妙记离线转写任务。
# 1. 校验 meeting_id 与 audio_id（volc provider）归属。
# 2. 固化 input_file_url/type，调用妙记 submit，写 volc_minutes_jobs。
# 3. 返回 VolcMinutesJobInDB；后续状态由后台轮询更新。
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
        raise _http_from_volc_minutes_value_error(exc) from exc

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


# 聚合读取火山链路下当前会议的转写、分段、摘要、待办与妙记任务态。
# 1. 会议不存在则 404。
# 2. service 拼装 VolcMeetingMinutesResponse（流式稿、精准稿、segments、summary、todos 等）。
# 3. minutes_job_status / audio_status 等字段见 schema 说明；无数据仍可 200。
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
        raise _http_from_volc_minutes_value_error(exc) from exc
    logger.info("获取火山纪要成功 meeting_id=%s todo_count=%s", meeting_id, len(data.todos))
    return StandardResponse(success=True, data=data, message="获取会议纪要成功")


# 列出本会下全部火山纪要历史快照（妙记等流程落地后的整包版本）。
# 1. 校验会议存在。
# 2. 查询 volc_meeting_minutes_sessions。
# 3. 返回 VolcMeetingMinutesSessionInDB 列表。
@router.get(
    "/{meeting_id}/sessions",
    response_model=StandardResponse[List[schemas.VolcMeetingMinutesSessionInDB]],
)
def list_minutes_sessions(
    meeting_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取火山纪要历史列表请求 meeting_id=%s", meeting_id)
    try:
        data = volc_meeting_minute_service.list_minutes_sessions(db, meeting_id)
    except ValueError as exc:
        logger.warning("获取火山纪要历史列表失败 meeting_id=%s error=%s", meeting_id, exc)
        raise _http_from_volc_minutes_value_error(exc) from exc
    logger.info("获取火山纪要历史列表成功 meeting_id=%s count=%s", meeting_id, len(data))
    return StandardResponse(success=True, data=data, message="获取会话历史成功")


# 获取单条火山纪要历史快照（含说话人分段、摘要、待办 JSON 等）。
# 1. 校验会议存在。
# 2. service 按 meeting_id + session_id 取行并解析 JSON 列。
# 3. 会话不存在或 JSON 非法时由 ValueError 映射为 404/400。
@router.get(
    "/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[schemas.VolcMeetingMinutesSessionInDB],
)
def get_minutes_session(
    meeting_id: int,
    session_id: int,
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("获取火山纪要历史详情请求 meeting_id=%s session_id=%s", meeting_id, session_id)
    try:
        data = volc_meeting_minute_service.get_minutes_session(db, meeting_id, session_id)
    except ValueError as exc:
        logger.warning(
            "获取火山纪要历史详情失败 meeting_id=%s session_id=%s error=%s",
            meeting_id,
            session_id,
            exc,
        )
        raise _http_from_volc_minutes_value_error(exc) from exc
    logger.info("获取火山纪要历史详情成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=data, message="获取会话历史详情成功")


# 更新火山历史快照；若为最新快照则回写当前 volc 摘要/待办/转写等视图表。
# 1. 加载 volc_meeting_minutes_sessions。
# 2. 按 payload 更新各快照列（含 speaker_segments 等）。
# 3. 最新快照时 service 同步当前业务表。
@router.put(
    "/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[schemas.VolcMeetingMinutesSessionInDB],
)
def update_minutes_session(
    meeting_id: int,
    session_id: int,
    payload: schemas.VolcMeetingMinutesSessionUpdate = Body(...),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info(
        "更新火山纪要历史会话请求 meeting_id=%s session_id=%s fields=%s",
        meeting_id,
        session_id,
        sorted(payload.model_fields_set),
    )
    try:
        data = volc_meeting_minute_service.update_minutes_session(db, meeting_id, session_id, payload)
    except ValueError as exc:
        logger.warning(
            "更新火山纪要历史会话失败 meeting_id=%s session_id=%s error=%s",
            meeting_id,
            session_id,
            exc,
        )
        raise _http_from_volc_minutes_value_error(exc) from exc
    logger.info("更新火山纪要历史会话成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=data, message="会话历史已更新")


# 删除一条火山纪要历史快照。
# 1. 校验会议存在。
# 2. service 按 meeting_id + session_id 定位并删除行。
# 3. 会话不存在时 ValueError→404。
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
    logger.info("删除火山纪要历史会话请求 meeting_id=%s session_id=%s", meeting_id, session_id)
    try:
        volc_meeting_minute_service.delete_minutes_session(db, meeting_id, session_id)
    except ValueError as exc:
        logger.warning(
            "删除火山纪要历史会话失败 meeting_id=%s session_id=%s error=%s",
            meeting_id,
            session_id,
            exc,
        )
        raise _http_from_volc_minutes_value_error(exc) from exc
    logger.info("删除火山纪要历史会话成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=None, message="会话历史已删除")
