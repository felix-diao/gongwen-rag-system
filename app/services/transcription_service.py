import logging
import threading
from typing import Optional

from app.models import database
from app.models.database import SessionLocal

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
        res = model.transcribe(str(file_path), language="zh")
        return res.get("text")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Whisper 转写失败: {e}")
        return None


def transcribe_audio_background(audio_id: int, meeting_id: int, file_path: str):
    """在后台线程执行转写，并直接更新会议音频记录。"""

    def _worker():
        db = SessionLocal()
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

            text = _transcribe_with_whisper(file_path)
            if text is None:
                audio.status = "failed"
                audio.error_msg = "转写不可用"
            else:
                audio.transcript_text = text
                audio.status = "completed"
            db.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"后台转写失败: {e}")
            try:
                audio = db.query(database.MeetingAudio).filter(database.MeetingAudio.id == audio_id).first()
                if audio:
                    audio.status = "failed"
                    audio.error_msg = str(e)
                    db.commit()
            except Exception:
                pass
        finally:
            db.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
