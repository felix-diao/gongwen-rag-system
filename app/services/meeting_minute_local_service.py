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
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from sqlalchemy.orm import Session

from app.config import settings
from app.models import database, schemas
from app.services.meeting_audio_service import meeting_audio_service
from app.utils.logger import get_logger

logger = get_logger("meeting_local_minutes_service")
SESSION_NO_TIMEZONE = timezone(timedelta(hours=8))
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
    """构造 Qwen 实时 ASR 鉴权头。

    若未配置 API Key，返回空字典，让上游在连接阶段明确失败，而不是伪造默认值。
    """
    api_key = (settings.QWEN_ASR_API_KEY or "").strip()
    if not api_key:
        return {}
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


class LocalMeetingMinuteService:
    # 设计约束：
    # 1) local 模式只依赖“当前最新流式转写”生成纪要，因此生成入口必须严格校验 ASR 会话存在；
    # 2) 当前纪要视图（summary/todo）与历史快照（session）同时维护，确保“可编辑”和“可回放”两种能力都成立；
    # 3) 不做静默失败兜底：LLM、转写、数据库任一环节出错，都必须留下明确日志和错误文案。

    def _assert_meeting_exists(self, db: Session, meeting_id: int) -> None:
        exists = db.query(database.Meeting.id).filter(database.Meeting.id == meeting_id).first()
        if not exists:
            raise ValueError("会议不存在")

    @staticmethod
    def _latest_asr_session(db: Session, meeting_id: int) -> Optional[database.LocalAsrSession]:
        return (
            db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.meeting_id == meeting_id)
            .order_by(database.LocalAsrSession.updated_at.desc(), database.LocalAsrSession.id.desc())
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

    def generate_minutes(self, db: Session, meeting_id: int) -> schemas.LocalMeetingMinutesResponse:
        logger.info("开始生成本地会议纪要 meeting_id=%s", meeting_id)
        self._assert_meeting_exists(db, meeting_id)
        asr_session = self._latest_asr_session(db, meeting_id)
        if not asr_session or not asr_session.transcript_text:
            raise ValueError("当前会议尚无流式转写文本")

        meeting = db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
        meeting_title = meeting.title if meeting else "会议"
        payload = self._call_llm(meeting_title, asr_session.transcript_text)

        latest_local_audio = self._latest_local_audio(db, meeting_id)
        source_audio_id = latest_local_audio.id if latest_local_audio else None
        if latest_local_audio:
            latest_local_audio.transcript_text = asr_session.transcript_text
            latest_local_audio.source_asr_session_id = asr_session.id

        db.query(database.LocalMeetingSummary).filter(
            database.LocalMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingTodo).filter(
            database.LocalMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.flush()

        summary_raw = payload.get("summary") or {}
        if isinstance(summary_raw, str):
            summary_raw = {"paragraph": summary_raw}
        db.add(
            database.LocalMeetingSummary(
                meeting_id=meeting_id,
                source_audio_id=source_audio_id,
                title=summary_raw.get("title") or meeting_title,
                paragraph=summary_raw.get("paragraph") or summary_raw.get("content") or "",
            )
        )

        for item in payload.get("todos") or []:
            if isinstance(item, str):
                item = {"content": item}
            content = item.get("content") or item.get("description")
            if not isinstance(content, str) or not content.strip():
                continue
            db.add(
                database.LocalMeetingTodo(
                    meeting_id=meeting_id,
                    source_audio_id=source_audio_id,
                    content=content.strip(),
                    executor=item.get("executor") or item.get("owner"),
                    execution_time=item.get("execution_time") or item.get("deadline"),
                )
            )

        db.commit()
        self._create_minutes_session_snapshot(
            db=db,
            meeting_id=meeting_id,
            source_audio_id=source_audio_id,
            source_asr_session_id=asr_session.id,
            stream_transcript_text=asr_session.transcript_text,
        )
        logger.info("本地会议纪要生成完成 meeting_id=%s source_audio_id=%s", meeting_id, source_audio_id)
        return self.get_minutes(db, meeting_id)

    def get_minutes(self, db: Session, meeting_id: int) -> schemas.LocalMeetingMinutesResponse:
        self._assert_meeting_exists(db, meeting_id)
        latest_session = self._latest_asr_session(db, meeting_id)
        latest_audio = self._latest_local_audio(db, meeting_id)
        summary = self._meeting_summary(db, meeting_id)
        todos = self._meeting_todos(db, meeting_id)
        return schemas.LocalMeetingMinutesResponse(
            transcript_text=(
                latest_audio.transcript_text
                if latest_audio and latest_audio.transcript_text
                else latest_session.transcript_text if latest_session else None
            ),
            stream_transcript_text=latest_session.transcript_text if latest_session else None,
            audio_status=latest_audio.status if latest_audio else latest_session.status if latest_session else None,
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
    ) -> Optional[schemas.LocalMeetingMinutesSessionInDB]:
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
            return None
        return self._build_session_schema(session)

    def update_minutes_session(
        self,
        db: Session,
        meeting_id: int,
        session_id: int,
        payload: schemas.LocalMeetingMinutesSessionUpdate,
    ) -> Optional[schemas.LocalMeetingMinutesSessionInDB]:
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
            return False
        db.delete(session)
        db.commit()
        return True

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

    def _call_llm(self, meeting_title: str, transcript: str) -> dict:
        from app.llm_client.generators import get_client

        instruction = (
            "你是企业会议纪要助手。请基于转写文本输出严格 JSON："
            '{"summary":{"title":"string","paragraph":"string"},"todos":[{"content":"string","executor":"string|null","execution_time":"string|null"}]}'
            "只允许输出 JSON 对象，不要输出解释文本。"
        )
        user_msg = f"会议标题：{meeting_title}\n\n会议转写文本：\n{transcript}"
        try:
            cli = get_client()
            raw = cli.chat(
                [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=1800,
            )
            payload = self._parse_json(raw)
            return self._normalize_payload(payload, meeting_title)
        except Exception as exc:  # noqa: BLE001
            logger.exception("本地会议纪要 LLM 调用失败 meeting_title=%s", meeting_title)
            raise RuntimeError(f"LLM 生成会议纪要失败: {exc}") from exc

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
            raise ValueError(f"LLM 返回内容不是合法 JSON: {cleaned[:300]}") from exc
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
        source_asr_session_id: Optional[int],
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
            source_asr_session_id=source_asr_session_id,
            status="completed",
            error_msg=None,
            stream_transcript_text=stream_transcript_text,
            transcript_text=stream_transcript_text,
            summary_title=summary.title if summary else None,
            summary_paragraph=summary.paragraph if summary else None,
            todos_json=json.dumps(todos_payload, ensure_ascii=False),
        )
        db.add(session)
        db.commit()
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
    def _safe_json_load(raw: Optional[str]) -> List[dict]:
        if not raw:
            return []
        try:
            loaded = json.loads(raw)
        except ValueError:
            logger.warning("本地纪要会话 JSON 反序列化失败 raw_preview=%s", raw[:300])
            return []
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        logger.warning("本地纪要会话 JSON 顶层不是 list type=%s", type(loaded).__name__)
        return []

    def _build_session_schema(
        self,
        item: database.LocalMeetingMinutesSession,
    ) -> schemas.LocalMeetingMinutesSessionInDB:
        todos = [schemas.LocalSessionTodoItem(**todo) for todo in self._safe_json_load(item.todos_json)]
        return schemas.LocalMeetingMinutesSessionInDB(
            id=item.id,
            session_no=item.session_no,
            meeting_id=item.meeting_id,
            source_audio_id=item.source_audio_id,
            source_asr_session_id=item.source_asr_session_id,
            status=item.status,
            error_msg=item.error_msg,
            stream_transcript_text=item.stream_transcript_text,
            transcript_text=item.transcript_text,
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
        if "stream_transcript_text" in fields_set:
            asr_session = self._latest_asr_session(db, meeting_id)
            if asr_session:
                asr_session.transcript_text = payload.stream_transcript_text
                if session.source_audio_id:
                    audio = (
                        db.query(database.MeetingAudio)
                        .filter(database.MeetingAudio.id == session.source_audio_id)
                        .first()
                    )
                    if audio:
                        audio.transcript_text = payload.stream_transcript_text
                        audio.source_asr_session_id = asr_session.id

        if "transcript_text" in fields_set:
            audio = None
            if session.source_audio_id:
                audio = (
                    db.query(database.MeetingAudio)
                    .filter(database.MeetingAudio.id == session.source_audio_id)
                    .first()
                )
            if audio is None:
                audio = self._latest_local_audio(db, meeting_id)
            if audio:
                audio.transcript_text = payload.transcript_text

        if "summary_title" in fields_set or "summary_paragraph" in fields_set:
            summary = self._meeting_summary(db, meeting_id)
            if summary is None:
                summary = database.LocalMeetingSummary(
                    meeting_id=meeting_id,
                    source_audio_id=session.source_audio_id,
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

    处理链路：
    1. 接收前端 PCM 音频。
    2. 转发给 Qwen 实时 ASR。
    3. 将 partial/final 文本回推前端。
    4. 录音结束后把 PCM 落为 WAV，并上传到统一音频表。
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
        self._sample_rate = 16000
        self._channels = 1
        self._sample_width = 2
        self._ws_alive = True

    async def run(self) -> None:
        # `run` 是实时会话总入口，负责创建 ASR session、启动双向转发、并在结束时统一收尾。
        self._service._assert_meeting_exists(self._db, self._meeting_id)
        await self._ws.accept()

        asr_session = database.LocalAsrSession(
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
            logger.info("开始本地实时 ASR 会话 meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
            ws_url = _build_qwen_ws_url()
            headers = _build_qwen_ws_headers()
            stop_event = asyncio.Event()

            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, headers=headers) as qwen_ws:
                    await qwen_ws.send_str(_session_update_event())
                    send_task = asyncio.create_task(self._forward_audio(qwen_ws, stop_event))
                    recv_task = asyncio.create_task(self._recv_asr_result(qwen_ws, stop_event))
                    await asyncio.wait([send_task, recv_task], return_when=asyncio.ALL_COMPLETED)

            await self._finalize()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Local live ASR run failed meeting_id=%s session_id=%s", self._meeting_id, self._session_id)
            asr_session.status = "failed"
            asr_session.transcript_text = "".join(self._final_parts)
            asr_session.error_msg = str(exc)
            self._db.commit()
            if self._ws_alive:
                await self._ws.send_json({"type": "error", "message": str(exc)})
            raise

    async def _forward_audio(self, qwen_ws, stop_event: asyncio.Event) -> None:
        # 前端到 Qwen 的上行链路：控制消息负责 stop/config，二进制消息负责真正音频内容。
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
                if text and self._ws_alive:
                    await self._ws.send_json(
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
                if text and self._ws_alive:
                    await self._ws.send_json(
                        {"type": "final", "text": text, "accumulated": "".join(self._final_parts)}
                    )
            elif event_type == "session.finished":
                break
        stop_event.set()

    async def _finalize(self) -> None:
        # 收尾阶段必须串行完成：
        # 1. 更新 ASR session；
        # 2. 落本地 WAV；
        # 3. 上传对象存储；
        # 4. 回填音频与会话关联。
        transcript = "".join(self._final_parts)
        asr_session = (
            self._db.query(database.LocalAsrSession)
            .filter(database.LocalAsrSession.id == self._session_id)
            .first()
        )
        if not asr_session:
            raise RuntimeError("ASR 会话不存在")
        if not self._audio_chunks:
            raise RuntimeError("未接收到任何音频数据，无法生成录音文件")

        wav_dir = Path(settings.QWEN_ASR_AUDIO_SAVE_DIR or os.path.join(settings.UPLOAD_DIR, "local_asr_recordings"))
        wav_dir.mkdir(parents=True, exist_ok=True)
        wav_path = wav_dir / f"meeting_{self._meeting_id}_session_{self._session_id}.wav"
        logger.info(
            "开始落盘本地实时录音 meeting_id=%s session_id=%s wav_path=%s",
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
        self._db.commit()

        if self._ws_alive:
            await self._ws.send_json({"type": "saving_audio", "session_id": self._session_id})

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
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("本地实时录音临时 WAV 清理失败 path=%s", wav_path)

        asr_session.source_audio_id = audio_record.id
        audio_record.source_asr_session_id = asr_session.id
        audio_record.transcript_text = transcript
        self._db.commit()
        logger.info(
            "本地实时 ASR 会话完成 meeting_id=%s session_id=%s audio_id=%s duration=%.3f",
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
                    "stream_transcript_text": transcript,
                    "audio_uploaded": True,
                    "duration_seconds": duration,
                }
            )


local_meeting_minute_service = LocalMeetingMinuteService()
