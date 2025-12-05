import logging
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import database, schemas2
from app.utils.text_processor import TextProcessor


logger = logging.getLogger(__name__)


# 会议服务类
class MeetingService:
    # 创建会议
    def create_meeting(self, db: Session, meeting: schemas2.MeetingCreate, creator_id: str = None):
        data = meeting.dict()
        if creator_id:
            data["creator_id"] = creator_id
        db_meeting = database.Meeting(**data)
        db.add(db_meeting)
        db.commit()
        db.refresh(db_meeting)
        return db_meeting
    
    # 获取会议信息
    def get_meeting(self, db: Session, meeting_id: int):
        return db.query(database.Meeting).filter(database.Meeting.id == meeting_id).first()
    
    # 更新会议信息
    def update_meeting(self, db: Session, meeting_id: int, meeting_update: schemas2.MeetingUpdate):
        db_meeting = self.get_meeting(db, meeting_id)
        if db_meeting:
            for key, value in meeting_update.dict(exclude_unset=True).items():
                setattr(db_meeting, key, value)
            db.commit()
            db.refresh(db_meeting)
        return db_meeting
    
    # 删除会议
    def delete_meeting(self, db: Session, meeting_id: int):
        db_meeting = self.get_meeting(db, meeting_id)
        if db_meeting:
            db.delete(db_meeting)
            db.commit()
            return True
        return False

    # 获取所有会议
    def get_all_meetings(self, db: Session):
        return db.query(database.Meeting).all()

    # 根据创建者ID获取该用户创建的所有会议
    def get_meetings_by_creator(self, db: Session, creator_id: str):
        return db.query(database.Meeting).filter(database.Meeting.creator_id == creator_id).all()

# 文件服务类
class FileService:
    # 创建会议文件记录
    def create_file(self, db: Session, file_data: schemas2.MeetingFileCreate):
        db_file = database.MeetingFile(**file_data.dict())
        db.add(db_file)
        db.commit()
        db.refresh(db_file)
        return db_file
    
    # 根据会议ID获取文件列表
    def get_files_by_meeting(self, db: Session, meeting_id: int):
        return db.query(database.MeetingFile).filter(
            database.MeetingFile.meeting_id == meeting_id
        ).all()

    # 根据文件ID获取文件记录
    def get_file_by_id(self, db: Session, file_id: int):
        return db.query(database.MeetingFile).filter(database.MeetingFile.id == file_id).first()

    # 删除文件记录（不负责删除磁盘文件）
    def delete_file_record(self, db: Session, file_id: int):
        db_file = self.get_file_by_id(db, file_id)
        if db_file:
            db.delete(db_file)
            db.commit()
            return True
        return False


class AudioService:
    def create_audio_record(
        self,
        db: Session,
        meeting_id: int,
        filename: str,
        file_path: str,
        file_type: Optional[str] = None,
    ):
        audio_record = database.MeetingAudio(
            meeting_id=meeting_id,
            filename=filename,
            file_path=file_path,
            file_type=file_type or "audio/*",
        )
        db.add(audio_record)
        db.commit()
        db.refresh(audio_record)
        return audio_record

    def get_audios_by_meeting(self, db: Session, meeting_id: int):
        return db.query(database.MeetingAudio).filter(database.MeetingAudio.meeting_id == meeting_id).all()

    def get_audio_by_id(self, db: Session, audio_id: int):
        return db.query(database.MeetingAudio).filter(database.MeetingAudio.id == audio_id).first()

    def delete_audio_record(self, db: Session, audio: database.MeetingAudio) -> bool:
        try:
            db.delete(audio)
            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception(f"删除会议音频记录失败，音频ID: {audio.id}")
            return False

    def delete_audios_by_meeting(self, db: Session, meeting_id: int) -> bool:
        audios = self.get_audios_by_meeting(db, meeting_id)
        if not audios:
            return True

        try:
            for audio in audios:
                file_path = audio.file_path
                if file_path:
                    try:
                        path_obj = Path(file_path)
                        if path_obj.exists():
                            path_obj.unlink()
                    except Exception as exc:
                        logger.warning(f"删除会议音频文件出错: {exc}")
                db.delete(audio)

            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception(f"删除会议 {meeting_id} 音频记录失败")
            return False

# 创建服务实例
meeting_service = MeetingService()
file_service = FileService()
audio_service = AudioService()