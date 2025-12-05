import logging
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import schemas2
from app.models.database import MeetingAudio, MeetingFile, get_db
from app.models.schemas import StandardResponse
from app.services.meeting_minute_service import minutes_service
from app.services.meeting_service import MeetingService
from app.utils.auth import get_current_user

# 会议纪要相关路由
router = APIRouter(prefix="/api/minutes", tags=["meeting_minutes"])

logger = logging.getLogger(__name__)

meeting_service = MeetingService()


class GenerateMinutesRequest(BaseModel):
    file_ids: Optional[List[int]] = None
    audio_ids: Optional[List[int]] = None


def _ensure_meeting_exists(db: Session, meeting_id: int):
    meeting = meeting_service.get_meeting(db, meeting_id)
    if not meeting:
        logger.warning("会议未找到，ID: %s", meeting_id)
        raise HTTPException(status_code=404, detail="会议未找到")
    return meeting


def _validate_file_ids(db: Session, meeting_id: int, file_ids: Optional[List[int]]) -> None:
    if not file_ids:
        return
    if len(file_ids) > 5:
        raise HTTPException(status_code=400, detail="最多只能选择5个文件用于生成纪要")
    files = db.query(MeetingFile).filter(MeetingFile.id.in_(file_ids)).all()
    if len(files) != len(file_ids):
        raise HTTPException(status_code=400, detail="file_ids 中存在不存在的文件")
    for f in files:
        if f.meeting_id != meeting_id:
            raise HTTPException(status_code=400, detail=f"文件 {f.id} 不属于会议 {meeting_id}")


def _collect_audio_segments(db: Session, meeting_id: int, audio_ids: Optional[List[int]]) -> List[dict]:
    segments: List[dict] = []
    if not audio_ids:
        return segments
    if len(audio_ids) > 3:
        raise HTTPException(status_code=400, detail="最多只能选择3段音频用于生成纪要")
    audios = db.query(MeetingAudio).filter(MeetingAudio.id.in_(audio_ids)).all()
    if len(audios) != len(audio_ids):
        raise HTTPException(status_code=400, detail="audio_ids 中存在不存在的音频")
    for audio in audios:
        if audio.meeting_id != meeting_id:
            raise HTTPException(status_code=400, detail=f"音频 {audio.id} 不属于会议 {meeting_id}")
        if not audio.transcript_text:
            raise HTTPException(status_code=400, detail=f"音频 {audio.id} 尚未完成转写，请稍后再试")
        segments.append({
            "name": audio.filename or f"音频{audio.id}",
            "text": audio.transcript_text
        })
    return segments


@router.post("/insights/generate/{meeting_id}", response_model=StandardResponse[schemas2.MeetingInsightsResponse])
def generate_meeting_insights(
    meeting_id: int,
    payload: GenerateMinutesRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """生成新的结构化会议纪要（会议摘要、行动项、决策事项）。"""
    file_ids = payload.file_ids
    audio_ids = payload.audio_ids

    logger.info("生成结构化会议纪要，会议ID: %s，文件: %s，音频: %s", meeting_id, file_ids, audio_ids)

    _ensure_meeting_exists(db, meeting_id)

    _validate_file_ids(db, meeting_id, file_ids)
    audio_segments = _collect_audio_segments(db, meeting_id, audio_ids)

    result = minutes_service.generate_structured_minutes(
        db,
        meeting_id,
        selected_file_ids=file_ids,
        audio_segments=audio_segments,
    )
    if not result:
        # raise HTTPException(status_code=500, detail="生成结构化纪要失败")
        return None
    return StandardResponse(success=True, data=result, message="结构化会议纪要生成成功")


@router.get("/insights/{meeting_id}", response_model=StandardResponse[schemas2.MeetingInsightsResponse])
def get_meeting_insights(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """获取结构化会议纪要（摘要/行动项/决策事项）。"""
    logger.info("获取结构化会议纪要，会议ID: %s", meeting_id)
    _ensure_meeting_exists(db, meeting_id)
    result = minutes_service.get_meeting_insights(db, meeting_id)
    if not result:
        return None
    return StandardResponse(success=True, data=result, message="获取结构化会议纪要成功")


@router.get("/insights/{meeting_id}/summary", response_model=StandardResponse[schemas2.MeetingSummaryInDB])
def get_meeting_summary(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """仅获取会议摘要。"""
    _ensure_meeting_exists(db, meeting_id)
    summary = minutes_service.get_summary(db, meeting_id)
    if not summary:
        raise HTTPException(status_code=404, detail="会议摘要未找到")
    return StandardResponse(
        success=True,
        data=schemas2.MeetingSummaryInDB.from_orm(summary),
        message="获取会议摘要成功",
    )


@router.put("/insights/{meeting_id}/summary", response_model=StandardResponse[schemas2.MeetingSummaryInDB])
def update_meeting_summary(
    meeting_id: int,
    payload: schemas2.MeetingSummaryUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """更新会议摘要文本。"""
    _ensure_meeting_exists(db, meeting_id)
    summary = minutes_service.update_summary(db, meeting_id, payload)
    return StandardResponse(
        success=True,
        data=schemas2.MeetingSummaryInDB.from_orm(summary),
        message="会议摘要更新成功",
    )


@router.get("/insights/{meeting_id}/actions", response_model=StandardResponse[List[schemas2.MeetingActionItemInDB]])
def list_action_items(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """列出会议所有行动项。"""
    _ensure_meeting_exists(db, meeting_id)
    items = minutes_service.list_action_items(db, meeting_id)
    data = [schemas2.MeetingActionItemInDB.from_orm(item) for item in items]
    return StandardResponse(success=True, data=data, message="获取行动项成功")


@router.get("/insights/{meeting_id}/actions/{item_id}", response_model=StandardResponse[schemas2.MeetingActionItemInDB])
def get_action_item(
    meeting_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.get_action_item(db, meeting_id, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="行动项未找到")
    return StandardResponse(success=True, data=schemas2.MeetingActionItemInDB.from_orm(item), message="获取行动项成功")


@router.post("/insights/{meeting_id}/actions", response_model=StandardResponse[schemas2.MeetingActionItemInDB])
def create_action_item(
    meeting_id: int,
    payload: schemas2.MeetingActionItemCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.create_action_item(db, meeting_id, payload)
    return StandardResponse(success=True, data=schemas2.MeetingActionItemInDB.from_orm(item), message="创建行动项成功")


@router.put("/insights/{meeting_id}/actions/{item_id}", response_model=StandardResponse[schemas2.MeetingActionItemInDB])
def update_action_item(
    meeting_id: int,
    item_id: int,
    payload: schemas2.MeetingActionItemUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    updated = minutes_service.update_action_item(db, meeting_id, item_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="行动项未找到")
    return StandardResponse(success=True, data=schemas2.MeetingActionItemInDB.from_orm(updated), message="更新行动项成功")


@router.delete("/insights/{meeting_id}/actions/{item_id}", response_model=StandardResponse[None])
def delete_action_item(
    meeting_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    success = minutes_service.delete_action_item(db, meeting_id, item_id)
    if not success:
        raise HTTPException(status_code=404, detail="行动项未找到")
    return StandardResponse(success=True, data=None, message="删除行动项成功")


@router.get("/insights/{meeting_id}/decisions", response_model=StandardResponse[List[schemas2.MeetingDecisionItemInDB]])
def list_decision_items(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    items = minutes_service.list_decision_items(db, meeting_id)
    data = [schemas2.MeetingDecisionItemInDB.from_orm(item) for item in items]
    return StandardResponse(success=True, data=data, message="获取决策事项成功")


@router.get("/insights/{meeting_id}/decisions/{item_id}", response_model=StandardResponse[schemas2.MeetingDecisionItemInDB])
def get_decision_item(
    meeting_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.get_decision_item(db, meeting_id, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="决策事项未找到")
    return StandardResponse(success=True, data=schemas2.MeetingDecisionItemInDB.from_orm(item), message="获取决策事项成功")


@router.post("/insights/{meeting_id}/decisions", response_model=StandardResponse[schemas2.MeetingDecisionItemInDB])
def create_decision_item(
    meeting_id: int,
    payload: schemas2.MeetingDecisionItemCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.create_decision_item(db, meeting_id, payload)
    return StandardResponse(success=True, data=schemas2.MeetingDecisionItemInDB.from_orm(item), message="创建决策事项成功")


@router.put("/insights/{meeting_id}/decisions/{item_id}", response_model=StandardResponse[schemas2.MeetingDecisionItemInDB])
def update_decision_item(
    meeting_id: int,
    item_id: int,
    payload: schemas2.MeetingDecisionItemUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    updated = minutes_service.update_decision_item(db, meeting_id, item_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="决策事项未找到")
    return StandardResponse(success=True, data=schemas2.MeetingDecisionItemInDB.from_orm(updated), message="更新决策事项成功")


@router.delete("/insights/{meeting_id}/decisions/{item_id}", response_model=StandardResponse[None])
def delete_decision_item(
    meeting_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    success = minutes_service.delete_decision_item(db, meeting_id, item_id)
    if not success:
        raise HTTPException(status_code=404, detail="决策事项未找到")
    return StandardResponse(success=True, data=None, message="删除决策事项成功")


@router.get("/insights/export/docx/{meeting_id}")
def export_insights_docx(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    file_path = minutes_service.export_structured_docx(db, meeting_id)
    if not file_path:
        raise HTTPException(status_code=404, detail="导出失败，会议数据不存在")
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

