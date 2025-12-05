from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.models.database import get_db
from app.models.schemas import ConversationCreate, ConversationFeedback
from app.services.conversation_service import conversation_service
from app.services.embedding_service import embedding_service
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/conversations", tags=["会话管理"])


# =========================
# 1. 创建会话（含向量化）
# =========================
@router.post("")
async def create_conversation(
    conv_data: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """创建会话（包含向量入库）"""
    if conv_data.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="禁止为其他用户创建会话")

    new_conv = await conversation_service.create_conversation(db, conv_data)

    return {
        "conv_id": new_conv.conv_id,
        "query": new_conv.query,
        "answer": new_conv.answer,
        "weight": new_conv.weight,
        "liked": new_conv.liked,
        "created_at": new_conv.created_at
    }


# =========================
# 2. 列出历史会话
# =========================
@router.get("")
def list_conversations(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
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


# =========================
# 3. 获取单条会话
# =========================
@router.get("/{conv_id}")
def get_conversation(
    conv_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
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


# =========================
# 4. 会话反馈（点赞 / 权重调整）
# =========================
@router.patch("/{conv_id}/feedback")
def update_conversation_feedback(
    conv_id: str,
    feedback: ConversationFeedback,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
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


# =========================
# 5. 删除会话（软删除 + 真实向量删除）
# =========================
@router.delete("/{conv_id}")
def delete_conversation(
    conv_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    conversation = conversation_service.get_conversation(db, conv_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    if conversation.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作")
    
    success = conversation_service.delete_conversation(db, conv_id)
    
    if not success:
        raise HTTPException(status_code=500, detail="删除失败")
    
    return {"message": "删除成功"}


# =========================
# 6. 历史会话向量检索
# =========================
@router.post("/search")
async def search_conversations(
    query: str,
    top_k: int = 3,
    current_user: dict = Depends(get_current_user)
):
    """基于向量搜索用户历史会话"""
    # 生成查询向量
    query_vector = (await embedding_service.embed_texts([query]))[0]

    results = await conversation_service.search_conversations(
        user_id=current_user["user_id"],
        query=query,
        query_vector=query_vector,
        top_k=top_k
    )

    return results


# =========================
# 7. 获取会话统计
# =========================
@router.get("/statistics")
def get_statistics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    stats = conversation_service.get_statistics(db, current_user["user_id"])
    return stats
