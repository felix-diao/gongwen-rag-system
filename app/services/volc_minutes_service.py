import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import requests
from sqlalchemy.orm import Session

from app.config import settings
from app.models import database, schemas2
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger

# 火山引擎 TOS / ASR 域名需绕过系统代理直连，否则在配置了本地代理的环境下会 Connection refused
_VOLC_NO_PROXY_DOMAINS = "volces.com,bytedance.com,openspeech.bytedance.com"
for _env_key in ("NO_PROXY", "no_proxy"):
    _existing = os.environ.get(_env_key, "")
    _missing = [d for d in _VOLC_NO_PROXY_DOMAINS.split(",") if d not in _existing]
    if _missing:
        os.environ[_env_key] = ",".join(filter(None, [_existing] + _missing))

try:
    import boto3  # type: ignore
    from botocore.config import Config  # type: ignore
    from boto3.s3.transfer import TransferConfig  # type: ignore
except ImportError:  # pragma: no cover
    boto3 = None
    Config = None
    TransferConfig = None

try:
    import tos  # type: ignore
except ImportError:  # pragma: no cover
    tos = None

logger = get_logger("volc_minutes_service")
MAX_VOLC_MINUTES_AUDIOS_PER_MEETING = 10
MINUTES_STATUS_PROCESSING = "处理中"
MINUTES_STATUS_COMPLETED = "已完成"
MINUTES_STATUS_FAILED = "失败"
SESSION_NO_TIMEZONE = timezone(timedelta(hours=8))


class _TosUploaderBase:
    def upload_file(self, source_path: Path, object_key: str, content_type: Optional[str]) -> str:
        raise NotImplementedError

    def upload_fileobj(self, fileobj, object_key: str, content_type: Optional[str]) -> str:
        raise NotImplementedError

    def download_file(self, object_key: str, dest_path: Path) -> None:
        raise NotImplementedError

    def delete_file(self, object_key: str) -> None:
        raise NotImplementedError


class VolcTosUploaderSDK(_TosUploaderBase):
    def __init__(self) -> None:
        if not settings.VOLC_TOS_BUCKET:
            raise ValueError("VOLC_TOS_BUCKET is not configured")
        if tos is None:
            raise RuntimeError("ve-tos-python-sdk is required for TOS uploads; install ve-tos-python-sdk to proceed")
        endpoint = settings.VOLC_TOS_ENDPOINT.rstrip("/") if settings.VOLC_TOS_ENDPOINT else None
        region = settings.VOLC_TOS_REGION or None
        access_key = settings.VOLC_TOS_ACCESS_KEY_ID or None
        secret_key = settings.VOLC_TOS_SECRET_ACCESS_KEY or None
        if not endpoint or not region or not access_key or not secret_key:
            raise ValueError("VOLC_TOS_ENDPOINT/VOLC_TOS_REGION/VOLC_TOS_ACCESS_KEY_ID/VOLC_TOS_SECRET_ACCESS_KEY must be configured")
        self._bucket = settings.VOLC_TOS_BUCKET
        self._public_base = settings.VOLC_TOS_PUBLIC_BASE.rstrip("/") if settings.VOLC_TOS_PUBLIC_BASE else ""
        self._endpoint = endpoint
        parsed_endpoint = urlparse(endpoint) if endpoint else None
        self._endpoint_scheme = parsed_endpoint.scheme if parsed_endpoint and parsed_endpoint.scheme else "https"
        self._endpoint_host = parsed_endpoint.netloc if parsed_endpoint else ""
        self._client = tos.TosClientV2(access_key, secret_key, endpoint, region)

    def upload_file(self, source_path: Path, object_key: str, content_type: Optional[str]) -> str:
        part_size = 20 * 1024 * 1024
        task_num = 8
        size = None
        try:
            size = source_path.stat().st_size
        except OSError:
            pass

        if size:
            # Ensure at most 9000 parts to be safe (limit is 10000)
            min_part_size = size // 9000
            if min_part_size > part_size:
                part_size = min_part_size
                # Round up to nearest MB
                part_size = ((part_size // (1024 * 1024)) + 1) * 1024 * 1024

        logger.info(
            "TOS(SDK) upload_file bucket=%s key=%r content_type=%s size=%s part_size=%s task_num=%s",
            self._bucket,
            object_key,
            content_type,
            size,
            part_size,
            task_num,
        )
        started = time.monotonic()
        self._client.upload_file(
            bucket=self._bucket,
            key=object_key,
            file_path=str(source_path),
            content_type=content_type,
            part_size=part_size,
            task_num=task_num,
            enable_checkpoint=True,
        )
        elapsed = time.monotonic() - started
        if size and elapsed > 0:
            mbps = (size / (1024 * 1024)) / elapsed
            logger.info("TOS(SDK) upload_file done key=%r elapsed=%.3fs throughput=%.2fMB/s", object_key, elapsed, mbps)
        else:
            logger.info("TOS(SDK) upload_file done key=%r elapsed=%.3fs", object_key, elapsed)
        return self._build_public_url(object_key)

    def upload_fileobj(self, fileobj, object_key: str, content_type: Optional[str]) -> str:
        file_path = getattr(fileobj, "name", None)
        if isinstance(file_path, str) and file_path and os.path.exists(file_path) and os.path.isfile(file_path):
            return self.upload_file(Path(file_path), object_key, content_type)

        # Fallback: persist to temp file for SDK's multipart uploader.
        suffix = Path(object_key).suffix or ".bin"
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                shutil.copyfileobj(fileobj, tmp_file)
                tmp_path = Path(tmp_file.name)
            return self.upload_file(tmp_path, object_key, content_type)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to delete temp upload file: %s", tmp_path)

    def download_file(self, object_key: str, dest_path: Path) -> None:
        try:
            head = self._client.head_object(self._bucket, object_key)
            total_size = head.content_length
        except Exception as e:
            logger.error("TOS head_object failed for key=%r: %s", object_key, e)
            raise

        part_size = 20 * 1024 * 1024
        task_num = 8
        
        # Optimize for smaller files
        if total_size is not None and total_size < part_size:
            task_num = 1
            
        logger.info(
            "TOS(SDK) download_file bucket=%s key=%r dest=%s size=%s part_size=%s task_num=%s",
            self._bucket,
            object_key,
            dest_path,
            total_size,
            part_size,
            task_num,
        )
        started = time.monotonic()
        try:
            self._client.download_file(
                bucket=self._bucket,
                key=object_key,
                file_path=str(dest_path),
                part_size=part_size,
                task_num=task_num,
                enable_checkpoint=True,
            )
        except Exception as e:
             logger.error("TOS download_file failed: %s", e)
             raise

        elapsed = time.monotonic() - started
        logger.info("TOS(SDK) download_file done key=%r elapsed=%.3fs", object_key, elapsed)

    def delete_file(self, object_key: str) -> None:
        logger.info("TOS(SDK) delete_file bucket=%s key=%r", self._bucket, object_key)
        self._client.delete_object(self._bucket, object_key)

    def _build_public_url(self, object_key: str) -> str:
        if self._public_base:
            return f"{self._public_base}/{object_key}"
        if self._endpoint_host:
            return f"{self._endpoint_scheme}://{self._bucket}.{self._endpoint_host}/{object_key}"
        if self._endpoint:
            return f"{self._endpoint}/{self._bucket}/{object_key}"
        return f"/{self._bucket}/{object_key}"


class VolcTosUploader(_TosUploaderBase):
    def __init__(self) -> None:
        if not settings.VOLC_TOS_BUCKET:
            raise ValueError("VOLC_TOS_BUCKET is not configured")
        if boto3 is None:
            raise RuntimeError("boto3 is required for TOS uploads; install boto3 to proceed")
        endpoint = settings.VOLC_TOS_ENDPOINT.rstrip("/") if settings.VOLC_TOS_ENDPOINT else None
        region = settings.VOLC_TOS_REGION or None
        access_key = settings.VOLC_TOS_ACCESS_KEY_ID or None
        secret_key = settings.VOLC_TOS_SECRET_ACCESS_KEY or None
        self._bucket = settings.VOLC_TOS_BUCKET
        self._public_base = settings.VOLC_TOS_PUBLIC_BASE.rstrip("/") if settings.VOLC_TOS_PUBLIC_BASE else ""
        self._endpoint = endpoint
        parsed_endpoint = urlparse(endpoint) if endpoint else None
        self._endpoint_scheme = parsed_endpoint.scheme if parsed_endpoint and parsed_endpoint.scheme else "https"
        self._endpoint_host = parsed_endpoint.netloc if parsed_endpoint else ""
        client_kwargs: Dict[str, Optional[str]] = {
            "endpoint_url": endpoint,
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }
        if Config is not None:
            client_kwargs["config"] = Config(s3={"addressing_style": "virtual"})
        cleaned_kwargs = {k: v for k, v in client_kwargs.items() if v}
        self._client = boto3.client("s3", **cleaned_kwargs)

    def upload_file(self, source_path: Path, object_key: str, content_type: Optional[str]) -> str:
        logger.info("TOS upload bucket=%s key=%r content_type=%s", self._bucket, object_key, content_type)
        extra_args: Dict[str, str] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        transfer_config = None
        if TransferConfig is not None:
            # Favor multipart upload + concurrency for larger files.
            transfer_config = TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                multipart_chunksize=8 * 1024 * 1024,
                max_concurrency=8,
                use_threads=True,
            )
        with source_path.open("rb") as stream:
            upload_kwargs: Dict[str, Dict[str, str]] = {}
            if extra_args:
                upload_kwargs["ExtraArgs"] = extra_args
            if transfer_config is not None:
                upload_kwargs["Config"] = transfer_config
            self._client.upload_fileobj(stream, self._bucket, object_key, **upload_kwargs)
        if self._public_base:
            return f"{self._public_base}/{object_key}"
        if self._endpoint_host:
            return f"{self._endpoint_scheme}://{self._bucket}.{self._endpoint_host}/{object_key}"
        if self._endpoint:
            return f"{self._endpoint}/{self._bucket}/{object_key}"
        return f"/{self._bucket}/{object_key}"

    def upload_fileobj(self, fileobj, object_key: str, content_type: Optional[str]) -> str:
        logger.info("TOS upload(bucket=%s) fileobj key=%r content_type=%s", self._bucket, object_key, content_type)
        extra_args: Dict[str, str] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        transfer_config = None
        if TransferConfig is not None:
            transfer_config = TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                multipart_chunksize=8 * 1024 * 1024,
                max_concurrency=8,
                use_threads=True,
            )
        upload_kwargs: Dict[str, object] = {}
        if extra_args:
            upload_kwargs["ExtraArgs"] = extra_args
        if transfer_config is not None:
            upload_kwargs["Config"] = transfer_config
        self._client.upload_fileobj(fileobj, self._bucket, object_key, **upload_kwargs)
        if self._public_base:
            return f"{self._public_base}/{object_key}"
        if self._endpoint_host:
            return f"{self._endpoint_scheme}://{self._bucket}.{self._endpoint_host}/{object_key}"
        if self._endpoint:
            return f"{self._endpoint}/{self._bucket}/{object_key}"
        return f"/{self._bucket}/{object_key}"

    def download_file(self, object_key: str, dest_path: Path) -> None:
        logger.info("TOS download bucket=%s key=%r dest=%s", self._bucket, object_key, dest_path)
        transfer_config = None
        if TransferConfig is not None:
             transfer_config = TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                multipart_chunksize=8 * 1024 * 1024,
                max_concurrency=8,
                use_threads=True,
            )
        download_kwargs = {}
        if transfer_config is not None:
             download_kwargs["Config"] = transfer_config
        self._client.download_file(self._bucket, object_key, str(dest_path), **download_kwargs)

    def delete_file(self, object_key: str) -> None:
        logger.info("TOS delete bucket=%s key=%r", self._bucket, object_key)
        self._client.delete_object(Bucket=self._bucket, Key=object_key)


class VolcMinutesAPI:
    def __init__(self) -> None:
        base = settings.VOLC_MINUTES_API_BASE or "https://openspeech.bytedance.com"
        self._base_url = base.rstrip("/")
        self._submit_path = settings.VOLC_MINUTES_SUBMIT_PATH or "/api/v3/auc/lark/submit"
        self._query_path = settings.VOLC_MINUTES_QUERY_PATH or "/api/v3/auc/lark/query"
        self._timeout = settings.VOLC_MINUTES_TIMEOUT or 10
        self._app_key = settings.VOLC_MINUTES_APP_KEY
        self._access_key = settings.VOLC_MINUTES_ACCESS_KEY
        self._resource_id = settings.VOLC_MINUTES_RESOURCE_ID or "volc.lark.minutes"
        if not self._app_key or not self._access_key:
            raise ValueError("VOLC_MINUTES_APP_KEY and VOLC_MINUTES_ACCESS_KEY must be configured")
        self._session = requests.Session()
        # requests.Session is not guaranteed thread-safe; serialize access across poller threads.
        self._http_lock = threading.Lock()

    def submit_task(self, payload: Dict) -> Dict:
        request_id = str(uuid4())
        url = f"{self._base_url}{self._submit_path}"
        headers = self._build_headers(request_id)
        try:
            payload_preview = json.dumps(payload, ensure_ascii=False)
        except TypeError:
            payload_preview = str(payload)
        logger.info(
            "Submitting Volc minutes task request_id=%s url=%s payload=%s",
            request_id,
            url,
            payload_preview,
        )
        with self._http_lock:
            response = self._session.post(url, json=payload, headers=headers, timeout=self._timeout)
        self._raise_for_status(response)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"Raw": body}
        data_section = body.get("Data") or {}
        if not isinstance(data_section, dict):
            data_section = {}
        task_id = data_section.get("TaskID")
        status_code = response.headers.get("X-Api-Status-Code")
        api_message = response.headers.get("X-Api-Message")
        x_tt_logid = response.headers.get("X-Tt-Logid")
        logger.info(
            "Volc minutes submit response request_id=%s http_status=%s api_status=%s api_message=%s x_tt_logid=%s body=%s",
            request_id,
            response.status_code,
            status_code,
            api_message,
            x_tt_logid,
            json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body,
        )
        if status_code and status_code != "20000000":
            message = response.headers.get("X-Api-Message") or body.get("Message") or "Volc minutes submit failed"
            raise RuntimeError(f"Volc minutes submit failed: {message}")
        if not task_id:
            raise RuntimeError("Volc minutes submit did not return TaskID")
        return {
            "task_id": task_id,
            "body": body,
            "headers": dict(response.headers),
            "request_id": request_id,
        }

    def query_task(self, task_id: str) -> Dict:
        request_id = str(uuid4())
        url = f"{self._base_url}{self._query_path}"
        headers = self._build_headers(request_id)
        with self._http_lock:
            response = self._session.post(url, json={"TaskID": task_id}, headers=headers, timeout=self._timeout)
        self._raise_for_status(response)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"Raw": body}
        status_code = response.headers.get("X-Api-Status-Code")
        if status_code and status_code not in {"20000000", "20000001", "20000002"}:
            message = response.headers.get("X-Api-Message") or body.get("Message") or "Volc minutes query failed"
            raise RuntimeError(f"Volc minutes query failed: {message}")
        data_section = body.get("Data") or {}
        if not isinstance(data_section, dict):
            data_section = {}
        status = data_section.get("Status")
        if not status:
            logger.warning("Volc minutes query returned no status for task %s", task_id)
        else:
            logger.info(
                "Volc minutes query response request_id=%s task_id=%s http_status=%s api_status=%s api_message=%s status=%s",
                request_id,
                task_id,
                response.status_code,
                status_code,
                response.headers.get("X-Api-Message"),
                status,
            )
        return {
            "body": body,
            "headers": dict(response.headers),
            "request_id": request_id,
        }

    def _build_headers(self, request_id: str) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": self._app_key,
            "X-Api-Access-Key": self._access_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        return headers

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:  # pragma: no cover
            body = {}
            try:
                body = response.json()
            except ValueError:
                body = {"text": response.text}
            raise RuntimeError(f"Volc minutes API error: {body}") from exc


class VolcMinutesService:
    @staticmethod
    def _normalize_minutes_session_status(
        status: Optional[str],
        default: str = MINUTES_STATUS_FAILED,
    ) -> str:
        if not status:
            return MINUTES_STATUS_COMPLETED if default == MINUTES_STATUS_COMPLETED else MINUTES_STATUS_FAILED
        raw = str(status).strip()
        lower = raw.lower()
        if raw == MINUTES_STATUS_FAILED or lower in {"failed", "fail", "error"}:
            return MINUTES_STATUS_FAILED
        if raw == MINUTES_STATUS_COMPLETED or lower in {"completed", "success", "succeeded", "successed", "finished", "done"}:
            return MINUTES_STATUS_COMPLETED
        return MINUTES_STATUS_FAILED

    def __init__(self) -> None:
        self._api = VolcMinutesAPI()
        self._uploader: Optional[_TosUploaderBase] = None
        self._timeout = settings.VOLC_MINUTES_TIMEOUT or 10
        self._poll_lock = threading.Lock()
        self._poll_stop_flags: Dict[int, threading.Event] = {}
        self._poll_threads: Dict[int, threading.Thread] = {}
        self._upload_task_lock = threading.Lock()
        self._upload_tasks: Dict[str, Dict[str, Any]] = {}

    def _set_upload_task(self, task_id: str, **kwargs: Any) -> None:
        with self._upload_task_lock:
            task = self._upload_tasks.get(task_id)
            if not task:
                return
            task.update(kwargs)
            task["updated_at"] = datetime.utcnow()

    def _build_upload_task_schema(self, task: Dict[str, Any]) -> schemas2.VolcAudioUploadTask:
        payload = {
            "task_id": task["task_id"],
            "meeting_id": task["meeting_id"],
            "file_name": task["file_name"],
            "status": task["status"],
            "audio_id": task.get("audio_id"),
            "error_msg": task.get("error_msg"),
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
        }
        return schemas2.VolcAudioUploadTask.model_validate(payload)

    def get_upload_task(self, task_id: str) -> Optional[schemas2.VolcAudioUploadTask]:
        with self._upload_task_lock:
            task = self._upload_tasks.get(task_id)
            if not task:
                return None
            task_copy = dict(task)
        return self._build_upload_task_schema(task_copy)

    def _run_upload_task(
        self,
        task_id: str,
        meeting_id: int,
        upload_path: Path,
        original_name: str,
        content_type: Optional[str],
    ) -> None:
        self._set_upload_task(task_id, status="running", error_msg=None)
        db = database.SessionLocal()
        try:
            record = self.upload_audio(
                db=db,
                meeting_id=meeting_id,
                upload_path=upload_path,
                original_name=original_name,
                content_type=content_type,
            )
            self._set_upload_task(
                task_id,
                status="completed",
                audio_id=record.id,
                error_msg=None,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_upload_task(
                task_id,
                status="failed",
                error_msg=str(exc),
            )
            logger.exception("Async volc audio upload failed task_id=%s meeting_id=%s: %s", task_id, meeting_id, exc)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                upload_path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to remove temp upload file %s: %s", upload_path, exc)

    def start_upload_audio_task(
        self,
        db: Session,
        meeting_id: int,
        upload_file,
        original_name: str,
        content_type: Optional[str],
    ) -> schemas2.VolcAudioUploadTask:
        self._ensure_audio_upload_limit(db, meeting_id)
        temp_path = self.save_upload_to_temp(upload_file)
        task_id = uuid4().hex
        now = datetime.utcnow()
        task_payload: Dict[str, Any] = {
            "task_id": task_id,
            "meeting_id": meeting_id,
            "file_name": original_name,
            "status": "pending",
            "audio_id": None,
            "error_msg": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._upload_task_lock:
            self._upload_tasks[task_id] = task_payload

        thread = threading.Thread(
            target=self._run_upload_task,
            name=f"volc-upload-task-{task_id[:8]}",
            args=(task_id, meeting_id, temp_path, original_name, content_type),
            daemon=True,
        )
        thread.start()
        return self._build_upload_task_schema(task_payload)

    def _ensure_audio_upload_limit(self, db: Session, meeting_id: int) -> None:
        count = (
            db.query(database.VolcMeetingAudio)
            .filter(database.VolcMeetingAudio.meeting_id == meeting_id)
            .count()
        )
        if count >= MAX_VOLC_MINUTES_AUDIOS_PER_MEETING:
            raise ValueError(f"每个会议最多上传 {MAX_VOLC_MINUTES_AUDIOS_PER_MEETING} 个火山音频，请先删除旧音频后再上传")

    def upload_audio(
        self,
        db: Session,
        meeting_id: int,
        upload_path: Path,
        original_name: str,
        content_type: Optional[str],
    ) -> database.VolcMeetingAudio:
        self._ensure_audio_upload_limit(db, meeting_id)
        object_key, file_url = self._upload_to_tos(meeting_id, upload_path, original_name, content_type)
        audio_record = database.VolcMeetingAudio(
            meeting_id=meeting_id,
            file_name=original_name,
            object_key=object_key,
            file_url=file_url,
            file_type=content_type,
            status="uploaded",
        )
        db.add(audio_record)
        db.commit()
        db.refresh(audio_record)
        logger.info("Volc minutes audio uploaded audio_id=%s meeting_id=%s", audio_record.id, meeting_id)
        return audio_record

    def upload_audio_fileobj(
        self,
        db: Session,
        meeting_id: int,
        upload_file,
        original_name: str,
        content_type: Optional[str],
    ) -> database.VolcMeetingAudio:
        self._ensure_audio_upload_limit(db, meeting_id)
        uploader = self._ensure_uploader()
        object_key = self._build_object_key(meeting_id, original_name)

        started = time.monotonic()
        seek_started = time.monotonic()
        try:
            upload_file.file.seek(0)
        except Exception:  # noqa: BLE001
            pass
        seek_elapsed = time.monotonic() - seek_started

        # Prefer SDK's file-path uploader when possible (avoids extra buffering).
        stream_path = getattr(upload_file.file, "name", None)
        use_path = isinstance(stream_path, str) and stream_path and os.path.exists(stream_path) and os.path.isfile(stream_path)
        size = None
        if use_path:
            try:
                size = os.path.getsize(stream_path)
            except OSError:
                size = None

        upload_started = time.monotonic()
        if use_path:
            file_url = uploader.upload_file(Path(stream_path), object_key, content_type)
        else:
            file_url = uploader.upload_fileobj(upload_file.file, object_key, content_type)
        upload_elapsed = time.monotonic() - upload_started
        audio_record = database.VolcMeetingAudio(
            meeting_id=meeting_id,
            file_name=original_name,
            object_key=object_key,
            file_url=file_url,
            file_type=content_type,
            status="uploaded",
        )
        db.add(audio_record)
        db_started = time.monotonic()
        db.commit()
        db.refresh(audio_record)
        db_elapsed = time.monotonic() - db_started
        total_elapsed = time.monotonic() - started
        logger.info(
            "Volc minutes audio uploaded (stream) audio_id=%s meeting_id=%s object_key=%r use_path=%s size=%s seek=%.3fs tos_upload=%.3fs db=%.3fs total=%.3fs",
            audio_record.id,
            meeting_id,
            object_key,
            use_path,
            size,
            seek_elapsed,
            upload_elapsed,
            db_elapsed,
            total_elapsed,
        )
        return audio_record

    def download_audio(self, object_key: str, dest_path: Path) -> None:
        """Download audio file from TOS to local path."""
        self._ensure_uploader().download_file(object_key, dest_path)

    def delete_audio(self, object_key: str) -> None:
        """Delete audio file from TOS."""
        self._ensure_uploader().delete_file(object_key)

    def abandon_audio_task(
        self,
        db: Session,
        audio_id: int,
        meeting_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> database.VolcMeetingAudio:
        query = db.query(database.VolcMeetingAudio).filter(database.VolcMeetingAudio.id == audio_id)
        if meeting_id is not None:
            query = query.filter(database.VolcMeetingAudio.meeting_id == meeting_id)
        audio = query.first()
        if not audio:
            raise ValueError(f"Volc meeting audio {audio_id} not found")

        # 音频一旦上传成功到对象存储，上传状态保持为 uploaded；
        # “作废/中断”仅影响妙记任务，不应污染上传状态。
        audio.status = "uploaded"
        audio.task_id = None
        audio.error_msg = reason or "用户离开页面，任务已作废"
        db.commit()
        db.refresh(audio)

        with self._poll_lock:
            stop_flag = self._poll_stop_flags.get(audio_id)
            if stop_flag:
                stop_flag.set()
        logger.info("Volc minutes task abandoned audio_id=%s meeting_id=%s", audio_id, audio.meeting_id)
        return audio

    def submit_audio(
        self,
        db: Session,
        audio_id: int,
        meeting_id: Optional[int] = None,
        source: Optional[str] = None,
    ) -> database.VolcMeetingAudio:
        query = db.query(database.VolcMeetingAudio).filter(database.VolcMeetingAudio.id == audio_id)
        if meeting_id is not None:
            query = query.filter(database.VolcMeetingAudio.meeting_id == meeting_id)
        audio = query.first()
        if not audio:
            raise ValueError(f"Volc meeting audio {audio_id} not found")
        if audio.status not in {"uploaded", "failed"}:
            logger.info(
                "Volc minutes resubmit requested audio_id=%s meeting_id=%s previous_status=%s previous_task_id=%s",
                audio.id,
                audio.meeting_id,
                audio.status,
                audio.task_id,
            )
        payload = self._build_submit_payload(audio.file_url, audio.file_type)
        try:
            payload_preview = json.dumps(payload, ensure_ascii=False)
        except TypeError:
            payload_preview = str(payload)
        logger.info(
            "Prepared Volc submit payload audio_id=%s meeting_id=%s file_url=%s payload=%s",
            audio.id,
            audio.meeting_id,
            audio.file_url,
            payload_preview,
        )
        submit_result = self._api.submit_task(payload)
        audio.status = "submitted"
        audio.task_id = submit_result["task_id"]
        audio.error_msg = None
        headers = submit_result.get("headers", {})
        logger.info(
            "Volc submit acknowledged audio_id=%s task_id=%s api_status=%s api_message=%s x_tt_logid=%s",
            audio.id,
            audio.task_id,
            headers.get("X-Api-Status-Code"),
            headers.get("X-Api-Message"),
            headers.get("X-Tt-Logid"),
        )
        db.commit()
        db.refresh(audio)
        logger.info("Volc minutes submit succeeded task_id=%s audio_id=%s", audio.task_id, audio.id)

        # Fire-and-forget polling to refresh minutes and notify frontend via websocket.
        # Frontend can update progress bar and then fetch latest minutes via GET /api/minutes/volc/{meeting_id}.
        self.start_polling(audio_id=audio.id, submit_source=source)
        return audio

    def start_polling(
        self,
        audio_id: int,
        minutes_session_id: Optional[int] = None,
        submit_source: Optional[str] = None,
    ) -> None:
        """Start (or restart) a background poller for a volc audio task.

        This will continuously query task status; once completed, it overwrites minutes in DB
        and notifies websocket clients for the meeting.
        """
        with self._poll_lock:
            existing = self._poll_stop_flags.get(audio_id)
            if existing:
                existing.set()
            stop_event = threading.Event()
            self._poll_stop_flags[audio_id] = stop_event
            thread = threading.Thread(
                target=self._poll_loop,
                name=f"volc-minutes-poll-{audio_id}",
                args=(audio_id, stop_event, minutes_session_id, submit_source),
                daemon=True,
            )
            self._poll_threads[audio_id] = thread
            thread.start()

    def _poll_loop(
        self,
        audio_id: int,
        stop_event: threading.Event,
        minutes_session_id: Optional[int] = None,
        submit_source: Optional[str] = None,
    ) -> None:
        poll_interval = 5.0
        max_seconds = 60.0 * 60.0  # 1 hour safety cap
        started = time.monotonic()
        last_status: Optional[str] = None
        last_task_id: Optional[str] = None

        logger.info("Volc minutes poller started audio_id=%s", audio_id)
        while not stop_event.is_set():
            if time.monotonic() - started > max_seconds:
                logger.warning("Volc minutes poller timeout audio_id=%s", audio_id)
                break

            db = database.SessionLocal()
            try:
                audio = (
                    db.query(database.VolcMeetingAudio)
                    .filter(database.VolcMeetingAudio.id == audio_id)
                    .first()
                )
                if not audio:
                    logger.warning("Volc minutes poller audio missing audio_id=%s", audio_id)
                    break
                if str(audio.status or "").lower() in {"abandoned", "cancelled"}:
                    logger.info("Volc minutes poller aborted audio_id=%s status=%s", audio_id, audio.status)
                    break
                if not audio.task_id:
                    logger.info("Volc minutes poller no task_id yet audio_id=%s status=%s", audio_id, audio.status)
                    time.sleep(poll_interval)
                    continue

                updated_audio, completed, minutes = self.refresh_minutes(
                    db,
                    audio_id,
                    minutes_session_id=minutes_session_id,
                    submit_source=submit_source,
                )
                current_status = str(updated_audio.status or "")
                current_task_id = updated_audio.task_id
                meeting_id = updated_audio.meeting_id

                if current_task_id != last_task_id or current_status != last_status:
                    meeting_ws_manager.notify_from_thread(
                        meeting_id,
                        {
                            "type": "volc_minutes_status",
                            "meeting_id": meeting_id,
                            "audio_id": updated_audio.id,
                            "task_id": current_task_id,
                            "status": current_status,
                        },
                    )
                    last_task_id = current_task_id
                    last_status = current_status

                if completed:
                    meeting_ws_manager.notify_from_thread(
                        meeting_id,
                        {
                            "type": "volc_minutes_completed",
                            "meeting_id": meeting_id,
                            "audio_id": updated_audio.id,
                            "task_id": current_task_id,
                            "status": "completed",
                            "refresh": True,
                        },
                    )
                    logger.info("Volc minutes poller completed audio_id=%s task_id=%s", audio_id, current_task_id)
                    break

                if current_status in {"failed", "error"}:
                    meeting_ws_manager.notify_from_thread(
                        meeting_id,
                        {
                            "type": "volc_minutes_failed",
                            "meeting_id": meeting_id,
                            "audio_id": updated_audio.id,
                            "task_id": current_task_id,
                            "status": current_status,
                            "error": updated_audio.error_msg,
                        },
                    )
                    logger.warning(
                        "Volc minutes poller failed audio_id=%s task_id=%s error=%s",
                        audio_id,
                        current_task_id,
                        updated_audio.error_msg,
                    )
                    break
            except Exception as exc:  # noqa: BLE001
                logger.exception("Volc minutes poller error audio_id=%s: %s", audio_id, exc)
            finally:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

            time.sleep(poll_interval)

        with self._poll_lock:
            flag = self._poll_stop_flags.get(audio_id)
            if flag is stop_event:
                self._poll_stop_flags.pop(audio_id, None)
            self._poll_threads.pop(audio_id, None)
        logger.info("Volc minutes poller stopped audio_id=%s", audio_id)

    def upload_and_submit(
        self,
        db: Session,
        meeting_id: int,
        upload_path: Path,
        original_name: str,
        content_type: Optional[str],
    ) -> database.VolcMeetingAudio:
        audio_record = self.upload_audio(
            db=db,
            meeting_id=meeting_id,
            upload_path=upload_path,
            original_name=original_name,
            content_type=content_type,
        )
        return self.submit_audio(db=db, audio_id=audio_record.id)

    def refresh_minutes(
        self,
        db: Session,
        audio_id: int,
        minutes_session_id: Optional[int] = None,
        submit_source: Optional[str] = None,
    ) -> Tuple[database.VolcMeetingAudio, bool, Optional[schemas2.VolcMeetingMinutesResponse]]:
        audio = db.query(database.VolcMeetingAudio).filter(database.VolcMeetingAudio.id == audio_id).first()
        if not audio:
            raise ValueError(f"Volc meeting audio {audio_id} not found")
        if str(audio.status or "").lower() in {"abandoned", "cancelled"}:
            return audio, False, None
        if not audio.task_id:
            raise ValueError(f"Volc meeting audio {audio_id} has no task_id")
        query_result = self._api.query_task(audio.task_id)
        raw_status = ((query_result["body"].get("Data") or {})).get("Status")
        status = str(raw_status).lower() if raw_status else ""
        if status in {"running", "queued", "processing"}:
            audio.status = raw_status or status or "processing"
            db.commit()
            db.refresh(audio)
            self._sync_minutes_session_status(db, minutes_session_id, audio)
            return audio, False, None
        if status in {"failed", "error"}:
            db.refresh(audio)
            if str(audio.status or "").lower() in {"abandoned", "cancelled"}:
                return audio, False, None
            body = query_result["body"].get("Data") or {}
            audio.status = raw_status or "failed"
            audio.error_msg = body.get("ErrMessage") or query_result["body"].get("Message")
            db.commit()
            db.refresh(audio)
            # 仅“完整成功生成纪要”才落会话；失败不创建会话历史。
            return audio, False, None
        if status in {"success", "succeeded", "successed", "finished", "completed"}:
            db.refresh(audio)
            if str(audio.status or "").lower() in {"abandoned", "cancelled"}:
                return audio, False, None
            body = query_result["body"].get("Data") or {}
            result = body.get("Result") or {}
            summary_record, todo_records = self._store_minutes_payload(db, audio, result)
            audio.status = "completed"
            audio.error_msg = None
            db.commit()
            db.refresh(audio)
            if summary_record:
                db.refresh(summary_record)
            for item in todo_records:
                db.refresh(item)
            # 会话历史仅在最终结果出炉后落库：成功落一条。
            self._create_minutes_session(db, audio, submit_source=submit_source)
            minutes = schemas2.VolcMeetingMinutesResponse(
                transcript_text=audio.transcript_text,
                summary=schemas2.VolcMeetingSummaryInDB.model_validate(summary_record) if summary_record else None,
                todos=[schemas2.VolcMeetingTodoInDB.model_validate(item) for item in todo_records],
            )
            return audio, True, minutes
        audio.status = raw_status or status or "unknown"
        db.commit()
        db.refresh(audio)
        self._sync_minutes_session_status(db, minutes_session_id, audio)
        return audio, False, None

    def list_minutes_sessions(
        self,
        db: Session,
        meeting_id: int,
    ) -> List[schemas2.VolcMeetingMinutesSessionInDB]:
        sessions = (
            db.query(database.VolcMeetingMinutesSession)
            .filter(
                database.VolcMeetingMinutesSession.meeting_id == meeting_id,
                database.VolcMeetingMinutesSession.session_no.isnot(None),
                database.VolcMeetingMinutesSession.status.in_(
                    [MINUTES_STATUS_COMPLETED, "completed", "success", "succeeded", "finished"]
                ),
            )
            .order_by(database.VolcMeetingMinutesSession.created_at.asc())
            .all()
        )
        return [self._build_session_schema(item) for item in sessions]

    def get_minutes_session(
        self,
        db: Session,
        meeting_id: int,
        session_id: int,
    ) -> Optional[schemas2.VolcMeetingMinutesSessionInDB]:
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
        payload: schemas2.VolcMeetingMinutesSessionUpdate,
    ) -> Optional[schemas2.VolcMeetingMinutesSessionInDB]:
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
            session.status = self._normalize_minutes_session_status(payload.status, default=session.status)
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
        if "speaker_segments" in fields_set:
            segments_payload = [seg.model_dump() for seg in (payload.speaker_segments or [])]
            session.speaker_segments_json = json.dumps(segments_payload, ensure_ascii=False)
        if "todos" in fields_set:
            todos_payload = [todo.model_dump() for todo in (payload.todos or [])]
            session.todos_json = json.dumps(todos_payload, ensure_ascii=False)

        is_latest_session = self._is_latest_minutes_session(db, meeting_id, session.id)
        if is_latest_session:
            self._apply_latest_session_to_current_minutes(db, session, payload, fields_set)

        db.commit()
        db.refresh(session)
        if is_latest_session:
            self._sync_latest_session_from_current_minutes(db, meeting_id)
            db.refresh(session)
        return self._build_session_schema(session)

    def delete_minutes_session(
        self,
        db: Session,
        meeting_id: int,
        session_id: int,
    ) -> bool:
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

    def get_minutes(self, db: Session, meeting_id: int) -> schemas2.VolcMeetingMinutesResponse:
        summary = (
            db.query(database.VolcMeetingSummary)
            .filter(database.VolcMeetingSummary.meeting_id == meeting_id)
            .first()
        )
        todos = (
            db.query(database.VolcMeetingTodo)
            .filter(database.VolcMeetingTodo.meeting_id == meeting_id)
            .order_by(database.VolcMeetingTodo.id.asc())
            .all()
        )
        # 仅取“当前最新音频”作为纪要视图来源，避免回退到历史 ASR 会话造成流式文本残留。
        latest_audio = (
            db.query(database.VolcMeetingAudio)
            .filter(
                database.VolcMeetingAudio.meeting_id == meeting_id,
            )
            .order_by(database.VolcMeetingAudio.updated_at.desc(), database.VolcMeetingAudio.id.desc())
            .first()
        )

        transcript_text = latest_audio.transcript_text if latest_audio else None

        # 粗 ASR 流式转写：从关联的 VolcAsrSession 取（退出重进后用于恢复流式文本框）
        stream_transcript_text: Optional[str] = None
        if latest_audio and latest_audio.source_asr_session_id:
            asr_session = (
                db.query(database.VolcAsrSession)
                .filter(database.VolcAsrSession.id == latest_audio.source_asr_session_id)
                .first()
            )
            if asr_session and asr_session.transcript_text:
                stream_transcript_text = asr_session.transcript_text

        # 解析说话人分段
        speaker_segments: list = []
        if latest_audio and latest_audio.speaker_transcript:
            try:
                raw_segs = json.loads(latest_audio.speaker_transcript)
                speaker_segments = [schemas2.SpeakerSegment(**seg) for seg in raw_segs]
            except Exception as exc:
                logger.warning("Failed to parse speaker_transcript audio_id=%s: %s", latest_audio.id, exc)

        audio_status = latest_audio.status if latest_audio else None

        return schemas2.VolcMeetingMinutesResponse(
            transcript_text=transcript_text,
            stream_transcript_text=stream_transcript_text,
            audio_status=audio_status,
            speaker_segments=speaker_segments,
            summary=schemas2.VolcMeetingSummaryInDB.model_validate(summary) if summary else None,
            todos=[schemas2.VolcMeetingTodoInDB.model_validate(item) for item in todos],
        )

    def _create_minutes_session(
        self,
        db: Session,
        audio: database.VolcMeetingAudio,
        submit_source: Optional[str] = None,
    ) -> database.VolcMeetingMinutesSession:
        # 强规则：基于已有音频生成时，会话中的流式转写必须为空。
        stream_transcript_text: Optional[str]
        if submit_source == "existing_audio":
            stream_transcript_text = None
        else:
            stream_transcript_text = self._resolve_stream_transcript_text(db, audio)
        summary = (
            db.query(database.VolcMeetingSummary)
            .filter(
                database.VolcMeetingSummary.meeting_id == audio.meeting_id,
                database.VolcMeetingSummary.source_audio_id == audio.id,
            )
            .first()
        )
        todos = (
            db.query(database.VolcMeetingTodo)
            .filter(
                database.VolcMeetingTodo.meeting_id == audio.meeting_id,
                database.VolcMeetingTodo.source_audio_id == audio.id,
            )
            .order_by(database.VolcMeetingTodo.id.asc())
            .all()
        )
        todos_payload: List[Dict[str, Any]] = [
            {
                "content": item.content,
                "executor": item.executor,
                "execution_time": item.execution_time,
                "source_audio_id": item.source_audio_id,
            }
            for item in todos
        ]

        session = database.VolcMeetingMinutesSession(
            meeting_id=audio.meeting_id,
            source_audio_id=audio.id,
            source_asr_session_id=audio.source_asr_session_id,
            volc_task_id=audio.task_id,
            status=self._normalize_minutes_session_status(audio.status),
            error_msg=audio.error_msg,
            stream_transcript_text=stream_transcript_text,
            transcript_text=audio.transcript_text,
            speaker_segments_json=audio.speaker_transcript,
            summary_title=summary.title if summary else None,
            summary_paragraph=summary.paragraph if summary else None,
            todos_json=json.dumps(todos_payload, ensure_ascii=False),
        )
        session.session_no = self._build_unique_session_no(db, audio.meeting_id)
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    def _sync_minutes_session_status(
        self,
        db: Session,
        session_id: Optional[int],
        audio: database.VolcMeetingAudio,
    ) -> None:
        if not session_id:
            return
        session = (
            db.query(database.VolcMeetingMinutesSession)
            .filter(database.VolcMeetingMinutesSession.id == session_id)
            .first()
        )
        if not session:
            return
        session.status = self._normalize_minutes_session_status(audio.status, default=session.status)
        session.error_msg = audio.error_msg
        session.volc_task_id = audio.task_id
        session.stream_transcript_text = self._resolve_stream_transcript_text(db, audio)
        session.transcript_text = audio.transcript_text
        db.commit()

    def _sync_minutes_session_payload(
        self,
        db: Session,
        session_id: Optional[int],
        audio: database.VolcMeetingAudio,
        summary_record: Optional[database.VolcMeetingSummary],
        todo_records: List[database.VolcMeetingTodo],
    ) -> None:
        if not session_id:
            return
        session = (
            db.query(database.VolcMeetingMinutesSession)
            .filter(database.VolcMeetingMinutesSession.id == session_id)
            .first()
        )
        if not session:
            return
        session.status = self._normalize_minutes_session_status(audio.status, default=MINUTES_STATUS_COMPLETED)
        session.error_msg = audio.error_msg
        session.volc_task_id = audio.task_id
        session.transcript_text = audio.transcript_text
        session.speaker_segments_json = audio.speaker_transcript

        if summary_record:
            session.summary_title = summary_record.title
            session.summary_paragraph = summary_record.paragraph
        else:
            session.summary_title = None
            session.summary_paragraph = None

        todos_payload: List[Dict[str, Any]] = []
        for item in todo_records:
            todos_payload.append(
                {
                    "content": item.content,
                    "executor": item.executor,
                    "execution_time": item.execution_time,
                    "source_audio_id": item.source_audio_id,
                }
            )
        session.todos_json = json.dumps(todos_payload, ensure_ascii=False)
        db.commit()

    def _build_session_schema(
        self,
        item: database.VolcMeetingMinutesSession,
    ) -> schemas2.VolcMeetingMinutesSessionInDB:
        speaker_segments: List[schemas2.SpeakerSegment] = []
        for seg in self._safe_load_json(item.speaker_segments_json, []):
            if isinstance(seg, dict):
                try:
                    speaker_segments.append(schemas2.SpeakerSegment(**seg))
                except Exception:
                    continue

        todos: List[schemas2.VolcSessionTodoItem] = []
        for todo in self._safe_load_json(item.todos_json, []):
            if isinstance(todo, dict):
                try:
                    todos.append(schemas2.VolcSessionTodoItem(**todo))
                except Exception:
                    continue

        # 业务硬规则：
        # - 基于已有音频生成：无 source_asr_session_id，流式转写必须为空
        # - 在线录音生成：有 source_asr_session_id，流式转写来自 ASR 文本快照
        stream_snapshot = item.stream_transcript_text if item.source_asr_session_id else None

        payload = {
            "id": item.id,
            "session_no": item.session_no,
            "meeting_id": item.meeting_id,
            "source_audio_id": item.source_audio_id,
            "source_asr_session_id": item.source_asr_session_id,
            "volc_task_id": item.volc_task_id,
            "status": self._normalize_minutes_session_status(item.status),
            "error_msg": item.error_msg,
            "stream_transcript_text": stream_snapshot,
            "transcript_text": item.transcript_text,
            "speaker_segments": speaker_segments,
            "summary_title": item.summary_title,
            "summary_paragraph": item.summary_paragraph,
            "todos": todos,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        return schemas2.VolcMeetingMinutesSessionInDB.model_validate(payload)

    def _format_session_no(self, meeting_id: int, dt_value: datetime) -> str:
        ts = dt_value.strftime("%Y%m%d%H%M%S")
        return f"VOLC-{meeting_id}-{ts}"

    def _build_unique_session_no(
        self,
        db: Session,
        meeting_id: int,
        base_dt: Optional[datetime] = None,
    ) -> str:
        # 规则：VOLC + 会议ID + 精确到秒时间戳。若同秒冲突，则顺延秒数保证唯一。
        if base_dt is None:
            cursor = datetime.now(SESSION_NO_TIMEZONE)
        elif base_dt.tzinfo is None:
            cursor = base_dt
        else:
            cursor = base_dt.astimezone(SESSION_NO_TIMEZONE)
        cursor = cursor.replace(microsecond=0)
        while True:
            candidate = self._format_session_no(meeting_id, cursor)
            exists = (
                db.query(database.VolcMeetingMinutesSession.id)
                .filter(database.VolcMeetingMinutesSession.session_no == candidate)
                .first()
            )
            if not exists:
                return candidate
            cursor = cursor + timedelta(seconds=1)

    @staticmethod
    def _safe_load_json(raw: Optional[str], default):
        if not raw:
            return default
        try:
            loaded = json.loads(raw)
        except Exception:
            return default
        return loaded if loaded is not None else default

    def _resolve_stream_transcript_text(
        self,
        db: Session,
        audio: database.VolcMeetingAudio,
    ) -> Optional[str]:
        """
        仅返回流式 ASR 会话文本。
        注意：不要回退到音频表 transcript_text（该字段可能已被妙记精确转写覆盖，并带说话人标签），
        否则会导致“流式转写”与“精确转写”内容串字段。
        """
        if audio.source_asr_session_id:
            asr_session = (
                db.query(database.VolcAsrSession)
                .filter(database.VolcAsrSession.id == audio.source_asr_session_id)
                .first()
            )
            if asr_session and asr_session.transcript_text:
                return asr_session.transcript_text
        return None

    def update_summary(
        self, db: Session, meeting_id: int, payload: schemas2.VolcMeetingSummaryCreate
    ) -> database.VolcMeetingSummary:
        summary = (
            db.query(database.VolcMeetingSummary)
            .filter(database.VolcMeetingSummary.meeting_id == meeting_id)
            .first()
        )
        if summary:
            summary.paragraph = payload.paragraph
            if payload.title is not None:
                summary.title = payload.title
        else:
            summary = database.VolcMeetingSummary(
                meeting_id=meeting_id,
                paragraph=payload.paragraph,
                title=payload.title,
                source_audio_id=payload.source_audio_id,
            )
            db.add(summary)
        db.commit()
        db.refresh(summary)
        return summary

    def _stop_audio_poller(self, audio_id: int) -> None:
        with self._poll_lock:
            stop_flag = self._poll_stop_flags.get(audio_id)
        if stop_flag:
            stop_flag.set()

    def _cleanup_asr_runtime_artifacts(self, db: Session, meeting_id: int) -> None:
        asr_sessions = (
            db.query(database.VolcAsrSession)
            .filter(database.VolcAsrSession.meeting_id == meeting_id)
            .all()
        )
        for session in asr_sessions:
            local_path = (session.audio_local_path or "").strip()
            if not local_path:
                continue
            try:
                Path(local_path).unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to remove ASR local audio file meeting_id=%s session_id=%s path=%s err=%s",
                    meeting_id,
                    session.id,
                    local_path,
                    exc,
                )

        db.query(database.VolcAudioTranscription).filter(
            database.VolcAudioTranscription.meeting_id == meeting_id
        ).delete(synchronize_session=False)
        db.query(database.VolcAsrSession).filter(
            database.VolcAsrSession.meeting_id == meeting_id
        ).delete(synchronize_session=False)

    def clear_minutes(
        self,
        db: Session,
        meeting_id: int,
        reason: str = "纪要已清空，旧任务作废",
        cleanup_runtime_artifacts: bool = False,
    ) -> None:
        """
        清空指定会议的所有妙记内容：摘要、Todos、以及所有 TOS 音频记录上的转写文本。
        在每次新建 ASR 任务开始时调用，确保新一轮转写与摘要的一致性。
        """
        # 新一轮开始/离开主页面时，先把该会议仍在跑的任务作废，避免后续回写历史会话。
        active_audios = (
            db.query(database.VolcMeetingAudio)
            .filter(database.VolcMeetingAudio.meeting_id == meeting_id)
            .all()
        )
        for audio in active_audios:
            status_lower = str(audio.status or "").lower()
            if status_lower in {"submitted", "running", "queued", "processing"}:
                audio.status = "uploaded"
                audio.task_id = None
                audio.error_msg = reason
                self._stop_audio_poller(audio.id)

        db.query(database.VolcMeetingSummary).filter(
            database.VolcMeetingSummary.meeting_id == meeting_id
        ).delete(synchronize_session=False)

        db.query(database.VolcMeetingTodo).filter(
            database.VolcMeetingTodo.meeting_id == meeting_id
        ).delete(synchronize_session=False)

        # 清空所有音频记录上的转写与说话人分段（新转写完成后会重新写入）
        db.query(database.VolcMeetingAudio).filter(
            database.VolcMeetingAudio.meeting_id == meeting_id
        ).update({"transcript_text": None, "speaker_transcript": None}, synchronize_session=False)

        if cleanup_runtime_artifacts:
            self._cleanup_asr_runtime_artifacts(db, meeting_id)

        db.commit()
        logger.info("Cleared minutes for meeting_id=%s", meeting_id)

    def discard_workspace(
        self,
        db: Session,
        meeting_id: int,
        reason: str = "用户离开页面或重置，当前工作区内容已丢弃",
        current_audio_id: Optional[int] = None,
    ) -> None:
        """
        丢弃当前会议尚未完成的火山纪要工作区数据：
        - 停止进行中的轮询任务并标记音频作废
        - 清空当前分钟级展示数据（摘要/Todo/转写）
        - 清理 ASR 会话与逐段转写，删除本地临时音频文件
        - 删除非最终态会话快照，避免“半成品”进入历史
        """
        # 先执行与 clear 相同的“当前纪要数据清空”，并清理 ASR 运行期产物。
        self.clear_minutes(
            db=db,
            meeting_id=meeting_id,
            reason=reason,
            cleanup_runtime_artifacts=True,
        )

        audios = (
            db.query(database.VolcMeetingAudio)
            .filter(database.VolcMeetingAudio.meeting_id == meeting_id)
            .all()
        )
        for audio in audios:
            status_lower = str(audio.status or "").lower()
            if status_lower in {"completed", "success", "succeeded", "finished"}:
                continue
            # 丢弃工作区只取消“纪要生成任务”，不改变“音频已上传”事实状态。
            audio.status = "uploaded"
            audio.error_msg = reason
            audio.task_id = None
            audio.transcript_text = None
            audio.speaker_transcript = None
            audio.source_asr_session_id = None
            self._stop_audio_poller(audio.id)

        # 删除非最终态会话，避免半成品保留。
        db.query(database.VolcMeetingMinutesSession).filter(
            database.VolcMeetingMinutesSession.meeting_id == meeting_id,
            ~database.VolcMeetingMinutesSession.status.in_(
                [MINUTES_STATUS_COMPLETED, "completed", "success", "succeeded", "finished"]
            ),
        ).delete(synchronize_session=False)

        # 若前端明确传入当前音频 ID，则无条件删除该音频关联会话。
        # 这样可覆盖“离开瞬间任务刚好完成并写入 completed 会话”的竞态。
        if current_audio_id is not None:
            db.query(database.VolcMeetingMinutesSession).filter(
                database.VolcMeetingMinutesSession.meeting_id == meeting_id,
                database.VolcMeetingMinutesSession.source_audio_id == current_audio_id,
            ).delete(synchronize_session=False)

        db.commit()
        logger.info("Discarded volc minutes workspace meeting_id=%s", meeting_id)

    def delete_summary(self, db: Session, meeting_id: int) -> bool:
        summary = (
            db.query(database.VolcMeetingSummary)
            .filter(database.VolcMeetingSummary.meeting_id == meeting_id)
            .first()
        )
        if not summary:
            return False
        db.delete(summary)
        db.commit()
        return True

    def create_todo(
        self, db: Session, meeting_id: int, payload: schemas2.VolcMeetingTodoCreate
    ) -> database.VolcMeetingTodo:
        todo = database.VolcMeetingTodo(
            meeting_id=meeting_id,
            content=payload.content,
            executor=payload.executor,
            execution_time=payload.execution_time,
            source_audio_id=payload.source_audio_id,
        )
        db.add(todo)
        db.commit()
        db.refresh(todo)
        return todo

    def update_todo(
        self, db: Session, meeting_id: int, todo_id: int, payload: schemas2.VolcMeetingTodoCreate
    ) -> Optional[database.VolcMeetingTodo]:
        todo = (
            db.query(database.VolcMeetingTodo)
            .filter(
                database.VolcMeetingTodo.id == todo_id,
                database.VolcMeetingTodo.meeting_id == meeting_id,
            )
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
            db.query(database.VolcMeetingTodo)
            .filter(
                database.VolcMeetingTodo.id == todo_id,
                database.VolcMeetingTodo.meeting_id == meeting_id,
            )
            .first()
        )
        if not todo:
            return False
        db.delete(todo)
        db.commit()
        return True

    def update_latest_transcript(
        self,
        db: Session,
        meeting_id: int,
        transcript_text: str,
    ) -> database.VolcMeetingAudio:
        audio = (
            db.query(database.VolcMeetingAudio)
            .filter(database.VolcMeetingAudio.meeting_id == meeting_id)
            .order_by(database.VolcMeetingAudio.created_at.desc(), database.VolcMeetingAudio.id.desc())
            .first()
        )
        if not audio:
            raise ValueError("该会议尚无已上传的音频，请先完成录音或上传音频文件")
        audio.transcript_text = transcript_text
        db.commit()
        db.refresh(audio)
        return audio

    def _is_latest_minutes_session(self, db: Session, meeting_id: int, session_id: int) -> bool:
        latest = self._get_latest_minutes_session_record(db, meeting_id)
        return bool(latest and latest.id == session_id)

    def _get_latest_minutes_session_record(
        self,
        db: Session,
        meeting_id: int,
    ) -> Optional[database.VolcMeetingMinutesSession]:
        return (
            db.query(database.VolcMeetingMinutesSession)
            .filter(database.VolcMeetingMinutesSession.meeting_id == meeting_id)
            .order_by(database.VolcMeetingMinutesSession.created_at.desc(), database.VolcMeetingMinutesSession.id.desc())
            .first()
        )

    def _sync_latest_session_from_current_minutes(self, db: Session, meeting_id: int) -> None:
        session = self._get_latest_minutes_session_record(db, meeting_id)
        if not session:
            return
        audio = (
            db.query(database.VolcMeetingAudio)
            .filter(database.VolcMeetingAudio.meeting_id == meeting_id)
            .order_by(database.VolcMeetingAudio.updated_at.desc(), database.VolcMeetingAudio.id.desc())
            .first()
        )
        if not audio:
            return
        summary = (
            db.query(database.VolcMeetingSummary)
            .filter(database.VolcMeetingSummary.meeting_id == meeting_id)
            .first()
        )
        todos = (
            db.query(database.VolcMeetingTodo)
            .filter(database.VolcMeetingTodo.meeting_id == meeting_id)
            .order_by(database.VolcMeetingTodo.id.asc())
            .all()
        )
        session.source_audio_id = audio.id
        session.source_asr_session_id = audio.source_asr_session_id
        session.volc_task_id = audio.task_id
        session.status = self._normalize_minutes_session_status(audio.status, default=session.status)
        session.error_msg = audio.error_msg
        session.stream_transcript_text = self._resolve_stream_transcript_text(db, audio)
        session.transcript_text = audio.transcript_text
        session.speaker_segments_json = audio.speaker_transcript
        session.summary_title = summary.title if summary else None
        session.summary_paragraph = summary.paragraph if summary else None
        todos_payload = [
            {
                "content": item.content,
                "executor": item.executor,
                "execution_time": item.execution_time,
                "source_audio_id": item.source_audio_id,
            }
            for item in todos
        ]
        session.todos_json = json.dumps(todos_payload, ensure_ascii=False)
        db.commit()

    def _apply_latest_session_to_current_minutes(
        self,
        db: Session,
        session: database.VolcMeetingMinutesSession,
        payload: schemas2.VolcMeetingMinutesSessionUpdate,
        fields_set: set,
    ) -> None:
        meeting_id = session.meeting_id
        audio = None
        if session.source_audio_id:
            audio = (
                db.query(database.VolcMeetingAudio)
                .filter(
                    database.VolcMeetingAudio.id == session.source_audio_id,
                    database.VolcMeetingAudio.meeting_id == meeting_id,
                )
                .first()
            )
        if not audio:
            audio = (
                db.query(database.VolcMeetingAudio)
                .filter(database.VolcMeetingAudio.meeting_id == meeting_id)
                .order_by(database.VolcMeetingAudio.updated_at.desc(), database.VolcMeetingAudio.id.desc())
                .first()
            )
        if audio:
            if "transcript_text" in fields_set:
                audio.transcript_text = payload.transcript_text
            if "stream_transcript_text" in fields_set and audio.source_asr_session_id:
                asr_session = (
                    db.query(database.VolcAsrSession)
                    .filter(database.VolcAsrSession.id == audio.source_asr_session_id)
                    .first()
                )
                if asr_session:
                    asr_session.transcript_text = payload.stream_transcript_text

        if "summary_title" in fields_set or "summary_paragraph" in fields_set:
            summary = (
                db.query(database.VolcMeetingSummary)
                .filter(database.VolcMeetingSummary.meeting_id == meeting_id)
                .first()
            )
            if summary is None:
                summary = database.VolcMeetingSummary(
                    meeting_id=meeting_id,
                    title=payload.summary_title if "summary_title" in fields_set else None,
                    paragraph=payload.summary_paragraph if "summary_paragraph" in fields_set else "",
                    source_audio_id=session.source_audio_id,
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
                        content=item.content,
                        executor=item.executor,
                        execution_time=item.execution_time,
                        source_audio_id=item.source_audio_id or session.source_audio_id,
                    )
                )

    def save_upload_to_temp(self, upload_file) -> Path:
        suffix = Path(upload_file.filename or "audio").suffix or ".tmp"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            shutil.copyfileobj(upload_file.file, tmp_file)
            temp_path = Path(tmp_file.name)
        upload_file.file.seek(0)
        return temp_path

    def _ensure_uploader(self) -> _TosUploaderBase:
        if self._uploader is None:
            # Prefer official ve-tos-python-sdk when available; fallback to boto3 S3 compatible.
            if tos is not None:
                self._uploader = VolcTosUploaderSDK()
            else:
                self._uploader = VolcTosUploader()
        return self._uploader

    @staticmethod
    def _build_object_key(meeting_id: int, original_name: str) -> str:
        ext = Path(original_name).suffix
        unique = uuid4().hex
        return f"meetings/{meeting_id}/{unique}{ext}"

    @staticmethod
    def _guess_file_type(content_type: Optional[str]) -> str:
        if content_type and content_type.startswith("video"):
            return "video"
        return "audio"

    def _build_submit_payload(self, file_url: str, content_type: Optional[str]) -> Dict:
        file_type = self._guess_file_type(content_type)
        info_types_setting = settings.VOLC_MINUTES_INFORMATION_EXTRACTION_TYPES
        summary_types_setting = settings.VOLC_MINUTES_SUMMARIZATION_TYPES
        info_types = [item for item in (info_types_setting or ["todo_list", "question_answer", "transition"]) if item]
        summary_types = [item for item in (summary_types_setting or ["summary"]) if item]
        translation_enable = bool(settings.VOLC_MINUTES_TRANSLATION_ENABLE)
        translation_target = settings.VOLC_MINUTES_TRANSLATION_TARGET_LANG or "zh_cn"
        chapter_enabled = bool(settings.VOLC_MINUTES_CHAPTER_ENABLED)
        payload = {
            "Input": {
                "Offline": {
                    "FileURL": file_url,
                    "FileType": file_type,
                }
            },
            "Params": {
                "AllActivate": True,
                "SourceLang": settings.VOLC_MINUTES_SOURCE_LANG or "zh_cn",
                "AudioTranscriptionEnable": True,
                "AudioTranscriptionParams": {
                    "SpeakerIdentification": bool(settings.VOLC_MINUTES_SPEAKER_IDENTIFICATION),
                    "NumberOfSpeaker": int(settings.VOLC_MINUTES_NUMBER_OF_SPEAKERS or 0),
                    "NeedWordTimeSeries": bool(settings.VOLC_MINUTES_NEED_WORD_TS),
                },
                "TranslationEnable": translation_enable,
                "TranslationParams": {
                    "TargetLang": translation_target,
                },
                "InformationExtractionEnabled": bool(info_types),
                "InformationExtractionParams": {
                    "Types": info_types,
                },
                "SummarizationEnabled": bool(summary_types),
                "SummarizationParams": {
                    "Types": summary_types,
                },
                "ChapterEnabled": chapter_enabled,
            },
        }
        return payload

    def _upload_to_tos(
        self,
        meeting_id: int,
        upload_path: Path,
        original_name: str,
        content_type: Optional[str],
    ) -> Tuple[str, str]:
        uploader = self._ensure_uploader()
        object_key = self._build_object_key(meeting_id, original_name)
        file_url = uploader.upload_file(upload_path, object_key, content_type)
        return object_key, file_url

    def upload_from_local(
        self,
        db: Session,
        meeting_id: int,
        local_path: Path,
        original_name: str,
        content_type: Optional[str],
        source_asr_session_id: Optional[int] = None,
    ) -> database.VolcMeetingAudio:
        """将本地音频文件上传至 TOS，创建 VolcMeetingAudio 记录（供功能2使用）。"""
        self._ensure_audio_upload_limit(db, meeting_id)
        object_key, file_url = self._upload_to_tos(meeting_id, local_path, original_name, content_type)
        audio_record = database.VolcMeetingAudio(
            meeting_id=meeting_id,
            file_name=original_name,
            object_key=object_key,
            file_url=file_url,
            file_type=content_type,
            status="uploaded",
            source_asr_session_id=source_asr_session_id,
        )
        db.add(audio_record)
        db.commit()
        db.refresh(audio_record)
        logger.info(
            "Volc audio uploaded from local session_id=%s audio_id=%s meeting_id=%s",
            source_asr_session_id, audio_record.id, meeting_id,
        )
        return audio_record

    @staticmethod
    def _parse_speaker_segments(transcript_payload) -> List[Dict]:
        """
        从妙记转写 JSON 中提取说话人分段。
        支持格式：
          - 列表：[{"speaker_id":"S_0","text":"...","start_time":0,"end_time":1500}, ...]
          - 字典：{"utterances": [...]} 或 {"sentences": [...]}
        返回：[{"speaker":"说话人1","text":"...","start_ms":0,"end_ms":1500}, ...]
        若无说话人信息则返回空列表。
        """
        utterances: List = []
        if isinstance(transcript_payload, list):
            utterances = transcript_payload
        elif isinstance(transcript_payload, dict):
            utterances = (
                transcript_payload.get("utterances")
                or transcript_payload.get("sentences")
                or transcript_payload.get("results")
                or []
            )

        if not utterances:
            return []

        has_speaker = any(
            isinstance(u, dict) and (u.get("speaker_id") or u.get("speaker"))
            for u in utterances
        )
        if not has_speaker:
            return []

        # 按出现顺序建立 speaker_id → "说话人N" 映射
        # speaker_id 可能是字符串或字典，统一转为字符串
        def _to_str(v) -> str:
            if not v:
                return ""
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                return v.get("id") or v.get("name") or v.get("speaker_id") or json.dumps(v, ensure_ascii=False)
            return str(v)

        speaker_name_map: Dict[str, str] = {}
        for u in utterances:
            if not isinstance(u, dict):
                continue
            sid = _to_str(u.get("speaker_id") or u.get("speaker"))
            if sid and sid not in speaker_name_map:
                speaker_name_map[sid] = f"说话人{len(speaker_name_map) + 1}"

        # 先收集原始分段，再合并连续同一说话人的片段
        raw_segments = []
        for u in utterances:
            if not isinstance(u, dict):
                continue
            text = u.get("text") or u.get("transcript") or u.get("content") or ""
            if not text:
                continue
            sid = _to_str(u.get("speaker_id") or u.get("speaker"))
            raw_segments.append({
                "speaker": speaker_name_map.get(sid, sid or "未知"),
                "text": text,
                "start_ms": u.get("start_time"),
                "end_ms": u.get("end_time"),
            })

        # 合并连续同一说话人的片段
        segments: List[Dict] = []
        for seg in raw_segments:
            if segments and segments[-1]["speaker"] == seg["speaker"]:
                # 追加文本，更新结束时间
                segments[-1]["text"] += seg["text"]
                if seg["end_ms"] is not None:
                    segments[-1]["end_ms"] = seg["end_ms"]
            else:
                segments.append(dict(seg))
        return segments

    def _store_minutes_payload(
        self,
        db: Session,
        audio: database.VolcMeetingAudio,
        result: Dict,
    ) -> Tuple[Optional[database.VolcMeetingSummary], List[database.VolcMeetingTodo]]:
        summary_url = result.get("SummarizationFile")
        todo_url = result.get("InformationExtractionFile")
        # 语音妙记返回的精准转写文件（尝试多个常见字段名）
        transcript_url = (
            result.get("TranscriptionFile")
            or result.get("AsrFile")
            or result.get("RecognitionFile")
            or result.get("AudioTranscriptionFile")
        )
        summaries = self._fetch_json(summary_url) if summary_url else None
        todos_payload = self._fetch_json(todo_url) if todo_url else None
        transcript_payload = self._fetch_json(transcript_url) if transcript_url else None

        db.query(database.VolcMeetingSummary).filter(database.VolcMeetingSummary.meeting_id == audio.meeting_id).delete(synchronize_session=False)
        db.query(database.VolcMeetingTodo).filter(database.VolcMeetingTodo.meeting_id == audio.meeting_id).delete(synchronize_session=False)
        db.flush()

        # 保存精准转写文本到 VolcMeetingAudio（覆盖流式 ASR 结果）
        if transcript_payload:
            # 优先尝试提取说话人分段
            speaker_segs = self._parse_speaker_segments(transcript_payload)
            if speaker_segs:
                audio.speaker_transcript = json.dumps(speaker_segs, ensure_ascii=False)
                transcript_text = "\n".join(
                    f"[{seg['speaker']}] {seg['text']}" for seg in speaker_segs
                )
                logger.info(
                    "Stored speaker transcript from 妙记 audio_id=%s speakers=%d segments=%d",
                    audio.id,
                    len({s["speaker"] for s in speaker_segs}),
                    len(speaker_segs),
                )
            else:
                # 无说话人信息：降级为纯文本拼接
                audio.speaker_transcript = None
                if isinstance(transcript_payload, list):
                    parts = []
                    for item in transcript_payload:
                        if isinstance(item, dict):
                            t = item.get("text") or item.get("transcript") or item.get("content") or ""
                        else:
                            t = str(item)
                        if t:
                            parts.append(t)
                    transcript_text = "".join(parts) or json.dumps(transcript_payload, ensure_ascii=False)
                else:
                    transcript_text = (
                        transcript_payload.get("text")
                        or transcript_payload.get("transcript")
                        or transcript_payload.get("content")
                        or json.dumps(transcript_payload, ensure_ascii=False)
                    )
            if transcript_text:
                audio.transcript_text = transcript_text
                logger.info("Stored transcript from 妙记 audio_id=%s len=%d", audio.id, len(transcript_text))

        summary_record: Optional[database.VolcMeetingSummary] = None
        if summaries is not None:
            # 火山可能返回包装结构，先解包
            raw = summaries
            if isinstance(raw, dict):
                summaries = (
                    raw.get("Data") or raw.get("Result") or raw.get("Summary") or raw.get("Summaries")
                    or raw.get("summary") or raw.get("summaries")
                )
                if summaries is None and (raw.get("paragraph") is not None or raw.get("summary") or raw.get("title") is not None):
                    summaries = raw
            if summaries:
                if isinstance(summaries, list):
                    # 列表格式：拼接所有摘要段落，支持多种字段名
                    def _para_text(item):
                        if not isinstance(item, dict):
                            return str(item)
                        return (
                            item.get("paragraph") or item.get("summary") or item.get("content")
                            or item.get("text") or item.get("summary_text")
                            or json.dumps(item, ensure_ascii=False)
                        )
                    paragraph = "\n".join(_para_text(item) for item in summaries if _para_text(item))
                    title = next(
                        (item.get("title") for item in summaries if isinstance(item, dict) and item.get("title")),
                        None,
                    )
                elif isinstance(summaries, dict):
                    paragraph = (
                        summaries.get("paragraph") or summaries.get("summary") or summaries.get("content")
                        or summaries.get("text") or summaries.get("summary_text")
                        or json.dumps(summaries, ensure_ascii=False)
                    )
                    title = summaries.get("title")
                else:
                    paragraph = str(summaries)
                    title = None
                if paragraph or title is not None:
                    summary_record = database.VolcMeetingSummary(
                        meeting_id=audio.meeting_id,
                        source_audio_id=audio.id,
                        title=title,
                        paragraph=paragraph or "",
                    )
                    db.add(summary_record)
                    logger.info("Stored summary from 妙记 audio_id=%s meeting_id=%s len=%d", audio.id, audio.meeting_id, len(paragraph or ""))
                else:
                    logger.info("SummarizationFile returned empty paragraph/title for audio_id=%s, skipping summary record", audio.id)

        todo_records: List[database.VolcMeetingTodo] = []
        if todos_payload:
            # 列表格式直接就是 todo 列表，字典格式从 todo_list 字段取
            if isinstance(todos_payload, list):
                todo_items = todos_payload
            else:
                todo_items = todos_payload.get("todo_list") or []
            for item in todo_items:
                content = item.get("content") or (
                    (item.get("polished_res") or {}).get("content")
                )
                if not content:
                    content = json.dumps(item, ensure_ascii=False)
                todo_record = database.VolcMeetingTodo(
                    meeting_id=audio.meeting_id,
                    source_audio_id=audio.id,
                    content=content,
                    executor=self._extract_executor(item),
                    execution_time=self._extract_execution_time(item),
                )
                db.add(todo_record)
                todo_records.append(todo_record)
        db.flush()
        return summary_record, todo_records

    @staticmethod
    def _extract_executor(todo_item: Dict) -> Optional[str]:
        executor = todo_item.get("executor")
        if isinstance(executor, list):
            return ",".join(str(x) for x in executor if x)
        if executor:
            return str(executor)
        polished = todo_item.get("polished_res") or {}
        executor_field = polished.get("executor")
        if isinstance(executor_field, list):
            return ",".join(str(x) for x in executor_field if x)
        if executor_field:
            return str(executor_field)
        return None

    @staticmethod
    def _extract_execution_time(todo_item: Dict) -> Optional[str]:
        if todo_item.get("execution_time"):
            value = todo_item["execution_time"]
            if isinstance(value, list):
                return ",".join(str(x) for x in value if x)
            return str(value)
        polished = todo_item.get("polished_res") or {}
        if polished.get("execution_time"):
            value = polished["execution_time"]
            if isinstance(value, list):
                return ",".join(str(x) for x in value if x)
            return str(value)
        if todo_item.get("execution_ddl"):
            return str(todo_item["execution_ddl"])
        return None

    def _fetch_json(self, url: str) -> Optional[Dict]:
        if not url:
            return None
        try:
            response = requests.get(url, timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to download JSON from %s: %s", url, exc)
            return None


volc_minutes_service = VolcMinutesService()
