from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.models import database, schemas2
from app.models.schemas import StandardResponse
from app.models.database import get_db
from app.services.meeting_minute_service import minutes_service
from app.services.meeting_service import MeetingService
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/minutes", tags=["meeting_minutes_structured"])
meeting_service = MeetingService()


class GenerateInsightsPayload(BaseModel):
    file_ids: Optional[List[int]] = None
    audio_ids: Optional[List[int]] = None


def _ensure_meeting_exists(db: Session, meeting_id: int) -> None:
    meeting = meeting_service.get_meeting(db, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="会议未找到")


def _build_audio_segments(db: Session, meeting_id: int, audio_ids: Optional[List[int]]) -> Optional[List[dict]]:
    if not audio_ids:
        return None
    records = (
        db.query(database.MeetingAudio)
        .filter(
            database.MeetingAudio.meeting_id == meeting_id,
            database.MeetingAudio.id.in_(audio_ids),
        )
        .all()
    )
    segments: List[dict] = []
    for item in records:
        if not item.transcript_text:
            continue
        segments.append(
            {
                "name": item.filename or f"音频{item.id}",
                "text": item.transcript_text,
            }
        )
    return segments


@router.get(
    "/insights/{meeting_id}",
    response_model=StandardResponse[schemas2.MeetingInsightsResponse],
)
def get_insights(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    insights = minutes_service.get_meeting_insights(db, meeting_id)
    if insights is None:
        insights = schemas2.MeetingInsightsResponse(summary=None, action_items=[], decision_items=[])
    return StandardResponse(success=True, data=insights, message="获取结构化纪要成功")


@router.post(
    "/insights/generate/{meeting_id}",
    response_model=StandardResponse[schemas2.MeetingInsightsResponse],
)
def generate_insights(
    meeting_id: int,
    payload: GenerateInsightsPayload = Body(default=GenerateInsightsPayload()),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    result = minutes_service.generate_structured_minutes(
        db=db,
        meeting_id=meeting_id,
        selected_file_ids=payload.file_ids,
        audio_segments=_build_audio_segments(db, meeting_id, payload.audio_ids),
    )
    if result is None:
        raise HTTPException(status_code=400, detail="结构化纪要生成失败")
    return StandardResponse(success=True, data=result, message="结构化纪要生成成功")


@router.put(
    "/insights/{meeting_id}/summary",
    response_model=StandardResponse[schemas2.MeetingSummaryInDB],
)
def update_insights_summary(
    meeting_id: int,
    payload: schemas2.MeetingSummaryUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    summary = minutes_service.update_summary(db, meeting_id, payload)
    return StandardResponse(
        success=True,
        data=schemas2.MeetingSummaryInDB.model_validate(summary),
        message="摘要已更新",
    )


@router.post(
    "/insights/{meeting_id}/actions",
    response_model=StandardResponse[schemas2.MeetingActionItemInDB],
)
def create_action(
    meeting_id: int,
    payload: schemas2.MeetingActionItemCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.create_action_item(db, meeting_id, payload)
    return StandardResponse(success=True, data=schemas2.MeetingActionItemInDB.model_validate(item), message="行动项已新增")


@router.put(
    "/insights/{meeting_id}/actions/{item_id}",
    response_model=StandardResponse[schemas2.MeetingActionItemInDB],
)
def update_action(
    meeting_id: int,
    item_id: int,
    payload: schemas2.MeetingActionItemUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.update_action_item(db, meeting_id, item_id, payload)
    if not item:
        raise HTTPException(status_code=404, detail="行动项未找到")
    return StandardResponse(success=True, data=schemas2.MeetingActionItemInDB.model_validate(item), message="行动项已更新")


@router.delete(
    "/insights/{meeting_id}/actions/{item_id}",
    response_model=StandardResponse[None],
)
def delete_action(
    meeting_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    if not minutes_service.delete_action_item(db, meeting_id, item_id):
        raise HTTPException(status_code=404, detail="行动项未找到")
    return StandardResponse(success=True, data=None, message="行动项已删除")


@router.post(
    "/insights/{meeting_id}/decisions",
    response_model=StandardResponse[schemas2.MeetingDecisionItemInDB],
)
def create_decision(
    meeting_id: int,
    payload: schemas2.MeetingDecisionItemCreate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.create_decision_item(db, meeting_id, payload)
    return StandardResponse(success=True, data=schemas2.MeetingDecisionItemInDB.model_validate(item), message="决策项已新增")


@router.put(
    "/insights/{meeting_id}/decisions/{item_id}",
    response_model=StandardResponse[schemas2.MeetingDecisionItemInDB],
)
def update_decision(
    meeting_id: int,
    item_id: int,
    payload: schemas2.MeetingDecisionItemUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    item = minutes_service.update_decision_item(db, meeting_id, item_id, payload)
    if not item:
        raise HTTPException(status_code=404, detail="决策项未找到")
    return StandardResponse(success=True, data=schemas2.MeetingDecisionItemInDB.model_validate(item), message="决策项已更新")


@router.delete(
    "/insights/{meeting_id}/decisions/{item_id}",
    response_model=StandardResponse[None],
)
def delete_decision(
    meeting_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    if not minutes_service.delete_decision_item(db, meeting_id, item_id):
        raise HTTPException(status_code=404, detail="决策项未找到")
    return StandardResponse(success=True, data=None, message="决策项已删除")


@router.get("/insights/export/docx/{meeting_id}")
def export_insights_docx(
    meeting_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ensure_meeting_exists(db, meeting_id)
    file_path = minutes_service.export_structured_docx(db, meeting_id)
    if not file_path:
        raise HTTPException(status_code=404, detail="导出失败，会议不存在")
    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="导出文件不存在")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        background=BackgroundTask(lambda p=path: p.exists() and p.unlink(missing_ok=True)),
    )
