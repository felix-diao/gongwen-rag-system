"""本地 AI 会议纪要服务。

核心能力：
1. 建立本地实时 ASR WebSocket，会话结束后把录音上传为统一音频。
2. 基于最新流式转写调用 LLM 生成摘要和待办。
3. 维护“当前纪要视图”和“历史快照”两套数据。

设计约束：
1. local 模式没有独立的“精准转写”阶段，当前纪要直接建立在流式转写全文之上。
2. 生成纪要时坚持“失败就显式报错”，避免用模糊兜底文本掩盖问题。
3. 每次生成成功都会写入 `LocalMeetingMinutesSession`，方便回看和人工修订。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import aiohttp
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.models import database, schemas
from app.models.database import SessionLocal
from app.services.meeting_audio_service import meeting_audio_service
from app.services.meeting_minute_local_prompt import (
    build_local_minutes_llm_instruction,
    resolve_max_tokens,
)
from app.services.qwen_asr_incremental_http import (
    asr_http_runtime_params,
    build_asr_requests_session,
    ensure_incremental_http_serve_root,
    get_incremental_http_public_base,
    merge_pair,
    post_served_wav_chunk,
    transcribe_audio_file_incremental,
    validate_incremental_http_config,
    write_pcm_as_wav_file,
)
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger

logger = get_logger("meeting_local_minutes_service")
SESSION_NO_TIMEZONE = timezone(timedelta(hours=8))
_LOCAL_EMPTY_TRANSCRIPT_TITLE = "无有效发言内容"
_LOCAL_EMPTY_TRANSCRIPT_HINT = "本次录音未识别到有效发言内容"
_LOCAL_AUDIO_TRANSCRIBE_SUBMIT_LOCK = threading.Lock()
_LOCAL_ASR_ACTIVE_STATUS = {"pending", "processing", "running", "submitted"}
_LOCAL_CANCELLED_STATUS = {"cancelled", "canceled"}
_LOCAL_CANCEL_LOCK = threading.Lock()
_LOCAL_CANCEL_REQUESTED_ASR_IDS: set[int] = set()
_LOCAL_ACTIVE_GENERATE_ASR_IDS: set[int] = set()


class ProcessingCancelledError(RuntimeError):
    """当前处理任务已被用户取消。"""


def _request_local_cancel(asr_session_id: int) -> None:
    with _LOCAL_CANCEL_LOCK:
        _LOCAL_CANCEL_REQUESTED_ASR_IDS.add(int(asr_session_id))


def _clear_local_cancel(asr_session_id: int) -> None:
    with _LOCAL_CANCEL_LOCK:
        _LOCAL_CANCEL_REQUESTED_ASR_IDS.discard(int(asr_session_id))


def _is_local_cancel_requested(asr_session_id: int) -> bool:
    with _LOCAL_CANCEL_LOCK:
        return int(asr_session_id) in _LOCAL_CANCEL_REQUESTED_ASR_IDS


def _mark_local_generate_active(asr_session_id: int) -> None:
    with _LOCAL_CANCEL_LOCK:
        _LOCAL_ACTIVE_GENERATE_ASR_IDS.add(int(asr_session_id))


def _mark_local_generate_finished(asr_session_id: int) -> None:
    with _LOCAL_CANCEL_LOCK:
        _LOCAL_ACTIVE_GENERATE_ASR_IDS.discard(int(asr_session_id))


def _is_local_generate_active(asr_session_id: int) -> bool:
    with _LOCAL_CANCEL_LOCK:
        return int(asr_session_id) in _LOCAL_ACTIVE_GENERATE_ASR_IDS


def _raise_if_local_cancel_requested(
    db: Session,
    asr_session_id: int,
    message: str,
) -> None:
    if _is_local_cancel_requested(asr_session_id):
        raise ProcessingCancelledError(message)
    row = (
        db.query(database.LocalAsrSession)
        .filter(database.LocalAsrSession.id == asr_session_id)
        .first()
    )
    if row and str(row.status or "").strip().lower() in _LOCAL_CANCELLED_STATUS:
        raise ProcessingCancelledError(message)


def _run_local_uploaded_audio_transcribe_job(task: Dict[str, int]) -> None:
    asr_id = task["asr_session_id"]
    meeting_id = task["meeting_id"]
    audio_id = task["audio_id"]
    db = SessionLocal()
    tmp_path: Optional[Path] = None
    try:
        meeting_ws_manager.notify_from_thread(
            meeting_id,
            {
                "type": "local_audio_transcribe_stage",
                "meeting_id": meeting_id,
                "audio_id": audio_id,
                "asr_session_id": asr_id,
                "status": "processing",
                "phase": "downloading_audio",
                "message": "正在准备音频文件…",
            },
        )
        tmp_path, _, _ = meeting_audio_service.download_audio_to_temp(
            db, meeting_id, "local", audio_id
        )
        _raise_if_local_cancel_requested(db, asr_id, "已取消当前本地音频转写任务")
        def _persist_progress(partial_text: str, chunk_idx: int, total_chunks: int) -> None:
            row = (
                db.query(database.LocalAsrSession)
                .filter(database.LocalAsrSession.id == asr_id)
                .first()
            )
            if not row:
                return
            if _is_local_cancel_requested(asr_id) or str(row.status or "").strip().lower() in _LOCAL_CANCELLED_STATUS:
                raise ProcessingCancelledError("已取消当前本地音频转写任务")
            row.stream_transcript_text = partial_text
            row.source_audio_id = audio_id
            row.status = "processing"
            row.error_msg = None
            
            db.commit()
            logger.debug(
                "本地音频分段转写进度 meeting_id=%s audio_id=%s asr_session_id=%s chunk=%s/%s len=%s",
                meeting_id,
                audio_id,
                asr_id,
                chunk_idx,
                total_chunks,
                len(partial_text or ""),
            )
            meeting_ws_manager.notify_from_thread(
                meeting_id,
                {
                    "type": "local_audio_transcribe_progress",
                    "meeting_id": meeting_id,
                    "audio_id": audio_id,
                    "asr_session_id": asr_id,
                    "status": "processing",
                    "chunk_idx": chunk_idx,
                    "total_chunks": total_chunks,
                    "accumulated": partial_text,
                },
            )

        def _notify_stage(stage: Dict[str, Any]) -> None:
            _raise_if_local_cancel_requested(db, asr_id, "已取消当前本地音频转写任务")
            message = str(stage.get("message") or "").strip()
            logger.debug(
                "本地音频分段转写阶段 meeting_id=%s audio_id=%s asr_session_id=%s phase=%s message=%s",
                meeting_id,
                audio_id,
                asr_id,
                stage.get("phase"),
                message,
            )
            meeting_ws_manager.notify_from_thread(
                meeting_id,
                {
                    "type": "local_audio_transcribe_stage",
                    "meeting_id": meeting_id,
                    "audio_id": audio_id,
                    "asr_session_id": asr_id,
                    "status": "processing",
                    **stage,
                },
            )

        merged, duration = transcribe_audio_file_incremental(
            tmp_path,
            on_progress=_persist_progress,
            on_stage=_notify_stage,
        )
        _raise_if_local_cancel_requested(db, asr_id, "已取消当前本地音频转写任务")
        row = (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.id == asr_id)
            .first()
        )
        if not row:
            return
        row.stream_transcript_text = merged
        row.duration_seconds = duration
        row.source_audio_id = audio_id
        row.status = "completed"
        row.error_msg = None
        
        try:
            from app.services.token_tracker import token_tracker
            creator_id = db.execute(
                text("SELECT creator_id FROM meetings WHERE id = :id"),
                {"id": meeting_id},
            ).scalar()
            token_tracker.record(
                user_id=creator_id,
                api_category="qwen_asr",
                api_endpoint=settings.QWEN_ASR_HTTP_CHAT_URL or "qwen_asr_http",
                total_tokens=0,
                request_chars=len(merged or ""),
                duration_ms=int(duration * 1000) if duration else 0,
                status="success",
                metadata_json=json.dumps({
                    "meeting_id": meeting_id,
                    "asr_session_id": asr_id,
                }),
            )
        except Exception:
            pass
        
        audio = (
            db.query(database.MeetingAudio)
            .filter(
                database.MeetingAudio.id == audio_id,
                database.MeetingAudio.meeting_id == meeting_id,
                database.MeetingAudio.provider == "local",
            )
            .first()
        )
        if audio:
            audio.status = "completed"
        db.commit()
        minutes_generated = False
        minutes_error: Optional[str] = None
        try:
            local_meeting_minute_service.generate_minutes(
                db,
                meeting_id,
                asr_session_id=asr_id,
            )
            minutes_generated = True
        except ProcessingCancelledError:
            raise
        except Exception as minutes_exc:  # noqa: BLE001
            minutes_error = str(minutes_exc)
            logger.warning(
                "本地音频转写完成后自动生成纪要失败 meeting_id=%s audio_id=%s asr_session_id=%s error=%s",
                meeting_id,
                audio_id,
                asr_id,
                minutes_error,
            )
        logger.info(
            "本地音频分段转写完成 meeting_id=%s audio_id=%s asr_session_id=%s len=%s",
            meeting_id,
            audio_id,
            asr_id,
            len(merged or ""),
        )
        meeting_ws_manager.notify_from_thread(
            meeting_id,
            {
                "type": "local_audio_transcribe_completed",
                "meeting_id": meeting_id,
                "audio_id": audio_id,
                "asr_session_id": asr_id,
                "status": "completed",
                "duration_seconds": duration,
                "transcript": merged,
                "minutes_generated": minutes_generated,
                "minutes_error": minutes_error,
            },
        )
    except ProcessingCancelledError as exc:
        logger.info(
            "本地音频分段转写已取消 meeting_id=%s audio_id=%s asr_session_id=%s reason=%s",
            meeting_id,
            audio_id,
            asr_id,
            exc,
        )
        db.rollback()
        row = (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.id == asr_id)
            .first()
        )
        if row:
            previous_status = str(row.status or "").strip().lower()
            if previous_status in _LOCAL_ASR_ACTIVE_STATUS:
                row.status = "cancelled"
            row.error_msg = str(exc)
            audio = (
                db.query(database.MeetingAudio)
                .filter(
                    database.MeetingAudio.id == audio_id,
                    database.MeetingAudio.meeting_id == meeting_id,
                    database.MeetingAudio.provider == "local",
                )
                .first()
            )
            if audio and audio.status == "processing":
                audio.status = "uploaded"
            db.commit()
            if previous_status != "completed":
                meeting_ws_manager.notify_from_thread(
                    meeting_id,
                    {
                        "type": "local_audio_transcribe_cancelled",
                        "meeting_id": meeting_id,
                        "audio_id": audio_id,
                        "asr_session_id": asr_id,
                        "status": "cancelled",
                        "error": row.error_msg,
                        "transcript": row.stream_transcript_text,
                    },
                )
    except HTTPException as exc:
        logger.warning(
            "本地音频分段转写失败(HTTP) meeting_id=%s audio_id=%s asr_session_id=%s detail=%s",
            meeting_id,
            audio_id,
            asr_id,
            exc.detail,
        )
        row = (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.id == asr_id)
            .first()
        )
        if row:
            row.status = "failed"
            detail = exc.detail
            row.error_msg = detail if isinstance(detail, str) else repr(detail)
            audio = (
                db.query(database.MeetingAudio)
                .filter(
                    database.MeetingAudio.id == audio_id,
                    database.MeetingAudio.meeting_id == meeting_id,
                    database.MeetingAudio.provider == "local",
                )
                .first()
            )
            if audio:
                audio.status = "uploaded"
            db.commit()
            meeting_ws_manager.notify_from_thread(
                meeting_id,
                {
                    "type": "local_audio_transcribe_failed",
                    "meeting_id": meeting_id,
                    "audio_id": audio_id,
                    "asr_session_id": asr_id,
                    "status": "failed",
                    "error": row.error_msg,
                    "transcript": row.stream_transcript_text,
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "本地音频分段转写失败 meeting_id=%s audio_id=%s asr_session_id=%s",
            meeting_id,
            audio_id,
            asr_id,
        )
        row = (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.id == asr_id)
            .first()
        )
        if row:
            row.status = "failed"
            row.error_msg = str(exc)
            audio = (
                db.query(database.MeetingAudio)
                .filter(
                    database.MeetingAudio.id == audio_id,
                    database.MeetingAudio.meeting_id == meeting_id,
                    database.MeetingAudio.provider == "local",
                )
                .first()
            )
            if audio:
                audio.status = "uploaded"
            db.commit()
            meeting_ws_manager.notify_from_thread(
                meeting_id,
                {
                    "type": "local_audio_transcribe_failed",
                    "meeting_id": meeting_id,
                    "audio_id": audio_id,
                    "asr_session_id": asr_id,
                    "status": "failed",
                    "error": row.error_msg,
                    "transcript": row.stream_transcript_text,
                },
            )
    finally:
        _clear_local_cancel(asr_id)
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        db.close()


_TEXT_FIELDS = ("text", "transcript", "delta")
_NESTED_FIELDS = ("item", "data", "result", "response")
_FINAL_EVENT_TYPES = {"conversation.item.input_audio_transcription.completed"}
_PARTIAL_EVENT_TYPES = {"conversation.item.input_audio_transcription.text"}


def _extract_transcription_text(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""

    for key in _TEXT_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    for parent in _NESTED_FIELDS:
        child = payload.get(parent)
        if not isinstance(child, dict):
            continue
        for key in _TEXT_FIELDS:
            value = child.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _is_final_transcription_event(event_type: str) -> bool:
    if event_type in _FINAL_EVENT_TYPES:
        return True
    return (
        event_type.startswith("conversation.item.input_audio_transcription.")
        and event_type.endswith(".completed")
    )


def _is_partial_transcription_event(event_type: str) -> bool:
    if event_type in _PARTIAL_EVENT_TYPES:
        return True
    return (
        event_type.startswith("conversation.item.input_audio_transcription.")
        and event_type.endswith(".text")
    )


def _build_qwen_ws_url() -> str:
    """构造 Qwen 实时 ASR WebSocket 地址。"""
    base = (settings.QWEN_ASR_WS_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("QWEN_ASR_WS_URL 未配置")
    model = settings.QWEN_ASR_MODEL or "qwen3-asr-flash-realtime"
    if "model=" in base:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}model={model}"


def _build_qwen_ws_headers() -> Dict[str, str]:
    """构造 Qwen 实时 ASR 鉴权头。"""
    api_key = (settings.QWEN_ASR_API_KEY or "").strip()
    if not api_key:
        raise RuntimeError("QWEN_ASR_API_KEY 未配置")
    return {"Authorization": f"bearer {api_key}", "OpenAI-Beta": "realtime=v1"}


def _session_update_event() -> str:
    """构造实时 ASR 会话初始化事件。"""
    payload = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": 16000,
            "input_audio_transcription": {"language": settings.QWEN_ASR_LANGUAGE or "zh"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": float(settings.QWEN_ASR_VAD_THRESHOLD or 0.65),
                "silence_duration_ms": int(settings.QWEN_ASR_SILENCE_DURATION_MS or 400),
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _audio_append_event(pcm_bytes: bytes) -> str:
    """构造向实时 ASR 追加音频分片的事件。"""
    payload = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm_bytes).decode("ascii"),
    }
    return json.dumps(payload, ensure_ascii=False)


def _audio_commit_event() -> str:
    """构造音频输入结束事件。"""
    payload = {"event_id": f"evt_{uuid.uuid4().hex[:12]}", "type": "input_audio_buffer.commit"}
    return json.dumps(payload, ensure_ascii=False)


def _session_finish_event() -> str:
    """构造会话关闭事件。"""
    payload = {"event_id": f"evt_{uuid.uuid4().hex[:12]}", "type": "session.finish"}
    return json.dumps(payload, ensure_ascii=False)


def _save_pcm_as_wav(
    pcm_chunks: List[bytes],
    dest_path: Path,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> float:
    """把前端上传的 PCM 分片合并为 WAV，并返回录音时长（秒）。"""
    pcm_data = b"".join(pcm_chunks)
    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    frame_count = len(pcm_data) // (channels * sample_width)
    return frame_count / sample_rate


def _merge_wav_files(part_paths: List[Path], output_path: Path) -> None:
    """合并参数一致的 WAV 片段。"""
    if not part_paths:
        raise ValueError("没有可合并的 WAV 片段")

    with wave.open(str(part_paths[0]), "rb") as first:
        params = first.getparams()
        frames = [first.readframes(first.getnframes())]

    for part_path in part_paths[1:]:
        with wave.open(str(part_path), "rb") as current:
            current_params = current.getparams()
            if current_params[:3] != params[:3]:
                raise RuntimeError(
                    f"WAV 参数不一致，无法合并: {part_path}"
                )
            frames.append(current.readframes(current.getnframes()))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as output:
        output.setparams(params)
        for frame in frames:
            output.writeframes(frame)


class LocalMeetingMinuteService:
    # 设计约束：
    # 1) local 模式只依赖“当前最新流式转写”生成纪要，因此生成入口必须严格校验 ASR 会话存在；
    # 2) 当前纪要视图（summary/todo）与历史快照（session）同时维护，确保“可编辑”和“可回放”两种能力都成立；
    # 3) 不做静默失败兜底：LLM、转写、数据库任一环节出错，都必须留下明确日志和错误文案。

    def __init__(self) -> None:
        # key = (meeting_id, recording_session_id)
        self._pending_finalize_tasks: Dict[
            tuple[int, str],
            asyncio.Task,
        ] = {}
        self._active_live_handlers: Dict[
            tuple[int, str],
            "LiveLocalAsrHandler",
        ] = {}
        self._finalize_locks: Dict[
            tuple[int, str],
            asyncio.Lock,
        ] = {}

    def _assert_meeting_exists(self, db: Session, meeting_id: int) -> None:
        exists = db.execute(
            text("SELECT 1 FROM meetings WHERE id = :meeting_id LIMIT 1"),
            {"meeting_id": meeting_id},
        ).first()
        if not exists:
            raise ValueError("会议不存在")

    @staticmethod
    def _get_meeting_title(db: Session, meeting_id: int) -> str:
        row = db.execute(
            text("SELECT title FROM meetings WHERE id = :meeting_id LIMIT 1"),
            {"meeting_id": meeting_id},
        ).first()
        if not row:
            raise ValueError("会议不存在")
        title = row[0]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("会议标题不能为空")
        return title.strip()

    @staticmethod
    def _latest_asr_session(db: Session, meeting_id: int) -> Optional[database.LocalAsrSession]:
        return (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.meeting_id == meeting_id)
            .order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc())
            .first()
        )

    @staticmethod
    def _latest_asr_session_with_transcript(
        db: Session, meeting_id: int
    ) -> Optional[database.LocalAsrSession]:
        """用于生成纪要：跳过尚无正文的 processing 行，避免与异步文件转写抢最新空行。"""
        rows = (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.meeting_id == meeting_id)
            .order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc())
            .all()
        )
        for row in rows:
            if (row.stream_transcript_text or "").strip():
                return row
        return None

    @staticmethod
    def _active_asr_session_for_audio(
        db: Session,
        meeting_id: int,
        audio_id: int,
    ) -> Optional[database.LocalAsrSession]:
        return (
            db.query(database.LocalAsrSession)
            .filter(
                database.LocalAsrSession.meeting_id == meeting_id,
                database.LocalAsrSession.source_audio_id == audio_id,
                database.LocalAsrSession.status.in_(_LOCAL_ASR_ACTIVE_STATUS),
            )
            .order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc())
            .first()
        )

    @staticmethod
    def _cancel_target_asr_session(
        db: Session,
        meeting_id: int,
        asr_session_id: Optional[int],
    ) -> Optional[database.LocalAsrSession]:
        q = db.query(database.LocalAsrSession).filter(database.LocalAsrSession.meeting_id == meeting_id)
        if asr_session_id is not None:
            return q.filter(database.LocalAsrSession.id == asr_session_id).first()
        rows = (
            q.order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc())
            .all()
        )
        for row in rows:
            status = str(row.status or "").strip().lower()
            if status in _LOCAL_ASR_ACTIVE_STATUS or _is_local_generate_active(row.id):
                return row
        return None

    @staticmethod
    def _latest_processing_task(
        db: Session,
        meeting_id: int,
    ) -> tuple[Optional[database.LocalAsrSession], Optional[str]]:
        rows = (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.meeting_id == meeting_id)
            .order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc())
            .all()
        )
        for row in rows:
            status = str(row.status or "").strip().lower()
            if status in _LOCAL_ASR_ACTIVE_STATUS:
                return row, "transcribe"
            if _is_local_generate_active(row.id):
                return row, "minutes"
        return None, None

    @staticmethod
    def _asr_session_for_minutes_snapshot(
        db: Session,
        meeting_id: int,
        source_audio_id: Optional[int],
    ) -> Optional[database.LocalAsrSession]:
        """按快照 source_audio_id 定位对应 ASR 行；未绑音频时取该会议下最近一条 ASR。"""
        q = db.query(database.LocalAsrSession).filter(database.LocalAsrSession.meeting_id == meeting_id)
        if source_audio_id is not None:
            return (
                q.filter(database.LocalAsrSession.source_audio_id == source_audio_id)
                .order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc())
                .first()
            )
        return (
            q.order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc()).first()
        )

    @staticmethod
    def _local_audio_by_id(
        db: Session,
        meeting_id: int,
        audio_id: Optional[int],
    ) -> Optional[database.MeetingAudio]:
        if audio_id is None:
            return None
        return (
            db.query(database.MeetingAudio)
            .filter(
                database.MeetingAudio.id == audio_id,
                database.MeetingAudio.meeting_id == meeting_id,
                database.MeetingAudio.provider == "local",
            )
            .first()
        )

    @staticmethod
    def _latest_local_audio(db: Session, meeting_id: int) -> Optional[database.MeetingAudio]:
        return (
            db.query(database.MeetingAudio)
            .filter(
                database.MeetingAudio.meeting_id == meeting_id,
                database.MeetingAudio.provider == "local",
            )
            .order_by(database.MeetingAudio.updated_at.desc(), database.MeetingAudio.id.desc())
            .first()
        )

    def _recoverable_recording_info(
        self,
        db: Session,
        meeting_id: int,
        summary: Optional[database.LocalMeetingSummary],
    ) -> Optional[schemas.RecoverableRecordingInfo]:
        if summary:
            return None

        sessions = (
            db.query(database.LocalAsrSession)
            .filter(
                database.LocalAsrSession.meeting_id == meeting_id,
                database.LocalAsrSession.recording_session_id.isnot(None),
            )
            .order_by(
                database.LocalAsrSession.updated_at.desc(),
                database.LocalAsrSession.id.desc(),
            )
            .all()
        )

        seen_recording_ids: set[str] = set()
        for session in sessions:
            recording_session_id = (
                session.recording_session_id or ""
            ).strip()
            if not recording_session_id:
                continue
            if recording_session_id in seen_recording_ids:
                continue
            seen_recording_ids.add(recording_session_id)

            related_sessions = [
                item
                for item in sessions
                if item.recording_session_id == recording_session_id
            ]
            has_audio_part = any(
                bool(item.audio_part_path) for item in related_sessions
            )
            has_audio_id = any(
                item.source_audio_id is not None
                for item in related_sessions
            )
            has_processing = any(
                str(item.status or "").lower() == "processing"
                for item in related_sessions
            )

            if has_audio_id:
                continue

            if not has_audio_part:
                continue

            return schemas.RecoverableRecordingInfo(
                provider="local",
                recording_session_id=recording_session_id,
                asr_session_id=session.id,
                status="active" if has_processing else "saved_part",
                has_audio_part=True,
            )

        return None

    def register_live_handler(
        self,
        meeting_id: int,
        recording_session_id: str,
        handler: "LiveLocalAsrHandler",
    ) -> None:
        if not recording_session_id:
            return
        self._active_live_handlers[
            (meeting_id, recording_session_id)
        ] = handler

    def unregister_live_handler(
        self,
        meeting_id: int,
        recording_session_id: str,
        handler: "LiveLocalAsrHandler",
    ) -> None:
        if not recording_session_id:
            return
        key = (meeting_id, recording_session_id)
        if self._active_live_handlers.get(key) is handler:
            self._active_live_handlers.pop(key, None)

    async def request_active_recording_finalize(
        self,
        meeting_id: int,
        recording_session_id: str,
    ) -> bool:
        handler = self._active_live_handlers.get(
            (meeting_id, recording_session_id)
        )
        if not handler:
            return False

        await handler.request_recover_finalize()
        return True

    def cancel_delayed_finalize(
        self,
        meeting_id: int,
        recording_session_id: str,
    ) -> None:
        """前端使用同一 recording_session_id 重连后，取消延迟收尾。"""
        key = (meeting_id, recording_session_id)
        task = self._pending_finalize_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            logger.info(
                "取消本地录音延迟收尾 meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )

    def schedule_delayed_finalize(
        self,
        meeting_id: int,
        recording_session_id: str,
    ) -> None:
        """WS 异常断开后延迟收尾，给前端自动重连留时间。"""
        if not recording_session_id:
            return

        key = (meeting_id, recording_session_id)
        existing = self._pending_finalize_tasks.get(key)
        if existing and not existing.done():
            return

        task = asyncio.create_task(
            self._delayed_finalize_recording(
                meeting_id=meeting_id,
                recording_session_id=recording_session_id,
            )
        )
        self._pending_finalize_tasks[key] = task
        logger.info(
            "已安排本地录音延迟收尾 meeting_id=%s recording_session_id=%s delay=%ss",
            meeting_id,
            recording_session_id,
            settings.VOLC_RECORDING_RECONNECT_GRACE_SECONDS,
        )

    async def _delayed_finalize_recording(
        self,
        meeting_id: int,
        recording_session_id: str,
    ) -> None:
        key = (meeting_id, recording_session_id)
        current_task = asyncio.current_task()

        try:
            await asyncio.sleep(
                settings.VOLC_RECORDING_RECONNECT_GRACE_SECONDS
            )

            db = SessionLocal()
            try:
                processing_count = (
                    db.query(database.LocalAsrSession)
                    .filter(
                        database.LocalAsrSession.meeting_id == meeting_id,
                        database.LocalAsrSession.recording_session_id == recording_session_id,
                        database.LocalAsrSession.status == "processing",
                    )
                    .count()
                )
                if processing_count > 0:
                    logger.info(
                        "本地录音已重连或仍在处理，跳过延迟收尾 "
                        "meeting_id=%s recording_session_id=%s processing=%d",
                        meeting_id,
                        recording_session_id,
                        processing_count,
                    )
                    return

                result = await self.finalize_and_generate_async(
                    db=db,
                    meeting_id=meeting_id,
                    recording_session_id=recording_session_id,
                )
                logger.info(
                    "本地录音延迟收尾完成 "
                    "meeting_id=%s recording_session_id=%s result=%s",
                    meeting_id,
                    recording_session_id,
                    result,
                )
            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info(
                "本地录音延迟收尾任务已取消 "
                "meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )
            raise
        except Exception:
            logger.exception(
                "本地录音延迟收尾失败 "
                "meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )
        finally:
            if self._pending_finalize_tasks.get(key) is current_task:
                self._pending_finalize_tasks.pop(key, None)

    @staticmethod
    def _assert_local_audio(db: Session, meeting_id: int, audio_id: int) -> database.MeetingAudio:
        rec = (
            db.query(database.MeetingAudio)
            .filter(
                database.MeetingAudio.id == audio_id,
                database.MeetingAudio.meeting_id == meeting_id,
                database.MeetingAudio.provider == "local",
            )
            .first()
        )
        if not rec:
            raise ValueError("本地音频记录不存在或不属于该会议")
        return rec

    def queue_transcribe_uploaded_local_audio(
        self,
        db: Session,
        meeting_id: int,
        audio_id: int,
    ) -> schemas.LocalAsrTranscribeFromAudioResponse:
        """对已上传的本地会议音频排队分段 HTTP 转写（异步写入 local_asr_sessions）。"""
        self._assert_meeting_exists(db, meeting_id)
        with _LOCAL_AUDIO_TRANSCRIBE_SUBMIT_LOCK:
            audio = self._assert_local_audio(db, meeting_id, audio_id)
            existing = self._active_asr_session_for_audio(db, meeting_id, audio_id)
            if existing:
                if audio.status != "processing":
                    audio.status = "processing"
                    db.commit()
                logger.info(
                    "复用进行中的本地音频转写任务 meeting_id=%s audio_id=%s asr_session_id=%s",
                    meeting_id,
                    audio_id,
                    existing.id,
                )
                return schemas.LocalAsrTranscribeFromAudioResponse(
                    asr_session_id=existing.id,
                    meeting_id=meeting_id,
                    source_audio_id=audio_id,
                    status="processing",
                )

            audio.status = "processing"
            db.flush()
        validate_incremental_http_config()

        asr = database.LocalAsrSession(
            meeting_id=meeting_id,
            status="processing",
            source_audio_id=audio_id,
        )
        db.add(asr)
        db.commit()
        db.refresh(asr)
        asr_id = asr.id

        task = {"asr_session_id": asr_id, "meeting_id": meeting_id, "audio_id": audio_id}
        threading.Thread(
            target=_run_local_uploaded_audio_transcribe_job,
            args=(task,),
            daemon=True,
            name=f"local-asr-file-{asr_id}",
        ).start()
        meeting_ws_manager.notify_from_thread(
            meeting_id,
            {
                "type": "local_audio_transcribe_started",
                "meeting_id": meeting_id,
                "audio_id": audio_id,
                "asr_session_id": asr_id,
                "status": "processing",
            },
        )
        logger.info(
            "已排队本地音频分段转写 meeting_id=%s audio_id=%s asr_session_id=%s",
            meeting_id,
            audio_id,
            asr_id,
        )
        return schemas.LocalAsrTranscribeFromAudioResponse(
            asr_session_id=asr_id,
            meeting_id=meeting_id,
            source_audio_id=audio_id,
            status="processing",
        )

    @staticmethod
    def _meeting_summary(db: Session, meeting_id: int) -> Optional[database.LocalMeetingSummary]:
        return (
            db.query(database.LocalMeetingSummary)
            .filter(database.LocalMeetingSummary.meeting_id == meeting_id)
            .first()
        )

    @staticmethod
    def _meeting_todos(db: Session, meeting_id: int) -> List[database.LocalMeetingTodo]:
        return (
            db.query(database.LocalMeetingTodo)
            .filter(database.LocalMeetingTodo.meeting_id == meeting_id)
            .order_by(database.LocalMeetingTodo.id.asc())
            .all()
        )

    @staticmethod
    def _latest_minutes_session(
        db: Session,
        meeting_id: int,
    ) -> Optional[database.LocalMeetingMinutesSession]:
        return (
            db.query(database.LocalMeetingMinutesSession)
            .filter(database.LocalMeetingMinutesSession.meeting_id == meeting_id)
            .order_by(
                database.LocalMeetingMinutesSession.created_at.desc(),
                database.LocalMeetingMinutesSession.id.desc(),
            )
            .first()
        )
    
    def _create_empty_minutes_for_audio(
        self,
        db: Session,
        meeting_id: int,
        asr_session: database.LocalAsrSession,
    ) -> schemas.LocalMeetingMinutesResponse:
        source_audio_id = asr_session.source_audio_id
        if source_audio_id is None:
            latest_local_audio = self._latest_local_audio(db, meeting_id)
            source_audio_id = latest_local_audio.id if latest_local_audio else None

        db.query(database.LocalMeetingSummary).filter(
            database.LocalMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingTodo).filter(
            database.LocalMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.flush()

        db.add(
            database.LocalMeetingSummary(
                meeting_id=meeting_id,
                source_audio_id=source_audio_id,
                title=_LOCAL_EMPTY_TRANSCRIPT_TITLE,
                paragraph=_LOCAL_EMPTY_TRANSCRIPT_HINT,
            )
        )
        db.flush()

        self._create_minutes_session_snapshot(
            db=db,
            meeting_id=meeting_id,
            source_audio_id=source_audio_id,
            stream_transcript_text=asr_session.stream_transcript_text or "",
        )

        db.commit()
        logger.info(
            "本地会议无有效转写，已生成空纪要 meeting_id=%s asr_session_id=%s source_audio_id=%s",
            meeting_id,
            asr_session.id,
            source_audio_id,
        )
        return self.get_minutes(db, meeting_id)

    def _create_empty_minutes_without_audio(
        self,
        db: Session,
        meeting_id: int,
        reason: Optional[str] = None,
    ) -> schemas.LocalMeetingMinutesResponse:
        db.query(database.LocalMeetingSummary).filter(
            database.LocalMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingTodo).filter(
            database.LocalMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.flush()

        db.add(
            database.LocalMeetingSummary(
                meeting_id=meeting_id,
                source_audio_id=None,
                title=_LOCAL_EMPTY_TRANSCRIPT_TITLE,
                paragraph=_LOCAL_EMPTY_TRANSCRIPT_HINT,
            )
        )
        db.flush()

        self._create_minutes_session_snapshot(
            db=db,
            meeting_id=meeting_id,
            source_audio_id=None,
            stream_transcript_text="",
        )

        db.commit()
        logger.info(
            "本地会议无可用音频，已生成空纪要 meeting_id=%s reason=%s",
            meeting_id,
            reason,
        )
        return self.get_minutes(db, meeting_id)

    async def recover_and_finalize_async(
        self,
        db: Session,
        meeting_id: int,
        recording_session_id: str,
    ) -> Dict[str, Any]:
        """用户回到列表/详情时，主动推进异常录音收尾。"""
        if not recording_session_id:
            raise ValueError("recording_session_id 不能为空")

        self.cancel_delayed_finalize(
            meeting_id=meeting_id,
            recording_session_id=recording_session_id,
        )

        active_requested = await self.request_active_recording_finalize(
            meeting_id=meeting_id,
            recording_session_id=recording_session_id,
        )

        if active_requested:
            logger.info(
                "已通知本地实时录音 handler 主动收尾 "
                "meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )

        return await self.finalize_and_generate_async(
            db=db,
            meeting_id=meeting_id,
            recording_session_id=recording_session_id,
        )

    async def finalize_and_generate_async(
        self,
        db: Session,
        meeting_id: int,
        recording_session_id: str,
    ) -> Dict[str, Any]:
        """合并同一次本地录音的所有片段，并保证只生成一次纪要。"""
        if not recording_session_id:
            raise ValueError("recording_session_id 不能为空")

        key = (meeting_id, recording_session_id)
        lock = self._finalize_locks.setdefault(
            key,
            asyncio.Lock(),
        )

        async with lock:
            audio = await self.finalize_recording_async(
                db=db,
                meeting_id=meeting_id,
                recording_session_id=recording_session_id,
                auto_generate_minutes=True,
            )

            if not audio:
                empty_minutes = self._create_empty_minutes_without_audio(
                    db=db,
                    meeting_id=meeting_id,
                    reason="no_audio",
                )
                return {
                    "status": "completed_empty",
                    "audio_id": None,
                    "asr_session_id": getattr(empty_minutes, "asr_session_id", None),
                }

            latest_session = (
                db.query(database.LocalAsrSession)
                .filter(
                    database.LocalAsrSession.meeting_id == meeting_id,
                    database.LocalAsrSession.source_audio_id == audio.id,
                )
                .order_by(database.LocalAsrSession.id.desc())
                .first()
            )

            existing_minutes = self._latest_minutes_session(db, meeting_id)
            if (
                existing_minutes
                and existing_minutes.source_audio_id == audio.id
            ):
                return {
                    "status": "already_submitted",
                    "audio_id": audio.id,
                    "asr_session_id": latest_session.id if latest_session else None,
                }

            audio_status = str(audio.status or "").strip().lower()
            if audio_status == "failed":
                return {
                    "status": "failed_no_audio",
                    "audio_id": audio.id,
                    "asr_session_id": latest_session.id if latest_session else None,
                }

            if audio_status == "uploaded":
                self.generate_minutes(
                    db=db,
                    meeting_id=meeting_id,
                    asr_session_id=latest_session.id if latest_session else None,
                )
                return {
                    "status": "submitted",
                    "audio_id": audio.id,
                    "asr_session_id": latest_session.id if latest_session else None,
                }

            return {
                "status": "accepted",
                "audio_id": audio.id,
                "asr_session_id": latest_session.id if latest_session else None,
            }

    async def finalize_recording_async(
        self,
        db: Session,
        meeting_id: int,
        recording_session_id: Optional[str] = None,
        max_wait_seconds: int = 10,
        auto_generate_minutes: bool = False,
    ) -> Optional[database.MeetingAudio]:
        """结束本地录音时统一合并同一次 recording_session 的所有片段。"""
        self._assert_meeting_exists(db, meeting_id)

        if not recording_session_id:
            latest_session = self._latest_asr_session(db, meeting_id)
            if not latest_session:
                logger.warning("会议没有任何本地 ASR 会话 meeting_id=%s", meeting_id)
                return None
            recording_session_id = latest_session.recording_session_id
            if not recording_session_id:
                logger.info(
                    "会议没有 recording_session_id，使用最新本地音频 meeting_id=%s",
                    meeting_id,
                )
                return self._latest_local_audio(db, meeting_id)

        for waited in range(max_wait_seconds):
            processing_count = (
                db.query(database.LocalAsrSession)
                .filter(
                    database.LocalAsrSession.meeting_id == meeting_id,
                    database.LocalAsrSession.recording_session_id == recording_session_id,
                    database.LocalAsrSession.status == "processing",
                )
                .count()
            )
            if processing_count == 0:
                break
            logger.info(
                "等待本地 ASR 会话结束 meeting_id=%s recording_session_id=%s processing=%d waited=%d",
                meeting_id,
                recording_session_id,
                processing_count,
                waited,
            )
            await asyncio.sleep(1)
        else:
            logger.warning(
                "等待本地 ASR 会话结束超时 meeting_id=%s recording_session_id=%s max_wait=%d",
                meeting_id,
                recording_session_id,
                max_wait_seconds,
            )

        sessions = (
            db.query(database.LocalAsrSession)
            .filter(
                database.LocalAsrSession.meeting_id == meeting_id,
                database.LocalAsrSession.recording_session_id == recording_session_id,
                database.LocalAsrSession.status.in_(["completed", "failed"]),
                database.LocalAsrSession.audio_part_path.isnot(None),
            )
            .order_by(database.LocalAsrSession.created_at.asc())
            .all()
        )
        if not sessions:
            logger.warning(
                "没有可合并的本地 ASR 片段 meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )
            return None

        existing_merged_ids = {
            sess.source_audio_id
            for sess in sessions
            if sess.source_audio_id is not None
        }
        if (
            len(existing_merged_ids) == 1
            and all(sess.source_audio_id is not None for sess in sessions)
        ):
            existing_audio_id = next(iter(existing_merged_ids))
            existing_audio = (
                db.query(database.MeetingAudio)
                .filter(
                    database.MeetingAudio.id == existing_audio_id,
                    database.MeetingAudio.meeting_id == meeting_id,
                    database.MeetingAudio.provider == "local",
                )
                .first()
            )
            if existing_audio:
                logger.info(
                    "本地录音已经完成合并，无需重复处理 "
                    "meeting_id=%s recording_session_id=%s audio_id=%s",
                    meeting_id,
                    recording_session_id,
                    existing_audio.id,
                )
                return existing_audio

        creator_row = db.execute(
            text("SELECT creator_id FROM meetings WHERE id = :meeting_id LIMIT 1"),
            {"meeting_id": meeting_id},
        ).first()
        creator_id = creator_row.creator_id if creator_row else None

        wav_dir = Path(
            settings.QWEN_ASR_AUDIO_SAVE_DIR
            or os.path.join(settings.UPLOAD_DIR, "local_asr_recordings")
        )
        wav_dir.mkdir(parents=True, exist_ok=True)

        part_paths = [
            Path(sess.audio_part_path)
            for sess in sessions
            if sess.audio_part_path
        ]
        if not part_paths or not all(p.exists() for p in part_paths):
            missing = [str(p) for p in part_paths if not p.exists()]
            logger.error(
                "部分本地音频片段文件缺失 meeting_id=%s recording_session_id=%s missing=%s",
                meeting_id,
                recording_session_id,
                missing,
            )
            raise RuntimeError(f"本地音频片段文件缺失: {missing}")

        merged_path = (
            wav_dir
            / f"meeting_{meeting_id}_recording_{recording_session_id}_merged.wav"
        )
        _merge_wav_files(part_paths, merged_path)

        latest_session = sessions[-1]
        merged_transcript = "\n".join(
            [
                (sess.stream_transcript_text or "").strip()
                for sess in sessions
                if (sess.stream_transcript_text or "").strip()
            ]
        )
        total_duration = sum((sess.duration_seconds or 0) for sess in sessions)

        on_upload_complete: Optional[Callable[[database.MeetingAudio], None]] = None
        if auto_generate_minutes:

            def _on_upload_complete(uploaded_audio: database.MeetingAudio) -> None:
                db_callback = SessionLocal()
                try:
                    callback_session = (
                        db_callback.query(database.LocalAsrSession)
                        .filter(
                            database.LocalAsrSession.meeting_id == meeting_id,
                            database.LocalAsrSession.source_audio_id == uploaded_audio.id,
                        )
                        .order_by(database.LocalAsrSession.id.desc())
                        .first()
                    )
                    if not callback_session:
                        logger.warning(
                            "本地音频上传完成但未找到 ASR 会话 "
                            "meeting_id=%s audio_id=%s",
                            meeting_id,
                            uploaded_audio.id,
                        )
                        return

                    existing_minutes = self._latest_minutes_session(
                        db_callback,
                        meeting_id,
                    )
                    if (
                        existing_minutes
                        and existing_minutes.source_audio_id == uploaded_audio.id
                    ):
                        logger.info(
                            "本地纪要已存在，跳过自动生成 "
                            "meeting_id=%s audio_id=%s session_id=%s",
                            meeting_id,
                            uploaded_audio.id,
                            existing_minutes.id,
                        )
                        return

                    self.generate_minutes(
                        db=db_callback,
                        meeting_id=meeting_id,
                        asr_session_id=callback_session.id,
                    )
                except Exception:
                    logger.exception(
                        "自动生成本地纪要失败 meeting_id=%s audio_id=%s",
                        meeting_id,
                        uploaded_audio.id,
                    )
                finally:
                    db_callback.close()

            on_upload_complete = _on_upload_complete

        audio_record = meeting_audio_service.create_audio_from_path_async(
            db=db,
            meeting_id=meeting_id,
            provider="local",
            creator_id=creator_id,
            source_path=merged_path,
            file_name=f"recording_{recording_session_id}.wav",
            content_type="audio/wav",
            on_upload_complete=on_upload_complete,
        )

        audio_record.duration_seconds = total_duration

        for sess in sessions:
            sess.source_audio_id = audio_record.id
            sess.status = "completed"
            sess.error_msg = None

        latest_session.stream_transcript_text = merged_transcript
        latest_session.duration_seconds = total_duration
        db.commit()
        db.refresh(audio_record)

        for p in part_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                logger.warning("本地录音片段清理失败 path=%s", p)

        logger.info(
            "本地录音合并完成并已启动异步上传 "
            "meeting_id=%s recording_session_id=%s audio_id=%s duration=%.3f sessions=%d",
            meeting_id,
            recording_session_id,
            audio_record.id,
            total_duration,
            len(sessions),
        )
        return audio_record

    def generate_minutes(
        self,
        db: Session,
        meeting_id: int,
        asr_session_id: Optional[int] = None,
    ) -> schemas.LocalMeetingMinutesResponse:
        logger.info(
            "开始生成本地会议纪要 meeting_id=%s asr_session_id=%s",
            meeting_id,
            asr_session_id,
        )
        self._assert_meeting_exists(db, meeting_id)
        asr_session: Optional[database.LocalAsrSession] = None
        if asr_session_id is not None:
            asr_session = (
                db.query(database.LocalAsrSession)
                .filter(
                    database.LocalAsrSession.id == asr_session_id,
                    database.LocalAsrSession.meeting_id == meeting_id,
                )
                .first()
            )
            if not asr_session:
                raise ValueError("指定的转写会话不存在")

        if asr_session is None:
            asr_session = self._latest_asr_session_with_transcript(db, meeting_id)

        if asr_session is None:
            latest_session = self._latest_asr_session(db, meeting_id)
            if latest_session and latest_session.source_audio_id is not None:
                asr_session = latest_session

        if not asr_session:
            raise ValueError("当前会议暂无可用转写文本，请先完成实时录音、HTTP 分段录音或上传音频转写")

        if not (asr_session.stream_transcript_text or "").strip():
            if asr_session.source_audio_id is not None or self._latest_local_audio(db, meeting_id):
                return self._create_empty_minutes_for_audio(
                    db=db,
                    meeting_id=meeting_id,
                    asr_session=asr_session,
                )
            raise ValueError("当前转写会话尚未生成有效文本")

        _mark_local_generate_active(asr_session.id)
        try:
            _raise_if_local_cancel_requested(db, asr_session.id, "已取消当前会议纪要生成任务")
            meeting_title = self._get_meeting_title(db, meeting_id)
            creator_id = db.execute(
                text("SELECT creator_id FROM meetings WHERE id = :id"),
                {"id": meeting_id},
            ).scalar()
            payload = self._call_llm(
                meeting_title,
                asr_session.stream_transcript_text or "",
                duration_seconds=asr_session.duration_seconds or 0,
                user_id=creator_id,
                meeting_id=meeting_id,
            )
            _raise_if_local_cancel_requested(db, asr_session.id, "已取消当前会议纪要生成任务")

            source_audio_id = asr_session.source_audio_id
            if source_audio_id is None:
                latest_local_audio = self._latest_local_audio(db, meeting_id)
                source_audio_id = latest_local_audio.id if latest_local_audio else None

            db.query(database.LocalMeetingSummary).filter(
                database.LocalMeetingSummary.meeting_id == meeting_id
            ).delete(synchronize_session=False)
            db.query(database.LocalMeetingTodo).filter(
                database.LocalMeetingTodo.meeting_id == meeting_id
            ).delete(synchronize_session=False)
            db.flush()
            _raise_if_local_cancel_requested(db, asr_session.id, "已取消当前会议纪要生成任务")

            summary = payload["summary"]
            db.add(
                database.LocalMeetingSummary(
                    meeting_id=meeting_id,
                    source_audio_id=source_audio_id,
                    title=summary["title"],
                    paragraph=summary["paragraph"],
                )
            )

            for item in payload["todos"]:
                db.add(
                    database.LocalMeetingTodo(
                        meeting_id=meeting_id,
                        source_audio_id=source_audio_id,
                        content=item["content"],
                        executor=item.get("executor"),
                        execution_time=item.get("execution_time"),
                    )
                )

            db.flush()
            _raise_if_local_cancel_requested(db, asr_session.id, "已取消当前会议纪要生成任务")
            self._create_minutes_session_snapshot(
                db=db,
                meeting_id=meeting_id,
                source_audio_id=source_audio_id,
                stream_transcript_text=asr_session.stream_transcript_text,
            )
            db.commit()
            logger.info(
                "本地会议纪要生成完成 meeting_id=%s asr_session_id=%s source_audio_id=%s",
                meeting_id,
                asr_session.id,
                source_audio_id,
            )
            return self.get_minutes(db, meeting_id)
        except ProcessingCancelledError:
            db.rollback()
            meeting_ws_manager.notify_from_thread(
                meeting_id,
                {
                    "type": "local_minutes_cancelled",
                    "meeting_id": meeting_id,
                    "asr_session_id": asr_session.id,
                    "status": "cancelled",
                    "source_audio_id": asr_session.source_audio_id,
                },
            )
            raise
        finally:
            _mark_local_generate_finished(asr_session.id)
            _clear_local_cancel(asr_session.id)

    def cancel_processing(
        self,
        db: Session,
        meeting_id: int,
        asr_session_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> schemas.LocalProcessingCancelResponse:
        self._assert_meeting_exists(db, meeting_id)
        asr = self._cancel_target_asr_session(db, meeting_id, asr_session_id)
        if not asr:
            raise ValueError("当前没有可取消的本地处理任务")

        status = str(asr.status or "").strip().lower()
        transcribe_active = status in _LOCAL_ASR_ACTIVE_STATUS or status in _LOCAL_CANCELLED_STATUS
        minutes_active = _is_local_generate_active(asr.id)
        if not transcribe_active and not minutes_active:
            raise ValueError("当前没有可取消的本地处理任务")

        _request_local_cancel(asr.id)
        message = str(reason or "").strip() or "用户已取消当前处理"
        stage = "transcribe" if transcribe_active else "minutes"
        if status in _LOCAL_ASR_ACTIVE_STATUS:
            asr.status = "cancelled"
            asr.error_msg = message
            audio = self._local_audio_by_id(db, meeting_id, asr.source_audio_id)
            if audio and audio.status == "processing":
                audio.status = "uploaded"
            db.commit()

        logger.info(
            "已请求取消本地处理 meeting_id=%s asr_session_id=%s stage=%s",
            meeting_id,
            asr.id,
            stage,
        )
        return schemas.LocalProcessingCancelResponse(
            meeting_id=meeting_id,
            asr_session_id=asr.id,
            source_audio_id=asr.source_audio_id,
            stage=stage,
            status="cancel_requested",
        )

    def get_minutes(self, db: Session, meeting_id: int) -> schemas.LocalMeetingMinutesResponse:
        self._assert_meeting_exists(db, meeting_id)
        latest_session = self._latest_asr_session(db, meeting_id)
        processing_session, processing_stage = self._latest_processing_task(db, meeting_id)
        latest_minutes_session = self._latest_minutes_session(db, meeting_id)
        latest_audio = self._latest_local_audio(db, meeting_id)
        summary = self._meeting_summary(db, meeting_id)
        todos = self._meeting_todos(db, meeting_id)
        display_session: Optional[database.LocalAsrSession] = None
        transcript: Optional[str] = None
        pinned_audio_id: Optional[int] = None

        # 当前主视图一旦已有纪要结果，就优先回显与该结果同源的转写内容，避免被会议里其他更新的 ASR 稿串掉。
        if latest_minutes_session and (summary is not None or bool(todos)):
            pinned_audio_id = latest_minutes_session.source_audio_id
            transcript = latest_minutes_session.stream_transcript_text
            display_session = self._asr_session_for_minutes_snapshot(db, meeting_id, pinned_audio_id)
        elif summary and summary.source_audio_id is not None:
            pinned_audio_id = summary.source_audio_id
            display_session = self._asr_session_for_minutes_snapshot(db, meeting_id, pinned_audio_id)

        if display_session is None:
            display_session = latest_session

        if transcript is None:
            if display_session and (
                (display_session.stream_transcript_text or "").strip()
                or display_session.source_audio_id is not None
            ):
                transcript = display_session.stream_transcript_text
            else:
                with_text = self._latest_asr_session_with_transcript(db, meeting_id)
                transcript = with_text.stream_transcript_text if with_text else None

        source_audio_id = pinned_audio_id
        if source_audio_id is None and display_session:
            source_audio_id = display_session.source_audio_id

        source_audio = self._local_audio_by_id(db, meeting_id, source_audio_id)
        recoverable_recording = self._recoverable_recording_info(
            db=db,
            meeting_id=meeting_id,
            summary=summary,
        )
        return schemas.LocalMeetingMinutesResponse(
            transcript_text=transcript,
            stream_transcript_text=transcript,
            asr_session_id=display_session.id if display_session else None,
            asr_status=display_session.status if display_session else None,
            processing_asr_session_id=processing_session.id if processing_session else None,
            processing_stage=processing_stage,
            processing_status="processing" if processing_session and processing_stage else None,
            source_audio_id=source_audio_id,
            audio_status=(
                source_audio.status
                if source_audio
                else (latest_audio.status if latest_audio else (display_session.status if display_session else None))
            ),
            recoverable_recording=recoverable_recording,
            summary=schemas.LocalMeetingSummaryInDB.model_validate(summary) if summary else None,
            todos=[schemas.LocalMeetingTodoInDB.model_validate(x) for x in todos],
        )

    def list_minutes_sessions(
        self,
        db: Session,
        meeting_id: int,
    ) -> List[schemas.LocalMeetingMinutesSessionInDB]:
        logger.info("查询本地纪要会话列表 meeting_id=%s", meeting_id)
        self._assert_meeting_exists(db, meeting_id)
        sessions = (
            db.query(database.LocalMeetingMinutesSession)
            .filter(database.LocalMeetingMinutesSession.meeting_id == meeting_id)
            .order_by(
                database.LocalMeetingMinutesSession.created_at.asc(),
                database.LocalMeetingMinutesSession.id.asc(),
            )
            .all()
        )
        return [self._build_session_schema(item) for item in sessions]

    def get_minutes_session(
        self,
        db: Session,
        meeting_id: int,
        session_id: int,
    ) -> schemas.LocalMeetingMinutesSessionInDB:
        logger.info("查询本地纪要会话详情 meeting_id=%s session_id=%s", meeting_id, session_id)
        self._assert_meeting_exists(db, meeting_id)
        session = (
            db.query(database.LocalMeetingMinutesSession)
            .filter(
                database.LocalMeetingMinutesSession.id == session_id,
                database.LocalMeetingMinutesSession.meeting_id == meeting_id,
            )
            .first()
        )
        if not session:
            raise ValueError("会话历史不存在")
        return self._build_session_schema(session)

    def update_minutes_session(
        self,
        db: Session,
        meeting_id: int,
        session_id: int,
        payload: schemas.LocalMeetingMinutesSessionUpdate,
    ) -> schemas.LocalMeetingMinutesSessionInDB:
        logger.info("更新本地纪要会话 meeting_id=%s session_id=%s", meeting_id, session_id)
        self._assert_meeting_exists(db, meeting_id)
        session = (
            db.query(database.LocalMeetingMinutesSession)
            .filter(
                database.LocalMeetingMinutesSession.id == session_id,
                database.LocalMeetingMinutesSession.meeting_id == meeting_id,
            )
            .first()
        )
        if not session:
            raise ValueError("会话历史不存在")

        fields_set = payload.model_fields_set
        # 本地快照仅存一稿 stream_transcript_text；transcript_text 为 API 别名，同时传时以 transcript_text 为准。
        if "transcript_text" in fields_set:
            session.stream_transcript_text = payload.transcript_text
        elif "stream_transcript_text" in fields_set:
            session.stream_transcript_text = payload.stream_transcript_text
        if "summary_title" in fields_set:
            session.summary_title = payload.summary_title
        if "summary_paragraph" in fields_set:
            session.summary_paragraph = payload.summary_paragraph
        if "todos" in fields_set:
            todos_payload = [item.model_dump() for item in (payload.todos or [])]
            session.todos_json = json.dumps(todos_payload, ensure_ascii=False)

        if self._is_latest_minutes_session(db, meeting_id, session.id):
            self._apply_latest_session_to_current_minutes(db, meeting_id, session, payload, fields_set)

        db.commit()
        db.refresh(session)
        return self._build_session_schema(session)

    def delete_minutes_session(self, db: Session, meeting_id: int, session_id: int) -> None:
        logger.info("删除本地纪要会话 meeting_id=%s session_id=%s", meeting_id, session_id)
        self._assert_meeting_exists(db, meeting_id)
        session = (
            db.query(database.LocalMeetingMinutesSession)
            .filter(
                database.LocalMeetingMinutesSession.id == session_id,
                database.LocalMeetingMinutesSession.meeting_id == meeting_id,
            )
            .first()
        )
        if not session:
            raise ValueError("会话历史不存在")
        db.delete(session)
        db.commit()

    def delete_meeting_minutes_data(self, db: Session, meeting_id: int) -> None:
        # 会议删除前必须显式清空 local 纪要相关表，避免留下 meeting_id 孤儿数据。
        self._assert_meeting_exists(db, meeting_id)
        logger.info("清理本地纪要关联数据 meeting_id=%s", meeting_id)
        db.query(database.LocalMeetingTodo).filter(
            database.LocalMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingSummary).filter(
            database.LocalMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingMinutesSession).filter(
            database.LocalMeetingMinutesSession.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalAsrSession).filter(
            database.LocalAsrSession.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.commit()

    def _call_llm(
        self,
        meeting_title: str,
        transcript: str,
        duration_seconds: float = 0,
        user_id: Optional[str] = None,
        meeting_id: Optional[int] = None,
    ) -> dict:
        from app.llm_client.generators import get_client

        char_count = len((transcript or "").strip().replace(" ", "").replace("\n", ""))
        instruction = build_local_minutes_llm_instruction(char_count, duration_seconds)
        user_msg = f"会议标题：{meeting_title}\n\n会议转写文本：\n{transcript}"
        try:
            cli = get_client()
            max_tokens = resolve_max_tokens(char_count, duration_seconds)
            raw = cli.chat(
                [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
                user_id=user_id,
                metadata_json=json.dumps({"meeting_id": meeting_id}) if meeting_id else None,
            )
            payload = self._parse_json_with_repair(
                raw=raw,
                max_attempts=3,
                max_tokens=max_tokens,
            )
            return self._normalize_payload(payload, meeting_title)
        except Exception as exc:  # noqa: BLE001
            logger.exception("本地会议纪要 LLM 调用失败 meeting_title=%s", meeting_title)
            raise RuntimeError(f"LLM 生成会议纪要失败: {exc}") from exc

    def _parse_json_with_repair(
        self,
        raw: str,
        max_attempts: int = 3,
        max_tokens: Optional[int] = None,
    ) -> dict:
        current_raw = raw
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                return self._parse_json(current_raw)
            except ValueError as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break

                logger.warning(
                    "LLM 返回 JSON 解析失败，尝试修复格式 attempt=%s/%s error=%s",
                    attempt,
                    max_attempts,
                    exc,
                )
                current_raw = self._repair_llm_json(
                    invalid_content=current_raw,
                    error_message=str(exc),
                    max_tokens=max_tokens,
                )

        raise ValueError(
            f"LLM 返回内容不是合法 JSON，已尝试 {max_attempts} 次仍失败: {last_error}"
        )

    @staticmethod
    def _repair_llm_json(
        invalid_content: str,
        error_message: str,
        max_tokens: Optional[int] = None,
    ) -> str:
        from app.llm_client.generators import get_client

        instruction = (
            "你是 JSON 格式修复器。"
            "下面用户会提供一段不合法的 JSON，它本应是会议纪要生成结果。"
            "请只修复 JSON 语法错误，不要改写原始内容，不要新增字段，不要删除字段，"
            "不要输出解释，不要输出 Markdown 代码块，只返回修复后的合法 JSON。"
        )
        user_msg = (
            f"JSON 解析错误：\n{error_message}\n\n"
            f"待修复内容：\n{invalid_content}"
        )

        cli = get_client()
        repaired = cli.chat(
            [
                {"role": "system", "content": instruction},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        if not repaired:
            raise ValueError("LLM JSON 修复未返回任何内容")
        return repaired

    @staticmethod
    def _parse_json(raw: str) -> dict:
        if not raw:
            raise ValueError("LLM 未返回任何内容")
        cleaned = raw.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(0)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"LLM 返回内容不是合法 JSON: {exc}; 内容片段: {cleaned[:300]}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM 返回内容不是 JSON 对象")
        return payload

    @staticmethod
    def _normalize_payload(payload: dict, meeting_title: str) -> dict:
        summary = payload.get("summary")
        if isinstance(summary, str):
            summary = {"title": meeting_title, "paragraph": summary}
        if not isinstance(summary, dict):
            raise ValueError("LLM 返回缺少 summary 对象")
        paragraph = str(summary.get("paragraph") or summary.get("content") or "").strip()
        if not paragraph:
            raise ValueError("LLM 返回缺少 summary.paragraph")
        title = str(summary.get("title") or "").strip() or meeting_title

        todos: List[dict] = []
        raw_todos = payload.get("todos")
        if isinstance(raw_todos, list):
            for item in raw_todos:
                if isinstance(item, str):
                    item = {"content": item}
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or item.get("description") or "").strip()
                if not content:
                    continue
                executor = item.get("executor") or item.get("owner")
                execution_time = item.get("execution_time") or item.get("deadline")
                todos.append(
                    {
                        "content": content,
                        "executor": executor if isinstance(executor, str) and executor.strip() else None,
                        "execution_time": execution_time
                        if isinstance(execution_time, str) and execution_time.strip()
                        else None,
                    }
                )
        return {"summary": {"title": title, "paragraph": paragraph}, "todos": todos}

    def _create_minutes_session_snapshot(
        self,
        db: Session,
        meeting_id: int,
        source_audio_id: Optional[int],
        stream_transcript_text: Optional[str],
    ) -> database.LocalMeetingMinutesSession:
        # 会话快照用于“可回放”与“可编辑”：每次生成纪要后写入一条稳定版本。
        summary = self._meeting_summary(db, meeting_id)
        todos = self._meeting_todos(db, meeting_id)
        todos_payload = [
            {
                "content": item.content,
                "executor": item.executor,
                "execution_time": item.execution_time,
                "source_audio_id": item.source_audio_id,
            }
            for item in todos
        ]
        session = database.LocalMeetingMinutesSession(
            session_no=self._build_unique_session_no(db, meeting_id),
            meeting_id=meeting_id,
            source_audio_id=source_audio_id,
            stream_transcript_text=stream_transcript_text,
            summary_title=summary.title if summary else None,
            summary_paragraph=summary.paragraph if summary else None,
            todos_json=json.dumps(todos_payload, ensure_ascii=False),
        )
        db.add(session)
        db.flush()
        db.refresh(session)
        return session

    def _build_unique_session_no(self, db: Session, meeting_id: int) -> str:
        cursor = datetime.now(SESSION_NO_TIMEZONE).replace(microsecond=0)
        while True:
            candidate = f"LOCAL-{meeting_id}-{cursor.strftime('%Y%m%d%H%M%S')}"
            exists = (
                db.query(database.LocalMeetingMinutesSession.id)
                .filter(database.LocalMeetingMinutesSession.session_no == candidate)
                .first()
            )
            if not exists:
                return candidate
            cursor += timedelta(seconds=1)

    @staticmethod
    def _session_todos_from_json(raw: Optional[str]) -> List[dict]:
        if raw is None or not str(raw).strip():
            return []
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("历史快照 todos_json 不是合法 JSON") from exc
        if not isinstance(loaded, list):
            raise ValueError("历史快照 todos_json 须为 JSON 数组")
        out: List[dict] = []
        for idx, item in enumerate(loaded):
            if not isinstance(item, dict):
                raise ValueError(f"历史快照 todos_json[{idx}] 须为 JSON 对象")
            out.append(item)
        return out

    def _build_session_schema(
        self,
        item: database.LocalMeetingMinutesSession,
    ) -> schemas.LocalMeetingMinutesSessionInDB:
        todos: List[schemas.LocalSessionTodoItem] = []
        for todo in self._session_todos_from_json(item.todos_json):
            content = str(todo.get("content") or "").strip()
            if not content:
                continue
            ex = todo.get("executor")
            et = todo.get("execution_time")
            sid = todo.get("source_audio_id")
            sid_int: Optional[int] = None
            if isinstance(sid, int):
                sid_int = sid
            elif isinstance(sid, str) and sid.strip().isdigit():
                sid_int = int(sid.strip())
            todos.append(
                schemas.LocalSessionTodoItem(
                    content=content,
                    executor=str(ex).strip() if isinstance(ex, str) and ex.strip() else None,
                    execution_time=str(et).strip() if isinstance(et, str) and et.strip() else None,
                    source_audio_id=sid_int,
                )
            )
        return schemas.LocalMeetingMinutesSessionInDB(
            id=item.id,
            session_no=item.session_no,
            meeting_id=item.meeting_id,
            source_audio_id=item.source_audio_id,
            stream_transcript_text=item.stream_transcript_text,
            transcript_text=item.stream_transcript_text,
            summary_title=item.summary_title,
            summary_paragraph=item.summary_paragraph,
            todos=todos,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _is_latest_minutes_session(self, db: Session, meeting_id: int, session_id: int) -> bool:
        latest = self._latest_minutes_session(db, meeting_id)
        return bool(latest and latest.id == session_id)

    def _apply_latest_session_to_current_minutes(
        self,
        db: Session,
        meeting_id: int,
        session: database.LocalMeetingMinutesSession,
        payload: schemas.LocalMeetingMinutesSessionUpdate,
        fields_set: set[str],
    ) -> None:
        if "stream_transcript_text" in fields_set or "transcript_text" in fields_set:
            asr_session = self._asr_session_for_minutes_snapshot(
                db, meeting_id, session.source_audio_id
            )
            if asr_session:
                if "transcript_text" in fields_set:
                    asr_session.stream_transcript_text = payload.transcript_text
                else:
                    asr_session.stream_transcript_text = payload.stream_transcript_text

        if "summary_title" in fields_set or "summary_paragraph" in fields_set:
            summary = self._meeting_summary(db, meeting_id)
            if summary is None:
                para = (session.summary_paragraph or "").strip()
                if not para:
                    raise ValueError("当前会议尚无摘要记录，请先填写 summary_paragraph 再同步")
                summary = database.LocalMeetingSummary(
                    meeting_id=meeting_id,
                    source_audio_id=session.source_audio_id,
                    title=session.summary_title,
                    paragraph=para,
                )
                db.add(summary)
            else:
                if "summary_title" in fields_set:
                    summary.title = payload.summary_title
                if "summary_paragraph" in fields_set:
                    summary.paragraph = (payload.summary_paragraph or "").strip()
                    if not summary.paragraph:
                        raise ValueError("summary_paragraph 不能为空")

        if "todos" in fields_set:
            db.query(database.LocalMeetingTodo).filter(
                database.LocalMeetingTodo.meeting_id == meeting_id
            ).delete(synchronize_session=False)
            for item in payload.todos or []:
                db.add(
                    database.LocalMeetingTodo(
                        meeting_id=meeting_id,
                        source_audio_id=item.source_audio_id or session.source_audio_id,
                        content=item.content,
                        executor=item.executor,
                        execution_time=item.execution_time,
                    )
                )


class LiveLocalAsrHandler:
    """本地实时 ASR 处理器。

    QWEN_ASR_LIVE_FORCE_HTTP_CHUNK 为真时对齐 qwen_asr_smoketest_incremental_merge.py：
    按 QWEN_ASR_CHUNK_SEC / QWEN_ASR_OVERLAP_SEC 滑窗（step=chunk-overlap），每段 wav → audio_url → merge_pair，
    WS 推送 delta 或 boundary_corrected 全量。为假时走 Qwen 实时 WS。结束写整轨 WAV 上传。
    """

    def __init__(
        self,
        websocket,
        db: Session,
        meeting_id: int,
        service: LocalMeetingMinuteService,
        creator_id: Optional[str] = None,
    ):
        self._ws = websocket
        self._db = db
        self._meeting_id = meeting_id
        self._service = service
        self._creator_id = creator_id
        self._audio_chunks: List[bytes] = []
        self._final_parts: List[str] = []
        self._session_id: Optional[int] = None
        self._recording_session_id: Optional[str] = None
        self._sample_rate = 16000
        self._channels = 1
        self._sample_width = 2
        self._ws_alive = True
        self._client_disconnected = False
        self._recover_finalize_requested = False
        # HTTP 滑窗分段时 merge_pair 的累计稿，异常收尾时写入 DB 便于排障
        self._live_http_merged: str = ""

    async def _safe_send_json(self, payload: dict) -> bool:
        if not self._ws_alive:
            return False
        try:
            await self._ws.send_json(payload)
            return True
        except (WebSocketDisconnect, ClientDisconnected, ConnectionClosed):
            self._ws_alive = False
            logger.info(
                "本地实时 ASR 前端 WebSocket 已断开，停止推送 meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
            )
            return False

    async def request_recover_finalize(self) -> None:
        """由 HTTP 恢复入口主动关闭当前实时 WS，让 run() 尽快落盘。"""
        self._recover_finalize_requested = True
        self._client_disconnected = False
        try:
            await self._ws.close(code=1000, reason="recover_finalize")
        except Exception:
            logger.warning(
                "主动关闭本地实时 WS 失败 meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
                exc_info=True,
            )

    async def run(self) -> None:
        # `run` 是实时会话总入口，负责创建 ASR session、启动双向转发、并在结束时统一收尾。
        self._service._assert_meeting_exists(self._db, self._meeting_id)
        await self._ws.accept()

        asr_session = database.LocalAsrSession(
            meeting_id=self._meeting_id,
            status="processing",
        )
        self._db.add(asr_session)
        self._db.commit()
        self._db.refresh(asr_session)
        self._session_id = asr_session.id
        await self._safe_send_json({"type": "session_created", "session_id": self._session_id})

        try:
            logger.info("开始本地实时 ASR 会话 meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
            if settings.QWEN_ASR_LIVE_FORCE_HTTP_CHUNK:
                await self._run_live_incremental_http()
            else:
                ws_url = _build_qwen_ws_url()
                headers = _build_qwen_ws_headers()
                stop_event = asyncio.Event()

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url, headers=headers) as qwen_ws:
                        await qwen_ws.send_str(_session_update_event())
                        send_task = asyncio.create_task(self._forward_audio(qwen_ws, stop_event))
                        recv_task = asyncio.create_task(self._recv_asr_result(qwen_ws, stop_event))
                        await asyncio.wait([send_task, recv_task], return_when=asyncio.ALL_COMPLETED)

                await self._finalize_from_ws_transcript()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Local live ASR run failed meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
            transcript = (self._live_http_merged or "").strip() or "".join(
                self._final_parts
            )

            if self._audio_chunks and self._recording_session_id:
                await self._save_audio_part_for_later(
                    transcript=transcript,
                    error_msg=str(exc),
                )
                self._service.schedule_delayed_finalize(
                    meeting_id=self._meeting_id,
                    recording_session_id=self._recording_session_id,
                )
                await self._safe_send_json(
                    {
                        "type": "session_saved",
                        "session_id": self._session_id,
                        "recording_session_id": self._recording_session_id,
                    }
                )
                return

            asr_session.status = "failed"
            asr_session.stream_transcript_text = transcript
            asr_session.error_msg = str(exc)
            self._db.commit()
            await self._safe_send_json({"type": "error", "message": str(exc)})
            raise
        finally:
            if self._recording_session_id:
                self._service.unregister_live_handler(
                    meeting_id=self._meeting_id,
                    recording_session_id=self._recording_session_id,
                    handler=self,
                )

    async def _forward_audio(self, qwen_ws, stop_event: asyncio.Event) -> None:
        # 前端到 Qwen 的上行链路：控制消息负责 stop/config，二进制消息负责真正音频内容。
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(
                    self._ws.receive(),
                    timeout=settings.VOLC_CLIENT_WS_IDLE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "本地实时 ASR 前端 WS 空闲超时 meeting_id=%s session_id=%s timeout=%ss",
                    self._meeting_id,
                    self._session_id,
                    settings.VOLC_CLIENT_WS_IDLE_TIMEOUT_SECONDS,
                )
                self._ws_alive = False
                self._client_disconnected = True
                stop_event.set()
                break

            msg_type = raw.get("type", "")
            if msg_type in {"websocket.disconnect", "websocket.close"}:
                self._ws_alive = False
                self._client_disconnected = (
                    not self._recover_finalize_requested
                )
                stop_event.set()
                break
            if raw.get("text"):
                ctrl = json.loads(raw["text"])
                action = ctrl.get("action")
                if action == "stop":
                    stop_event.set()
                    break
                if action == "config":
                    self._sample_rate = int(ctrl.get("rate", self._sample_rate))
                    self._channels = int(ctrl.get("channels", self._channels))
                    rsid = ctrl.get("recording_session_id")
                    if isinstance(rsid, str) and rsid.strip():
                        self._recording_session_id = rsid.strip()
                        asr_session = (
                            self._db.query(database.LocalAsrSession)
                            .filter(database.LocalAsrSession.id == self._session_id)
                            .first()
                        )
                        if asr_session:
                            asr_session.recording_session_id = self._recording_session_id
                            self._db.commit()
                        self._service.register_live_handler(
                            meeting_id=self._meeting_id,
                            recording_session_id=self._recording_session_id,
                            handler=self,
                        )
                        self._service.cancel_delayed_finalize(
                            meeting_id=self._meeting_id,
                            recording_session_id=self._recording_session_id,
                        )
                continue
            if raw.get("bytes"):
                chunk = raw["bytes"]
                self._audio_chunks.append(chunk)
                await qwen_ws.send_str(_audio_append_event(chunk))

        await qwen_ws.send_str(_audio_commit_event())
        await qwen_ws.send_str(_session_finish_event())

    async def _recv_asr_result(self, qwen_ws, stop_event: asyncio.Event) -> None:
        # Qwen 到前端的下行链路：把 partial/final 文本统一转换成前端约定的 JSON 事件。
        async for msg in qwen_ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(msg.data)
            event_type = payload.get("type", "")
            if _is_partial_transcription_event(event_type):
                raw_text = _extract_transcription_text(payload)
                text = raw_text.strip() if isinstance(raw_text, str) else ""
                if text:
                    await self._safe_send_json(
                        {
                            "type": "partial",
                            "text": text,
                            "accumulated": "".join(self._final_parts) + text,
                        }
                    )
            elif _is_final_transcription_event(event_type):
                raw_text = _extract_transcription_text(payload)
                text = raw_text.strip() if isinstance(raw_text, str) else ""
                if text:
                    self._final_parts.append(text)
                if text:
                    await self._safe_send_json(
                        {"type": "final", "text": text, "accumulated": "".join(self._final_parts)}
                    )
            elif event_type == "session.finished":
                break
        stop_event.set()

    def _materialize_wav_path(self) -> tuple[Path, float]:
        if not self._audio_chunks:
            raise RuntimeError("未接收到任何音频数据，无法生成录音文件")
        wav_dir = Path(
            settings.QWEN_ASR_AUDIO_SAVE_DIR or os.path.join(settings.UPLOAD_DIR, "local_asr_recordings")
        )
        wav_dir.mkdir(parents=True, exist_ok=True)
        if self._recording_session_id:
            wav_path = (
                wav_dir
                / f"meeting_{self._meeting_id}_recording_{self._recording_session_id}_part_{self._session_id}.wav"
            )
        else:
            wav_path = wav_dir / f"meeting_{self._meeting_id}_session_{self._session_id}.wav"
        logger.info(
            "开始落盘本地实时录音 meeting_id=%s session_id=%s recording_session_id=%s wav_path=%s",
            self._meeting_id,
            self._session_id,
            self._recording_session_id,
            wav_path,
        )
        duration = _save_pcm_as_wav(
            self._audio_chunks,
            wav_path,
            sample_rate=self._sample_rate,
            channels=self._channels,
            sample_width=self._sample_width,
        )
        return wav_path, duration

    async def _save_audio_part_for_later(
        self,
        transcript: str,
        error_msg: Optional[str] = None,
    ) -> None:
        """保存当前 WS 会话已有音频片段，等待同一 recording_session_id 最终合并。"""
        if not self._session_id:
            return
        if not self._audio_chunks:
            logger.warning(
                "本地实时 ASR 没有可保存的音频片段 meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
            )
            return

        wav_path, duration = self._materialize_wav_path()
        asr_session = (
            self._db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.id == self._session_id)
            .first()
        )
        if not asr_session:
            return

        asr_session.recording_session_id = self._recording_session_id
        asr_session.audio_part_path = str(wav_path)
        asr_session.stream_transcript_text = transcript
        asr_session.duration_seconds = duration
        asr_session.error_msg = error_msg

        if self._recording_session_id:
            asr_session.status = "completed"
            self._db.commit()
            logger.info(
                "本地实时 ASR 片段已保存 meeting_id=%s session_id=%s recording_session_id=%s wav_path=%s duration=%.3f",
                self._meeting_id,
                self._session_id,
                self._recording_session_id,
                wav_path,
                duration,
            )
            return

        self._db.commit()

    async def _finalize_common(self, transcript: str, duration: float, wav_path: Path) -> None:
        """先落转写与时长；有 recording_session_id 时只保存片段，最终统一合并。"""
        asr_session = (
            self._db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.id == self._session_id)
            .first()
        )
        if not asr_session:
            raise RuntimeError("ASR 会话不存在")
        asr_session.recording_session_id = self._recording_session_id
        asr_session.stream_transcript_text = transcript
        asr_session.duration_seconds = duration
        asr_session.audio_part_path = str(wav_path)
        self._db.commit()

        await self._safe_send_json({"type": "saving_audio", "session_id": self._session_id})

        if self._recording_session_id:
            asr_session.status = "completed"
            asr_session.error_msg = None
            self._db.commit()
            logger.info(
                "本地实时 ASR 片段完成 meeting_id=%s session_id=%s recording_session_id=%s duration=%.3f",
                self._meeting_id,
                self._session_id,
                self._recording_session_id,
                duration,
            )

            if self._client_disconnected:
                self._service.schedule_delayed_finalize(
                    meeting_id=self._meeting_id,
                    recording_session_id=self._recording_session_id,
                )

            await self._safe_send_json(
                {
                    "type": "completed",
                    "session_id": self._session_id,
                    "transcript": transcript,
                    "stream_transcript_text": transcript,
                    "audio_uploaded": False,
                    "duration_seconds": duration,
                }
            )
            return

        try:
            audio_record = meeting_audio_service.create_audio_from_path(
                db=self._db,
                meeting_id=self._meeting_id,
                provider="local",
                creator_id=self._creator_id,
                source_path=wav_path,
                file_name=f"live_{self._session_id}.wav",
                content_type="audio/wav",
            )
        except Exception as upload_exc:  # noqa: BLE001
            logger.exception(
                "本地实时录音上传失败 meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
            )
            asr_session.status = "failed"
            asr_session.error_msg = str(upload_exc)
            self._db.commit()
            raise
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("本地实时录音临时 WAV 清理失败 path=%s", wav_path)

        asr_session.status = "completed"
        asr_session.source_audio_id = audio_record.id
        asr_session.error_msg = None
        self._db.commit()
        logger.info(
            "本地实时 ASR 会话完成 meeting_id=%s session_id=%s audio_id=%s duration=%.3f",
            self._meeting_id,
            self._session_id,
            audio_record.id,
            duration,
        )

        await self._safe_send_json(
            {
                "type": "completed",
                "session_id": self._session_id,
                "audio_id": audio_record.id,
                "transcript": transcript,
                "stream_transcript_text": transcript,
                "audio_uploaded": True,
                "duration_seconds": duration,
            }
        )

    async def _finalize_from_ws_transcript(self) -> None:
        transcript = "".join(self._final_parts)
        wav_path, pcm_duration = self._materialize_wav_path()
        
        await self._finalize_common(transcript, pcm_duration, wav_path)

    async def _push_incremental_merge_ws(self, old_merged: str, merged: str, chunk_idx: int) -> None:
        """对齐 smoketest incremental_transcribe_and_merge 269-276：追加 delta 或边界修正后全量 accumulated。"""
        if not self._ws_alive:
            return
        if merged.startswith(old_merged):
            delta = merged[len(old_merged) :]
            await self._safe_send_json(
                {
                    "type": "partial",
                    "text": delta,
                    "accumulated": merged,
                    "chunk_idx": chunk_idx,
                }
            )
        else:
            await self._safe_send_json(
                {
                    "type": "partial",
                    "accumulated": merged,
                    "chunk_idx": chunk_idx,
                    "boundary_corrected": True,
                }
            )

    async def _run_live_incremental_http(self) -> None:
        """实时 HTTP 滑窗：与 qwen_asr_smoketest_incremental_merge 同源的分段请求 + merge_pair。"""
        validate_incremental_http_config()
        public_base = get_incremental_http_public_base()
        chat_url, model, headers, timeout, max_tokens = asr_http_runtime_params()

        chunk_sec = float(settings.QWEN_ASR_CHUNK_SEC)
        overlap_sec = float(settings.QWEN_ASR_OVERLAP_SEC)
        step_sec = chunk_sec - overlap_sec
        if step_sec <= 0:
            raise ValueError("QWEN_ASR_CHUNK_SEC 必须大于 QWEN_ASR_OVERLAP_SEC")

        frame = self._channels * self._sample_width
        bytes_per_sec = self._sample_rate * frame
        chunk_bytes = max(1, int(chunk_sec * bytes_per_sec))
        step_bytes = max(1, int(step_sec * bytes_per_sec))
        min_tail_bytes = max(frame, int(0.5 * bytes_per_sec))

        url_path_prefix = uuid.uuid4().hex
        chunks_dir: Optional[Path] = None
        try:
            serve_parent = await asyncio.to_thread(ensure_incremental_http_serve_root)
            chunks_dir = serve_parent / url_path_prefix
            chunks_dir.mkdir(parents=True, exist_ok=True)

            req_session = build_asr_requests_session()
            pcm_total = bytearray()
            cursor = 0
            seg_idx = 0
            merged_text = ""

            async def flush_full_chunks() -> None:
                nonlocal cursor, seg_idx, merged_text
                while len(pcm_total) - cursor >= chunk_bytes:
                    segment = bytes(pcm_total[cursor : cursor + chunk_bytes])
                    fname = f"chunk_{seg_idx:04d}.wav"
                    fpath = chunks_dir / fname
                    await asyncio.to_thread(
                        write_pcm_as_wav_file,
                        fpath,
                        segment,
                        sample_rate=self._sample_rate,
                        channels=self._channels,
                        sample_width=self._sample_width,
                    )

                    def _post_one() -> str:
                        try:
                            return post_served_wav_chunk(
                                req_session,
                                chat_url,
                                model,
                                headers,
                                public_base,
                                url_path_prefix,
                                fname,
                                timeout,
                                max_tokens,
                            )
                        except Exception as post_exc:  # noqa: BLE001
                            # 与 transcribe_audio_file_incremental 一致：单段失败记日志并空串，由 merge_pair 承接
                            logger.warning(
                                "实时 HTTP 分段 ASR 失败 session_id=%s idx=%s: %s",
                                self._session_id,
                                seg_idx,
                                post_exc,
                            )
                            return ""

                    curr = await asyncio.to_thread(_post_one)
                    old_m = merged_text
                    merged_text, _info = merge_pair(merged_text, curr)
                    self._live_http_merged = merged_text
                    await self._push_incremental_merge_ws(old_m, merged_text, seg_idx)
                    cursor += step_bytes
                    seg_idx += 1

            await self._safe_send_json(
                {
                    "type": "transcribing",
                    "session_id": self._session_id,
                    "mode": "http_chunk_incremental",
                }
            )

            while True:
                try:
                    raw = await asyncio.wait_for(
                        self._ws.receive(),
                        timeout=settings.VOLC_CLIENT_WS_IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "本地 HTTP 实时 ASR 前端 WS 空闲超时 meeting_id=%s session_id=%s timeout=%ss",
                        self._meeting_id,
                        self._session_id,
                        settings.VOLC_CLIENT_WS_IDLE_TIMEOUT_SECONDS,
                    )
                    self._ws_alive = False
                    self._client_disconnected = True
                    break

                msg_type = raw.get("type", "")
                if msg_type in {"websocket.disconnect", "websocket.close"}:
                    self._ws_alive = False
                    self._client_disconnected = (
                        not self._recover_finalize_requested
                    )
                    break
                if raw.get("text"):
                    ctrl = json.loads(raw["text"])
                    action = ctrl.get("action")
                    if action == "stop":
                        break
                    if action == "config":
                        self._sample_rate = int(ctrl.get("rate", self._sample_rate))
                        self._channels = int(ctrl.get("channels", self._channels))
                        rsid = ctrl.get("recording_session_id")
                        if isinstance(rsid, str) and rsid.strip():
                            self._recording_session_id = rsid.strip()
                            asr_session = (
                                self._db.query(database.LocalAsrSession)
                                .filter(database.LocalAsrSession.id == self._session_id)
                                .first()
                            )
                            if asr_session:
                                asr_session.recording_session_id = self._recording_session_id
                                self._db.commit()
                            self._service.register_live_handler(
                                meeting_id=self._meeting_id,
                                recording_session_id=self._recording_session_id,
                                handler=self,
                            )
                            self._service.cancel_delayed_finalize(
                                meeting_id=self._meeting_id,
                                recording_session_id=self._recording_session_id,
                            )
                        # 与前端约定：config 应在首包 PCM 之前；若晚到则仅影响后续滑窗尺寸
                        frame = self._channels * self._sample_width
                        bytes_per_sec = self._sample_rate * frame
                        chunk_bytes = max(1, int(chunk_sec * bytes_per_sec))
                        step_bytes = max(1, int(step_sec * bytes_per_sec))
                        min_tail_bytes = max(frame, int(0.5 * bytes_per_sec))
                    continue
                if raw.get("bytes"):
                    pcm_total.extend(raw["bytes"])
                    await flush_full_chunks()

            await flush_full_chunks()

            remainder = len(pcm_total) - cursor
            if remainder >= min_tail_bytes:
                segment = bytes(pcm_total[cursor:])
                fname = f"chunk_{seg_idx:04d}.wav"
                fpath = chunks_dir / fname
                await asyncio.to_thread(
                    write_pcm_as_wav_file,
                    fpath,
                    segment,
                    sample_rate=self._sample_rate,
                    channels=self._channels,
                    sample_width=self._sample_width,
                )

                def _post_tail() -> str:
                    try:
                        return post_served_wav_chunk(
                            req_session,
                            chat_url,
                            model,
                            headers,
                            public_base,
                            url_path_prefix,
                            fname,
                            timeout,
                            max_tokens,
                        )
                    except Exception as post_exc:  # noqa: BLE001
                        logger.warning(
                            "实时 HTTP 尾段 ASR 失败 session_id=%s: %s",
                            self._session_id,
                            post_exc,
                        )
                        return ""

                curr = await asyncio.to_thread(_post_tail)
                old_m = merged_text
                merged_text, _info = merge_pair(merged_text, curr)
                self._live_http_merged = merged_text
                await self._push_incremental_merge_ws(old_m, merged_text, seg_idx)

            if not pcm_total:
                raise RuntimeError("未接收到任何音频数据，无法生成录音文件")

            self._audio_chunks = [bytes(pcm_total)]
            wav_path, pcm_duration = self._materialize_wav_path()
            try:
                from app.services.token_tracker import token_tracker
                creator_id = self._db.execute(
                    text("SELECT creator_id FROM meetings WHERE id = :id"),
                    {"id": self._meeting_id},
                ).scalar()
                token_tracker.record(
                    user_id=creator_id,
                    api_category="qwen_asr",
                    api_endpoint=chat_url or "qwen_asr_http",
                    total_tokens=0,
                    request_chars=len(merged_text or ""),
                    duration_ms=int(pcm_duration * 1000) if pcm_duration else 0,
                    status="success",
                    metadata_json=json.dumps({
                        "meeting_id": self._meeting_id,
                        "session_id": self._session_id,
                    }),
                )
            except Exception:
                pass
            await self._finalize_common(merged_text, pcm_duration, wav_path)
        finally:
            if chunks_dir is not None:
                shutil.rmtree(chunks_dir, ignore_errors=True)


local_meeting_minute_service = LocalMeetingMinuteService()
