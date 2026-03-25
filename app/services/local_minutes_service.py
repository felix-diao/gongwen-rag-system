"""
本地会议纪要服务：TOS 对象存储 + LLM 生成摘要/Todos + CRUD。

复用 VOLC_TOS 的 endpoint / region / access key，并写入同一 bucket（VOLC_TOS_BUCKET）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import settings
from app.models import database, schemas2
from app.utils.logger import get_logger

try:
    import tos  # type: ignore
except ImportError:
    tos = None

logger = get_logger("local_minutes_service")


# ─── TOS 上传器（复用 volc TOS 基础设施，bucket 不同）─────────────────────────

class _LocalTosUploader:
    def __init__(self) -> None:
        bucket = settings.VOLC_TOS_BUCKET
        if not bucket:
            raise ValueError("VOLC_TOS_BUCKET is not configured")
        if tos is None:
            raise RuntimeError("ve-tos-python-sdk is required; install ve-tos-python-sdk")
        endpoint = (settings.VOLC_TOS_ENDPOINT or "").rstrip("/")
        region = settings.VOLC_TOS_REGION or ""
        ak = settings.VOLC_TOS_ACCESS_KEY_ID or ""
        sk = settings.VOLC_TOS_SECRET_ACCESS_KEY or ""
        if not endpoint or not region or not ak or not sk:
            raise ValueError("VOLC_TOS_ENDPOINT/REGION/ACCESS_KEY_ID/SECRET_ACCESS_KEY must be set")
        self._bucket = bucket
        self._endpoint = endpoint
        parsed = urlparse(endpoint)
        self._scheme = parsed.scheme or "https"
        self._host = parsed.netloc or ""
        self._client = tos.TosClientV2(ak, sk, endpoint, region)

    def upload_file(self, source: Path, object_key: str, content_type: Optional[str]) -> str:
        part_size = 20 * 1024 * 1024
        task_num = 8
        try:
            size = source.stat().st_size
            min_ps = size // 9000
            if min_ps > part_size:
                part_size = ((min_ps // (1024 * 1024)) + 1) * 1024 * 1024
        except OSError:
            size = None
        started = time.monotonic()
        self._client.upload_file(
            bucket=self._bucket, key=object_key,
            file_path=str(source), content_type=content_type,
            part_size=part_size, task_num=task_num, enable_checkpoint=True,
        )
        elapsed = time.monotonic() - started
        logger.info("LocalTOS upload key=%r elapsed=%.3fs size=%s", object_key, elapsed, size)
        return self._url(object_key)

    def upload_fileobj(self, fileobj, object_key: str, content_type: Optional[str]) -> str:
        suffix = Path(object_key).suffix or ".bin"
        tmp: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                shutil.copyfileobj(fileobj, f)
                tmp = Path(f.name)
            return self.upload_file(tmp, object_key, content_type)
        finally:
            if tmp:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    def download_file(self, object_key: str, dest: Path) -> None:
        logger.info("LocalTOS download key=%r dest=%s", object_key, dest)
        self._client.download_file(
            bucket=self._bucket, key=object_key, file_path=str(dest),
            part_size=20 * 1024 * 1024, task_num=8, enable_checkpoint=True,
        )

    def delete_file(self, object_key: str) -> None:
        logger.info("LocalTOS delete key=%r", object_key)
        self._client.delete_object(self._bucket, object_key)

    def _url(self, key: str) -> str:
        if self._host:
            return f"{self._scheme}://{self._bucket}.{self._host}/{key}"
        return f"{self._endpoint}/{self._bucket}/{key}"


# ─── 服务主体 ────────────────────────────────────────────────────────────────

class LocalMinutesService:
    def __init__(self) -> None:
        self._uploader: Optional[_LocalTosUploader] = None

    def _ensure_uploader(self) -> _LocalTosUploader:
        if self._uploader is None:
            self._uploader = _LocalTosUploader()
        return self._uploader

    @staticmethod
    def _build_object_key(meeting_id: int, original_name: str) -> str:
        ext = Path(original_name).suffix
        return f"meetings-local/{meeting_id}/{uuid4().hex}{ext}"

    # ── TOS 操作 ──────────────────────────────────────────────────────────────

    def upload_audio_fileobj(
        self, db: Session, meeting_id: int,
        upload_file, original_name: str, content_type: Optional[str],
    ) -> database.LocalMeetingAudio:
        uploader = self._ensure_uploader()
        object_key = self._build_object_key(meeting_id, original_name)
        try:
            upload_file.file.seek(0)
        except Exception:
            pass
        stream_path = getattr(upload_file.file, "name", None)
        use_path = isinstance(stream_path, str) and stream_path and os.path.isfile(stream_path)
        if use_path:
            file_url = uploader.upload_file(Path(stream_path), object_key, content_type)
        else:
            file_url = uploader.upload_fileobj(upload_file.file, object_key, content_type)
        record = database.LocalMeetingAudio(
            meeting_id=meeting_id, file_name=original_name,
            object_key=object_key, file_url=file_url,
            file_type=content_type, status="uploaded",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info("Local audio uploaded audio_id=%s meeting_id=%s", record.id, meeting_id)
        return record

    def upload_from_local(
        self, db: Session, meeting_id: int, local_path: Path,
        original_name: str, content_type: Optional[str],
        source_asr_session_id: Optional[int] = None,
    ) -> database.LocalMeetingAudio:
        uploader = self._ensure_uploader()
        object_key = self._build_object_key(meeting_id, original_name)
        file_url = uploader.upload_file(local_path, object_key, content_type)
        record = database.LocalMeetingAudio(
            meeting_id=meeting_id, file_name=original_name,
            object_key=object_key, file_url=file_url,
            file_type=content_type, status="uploaded",
            source_asr_session_id=source_asr_session_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info("Local audio uploaded from local audio_id=%s", record.id)
        return record

    def download_audio(self, object_key: str, dest: Path) -> None:
        self._ensure_uploader().download_file(object_key, dest)

    def delete_audio(self, object_key: str) -> None:
        self._ensure_uploader().delete_file(object_key)

    # ── LLM 生成摘要 + Todos ─────────────────────────────────────────────────

    def generate_minutes_from_transcript(
        self, db: Session, meeting_id: int,
    ) -> Tuple[Optional[database.LocalMeetingSummary], List[database.LocalMeetingTodo]]:
        """取该会议最新音频的转写文本，调用 LLM 生成摘要 + Todos。"""
        latest_audio = (
            db.query(database.LocalMeetingAudio)
            .filter(database.LocalMeetingAudio.meeting_id == meeting_id)
            .order_by(database.LocalMeetingAudio.updated_at.desc())
            .first()
        )
        audio = (
            db.query(database.LocalMeetingAudio)
            .filter(
                database.LocalMeetingAudio.meeting_id == meeting_id,
                database.LocalMeetingAudio.transcript_text.isnot(None),
            )
            .order_by(database.LocalMeetingAudio.updated_at.desc())
            .first()
        )
        if not audio or not audio.transcript_text:
            if latest_audio:
                latest_audio.status = "failed"
                db.commit()
            raise ValueError("该会议尚无转写文本，请先完成录音或音频转写")

        meeting = db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
        meeting_title = meeting.title if meeting else "会议"
        transcript = audio.transcript_text

        payload = self._call_llm(meeting_title, transcript)
        if not payload:
            audio.status = "failed"
            db.commit()
            raise RuntimeError("LLM 生成会议纪要失败，请重试")

        # 清旧
        db.query(database.LocalMeetingSummary).filter(
            database.LocalMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingTodo).filter(
            database.LocalMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.flush()

        # 写摘要
        summary_raw = payload.get("summary") or {}
        if isinstance(summary_raw, str):
            summary_raw = {"paragraph": summary_raw}
        summary_record = database.LocalMeetingSummary(
            meeting_id=meeting_id,
            source_audio_id=audio.id,
            title=summary_raw.get("title") or meeting_title,
            paragraph=summary_raw.get("paragraph") or summary_raw.get("content") or "",
        )
        db.add(summary_record)

        # 写 Todos
        todo_records: List[database.LocalMeetingTodo] = []
        for item in payload.get("todos") or []:
            if isinstance(item, str):
                item = {"content": item}
            content = item.get("content") or item.get("description") or ""
            if not content:
                continue
            todo = database.LocalMeetingTodo(
                meeting_id=meeting_id,
                source_audio_id=audio.id,
                content=content,
                executor=item.get("executor") or item.get("owner"),
                execution_time=item.get("execution_time") or item.get("deadline"),
            )
            db.add(todo)
            todo_records.append(todo)

        audio.status = "completed"
        db.commit()
        db.refresh(summary_record)
        for t in todo_records:
            db.refresh(t)
        logger.info("Local minutes generated meeting_id=%s summary_len=%d todos=%d",
                     meeting_id, len(summary_record.paragraph), len(todo_records))
        return summary_record, todo_records

    def _call_llm(self, meeting_title: str, transcript: str) -> Optional[dict]:
        from app.llm_client.generators import get_client
        instruction = (
            "你是企业会议纪要助手。请基于转写文本产出“可直接入库”的会议纪要 JSON。\n"
            "你必须只输出一个合法 JSON 对象，不要输出 Markdown 代码块，不要输出解释性文字。\n\n"
            "【输出 JSON Schema】\n"
            "{\n"
            '  "summary": {\n'
            '    "title": "string，简短会议标题",\n'
            '    "paragraph": "string，中文结构化摘要（使用 Markdown）"\n'
            "  },\n"
            '  "todos": [\n'
            "    {\n"
            '      "content": "string，明确可执行动作（必须有动词）",\n'
            '      "executor": "string|null，负责人，不确定填 null",\n'
            '      "execution_time": "string|null，截止时间或时间点，不确定填 null"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "【规则】\n"
            "1) 所有字段必须存在，summary/title/summary/paragraph 不能为空字符串。\n"
            "2) todos 必须是数组；没有明确待办时返回 []。\n"
            "3) 不得编造人名、时间、结论；不确定就写 null 或不写入该待办。\n"
            "4) paragraph 必须使用结构化 Markdown，格式尽量如下：\n"
            '   "先写 1 行总述\\n\\n'
            '   **讨论要点：**\\n'
            '   - 要点1\\n'
            '   - 要点2\\n\\n'
            '   **结论：**\\n'
            '   - 结论1\\n\\n'
            '   **风险与建议：**\\n'
            '   - 风险或建议1"\n'
            "5) paragraph 不少于 120 字，优先使用短句 + 列表，避免长段落堆叠。\n"
        )
        user_msg = f"会议标题：{meeting_title}\n\n会议转写文本：\n{transcript}"
        try:
            cli = get_client()
            raw = cli.chat(
                [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=2000,
            )
            payload = self._parse_json(raw)
            if payload:
                return self._normalize_payload(payload, meeting_title, transcript)

            # 二次修复：将首次输出修正为严格 JSON
            repair_prompt = (
                "将下面内容修正为合法 JSON，且严格遵循给定 schema。"
                "只输出 JSON 对象，不要其它文本。\n\n"
                "schema:\n"
                '{"summary":{"title":"string","paragraph":"string"},"todos":[{"content":"string","executor":"string|null","execution_time":"string|null"}]}\n\n'
                f"待修正内容:\n{raw}"
            )
            repaired_raw = cli.chat(
                [
                    {"role": "system", "content": "你是 JSON 修复器，只输出合法 JSON 对象。"},
                    {"role": "user", "content": repair_prompt},
                ],
                max_tokens=1600,
            )
            repaired_payload = self._parse_json(repaired_raw)
            if repaired_payload:
                return self._normalize_payload(repaired_payload, meeting_title, transcript)
            return None
        except Exception as exc:
            logger.exception("LLM generate minutes failed: %s", exc)
            return None

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        if not raw:
            return None
        cleaned = raw.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(0)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("JSON parse failed: %s", raw[:500])
            return None

    @staticmethod
    def _normalize_payload(payload: dict, meeting_title: str, transcript: str) -> dict:
        summary = payload.get("summary")
        if isinstance(summary, str):
            summary = {"title": meeting_title, "paragraph": summary}
        if not isinstance(summary, dict):
            summary = {}

        title = str(summary.get("title") or "").strip() or meeting_title
        paragraph = str(summary.get("paragraph") or summary.get("content") or "").strip()
        if not paragraph:
            paragraph = LocalMinutesService._fallback_summary(transcript)
        paragraph = LocalMinutesService._ensure_structured_markdown_summary(paragraph, meeting_title)

        todos_raw = payload.get("todos")
        todos_list: List[dict] = []
        if isinstance(todos_raw, list):
            for item in todos_raw:
                normalized = LocalMinutesService._normalize_todo_item(item)
                if normalized:
                    todos_list.append(normalized)

        return {
            "summary": {
                "title": title,
                "paragraph": paragraph,
            },
            "todos": todos_list,
        }

    @staticmethod
    def _ensure_structured_markdown_summary(paragraph: str, meeting_title: str) -> str:
        text = (paragraph or "").replace("\r\n", "\n").strip()
        if not text:
            return text

        # 已是结构化 Markdown（列表/加粗/编号）则原样保留
        if re.search(r"(^|\n)\s*[-*]\s+|(^|\n)\s*\d+\.\s+|\*\*[^*]+\*\*", text):
            return text

        compact = re.sub(r"\s+", " ", text).strip()
        sentences = [s.strip() for s in re.split(r"(?<=[。！？!?])\s*", compact) if s.strip()]
        if not sentences:
            sentences = [compact]

        key_points = sentences[:3]
        conclusion = sentences[3] if len(sentences) > 3 else sentences[-1]
        suggestions = sentences[4:6]

        lines: List[str] = [
            f"围绕“{meeting_title}”开展讨论，形成以下摘要：",
            "",
            "**讨论要点：**",
        ]
        lines.extend([f"- {item}" for item in key_points])
        lines.extend(["", "**结论：**", f"- {conclusion}"])
        if suggestions:
            lines.append("")
            lines.append("**风险与建议：**")
            lines.extend([f"- {item}" for item in suggestions])
        return "\n".join(lines)

    @staticmethod
    def _normalize_todo_item(item: Any) -> Optional[dict]:
        if isinstance(item, str):
            content = item.strip()
            if not content:
                return None
            return {"content": content, "executor": None, "execution_time": None}
        if not isinstance(item, dict):
            return None

        content = str(item.get("content") or item.get("description") or "").strip()
        if not content:
            return None
        executor = item.get("executor") or item.get("owner")
        execution_time = item.get("execution_time") or item.get("deadline")

        return {
            "content": content,
            "executor": str(executor).strip() if isinstance(executor, str) and executor.strip() else None,
            "execution_time": (
                str(execution_time).strip()
                if isinstance(execution_time, str) and execution_time.strip()
                else None
            ),
        }

    @staticmethod
    def _fallback_summary(transcript: str) -> str:
        cleaned = re.sub(r"\s+", " ", (transcript or "")).strip()
        if not cleaned:
            return "本次会议已完成转写，但暂未提取到可用摘要，请稍后重试生成。"
        clipped = cleaned[:260]
        suffix = "..." if len(cleaned) > 300 else ""
        return (
            "基于当前转写内容，先给出自动兜底摘要：\n\n"
            "**讨论要点：**\n"
            f"- {clipped}{suffix}\n\n"
            "**结论：**\n"
            "- 建议补充更完整转写后再次生成，以获得更精准的结构化纪要。\n\n"
            "**风险与建议：**\n"
            "- 当前摘要为兜底结果，可能遗漏上下文细节。"
        )

    # ── 查询 ─────────────────────────────────────────────────────────────────

    def get_minutes(self, db: Session, meeting_id: int) -> schemas2.LocalMeetingMinutesResponse:
        summary = (
            db.query(database.LocalMeetingSummary)
            .filter(database.LocalMeetingSummary.meeting_id == meeting_id)
            .first()
        )
        todos = (
            db.query(database.LocalMeetingTodo)
            .filter(database.LocalMeetingTodo.meeting_id == meeting_id)
            .order_by(database.LocalMeetingTodo.id.asc())
            .all()
        )
        latest_audio = (
            db.query(database.LocalMeetingAudio)
            .filter(
                database.LocalMeetingAudio.meeting_id == meeting_id,
                database.LocalMeetingAudio.transcript_text.isnot(None),
            )
            .order_by(database.LocalMeetingAudio.updated_at.desc())
            .first()
        )
        if latest_audio is None:
            latest_audio = (
                db.query(database.LocalMeetingAudio)
                .filter(database.LocalMeetingAudio.meeting_id == meeting_id)
                .order_by(database.LocalMeetingAudio.updated_at.desc())
                .first()
            )

        transcript_text = latest_audio.transcript_text if latest_audio else None

        stream_transcript_text: Optional[str] = None
        if latest_audio and latest_audio.source_asr_session_id:
            asr_session = (
                db.query(database.LocalAsrSession)
                .filter(database.LocalAsrSession.id == latest_audio.source_asr_session_id)
                .first()
            )
            if asr_session and asr_session.transcript_text:
                stream_transcript_text = asr_session.transcript_text

        audio_status = latest_audio.status if latest_audio else None

        return schemas2.LocalMeetingMinutesResponse(
            transcript_text=transcript_text,
            stream_transcript_text=stream_transcript_text,
            audio_status=audio_status,
            summary=schemas2.LocalMeetingSummaryInDB.model_validate(summary) if summary else None,
            todos=[schemas2.LocalMeetingTodoInDB.model_validate(t) for t in todos],
        )

    # ── 清空 ─────────────────────────────────────────────────────────────────

    def clear_minutes(self, db: Session, meeting_id: int) -> None:
        db.query(database.LocalMeetingSummary).filter(
            database.LocalMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingTodo).filter(
            database.LocalMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.LocalMeetingAudio).filter(
            database.LocalMeetingAudio.meeting_id == meeting_id
        ).update({"transcript_text": None}, synchronize_session=False)
        db.commit()
        logger.info("Cleared local minutes meeting_id=%s", meeting_id)

    # ── Summary CRUD ─────────────────────────────────────────────────────────

    def update_summary(
        self, db: Session, meeting_id: int, payload: schemas2.LocalMeetingSummaryCreate,
    ) -> database.LocalMeetingSummary:
        summary = (
            db.query(database.LocalMeetingSummary)
            .filter(database.LocalMeetingSummary.meeting_id == meeting_id)
            .first()
        )
        if summary:
            summary.paragraph = payload.paragraph
            if payload.title is not None:
                summary.title = payload.title
        else:
            summary = database.LocalMeetingSummary(
                meeting_id=meeting_id, paragraph=payload.paragraph,
                title=payload.title, source_audio_id=payload.source_audio_id,
            )
            db.add(summary)
        db.commit()
        db.refresh(summary)
        return summary

    # ── Todo CRUD ────────────────────────────────────────────────────────────

    def create_todo(
        self, db: Session, meeting_id: int, payload: schemas2.LocalMeetingTodoCreate,
    ) -> database.LocalMeetingTodo:
        todo = database.LocalMeetingTodo(
            meeting_id=meeting_id, content=payload.content,
            executor=payload.executor, execution_time=payload.execution_time,
            source_audio_id=payload.source_audio_id,
        )
        db.add(todo)
        db.commit()
        db.refresh(todo)
        return todo

    def update_todo(
        self, db: Session, meeting_id: int, todo_id: int,
        payload: schemas2.LocalMeetingTodoCreate,
    ) -> Optional[database.LocalMeetingTodo]:
        todo = (
            db.query(database.LocalMeetingTodo)
            .filter(database.LocalMeetingTodo.id == todo_id, database.LocalMeetingTodo.meeting_id == meeting_id)
            .first()
        )
        if not todo:
            return None
        todo.content = payload.content
        if payload.executor is not None:
            todo.executor = payload.executor
        if payload.execution_time is not None:
            todo.execution_time = payload.execution_time
        db.commit()
        db.refresh(todo)
        return todo

    def delete_todo(self, db: Session, meeting_id: int, todo_id: int) -> bool:
        todo = (
            db.query(database.LocalMeetingTodo)
            .filter(database.LocalMeetingTodo.id == todo_id, database.LocalMeetingTodo.meeting_id == meeting_id)
            .first()
        )
        if not todo:
            return False
        db.delete(todo)
        db.commit()
        return True


local_minutes_service = LocalMinutesService()
