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
import struct
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import requests
from sqlalchemy.orm import Session

from app.config import settings
from app.models import database, schemas
from app.services.meeting_audio_service import meeting_audio_service
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger

logger = get_logger("meeting_volc_minutes_service")


ASR_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
MINUTES_RUNNING_STATUS = {"queued", "running", "processing"}
MINUTES_SUCCESS_STATUS = {"success", "succeeded", "successed", "finished", "completed", "done"}
MINUTES_FAILED_STATUS = {"failed", "error"}
SESSION_NO_TIMEZONE = timezone(timedelta(hours=8))


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
    # 2) 状态迁移明确：uploaded -> submitted -> processing -> completed/failed；
    # 3) 避免静默兜底，异常要么上抛要么记录明确状态。

    def __init__(self) -> None:
        self._minutes_api = _VolcMinutesApi()
        self._poll_stop: Dict[int, threading.Event] = {}
        self._poll_lock = threading.Lock()

    def _assert_meeting_exists(self, db: Session, meeting_id: int) -> None:
        exists = (
            db.query(database.Meeting.id)
            .filter(database.Meeting.id == meeting_id)
            .first()
        )
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
    def _latest_asr_session(db: Session, meeting_id: int) -> Optional[database.VolcAsrSession]:
        return (
            db.query(database.VolcAsrSession)
            .filter(database.VolcAsrSession.meeting_id == meeting_id)
            .order_by(database.VolcAsrSession.updated_at.desc(), database.VolcAsrSession.id.desc())
            .first()
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
    ) -> Optional[database.VolcAudioTranscription]:
        return (
            db.query(database.VolcAudioTranscription)
            .filter(database.VolcAudioTranscription.source_audio_id == source_audio_id)
            .order_by(database.VolcAudioTranscription.created_at.desc(), database.VolcAudioTranscription.id.desc())
            .first()
        )

    @staticmethod
    def _speaker_segments_for_audio(
        db: Session,
        source_audio_id: int,
    ) -> List[database.VolcSpeakerSegment]:
        return (
            db.query(database.VolcSpeakerSegment)
            .filter(database.VolcSpeakerSegment.source_audio_id == source_audio_id)
            .order_by(database.VolcSpeakerSegment.segment_index.asc(), database.VolcSpeakerSegment.id.asc())
            .all()
        )

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

    def submit_minutes(
        self,
        db: Session,
        meeting_id: int,
        audio_id: int,
    ) -> database.MeetingAudio:
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
        audio.status = "submitted"
        audio.task_id = task_id
        audio.error_msg = None
        db.commit()
        db.refresh(audio)

        self._start_poller(audio.id)
        return audio

    def _start_poller(self, audio_id: int) -> None:
        # 同一 audio_id 只保留一个活跃轮询器；新轮询启动前会停掉旧轮询。
        with self._poll_lock:
            prev = self._poll_stop.get(audio_id)
            if prev:
                prev.set()
            flag = threading.Event()
            self._poll_stop[audio_id] = flag

        thread = threading.Thread(
            target=self._poll_loop,
            args=(audio_id, flag),
            daemon=True,
            name=f"meeting-domain-volc-poll-{audio_id}",
        )
        thread.start()
        logger.info("已启动火山妙记轮询器 audio_id=%s thread=%s", audio_id, thread.name)

    def _poll_loop(self, audio_id: int, stop_flag: threading.Event) -> None:
        # 轮询职责：
        # 1) 查询妙记任务状态；
        # 2) 把状态同步到 MeetingAudio；
        # 3) 成功后落库摘要/待办/转写并通过 websocket 广播结果。
        db = database.SessionLocal()
        try:
            while not stop_flag.is_set():
                audio = (
                    self._volc_audio_query(db)
                    .filter(database.MeetingAudio.id == audio_id)
                    .first()
                )
                if not audio or not audio.task_id:
                    break

                result = self._minutes_api.query(audio.task_id)
                data = result["Data"]
                status_raw = str(data["Status"]).strip()
                status = status_raw.lower()
                logger.info(
                    "火山妙记轮询状态更新 audio_id=%s meeting_id=%s task_id=%s status=%s",
                    audio.id,
                    audio.meeting_id,
                    audio.task_id,
                    status_raw,
                )

                if status in MINUTES_RUNNING_STATUS:
                    audio.status = status_raw
                    db.commit()
                elif status in MINUTES_FAILED_STATUS:
                    audio.status = "failed"
                    audio.error_msg = str(data["ErrMessage"])
                    db.commit()
                    meeting_ws_manager.notify_from_thread(
                        audio.meeting_id,
                        {
                            "type": "volc_minutes_failed",
                            "meeting_id": audio.meeting_id,
                            "audio_id": audio.id,
                            "task_id": audio.task_id,
                            "error": audio.error_msg,
                        },
                    )
                    break
                elif status in MINUTES_SUCCESS_STATUS:
                    self._consume_minutes_success_result(db, audio, data["Result"])
                    audio.status = "completed"
                    audio.error_msg = None
                    self._create_minutes_session_snapshot(db, audio)
                    db.commit()
                    meeting_ws_manager.notify_from_thread(
                        audio.meeting_id,
                        {
                            "type": "volc_minutes_completed",
                            "meeting_id": audio.meeting_id,
                            "audio_id": audio.id,
                            "task_id": audio.task_id,
                        },
                    )
                    break
                else:
                    raise RuntimeError(f"未知妙记状态: {status_raw}")
                time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            try:
                audio = (
                    self._volc_audio_query(db)
                    .filter(database.MeetingAudio.id == audio_id)
                    .first()
                )
                if audio:
                    audio.status = "failed"
                    audio.error_msg = str(exc)
                    db.commit()
                    meeting_ws_manager.notify_from_thread(
                        audio.meeting_id,
                        {
                            "type": "volc_minutes_failed",
                            "meeting_id": audio.meeting_id,
                            "audio_id": audio.id,
                            "task_id": audio.task_id,
                            "error": audio.error_msg,
                        },
                    )
            except Exception:  # noqa: BLE001
                logger.exception("minutes poller failed to persist error audio_id=%s", audio_id)
            logger.exception("minutes poller crashed audio_id=%s error=%s", audio_id, exc)
        finally:
            db.close()
            with self._poll_lock:
                current = self._poll_stop.get(audio_id)
                if current is stop_flag:
                    self._poll_stop.pop(audio_id, None)

    def _consume_minutes_success_result(
        self,
        db: Session,
        audio: database.MeetingAudio,
        result: Dict[str, Any],
    ) -> None:
        # 步骤说明（妙记结果落库）：
        # 1) 先落精确转写；
        # 2) 覆盖当前摘要；
        # 3) 覆盖当前待办列表。
        # 注意：这里覆盖的是“当前纪要视图”；历史快照由调用方在成功后额外写一份。
        transcript_payload = self._fetch_json(result["TranscriptionFile"])
        transcript_text = self._normalize_transcript_text(transcript_payload)
        speaker_segments = self._normalize_speaker_segments(transcript_payload)
        audio.transcript_text = transcript_text
        db.query(database.VolcAudioTranscription).filter(
            database.VolcAudioTranscription.source_audio_id == audio.id,
        ).delete(synchronize_session=False)
        db.add(
            database.VolcAudioTranscription(
                meeting_id=audio.meeting_id,
                source_audio_id=audio.id,
                text=transcript_text,
                is_final=True,
            )
        )
        db.query(database.VolcSpeakerSegment).filter(
            database.VolcSpeakerSegment.source_audio_id == audio.id
        ).delete(synchronize_session=False)
        for idx, seg in enumerate(speaker_segments):
            db.add(
                database.VolcSpeakerSegment(
                    meeting_id=audio.meeting_id,
                    source_audio_id=audio.id,
                    segment_index=idx,
                    speaker=seg["speaker"],
                    text=seg["text"],
                    start_ms=seg["start_ms"],
                    end_ms=seg["end_ms"],
                )
            )

        summary_payload = self._fetch_json(result["SummarizationFile"])
        db.query(database.VolcMeetingSummary).filter(
            database.VolcMeetingSummary.meeting_id == audio.meeting_id
        ).delete(synchronize_session=False)
        title, paragraph = self._normalize_summary(summary_payload)
        db.add(
            database.VolcMeetingSummary(
                meeting_id=audio.meeting_id,
                source_audio_id=audio.id,
                title=title,
                paragraph=paragraph,
            )
        )

        todos_payload = self._fetch_json(result["InformationExtractionFile"])
        db.query(database.VolcMeetingTodo).filter(
            database.VolcMeetingTodo.meeting_id == audio.meeting_id
        ).delete(synchronize_session=False)
        for item in self._normalize_todos(todos_payload):
            db.add(
                database.VolcMeetingTodo(
                    meeting_id=audio.meeting_id,
                    source_audio_id=audio.id,
                    content=item["content"],
                    executor=item["executor"],
                    execution_time=item["execution_time"],
                )
            )

    @staticmethod
    def _fetch_json(url: str) -> Any:
        if not url:
            raise ValueError("妙记结果文件 URL 为空")
        try:
            resp = requests.get(url, timeout=settings.VOLC_MINUTES_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.exception("下载火山妙记结果文件失败 url=%s", url)
            raise RuntimeError(f"下载妙记结果文件失败: {url}") from exc

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
            paragraph = _require_text_field(payload, ("paragraph", "summary", "content", "text"), "SummarizationFile")
            return title, paragraph
        if isinstance(payload, list):
            if not payload:
                raise ValueError("SummarizationFile 结果为空")
            first = payload[0]
            if not isinstance(first, dict):
                raise TypeError("SummarizationFile 列表项格式非法")
            title = first.get("title")
            paragraph = _require_text_field(first, ("paragraph", "summary", "content", "text"), "SummarizationFile")
            return title, paragraph
        raise TypeError("SummarizationFile JSON 格式非法")

    @staticmethod
    def _normalize_todos(payload: Any) -> List[Dict[str, Optional[str]]]:
        # 火山 InformationExtractionFile 的字段命名不稳定，这里统一折叠成前端约定的 content/executor/execution_time。
        if not isinstance(payload, dict):
            raise TypeError("InformationExtractionFile JSON 格式非法")
        items = payload["todo_list"]
        if not isinstance(items, list):
            raise TypeError("InformationExtractionFile JSON 格式非法")
        result: List[Dict[str, Optional[str]]] = []
        for item in items:
            if not isinstance(item, dict):
                raise TypeError("InformationExtractionFile JSON 格式非法")
            polished = item.get("polished_res")
            if polished is not None and not isinstance(polished, dict):
                raise TypeError("InformationExtractionFile.polished_res 格式非法")
            polished_dict = polished if isinstance(polished, dict) else {}
            content = item.get("content") or polished_dict.get("content")
            if not isinstance(content, str) or not content.strip():
                raise KeyError("InformationExtractionFile.todo_list.content 缺失")
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
        stream_text: Optional[str] = None
        transcript_text: Optional[str] = None
        speaker_segments: List[database.VolcSpeakerSegment] = []
        asr_session = self._latest_asr_session(db, meeting_id)
        if asr_session:
            stream_text = asr_session.transcript_text
        if latest_audio:
            precise = self._latest_precise_transcription(db, latest_audio.id)
            if precise:
                transcript_text = precise.text
            elif latest_audio.transcript_text:
                transcript_text = latest_audio.transcript_text
            speaker_segments = self._speaker_segments_for_audio(db, latest_audio.id)

        summary = self._meeting_summary(db, meeting_id)
        todos = self._meeting_todos(db, meeting_id)
        return schemas.VolcMeetingMinutesResponse(
            stream_transcript_text=stream_text,
            transcript_text=transcript_text,
            audio_status=latest_audio.status if latest_audio else asr_session.status if asr_session else None,
            speaker_segments=[schemas.VolcSpeakerSegmentInDB.model_validate(x) for x in speaker_segments],
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
        db.query(database.VolcSpeakerSegment).filter(
            database.VolcSpeakerSegment.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcAudioTranscription).filter(
            database.VolcAudioTranscription.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcMeetingMinutesSession).filter(
            database.VolcMeetingMinutesSession.meeting_id == meeting_id
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
    ) -> Optional[schemas.VolcMeetingMinutesSessionInDB]:
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
            return None
        return self._build_session_schema(session)

    def update_minutes_session(
        self,
        db: Session,
        meeting_id: int,
        session_id: int,
        payload: schemas.VolcMeetingMinutesSessionUpdate,
    ) -> Optional[schemas.VolcMeetingMinutesSessionInDB]:
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
            return None

        fields_set = payload.model_fields_set
        if "status" in fields_set:
            session.status = payload.status or session.status
        if "error_msg" in fields_set:
            session.error_msg = payload.error_msg
        if "stream_transcript_text" in fields_set:
            session.stream_transcript_text = payload.stream_transcript_text
        if "transcript_text" in fields_set:
            session.transcript_text = payload.transcript_text
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

    def delete_minutes_session(self, db: Session, meeting_id: int, session_id: int) -> bool:
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
            return False
        db.delete(session)
        db.commit()
        return True

    def _create_minutes_session_snapshot(
        self,
        db: Session,
        audio: database.MeetingAudio,
    ) -> database.VolcMeetingMinutesSession:
        # 火山纪要会话快照：记录“当次妙记结果”的稳定版本，便于历史回看与人工修订。
        summary = self._meeting_summary(db, audio.meeting_id)
        todos = self._meeting_todos(db, audio.meeting_id)
        precise = self._latest_precise_transcription(db, audio.id)
        speaker_segments = self._speaker_segments_for_audio(db, audio.id)
        asr_session = (
            db.query(database.VolcAsrSession)
            .filter(database.VolcAsrSession.source_audio_id == audio.id)
            .first()
        )
        todos_payload = [
            {
                "content": item.content,
                "executor": item.executor,
                "execution_time": item.execution_time,
                "source_audio_id": item.source_audio_id,
            }
            for item in todos
        ]
        session = database.VolcMeetingMinutesSession(
            session_no=self._build_unique_session_no(db, audio.meeting_id),
            meeting_id=audio.meeting_id,
            source_audio_id=audio.id,
            source_asr_session_id=audio.source_asr_session_id,
            volc_task_id=audio.task_id,
            status="completed",
            error_msg=audio.error_msg,
            stream_transcript_text=asr_session.transcript_text if asr_session else None,
            transcript_text=precise.text if precise else audio.transcript_text,
            speaker_segments_json=json.dumps(
                [
                    {
                        "speaker": item.speaker,
                        "text": item.text,
                        "start_ms": item.start_ms,
                        "end_ms": item.end_ms,
                    }
                    for item in speaker_segments
                ],
                ensure_ascii=False,
            ),
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
    def _safe_json_load(raw: Optional[str]) -> List[dict]:
        if not raw:
            return []
        try:
            loaded = json.loads(raw)
        except ValueError:
            logger.warning("火山纪要会话 JSON 反序列化失败 raw_preview=%s", raw[:300])
            return []
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        logger.warning("火山纪要会话 JSON 顶层不是 list type=%s", type(loaded).__name__)
        return []

    def _build_session_schema(
        self,
        item: database.VolcMeetingMinutesSession,
    ) -> schemas.VolcMeetingMinutesSessionInDB:
        speaker_segments = [
            schemas.VolcSessionSpeakerSegment(**seg)
            for seg in self._safe_json_load(item.speaker_segments_json)
        ]
        todos = [
            schemas.VolcSessionTodoItem(**todo)
            for todo in self._safe_json_load(item.todos_json)
        ]
        return schemas.VolcMeetingMinutesSessionInDB(
            id=item.id,
            session_no=item.session_no,
            meeting_id=item.meeting_id,
            source_audio_id=item.source_audio_id,
            source_asr_session_id=item.source_asr_session_id,
            volc_task_id=item.volc_task_id,
            status=item.status,
            error_msg=item.error_msg,
            stream_transcript_text=item.stream_transcript_text,
            transcript_text=item.transcript_text,
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
            asr_session = self._latest_asr_session(db, meeting_id)
            if asr_session:
                asr_session.transcript_text = payload.stream_transcript_text
                if source_audio_id:
                    audio = (
                        db.query(database.MeetingAudio)
                        .filter(database.MeetingAudio.id == source_audio_id)
                        .first()
                    )
                    if audio:
                        audio.transcript_text = payload.stream_transcript_text
                        audio.source_asr_session_id = asr_session.id

        if source_audio_id and "transcript_text" in fields_set:
            db.query(database.VolcAudioTranscription).filter(
                database.VolcAudioTranscription.source_audio_id == source_audio_id
            ).delete(synchronize_session=False)
            db.add(
                database.VolcAudioTranscription(
                    meeting_id=meeting_id,
                    source_audio_id=source_audio_id,
                    text=payload.transcript_text or "",
                    is_final=True,
                )
            )
            audio = db.query(database.MeetingAudio).filter(database.MeetingAudio.id == source_audio_id).first()
            if audio:
                audio.transcript_text = payload.transcript_text or ""

        if source_audio_id and "speaker_segments" in fields_set:
            db.query(database.VolcSpeakerSegment).filter(
                database.VolcSpeakerSegment.source_audio_id == source_audio_id
            ).delete(synchronize_session=False)
            for idx, item in enumerate(payload.speaker_segments or []):
                db.add(
                    database.VolcSpeakerSegment(
                        meeting_id=meeting_id,
                        source_audio_id=source_audio_id,
                        segment_index=idx,
                        speaker=item.speaker,
                        text=item.text,
                        start_ms=item.start_ms,
                        end_ms=item.end_ms,
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


class LiveVolcAsrHandler:
    # 核心职责：
    # 1) 实时透传音频帧并回推增量转写；
    # 2) 录音结束后持久化 WAV 并上传对象存储；
    # 3) 输出完整状态事件，避免前端猜测流程状态。

    def __init__(self, websocket, db: Session, meeting_id: int, service: VolcMeetingMinuteService):
        self._ws = websocket
        self._db = db
        self._meeting_id = meeting_id
        self._service = service
        self._audio_chunks: List[bytes] = []
        self._transcript_parts: List[str] = []
        self._session_id: Optional[int] = None
        self._sample_rate = 16000
        self._channels = 1
        self._sample_width = 2
        self._ws_alive = True

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

    async def run(self) -> None:
        # 实时 ASR 主流程：
        # 1) 建立前端 websocket 与火山 websocket；
        # 2) 并发执行“转发音频”和“接收识别结果”；
        # 3) 结束后统一落盘并上传音频。
        self._service._assert_meeting_exists(self._db, self._meeting_id)
        await self._ws.accept()
        asr_session = database.VolcAsrSession(
            meeting_id=self._meeting_id,
            session_type="live",
            status="processing",
        )
        self._db.add(asr_session)
        self._db.commit()
        self._db.refresh(asr_session)
        self._session_id = asr_session.id
        await self._ws.send_json({"type": "session_created", "session_id": self._session_id})

        try:
            logger.info("开始火山实时 ASR 会话 meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
            stop_event = asyncio.Event()
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ASR_WS_URL, headers=self._auth_headers()) as volc_ws:
                    seq = 1
                    await volc_ws.send_bytes(self._build_init_packet(seq))
                    seq += 1
                    await volc_ws.receive()  # 初始化应答

                    send_task = asyncio.create_task(self._forward_audio(volc_ws, stop_event, seq))
                    recv_task = asyncio.create_task(self._recv_asr_result(volc_ws, stop_event))
                    await asyncio.wait([send_task, recv_task], return_when=asyncio.ALL_COMPLETED)

            await self._finalize()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Volc live ASR run failed meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
            asr_session.status = "failed"
            asr_session.transcript_text = "".join(self._transcript_parts)
            asr_session.error_msg = str(exc)
            self._db.commit()
            if self._ws_alive:
                await self._ws.send_json({"type": "error", "message": str(exc)})
            raise

    async def _forward_audio(self, volc_ws, stop_event: asyncio.Event, seq: int) -> None:
        # 从前端 websocket 读取控制消息和 PCM 分片，转成火山协议包发送。
        while not stop_event.is_set():
            raw = await self._ws.receive()
            msg_type = raw.get("type", "")
            if msg_type in {"websocket.disconnect", "websocket.close"}:
                self._ws_alive = False
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
                continue
            if raw.get("bytes"):
                chunk = raw["bytes"]
                self._audio_chunks.append(chunk)
                await volc_ws.send_bytes(self._build_audio_packet(seq, chunk, False))
                seq += 1

        # 无论 stop 来源是什么，都要显式发结束包，确保服务端尽快收敛。
        await volc_ws.send_bytes(self._build_audio_packet(-seq, b"\x00" * 160, True))

    async def _recv_asr_result(self, volc_ws, stop_event: asyncio.Event) -> None:
        # 消费火山二进制回包：提取 partial/final 文本，必要时持久化并回推给前端。
        async for msg in volc_ws:
            if msg.type != aiohttp.WSMsgType.BINARY:
                continue
            resp = ResponseParser.parse_response(msg.data)
            if resp.code != 0:
                stop_event.set()
                raise RuntimeError(f"火山实时 ASR 返回错误 code={resp.code}")
            payload = resp.payload_msg
            text = _extract_text(payload) if payload else None
            is_last = bool(resp.is_last_package)
            is_definite = bool((payload.get("result") or {}).get("definite", False)) if payload else False
            should_accumulate = is_definite or is_last
            if text:
                if should_accumulate:
                    self._transcript_parts.append(text)
                    self._db.add(
                        database.VolcAudioTranscription(
                            meeting_id=self._meeting_id,
                            source_session_id=self._session_id,
                            text=text,
                            is_final=is_last,
                        )
                    )
                    self._db.commit()
                if self._ws_alive:
                    await self._ws.send_json(
                        {
                            "type": "final" if is_definite else "partial",
                            "text": text,
                            "accumulated": "".join(self._transcript_parts) if should_accumulate else "".join(self._transcript_parts) + text,
                        }
                    )
            if is_last:
                break
        stop_event.set()

    async def _finalize(self) -> None:
        # 统一收尾：
        # 1) 拼接转写文本并更新 ASR 会话；
        # 2) 把 PCM 片段落成 WAV；
        # 3) 上传 WAV 并回传最终 completed 事件。
        transcript = "".join(self._transcript_parts)
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
        wav_path = wav_dir / f"meeting_{self._meeting_id}_session_{self._session_id}.wav"
        logger.info(
            "开始落盘火山实时录音 meeting_id=%s session_id=%s wav_path=%s",
            self._meeting_id,
            self._session_id,
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
        asr_session.transcript_text = transcript
        asr_session.audio_local_path = str(wav_path)
        asr_session.audio_filename = wav_path.name

        if self._ws_alive:
            await self._ws.send_json({"type": "saving_audio", "session_id": self._session_id})

        # 统一复用 meeting_audio_service 的上传/TOS链路，不在本服务重复维护上传实现。
        try:
            audio_record = meeting_audio_service.create_audio_from_path(
                db=self._db,
                meeting_id=self._meeting_id,
                provider="volc",
                source_path=wav_path,
                file_name=f"live_{self._session_id}.wav",
                content_type="audio/wav",
            )
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("火山实时录音临时 WAV 清理失败 path=%s", wav_path)
        asr_session.source_audio_id = audio_record.id
        audio_record.source_asr_session_id = asr_session.id
        audio_record.transcript_text = transcript
        self._db.commit()
        logger.info(
            "火山实时 ASR 会话完成 meeting_id=%s session_id=%s audio_id=%s duration=%.3f",
            self._meeting_id,
            self._session_id,
            audio_record.id,
            duration,
        )

        if self._ws_alive:
            await self._ws.send_json(
                {
                    "type": "completed",
                    "session_id": self._session_id,
                    "audio_id": audio_record.id,
                    "transcript": transcript,
                    "audio_uploaded": True,
                    "duration_seconds": duration,
                }
            )


volc_meeting_minute_service = VolcMeetingMinuteService()
