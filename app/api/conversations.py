from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models.database import get_db
from app.models.schemas import ConversationFeedback
from app.services.conversation_service import conversation_service
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/conversations", tags=["会话管理"])

@router.get("/")
def list_conversations(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """列出历史会话"""
    conversations = conversation_service.list_conversations(
        db=db,
        user_id=current_user["user_id"],
        limit=limit,
        offset=offset
    )
    
    return [
        {
            "conv_id": conv.conv_id,
            "query": conv.query,
            "answer": conv.answer,
            "weight": conv.weight,
            "liked": conv.liked,
            "created_at": conv.created_at
        }
        for conv in conversations
    ]

@router.get("/{conv_id}")
def get_conversation(
    conv_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取会话详情"""
    conversation = conversation_service.get_conversation(db, conv_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    if conversation.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    
    return {
        "conv_id": conversation.conv_id,
        "user_id": conversation.user_id,
        "query": conversation.query,
        "answer": conversation.answer,
        "weight": conversation.weight,
        "liked": conversation.liked,
        "created_at": conversation.created_at
    }

@router.patch("/{conv_id}/feedback")
def update_conversation_feedback(
    conv_id: str,
    feedback: ConversationFeedback,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """更新会话反馈"""
    conversation = conversation_service.get_conversation(db, conv_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    if conversation.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    
    updated_conv = conversation_service.update_conversation(db, conv_id, feedback)
    
    return {
        "conv_id": updated_conv.conv_id,
        "weight": updated_conv.weight,
        "liked": updated_conv.liked
    }

@router.delete("/{conv_id}")
def delete_conversation(
    conv_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """删除会话"""
    conversation = conversation_service.get_conversation(db, conv_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    if conversation.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    
    success = conversation_service.delete_conversation(db, conv_id)
    
    if not success:
        raise HTTPException(status_code=500, detail="删除失败")
    
    return {"message": "删除成功"}