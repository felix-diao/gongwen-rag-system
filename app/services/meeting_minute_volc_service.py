"""火山会议纪要服务。

核心能力：
1. 建立火山实时 ASR WebSocket，获取录音时的流式转写。
2. 录音结束后把音频上传到统一音频表。
3. 把音频提交给火山语音妙记离线任务，并轮询精准转写、摘要、待办结果。
4. 写入当前纪要视图与历史快照，并通过会议级 WebSocket 广播状态。

设计约束：
1. 火山链路天然分成“实时粗转写”和“离线精准纪要”两段，因此字段和状态会比 local 模式更复杂。
2. 轮询线程、结果归一化、历史快照构建都收敛在这一个 service 中，避免 API 层出现状态机逻辑。
3. 对第三方响应格式采用“显式支持 + 明确报错”的策略，不写模糊兜底分支。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import struct
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp
import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import database, schemas
from app.services.meeting_audio_service import meeting_audio_service
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected
from websockets.exceptions import ConnectionClosed

logger = get_logger("meeting_volc_minutes_service")


ASR_WS_URL = f"wss://openspeech.bytedance.com{settings.VOLC_ASR_WS_PATH}"
MINUTES_RUNNING_STATUS = {"queued", "running", "processing"}
MINUTES_SUCCESS_STATUS = {"success", "succeeded", "successed", "finished", "completed", "done"}
MINUTES_FAILED_STATUS = {"failed", "error"}
MINUTES_CANCELLED_STATUS = {"cancelled", "canceled"}
MINUTES_CANCELABLE_STATUS = {"submitted", *MINUTES_RUNNING_STATUS}
SESSION_NO_TIMEZONE = timezone(timedelta(hours=8))
EMPTY_SUMMARY_HINT = "摘要为空，可能是录音内容较短或有效信息不足，暂未生成摘要。"
_VOLC_EMPTY_TRANSCRIPT_TITLE = "无有效发言内容"
_VOLC_EMPTY_TRANSCRIPT_HINT = "本次录音未识别到有效发言内容"
_VOLC_EMPTY_TRANSCRIPT_ERROR_KEYWORDS = (
    "audio empty",
    "empty audio",
    "empty file",
    "audio is empty",
    "empty transcript",
    "empty transcription",
    "no speech",
    "no valid speech",
    "silent",
    "silence",
    "无有效语音",
    "无有效发言",
    "未识别到",
    "转写为空",
    "音频为空",
    "静音",
)
class ProtocolVersion:
    V1 = 1


class MessageType:
    CLIENT_FULL_REQUEST = 1
    CLIENT_AUDIO_ONLY_REQUEST = 2
    SERVER_FULL_RESPONSE = 9
    SERVER_ERROR_RESPONSE = 15


class MessageTypeSpecificFlags:
    NO_SEQUENCE = 0
    POS_SEQUENCE = 1
    NEG_SEQUENCE = 2
    NEG_WITH_SEQUENCE = 3


class SerializationType:
    NO_SERIALIZATION = 0
    JSON = 1


class CompressionType:
    GZIP = 1


class _AsrResponse:
    def __init__(self) -> None:
        self.message_type = 0
        self.code = 0
        self.event = 0
        self.is_last_package = False
        self.payload_sequence = 0
        self.payload_size = 0
        self.payload_msg: Optional[dict] = None


class ResponseParser:
    @staticmethod
    def parse_response(msg: bytes) -> _AsrResponse:
        response = _AsrResponse()
        header_size = msg[0] & 0x0F
        message_type = msg[1] >> 4
        response.message_type = message_type
        flags = msg[1] & 0x0F
        serialization_method = msg[2] >> 4
        compression_type = msg[2] & 0x0F
        payload = msg[header_size * 4 :]

        if flags & 0x01:
            response.payload_sequence = struct.unpack(">i", payload[:4])[0]
            payload = payload[4:]
        if flags & 0x02:
            response.is_last_package = True
        if flags & 0x04:
            response.event = struct.unpack(">i", payload[:4])[0]
            payload = payload[4:]

        if message_type == MessageType.SERVER_FULL_RESPONSE:
            response.payload_size = struct.unpack(">I", payload[:4])[0]
            payload = payload[4:]
        elif message_type == MessageType.SERVER_ERROR_RESPONSE:
            response.code = struct.unpack(">i", payload[:4])[0]
            response.payload_size = struct.unpack(">I", payload[4:8])[0]
            payload = payload[8:]

        if not payload:
            return response

        if compression_type == CompressionType.GZIP:
            try:
                payload = gzip.decompress(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("火山 ASR 响应解压失败: %s", exc)
                return response

        if serialization_method == SerializationType.JSON:
            try:
                parsed = json.loads(payload.decode("utf-8"))
                if isinstance(parsed, dict):
                    response.payload_msg = parsed
            except Exception as exc:  # noqa: BLE001
                logger.error("火山 ASR 响应解析失败: %s", exc)
        return response


def _guess_file_type(content_type: Optional[str]) -> str:
    """根据 MIME 类型推断妙记接口需要的 FileType。"""
    if not content_type:
        raise ValueError("音频 MIME 类型缺失")
    if content_type.startswith("video"):
        return "video"
    if content_type.startswith("audio"):
        return "audio"
    raise ValueError(f"不支持的 MIME 类型: {content_type}")


def _extract_text(payload: Optional[dict]) -> Optional[str]:
    """从火山实时 ASR 回包中提取文本字段。

    第三方返回结构存在多个变体，这里只显式支持当前已验证过的几种形式。
    """
    if not payload:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        if isinstance(result.get("text"), str):
            return result["text"] or None
        alternatives = result.get("alternatives")
        if isinstance(alternatives, list) and alternatives and isinstance(alternatives[0], dict):
            return alternatives[0].get("transcript") or alternatives[0].get("text")
    if isinstance(payload.get("text"), str):
        return payload["text"] or None
    return None


def _format_speaker(speaker: Any) -> Optional[str]:
    """把火山返回的 speaker 对象/字符串/整数格式化成展示名。"""
    if isinstance(speaker, dict):
        return speaker.get("name") or speaker.get("id") or None
    if isinstance(speaker, (str, int)) and str(speaker):
        return str(speaker)
    return None


def _extract_speaker(payload: Optional[dict]) -> Optional[str]:
    """从火山实时 ASR 回包中提取说话人标识。

    说话人信息可能在 result.speaker、result.utterances[].speaker 或
    result.utterances[].additions.speaker_id 中。
    """
    if not payload:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    if "speaker" in result:
        return _format_speaker(result["speaker"])
    utterances = result.get("utterances")
    if isinstance(utterances, list):
        for u in utterances:
            if not isinstance(u, dict):
                continue
            # 火山 ASR 2.0 说话人分离通常放在 additions.speaker_id
            additions = u.get("additions")
            if isinstance(additions, dict):
                sid = additions.get("speaker_id")
                if sid is not None:
                    return _format_speaker(sid)
            for key in ("speaker", "speaker_id", "spk", "spk_id"):
                if key in u:
                    return _format_speaker(u[key])
    # 顶层 payload fallback
    for key in ("speaker", "speaker_id", "spk", "spk_id"):
        if key in payload:
            return _format_speaker(payload[key])
    return None


def _require_text_field(item: dict, field_group: tuple[str, ...], scope: str) -> str:
    """从候选字段组中取第一个非空文本字段，否则直接抛错。"""
    for key in field_group:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise KeyError(f"{scope} 缺少可用文本字段: {field_group}")


def _save_pcm_as_wav(
    pcm_chunks: List[bytes],
    dest_path: Path,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> float:
    # 步骤说明：拼接 PCM 分片 -> 写 WAV 头与数据 -> 计算并返回时长。
    pcm_data = b"".join(pcm_chunks)
    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    frame_count = len(pcm_data) // (channels * sample_width)
    return frame_count / sample_rate


def _merge_wav_files(part_paths: List[Path], dest_path: Path) -> None:
    """把多个同格式 WAV 文件按顺序合并成一个。

    假设所有文件都是 PCM_S16LE、单声道、16kHz。
    """
    if not part_paths:
        raise ValueError("没有可合并的 WAV 片段")

    params: Optional[tuple] = None
    frames: List[bytes] = []
    for p in part_paths:
        with wave.open(str(p), "rb") as wf:
            if params is None:
                params = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
            frames.append(wf.readframes(wf.getnframes()))

    if params is None:
        raise ValueError("无法读取 WAV 参数")

    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(params[0])
        wf.setsampwidth(params[1])
        wf.setframerate(params[2])
        wf.writeframes(b"".join(frames))


class _VolcMinutesApi:
    # 说明：封装妙记提交与查询，隔离 HTTP 协议细节。

    def __init__(self) -> None:
        self._base = (settings.VOLC_MINUTES_API_BASE or "").rstrip("/")
        self._submit_path = settings.VOLC_MINUTES_SUBMIT_PATH or ""
        self._query_path = settings.VOLC_MINUTES_QUERY_PATH or ""
        self._timeout = settings.VOLC_MINUTES_TIMEOUT
        self._app_key = settings.VOLC_MINUTES_APP_KEY
        self._access_key = settings.VOLC_MINUTES_ACCESS_KEY
        self._resource_id = settings.VOLC_MINUTES_RESOURCE_ID
        if not self._base or not self._submit_path or not self._query_path:
            raise ValueError("VOLC_MINUTES_API_BASE / VOLC_MINUTES_SUBMIT_PATH / VOLC_MINUTES_QUERY_PATH 未配置")
        if not self._timeout:
            raise ValueError("VOLC_MINUTES_TIMEOUT 未配置")
        if not self._app_key or not self._access_key or not self._resource_id:
            raise ValueError("VOLC_MINUTES_APP_KEY / VOLC_MINUTES_ACCESS_KEY / VOLC_MINUTES_RESOURCE_ID 未配置")
        self._session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        # 每次请求都生成新的 request id，便于后端日志与第三方请求链路对齐。
        return {
            "Content-Type": "application/json",
            "X-Api-App-Key": self._app_key,
            "X-Api-Access-Key": self._access_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }

    def submit(self, file_url: str, file_type: Optional[str]) -> str:
        if not settings.VOLC_MINUTES_SOURCE_LANG:
            raise ValueError("VOLC_MINUTES_SOURCE_LANG 未配置")
        if settings.VOLC_MINUTES_NUMBER_OF_SPEAKERS is None:
            raise ValueError("VOLC_MINUTES_NUMBER_OF_SPEAKERS 未配置")
        if not settings.VOLC_MINUTES_INFORMATION_EXTRACTION_TYPES:
            raise ValueError("VOLC_MINUTES_INFORMATION_EXTRACTION_TYPES 未配置")
        if not settings.VOLC_MINUTES_SUMMARIZATION_TYPES:
            raise ValueError("VOLC_MINUTES_SUMMARIZATION_TYPES 未配置")
        payload = {
            "Input": {
                "Offline": {
                    "FileURL": file_url,
                    "FileType": _guess_file_type(file_type),
                }
            },
            "Params": {
                "AllActivate": True,
                "SourceLang": settings.VOLC_MINUTES_SOURCE_LANG,
                "AudioTranscriptionEnable": True,
                "AudioTranscriptionParams": {
                    "SpeakerIdentification": bool(settings.VOLC_MINUTES_SPEAKER_IDENTIFICATION),
                    "NumberOfSpeaker": int(settings.VOLC_MINUTES_NUMBER_OF_SPEAKERS),
                    "NeedWordTimeSeries": bool(settings.VOLC_MINUTES_NEED_WORD_TS),
                },
                "InformationExtractionEnabled": True,
                "InformationExtractionParams": {
                    "Types": settings.VOLC_MINUTES_INFORMATION_EXTRACTION_TYPES,
                },
                "SummarizationEnabled": True,
                "SummarizationParams": {
                    "Types": settings.VOLC_MINUTES_SUMMARIZATION_TYPES,
                },
            },
        }
        resp = self._session.post(
            f"{self._base}{self._submit_path}",
            json=payload,
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        task_id = str(body["Data"]["TaskID"])
        return task_id

    def query(self, task_id: str) -> Dict[str, Any]:
        resp = self._session.post(
            f"{self._base}{self._query_path}",
            json={"TaskID": task_id},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise TypeError("妙记查询响应格式非法")
        return body


class VolcMeetingMinuteService:
    # 设计约束：
    # 1) 控制器层只做协议转换，核心业务在本层；
    # 2) 妙记状态仅落在 VolcMinutesJob；MeetingAudio.status 只表示音频文件侧状态（如 uploaded）。
    # 3) 避免静默兜底，异常要么上抛要么记录明确状态。

    def __init__(self) -> None:
        self._minutes_api = _VolcMinutesApi()
        self._poll_stop: Dict[int, threading.Event] = {}
        self._poll_lock = threading.Lock()

        # 前端 WS 异常断开后，等待用户重连的延迟收尾任务。
        # key = (meeting_id, recording_session_id)
        self._pending_finalize_tasks: Dict[
            Tuple[int, str],
            asyncio.Task,
        ] = {}

        # 当前仍在内存中的实时录音 handler。
        # 用户回到列表/详情时，可通过 recover-and-finalize 主动要求它落盘。
        self._active_live_handlers: Dict[
            Tuple[int, str],
            "LiveVolcAsrHandler",
        ] = {}

        # 防止同一次录音被多个入口同时触发收尾。
        self._finalize_locks: Dict[
            Tuple[int, str],
            asyncio.Lock,
        ] = {}

    def _assert_meeting_exists(self, db: Session, meeting_id: int) -> None:
        exists = db.execute(
            text("SELECT 1 FROM meetings WHERE id = :meeting_id LIMIT 1"),
            {"meeting_id": meeting_id},
        ).first()
        if not exists:
            raise ValueError("会议不存在")

    def _volc_audio_query(self, db: Session):
        return db.query(database.MeetingAudio).filter(database.MeetingAudio.provider == "volc")

    def _latest_volc_audio(self, db: Session, meeting_id: int) -> Optional[database.MeetingAudio]:
        return (
            self._volc_audio_query(db)
            .filter(database.MeetingAudio.meeting_id == meeting_id)
            .order_by(database.MeetingAudio.updated_at.desc(), database.MeetingAudio.id.desc())
            .first()
        )

    @staticmethod
    def _latest_minutes_job(db: Session, meeting_id: int) -> Optional[database.VolcMinutesJob]:
        return (
            db.query(database.VolcMinutesJob)
            .filter(database.VolcMinutesJob.meeting_id == meeting_id)
            .order_by(database.VolcMinutesJob.updated_at.desc(), database.VolcMinutesJob.id.desc())
            .first()
        )

    @staticmethod
    def _latest_asr_session(db: Session, meeting_id: int) -> Optional[database.VolcAsrSession]:
        return (
            db.query(database.VolcAsrSession)
            .filter(database.VolcAsrSession.meeting_id == meeting_id)
            .order_by(database.VolcAsrSession.updated_at.desc(), database.VolcAsrSession.id.desc())
            .first()
        )

    def _recoverable_recording_info(
        self,
        db: Session,
        meeting_id: int,
        summary: Optional[database.VolcMeetingSummary],
    ) -> Optional[schemas.RecoverableRecordingInfo]:
        if summary:
            return None

        sessions = (
            db.query(database.VolcAsrSession)
            .filter(
                database.VolcAsrSession.meeting_id == meeting_id,
                database.VolcAsrSession.recording_session_id.isnot(None),
            )
            .order_by(
                database.VolcAsrSession.updated_at.desc(),
                database.VolcAsrSession.id.desc(),
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

            if has_processing or has_audio_part:
                return schemas.RecoverableRecordingInfo(
                    provider="volc",
                    recording_session_id=recording_session_id,
                    asr_session_id=session.id,
                    status="active" if has_processing else "saved_part",
                    has_audio_part=has_audio_part,
                )

        return None

    def register_live_handler(
        self,
        meeting_id: int,
        recording_session_id: str,
        handler: "LiveVolcAsrHandler",
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
        handler: "LiveVolcAsrHandler",
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
                "取消录音延迟收尾 "
                "meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )

    def schedule_delayed_finalize(
        self,
        meeting_id: int,
        recording_session_id: str,
        disconnect_reason: Optional[str] = None,
    ) -> None:
        """WS 异常断开后，创建等待用户重连的延迟收尾任务。"""
        if not recording_session_id:
            return

        key = (meeting_id, recording_session_id)
        existing_task = self._pending_finalize_tasks.get(key)

        if existing_task and not existing_task.done():
            return

        task = asyncio.create_task(
            self._delayed_finalize_recording(
                meeting_id=meeting_id,
                recording_session_id=recording_session_id,
                disconnect_reason=disconnect_reason,
            )
        )
        self._pending_finalize_tasks[key] = task

        logger.info(
            "已创建录音延迟收尾任务 "
            "meeting_id=%s recording_session_id=%s delay=%ss",
            meeting_id,
            recording_session_id,
            settings.VOLC_RECORDING_RECONNECT_GRACE_SECONDS,
        )

    async def _delayed_finalize_recording(
        self,
        meeting_id: int,
        recording_session_id: str,
        disconnect_reason: Optional[str] = None,
    ) -> None:
        """等待重连超时后，检查是否仍需自动收尾。"""
        key = (meeting_id, recording_session_id)
        current_task = asyncio.current_task()

        try:
            await asyncio.sleep(
                settings.VOLC_RECORDING_RECONNECT_GRACE_SECONDS
            )

            db = database.SessionLocal()
            try:
                # 如果出现同 recording_session_id 的 processing 会话，
                # 说明前端已经重新建立 WS，不能结束录音。
                active_session = (
                    db.query(database.VolcAsrSession)
                    .filter(
                        database.VolcAsrSession.meeting_id == meeting_id,
                        database.VolcAsrSession.recording_session_id
                        == recording_session_id,
                        database.VolcAsrSession.status == "processing",
                    )
                    .order_by(database.VolcAsrSession.id.desc())
                    .first()
                )

                if active_session:
                    logger.info(
                        "录音已经恢复，跳过延迟收尾 "
                        "meeting_id=%s recording_session_id=%s "
                        "active_session_id=%s",
                        meeting_id,
                        recording_session_id,
                        active_session.id,
                    )
                    return

                result = await self.finalize_and_generate_async(
                    db=db,
                    meeting_id=meeting_id,
                    recording_session_id=recording_session_id,
                )

                logger.info(
                    "录音延迟收尾完成 "
                    "meeting_id=%s recording_session_id=%s disconnect_reason=%s result=%s",
                    meeting_id,
                    recording_session_id,
                    disconnect_reason,
                    result,
                )
            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info(
                "录音延迟收尾任务已取消 "
                "meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )
            raise
        except Exception:
            logger.exception(
                "录音延迟收尾失败 "
                "meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )
        finally:
            if self._pending_finalize_tasks.get(key) is current_task:
                self._pending_finalize_tasks.pop(key, None)

    async def recover_and_finalize_async(
        self,
        db: Session,
        meeting_id: int,
        recording_session_id: str,
    ) -> Dict[str, Any]:
        """用户回到列表/详情时，主动推进异常录音收尾。

        如果该 recording_session_id 下存在正在活跃（processing）的 ASR 会话，
        说明当前仍有前端 WebSocket 在持续推流录音，不应强制收尾，
        避免过早合并、生成不完整的纪要。
        """
        if not recording_session_id:
            raise ValueError("recording_session_id 不能为空")

        self.cancel_delayed_finalize(
            meeting_id=meeting_id,
            recording_session_id=recording_session_id,
        )

        # 检查是否存在正在活跃的录音会话。
        # 如果 DB 里是 processing 且内存中有活跃 handler，说明录音真正在进行中，
        # 不应强制收尾；如果 DB 是 processing 但没有 handler（例如服务重启导致的
        # 僵尸状态），则按异常恢复流程处理。
        active_session = (
            db.query(database.VolcAsrSession)
            .filter(
                database.VolcAsrSession.meeting_id == meeting_id,
                database.VolcAsrSession.recording_session_id == recording_session_id,
                database.VolcAsrSession.status == "processing",
            )
            .order_by(database.VolcAsrSession.id.desc())
            .first()
        )

        if active_session:
            handler_key = (meeting_id, recording_session_id)
            has_live_handler = handler_key in self._active_live_handlers

            if has_live_handler:
                logger.info(
                    "录音仍在进行中，跳过恢复收尾 "
                    "meeting_id=%s recording_session_id=%s active_session_id=%s",
                    meeting_id,
                    recording_session_id,
                    active_session.id,
                )
                return {
                    "status": "skipped_recording_active",
                    "audio_id": None,
                    "job_id": None,
                    "job_status": None,
                }

            # 僵尸状态：DB 中 processing 但 handler 已不在。
            # 把 session 标记为 failed 以便后续合并能正常处理。
            logger.warning(
                "发现僵尸 ASR 会话（DB 显示 processing 但无活跃 handler），"
                "标记为 failed 继续恢复 "
                "meeting_id=%s recording_session_id=%s session_id=%s",
                meeting_id,
                recording_session_id,
                active_session.id,
            )
            active_session.status = "failed"
            active_session.error_msg = "recover-and-finalize detected stale processing session"
            db.commit()

        active_requested = await self.request_active_recording_finalize(
            meeting_id=meeting_id,
            recording_session_id=recording_session_id,
        )

        if active_requested:
            logger.info(
                "已通知火山实时录音 handler 主动收尾 "
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
        """合并同一次录音的所有片段，并保证只提交一次纪要任务。

        音频上传改为异步：本方法快速返回，上传完成后的妙记提交由上传回调负责。
        """
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
                auto_submit_minutes=True,
            )

            if not audio:
                return self._create_empty_minutes_without_audio(
                    db=db,
                    meeting_id=meeting_id,
                    reason="no_audio",
                )

            # 同一个合并音频如果已经有未失败的妙记任务，
            # 直接返回原任务，不重复提交。
            jobs = (
                db.query(database.VolcMinutesJob)
                .filter(
                    database.VolcMinutesJob.meeting_id == meeting_id,
                    database.VolcMinutesJob.source_audio_id == audio.id,
                )
                .order_by(database.VolcMinutesJob.id.desc())
                .all()
            )

            for job in jobs:
                job_status = str(
                    job.status or ""
                ).strip().lower()

                if (
                    job_status not in MINUTES_FAILED_STATUS
                    and job_status not in MINUTES_CANCELLED_STATUS
                ):
                    return {
                        "status": "already_submitted",
                        "audio_id": audio.id,
                        "job_id": job.id,
                        "job_status": job.status,
                    }

            audio_status = str(audio.status or "").strip().lower()

            # 上传失败：源文件已不可用，无法再次提交。
            if audio_status == "failed":
                return {
                    "status": "failed_no_audio",
                    "audio_id": audio.id,
                    "job_id": None,
                    "job_status": None,
                }

            # 已经上传完成（例如之前调用过 finalize-recording）：立即提交。
            if audio_status == "uploaded":
                job = self.submit_minutes(
                    db=db,
                    meeting_id=meeting_id,
                    audio_id=audio.id,
                )
                return {
                    "status": "submitted",
                    "audio_id": audio.id,
                    "job_id": job.id,
                    "job_status": job.status,
                }

            # 正在上传：由上传完成回调自动提交妙记。
            return {
                "status": "accepted",
                "audio_id": audio.id,
                "job_id": None,
                "job_status": None,
            }

    @staticmethod
    def _asr_session_for_minutes_snapshot(
        db: Session,
        meeting_id: int,
        source_audio_id: Optional[int],
    ) -> Optional[database.VolcAsrSession]:
        """按快照关联的音频定位 ASR 行；无 source_audio_id 时退回会议内最新 ASR（兼容旧快照）。"""
        q = db.query(database.VolcAsrSession).filter(database.VolcAsrSession.meeting_id == meeting_id)
        if source_audio_id is not None:
            return q.filter(database.VolcAsrSession.source_audio_id == source_audio_id).first()
        return (
            q.order_by(database.VolcAsrSession.updated_at.desc(), database.VolcAsrSession.id.desc()).first()
        )

    @staticmethod
    def _meeting_summary(db: Session, meeting_id: int) -> Optional[database.VolcMeetingSummary]:
        return (
            db.query(database.VolcMeetingSummary)
            .filter(database.VolcMeetingSummary.meeting_id == meeting_id)
            .first()
        )

    @staticmethod
    def _meeting_todos(db: Session, meeting_id: int) -> List[database.VolcMeetingTodo]:
        return (
            db.query(database.VolcMeetingTodo)
            .filter(database.VolcMeetingTodo.meeting_id == meeting_id)
            .order_by(database.VolcMeetingTodo.id.asc())
            .all()
        )

    @staticmethod
    def _latest_precise_transcription(
        db: Session,
        source_audio_id: int,
    ) -> Optional[database.VolcAccurateTranscription]:
        return (
            db.query(database.VolcAccurateTranscription)
            .filter(database.VolcAccurateTranscription.source_audio_id == source_audio_id)
            .first()
        )

    @staticmethod
    def _speaker_segment_models_for_audio(
        db: Session,
        source_audio_id: int,
    ) -> List[schemas.VolcSpeakerSegmentInDB]:
        row = (
            db.query(database.VolcAccurateTranscription)
            .filter(database.VolcAccurateTranscription.source_audio_id == source_audio_id)
            .first()
        )
        return VolcMeetingMinuteService._speaker_segment_models_from_row(row, source_audio_id)

    @staticmethod
    def _speaker_segment_models_from_row(
        row: Optional[database.VolcAccurateTranscription],
        source_audio_id: int,
    ) -> List[schemas.VolcSpeakerSegmentInDB]:
        if not row or not row.speaker_segments_json or not str(row.speaker_segments_json).strip():
            return []
        try:
            items = json.loads(row.speaker_segments_json)
        except ValueError:
            logger.warning("speaker_segments_json 解析失败 source_audio_id=%s", source_audio_id)
            return []
        if not isinstance(items, list):
            return []
        out: List[schemas.VolcSpeakerSegmentInDB] = []
        for idx, raw in enumerate(items):
            if not isinstance(raw, dict):
                continue
            out.append(
                schemas.VolcSpeakerSegmentInDB(
                    id=(row.id * 100000 + idx) if row.id is not None else idx,
                    meeting_id=row.meeting_id,
                    source_audio_id=source_audio_id,
                    segment_index=idx,
                    speaker=str(raw.get("speaker") or ""),
                    text=str(raw.get("text") or ""),
                    start_ms=raw.get("start_ms"),
                    end_ms=raw.get("end_ms"),
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
            )
        return out

    @staticmethod
    def _latest_minutes_session(
        db: Session,
        meeting_id: int,
    ) -> Optional[database.VolcMeetingMinutesSession]:
        return (
            db.query(database.VolcMeetingMinutesSession)
            .filter(database.VolcMeetingMinutesSession.meeting_id == meeting_id)
            .order_by(
                database.VolcMeetingMinutesSession.created_at.desc(),
                database.VolcMeetingMinutesSession.id.desc(),
            )
            .first()
        )

    @staticmethod
    def _latest_cancelable_minutes_job(
        db: Session,
        meeting_id: int,
    ) -> Optional[database.VolcMinutesJob]:
        rows = (
            db.query(database.VolcMinutesJob)
            .filter(database.VolcMinutesJob.meeting_id == meeting_id)
            .order_by(database.VolcMinutesJob.updated_at.desc(), database.VolcMinutesJob.id.desc())
            .all()
        )
        for row in rows:
            status = str(row.status or "").strip().lower()
            if status in MINUTES_CANCELABLE_STATUS:
                return row
        return None

    def submit_minutes(
        self,
        db: Session,
        meeting_id: int,
        audio_id: int,
    ) -> database.VolcMinutesJob:
        self._assert_meeting_exists(db, meeting_id)
        audio = (
            self._volc_audio_query(db)
            .filter(
                database.MeetingAudio.id == audio_id,
                database.MeetingAudio.meeting_id == meeting_id,
            )
            .first()
        )
        if not audio:
            raise ValueError("音频记录不存在")
        if not audio.file_url:
            raise ValueError("音频缺少 file_url，无法提交语音妙记")

        logger.info("提交火山妙记任务 meeting_id=%s audio_id=%s file_url=%s", meeting_id, audio_id, audio.file_url)
        task_id = self._minutes_api.submit(audio.file_url, audio.file_type)
        job = database.VolcMinutesJob(
            meeting_id=meeting_id,
            source_audio_id=audio.id,
            input_file_url=audio.file_url,
            input_file_type=audio.file_type,
            volc_task_id=task_id,
            status="submitted",
            error_msg=None,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        try:
            from app.services.token_tracker import token_tracker
            meeting = (
                db.query(database.Meeting)
                .filter(database.Meeting.id == meeting_id)
                .first()
            )
            total_duration = db.execute(
                text("SELECT COALESCE(SUM(duration_seconds), 0) FROM volc_asr_sessions WHERE meeting_id = :id"),
                {"id": meeting_id},
            ).scalar()
            token_tracker.record(
                user_id=meeting.creator_id if meeting else None,
                api_category="volc_miaoji",
                api_endpoint=f"{self._minutes_api._base}{self._minutes_api._submit_path}",
                total_tokens=0,
                duration_ms=int(float(total_duration) * 1000) if total_duration else 0,
                status="success",
                metadata_json=json.dumps({"meeting_id": meeting_id, "job_id": job.id}),
            )
        except Exception:
            pass

        self._start_poller(job.id)
        return job

    def cancel_minutes_job(
        self,
        db: Session,
        meeting_id: int,
        job_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> schemas.VolcMinutesCancelResponse:
        self._assert_meeting_exists(db, meeting_id)
        if job_id is None:
            job = self._latest_cancelable_minutes_job(db, meeting_id)
        else:
            job = (
                db.query(database.VolcMinutesJob)
                .filter(
                    database.VolcMinutesJob.id == job_id,
                    database.VolcMinutesJob.meeting_id == meeting_id,
                )
                .first()
            )
        if not job:
            raise ValueError("妙记任务不存在")

        status = str(job.status or "").strip().lower()
        if status in MINUTES_SUCCESS_STATUS or status in MINUTES_FAILED_STATUS:
            raise ValueError("当前妙记任务已结束，无法取消")

        with self._poll_lock:
            stop_flag = self._poll_stop.get(job.id)
            if stop_flag:
                stop_flag.set()

        if status not in MINUTES_CANCELLED_STATUS:
            job.status = "cancelled"
            job.error_msg = str(reason or "").strip() or "用户已取消当前妙记任务"
            job.updated_at = datetime.utcnow()
            db.commit()
            meeting_ws_manager.notify_from_thread(
                meeting_id,
                {
                    "type": "volc_minutes_cancelled",
                    "meeting_id": meeting_id,
                    "job_id": job.id,
                    "audio_id": job.source_audio_id,
                    "task_id": job.volc_task_id,
                    "status": "cancelled",
                    "error": job.error_msg,
                },
            )

        logger.info("已取消火山妙记任务 meeting_id=%s job_id=%s", meeting_id, job.id)
        return schemas.VolcMinutesCancelResponse(
            meeting_id=meeting_id,
            job_id=job.id,
            source_audio_id=job.source_audio_id,
            task_id=job.volc_task_id,
            status="cancelled",
        )

    def _start_poller(self, job_id: int) -> None:
        # 同一任务只保留一个活跃轮询器；新轮询启动前会停掉旧轮询。
        with self._poll_lock:
            prev = self._poll_stop.get(job_id)
            if prev:
                prev.set()
            flag = threading.Event()
            self._poll_stop[job_id] = flag

        thread = threading.Thread(
            target=self._poll_loop,
            args=(job_id, flag),
            daemon=True,
            name=f"meeting-domain-volc-poll-{job_id}",
        )
        thread.start()
        logger.info("已启动火山妙记轮询器 job_id=%s thread=%s", job_id, thread.name)

    def _poll_loop(self, job_id: int, stop_flag: threading.Event) -> None:
        # 轮询：查询妙记状态并写入 VolcMinutesJob；成功后落库纪要视图并广播（不修改音频行状态）。
        db = database.SessionLocal()
        try:
            while not stop_flag.is_set():
                job = (
                    db.query(database.VolcMinutesJob)
                    .filter(database.VolcMinutesJob.id == job_id)
                    .first()
                )
                if not job or not job.volc_task_id:
                    break
                if str(job.status or "").strip().lower() in MINUTES_CANCELLED_STATUS:
                    logger.info("火山妙记轮询已取消 job_id=%s", job_id)
                    break
                audio = None
                if job.source_audio_id is not None:
                    audio = (
                        self._volc_audio_query(db)
                        .filter(database.MeetingAudio.id == job.source_audio_id)
                        .first()
                    )

                result = self._minutes_api.query(job.volc_task_id)
                if stop_flag.is_set():
                    db.expire_all()
                    latest_job = (
                        db.query(database.VolcMinutesJob)
                        .filter(database.VolcMinutesJob.id == job_id)
                        .first()
                    )
                    if not latest_job or str(latest_job.status or "").strip().lower() in MINUTES_CANCELLED_STATUS:
                        logger.info("火山妙记轮询收到取消信号后退出 job_id=%s", job_id)
                        break
                data = result["Data"]
                status_raw = str(data["Status"]).strip()
                status = status_raw.lower()
                logger.info(
                    "火山妙记轮询状态更新 audio_id=%s meeting_id=%s task_id=%s status=%s",
                    audio.id if audio else job.source_audio_id,
                    job.meeting_id,
                    job.volc_task_id,
                    status_raw,
                )

                if status in MINUTES_RUNNING_STATUS:
                    job.status = status_raw
                    job.updated_at = datetime.utcnow()
                    db.commit()
                elif status in MINUTES_FAILED_STATUS:
                    error_msg = str(
                        data.get("ErrMessage")
                        or data.get("ErrorMessage")
                        or data.get("Message")
                        or ""
                    )
                    if audio and self._is_empty_transcript_error(error_msg):
                        snapshot_payload = self._create_empty_minutes_result(
                            db=db,
                            audio=audio,
                            reason=error_msg,
                        )
                        db.flush()
                        job.status = "completed"
                        job.error_msg = error_msg or _VOLC_EMPTY_TRANSCRIPT_HINT
                        job.updated_at = datetime.utcnow()
                        self._create_minutes_session_snapshot(
                            db,
                            audio,
                            snapshot=snapshot_payload,
                        )
                        db.commit()
                        meeting_ws_manager.notify_from_thread(
                            job.meeting_id,
                            {
                                "type": "volc_minutes_completed",
                                "meeting_id": job.meeting_id,
                                "job_id": job.id,
                                "audio_id": audio.id,
                                "task_id": job.volc_task_id,
                            },
                        )
                        break

                    job.status = "failed"
                    job.error_msg = error_msg
                    job.updated_at = datetime.utcnow()
                    db.commit()
                    meeting_ws_manager.notify_from_thread(
                        job.meeting_id,
                        {
                            "type": "volc_minutes_failed",
                            "meeting_id": job.meeting_id,
                            "job_id": job.id,
                            "audio_id": audio.id if audio else job.source_audio_id,
                            "task_id": job.volc_task_id,
                            "error": job.error_msg,
                        },
                    )
                    break
                elif status in MINUTES_SUCCESS_STATUS:
                    if not audio:
                        raise RuntimeError(f"妙记任务缺少关联音频 source_audio_id={job.source_audio_id}")
                    snapshot_payload = self._consume_minutes_success_result(db, audio, data["Result"])
                    # SessionLocal 关闭了 autoflush；先把当前视图里的精确转写/摘要/待办 flush 到事务里，
                    # 再基于这些最新数据生成会话历史快照，避免快照读到旧值或空值。
                    db.flush()
                    job.status = "completed"
                    job.error_msg = None
                    job.updated_at = datetime.utcnow()
                    self._create_minutes_session_snapshot(db, audio, snapshot=snapshot_payload)
                    db.commit()
                    meeting_ws_manager.notify_from_thread(
                        job.meeting_id,
                        {
                            "type": "volc_minutes_completed",
                            "meeting_id": job.meeting_id,
                            "job_id": job.id,
                            "audio_id": audio.id,
                            "task_id": job.volc_task_id,
                        },
                    )
                    break
                else:
                    raise RuntimeError(f"未知妙记状态: {status_raw}")
                time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            try:
                job = (
                    db.query(database.VolcMinutesJob)
                    .filter(database.VolcMinutesJob.id == job_id)
                    .first()
                )
                if job:
                    if str(job.status or "").strip().lower() in MINUTES_CANCELLED_STATUS:
                        logger.info("火山妙记轮询异常但任务已取消，忽略失败回写 job_id=%s error=%s", job_id, exc)
                        return
                    job.status = "failed"
                    job.error_msg = str(exc)
                    job.updated_at = datetime.utcnow()
                    db.commit()
                    meeting_ws_manager.notify_from_thread(
                        job.meeting_id,
                        {
                            "type": "volc_minutes_failed",
                            "meeting_id": job.meeting_id,
                            "job_id": job.id,
                            "audio_id": job.source_audio_id,
                            "task_id": job.volc_task_id,
                            "error": job.error_msg,
                        },
                    )
            except Exception:  # noqa: BLE001
                logger.exception("minutes poller failed to persist error job_id=%s", job_id)
            logger.exception("minutes poller crashed job_id=%s error=%s", job_id, exc)
        finally:
            db.close()
            with self._poll_lock:
                current = self._poll_stop.get(job_id)
                if current is stop_flag:
                    self._poll_stop.pop(job_id, None)

    @staticmethod
    def _is_empty_transcript_error(error_msg: str) -> bool:
        text = (error_msg or "").strip().lower()
        if not text:
            return False
        return any(keyword.lower() in text for keyword in _VOLC_EMPTY_TRANSCRIPT_ERROR_KEYWORDS)
    
    def _create_empty_minutes_without_audio(
        self,
        db: Session,
        meeting_id: int,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        db.query(database.VolcMeetingSummary).filter(
            database.VolcMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.add(
            database.VolcMeetingSummary(
                meeting_id=meeting_id,
                source_audio_id=None,
                title=_VOLC_EMPTY_TRANSCRIPT_TITLE,
                paragraph=_VOLC_EMPTY_TRANSCRIPT_HINT,
            )
        )

        db.query(database.VolcMeetingTodo).filter(
            database.VolcMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)

        db.add(
            database.VolcMeetingMinutesSession(
                session_no=self._build_unique_session_no(db, meeting_id),
                meeting_id=meeting_id,
                source_audio_id=None,
                stream_transcript_text="",
                accurate_transcript_text="",
                speaker_segments_json="[]",
                summary_title=_VOLC_EMPTY_TRANSCRIPT_TITLE,
                summary_paragraph=_VOLC_EMPTY_TRANSCRIPT_HINT,
                todos_json="[]",
            )
        )

        db.commit()
        logger.info(
            "火山会议无可用音频，已生成空纪要 meeting_id=%s reason=%s",
            meeting_id,
            reason,
        )
        return {
            "status": "completed_empty",
            "audio_id": None,
            "job_id": None,
            "job_status": "completed",
        }

    def _create_empty_minutes_result(
        self,
        db: Session,
        audio: database.MeetingAudio,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        db.query(database.VolcAccurateTranscription).filter(
            database.VolcAccurateTranscription.source_audio_id == audio.id,
        ).delete(synchronize_session=False)
        db.add(
            database.VolcAccurateTranscription(
                meeting_id=audio.meeting_id,
                source_audio_id=audio.id,
                accurate_transcript_text="",
                speaker_segments_json="[]",
            )
        )

        db.query(database.VolcMeetingSummary).filter(
            database.VolcMeetingSummary.meeting_id == audio.meeting_id
        ).delete(synchronize_session=False)
        db.add(
            database.VolcMeetingSummary(
                meeting_id=audio.meeting_id,
                source_audio_id=audio.id,
                title=_VOLC_EMPTY_TRANSCRIPT_TITLE,
                paragraph=_VOLC_EMPTY_TRANSCRIPT_HINT,
            )
        )

        db.query(database.VolcMeetingTodo).filter(
            database.VolcMeetingTodo.meeting_id == audio.meeting_id
        ).delete(synchronize_session=False)

        logger.info(
            "火山会议无有效转写，已生成空纪要 meeting_id=%s audio_id=%s reason=%s",
            audio.meeting_id,
            audio.id,
            reason,
        )
        return {
            "transcript_text": "",
            "speaker_segments_json": "[]",
            "summary_title": _VOLC_EMPTY_TRANSCRIPT_TITLE,
            "summary_paragraph": _VOLC_EMPTY_TRANSCRIPT_HINT,
            "todos_json": "[]",
        }

    def _consume_minutes_success_result(
        self,
        db: Session,
        audio: database.MeetingAudio,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 步骤说明（妙记结果落库）：
        # 1) 先落精确转写；
        # 2) 覆盖当前摘要；
        # 3) 覆盖当前待办列表。
        # 注意：这里覆盖的是“当前纪要视图”；历史快照由调用方在成功后额外写一份。
        if not isinstance(result, dict):
            raise TypeError(f"火山妙记 Result 格式非法: {type(result).__name__}")

        logger.info(
            "火山妙记成功结果 meeting_id=%s audio_id=%s result_keys=%s",
            audio.meeting_id,
            audio.id,
            list(result.keys()),
        )

        transcript_payload = self._fetch_json(
            self._pick_result_source(
                result,
                (
                    "TranscriptionFile",
                    "AudioTranscriptionFile",
                    "TranscriptFile",
                    "AudioTranscriptionResult",
                ),
                "转写结果",
            )
        )
        transcript_text = self._normalize_transcript_text(transcript_payload)
        speaker_segments = self._normalize_speaker_segments(transcript_payload)
        if not (transcript_text or "").strip():
            return self._create_empty_minutes_result(
                db=db,
                audio=audio,
                reason="empty transcript in success result",
            )

        db.query(database.VolcAccurateTranscription).filter(
            database.VolcAccurateTranscription.source_audio_id == audio.id,
        ).delete(synchronize_session=False)
        seg_payload = [
            {
                "speaker": seg["speaker"],
                "text": seg["text"],
                "start_ms": seg.get("start_ms"),
                "end_ms": seg.get("end_ms"),
            }
            for seg in speaker_segments
        ]
        seg_json = json.dumps(seg_payload, ensure_ascii=False) if seg_payload else None
        db.add(
            database.VolcAccurateTranscription(
                meeting_id=audio.meeting_id,
                source_audio_id=audio.id,
                accurate_transcript_text=transcript_text,
                speaker_segments_json=seg_json,
            )
        )

        summary_title: Optional[str] = None
        summary_paragraph = EMPTY_SUMMARY_HINT
        try:
            summary_payload = self._fetch_json(
                self._pick_result_source(
                    result,
                    ("SummarizationFile", "SummaryFile", "SummarizationResult"),
                    "摘要结果",
                )
            )
            logger.info(
                "火山摘要结果结构 meeting_id=%s audio_id=%s shape=%s",
                audio.meeting_id,
                audio.id,
                self._describe_payload_shape(summary_payload),
            )
            summary_title, summary_paragraph = self._normalize_summary(summary_payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "火山摘要结果不可用，回退提示文案 meeting_id=%s audio_id=%s error=%s",
                audio.meeting_id,
                audio.id,
                exc,
            )

        todos_items: List[Dict[str, Optional[str]]] = []
        try:
            todos_payload = self._fetch_json(
                self._pick_result_source(
                    result,
                    (
                        "InformationExtractionFile",
                        "TodoFile",
                        "InformationExtractionResult",
                    ),
                    "待办结果",
                )
            )
            todos_items = self._normalize_todos(todos_payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "火山待办结果不可用，按空列表处理 meeting_id=%s audio_id=%s error=%s",
                audio.meeting_id,
                audio.id,
                exc,
            )
            
        summary_is_empty = (
            not summary_paragraph
            or summary_paragraph == EMPTY_SUMMARY_HINT
        )
        title_is_empty = not (summary_title and summary_title.strip())

        if (summary_is_empty or title_is_empty) and (transcript_text or "").strip():
            llm_summary = self._llm_fallback_minutes(
                meeting_id=audio.meeting_id,
                transcript=transcript_text,
            )
            if llm_summary:
                fb_title = (llm_summary.get("summary") or {}).get("title")
                fb_paragraph = (llm_summary.get("summary") or {}).get("paragraph")
                fb_todos = llm_summary.get("todos") or []
                if summary_is_empty and isinstance(fb_paragraph, str) and fb_paragraph.strip():
                    summary_paragraph = fb_paragraph.strip()
                    logger.info(
                        "volc 摘要 LLM 兜底成功 meeting_id=%s audio_id=%s paragraph_len=%d",
                        audio.meeting_id, audio.id, len(summary_paragraph),
                    )
                if title_is_empty and isinstance(fb_title, str) and fb_title.strip():
                    summary_title = fb_title.strip()
                    logger.info(
                        "volc 标题 LLM 兜底成功 meeting_id=%s audio_id=%s title=%s",
                        audio.meeting_id, audio.id, summary_title,
                    )
                # 火山待办为空但 LLM 兜底有 todos：补上
                if not todos_items and isinstance(fb_todos, list):
                    for it in fb_todos:
                        if not isinstance(it, dict):
                            continue
                        content = str(it.get("content") or "").strip()
                        if not content:
                            continue
                        ex = it.get("executor")
                        et = it.get("execution_time")
                        todos_items.append(
                            {
                                "content": content,
                                "executor": str(ex).strip() if isinstance(ex, str) and ex.strip() else None,
                                "execution_time": str(et).strip() if isinstance(et, str) and et.strip() else None,
                            }
                        )
                    if todos_items:
                        logger.info(
                            "volc 待办 LLM 兜底补充 meeting_id=%s audio_id=%s count=%d",
                            audio.meeting_id, audio.id, len(todos_items),
                        )
        
        db.query(database.VolcMeetingSummary).filter(
            database.VolcMeetingSummary.meeting_id == audio.meeting_id
        ).delete(synchronize_session=False)
        db.add(
            database.VolcMeetingSummary(
                meeting_id=audio.meeting_id,
                source_audio_id=audio.id,
                title=summary_title,
                paragraph=summary_paragraph,
            )
        )
        
        db.query(database.VolcMeetingTodo).filter(
            database.VolcMeetingTodo.meeting_id == audio.meeting_id
        ).delete(synchronize_session=False)
        for item in todos_items:
            db.add(
                database.VolcMeetingTodo(
                    meeting_id=audio.meeting_id,
                    source_audio_id=audio.id,
                    content=item["content"],
                    executor=item["executor"],
                    execution_time=item["execution_time"],
                )
            )
        
        if summary_title and summary_title.strip():
            try:
                meeting_row = (
                    db.query(database.Meeting)
                    .filter(database.Meeting.id == audio.meeting_id)
                    .first()
                )
                if meeting_row and _DEFAULT_MEETING_TITLE_RE.match(
                    (meeting_row.title or "").strip()
                ):
                    new_title = summary_title.strip()[:_MEETING_TITLE_MAX_LEN]
                    logger.info(
                        "volc 妙记完成后自动回填 meetings.title meeting_id=%s old=%s new=%s",
                        meeting_row.id, meeting_row.title, new_title,
                    )
                    meeting_row.title = new_title
                    # 不在这里 commit，外层 _poll_loop 会统一 commit
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "volc 自动回填 meetings.title 失败（忽略，不影响主流程）meeting_id=%s error=%s",
                    audio.meeting_id, exc,
                )
        
        return {
            "transcript_text": transcript_text,
            "speaker_segments_json": seg_json or "[]",
            "summary_title": summary_title,
            "summary_paragraph": summary_paragraph,
            "todos_json": json.dumps(
                [
                    {
                        "content": item["content"],
                        "executor": item["executor"],
                        "execution_time": item["execution_time"],
                        "source_audio_id": audio.id,
                    }
                    for item in todos_items
                ],
                ensure_ascii=False,
            ),
        }
    
    def _llm_fallback_minutes(
        self,
        meeting_id: int,
        transcript: str,
    ) -> Optional[Dict[str, Any]]:
        """火山妙记摘要/标题为空时，用本地 LLM 基于转写文本兜底生成完整纪要。

        复用 LocalMeetingMinuteService._call_llm，保持两条链路 prompt 完全一致。
        - 转写为空：直接返回 None，不浪费 LLM 调用；
        - 任何异常一律返回 None，由调用方决定是否回退到 EMPTY_SUMMARY_HINT。
        """
        text_to_use = (transcript or "").strip()
        if not text_to_use:
            return None
        try:
            # 延迟 import 避免顶层循环依赖（local 服务里也会反向引用 volc 模块）
            from app.services.meeting_minute_local_service import (
                local_meeting_minute_service,
            )

            # 取会议标题作为 LLM 提示上下文（拿不到就用"会议"占位，不影响主流程）。
            # 必须用独立的 SessionLocal，避免污染外层 _poll_loop 的事务。
            meeting_title_for_prompt = "会议"
            _title_db = database.SessionLocal()
            try:
                meeting_title_for_prompt = (
                    local_meeting_minute_service._get_meeting_title(
                        _title_db, meeting_id
                    )
                    or "会议"
                )
            except Exception:
                meeting_title_for_prompt = "会议"
            finally:
                try:
                    _title_db.close()
                except Exception:
                    pass

            # duration_seconds 给 0：local 的 prompt 会按转写字符数自动选档。
            creator_id = _title_db.execute(
                text("SELECT creator_id FROM meetings WHERE id = :id"),
                {"id": meeting_id},
            ).scalar()
            payload = local_meeting_minute_service._call_llm(
                meeting_title=meeting_title_for_prompt,
                transcript=text_to_use,
                duration_seconds=0,
                user_id=creator_id,
                meeting_id=meeting_id,
            )
            if isinstance(payload, dict):
                return payload
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "volc LLM 兜底生成纪要失败 meeting_id=%s error=%s",
                meeting_id, exc,
            )
            return None

    @staticmethod
    def _pick_result_source(result: Dict[str, Any], candidates: tuple[str, ...], label: str) -> Any:
        for key in candidates:
            value = result.get(key)
            if value not in (None, "", [], {}):
                return value
        raise KeyError(f"{label} 缺失，可用字段: {list(result.keys())}")

    @staticmethod
    def _fetch_json(source: Any) -> Any:
        if isinstance(source, list):
            return source

        if isinstance(source, dict):
            for key in (
                "url",
                "Url",
                "file_url",
                "FileUrl",
                "download_url",
                "DownloadUrl",
            ):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    source = value.strip()
                    break
            else:
                return source

        if isinstance(source, str):
            raw = source.strip()
            if not raw:
                raise ValueError("妙记结果文件内容为空")

            if raw.startswith("{") or raw.startswith("["):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass

            try:
                resp = requests.get(raw, timeout=settings.VOLC_MINUTES_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.exception("下载火山妙记结果文件失败 source=%s", raw)
                raise RuntimeError(f"下载妙记结果文件失败: {raw}") from exc

        raise TypeError(f"妙记结果文件格式非法: {type(source).__name__}")

    @staticmethod
    def _describe_payload_shape(payload: Any) -> str:
        if isinstance(payload, dict):
            keys = list(payload.keys())
            preview = json.dumps(payload, ensure_ascii=False)[:1000]
            return f"dict keys={keys} preview={preview}"
        if isinstance(payload, list):
            first = payload[0] if payload else None
            first_type = type(first).__name__ if first is not None else "None"
            preview = json.dumps(first, ensure_ascii=False)[:500] if first is not None else "None"
            return f"list len={len(payload)} first_type={first_type} first_preview={preview}"
        preview = str(payload)
        return f"{type(payload).__name__} preview={preview[:500]}"

    @staticmethod
    def _normalize_transcript_text(payload: Any) -> str:
        if isinstance(payload, list):
            parts: List[str] = []
            for item in payload:
                if not isinstance(item, dict):
                    raise TypeError("TranscriptionFile 列表项格式非法")
                parts.append(_require_text_field(item, ("text", "transcript", "content"), "TranscriptionFile"))
            return "".join(parts)
        if isinstance(payload, dict):
            if "Data" in payload and isinstance(payload["Data"], list):
                return VolcMeetingMinuteService._normalize_transcript_text(payload["Data"])
            if "Result" in payload and isinstance(payload["Result"], list):
                return VolcMeetingMinuteService._normalize_transcript_text(payload["Result"])
            if "utterances" in payload and isinstance(payload["utterances"], list):
                return VolcMeetingMinuteService._normalize_transcript_text(payload["utterances"])
            if "sentences" in payload and isinstance(payload["sentences"], list):
                return VolcMeetingMinuteService._normalize_transcript_text(payload["sentences"])
            return _require_text_field(payload, ("text", "transcript", "content"), "TranscriptionFile")
        raise TypeError("TranscriptionFile JSON 格式非法")

    @staticmethod
    def _normalize_speaker_segments(payload: Any) -> List[Dict[str, Any]]:
        # 明确支持的结构：list[utterance] / {"utterances": [...]} / {"sentences": [...]} / {"Data":[...]} / {"Result":[...]}
        utterances: List[Any]
        if isinstance(payload, list):
            utterances = payload
        elif isinstance(payload, dict):
            if "utterances" in payload and isinstance(payload["utterances"], list):
                utterances = payload["utterances"]
            elif "sentences" in payload and isinstance(payload["sentences"], list):
                utterances = payload["sentences"]
            elif "Data" in payload and isinstance(payload["Data"], list):
                utterances = payload["Data"]
            elif "Result" in payload and isinstance(payload["Result"], list):
                utterances = payload["Result"]
            else:
                return []
        else:
            raise TypeError("TranscriptionFile JSON 格式非法")

        if not utterances:
            return []

        def _speaker_key(item: dict) -> Optional[str]:
            value = item.get("speaker_id") or item.get("speaker")
            if value is None:
                return None
            if isinstance(value, str):
                return value.strip() or None
            return str(value)

        has_speaker = False
        speaker_map: Dict[str, str] = {}
        for item in utterances:
            if not isinstance(item, dict):
                raise TypeError("TranscriptionFile utterance 格式非法")
            key = _speaker_key(item)
            if key:
                has_speaker = True
                if key not in speaker_map:
                    speaker_map[key] = f"说话人{len(speaker_map) + 1}"
        if not has_speaker:
            return []

        normalized: List[Dict[str, Any]] = []
        for item in utterances:
            if not isinstance(item, dict):
                raise TypeError("TranscriptionFile utterance 格式非法")
            key = _speaker_key(item)
            text = _require_text_field(item, ("text", "transcript", "content"), "TranscriptionFile")
            start_ms = item["start_time"] if "start_time" in item else item.get("start_ms")
            end_ms = item["end_time"] if "end_time" in item else item.get("end_ms")
            normalized.append(
                {
                    "speaker": speaker_map.get(key or "", key or "未知"),
                    "text": text,
                    "start_ms": float(start_ms) if isinstance(start_ms, (int, float)) else None,
                    "end_ms": float(end_ms) if isinstance(end_ms, (int, float)) else None,
                }
            )

        merged: List[Dict[str, Any]] = []
        for seg in normalized:
            if merged and merged[-1]["speaker"] == seg["speaker"]:
                merged[-1]["text"] = f"{merged[-1]['text']}{seg['text']}"
                if seg["end_ms"] is not None:
                    merged[-1]["end_ms"] = seg["end_ms"]
                continue
            merged.append(dict(seg))
        return merged

    @staticmethod
    def _normalize_summary(payload: Any) -> tuple[Optional[str], str]:
        if isinstance(payload, dict):
            if "Data" in payload:
                return VolcMeetingMinuteService._normalize_summary(payload["Data"])
            if "Result" in payload:
                return VolcMeetingMinuteService._normalize_summary(payload["Result"])
            title = payload.get("title")
            paragraph, has_candidate = VolcMeetingMinuteService._extract_summary_text(payload)
            if paragraph is None:
                if has_candidate:
                    logger.warning(
                        "SummarizationFile 文本为空，回退提示文案 payload=%s",
                        VolcMeetingMinuteService._describe_payload_shape(payload),
                    )
                    paragraph = EMPTY_SUMMARY_HINT
                else:
                    raise KeyError(
                        "SummarizationFile 缺少可用文本字段: ('paragraph', 'summary', 'content', 'text')"
                    )
            return title, paragraph
        if isinstance(payload, list):
            if not payload:
                raise ValueError("SummarizationFile 结果为空")
            first = payload[0]
            if not isinstance(first, dict):
                raise TypeError("SummarizationFile 列表项格式非法")
            title = first.get("title")
            paragraph, has_candidate = VolcMeetingMinuteService._extract_summary_text(first)
            if paragraph is None:
                if has_candidate:
                    logger.warning(
                        "SummarizationFile 列表首项文本为空，回退提示文案 payload=%s",
                        VolcMeetingMinuteService._describe_payload_shape(first),
                    )
                    paragraph = EMPTY_SUMMARY_HINT
                else:
                    raise KeyError(
                        "SummarizationFile 缺少可用文本字段: ('paragraph', 'summary', 'content', 'text')"
                    )
            return title, paragraph
        raise TypeError("SummarizationFile JSON 格式非法")

    @staticmethod
    def _extract_summary_text(item: dict) -> tuple[Optional[str], bool]:
        has_candidate = False
        for key in ("paragraph", "summary", "content", "text"):
            if key not in item:
                continue
            has_candidate = True
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), True
        return None, has_candidate

    @staticmethod
    def _normalize_todos(payload: Any) -> List[Dict[str, Optional[str]]]:
        # 火山 InformationExtractionFile 的字段命名不稳定，这里统一折叠成前端约定的 content/executor/execution_time。
        if not isinstance(payload, dict):
            logger.warning("InformationExtractionFile JSON 格式非法，按空待办处理 payload=%s", type(payload).__name__)
            return []
        items = payload.get("todo_list")
        if not isinstance(items, list):
            logger.warning(
                "InformationExtractionFile.todo_list 缺失或非法，按空待办处理 payload=%s",
                VolcMeetingMinuteService._describe_payload_shape(payload),
            )
            return []
        result: List[Dict[str, Optional[str]]] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                logger.warning("InformationExtractionFile.todo_list[%s] 非对象，已跳过", idx)
                continue
            polished = item.get("polished_res")
            if polished is not None and not isinstance(polished, dict):
                logger.warning("InformationExtractionFile.todo_list[%s].polished_res 非对象，已忽略", idx)
                polished = None
            polished_dict = polished if isinstance(polished, dict) else {}
            content = item.get("content") or polished_dict.get("content")
            if not isinstance(content, str) or not content.strip():
                logger.warning("InformationExtractionFile.todo_list[%s].content 缺失，已跳过", idx)
                continue
            executor = item.get("executor") or polished_dict.get("executor")
            execution_time = (
                item.get("execution_time")
                or polished_dict.get("execution_time")
                or item.get("execution_ddl")
            )
            if isinstance(executor, list):
                executor = ",".join(str(x) for x in executor if x is not None)
            if isinstance(execution_time, list):
                execution_time = ",".join(str(x) for x in execution_time if x is not None)
            result.append(
                {
                    "content": content,
                    "executor": str(executor) if isinstance(executor, str) and executor.strip() else None,
                    "execution_time": str(execution_time)
                    if isinstance(execution_time, str) and execution_time.strip()
                    else None,
                }
            )
        return result

    def get_minutes(self, db: Session, meeting_id: int) -> schemas.VolcMeetingMinutesResponse:
        self._assert_meeting_exists(db, meeting_id)
        latest_audio = self._latest_volc_audio(db, meeting_id)
        latest_job = self._latest_minutes_job(db, meeting_id)
        stream_text: Optional[str] = None
        transcript_text: Optional[str] = None
        speaker_segment_models: List[schemas.VolcSpeakerSegmentInDB] = []
        asr_session = self._latest_asr_session(db, meeting_id)
        if asr_session:
            stream_text = asr_session.stream_transcript_text
        if latest_audio:
            precise = self._latest_precise_transcription(db, latest_audio.id)
            if precise:
                transcript_text = precise.accurate_transcript_text
            speaker_segment_models = self._speaker_segment_models_for_audio(db, latest_audio.id)

        summary = self._meeting_summary(db, meeting_id)
        todos = self._meeting_todos(db, meeting_id)
        recoverable_recording = self._recoverable_recording_info(
            db=db,
            meeting_id=meeting_id,
            summary=summary,
        )
        return schemas.VolcMeetingMinutesResponse(
            stream_transcript_text=stream_text,
            transcript_text=transcript_text,
            minutes_job_id=latest_job.id if latest_job else None,
            minutes_job_status=latest_job.status if latest_job else None,
            audio_status=(
                latest_audio.status
                if latest_audio
                else (asr_session.status if asr_session else None)
            ),
            recoverable_recording=recoverable_recording,
            speaker_segments=[
                schemas.SpeakerSegment.model_validate(m.model_dump())
                for m in speaker_segment_models
            ],
            summary=schemas.VolcMeetingSummaryInDB.model_validate(summary) if summary else None,
            todos=[schemas.VolcMeetingTodoInDB.model_validate(x) for x in todos],
        )

    def delete_meeting_minutes_data(self, db: Session, meeting_id: int) -> None:
        # 会议删除前必须显式清空 volc 纪要相关表，避免留下 meeting_id 孤儿数据。
        self._assert_meeting_exists(db, meeting_id)
        logger.info("清理火山纪要关联数据 meeting_id=%s", meeting_id)
        db.query(database.VolcMeetingTodo).filter(
            database.VolcMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcMeetingSummary).filter(
            database.VolcMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcAccurateTranscription).filter(
            database.VolcAccurateTranscription.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcMeetingMinutesSession).filter(
            database.VolcMeetingMinutesSession.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcMinutesJob).filter(
            database.VolcMinutesJob.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcAsrSession).filter(
            database.VolcAsrSession.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.commit()

    def list_minutes_sessions(
        self,
        db: Session,
        meeting_id: int,
    ) -> List[schemas.VolcMeetingMinutesSessionInDB]:
        logger.info("查询火山纪要会话列表 meeting_id=%s", meeting_id)
        self._assert_meeting_exists(db, meeting_id)
        sessions = (
            db.query(database.VolcMeetingMinutesSession)
            .filter(database.VolcMeetingMinutesSession.meeting_id == meeting_id)
            .order_by(
                database.VolcMeetingMinutesSession.created_at.asc(),
                database.VolcMeetingMinutesSession.id.asc(),
            )
            .all()
        )
        return [self._build_session_schema(item) for item in sessions]

    def get_minutes_session(
        self,
        db: Session,
        meeting_id: int,
        session_id: int,
    ) -> schemas.VolcMeetingMinutesSessionInDB:
        logger.info("查询火山纪要会话详情 meeting_id=%s session_id=%s", meeting_id, session_id)
        self._assert_meeting_exists(db, meeting_id)
        session = (
            db.query(database.VolcMeetingMinutesSession)
            .filter(
                database.VolcMeetingMinutesSession.id == session_id,
                database.VolcMeetingMinutesSession.meeting_id == meeting_id,
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
        payload: schemas.VolcMeetingMinutesSessionUpdate,
    ) -> schemas.VolcMeetingMinutesSessionInDB:
        logger.info("更新火山纪要会话 meeting_id=%s session_id=%s", meeting_id, session_id)
        self._assert_meeting_exists(db, meeting_id)
        session = (
            db.query(database.VolcMeetingMinutesSession)
            .filter(
                database.VolcMeetingMinutesSession.id == session_id,
                database.VolcMeetingMinutesSession.meeting_id == meeting_id,
            )
            .first()
        )
        if not session:
            raise ValueError("会话历史不存在")

        fields_set = payload.model_fields_set
        if "stream_transcript_text" in fields_set:
            session.stream_transcript_text = payload.stream_transcript_text
        if "transcript_text" in fields_set:
            session.accurate_transcript_text = payload.transcript_text
        if "speaker_segments" in fields_set:
            segments_payload = [item.model_dump() for item in (payload.speaker_segments or [])]
            session.speaker_segments_json = json.dumps(segments_payload, ensure_ascii=False)
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
        logger.info("删除火山纪要会话 meeting_id=%s session_id=%s", meeting_id, session_id)
        self._assert_meeting_exists(db, meeting_id)
        session = (
            db.query(database.VolcMeetingMinutesSession)
            .filter(
                database.VolcMeetingMinutesSession.id == session_id,
                database.VolcMeetingMinutesSession.meeting_id == meeting_id,
            )
            .first()
        )
        if not session:
            raise ValueError("会话历史不存在")
        db.delete(session)
        db.commit()

    def _create_minutes_session_snapshot(
        self,
        db: Session,
        audio: database.MeetingAudio,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> database.VolcMeetingMinutesSession:
        # 火山纪要会话快照：记录“当次妙记结果”的稳定版本，便于历史回看与人工修订。
        asr_session = (
            db.query(database.VolcAsrSession)
            .filter(database.VolcAsrSession.source_audio_id == audio.id)
            .first()
        )
        if snapshot is None:
            summary = self._meeting_summary(db, audio.meeting_id)
            todos = self._meeting_todos(db, audio.meeting_id)
            precise = self._latest_precise_transcription(db, audio.id)
            speaker_segments_snapshot = (
                precise.speaker_segments_json
                if precise and precise.speaker_segments_json and str(precise.speaker_segments_json).strip()
                else "[]"
            )
            accurate_transcript_text = precise.accurate_transcript_text if precise else ""
            summary_title = summary.title if summary else None
            summary_paragraph = summary.paragraph if summary else None
            todos_json = json.dumps(
                [
                    {
                        "content": item.content,
                        "executor": item.executor,
                        "execution_time": item.execution_time,
                        "source_audio_id": item.source_audio_id,
                    }
                    for item in todos
                ],
                ensure_ascii=False,
            )
        else:
            speaker_segments_snapshot = str(snapshot.get("speaker_segments_json") or "[]")
            accurate_transcript_text = str(snapshot.get("transcript_text") or "")
            summary_title = snapshot.get("summary_title")
            summary_paragraph = snapshot.get("summary_paragraph")
            todos_json = str(snapshot.get("todos_json") or "[]")
        session = database.VolcMeetingMinutesSession(
            session_no=self._build_unique_session_no(db, audio.meeting_id),
            meeting_id=audio.meeting_id,
            source_audio_id=audio.id,
            stream_transcript_text=asr_session.stream_transcript_text if asr_session else None,
            accurate_transcript_text=accurate_transcript_text,
            speaker_segments_json=speaker_segments_snapshot,
            summary_title=summary_title,
            summary_paragraph=summary_paragraph,
            todos_json=todos_json,
        )
        db.add(session)
        db.flush()
        db.refresh(session)
        return session

    def _build_unique_session_no(self, db: Session, meeting_id: int) -> str:
        cursor = datetime.now(SESSION_NO_TIMEZONE).replace(microsecond=0)
        while True:
            candidate = f"VOLC-{meeting_id}-{cursor.strftime('%Y%m%d%H%M%S')}"
            exists = (
                db.query(database.VolcMeetingMinutesSession.id)
                .filter(database.VolcMeetingMinutesSession.session_no == candidate)
                .first()
            )
            if not exists:
                return candidate
            cursor += timedelta(seconds=1)

    @staticmethod
    def _volc_session_json_array(raw: Optional[str], field_label: str) -> List[dict]:
        if raw is None or not str(raw).strip():
            return []
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_label} 不是合法 JSON") from exc
        if not isinstance(loaded, list):
            raise ValueError(f"{field_label} 须为 JSON 数组")
        out: List[dict] = []
        for idx, elem in enumerate(loaded):
            if not isinstance(elem, dict):
                raise ValueError(f"{field_label}[{idx}] 须为 JSON 对象")
            out.append(elem)
        return out

    def _build_session_schema(
        self,
        item: database.VolcMeetingMinutesSession,
    ) -> schemas.VolcMeetingMinutesSessionInDB:
        speaker_segments: List[schemas.VolcSessionSpeakerSegment] = []
        for seg in self._volc_session_json_array(item.speaker_segments_json, "历史快照 speaker_segments_json"):
            speaker_segments.append(
                schemas.VolcSessionSpeakerSegment(
                    speaker=str(seg.get("speaker") or ""),
                    text=str(seg.get("text") or ""),
                    start_ms=seg.get("start_ms"),
                    end_ms=seg.get("end_ms"),
                )
            )
        todos: List[schemas.VolcSessionTodoItem] = []
        for todo in self._volc_session_json_array(item.todos_json, "历史快照 todos_json"):
            content = str(todo.get("content") or "").strip()
            if not content:
                continue
            ex = todo.get("executor")
            et = todo.get("execution_time")
            sid = todo.get("source_audio_id")
            todos.append(
                schemas.VolcSessionTodoItem(
                    content=content,
                    executor=str(ex).strip() if isinstance(ex, str) and ex.strip() else None,
                    execution_time=str(et).strip() if isinstance(et, str) and et.strip() else None,
                    source_audio_id=int(sid) if isinstance(sid, int) else None,
                )
            )
        return schemas.VolcMeetingMinutesSessionInDB(
            id=item.id,
            session_no=item.session_no,
            meeting_id=item.meeting_id,
            source_audio_id=item.source_audio_id,
            status="completed",
            stream_transcript_text=item.stream_transcript_text,
            transcript_text=item.accurate_transcript_text,
            speaker_segments=speaker_segments,
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
        session: database.VolcMeetingMinutesSession,
        payload: schemas.VolcMeetingMinutesSessionUpdate,
        fields_set: set[str],
    ) -> None:
        source_audio_id = session.source_audio_id
        if source_audio_id is None:
            audio = self._latest_volc_audio(db, meeting_id)
            source_audio_id = audio.id if audio else None

        if "stream_transcript_text" in fields_set:
            asr_session = self._asr_session_for_minutes_snapshot(
                db, meeting_id, session.source_audio_id
            )
            if asr_session:
                asr_session.stream_transcript_text = payload.stream_transcript_text

        if source_audio_id and "transcript_text" in fields_set:
            row = (
                db.query(database.VolcAccurateTranscription)
                .filter(database.VolcAccurateTranscription.source_audio_id == source_audio_id)
                .first()
            )
            if row:
                row.accurate_transcript_text = payload.transcript_text or ""
            else:
                db.add(
                    database.VolcAccurateTranscription(
                        meeting_id=meeting_id,
                        source_audio_id=source_audio_id,
                        accurate_transcript_text=payload.transcript_text or "",
                        speaker_segments_json=None,
                    )
                )

        if source_audio_id and "speaker_segments" in fields_set:
            row = (
                db.query(database.VolcAccurateTranscription)
                .filter(database.VolcAccurateTranscription.source_audio_id == source_audio_id)
                .first()
            )
            segs = payload.speaker_segments or []
            seg_json = (
                json.dumps(
                    [
                        {
                            "speaker": item.speaker,
                            "text": item.text,
                            "start_ms": item.start_ms,
                            "end_ms": item.end_ms,
                        }
                        for item in segs
                    ],
                    ensure_ascii=False,
                )
                if segs
                else None
            )
            if row:
                row.speaker_segments_json = seg_json
            else:
                db.add(
                    database.VolcAccurateTranscription(
                        meeting_id=meeting_id,
                        source_audio_id=source_audio_id,
                        accurate_transcript_text="",
                        speaker_segments_json=seg_json,
                    )
                )

        if "summary_title" in fields_set or "summary_paragraph" in fields_set:
            summary = self._meeting_summary(db, meeting_id)
            if summary is None:
                summary = database.VolcMeetingSummary(
                    meeting_id=meeting_id,
                    source_audio_id=source_audio_id,
                    title=session.summary_title,
                    paragraph=session.summary_paragraph or "",
                )
                db.add(summary)
            else:
                if "summary_title" in fields_set:
                    summary.title = payload.summary_title
                if "summary_paragraph" in fields_set:
                    summary.paragraph = payload.summary_paragraph or ""

        if "todos" in fields_set:
            db.query(database.VolcMeetingTodo).filter(
                database.VolcMeetingTodo.meeting_id == meeting_id
            ).delete(synchronize_session=False)
            for item in payload.todos or []:
                db.add(
                    database.VolcMeetingTodo(
                        meeting_id=meeting_id,
                        source_audio_id=item.source_audio_id or source_audio_id,
                        content=item.content,
                        executor=item.executor,
                        execution_time=item.execution_time,
                    )
                )

    async def finalize_recording_async(
        self,
        db: Session,
        meeting_id: int,
        recording_session_id: Optional[str] = None,
        max_wait_seconds: int = 10,
        auto_submit_minutes: bool = False,
    ) -> Optional[database.MeetingAudio]:
        """结束录音时统一合并同一次 recording_session 的所有片段。

        流程：
        1. 等待同 recording_session_id 下所有 processing 的 ASR 会话结束。
        2. 收集 completed/failed 且带 audio_part_path 的会话。
        3. 如果所有会话都已关联同一 MeetingAudio → 直接返回（无新内容）。
        4. 如果有已有合并音频 + 新未合并 session → 追加合并：下载已有 merged.wav，
           与新 part 合并为新 WAV，创建新 MeetingAudio（status='uploading'）。
        5. 如果所有会话都是新的 → 首次合并：合并 WAV 片段并异步上传 TOS。
        6. 上传完成后通过回调自动提交妙记任务（首次合并检查去重，追加合并强制提交）。

        参数:
            recording_session_id: 录音 session id；为空时从最新 ASR 会话推断。
            max_wait_seconds: 等待 processing 会话结束的最大秒数。
            auto_submit_minutes: 上传完成后是否自动提交妙记任务。
        """
        self._assert_meeting_exists(db, meeting_id)

        # 推断 recording_session_id
        if not recording_session_id:
            latest_session = self._latest_asr_session(db, meeting_id)
            if not latest_session:
                logger.warning("会议没有任何 ASR 会话 meeting_id=%s", meeting_id)
                return None
            recording_session_id = latest_session.recording_session_id
            if not recording_session_id:
                # 旧数据没有 recording_session_id，退回到最新音频逻辑
                logger.info("会议没有 recording_session_id，使用最新音频 meeting_id=%s", meeting_id)
                return self._latest_volc_audio(db, meeting_id)

        # 等待所有 processing 会话结束
        for waited in range(max_wait_seconds):
            processing_count = (
                db.query(database.VolcAsrSession)
                .filter(
                    database.VolcAsrSession.meeting_id == meeting_id,
                    database.VolcAsrSession.recording_session_id == recording_session_id,
                    database.VolcAsrSession.status == "processing",
                )
                .count()
            )
            if processing_count == 0:
                break
            logger.info(
                "等待 ASR 会话结束 meeting_id=%s recording_session_id=%s processing=%d waited=%d",
                meeting_id,
                recording_session_id,
                processing_count,
                waited,
            )
            await asyncio.sleep(1)
        else:
            logger.warning(
                "等待 ASR 会话结束超时 meeting_id=%s recording_session_id=%s max_wait=%d",
                meeting_id,
                recording_session_id,
                max_wait_seconds,
            )

        # 收集可合并的会话
        sessions = (
            db.query(database.VolcAsrSession)
            .filter(
                database.VolcAsrSession.meeting_id == meeting_id,
                database.VolcAsrSession.recording_session_id == recording_session_id,
                database.VolcAsrSession.status.in_(["completed", "failed"]),
                database.VolcAsrSession.audio_part_path.isnot(None),
            )
            .order_by(database.VolcAsrSession.created_at.asc())
            .all()
        )
        if not sessions:
            logger.warning(
                "没有可合并的 ASR 片段 meeting_id=%s recording_session_id=%s",
                meeting_id,
                recording_session_id,
            )
            return None

        # 如果所有片段都已经关联同一个 MeetingAudio，
        # 说明该 recording_session 已经完成过合并，直接返回。
        existing_merged_ids = {
            sess.source_audio_id
            for sess in sessions
            if sess.source_audio_id is not None
        }
        unmerged_sessions = [s for s in sessions if s.source_audio_id is None]

        # 已有合并结果 + 没有未合并 session → 跳过（所有片段都已合并）
        if existing_merged_ids and not unmerged_sessions:
            existing_audio_id = next(iter(existing_merged_ids))
            existing_merged = (
                db.query(database.MeetingAudio)
                .filter(
                    database.MeetingAudio.id == existing_audio_id,
                    database.MeetingAudio.meeting_id == meeting_id,
                    database.MeetingAudio.provider == "volc",
                )
                .first()
            )
            if existing_merged:
                logger.info(
                    "录音已经完成合并，无需重复处理 "
                    "meeting_id=%s recording_session_id=%s audio_id=%s",
                    meeting_id,
                    recording_session_id,
                    existing_merged.id,
                )
                return existing_merged

        # 取会议 creator_id 作为音频创建者
        meeting = db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
        creator_id = meeting.creator_id if meeting else None

        wav_dir = Path(
            settings.VOLC_ASR_AUDIO_SAVE_DIR
            or os.path.join(settings.UPLOAD_DIR, "asr_recordings")
        )
        wav_dir.mkdir(parents=True, exist_ok=True)

        # ── 追加合并：已有合并音频 + 新 session ──────────────────────
        if existing_merged_ids and unmerged_sessions:
            existing_audio_id = next(iter(existing_merged_ids))
            existing_audio = db.query(database.MeetingAudio).get(existing_audio_id)

            logger.info(
                "发现未合并的 ASR 会话，追加到已有音频 "
                "meeting_id=%s recording_session_id=%s existing_audio_id=%s unmerged=%d",
                meeting_id,
                recording_session_id,
                existing_audio_id,
                len(unmerged_sessions),
            )

            unmerged_part_paths = [
                Path(sess.audio_part_path) for sess in unmerged_sessions if sess.audio_part_path
            ]
            if not unmerged_part_paths or not all(p.exists() for p in unmerged_part_paths):
                missing = [str(p) for p in unmerged_part_paths if not p.exists()]
                raise RuntimeError(f"音频片段文件缺失: {missing}")

            # 已有合并文件可能已被系统定期清理，从 TOS 下载恢复
            merged_path = wav_dir / f"meeting_{meeting_id}_recording_{recording_session_id}_merged.wav"
            if not merged_path.exists():
                if existing_audio and existing_audio.object_key:
                    logger.info(
                        "本地合并文件不存在，从 TOS 下载 meeting_id=%s audio_id=%s",
                        meeting_id,
                        existing_audio_id,
                    )
                    meeting_audio_service._get_uploader().download_file(
                        existing_audio.object_key, merged_path,
                    )
                else:
                    raise RuntimeError(f"无法获取已有合并音频 audio_id={existing_audio_id}")

            v2_path = wav_dir / f"meeting_{meeting_id}_recording_{recording_session_id}_merged_v2.wav"
            _merge_wav_files([merged_path] + unmerged_part_paths, v2_path)

            on_upload_complete = None
            if auto_submit_minutes:
                def _on_append_upload_complete(uploaded_audio: database.MeetingAudio) -> None:
                    """追加合并上传成功后强制提交妙记（不检查历史任务）。"""
                    db_callback = database.SessionLocal()
                    try:
                        self.submit_minutes(db_callback, meeting_id, uploaded_audio.id)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "追加合并后自动提交妙记失败 meeting_id=%s audio_id=%s",
                            meeting_id,
                            uploaded_audio.id,
                        )
                    finally:
                        db_callback.close()
                on_upload_complete = _on_append_upload_complete

            audio_record = meeting_audio_service.create_audio_from_path_async(
                db=db,
                meeting_id=meeting_id,
                provider="volc",
                creator_id=creator_id,
                source_path=v2_path,
                file_name=f"recording_{recording_session_id}_v2.wav",
                content_type="audio/wav",
                on_upload_complete=on_upload_complete,
            )

            # 累计总时长：原有音频 + 新 session
            existing_duration = existing_audio.duration_seconds or 0 if existing_audio else 0
            new_duration = sum((sess.duration_seconds or 0) for sess in unmerged_sessions)
            audio_record.duration_seconds = existing_duration + new_duration

            # 所有 session 全部指向新音频（已合并的和未合并的都包括）
            for sess in sessions:
                sess.source_audio_id = audio_record.id
            db.commit()

            # 已合并的 session → 删除本地的 part 文件
            self._cleanup_part_files(sessions)

            logger.info(
                "录音追加合并完成并已启动异步上传 "
                "meeting_id=%s recording_session_id=%s audio_id=%s duration=%.3f sessions=%d",
                meeting_id,
                recording_session_id,
                audio_record.id,
                audio_record.duration_seconds,
                len(sessions),
            )
            return audio_record

        # ── 首次合并：所有 session 都没有合并过 ──────────────────────
        part_paths = [Path(sess.audio_part_path) for sess in sessions if sess.audio_part_path]
        if not part_paths or not all(p.exists() for p in part_paths):
            missing = [str(p) for p in part_paths if not p.exists()]
            logger.error(
                "部分音频片段文件缺失 meeting_id=%s recording_session_id=%s missing=%s",
                meeting_id,
                recording_session_id,
                missing,
            )
            raise RuntimeError(f"音频片段文件缺失: {missing}")

        merged_path = wav_dir / f"meeting_{meeting_id}_recording_{recording_session_id}_merged.wav"
        _merge_wav_files(part_paths, merged_path)

        on_upload_complete = None
        if auto_submit_minutes:

            def _on_upload_complete(uploaded_audio: database.MeetingAudio) -> None:
                """音频上传成功后自动提交妙记任务。"""
                db_callback = database.SessionLocal()
                try:
                    existing_job = (
                        db_callback.query(database.VolcMinutesJob)
                        .filter(
                            database.VolcMinutesJob.meeting_id == meeting_id,
                            database.VolcMinutesJob.source_audio_id == uploaded_audio.id,
                        )
                        .order_by(database.VolcMinutesJob.id.desc())
                        .first()
                    )
                    if existing_job:
                        job_status = str(existing_job.status or "").strip().lower()
                        if (
                            job_status not in MINUTES_FAILED_STATUS
                            and job_status not in MINUTES_CANCELLED_STATUS
                        ):
                            logger.info(
                                "妙记任务已存在，跳过自动提交 "
                                "meeting_id=%s audio_id=%s job_id=%s",
                                meeting_id,
                                uploaded_audio.id,
                                existing_job.id,
                            )
                            return
                    self.submit_minutes(db_callback, meeting_id, uploaded_audio.id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "自动提交妙记失败 meeting_id=%s audio_id=%s",
                        meeting_id,
                        uploaded_audio.id,
                    )
                finally:
                    db_callback.close()

            on_upload_complete = _on_upload_complete

        audio_record = meeting_audio_service.create_audio_from_path_async(
            db=db,
            meeting_id=meeting_id,
            provider="volc",
            creator_id=creator_id,
            source_path=merged_path,
            file_name=f"recording_{recording_session_id}.wav",
            content_type="audio/wav",
            on_upload_complete=on_upload_complete,
        )

        total_duration = sum((sess.duration_seconds or 0) for sess in sessions)
        audio_record.duration_seconds = total_duration
        for sess in sessions:
            sess.source_audio_id = audio_record.id
        db.commit()

        self._cleanup_part_files(sessions)

        logger.info(
            "录音合并完成并已启动异步上传 "
            "meeting_id=%s recording_session_id=%s audio_id=%s duration=%.3f sessions=%d",
            meeting_id,
            recording_session_id,
            audio_record.id,
            total_duration,
            len(sessions),
        )
        return audio_record

    @staticmethod
    def _cleanup_part_files(sessions: List[database.VolcAsrSession]) -> None:
        """删除已合并的 ASR 会话的本地 part 片段文件。

        只在所有 session 都已关联 source_audio_id 后调用（确保不会再被第二次合并需要），
        避免 part 文件无限堆积。
        """
        for sess in sessions:
            if sess.source_audio_id is not None and sess.audio_part_path:
                p = Path(sess.audio_part_path)
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    logger.warning("录音片段清理失败 path=%s", p)

class LiveVolcAsrHandler:
    # 核心职责：
    # 1) 实时透传音频帧并回推增量转写；
    # 2) 录音结束后持久化 WAV 并上传对象存储；
    # 3) 输出完整状态事件，避免前端猜测流程状态。

    def __init__(
        self,
        websocket,
        db: Session,
        meeting_id: int,
        service: VolcMeetingMinuteService,
        creator_id: Optional[str] = None,
    ):
        self._ws = websocket
        self._db = db
        self._meeting_id = meeting_id
        self._service = service
        self._creator_id = creator_id
        self._audio_chunks: List[bytes] = []
        self._transcript_parts: List[str] = []
        self._last_utterance_index = -1
        self._speaker_name_map: Dict[str, str] = {}
        self._max_result_text: str = ""
        self._session_id: Optional[int] = None
        self._recording_session_id: Optional[str] = None
        self._explicit_stop = False
        self._sample_rate = 16000
        self._channels = 1
        self._sample_width = 2
        self._ws_alive = True

        # 前端活动计数：收到音频包、heartbeat、config 等消息都会递增。
        # 火山 timeout 后用它判断本次重连期间前端是否仍然活着。
        self._client_activity_marker = 0

        # 前端是否已经明确结束或异常离线
        self._client_finished = False

        # 恢复入口主动要求当前 handler 结束并落盘。
        self._recover_finalize_requested = False

        # 仅用于日志区分 user_stop / ws_disconnect / idle_timeout / recover_finalize
        self._disconnect_reason: Optional[str] = None

    async def _safe_send_json(self, payload: dict) -> bool:
        if not self._ws_alive:
            return False
        try:
            await self._ws.send_json(payload)
            return True
        except (WebSocketDisconnect, ClientDisconnected, ConnectionClosed):
            self._ws_alive = False
            logger.info(
                "火山实时 ASR 前端 WebSocket 已断开，停止推送 meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
            )
            return False

    async def request_recover_finalize(self) -> None:
        """由 HTTP 恢复入口主动关闭当前实时 WS，让 run() 尽快落盘。"""
        self._recover_finalize_requested = True
        self._explicit_stop = True
        self._client_finished = True
        self._disconnect_reason = "recover_finalize"
        try:
            await self._ws.close(code=1000, reason="recover_finalize")
        except Exception:
            logger.warning(
                "主动关闭火山实时 WS 失败 meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
                exc_info=True,
            )

    @staticmethod
    def _auth_headers() -> Dict[str, str]:
        app_key = settings.VOLC_ASR_APP_KEY
        access_key = settings.VOLC_ASR_ACCESS_KEY
        resource_id = settings.VOLC_ASR_RESOURCE_ID
        if not app_key or not access_key or not resource_id:
            raise RuntimeError("VOLC_ASR_APP_KEY / VOLC_ASR_ACCESS_KEY / VOLC_ASR_RESOURCE_ID 未配置")
        return {
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Access-Key": access_key,
            "X-Api-App-Key": app_key,
        }

    @staticmethod
    def _build_init_packet(seq: int) -> bytes:
        # 火山实时 ASR 使用自定义二进制协议，这里只负责构造“初始化包”。
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)
        header.append((MessageType.CLIENT_FULL_REQUEST << 4) | MessageTypeSpecificFlags.POS_SEQUENCE)
        header.append((SerializationType.JSON << 4) | CompressionType.GZIP)
        header.append(0x00)
        payload = {
            "user": {"uid": "meeting_live_user"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": {
                "model_name": "bigmodel",
                "enable_nonstream": settings.VOLC_ASR_ENABLE_NONSTREAM,
                "enable_speaker_info": settings.VOLC_ASR_SPEAKER_INFO,
                "ssd_version": settings.VOLC_ASR_SSD_VERSION,
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,
            },
        }
        payload_bytes = gzip.compress(json.dumps(payload).encode("utf-8"))
        return bytes(header) + struct.pack(">i", seq) + struct.pack(">I", len(payload_bytes)) + payload_bytes

    @staticmethod
    def _build_audio_packet(seq: int, chunk: bytes, is_last: bool) -> bytes:
        # 普通音频包和结束包的差异主要体现在序号符号位与尾包标记。
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)
        flag = MessageTypeSpecificFlags.NEG_WITH_SEQUENCE if is_last else MessageTypeSpecificFlags.POS_SEQUENCE
        header.append((MessageType.CLIENT_AUDIO_ONLY_REQUEST << 4) | flag)
        header.append((SerializationType.NO_SERIALIZATION << 4) | CompressionType.GZIP)
        header.append(0x00)
        compressed = gzip.compress(chunk or b"")
        return bytes(header) + struct.pack(">i", seq) + struct.pack(">I", len(compressed)) + compressed

    @staticmethod
    def _is_next_packet_timeout_error(exc: BaseException) -> bool:
        text = str(exc)
        return (
            "Timeout waiting next packet" in text
            or "waiting next packet timeout" in text
            or "code=45000081" in text
        )

    async def _run_asr_once(self, retry_count: int) -> None:
        """建立一次火山 ASR 连接，并处理音频发送和识别结果接收。"""
        stop_event = asyncio.Event()

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                ASR_WS_URL,
                headers=self._auth_headers(),
            ) as volc_ws:
                seq = 1
                await volc_ws.send_bytes(
                    self._build_init_packet(seq)
                )
                seq += 1

                init_response = await asyncio.wait_for(
                    volc_ws.receive(),
                    timeout=10,
                )

                if init_response.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    raise RuntimeError(
                        "火山实时 ASR 初始化连接失败"
                    )

                if retry_count > 0:
                    await self._safe_send_json(
                        {
                            "type": "asr_recovered",
                            "retry_count": retry_count,
                        }
                    )

                send_task = asyncio.create_task(
                    self._forward_audio(
                        volc_ws,
                        stop_event,
                        seq,
                    )
                )
                recv_task = asyncio.create_task(
                    self._recv_asr_result(
                        volc_ws,
                        stop_event,
                    )
                )

                try:
                    done, _ = await asyncio.wait(
                        {
                            send_task,
                            recv_task,
                        },
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if send_task in done:
                        send_task.result()

                        try:
                            await asyncio.wait_for(
                                recv_task,
                                timeout=10,
                            )
                        except Exception:
                            if not self._client_finished:
                                raise

                            logger.warning(
                                "客户端已结束，但火山 ASR 尾包响应异常，"
                                "继续保存已有录音 "
                                "meeting_id=%s session_id=%s",
                                self._meeting_id,
                                self._session_id,
                            )
                    else:
                        recv_task.result()

                        if not self._client_finished:
                            raise RuntimeError(
                                "火山实时 ASR 连接意外结束"
                            )
                finally:
                    for task in (
                        send_task,
                        recv_task,
                    ):
                        if not task.done():
                            task.cancel()

                    await asyncio.gather(
                        send_task,
                        recv_task,
                        return_exceptions=True,
                    )

    async def _run_asr_with_retry(self) -> None:
        """火山 ASR 临时异常时进行指数退避重试。"""
        max_retries = max(
            0,
            int(settings.VOLC_ASR_MAX_RETRIES),
        )
        base_delay = max(
            0.0,
            float(
                settings.VOLC_ASR_RETRY_BASE_DELAY_SECONDS
            ),
        )
        timeout_retry_count = 0
        other_retry_count = 0

        while True:
            # 每次重新连接火山后，utterance 下标会重新从 0 开始。
            # 这里只重置下标，之前的转录文本和录音数据继续保留。
            self._last_utterance_index = -1

            activity_marker_before_attempt = self._client_activity_marker
            retry_count_for_event = max(timeout_retry_count, other_retry_count)

            try:
                await self._run_asr_once(
                    retry_count=retry_count_for_event,
                )
                return
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                RuntimeError,
            ) as exc:
                if self._client_finished:
                    raise

                is_next_packet_timeout = self._is_next_packet_timeout_error(exc)
                had_frontend_activity = (
                    self._client_activity_marker > activity_marker_before_attempt
                )

                if is_next_packet_timeout:
                    # 本次火山 timeout 期间，如果前端仍有音频/心跳/config 消息，
                    # 说明页面没有假死；下一次 timeout 重新从 1/3 判断。
                    if had_frontend_activity:
                        timeout_retry_count = 0

                    if timeout_retry_count >= max_retries:
                        raise RuntimeError(
                            "火山实时 ASR timeout 后连续重试 "
                            f"{max_retries} 次仍未恢复前端音频/心跳: {exc}"
                        ) from exc

                    timeout_retry_count += 1
                    retry_count = timeout_retry_count
                else:
                    # 非 waiting-next-packet timeout 沿用原来的重试语义，
                    # 不因为前端 heartbeat 重置，避免掩盖火山服务端/网络通用错误。
                    if other_retry_count >= max_retries:
                        raise RuntimeError(
                            f"火山实时 ASR 重试 {max_retries} 次后仍然失败: {exc}"
                        ) from exc

                    other_retry_count += 1
                    retry_count = other_retry_count

                    # 出现非 timeout 错误时，不把之前某次 timeout 的计数带到下一轮。
                    timeout_retry_count = 0

                delay_seconds = base_delay * (
                    2 ** (retry_count - 1)
                )

                logger.warning(
                    "火山实时 ASR 异常，准备重试 "
                    "meeting_id=%s session_id=%s "
                    "retry=%s/%s delay=%ss timeout=%s frontend_activity=%s error=%s",
                    self._meeting_id,
                    self._session_id,
                    retry_count,
                    max_retries,
                    delay_seconds,
                    is_next_packet_timeout,
                    had_frontend_activity,
                    exc,
                )

                await self._safe_send_json(
                    {
                        "type": "asr_retrying",
                        "retry_count": retry_count,
                        "max_retries": max_retries,
                        "delay_seconds": delay_seconds,
                        "message": str(exc),
                    }
                )

                await asyncio.sleep(delay_seconds)

    async def run(self) -> None:
        # 实时 ASR 主流程：
        # 1) 建立前端 websocket；
        # 2) 建立火山 websocket，异常时自动重试；
        # 3) 保存当前录音片段；
        # 4) 前端异常断开时启动延迟收尾。
        self._service._assert_meeting_exists(
            self._db,
            self._meeting_id,
        )
        await self._ws.accept()

        asr_session = database.VolcAsrSession(
            meeting_id=self._meeting_id,
            status="processing",
        )
        self._db.add(asr_session)
        self._db.commit()
        self._db.refresh(asr_session)
        self._session_id = asr_session.id

        await self._safe_send_json(
            {
                "type": "session_created",
                "session_id": self._session_id,
            }
        )

        try:
            logger.info(
                "开始火山实时 ASR 会话 "
                "meeting_id=%s session_id=%s",
                self._meeting_id,
                self._session_id,
            )

            await self._run_asr_with_retry()
            await self._finalize()

            # 用户手动点击结束时，由前端调用收尾接口。
            # 只有 WS 异常断开或长时间无消息时，才启动 120 秒延迟收尾。
            if (
                not self._explicit_stop
                and not self._ws_alive
                and self._recording_session_id
            ):
                self._service.schedule_delayed_finalize(
                    meeting_id=self._meeting_id,
                    recording_session_id=(
                        self._recording_session_id
                    ),
                    disconnect_reason=self._disconnect_reason,
                )

        except Exception as exc:  # noqa: BLE001
            self._disconnect_reason = "asr_failure"
            logger.exception(
                "Volc live ASR run failed "
                "meeting_id=%s session_id=%s disconnect_reason=%s",
                self._meeting_id,
                self._session_id,
                self._disconnect_reason,
            )

            partial_audio_saved = False

            # ASR 重试全部失败后，先保存已经接收到的录音。
            if self._audio_chunks:
                try:
                    await self._save_partial_audio()
                    partial_audio_saved = True
                except Exception as save_exc:  # noqa: BLE001
                    logger.warning(
                        "ASR 失败时保存部分音频失败 "
                        "meeting_id=%s session_id=%s: %s",
                        self._meeting_id,
                        self._session_id,
                        save_exc,
                    )

            asr_session.status = "failed"
            asr_session.stream_transcript_text = "".join(
                self._transcript_parts
            )
            asr_session.error_msg = str(exc)
            self._db.commit()

            # ASR 最终失败，但存在录音时，仍然合并录音并生成会议纪要。
            if (
                partial_audio_saved
                and self._recording_session_id
            ):
                try:
                    result = (
                        await self._service.finalize_and_generate_async(
                            db=self._db,
                            meeting_id=self._meeting_id,
                            recording_session_id=(
                                self._recording_session_id
                            ),
                        )
                    )

                    await self._safe_send_json(
                        {
                            "type": "recording_auto_finalized",
                            **result,
                        }
                    )
                    return
                except Exception as finalize_exc:  # noqa: BLE001
                    logger.exception(
                        "ASR 失败后的自动收尾失败 "
                        "meeting_id=%s "
                        "recording_session_id=%s",
                        self._meeting_id,
                        self._recording_session_id,
                    )

                    await self._safe_send_json(
                        {
                            "type": "error",
                            "message": str(finalize_exc),
                        }
                    )
                    return

            # 没有任何录音，才真正按失败处理。
            await self._safe_send_json(
                {
                    "type": "error",
                    "message": str(exc),
                }
            )
        finally:
            if self._recording_session_id:
                self._service.unregister_live_handler(
                    meeting_id=self._meeting_id,
                    recording_session_id=self._recording_session_id,
                    handler=self,
                )

    async def _forward_audio(self, volc_ws, stop_event: asyncio.Event, seq: int) -> None:
        # 从前端 websocket 读取控制消息和 PCM 分片，转成火山协议包发送。
        # 录音期间 PCM 是活跃信号，暂停期间 heartbeat 是活跃信号。
        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(
                        self._ws.receive(),
                        timeout=settings.VOLC_CLIENT_WS_IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "前端 WebSocket 长时间无消息 "
                        "meeting_id=%s session_id=%s timeout=%ss",
                        self._meeting_id,
                        self._session_id,
                        settings.VOLC_CLIENT_WS_IDLE_TIMEOUT_SECONDS,
                    )
                    self._ws_alive = False
                    self._client_finished = True
                    self._disconnect_reason = "idle_timeout"
                    stop_event.set()
                    break
                except (
                    WebSocketDisconnect,
                    ClientDisconnected,
                    ConnectionClosed,
                ):
                    self._ws_alive = False
                    self._client_finished = True
                    self._disconnect_reason = "ws_disconnect"
                    stop_event.set()
                    break

                msg_type = raw.get("type", "")
                if msg_type in {"websocket.disconnect", "websocket.close"}:
                    self._ws_alive = False
                    self._client_finished = True
                    self._disconnect_reason = "ws_disconnect"
                    stop_event.set()
                    break

                if raw.get("text") or raw.get("bytes"):
                    self._client_activity_marker += 1

                if raw.get("text"):
                    try:
                        ctrl = json.loads(raw["text"])
                    except json.JSONDecodeError:
                        logger.warning(
                            "忽略非法 WebSocket 控制消息 "
                            "meeting_id=%s session_id=%s",
                            self._meeting_id,
                            self._session_id,
                        )
                        continue

                    action = ctrl.get("action")

                    if action == "stop":
                        self._explicit_stop = True
                        self._client_finished = True
                        self._disconnect_reason = "user_stop"
                        stop_event.set()
                        break

                    if action == "config":
                        self._sample_rate = int(
                            ctrl.get("rate", self._sample_rate)
                        )
                        self._channels = int(
                            ctrl.get("channels", self._channels)
                        )

                        rsid = ctrl.get("recording_session_id")
                        if isinstance(rsid, str) and rsid.strip():
                            self._recording_session_id = rsid.strip()

                            # 收到 config 后立即落库。
                            # 后续才能判断相同 recording_session_id 是否已经重连。
                            asr_session = (
                                self._db.query(database.VolcAsrSession)
                                .filter(
                                    database.VolcAsrSession.id == self._session_id
                                )
                                .first()
                            )
                            if asr_session:
                                asr_session.recording_session_id = (
                                    self._recording_session_id
                                )
                                self._db.commit()

                            self._service.register_live_handler(
                                meeting_id=self._meeting_id,
                                recording_session_id=(
                                    self._recording_session_id
                                ),
                                handler=self,
                            )

                            # 使用同一个 recording_session_id 建立新 WS，
                            # 说明前端已经重连成功，取消旧连接创建的延迟收尾。
                            self._service.cancel_delayed_finalize(
                                meeting_id=self._meeting_id,
                                recording_session_id=(
                                    self._recording_session_id
                                ),
                            )

                        continue

                    if action == "heartbeat":
                        # 收到 heartbeat 本身就表示前端仍然存活。
                        continue

                    continue

                if raw.get("bytes"):
                    chunk = raw["bytes"]
                    self._audio_chunks.append(chunk)
                    await volc_ws.send_bytes(
                        self._build_audio_packet(seq, chunk, False)
                    )
                    seq += 1
        finally:
            # 用户停止、WS断开或连接超时时，通知火山结束当前ASR流。
            # 火山ASR自身异常时，client_finished仍为False，
            # 不在这里发送尾包，后续由重试逻辑重新连接火山。
            if self._client_finished:
                try:
                    await volc_ws.send_bytes(
                        self._build_audio_packet(
                            -seq,
                            b"\x00" * 160,
                            True,
                        )
                    )
                except Exception:
                    logger.debug(
                        "发送火山ASR结束包失败 "
                        "meeting_id=%s session_id=%s",
                        self._meeting_id,
                        self._session_id,
                    )

    async def _recv_asr_result(self, volc_ws, stop_event: asyncio.Event) -> None:
        # 消费火山二进制回包：提取 partial/final 文本，必要时持久化并回推给前端。
        async for msg in volc_ws:
            if msg.type != aiohttp.WSMsgType.BINARY:
                continue
            raw = msg.data
            resp = ResponseParser.parse_response(raw)
            if resp.code != 0:
                stop_event.set()
                preview = raw[:64].hex() if raw else ""
                extra: Dict[str, Any] = {}
                if isinstance(resp.payload_msg, dict):
                    extra = resp.payload_msg
                logger.error(
                    "火山实时 ASR 错误帧 meeting_id=%s session_id=%s code=%s message_type=%s "
                    "payload_keys=%s raw_prefix_hex=%s",
                    self._meeting_id,
                    self._session_id,
                    resp.code,
                    resp.message_type,
                    sorted(extra.keys()) if extra else [],
                    preview,
                )
                detail = None
                if extra:
                    detail = (
                        extra.get("message")
                        or extra.get("error")
                        or extra.get("err_msg")
                        or extra.get("detail")
                    )
                    if isinstance(detail, dict):
                        detail = json.dumps(detail, ensure_ascii=False)
                err_text = f"火山实时 ASR 返回错误 code={resp.code}"
                if detail:
                    err_text = f"{err_text} ({detail})"
                raise RuntimeError(err_text)
            payload = resp.payload_msg
            if not payload or not isinstance(payload, dict):
                if resp.is_last_package:
                    break
                continue

            result = payload.get("result")
            if not isinstance(result, dict):
                if resp.is_last_package:
                    break
                continue

            # 保留所有响应里出现过的最长全量文本，作为兜底
            full_text = result.get("text")
            if isinstance(full_text, str) and len(full_text) > len(self._max_result_text):
                self._max_result_text = full_text

            utterances = result.get("utterances")
            is_last = bool(resp.is_last_package)

            if isinstance(utterances, list):
                for i, u in enumerate(utterances):
                    if not isinstance(u, dict):
                        continue
                    text = u.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue

                    is_definite = bool(u.get("definite", False))

                    # 提取说话人 ID 并映射为“说话人1/2/3...”
                    speaker_name = None
                    additions = u.get("additions")
                    if isinstance(additions, dict):
                        speaker_id = additions.get("speaker_id")
                        if speaker_id is not None:
                            sid = str(speaker_id)
                            if sid not in self._speaker_name_map:
                                self._speaker_name_map[sid] = f"说话人{len(self._speaker_name_map) + 1}"
                            speaker_name = self._speaker_name_map[sid]

                    if is_definite:
                        # 确定分句：落袋并推进索引
                        if i > self._last_utterance_index:
                            self._transcript_parts.append(text)
                            self._last_utterance_index = i
                            await self._safe_send_json(
                                {
                                    "type": "final",
                                    "text": text,
                                    "accumulated": "".join(self._transcript_parts),
                                    "speaker": speaker_name,
                                }
                            )
                    else:
                        # 未确定分句：只作为 partial 展示，不推进索引
                        # 这样它后续变成 definite 时还能再发一次 final
                        last_indefinite_index = i
                        if i > self._last_utterance_index:
                            await self._safe_send_json(
                                {
                                    "type": "partial",
                                    "text": text,
                                    "accumulated": "".join(self._transcript_parts) + text,
                                    "speaker": speaker_name,
                                }
                            )

            # 兜底：如果 result 没有 utterances 但又有 text，按旧模式推一把
            elif "text" in result and isinstance(result["text"], str):
                text = result["text"]
                if text.strip():
                    self._transcript_parts.append(text)
                    await self._safe_send_json(
                        {
                            "type": "final" if is_last else "partial",
                            "text": text,
                            "accumulated": "".join(self._transcript_parts),
                        }
                    )

            if is_last:
                break
        stop_event.set()

    async def _finalize(self) -> None:
        # 统一收尾：
        # 1) 拼接转写文本并更新 ASR 会话；
        # 2) 把 PCM 片段落成 WAV 片段；
        # 3) 如果是显式 stop（用户结束录音），合并同一次录音的所有片段并上传；
        #    如果是 pause/timeout 导致的断开，只保存片段，不生成 MeetingAudio。
        # 用 definite 分句拼接文本 和 所有响应里最长全量文本 两者取最长，避免漏字
        parts_text = "".join(self._transcript_parts)
        transcript = parts_text if len(parts_text) >= len(self._max_result_text) else self._max_result_text
        asr_session = (
            self._db.query(database.VolcAsrSession)
            .filter(database.VolcAsrSession.id == self._session_id)
            .first()
        )
        if not asr_session:
            raise RuntimeError("ASR 会话不存在")
        if not self._audio_chunks:
            raise RuntimeError("未接收到任何音频数据，无法生成录音文件")

        wav_dir = Path(settings.VOLC_ASR_AUDIO_SAVE_DIR or os.path.join(settings.UPLOAD_DIR, "asr_recordings"))
        wav_dir.mkdir(parents=True, exist_ok=True)

        # 兼容旧客户端：没有 recording_session_id 时退回到单 session 老逻辑
        recording_session_id = self._recording_session_id
        if recording_session_id:
            wav_path = wav_dir / f"meeting_{self._meeting_id}_recording_{recording_session_id}_part_{self._session_id}.wav"
        else:
            wav_path = wav_dir / f"meeting_{self._meeting_id}_session_{self._session_id}.wav"

        logger.info(
            "开始落盘火山实时录音 meeting_id=%s session_id=%s recording_session_id=%s "
            "disconnect_reason=%s wav_path=%s",
            self._meeting_id,
            self._session_id,
            recording_session_id,
            self._disconnect_reason,
            wav_path,
        )

        duration = _save_pcm_as_wav(
            self._audio_chunks,
            wav_path,
            sample_rate=self._sample_rate,
            channels=self._channels,
            sample_width=self._sample_width,
        )
        asr_session.status = "completed"
        asr_session.duration_seconds = duration
        asr_session.stream_transcript_text = transcript
        asr_session.recording_session_id = recording_session_id
        asr_session.audio_part_path = str(wav_path)
        
        try:
            from app.services.token_tracker import token_tracker
            creator_id = self._db.execute(
                text("SELECT creator_id FROM meetings WHERE id = :id"),
                {"id": self._meeting_id},
            ).scalar()
            token_tracker.record(
                user_id=creator_id,
                api_category="volc_asr",
                api_endpoint=ASR_WS_URL,
                total_tokens=0,
                request_chars=len(transcript or ""),
                duration_ms=int(duration * 1000) if duration else 0,
                status="success",
                metadata_json=json.dumps({
                    "meeting_id": self._meeting_id,
                    "session_id": self._session_id,
                    "recording_session_id": recording_session_id or "",
                }),
            )
        except Exception:
            pass
        
        self._db.commit()

        # 兼容旧客户端：没有 recording_session_id 时，沿用单 session 老逻辑，直接合并上传
        if not recording_session_id:
            await self._safe_send_json({"type": "merging_audio", "session_id": self._session_id})
            audio_record = await self._merge_and_upload_recording(wav_dir, None, transcript)

            logger.info(
                "火山实时 ASR 录音合并完成 meeting_id=%s session_id=%s audio_id=%s duration=%.3f",
                self._meeting_id,
                self._session_id,
                audio_record.id,
                audio_record.duration_seconds or 0,
            )

            await self._safe_send_json(
                {
                    "type": "completed",
                    "session_id": self._session_id,
                    "audio_id": audio_record.id,
                    "transcript": transcript,
                    "audio_uploaded": True,
                    "duration_seconds": audio_record.duration_seconds,
                }
            )
            return

        # 新客户端：只保存片段，合并由前端后续调用 finalize-recording 统一处理
        logger.info(
            "火山实时 ASR 片段已保存 meeting_id=%s session_id=%s recording_session_id=%s "
            "disconnect_reason=%s 等待结束录音时合并",
            self._meeting_id,
            self._session_id,
            recording_session_id,
            self._disconnect_reason,
        )
        await self._safe_send_json(
            {
                "type": "completed",
                "session_id": self._session_id,
                "transcript": transcript,
                "audio_uploaded": False,
            }
        )

    async def _save_partial_audio(self) -> None:
        """ASR 失败时，把已录制的 PCM 片段保存为 WAV 并关联 MeetingAudio。

        这样即使火山服务端超时/异常，用户录音数据也不会丢失，
        后续仍可合并到同一次 recording_session_id 的完整录音中。
        """
        if not self._audio_chunks:
            return

        asr_session = (
            self._db.query(database.VolcAsrSession)
            .filter(database.VolcAsrSession.id == self._session_id)
            .first()
        )
        if not asr_session:
            raise RuntimeError("ASR 会话不存在")

        wav_dir = Path(settings.VOLC_ASR_AUDIO_SAVE_DIR or os.path.join(settings.UPLOAD_DIR, "asr_recordings"))
        wav_dir.mkdir(parents=True, exist_ok=True)

        recording_session_id = self._recording_session_id
        if recording_session_id:
            wav_path = wav_dir / f"meeting_{self._meeting_id}_recording_{recording_session_id}_part_{self._session_id}.wav"
        else:
            wav_path = wav_dir / f"meeting_{self._meeting_id}_session_{self._session_id}.wav"

        logger.info(
            "保存火山实时 ASR 部分音频 meeting_id=%s session_id=%s recording_session_id=%s wav_path=%s",
            self._meeting_id,
            self._session_id,
            recording_session_id,
            wav_path,
        )

        duration = _save_pcm_as_wav(
            self._audio_chunks,
            wav_path,
            sample_rate=self._sample_rate,
            channels=self._channels,
            sample_width=self._sample_width,
        )

        # 片段只落盘，不创建独立 MeetingAudio，避免被 _latest_volc_audio 误选。
        # 等显式 stop 合并后，统一生成一条 merged 音频记录。
        asr_session.duration_seconds = duration
        asr_session.recording_session_id = recording_session_id
        asr_session.audio_part_path = str(wav_path)
        # status 保持原样（调用方会设为 failed），但 audio_part_path 必须落盘
        self._db.commit()

        logger.info(
            "火山实时 ASR 部分音频已保存 meeting_id=%s session_id=%s duration=%.3f",
            self._meeting_id,
            self._session_id,
            duration,
        )

    async def _merge_and_upload_recording(
        self,
        wav_dir: Path,
        recording_session_id: Optional[str],
        final_session_transcript: str,
    ) -> database.MeetingAudio:
        """合并同一次录音的所有 WAV 片段并上传到 TOS。"""
        # 1. 查询该录音 session 下的所有 ASR 会话
        if recording_session_id:
            sessions = (
                self._db.query(database.VolcAsrSession)
                .filter(
                    database.VolcAsrSession.recording_session_id == recording_session_id,
                    database.VolcAsrSession.status.in_(["completed", "failed"]),
                    database.VolcAsrSession.audio_part_path.isnot(None),
                )
                .order_by(database.VolcAsrSession.created_at.asc())
                .all()
            )
        else:
            # 兼容旧客户端：没有 recording_session_id，只处理当前 session
            sessions = (
                self._db.query(database.VolcAsrSession)
                .filter(
                    database.VolcAsrSession.id == self._session_id,
                    database.VolcAsrSession.status.in_(["completed", "failed"]),
                    database.VolcAsrSession.audio_part_path.isnot(None),
                )
                .order_by(database.VolcAsrSession.created_at.asc())
                .all()
            )
        if not sessions:
            raise RuntimeError(f"没有找到录音 session 的 ASR 记录: {recording_session_id or self._session_id}")

        # 2. 收集所有存在的 WAV 片段
        part_paths: List[Path] = []
        merged_transcript_parts: List[str] = []
        for sess in sessions:
            if sess.audio_part_path and Path(sess.audio_part_path).exists():
                part_paths.append(Path(sess.audio_part_path))
            if sess.stream_transcript_text:
                merged_transcript_parts.append(sess.stream_transcript_text)

        if not part_paths:
            raise RuntimeError(f"录音 session {recording_session_id or self._session_id} 没有可合并的音频片段")

        # 3. 合并 WAV
        if recording_session_id:
            merged_path = wav_dir / f"meeting_{self._meeting_id}_recording_{recording_session_id}_merged.wav"
            merged_file_name = f"recording_{recording_session_id}.wav"
        else:
            merged_path = wav_dir / f"meeting_{self._meeting_id}_session_{self._session_id}_merged.wav"
            merged_file_name = f"live_{self._session_id}.wav"
        _merge_wav_files(part_paths, merged_path)

        # 4. 上传合并后的文件
        try:
            audio_record = meeting_audio_service.create_audio_from_path(
                db=self._db,
                meeting_id=self._meeting_id,
                provider="volc",
                creator_id=self._creator_id,
                source_path=merged_path,
                file_name=merged_file_name,
                content_type="audio/wav",
            )
        finally:
            try:
                merged_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("合并 WAV 临时文件清理失败 path=%s", merged_path)

        # 5. 回填时长、source_audio_id，并聚合转写文本
        total_duration = sum((sess.duration_seconds or 0) for sess in sessions)
        audio_record.duration_seconds = total_duration
        merged_transcript = "".join(merged_transcript_parts)
        for sess in sessions:
            sess.source_audio_id = audio_record.id
        self._db.commit()

        # 已合并的 session → 删除本地的 part 文件
        for sess in sessions:
            if sess.source_audio_id is not None and sess.audio_part_path:
                p = Path(sess.audio_part_path)
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    logger.warning("录音片段清理失败 path=%s", p)

        return audio_record



volc_meeting_minute_service = VolcMeetingMinuteService()
