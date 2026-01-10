import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import requests
from sqlalchemy.orm import Session

from app.config import settings
from app.models import database, schemas2
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger

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
    def __init__(self) -> None:
        self._api = VolcMinutesAPI()
        self._uploader: Optional[_TosUploaderBase] = None
        self._timeout = settings.VOLC_MINUTES_TIMEOUT or 10
        self._poll_lock = threading.Lock()
        self._poll_stop_flags: Dict[int, threading.Event] = {}
        self._poll_threads: Dict[int, threading.Thread] = {}

    def upload_audio(
        self,
        db: Session,
        meeting_id: int,
        upload_path: Path,
        original_name: str,
        content_type: Optional[str],
    ) -> database.VolcMeetingAudio:
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

    def submit_audio(
        self,
        db: Session,
        audio_id: int,
        meeting_id: Optional[int] = None,
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
        self.start_polling(audio_id=audio.id)
        return audio

    def start_polling(self, audio_id: int) -> None:
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
                args=(audio_id, stop_event),
                daemon=True,
            )
            self._poll_threads[audio_id] = thread
            thread.start()

    def _poll_loop(self, audio_id: int, stop_event: threading.Event) -> None:
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
                if not audio.task_id:
                    logger.info("Volc minutes poller no task_id yet audio_id=%s status=%s", audio_id, audio.status)
                    time.sleep(poll_interval)
                    continue

                updated_audio, completed, minutes = self.refresh_minutes(db, audio_id)
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
    ) -> Tuple[database.VolcMeetingAudio, bool, Optional[schemas2.VolcMeetingMinutesResponse]]:
        audio = db.query(database.VolcMeetingAudio).filter(database.VolcMeetingAudio.id == audio_id).first()
        if not audio:
            raise ValueError(f"Volc meeting audio {audio_id} not found")
        if not audio.task_id:
            raise ValueError(f"Volc meeting audio {audio_id} has no task_id")
        query_result = self._api.query_task(audio.task_id)
        raw_status = ((query_result["body"].get("Data") or {})).get("Status")
        status = str(raw_status).lower() if raw_status else ""
        if status in {"running", "queued", "processing"}:
            audio.status = raw_status or status or "processing"
            db.commit()
            db.refresh(audio)
            return audio, False, None
        if status in {"failed", "error"}:
            body = query_result["body"].get("Data") or {}
            audio.status = raw_status or "failed"
            audio.error_msg = body.get("ErrMessage") or query_result["body"].get("Message")
            db.commit()
            db.refresh(audio)
            return audio, False, None
        if status in {"success", "succeeded", "successed", "finished", "completed"}:
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
            minutes = schemas2.VolcMeetingMinutesResponse(
                summary=schemas2.VolcMeetingSummaryInDB.model_validate(summary_record) if summary_record else None,
                todos=[schemas2.VolcMeetingTodoInDB.model_validate(item) for item in todo_records],
            )
            return audio, True, minutes
        audio.status = raw_status or status or "unknown"
        db.commit()
        db.refresh(audio)
        return audio, False, None

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
        return schemas2.VolcMeetingMinutesResponse(
            summary=schemas2.VolcMeetingSummaryInDB.model_validate(summary) if summary else None,
            todos=[schemas2.VolcMeetingTodoInDB.model_validate(item) for item in todos],
        )

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

    def _store_minutes_payload(
        self,
        db: Session,
        audio: database.VolcMeetingAudio,
        result: Dict,
    ) -> Tuple[Optional[database.VolcMeetingSummary], List[database.VolcMeetingTodo]]:
        summary_url = result.get("SummarizationFile")
        todo_url = result.get("InformationExtractionFile")
        summaries = self._fetch_json(summary_url) if summary_url else None
        todos_payload = self._fetch_json(todo_url) if todo_url else None

        db.query(database.VolcMeetingSummary).filter(database.VolcMeetingSummary.meeting_id == audio.meeting_id).delete(synchronize_session=False)
        db.query(database.VolcMeetingTodo).filter(database.VolcMeetingTodo.meeting_id == audio.meeting_id).delete(synchronize_session=False)
        db.flush()

        summary_record: Optional[database.VolcMeetingSummary] = None
        if summaries:
            paragraph = summaries.get("paragraph") or summaries.get("summary") or json.dumps(summaries, ensure_ascii=False)
            summary_record = database.VolcMeetingSummary(
                meeting_id=audio.meeting_id,
                source_audio_id=audio.id,
                title=summaries.get("title"),
                paragraph=paragraph,
            )
            db.add(summary_record)

        todo_records: List[database.VolcMeetingTodo] = []
        if todos_payload:
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
