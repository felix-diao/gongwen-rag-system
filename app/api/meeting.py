from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.models.database import get_db, Meeting
from app.models.schemas import (
    MeetingCreate,
    MeetingUpdate,
    MeetingResponse,
    MeetingListResponse
)
from app.services.tencent_meeting_service import tencent_meeting_service
from app.utils.auth import get_current_user
from app.utils.logger import logger

router = APIRouter(prefix="/api/meetings", tags=["腾讯会议"])


@router.post("/", response_model=MeetingResponse)
async def create_meeting(
    meeting_data: MeetingCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    创建会议
    
    - **subject**: 会议主题（必填）
    - **type**: 0:预约会议 1:快速会议
    - **start_time**: 开始时间（Unix时间戳，预约会议必填）
    - **end_time**: 结束时间（Unix时间戳，预约会议必填）
    - **settings**: 会议设置（可选）
    """
    try:
        meeting = await tencent_meeting_service.create_meeting(
            db=db,
            user_id=current_user["user_id"],
            meeting_data=meeting_data
        )
        
        return MeetingResponse.from_orm(meeting)
        
    except Exception as e:
        logger.error(f"创建会议失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/", response_model=MeetingListResponse)
async def list_meetings(
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    查询用户的会议列表
    
    - **start_time**: 开始时间（Unix时间戳，可选）
    - **end_time**: 结束时间（Unix时间戳，可选）
    """
    try:
        meetings = await tencent_meeting_service.list_user_meetings(
            db=db,
            user_id=current_user["user_id"],
            start_time=start_time,
            end_time=end_time
        )
        
        return MeetingListResponse(
            meetings=[MeetingResponse.from_orm(m) for m in meetings],
            total=len(meetings)
        )
        
    except Exception as e:
        logger.error(f"查询会议列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    查询会议详情
    
    - **meeting_id**: 腾讯会议ID
    """
    # 先从数据库查询
    db_meeting = db.query(Meeting).filter(
        Meeting.meeting_id == meeting_id,
        Meeting.user_id == current_user["user_id"]
    ).first()
    
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议不存在")
    
    # 从腾讯API获取最新状态
    try:
        tencent_info = await tencent_meeting_service.get_meeting_info(meeting_id)
        
        # 更新数据库状态
        if "status" in tencent_info:
            db_meeting.status = tencent_info["status"]
            db.commit()
            db.refresh(db_meeting)
        
        return MeetingResponse.from_orm(db_meeting)
        
    except Exception as e:
        logger.error(f"查询会议详情失败: {e}")
        # 如果腾讯API失败，返回数据库中的信息
        return MeetingResponse.from_orm(db_meeting)


@router.delete("/{meeting_id}")
async def cancel_meeting(
    meeting_id: str,
    reason: str = "主动取消",
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    取消会议
    
    - **meeting_id**: 腾讯会议ID
    - **reason**: 取消原因（可选）
    """
    # 验证会议所有权
    db_meeting = db.query(Meeting).filter(
        Meeting.meeting_id == meeting_id,
        Meeting.user_id == current_user["user_id"]
    ).first()
    
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议不存在")
    
    if db_meeting.status == "cancelled":
        raise HTTPException(status_code=400, detail="会议已取消")
    
    try:
        await tencent_meeting_service.cancel_meeting(
            db=db,
            user_id=current_user["user_id"],
            meeting_id=meeting_id,
            reason=reason
        )
        
        return {"message": "会议已取消"}
        
    except Exception as e:
        logger.error(f"取消会议失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{meeting_id}")
async def update_meeting(
    meeting_id: str,
    update_data: MeetingUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    修改会议
    
    - **meeting_id**: 腾讯会议ID
    - **update_data**: 要更新的会议信息
    """
    # 验证会议所有权
    db_meeting = db.query(Meeting).filter(
        Meeting.meeting_id == meeting_id,
        Meeting.user_id == current_user["user_id"]
    ).first()
    
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议不存在")
    
    if db_meeting.status != "active":
        raise HTTPException(status_code=400, detail="会议状态不允许修改")
    
    try:
        await tencent_meeting_service.update_meeting(
            db=db,
            user_id=current_user["user_id"],
            meeting_id=meeting_id,
            update_data=update_data
        )
        
        return {"message": "会议已更新"}
        
    except Exception as e:
        logger.error(f"修改会议失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{meeting_id}/participants")
async def get_participants(
    meeting_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    获取参会成员列表
    
    - **meeting_id**: 腾讯会议ID
    """
    # 验证会议所有权
    db_meeting = db.query(Meeting).filter(
        Meeting.meeting_id == meeting_id,
        Meeting.user_id == current_user["user_id"]
    ).first()
    
    if not db_meeting:
        raise HTTPException(status_code=404, detail="会议不存在")
    
    try:
        participants = await tencent_meeting_service.get_participants(meeting_id)
        return {"participants": participants, "total": len(participants)}
        
    except Exception as e:
        logger.error(f"获取参会成员失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))