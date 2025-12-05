import logging
import threading
import time
from typing import Optional

from app.models import database
from app.models.database import SessionLocal
from app.services.websocket_manager import meeting_ws_manager

logger = logging.getLogger(__name__)


def _transcribe_with_whisper(file_path: str) -> Optional[str]:
    """调用 Whisper 将音频转写为文本。"""
    try:
        import whisper  # type: ignore
    except Exception:
        logger.warning("未安装 whisper，跳过音频转写")
        return None
    try:
        model = whisper.load_model("small")
        start = time.perf_counter()  # ⏱ 开始计时
        res = model.transcribe(str(file_path), language="zh")
        cost = time.perf_counter() - start  # ⏱ 结束计时
        logger.info(f"Whisper 转写耗时: {cost:.2f} 秒")
        return res.get("text")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Whisper 转写失败: {e}")
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

            text = _transcribe_with_whisper(file_path)
            if text is None:
                audio.status = "failed"
                audio.error_msg = "转写不可用"
            else:
                audio.transcript_text = text
                audio.status = "completed"
            db.commit()
            _emit(audio)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"后台转写失败: {e}")
            try:
                audio = db.query(database.MeetingAudio).filter(database.MeetingAudio.id == audio_id).first()
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
