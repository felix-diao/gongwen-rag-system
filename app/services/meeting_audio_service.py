"""统一会议音频服务。

这个模块是会议音频资产操作的唯一入口。

核心职责：
1. 校验 provider、会议归属和上传文件类型。
2. 负责对象存储上传、下载、删除。
3. 维护进程内异步上传任务注册表。
4. 把 ORM 记录转换成前端统一使用的音频响应模型。

注意：
1. 上传任务注册表是进程内内存态，适合当前单进程/开发阶段；若后续要做多实例部署，需要迁移到外部任务系统。
2. 纪要服务不要直接调用对象存储 SDK，而应统一复用这里的入口。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional, Tuple, cast
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session
import tos  # type: ignore

from app.config import settings
from app.models import database, schemas
from app.utils.logger import get_logger

logger = get_logger("meeting_audio_service")

Provider = Literal["local", "volc"]
MAX_LOCAL_MINUTES_AUDIOS_PER_MEETING = 10
MAX_VOLC_MINUTES_AUDIOS_PER_MEETING = 10


@dataclass
class _UploadTask:
    """进程内上传任务快照。

    这里只描述任务当前状态，不直接持久化到数据库。
    """
    task_id: str
    provider: Provider
    meeting_id: int
    creator_id: Optional[str]
    file_name: str
    content_type: Optional[str]
    status: str
    audio_id: Optional[int]
    error_msg: Optional[str]
    created_at: datetime
    updated_at: datetime


class _MeetingTosUploader:
    """
    meeting_audio_service 专属对象存储客户端（固定使用 ve-tos-python-sdk）。

    约定：
    - 只负责对象存储读写，不处理数据库。
    - upload_file 返回可对外访问的 URL（用于落库给前端展示/下载）。
    """

    def __init__(self) -> None:
        # 启动即做配置硬校验，避免运行到上传阶段才发现缺配置。
        bucket = settings.VOLC_TOS_BUCKET
        endpoint = (settings.VOLC_TOS_ENDPOINT or "").rstrip("/")
        region = settings.VOLC_TOS_REGION or ""
        access_key = settings.VOLC_TOS_ACCESS_KEY_ID or ""
        secret_key = settings.VOLC_TOS_SECRET_ACCESS_KEY or ""
        if not bucket or not endpoint or not region or not access_key or not secret_key:
            raise ValueError(
                "VOLC_TOS_BUCKET/VOLC_TOS_ENDPOINT/VOLC_TOS_REGION/"
                "VOLC_TOS_ACCESS_KEY_ID/VOLC_TOS_SECRET_ACCESS_KEY 必须配置"
            )
        self._bucket = bucket
        self._endpoint = endpoint
        self._region = region
        self._public_base = settings.VOLC_TOS_PUBLIC_BASE.rstrip("/") if settings.VOLC_TOS_PUBLIC_BASE else ""
        parsed = urlparse(endpoint)
        self._scheme = parsed.scheme or "https"
        self._host = parsed.netloc or ""
        self._client = tos.TosClientV2(access_key, secret_key, endpoint, region)

    def _build_public_url(self, object_key: str) -> str:
        # 优先使用显式配置的公网前缀；未配置时按 bucket-host 规则拼接。
        if self._public_base:
            return f"{self._public_base}/{object_key}"
        if self._host:
            return f"{self._scheme}://{self._bucket}.{self._host}/{object_key}"
        return f"{self._endpoint}/{self._bucket}/{object_key}"

    def upload_file(self, source: Path, object_key: str, content_type: Optional[str]) -> str:
        self._client.upload_file(
            bucket=self._bucket,
            key=object_key,
            file_path=str(source),
            content_type=content_type,
            part_size=20 * 1024 * 1024,
            task_num=8,
            enable_checkpoint=True,
        )
        return self._build_public_url(object_key)

    def download_file(self, object_key: str, dest: Path) -> None:
        self._client.download_file(
            bucket=self._bucket,
            key=object_key,
            file_path=str(dest),
            part_size=20 * 1024 * 1024,
            task_num=8,
            enable_checkpoint=True,
        )

    def delete_file(self, object_key: str) -> None:
        self._client.delete_object(self._bucket, object_key)


class MeetingAudioService:
    """
    会议音频领域服务（唯一音频 CRUD 入口）。

    设计约束：
    1) 控制器层不直接访问音频表，不直接调用对象存储 SDK。
    2) local / volc 两类音频通过 provider 参数统一处理。
    3) 音频文件最终存放于对象存储；数据库只保存元数据与状态。
    """

    def __init__(self) -> None:
        # uploader 懒加载：首次上传/下载/删除时初始化，后续复用。
        self._uploader: Optional[_MeetingTosUploader] = None
        # 上传任务注册表（用于 local/volc 共用的异步上传任务查询）。
        self._upload_tasks: dict[str, _UploadTask] = {}
        self._upload_lock = threading.Lock()

    @staticmethod
    def _assert_meeting_exists(db: Session, meeting_id: int) -> None:
        exists = db.execute(
            text("SELECT 1 FROM meetings WHERE id = :meeting_id LIMIT 1"),
            {"meeting_id": meeting_id},
        ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="会议未找到")

    @staticmethod
    def _assert_upload_quota(db: Session, meeting_id: int, provider: Provider) -> None:
        upload_count = (
            db.query(database.MeetingAudio)
            .filter(
                database.MeetingAudio.meeting_id == meeting_id,
                database.MeetingAudio.provider == provider,
            )
            .count()
        )
        if provider == "local" and upload_count >= MAX_LOCAL_MINUTES_AUDIOS_PER_MEETING:
            raise HTTPException(
                status_code=400,
                detail=f"每个会议最多上传 {MAX_LOCAL_MINUTES_AUDIOS_PER_MEETING} 个本地AI音频，请先删除旧音频后再上传",
            )
        if provider == "volc" and upload_count >= MAX_VOLC_MINUTES_AUDIOS_PER_MEETING:
            raise HTTPException(
                status_code=400,
                detail=f"每个会议最多上传 {MAX_VOLC_MINUTES_AUDIOS_PER_MEETING} 个火山音频，请先删除旧音频后再上传",
            )

    @staticmethod
    def _validate_content_type(content_type: Optional[str]) -> str:
        raw = (content_type or "").strip().lower()
        if not raw:
            raise HTTPException(status_code=400, detail="音频 MIME 类型不能为空")
        if raw.startswith("audio/") or raw.startswith("video/"):
            return raw
        raise HTTPException(status_code=400, detail=f"不支持的 MIME 类型: {content_type}")

    @staticmethod
    def normalize_provider(provider: str) -> Provider:
        raw = (provider or "").strip().lower()
        if raw not in {"local", "volc"}:
            raise HTTPException(status_code=400, detail="provider 仅支持 local 或 volc")
        return cast(Provider, raw)

    @staticmethod
    def _to_schema(record: database.MeetingAudio) -> schemas.MeetingAudioInDB:
        return schemas.MeetingAudioInDB(
            provider=cast(Provider, record.provider),
            id=record.id,
            meeting_id=record.meeting_id,
            creator_id=record.creator_id,
            file_name=record.file_name,
            object_key=record.object_key,
            file_url=record.file_url,
            file_type=record.file_type,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _get_uploader(self) -> _MeetingTosUploader:
        if self._uploader is None:
            self._uploader = _MeetingTosUploader()
        return self._uploader

    @staticmethod
    def _get_audio_record(
        db: Session,
        meeting_id: int,
        provider: Provider,
        audio_id: int,
    ) -> database.MeetingAudio:
        record = (
            db.query(database.MeetingAudio)
            .filter(
                database.MeetingAudio.id == audio_id,
                database.MeetingAudio.meeting_id == meeting_id,
                database.MeetingAudio.provider == provider,
            )
            .first()
        )
        if not record:
            raise HTTPException(status_code=404, detail="音频记录未找到")
        return record

    def _save_upload_to_temp(self, upload_file: UploadFile) -> Path:
        """把上传流写入临时文件，供后台线程安全复用。

        背景：
        FastAPI 的 `UploadFile.file` 生命周期受请求上下文影响，
        因此异步上传任务不能直接把这个文件句柄交给后台线程，需要先落本地临时文件。
        """
        suffix = Path(upload_file.filename or "audio").suffix or ".tmp"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            shutil.copyfileobj(upload_file.file, tmp_file)
            temp_path = Path(tmp_file.name)
        upload_file.file.seek(0)
        return temp_path

    def create_audio_from_path(
        self,
        db: Session,
        meeting_id: int,
        provider: Provider,
        creator_id: Optional[str],
        source_path: Path,
        file_name: str,
        content_type: Optional[str],
    ) -> database.MeetingAudio:
        self._assert_meeting_exists(db, meeting_id)
        if not source_path.exists() or not source_path.is_file():
            raise HTTPException(status_code=400, detail="本地音频文件不存在")
        if not file_name:
            raise HTTPException(status_code=400, detail="音频文件名不能为空")
        normalized_content_type = self._validate_content_type(content_type)
        self._assert_upload_quota(db, meeting_id, provider)

        suffix = Path(file_name).suffix or source_path.suffix or ".bin"
        object_key = f"{meeting_id}/{provider}/{uuid4().hex}{suffix}"
        file_url = self._get_uploader().upload_file(source_path, object_key, normalized_content_type)
        audio_cls = database.LocalMeetingAudio if provider == "local" else database.VolcMeetingAudio
        record = audio_cls(
            meeting_id=meeting_id,
            provider=provider,
            creator_id=creator_id,
            file_name=file_name,
            object_key=object_key,
            file_url=file_url,
            file_type=normalized_content_type,
            status="uploaded",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    def _build_upload_task_schema(
        self,
        task: _UploadTask,
        audio: Optional[schemas.MeetingAudioInDB] = None,
    ) -> schemas.MeetingAudioUploadTask:
        return schemas.MeetingAudioUploadTask(
            provider=task.provider,
            task_id=task.task_id,
            meeting_id=task.meeting_id,
            creator_id=task.creator_id,
            file_name=task.file_name,
            file_type=task.content_type,
            status=task.status,
            audio_id=task.audio_id,
            error_msg=task.error_msg,
            audio=audio,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    def _run_upload_task(
        self,
        task_id: str,
        temp_path: Path,
    ) -> None:
        # 后台线程只关心“把本地临时文件转成正式音频记录”，不处理 HTTP 协议细节。
        with self._upload_lock:
            task = self._upload_tasks.get(task_id)
            if not task:
                return
            task.status = "running"
            task.updated_at = datetime.utcnow()

        db = database.SessionLocal()
        try:
            logger.info(
                "开始执行会议音频上传任务 task_id=%s meeting_id=%s provider=%s temp_path=%s",
                task.task_id,
                task.meeting_id,
                task.provider,
                temp_path,
            )
            audio = self.create_audio_from_path(
                db=db,
                meeting_id=task.meeting_id,
                provider=task.provider,
                creator_id=task.creator_id,
                source_path=temp_path,
                file_name=task.file_name,
                content_type=task.content_type,
            )

            with self._upload_lock:
                task.status = "completed"
                task.audio_id = audio.id
                task.error_msg = None
                task.updated_at = datetime.utcnow()
            logger.info(
                "会议音频上传任务完成 task_id=%s meeting_id=%s provider=%s audio_id=%s",
                task.task_id,
                task.meeting_id,
                task.provider,
                audio.id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "会议音频上传任务失败 task_id=%s meeting_id=%s provider=%s",
                task.task_id,
                task.meeting_id,
                task.provider,
            )
            with self._upload_lock:
                task.status = "failed"
                task.error_msg = str(exc)
                task.updated_at = datetime.utcnow()
        finally:
            db.close()
            temp_path.unlink(missing_ok=True)

    def create_upload_task(
        self,
        db: Session,
        meeting_id: int,
        provider: Provider,
        creator_id: Optional[str],
        upload_file: UploadFile,
    ) -> schemas.MeetingAudioUploadTask:
        if not upload_file or not upload_file.filename:
            raise HTTPException(status_code=400, detail="音频文件不能为空")
        self._assert_meeting_exists(db, meeting_id)
        self._assert_upload_quota(db, meeting_id, provider)
        normalized_content_type = self._validate_content_type(upload_file.content_type)
        temp_path = self._save_upload_to_temp(upload_file)

        now = datetime.utcnow()
        task = _UploadTask(
            task_id=uuid4().hex,
            provider=provider,
            meeting_id=meeting_id,
            creator_id=creator_id,
            file_name=upload_file.filename,
            content_type=normalized_content_type,
            status="pending",
            audio_id=None,
            error_msg=None,
            created_at=now,
            updated_at=now,
        )
        with self._upload_lock:
            self._upload_tasks[task.task_id] = task
        logger.info(
            "已创建会议音频上传任务 task_id=%s meeting_id=%s provider=%s file_name=%s",
            task.task_id,
            meeting_id,
            provider,
            upload_file.filename,
        )

        thread = threading.Thread(
            target=self._run_upload_task,
            args=(task.task_id, temp_path),
            daemon=True,
            name=f"meeting-audio-upload-task-{task.task_id[:8]}",
        )
        thread.start()
        return self._build_upload_task_schema(task)

    def get_upload_task(
        self,
        db: Session,
        task_id: str,
    ) -> Optional[schemas.MeetingAudioUploadTask]:
        # 已完成任务需要回查数据库拿到正式音频记录；未完成任务返回任务快照。
        with self._upload_lock:
            task = self._upload_tasks.get(task_id)
            if not task:
                return None
            snapshot = _UploadTask(**task.__dict__)

        if snapshot.status == "completed" and snapshot.audio_id is not None:
            audio = self.get_audio(db, snapshot.meeting_id, snapshot.provider, snapshot.audio_id)
            return self._build_upload_task_schema(snapshot, audio=audio)
        return self._build_upload_task_schema(snapshot)

    def list_audio(
        self, db: Session, meeting_id: int, provider: Provider
    ) -> list[schemas.MeetingAudioInDB]:
        self._assert_meeting_exists(db, meeting_id)
        records = (
            db.query(database.MeetingAudio)
            .filter(
                database.MeetingAudio.meeting_id == meeting_id,
                database.MeetingAudio.provider == provider,
            )
            .order_by(database.MeetingAudio.created_at.desc(), database.MeetingAudio.id.desc())
            .all()
        )
        logger.info("查询会议音频列表完成，模式=%s，会议ID=%s，数量=%s", provider, meeting_id, len(records))
        return [self._to_schema(item) for item in records]

    def get_audio(
        self, db: Session, meeting_id: int, provider: Provider, audio_id: int
    ) -> schemas.MeetingAudioInDB:
        self._assert_meeting_exists(db, meeting_id)
        record = self._get_audio_record(db, meeting_id, provider, audio_id)
        return self._to_schema(record)

    def download_audio_to_temp(
        self, db: Session, meeting_id: int, provider: Provider, audio_id: int
    ) -> Tuple[Path, str, str]:
        self._assert_meeting_exists(db, meeting_id)
        record = self._get_audio_record(db, meeting_id, provider, audio_id)
        if not record.object_key:
            raise HTTPException(status_code=404, detail="音频文件未存储在对象存储中")
        if not record.file_name:
            raise HTTPException(status_code=404, detail="音频文件名缺失")
        suffix = Path(record.file_name).suffix
        if not suffix:
            raise HTTPException(status_code=400, detail="音频文件后缀缺失")
        if not record.file_type:
            raise HTTPException(status_code=404, detail="音频文件类型缺失")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)
        tmp_file_path = Path(tmp_path)
        logger.info(
            "开始下载会议音频，模式=%s，会议ID=%s，音频ID=%s，对象键=%r，临时文件=%s",
            provider,
            meeting_id,
            audio_id,
            record.object_key,
            tmp_file_path,
        )
        try:
            uploader = self._get_uploader()
            uploader.download_file(record.object_key, tmp_file_path)
        except Exception as exc:
            if tmp_file_path.exists():
                tmp_file_path.unlink()
            logger.error(
                "下载会议音频失败，模式=%s，会议ID=%s，音频ID=%s，对象键=%r，错误=%s",
                provider,
                meeting_id,
                audio_id,
                record.object_key,
                exc,
            )
            raise HTTPException(status_code=502, detail=f"从对象存储下载失败: {exc}") from exc

        return tmp_file_path, record.file_name, record.file_type

    def delete_audio(
        self, db: Session, meeting_id: int, provider: Provider, audio_id: int
    ) -> schemas.MeetingAudioInDB:
        self._assert_meeting_exists(db, meeting_id)
        record = self._get_audio_record(db, meeting_id, provider, audio_id)
        if record.object_key:
            try:
                self._get_uploader().delete_file(record.object_key)
            except Exception as exc:
                logger.error(
                    "删除对象存储音频失败，模式=%s，会议ID=%s，音频ID=%s，对象键=%r，错误=%s",
                    provider,
                    meeting_id,
                    audio_id,
                    record.object_key,
                    exc,
                )
                raise HTTPException(status_code=502, detail=f"删除对象存储音频失败: {exc}") from exc
        data = self._to_schema(record)
        db.delete(record)
        db.commit()
        logger.info("删除会议音频完成，模式=%s，会议ID=%s，音频ID=%s", provider, meeting_id, audio_id)
        return data

    def delete_all_audio_by_meeting(self, db: Session, meeting_id: int) -> None:
        self._assert_meeting_exists(db, meeting_id)
        records = (
            db.query(database.MeetingAudio)
            .filter(database.MeetingAudio.meeting_id == meeting_id)
            .all()
        )
        uploader: Optional[_MeetingTosUploader] = None
        counts = {"local": 0, "volc": 0}

        for record in records:
            if record.provider in counts:
                counts[record.provider] += 1
            provider = cast(Provider, record.provider)
            if record.object_key:
                if uploader is None:
                    uploader = self._get_uploader()
                try:
                    uploader.delete_file(record.object_key)
                except Exception as exc:
                    logger.error(
                        "删除会议音频对象失败，模式=%s，会议ID=%s，音频ID=%s，对象键=%r，错误=%s",
                        provider,
                        meeting_id,
                        record.id,
                        record.object_key,
                        exc,
                    )
                    raise HTTPException(status_code=502, detail=f"删除对象存储音频失败: {exc}") from exc
            db.delete(record)

        db.commit()
        logger.info(
            "删除会议全部音频完成，会议ID=%s，本地数量=%s，火山数量=%s",
            meeting_id,
            counts["local"],
            counts["volc"],
        )


meeting_audio_service = MeetingAudioService()
