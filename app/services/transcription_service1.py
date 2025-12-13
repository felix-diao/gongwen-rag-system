import threading
import time
from typing import Optional
import os
from dotenv import load_dotenv

from app.models import database
from app.models.database import SessionLocal
from app.services.websocket_manager import meeting_ws_manager
from app.utils.logger import get_logger

from faster_whisper import WhisperModel

# =============================
# 加载 .env 配置
# =============================
load_dotenv()

MODEL_PATH = os.getenv("WHISPER_MODEL_PATH", "/root/models/tests/faster-whisper-small")
BEAM_SIZE = int(os.getenv("BEAM_SIZE", 1))
VAD_FILTER = os.getenv("VAD_FILTER", "False").lower() == "true"
LANGUAGE = os.getenv("LANGUAGE", "zh")

logger = get_logger("transcription_service1")


def _transcribe_with_faster_whisper(file_path: str) -> Optional[str]:
    """调用 Faster-Whisper 将音频转写为文本。"""
    if not os.path.exists(MODEL_PATH):
        logger.warning(f"模型目录不存在: {MODEL_PATH}")
        return None

    try:
        start_load = time.perf_counter()
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8")
        load_time = time.perf_counter() - start_load
        logger.info(f"模型加载耗时: {load_time:.2f}s")

        start_transcribe = time.perf_counter()
        segments, info = model.transcribe(
            file_path,
            beam_size=BEAM_SIZE,
            vad_filter=VAD_FILTER,
            language=LANGUAGE
        )
        transcribe_time = time.perf_counter() - start_transcribe
        logger.info(f"转写耗时: {transcribe_time:.2f}s, 音频总时长: {info.duration:.2f}s")

        all_text = " ".join(seg.text for seg in segments)
        return all_text

    except Exception as e:
        logger.exception(f"Faster-Whisper 转写失败: {e}")
        return None


def transcribe_audio_background(audio_id: int, meeting_id: int, file_path: str):
    """在后台线程执行转写，并直接更新会议音频记录。"""

    def _worker():
        db = SessionLocal()

        def _emit(audio_obj: database.MeetingAudio | None) -> None:
            if not audio_obj:
                return
            meeting_ws_manager.notify_from_thread(
                meeting_id,
                {
                    "type": "transcription.update",
                    "meetingId": meeting_id,
                    "audioId": audio_obj.id,
                    "status": audio_obj.status,
                    "transcriptText": audio_obj.transcript_text,
                    "errorMsg": audio_obj.error_msg,
                },
            )

        try:
            audio = db.query(database.MeetingAudio).filter(
                database.MeetingAudio.id == audio_id,
                database.MeetingAudio.meeting_id == meeting_id,
            ).first()
            if not audio:
                logger.warning("未找到需要转写的音频记录，直接返回")
                return

            audio.status = "processing"
            audio.error_msg = None
            db.commit()
            _emit(audio)

            text = _transcribe_with_faster_whisper(file_path)
            if text is None:
                audio.status = "failed"
                audio.error_msg = "转写不可用"
            else:
                audio.transcript_text = text
                audio.status = "completed"
            db.commit()
            _emit(audio)

        except Exception as e:
            logger.exception(f"后台转写失败: {e}")
            try:
                audio = db.query(database.MeetingAudio).filter(
                    database.MeetingAudio.id == audio_id
                ).first()
                if audio:
                    audio.status = "failed"
                    audio.error_msg = str(e)
                    db.commit()
                    _emit(audio)
            except Exception:
                pass
        finally:
            db.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
