from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import json

from app.models.database import get_db
from app.models.schemas import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseResponse,
    KnowledgeItemResponse,
    KnowledgeItemMove,
    KnowledgeItemBatchMove
)
from app.services.knowledge_service import knowledge_service
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/knowledge", tags=["知识库管理"])

# ========== 知识库 API ==========

@router.get("/bases", response_model=List[KnowledgeBaseResponse])
async def list_bases(
    include_public: bool = Query(True, description="是否包含公有知识库"),
    only_public: bool = Query(False, description="仅查看公有知识库"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    获取知识库列表
    
    **查询参数**:
    - include_public: 是否包含公有知识库（默认True）
    - only_public: 仅查看公有知识库（默认False）
    """
    if only_public:
        # 仅查看公有知识库
        from app.models.database import KnowledgeBase as KnowledgeBaseModel
        bases = db.query(KnowledgeBaseModel).filter(
            KnowledgeBaseModel.is_public == True
        ).order_by(KnowledgeBaseModel.created_at.desc()).all()
    else:
        bases = await knowledge_service.list_bases(db, current_user["user_id"], include_public)
    
    return bases


@router.post("/bases", response_model=KnowledgeBaseResponse)
async def create_base(
    payload: KnowledgeBaseCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    创建知识库
    
    **权限**:
    - 普通用户：只能创建私有知识库 (is_public=False)
    - 管理员：可以创建公有或私有知识库
    """
    base = await knowledge_service.create_base(db, current_user["user_id"], payload)
    return base

@router.patch("/bases/{base_id}", response_model=KnowledgeBaseResponse)
async def update_base(
    base_id: int,
    payload: KnowledgeBaseUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """更新知识库"""
    base = await knowledge_service.update_base(db, current_user["user_id"], base_id, payload)
    return base

@router.delete("/bases/{base_id}")
async def delete_base(
    base_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """删除知识库"""
    await knowledge_service.delete_base(db, current_user["user_id"], base_id)
    return {"message": "知识库已删除"}

# ========== 知识项 API ==========

@router.post("/upload", response_model=KnowledgeItemResponse)
async def upload_file(
    file: UploadFile = File(...),
    tags: str = Form("[]"),
    baseId: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """上传文件"""
    
    try:
        tags_list = json.loads(tags)
        base_id = int(baseId) if baseId else None
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="参数格式错误")
    
    item = await knowledge_service.upload_file(
        db, current_user["user_id"], file, tags_list, base_id
    )
    
    return item

@router.get("/items", response_model=List[KnowledgeItemResponse])
async def list_items(
    tag: Optional[str] = None,
    baseId: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取知识项列表"""
    items = await knowledge_service.list_items(db, current_user["user_id"], tag, baseId)
    return items

@router.delete("/items/{item_id}")
async def remove_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """删除知识项"""
    await knowledge_service.remove_item(db, current_user["user_id"], item_id)
    return {"message": "知识项已删除"}

@router.post("/items/{item_id}/move")
async def move_item(
    item_id: int,
    payload: KnowledgeItemMove,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """移动知识项"""
    await knowledge_service.move_item(
        db, current_user["user_id"], item_id, payload.target_base_id
    )
    return {"message": "知识项已移动"}

@router.post("/items/move")
async def move_batch(
    payload: KnowledgeItemBatchMove,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """批量移动知识项"""
    moved = await knowledge_service.move_batch(
        db, current_user["user_id"], payload.item_ids, payload.target_base_id
    )
    return {"data": {"moved": moved}}