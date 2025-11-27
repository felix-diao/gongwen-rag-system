import logging
from typing import List, Optional
import threading
import uuid

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.models import schemas2
from app.services.meeting_minute_service import minutes_service
from app.services.meeting_service import MeetingService
from app.models.database import get_db, SessionLocal
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path

# 会议纪要相关路由
router = APIRouter(prefix="/api/minutes", tags=["meeting_minutes"])

logger = logging.getLogger(__name__)

meeting_service = MeetingService()


class GenerateMinutesRequest(BaseModel):
    file_ids: Optional[List[int]] = None


@router.post("/{meeting_id}/generate", response_model=schemas2.MeetingMinutesInDB)
def generate_meeting_minutes(meeting_id: int, payload: GenerateMinutesRequest = Body(...), db: Session = Depends(get_db)):
    """统一的生成/重新生成接口：如果已有纪要则覆盖更新，否则创建新纪要。

    请求体只接受可选的 `file_ids` 列表（最多 5 个），若不传则使用该会议所有文件。
    """
    file_ids = payload.file_ids

    logger.info(f"生成会议纪要，会议ID: {meeting_id}，所选文件ids: {file_ids}")

    # 验证会议是否存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        logger.warning(f"生成纪要时未找到会议，会议ID: {meeting_id}")
        raise HTTPException(status_code=404, detail="会议未找到")

    # 验证 file_ids 合法性（数量、是否属于本会议）
    if file_ids:
        if len(file_ids) > 5:
            raise HTTPException(status_code=400, detail="最多只能选择5个文件用于生成纪要")
        # 检查文件是否存在且属于该会议
        from app.models.database import MeetingFile

        files = db.query(MeetingFile).filter(MeetingFile.id.in_(file_ids)).all()
        if len(files) != len(file_ids):
            raise HTTPException(status_code=400, detail="file_ids 中存在不存在的文件")
        for f in files:
            if f.meeting_id != meeting_id:
                raise HTTPException(status_code=400, detail=f"文件 {f.id} 不属于会议 {meeting_id}")

    # 同步执行：如果已有纪要则覆盖，否则创建新纪要（service 内部处理）
    result = minutes_service.generate_minutes(db, meeting_id, selected_file_ids=file_ids, create_new_version=False)
    if not result:
        raise HTTPException(status_code=500, detail="生成纪要失败")
    return result

"""会议纪要相关接口，包括生成、查询、更新、删除与导出。"""

"""获取会议纪要"""
@router.get("/{meeting_id}", response_model=schemas2.MeetingMinutesInDB)
def get_meeting_minutes(meeting_id: int, db: Session = Depends(get_db)):
    logger.info(f"获取会议纪要，会议ID: {meeting_id}")
    minutes = minutes_service.get_minutes_by_meeting(db, meeting_id)
    if not minutes:
        raise HTTPException(status_code=404, detail="会议纪要未找到")
    return minutes


"""更新会议纪要"""
@router.put("/{meeting_id}", response_model=schemas2.MeetingMinutesInDB)
def update_meeting_minutes(meeting_id: int, minutes_update: schemas2.MeetingMinutesUpdate, db: Session = Depends(get_db)):
    logger.info(f"更新会议纪要，会议ID: {meeting_id}")
    updated = minutes_service.update_minutes(db, meeting_id, minutes_update)
    if not updated:
        raise HTTPException(status_code=404, detail="会议纪要未找到")
    logger.info(f"成功更新会议纪要，会议ID: {meeting_id}")
    return updated


"""删除会议纪要"""
@router.delete("/{meeting_id}")
def delete_meeting_minutes(meeting_id: int, db: Session = Depends(get_db)):
    logger.info(f"删除会议纪要，会议ID: {meeting_id}")
    success = minutes_service.delete_minutes(db, meeting_id)
    if not success:
        raise HTTPException(status_code=404, detail="会议纪要未找到")
    logger.info(f"已删除会议纪要，会议ID: {meeting_id}")
    return {"message": "会议纪要已删除"}


@router.post("/{meeting_id}/export/docx", response_model=List[schemas2.MeetingFileInDB])
def export_minutes_docx(meeting_id: int, db: Session = Depends(get_db)):
    """导出会议纪要为 Word（docx），保存到 meeting_files/{meeting_id}/exports 并在 DB 创建文件记录。"""
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    created = minutes_service.export_minutes(db, meeting_id, formats=["docx"])
    if created is None:
        raise HTTPException(status_code=500, detail="导出失败或未找到纪要")
    return created


@router.post("/{meeting_id}/export/pdf", response_model=List[schemas2.MeetingFileInDB])
def export_minutes_pdf(meeting_id: int, db: Session = Depends(get_db)):
    """导出会议纪要为 PDF，保存到 meeting_files/{meeting_id}/exports 并在 DB 创建文件记录。"""
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    created = minutes_service.export_minutes(db, meeting_id, formats=["pdf"])
    if created is None:
        raise HTTPException(status_code=500, detail="导出失败或未找到纪要")
    return created


@router.get("/{meeting_id}/export/{file_id}/download")
def download_exported_file(meeting_id: int, file_id: int, db: Session = Depends(get_db)):
    # 验证会议是否存在
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")

    from app.services.meeting_service import FileService

    fs = FileService()
    db_file = fs.get_file_by_id(db, file_id)
    if not db_file or db_file.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="文件未找到")

    path_obj = Path(db_file.file_path)
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail="服务器上未找到文件")

    return FileResponse(path=str(path_obj), filename=db_file.filename, media_type=db_file.file_type or "application/octet-stream")





    # 重新生成会议纪要（可选择部分文件）
@router.post("/{meeting_id}/regenerate", response_model=schemas2.MeetingMinutesInDB)
def regenerate_meeting_minutes(meeting_id: int, file_ids: Optional[List[int]] = Body(None), db: Session = Depends(get_db)):
    logger.info(f"重新生成会议纪要，会议ID: {meeting_id}，所选文件ids: {file_ids}")
    db_meeting = meeting_service.get_meeting(db, meeting_id)
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议未找到")
    result = minutes_service.generate_minutes(db, meeting_id, selected_file_ids=file_ids)
    if not result:
        raise HTTPException(status_code=500, detail="生成纪要失败")
    return result
