# app/routers/conversation.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.models.database import get_db
from app.models.schemas import (
    ConversationCreate,
    ConversationResponse,
    ConversationFeedback,
    ConversationSearchRequest,
    ConversationSearchResult,
    ConversationStatistics,
    StandardResponse
)
from app.services.conversation_service import conversation_service
from app.services.embedding_service import embedding_service
from app.utils.auth import get_current_user
from app.utils.logger import get_logger

logger = get_logger("conversation_api")

router = APIRouter(prefix="/api/conversations", tags=["会话管理"])


# =========================
# 1. 获取会话统计
# =========================
@router.get("/statistics", response_model=StandardResponse[ConversationStatistics])
def get_statistics(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取用户会话统计"""
    try:
        stats = conversation_service.get_statistics(db, current_user["user_id"])
        return StandardResponse(
            success=True,
            data=ConversationStatistics(**stats),
            message="获取统计成功"
        )
    except Exception as e:
        logger.error(f"✗ 获取统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取统计失败: {str(e)}")


# =========================
# 2. 历史会话向量检索
# =========================
@router.post("/search", response_model=StandardResponse[List[ConversationSearchResult]])
async def search_conversations(
    req: ConversationSearchRequest,
    current_user: dict = Depends(get_current_user)
):
    """基于向量搜索用户历史会话"""
    try:
        # 生成查询向量
        query_vector = (await embedding_service.embed_texts([req.query]))[0]

        results = await conversation_service.search_conversations(
            user_id=current_user["user_id"],
            query=req.query,
            query_vector=query_vector,
            top_k=req.top_k
        )

        return StandardResponse(
            success=True,
            data=[ConversationSearchResult(**r) for r in results],
            message="检索成功"
        )
    except Exception as e:
        logger.error(f"✗ 检索失败: {e}")
        raise HTTPException(status_code=500, detail=f"检索失败: {str(e)}")


# =========================
# 3. 创建会话（含向量化）
# =========================
@router.post("", response_model=StandardResponse[ConversationResponse])
async def create_conversation(
    conv_data: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """创建会话（包含向量入库）"""
    if conv_data.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="禁止为其他用户创建会话")

    try:
        new_conv = await conversation_service.create_conversation(db, conv_data)

        return StandardResponse(
            success=True,
            data=ConversationResponse(
                conv_id=new_conv.conv_id,
                query=new_conv.query,
                answer=new_conv.answer,
                weight=new_conv.weight,
                liked=new_conv.liked,
                created_at=new_conv.created_at
            ),
            message="会话创建成功"
        )
    except Exception as e:
        logger.error(f"✗ 创建会话失败: {e}")
        raise HTTPException(status_code=500, detail=f"创建失败: {str(e)}")


# =========================
# 4. 列出历史会话
# =========================
@router.get("", response_model=StandardResponse[List[ConversationResponse]])
def list_conversations(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """列出用户历史会话"""
    try:
        conversations = conversation_service.list_conversations(
            db=db,
            user_id=current_user["user_id"],
            limit=limit,
            offset=offset
        )
        
        return StandardResponse(
            success=True,
            data=[
                ConversationResponse(
                    conv_id=conv.conv_id,
                    query=conv.query,
                    answer=conv.answer,
                    weight=conv.weight,
                    liked=conv.liked,
                    created_at=conv.created_at
                )
                for conv in conversations
            ],
            message="获取成功"
        )
    except Exception as e:
        logger.error(f"✗ 获取会话列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取失败: {str(e)}")


# =========================
# 5. 获取单条会话
# =========================
@router.get("/{conv_id}", response_model=StandardResponse[ConversationResponse])
def get_conversation(
    conv_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取单条会话详情"""
    conversation = conversation_service.get_conversation(db, conv_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    if conversation.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问")
    
    return StandardResponse(
        success=True,
        data=ConversationResponse(
            conv_id=conversation.conv_id,
            query=conversation.query,
            answer=conversation.answer,
            weight=conversation.weight,
            liked=conversation.liked,
            created_at=conversation.created_at
        ),
        message="获取成功"
    )


# =========================
# 6. 会话反馈（点赞 / 权重调整）
# =========================
@router.patch("/{conv_id}/feedback", response_model=StandardResponse[dict])
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
    
    try:
        updated_conv = conversation_service.update_conversation(db, conv_id, feedback)
        
        return StandardResponse(
            success=True,
            data={
                "conv_id": updated_conv.conv_id,
                "weight": updated_conv.weight,
                "liked": updated_conv.liked
            },
            message="反馈更新成功"
        )
    except Exception as e:
        logger.error(f"✗ 更新反馈失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")


# =========================
# 7. 删除会话（软删除 + 真实向量删除）
# =========================
@router.delete("/{conv_id}", response_model=StandardResponse[dict])
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
    
    try:
        success = conversation_service.delete_conversation(db, conv_id)
        
        if not success:
            raise HTTPException(status_code=500, detail="删除失败")
        
        return StandardResponse(
            success=True,
            data={"conv_id": conv_id},
            message="删除成功"
        )
    except Exception as e:
        logger.error(f"✗ 删除会话失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")