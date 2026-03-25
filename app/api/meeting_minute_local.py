"""
本地会议纪要路由（Qwen3-ASR + LLM）

工作流A — 实时录音（两个按钮顺序触发）
  按钮1  WS   /api/minutes/local/{meeting_id}/live
              实时录音 + Qwen3-ASR 流式转写；录音结束后自动保存WAV并上传TOS
              completed 消息携带 audio_id，供前端存储后触发按钮2
  按钮2  POST /api/minutes/local/{meeting_id}/generate
              取最新转写文本，调用 LLM 生成摘要 + Todos

工作流B — 上传音频（三个按钮顺序触发）
  按钮1  POST /api/minutes/local/{meeting_id}/upload
              上传音频到TOS（bucket: meeting-record-local-temp），返回 audio_id
  按钮2  GET  /api/minutes/local/audio/{audio_id}/stream?token=<JWT>
              SSE 流式 Qwen3-ASR 转写，实时推字；完成后转写文本落库
  按钮3  POST /api/minutes/local/{meeting_id}/generate
              同工作流A按钮2，复用同一接口

查询 & 编辑
  GET    /api/minutes/local/{meeting_id}                      查询会议纪要（转写+摘要+Todos）
  PUT    /api/minutes/local/{meeting_id}/transcript           修改转写文本
  PUT    /api/minutes/local/{meeting_id}/summary              修改摘要
  POST   /api/minutes/local/{meeting_id}/todos                新增Todo
  PUT    /api/minutes/local/{meeting_id}/todos/{todo_id}      修改Todo
  DELETE /api/minutes/local/{meeting_id}/todos/{todo_id}      删除Todo
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import (
    APIRouter, Body, Depends, File, HTTPException,
    Query, UploadFile, WebSocket,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.models import schemas2
from app.models.database import LocalMeetingAudio, get_db
from app.models.schemas import StandardResponse
from app.services.local_asr_service import (
    LocalLiveAsrHandler,
    _ensure_wav_on_disk,
    diagnose_qwen_asr_connectivity,
    stream_local_file_asr,
)
from app.services.local_minutes_service import local_minutes_service
from app.services.meeting_service import MeetingService
from app.utils.auth import decode_access_token, get_current_user
from app.utils.logger import get_logger

router = APIRouter(prefix="/api/minutes", tags=["meeting_minutes_local"])
logger = get_logger("meeting_minute_local_api")
meeting_service = MeetingService()


def _get_meeting_or_404(db: Session, meeting_id: int):
    meeting = meeting_service.get_meeting(db, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="会议未找到")
    return meeting


def _get_latest_audio_or_404(db: Session, meeting_id: int) -> LocalMeetingAudio:
    audio = (
        db.query(LocalMeetingAudio)
        .filter(LocalMeetingAudio.meeting_id == meeting_id)
        .order_by(LocalMeetingAudio.created_at.desc())
        .first()
    )
    if not audio:
        raise HTTPException(
            status_code=400,
            detail="该会议尚无已上传的音频，请先完成录音或上传音频文件",
        )
    return audio


# ═══════════════════════════════════════════════════════════════════════════════
# 工作流A 按钮1 — 实时录音 WebSocket
# ═══════════════════════════════════════════════════════════════════════════════

@router.websocket("/local/{meeting_id}/live")
async def live_recording(
    websocket: WebSocket,
    meeting_id: int,
    token: str = Query(..., description="JWT access token"),
):
    """
    【工作流A 按钮1】实时录音 + Qwen3-ASR 流式转写。

    协议：
    - 连接 URL：ws://.../api/minutes/local/{meeting_id}/live?token=<JWT>
    - 客户端发送（二进制）：PCM 音频帧（16kHz, 16-bit, 单声道）
    - 客户端发送（JSON）：
        {"action": "stop"}
        {"action": "config", "rate": 16000, "channels": 1}
    - 服务端推送（JSON）：
        {"type": "session_created", "session_id": 123}
        {"type": "partial",   "text": "...", "accumulated": "..."}
        {"type": "final",     "text": "...", "accumulated": "..."}
        {"type": "saving_audio",   "session_id": 123}
        {"type": "uploading_audio", "session_id": 123}
        {"type": "completed", "session_id": 123, "audio_id": 456,
         "transcript": "...", "audio_uploaded": true, "duration_seconds": 60.0}
        {"type": "error",     "message": "..."}
    """
    try:
        decode_access_token(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    from app.models.database import SessionLocal
    db = SessionLocal()
    try:
        if not meeting_service.get_meeting(db, meeting_id):
            await websocket.close(code=4004)
            return
        handler = LocalLiveAsrHandler(websocket, meeting_id, db)
        await handler.run()
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 工作流B 按钮1 — 上传音频到 TOS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/local/{meeting_id}/upload",
    response_model=StandardResponse[schemas2.LocalMeetingAudioInDB],
)
def upload_audio(
    meeting_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    【工作流B 按钮1】上传音频文件到 TOS 对象存储（bucket: meeting-record-local-temp）。
    支持 WAV / MP3 / M4A / OGG 等格式。返回 audio_id。
    """
    _get_meeting_or_404(db, meeting_id)
    try:
        record = local_minutes_service.upload_audio_fileobj(
            db=db, meeting_id=meeting_id,
            upload_file=file,
            original_name=file.filename or "audio",
            content_type=file.content_type,
        )
    except Exception as exc:
        logger.exception("Upload audio failed meeting_id=%s: %s", meeting_id, exc)
        raise HTTPException(status_code=502, detail=f"上传至对象存储失败: {exc}") from exc

    return StandardResponse(
        success=True,
        data=schemas2.LocalMeetingAudioInDB.model_validate(record),
        message="音频已上传至对象存储，请调用流式转写接口进行转写",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 工作流B 按钮2 — SSE 流式 ASR 转写
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/local/audio/{audio_id}/stream")
async def stream_asr(
    audio_id: int,
    token: str = Query(..., description="JWT access token"),
    db: Session = Depends(get_db),
):
    """
    【工作流B 按钮2】对已上传 TOS 的音频进行 Qwen3-ASR 流式转写，以 SSE 实时推送。

    SSE 事件格式：
      data: {"type": "session_created", "session_id": 123, "audio_id": 456}
      data: {"type": "partial",   "text": "...", "accumulated": "..."}
      data: {"type": "final",     "text": "...", "accumulated": "..."}
      data: {"type": "completed", "session_id": 123, "transcript": "..."}
      data: {"type": "error",     "message": "..."}
    """
    try:
        decode_access_token(token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="无效的 token")

    audio = db.query(LocalMeetingAudio).filter(LocalMeetingAudio.id == audio_id).first()
    if not audio:
        raise HTTPException(status_code=404, detail="音频记录未找到")
    _get_meeting_or_404(db, audio.meeting_id)

    suffix = Path(audio.file_name or "audio").suffix or ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()

    try:
        local_minutes_service.download_audio(audio.object_key, Path(tmp_path))
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=502, detail=f"从对象存储下载音频失败: {exc}") from exc

    actual_path = _ensure_wav_on_disk(tmp_path)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            async for event in stream_local_file_asr(audio_id, audio.meeting_id, actual_path):
                yield event
        finally:
            for p in {actual_path, tmp_path}:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/local/audio/{audio_id}/ws")
async def stream_asr_ws(
    websocket: WebSocket,
    audio_id: int,
    token: str = Query(..., description="JWT access token"),
):
    """
    上传音频转写 WS 推流接口。
    - 服务端复用 stream_local_file_asr 的分段增量能力
    - 每条消息均为 JSON（不使用 SSE 包装），避免代理缓冲导致“最后一次性显示”
    """
    try:
        decode_access_token(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    from app.models.database import SessionLocal
    db = SessionLocal()
    tmp_path: Optional[str] = None
    actual_path: Optional[str] = None
    heartbeat_task: Optional[asyncio.Task] = None
    ws_alive = True
    try:
        await websocket.accept()
        audio = db.query(LocalMeetingAudio).filter(LocalMeetingAudio.id == audio_id).first()
        if not audio:
            await websocket.close(code=4004)
            return
        if not meeting_service.get_meeting(db, audio.meeting_id):
            await websocket.close(code=4004)
            return

        heartbeat_sec = max(1.0, float(settings.QWEN_ASR_FILE_HEARTBEAT_SEC or 2.0))

        async def _heartbeat() -> None:
            nonlocal ws_alive
            while ws_alive:
                await asyncio.sleep(heartbeat_sec)
                if not ws_alive:
                    break
                try:
                    await websocket.send_json({"type": "heartbeat", "message": "processing"})
                except Exception:
                    ws_alive = False
                    break

        heartbeat_task = asyncio.create_task(_heartbeat())
        await websocket.send_json({"type": "progress", "stage": "downloading_audio"})

        suffix = Path(audio.file_name or "audio").suffix or ".wav"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp_path = tmp.name
        tmp.close()

        try:
            local_minutes_service.download_audio(audio.object_key, Path(tmp_path))
        except Exception as exc:
            await websocket.send_json({"type": "error", "message": f"从对象存储下载音频失败: {exc}"})
            await websocket.close(code=1011)
            return

        await websocket.send_json({"type": "progress", "stage": "audio_downloaded"})
        await websocket.send_json({"type": "progress", "stage": "converting_audio"})
        actual_path = _ensure_wav_on_disk(tmp_path)
        await websocket.send_json({"type": "progress", "stage": "transcribing"})

        async for sse_event in stream_local_file_asr(audio_id, audio.meeting_id, actual_path):
            # sse_event: "data: {...}\n\n"
            payload = str(sse_event).strip()
            if not payload.startswith("data:"):
                continue
            raw_json = payload[5:].strip()
            if not raw_json:
                continue
            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            await websocket.send_json(data)
    except Exception as exc:
        try:
            if websocket.client_state.name == "CONNECTED":
                await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        ws_alive = False
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        for p in (actual_path, tmp_path):
            if not p:
                continue
            try:
                os.unlink(p)
            except OSError:
                pass
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 共享 — 生成会议纪要（LLM 摘要 + Todos）
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/local/{meeting_id}/generate",
    response_model=StandardResponse[schemas2.LocalMeetingMinutesResponse],
)
def generate_minutes(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    【工作流A 按钮2 / 工作流B 按钮3】从转写文本生成会议纪要。
    调用 LLM 生成结构化摘要 + 待办事项。
    """
    _get_meeting_or_404(db, meeting_id)
    try:
        summary, todos = local_minutes_service.generate_minutes_from_transcript(db, meeting_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    minutes = local_minutes_service.get_minutes(db, meeting_id)
    return StandardResponse(
        success=True,
        data=minutes,
        message="会议纪要生成成功",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 清空会议纪要
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/local/{meeting_id}/clear",
    response_model=StandardResponse[None],
)
def clear_minutes(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """清空指定会议的全部本地纪要内容（摘要、待办、转写文本）。"""
    _get_meeting_or_404(db, meeting_id)
    local_minutes_service.clear_minutes(db, meeting_id)
    return StandardResponse(success=True, data=None, message="已清空会议纪要")


@router.get(
    "/local/asr/health",
    response_model=StandardResponse[dict],
)
async def local_asr_health(
    timeout_seconds: int = Query(8, ge=2, le=30, description="连通性检测超时（秒）"),
    check_protocol: bool = Query(True, description="是否在握手后发送 session.update 检测协议事件"),
    current_user: dict = Depends(get_current_user),
):
    """
    本地 Qwen ASR 连通性诊断接口。
    返回握手状态码、脱敏后的请求头、首个事件类型，用于快速定位 403/超时/鉴权错误。
    """
    result = await diagnose_qwen_asr_connectivity(
        timeout_seconds=timeout_seconds,
        check_protocol=check_protocol,
    )
    message = "ASR 连通性正常" if result.get("ok") else "ASR 连通性异常"
    return StandardResponse(success=True, data=result, message=message)


# ═══════════════════════════════════════════════════════════════════════════════
# 查询 & 编辑
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/local/{meeting_id}",
    response_model=StandardResponse[schemas2.LocalMeetingMinutesResponse],
)
def get_minutes(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """查询会议纪要：转写文本 + 摘要 + Todos。"""
    _get_meeting_or_404(db, meeting_id)
    minutes = local_minutes_service.get_minutes(db, meeting_id)
    return StandardResponse(success=True, data=minutes, message="获取会议纪要成功")


@router.put(
    "/local/{meeting_id}/transcript",
    response_model=StandardResponse[schemas2.LocalMeetingAudioInDB],
)
def update_transcript(
    meeting_id: int,
    payload: schemas2.LocalTranscriptUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """修改会议转写文本。"""
    _get_meeting_or_404(db, meeting_id)
    audio = _get_latest_audio_or_404(db, meeting_id)
    audio.transcript_text = payload.transcript_text
    db.commit()
    db.refresh(audio)
    return StandardResponse(
        success=True,
        data=schemas2.LocalMeetingAudioInDB.model_validate(audio),
        message="转写文本已更新",
    )


@router.put(
    "/local/{meeting_id}/summary",
    response_model=StandardResponse[schemas2.LocalMeetingSummaryInDB],
)
def update_summary(
    meeting_id: int,
    payload: schemas2.LocalMeetingSummaryCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """修改会议摘要。"""
    _get_meeting_or_404(db, meeting_id)
    summary = local_minutes_service.update_summary(db, meeting_id, payload)
    return StandardResponse(
        success=True,
        data=schemas2.LocalMeetingSummaryInDB.model_validate(summary),
        message="摘要已更新",
    )


@router.post(
    "/local/{meeting_id}/todos",
    response_model=StandardResponse[schemas2.LocalMeetingTodoInDB],
)
def create_todo(
    meeting_id: int,
    payload: schemas2.LocalMeetingTodoCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """新增待办事项。"""
    _get_meeting_or_404(db, meeting_id)
    todo = local_minutes_service.create_todo(db, meeting_id, payload)
    return StandardResponse(
        success=True,
        data=schemas2.LocalMeetingTodoInDB.model_validate(todo),
        message="待办事项已新增",
    )


@router.put(
    "/local/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[schemas2.LocalMeetingTodoInDB],
)
def update_todo(
    meeting_id: int,
    todo_id: int,
    payload: schemas2.LocalMeetingTodoCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """修改待办事项。"""
    _get_meeting_or_404(db, meeting_id)
    todo = local_minutes_service.update_todo(db, meeting_id, todo_id, payload)
    if not todo:
        raise HTTPException(status_code=404, detail="待办事项未找到")
    return StandardResponse(
        success=True,
        data=schemas2.LocalMeetingTodoInDB.model_validate(todo),
        message="待办事项已更新",
    )


@router.delete(
    "/local/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[None],
)
def delete_todo(
    meeting_id: int,
    todo_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """删除待办事项。"""
    _get_meeting_or_404(db, meeting_id)
    if not local_minutes_service.delete_todo(db, meeting_id, todo_id):
        raise HTTPException(status_code=404, detail="待办事项未找到")
    return StandardResponse(success=True, data=None, message="待办事项已删除")
