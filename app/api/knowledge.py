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
from app.utils.logger import get_logger  # ← 新增

router = APIRouter(prefix="/api/knowledge", tags=["知识库管理"])

# 创建路由专属 logger
logger = get_logger("knowledge_api")  # ← 新增

# ========== 知识库 API ==========

@router.get("/bases", response_model=List[KnowledgeBaseResponse])
async def list_bases(
    include_public: bool = Query(True, description="是否包含公有知识库"),
    only_public: bool = Query(False, description="仅查看公有知识库"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取知识库列表"""
    user_id = current_user["user_id"]
    logger.info(f"用户 {user_id} 查询知识库列表 (include_public={include_public}, only_public={only_public})")  # ← 新增
    
    try:
        if only_public:
            from app.models.database import KnowledgeBase as KnowledgeBaseModel
            bases = db.query(KnowledgeBaseModel).filter(
                KnowledgeBaseModel.is_public == True
            ).order_by(KnowledgeBaseModel.created_at.desc()).all()
            logger.info(f"查询到 {len(bases)} 个公有知识库")  # ← 新增
        else:
            bases = await knowledge_service.list_bases(db, user_id, include_public)
            logger.info(f"用户 {user_id} 查询到 {len(bases)} 个知识库")  # ← 新增
        
        return bases
        
    except Exception as e:
        logger.error(f"查询知识库列表失败: {e}", exc_info=True)  # ← 新增
        raise


@router.post("/bases", response_model=KnowledgeBaseResponse)
async def create_base(
    payload: KnowledgeBaseCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """创建知识库"""
    user_id = current_user["user_id"]
    base_type = "公有" if payload.is_public else "私有"
    logger.info(f"用户 {user_id} 请求创建{base_type}知识库: {payload.name}")  # ← 新增
    
    try:
        base = await knowledge_service.create_base(db, user_id, payload)
        logger.info(f"用户 {user_id} 创建{base_type}知识库成功: {base.name} (ID: {base.id})")  # ← 新增
        return base
        
    except HTTPException as e:
        logger.warning(f"创建知识库失败: {e.detail}")  # ← 新增
        raise
    except Exception as e:
        logger.error(f"创建知识库异常: {e}", exc_info=True)  # ← 新增
        raise


@router.patch("/bases/{base_id}", response_model=KnowledgeBaseResponse)
async def update_base(
    base_id: int,
    payload: KnowledgeBaseUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """更新知识库"""
    user_id = current_user["user_id"]
    logger.info(f"用户 {user_id} 请求更新知识库 {base_id}")  # ← 新增
    
    try:
        base = await knowledge_service.update_base(db, user_id, base_id, payload)
        logger.info(f"用户 {user_id} 更新知识库成功: {base_id}")  # ← 新增
        return base
        
    except HTTPException as e:
        logger.warning(f"更新知识库失败: {e.detail}")  # ← 新增
        raise
    except Exception as e:
        logger.error(f"更新知识库异常: {e}", exc_info=True)  # ← 新增
        raise


@router.delete("/bases/{base_id}")
async def delete_base(
    base_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """删除知识库"""
    user_id = current_user["user_id"]
    logger.info(f"用户 {user_id} 请求删除知识库 {base_id}")  # ← 新增
    
    try:
        await knowledge_service.delete_base(db, user_id, base_id)
        logger.info(f"用户 {user_id} 删除知识库成功: {base_id}")  # ← 新增
        return {"message": "知识库已删除"}
        
    except HTTPException as e:
        logger.warning(f"删除知识库失败: {e.detail}")  # ← 新增
        raise
    except Exception as e:
        logger.error(f"删除知识库异常: {e}", exc_info=True)  # ← 新增
        raise


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
    user_id = current_user["user_id"]
    
    try:
        tags_list = json.loads(tags)
        base_id = int(baseId) if baseId else None
        
        # 记录文件信息
        file_size_mb = file.size / (1024 * 1024) if file.size else 0
        logger.info(
            f"用户 {user_id} 上传文件: {file.filename} "
            f"({file_size_mb:.2f}MB) 到知识库 {base_id or '默认'}"
        )  # ← 新增
        
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"上传参数解析失败: {e}")  # ← 新增
        raise HTTPException(status_code=400, detail="参数格式错误")
    
    try:
        item = await knowledge_service.upload_file(
            db, user_id, file, tags_list, base_id
        )
        
        logger.info(
            f"用户 {user_id} 上传成功: {file.filename} "
            f"(ID: {item.id}, 状态: {item.status}, chunks: {item.chunk_count})"
        )  # ← 新增
        
        return item
        
    except HTTPException as e:
        logger.warning(f"上传文件失败: {e.detail}")  # ← 新增
        raise
    except Exception as e:
        logger.error(f"上传文件异常: {e}", exc_info=True)  # ← 新增
        raise


@router.get("/items", response_model=List[KnowledgeItemResponse])
async def list_items(
    tag: Optional[str] = None,
    baseId: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取知识项列表"""
    user_id = current_user["user_id"]
    logger.info(f"用户 {user_id} 查询知识项 (tag={tag}, baseId={baseId})")  # ← 新增
    
    try:
        items = await knowledge_service.list_items(db, user_id, tag, baseId)
        logger.info(f"用户 {user_id} 查询到 {len(items)} 个知识项")  # ← 新增
        return items
        
    except Exception as e:
        logger.error(f"查询知识项失败: {e}", exc_info=True)  # ← 新增
        raise


@router.delete("/items/{item_id}")
async def remove_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """删除知识项"""
    user_id = current_user["user_id"]
    logger.info(f"用户 {user_id} 请求删除知识项 {item_id}")  # ← 新增
    
    try:
        await knowledge_service.remove_item(db, user_id, item_id)
        logger.info(f"用户 {user_id} 删除知识项成功: {item_id}")  # ← 新增
        return {"message": "知识项已删除"}
        
    except HTTPException as e:
        logger.warning(f"删除知识项失败: {e.detail}")  # ← 新增
        raise
    except Exception as e:
        logger.error(f"删除知识项异常: {e}", exc_info=True)  # ← 新增
        raise


@router.post("/items/{item_id}/move")
async def move_item(
    item_id: int,
    payload: KnowledgeItemMove,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """移动知识项"""
    user_id = current_user["user_id"]
    logger.info(f"用户 {user_id} 移动知识项 {item_id} 到知识库 {payload.target_base_id}")  # ← 新增
    
    try:
        await knowledge_service.move_item(
            db, user_id, item_id, payload.target_base_id
        )
        logger.info(f"用户 {user_id} 移动知识项成功: {item_id}")  # ← 新增
        return {"message": "知识项已移动"}
        
    except HTTPException as e:
        logger.warning(f"移动知识项失败: {e.detail}")  # ← 新增
        raise
    except Exception as e:
        logger.error(f"移动知识项异常: {e}", exc_info=True)  # ← 新增
        raise


@router.post("/items/move")
async def move_batch(
    payload: KnowledgeItemBatchMove,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """批量移动知识项"""
    user_id = current_user["user_id"]
    logger.info(
        f"用户 {user_id} 批量移动 {len(payload.item_ids)} 个知识项 "
        f"到知识库 {payload.target_base_id}"
    )  # ← 新增
    
    try:
        moved = await knowledge_service.move_batch(
            db, user_id, payload.item_ids, payload.target_base_id
        )
        logger.info(f"用户 {user_id} 成功移动 {moved} 个知识项")  # ← 新增
        return {"data": {"moved": moved}}
        
    except HTTPException as e:
        logger.warning(f"批量移动失败: {e.detail}")  # ← 新增
        raise
    except Exception as e:
        logger.error(f"批量移动异常: {e}", exc_info=True)  # ← 新增
        raise