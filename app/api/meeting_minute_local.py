"""本地 AI 会议纪要：实时转写 WebSocket、一键生成纪要、当前聚合视图与历史快照 CRUD。

WebSocket：Qwen 实时 WS 或 QWEN_ASR_LIVE_FORCE_HTTP_CHUNK 下 HTTP 滑窗分段转写；均落 WAV 并写入 meeting_audios。
POST transcribe-audio：对已上传本地音频排队异步分段转写，轮询 GET /{meeting_id} 查看 stream_transcript_text 与 audio_status。
POST generate 基于最新可用转写稿调 LLM 写摘要/待办并落历史快照。
GET 当前视图只读；sessions 为历史快照，可编辑并回写当前摘要/待办/ASR 文本。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, WebSocket
from sqlalchemy.orm import Session

from app.models import database, schemas
from app.models.database import SessionLocal
from app.models.schemas import StandardResponse
from app.services.meeting_minute_local_service import (
    LiveLocalAsrHandler,
    ProcessingCancelledError,
    local_meeting_minute_service,
)
from app.utils.auth import decode_access_token, get_current_user
from app.utils.logger import get_logger

logger = get_logger("meeting_minute_local_api")

router = APIRouter(prefix="/api/meetings/minutes/local", tags=["meeting_local_minutes"])


def _http_from_local_minutes_value_error(exc: ValueError) -> HTTPException:
    detail = str(exc)
    if detail == "会议不存在":
        return HTTPException(status_code=404, detail=detail)
    if detail == "会话历史不存在":
        return HTTPException(status_code=404, detail=detail)
    return HTTPException(status_code=400, detail=detail)


# 实时录音并流式转写，结束后上传本会话对应音频文件。
# 1. Query 参数 token 换 JWT，失败关连接。
# 2. 独立 SessionLocal 与 LiveLocalAsrHandler：建 local_asr_sessions、连 Qwen WS、双向转发。
# 3. 收尾写转写全文、时长，create_audio_from_path 填 source_audio_id。
# 4. 业务错误以 WS close code/reason 或 JSON error 告知。
@router.websocket("/{meeting_id}/live")
async def live_recording(websocket: WebSocket, meeting_id: int, token: str = Query(...)):
    logger.info("本地实时纪要 WS 连接尝试 meeting_id=%s client=%s", meeting_id, websocket.client)
    try:
        payload = decode_access_token(token)
    except HTTPException:
        logger.warning("本地实时纪要 WS 鉴权失败 meeting_id=%s client=%s", meeting_id, websocket.client)
        await websocket.close(code=4001)
        return
    creator_id = payload.get("sub")

    db = SessionLocal()
    try:
        handler = LiveLocalAsrHandler(
            websocket,
            db,
            meeting_id,
            local_meeting_minute_service,
            creator_id=creator_id,
        )
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


# 根据当前会议最新一条本地 ASR 流式稿调用 LLM 生成摘要与待办，并写入历史快照。
# 1. 校验存在非空 stream_transcript_text。
# 2. 清空并重写 local_meeting_summaries、local_meeting_todos，绑定 source_audio_id。
# 3. 追加 local_meeting_minutes_sessions 后返回 LocalMeetingMinutesResponse。
@router.post(
    "/{meeting_id}/generate",
    response_model=StandardResponse[schemas.LocalMeetingMinutesResponse],
)
def generate_minutes(
    meeting_id: int,
    asr_session_id: int | None = Query(None, description="可选：指定用于生成纪要的 local_asr_sessions 主键"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("生成本地纪要请求 meeting_id=%s asr_session_id=%s", meeting_id, asr_session_id)
    try:
        data = local_meeting_minute_service.generate_minutes(db, meeting_id, asr_session_id=asr_session_id)
    except ValueError as exc:
        logger.warning(
            "生成本地纪要失败 meeting_id=%s asr_session_id=%s error=%s",
            meeting_id,
            asr_session_id,
            exc,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProcessingCancelledError as exc:
        logger.info(
            "本地纪要生成已取消 meeting_id=%s asr_session_id=%s error=%s",
            meeting_id,
            asr_session_id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning(
            "生成本地纪要失败：下游依赖异常 meeting_id=%s asr_session_id=%s error=%s",
            meeting_id,
            asr_session_id,
            exc,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info(
        "生成本地纪要成功 meeting_id=%s asr_session_id=%s todo_count=%s",
        meeting_id,
        asr_session_id,
        len(data.todos),
    )
    return StandardResponse(success=True, data=data, message="会议纪要生成成功")


# 读取当前会议的「展示用」纪要聚合（转写稿、摘要、待办、状态）。
# 1. 会议不存在则 404。
# 2. 聚合最新 local_asr_sessions 文本、local_meeting_summaries、todos、asr_session_id。
# 3. audio_status：有最新本地音频用其 status，否则用 ASR 会话 status；无内容仍 200。
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


# 对已上传的本地会议音频（meeting_audios.provider=local）提交分段 HTTP 转写，异步写入新的 local_asr_sessions 行。
# 成功后轮询 GET /{meeting_id}：stream_transcript_text、asr_session_id、audio_status；失败时该行 status=failed 且 error_msg 有说明。
@router.post(
    "/{meeting_id}/transcribe-audio",
    response_model=StandardResponse[schemas.LocalAsrTranscribeFromAudioResponse],
)
def transcribe_uploaded_local_audio(
    meeting_id: int,
    audio_id: int = Query(..., description="meeting_audios 主键，须为本会议下 provider=local 的记录"),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    logger.info("提交本地已上传音频分段转写 meeting_id=%s audio_id=%s", meeting_id, audio_id)
    try:
        data = local_meeting_minute_service.queue_transcribe_uploaded_local_audio(
            db, meeting_id, audio_id
        )
    except ValueError as exc:
        logger.warning("提交本地音频转写失败 meeting_id=%s audio_id=%s error=%s", meeting_id, audio_id, exc)
        raise _http_from_local_minutes_value_error(exc) from exc
    except RuntimeError as exc:
        logger.warning(
            "提交本地音频转写失败(配置) meeting_id=%s audio_id=%s error=%s",
            meeting_id,
            audio_id,
            exc,
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    logger.info(
        "已排队本地音频分段转写 meeting_id=%s audio_id=%s asr_session_id=%s",
        meeting_id,
        audio_id,
        data.asr_session_id,
    )
    return StandardResponse(
        success=True,
        data=data,
        message="已提交异步分段转写，请轮询 GET 当前纪要查看转写结果",
    )


@router.post(
    "/{meeting_id}/cancel",
    response_model=StandardResponse[schemas.LocalProcessingCancelResponse],
)
def cancel_local_processing(
    meeting_id: int,
    payload: schemas.LocalProcessingCancelRequest | None = Body(None),
    db: Session = Depends(database.get_db),
    current_user: dict = Depends(get_current_user),
):
    payload = payload or schemas.LocalProcessingCancelRequest()
    logger.info(
        "取消本地处理请求 meeting_id=%s asr_session_id=%s",
        meeting_id,
        payload.asr_session_id,
    )
    try:
        data = local_meeting_minute_service.cancel_processing(
            db,
            meeting_id,
            asr_session_id=payload.asr_session_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        logger.warning(
            "取消本地处理失败 meeting_id=%s asr_session_id=%s error=%s",
            meeting_id,
            payload.asr_session_id,
            exc,
        )
        raise _http_from_local_minutes_value_error(exc) from exc
    logger.info(
        "取消本地处理请求已接受 meeting_id=%s asr_session_id=%s stage=%s",
        meeting_id,
        data.asr_session_id,
        data.stage,
    )
    return StandardResponse(success=True, data=data, message="已提交取消请求")


# 列出本会下全部本地纪要历史快照（某次生成后的整包版本，非实时 WS）。
# 1. 校验会议存在。
# 2. 按 meeting_id 查 local_meeting_minutes_sessions，时间正序。
# 3. 解析 todos_json 等为 LocalMeetingMinutesSessionInDB 列表。
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
        raise _http_from_local_minutes_value_error(exc) from exc
    logger.info("获取本地纪要历史列表成功 meeting_id=%s count=%s", meeting_id, len(data))
    return StandardResponse(success=True, data=data, message="获取会话历史成功")


# 获取单条本地纪要历史快照详情。
# 1. 校验会议存在。
# 2. session_id 须属于该 meeting_id。
# 3. 组装转写/摘要/待办快照；不存在则 ValueError→404。
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
        raise _http_from_local_minutes_value_error(exc) from exc
    logger.info("获取本地纪要历史详情成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=data, message="获取会话历史详情成功")


# 更新历史快照内容；若该条为会议下最新快照则同步回写「当前」摘要/待办/ASR 稿。
# 1. 定位 local_meeting_minutes_sessions 行。
# 2. 按 payload 更新 stream/transcript 别名、summary、todos_json。
# 3. _is_latest 时调用 _apply_latest_session_to_current_minutes。
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
        raise _http_from_local_minutes_value_error(exc) from exc
    logger.info("更新本地纪要历史会话成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=data, message="会话历史已更新")


# 删除一条本地纪要历史快照（不删音频文件、不删当前摘要表除非业务侧另有逻辑）。
# 1. 校验会议存在。
# 2. 确认 session 属于该 meeting_id。
# 3. delete 行并 commit。
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
        local_meeting_minute_service.delete_minutes_session(db, meeting_id, session_id)
    except ValueError as exc:
        logger.warning(
            "删除本地纪要历史会话失败 meeting_id=%s session_id=%s error=%s",
            meeting_id,
            session_id,
            exc,
        )
        raise _http_from_local_minutes_value_error(exc) from exc
    logger.info("删除本地纪要历史会话成功 meeting_id=%s session_id=%s", meeting_id, session_id)
    return StandardResponse(success=True, data=None, message="会话历史已删除")
