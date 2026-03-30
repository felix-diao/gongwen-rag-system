"""
火山引擎会议纪要路由（精简版）

工作流A — 实时录音（两个按钮顺序触发）
  按钮1  WS   /api/minutes/volc/{meeting_id}/live
              实时录音 + 流式ASR推字；录音结束后自动保存WAV并上传TOS
              completed 消息携带 audio_id，供前端存储后触发按钮2
  按钮2  POST /api/minutes/volc/{meeting_id}/submit
              取最新TOS音频提交豆包语音妙记，覆盖纯转写文本并生成摘要+Todos

工作流B — 上传文件（三个按钮顺序触发）
  按钮1  POST /api/minutes/volc/{meeting_id}/upload
              上传音频到TOS，返回 audio_id
  按钮2  GET  /api/minutes/volc/audio/{audio_id}/stream?token=<JWT>
              SSE 流式ASR，实时推字；完成后转写文本落库
  按钮3  POST /api/minutes/volc/{meeting_id}/submit
              同工作流A按钮2，复用同一接口

查询 & 编辑
  GET    /api/minutes/volc/{meeting_id}                      查询会议纪要（转写+摘要+Todos）
  PUT    /api/minutes/volc/{meeting_id}/transcript           修改纯转写文本
  PUT    /api/minutes/volc/{meeting_id}/summary              修改摘要
  POST   /api/minutes/volc/{meeting_id}/todos                新增Todo
  PUT    /api/minutes/volc/{meeting_id}/todos/{todo_id}      修改Todo
  DELETE /api/minutes/volc/{meeting_id}/todos/{todo_id}      删除Todo
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Set

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.models import schemas2
from app.models.database import VolcMeetingAudio, get_db
from app.models.schemas import StandardResponse
from app.services.meeting_service import MeetingService
from app.services.volc_asr_service import LiveAsrHandler, _ensure_wav_on_disk, stream_file_asr
from app.services.volc_minutes_service import volc_minutes_service
from app.utils.auth import decode_access_token, get_current_user
from app.utils.logger import get_logger

router = APIRouter(prefix="/api/minutes", tags=["meeting_minutes_volc"])
logger = get_logger("meeting_minute_volc_api")
meeting_service = MeetingService()


# ─── 内部辅助 ─────────────────────────────────────────────────────────────────

def _get_meeting_or_404(db: Session, meeting_id: int):
    meeting = meeting_service.get_meeting(db, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="会议未找到")
    return meeting


def _get_latest_audio_or_404(db: Session, meeting_id: int) -> VolcMeetingAudio:
    """取该会议最新的 TOS 音频记录（用于提交妙记）。"""
    audio = (
        db.query(VolcMeetingAudio)
        .filter(VolcMeetingAudio.meeting_id == meeting_id)
        .order_by(VolcMeetingAudio.created_at.desc())
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

@router.websocket("/volc/{meeting_id}/live")
async def live_recording(
    websocket: WebSocket,
    meeting_id: int,
    token: str = Query(..., description="JWT access token"),
):
    """
    【工作流A 按钮1】实时录音 + 流式ASR。

    协议：
    - 连接 URL：ws://.../api/minutes/volc/{meeting_id}/live?token=<JWT>
    - 客户端发送（二进制）：PCM 音频帧（16kHz, 16-bit, 单声道）
    - 客户端发送（JSON）：
        {"action": "stop"}                                    # 结束录音
        {"action": "config", "rate": 16000, "channels": 1}   # 可选，配置参数
    - 服务端推送（JSON）：
        {"type": "session_created", "session_id": 123}
        {"type": "partial",   "text": "...", "accumulated": "..."}
        {"type": "final",     "text": "...", "accumulated": "..."}
        {"type": "saving_audio",   "session_id": 123}   # 用户停止录音后，开始保存 WAV 前推送
        {"type": "uploading_audio", "session_id": 123}   # 保存完成后，开始上传 TOS 前推送
        {"type": "completed", "session_id": 123, "audio_id": 456,
         "transcript": "...", "audio_uploaded": true, "duration_seconds": 60.0}
        {"type": "error",     "message": "..."}

    录音结束后服务端自动：
      1. 合成 WAV 保存本地
      2. 上传至 TOS 对象存储（completed.audio_id 即为 TOS 记录 ID）
      3. 转写文本落库

    前端收到 completed 后，将 audio_id 存储，用于触发【按钮2 提交妙记】。
    """
    logger.info("WS live_recording: connection attempt meeting_id=%s client=%s", meeting_id, websocket.client)
    try:
        decode_access_token(token)
    except HTTPException as e:
        logger.warning("WS live_recording: token invalid meeting_id=%s status=%s", meeting_id, e.status_code)
        await websocket.close(code=4001)
        return

    from app.models.database import SessionLocal
    db = SessionLocal()
    try:
        if not meeting_service.get_meeting(db, meeting_id):
            logger.warning("WS live_recording: meeting not found meeting_id=%s", meeting_id)
            await websocket.close(code=4004)
            return
        logger.info("WS live_recording: accept and start handler meeting_id=%s", meeting_id)
        handler = LiveAsrHandler(websocket, meeting_id, db)
        await handler.run()
        logger.info("WS live_recording: handler finished meeting_id=%s", meeting_id)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 工作流B 按钮1 — 上传音频到 TOS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/volc/{meeting_id}/upload",
    response_model=StandardResponse[schemas2.VolcAudioUploadTask],
)
def upload_audio(
    meeting_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    【工作流B 按钮1】上传音频文件到 TOS 对象存储。

    支持 WAV / MP3 / M4A / OGG 等格式。
    返回 audio_id，前端存储后用于触发【按钮2 流式ASR】。
    """
    _get_meeting_or_404(db, meeting_id)
    try:
        task = volc_minutes_service.start_upload_audio_task(
            db=db,
            meeting_id=meeting_id,
            upload_file=file,
            original_name=file.filename or "audio",
            content_type=file.content_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Upload audio failed meeting_id=%s: %s", meeting_id, exc)
        raise HTTPException(status_code=502, detail=f"上传至对象存储失败: {exc}") from exc

    return StandardResponse(
        success=True,
        data=task,
        message="音频上传任务已创建，请轮询任务状态",
    )

@router.get(
    "/volc/upload-tasks/{task_id}",
    response_model=StandardResponse[schemas2.VolcAudioUploadTask],
)
def get_upload_audio_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    task = volc_minutes_service.get_upload_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="上传任务未找到")
    return StandardResponse(success=True, data=task, message="获取上传任务状态成功")


# ═══════════════════════════════════════════════════════════════════════════════
# 工作流B 按钮2 — SSE 流式 ASR 转写
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/volc/audio/{audio_id}/stream")
async def stream_asr(
    audio_id: int,
    token: str = Query(..., description="JWT access token"),
    db: Session = Depends(get_db),
):
    """
    【工作流B 按钮2】对已上传 TOS 的音频进行流式 ASR，以 SSE 格式实时推送识别文字。

    连接方式（EventSource）：
      const es = new EventSource(`/api/minutes/volc/audio/${audioId}/stream?token=${token}`)

    SSE 事件格式：
      data: {"type": "session_created", "session_id": 123, "audio_id": 456}
      data: {"type": "partial",   "text": "...", "accumulated": "..."}
      data: {"type": "final",     "text": "...", "accumulated": "..."}
      data: {"type": "completed", "session_id": 123, "transcript": "..."}
      data: {"type": "error",     "message": "..."}

    转写完成后转写文本自动落库，前端随后可触发【按钮3 提交妙记】。
    """
    try:
        decode_access_token(token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="无效的 token")

    audio = db.query(VolcMeetingAudio).filter(VolcMeetingAudio.id == audio_id).first()
    if not audio:
        raise HTTPException(status_code=404, detail="音频记录未找到")
    _get_meeting_or_404(db, audio.meeting_id)

    # 从 TOS 下载到临时文件
    suffix = Path(audio.file_name or "audio").suffix or ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()

    try:
        volc_minutes_service.download_audio(audio.object_key, Path(tmp_path))
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=502, detail=f"从对象存储下载音频失败: {exc}") from exc

    # 确保是 WAV 格式
    actual_path = _ensure_wav_on_disk(tmp_path)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            async for event in stream_file_asr(audio_id, audio.meeting_id, actual_path):
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


# ═══════════════════════════════════════════════════════════════════════════════
# 共享 按钮A2/B3 — 提交语音妙记
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/volc/{meeting_id}/submit",
    response_model=StandardResponse[schemas2.VolcMeetingAudioInDB],
)
def submit_minutes(
    meeting_id: int,
    audio_id: Optional[int] = Query(None, description="指定要提交的音频 ID，不传则取该会议最新一条"),
    source: Optional[str] = Query(
        None,
        description="提交来源：live（在线录音）或 existing_audio（基于已有音频）",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    【工作流A 按钮2 / 工作流B 按钮3】提交音频到豆包语音妙记。

    - 若传 audio_id 则提交该条音频（用于「用已有音频生成纪要」时覆盖式提交刚转写的那条）
    - 若不传则取该会议最新一条 TOS 音频记录（按 created_at）
    - 提交后台异步处理：精准转写（覆盖粗转写）+ 摘要 + Todos
    - 处理完成通过会议 WebSocket 推送 volc_minutes_completed 消息
    - 可通过 GET /api/minutes/volc/{meeting_id} 查询最新结果
    """
    _get_meeting_or_404(db, meeting_id)
    if audio_id is not None:
        audio = db.query(VolcMeetingAudio).filter(
            VolcMeetingAudio.id == audio_id,
            VolcMeetingAudio.meeting_id == meeting_id,
        ).first()
        if not audio:
            raise HTTPException(status_code=404, detail="该会议下未找到指定音频")
    else:
        audio = _get_latest_audio_or_404(db, meeting_id)

    if source is not None and source not in {"live", "existing_audio"}:
        raise HTTPException(status_code=400, detail="source 仅支持 live 或 existing_audio")

    try:
        record = volc_minutes_service.submit_audio(
            db=db,
            audio_id=audio.id,
            source=source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return StandardResponse(
        success=True,
        data=schemas2.VolcMeetingAudioInDB.model_validate(record),
        message="已提交豆包语音妙记，处理中，完成后将通过 WebSocket 推送结果",
    )


@router.post(
    "/volc/{meeting_id}/abandon",
    response_model=StandardResponse[schemas2.VolcMeetingAudioInDB],
)
def abandon_minutes(
    meeting_id: int,
    audio_id: int = Query(..., description="要作废的音频任务 ID"),
    reason: Optional[str] = Query(None, description="作废原因"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    用户离开页面时，主动作废当前纪要生成任务。
    作废后任务不会继续产出纪要和会话历史。
    """
    _get_meeting_or_404(db, meeting_id)
    try:
        record = volc_minutes_service.abandon_audio_task(
            db=db,
            audio_id=audio_id,
            meeting_id=meeting_id,
            reason=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return StandardResponse(
        success=True,
        data=schemas2.VolcMeetingAudioInDB.model_validate(record),
        message="任务已作废",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 清空会议纪要（覆盖式：开启新录音/新生成前调用）
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/volc/{meeting_id}/clear",
    response_model=StandardResponse[None],
)
def clear_minutes(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """清空指定会议的全部妙记内容（摘要、待办、所有音频的转写/说话人文本），便于覆盖式生成新纪要。"""
    _get_meeting_or_404(db, meeting_id)
    volc_minutes_service.clear_minutes(db, meeting_id)
    return StandardResponse(success=True, data=None, message="已清空会议纪要")

@router.post(
    "/volc/{meeting_id}/discard",
    response_model=StandardResponse[None],
)
def discard_workspace(
    meeting_id: int,
    reason: Optional[str] = Query(None, description="丢弃原因"),
    current_audio_id: Optional[int] = Query(None, description="当前待丢弃的音频 ID"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """丢弃当前火山纪要工作区（重置/离开页面时使用，不保留当前会话）。"""
    _get_meeting_or_404(db, meeting_id)
    volc_minutes_service.discard_workspace(
        db=db,
        meeting_id=meeting_id,
        reason=reason or "用户离开页面或重置，当前工作区内容已丢弃",
        current_audio_id=current_audio_id,
    )
    return StandardResponse(success=True, data=None, message="已丢弃当前工作区")


# ═══════════════════════════════════════════════════════════════════════════════
# 查询 & 编辑
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/volc/{meeting_id}",
    response_model=StandardResponse[schemas2.VolcMeetingMinutesResponse],
)
def get_minutes(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """查询会议纪要：最新转写文本 + 摘要 + Todos。"""
    _get_meeting_or_404(db, meeting_id)
    minutes = volc_minutes_service.get_minutes(db, meeting_id)
    return StandardResponse(success=True, data=minutes, message="获取会议纪要成功")


@router.get(
    "/volc/{meeting_id}/sessions",
    response_model=StandardResponse[List[schemas2.VolcMeetingMinutesSessionInDB]],
)
def list_minutes_sessions(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """查询火山纪要会话历史列表（每次提交妙记一条）。"""
    _get_meeting_or_404(db, meeting_id)
    sessions = volc_minutes_service.list_minutes_sessions(db, meeting_id)
    return StandardResponse(success=True, data=sessions, message="获取会话历史成功")


@router.get(
    "/volc/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[schemas2.VolcMeetingMinutesSessionInDB],
)
def get_minutes_session(
    meeting_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """查询火山纪要会话历史详情。"""
    _get_meeting_or_404(db, meeting_id)
    session = volc_minutes_service.get_minutes_session(db, meeting_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话历史未找到")
    return StandardResponse(success=True, data=session, message="获取会话详情成功")


@router.put(
    "/volc/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[schemas2.VolcMeetingMinutesSessionInDB],
)
def update_minutes_session(
    meeting_id: int,
    session_id: int,
    payload: schemas2.VolcMeetingMinutesSessionUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """修改火山纪要会话历史详情（快照数据）。"""
    _get_meeting_or_404(db, meeting_id)
    session = volc_minutes_service.update_minutes_session(db, meeting_id, session_id, payload)
    if not session:
        raise HTTPException(status_code=404, detail="会话历史未找到")
    return StandardResponse(success=True, data=session, message="会话详情已更新")


@router.delete(
    "/volc/{meeting_id}/sessions/{session_id}",
    response_model=StandardResponse[None],
)
def delete_minutes_session(
    meeting_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """删除火山纪要会话历史。"""
    _get_meeting_or_404(db, meeting_id)
    deleted = volc_minutes_service.delete_minutes_session(db, meeting_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会话历史未找到")
    return StandardResponse(success=True, data=None, message="会话历史已删除")


@router.put(
    "/volc/{meeting_id}/transcript",
    response_model=StandardResponse[schemas2.VolcMeetingAudioInDB],
)
def update_transcript(
    meeting_id: int,
    payload: schemas2.VolcTranscriptUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """修改会议纯转写文本（写入最新一条 TOS 音频记录）。"""
    _get_meeting_or_404(db, meeting_id)
    try:
        audio = volc_minutes_service.update_latest_transcript(db, meeting_id, payload.transcript_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StandardResponse(
        success=True,
        data=schemas2.VolcMeetingAudioInDB.model_validate(audio),
        message="转写文本已更新",
    )


@router.put(
    "/volc/{meeting_id}/summary",
    response_model=StandardResponse[schemas2.VolcMeetingSummaryInDB],
)
def update_summary(
    meeting_id: int,
    payload: schemas2.VolcMeetingSummaryCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """修改会议摘要。"""
    _get_meeting_or_404(db, meeting_id)
    summary = volc_minutes_service.update_summary(db, meeting_id, payload)
    return StandardResponse(
        success=True,
        data=schemas2.VolcMeetingSummaryInDB.model_validate(summary),
        message="摘要已更新",
    )


@router.post(
    "/volc/{meeting_id}/todos",
    response_model=StandardResponse[schemas2.VolcMeetingTodoInDB],
)
def create_todo(
    meeting_id: int,
    payload: schemas2.VolcMeetingTodoCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """新增待办事项。"""
    _get_meeting_or_404(db, meeting_id)
    todo = volc_minutes_service.create_todo(db, meeting_id, payload)
    return StandardResponse(
        success=True,
        data=schemas2.VolcMeetingTodoInDB.model_validate(todo),
        message="待办事项已新增",
    )


@router.put(
    "/volc/{meeting_id}/todos/{todo_id}",
    response_model=StandardResponse[schemas2.VolcMeetingTodoInDB],
)
def update_todo(
    meeting_id: int,
    todo_id: int,
    payload: schemas2.VolcMeetingTodoCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """修改待办事项。"""
    _get_meeting_or_404(db, meeting_id)
    todo = volc_minutes_service.update_todo(db, meeting_id, todo_id, payload)
    if not todo:
        raise HTTPException(status_code=404, detail="待办事项未找到")
    return StandardResponse(
        success=True,
        data=schemas2.VolcMeetingTodoInDB.model_validate(todo),
        message="待办事项已更新",
    )


@router.delete(
    "/volc/{meeting_id}/todos/{todo_id}",
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
    if not volc_minutes_service.delete_todo(db, meeting_id, todo_id):
        raise HTTPException(status_code=404, detail="待办事项未找到")
    return StandardResponse(success=True, data=None, message="待办事项已删除")
