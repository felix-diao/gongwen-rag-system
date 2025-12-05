from fastapi import APIRouter, Depends, Query, Path, Body, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.models.database import get_db
from app.models.schemas import (
    PromptTemplateCreate,
    PromptTemplateUpdate,
    PromptTemplateToggle,
    PromptTemplateBatchDelete
)
from app.services.prompt_service import prompt_service
from app.utils.auth import get_current_user

router = APIRouter(prefix="/api/prompts", tags=["Prompt 模板管理"])


@router.get("")
async def list_prompts(
    name: Optional[str] = Query(None, description="名称搜索"),
    category: Optional[str] = Query(None, description="分类筛选"),
    isActive: Optional[bool] = Query(None, description="启用状态筛选"),
    current: int = Query(1, ge=1, description="当前页"),
    pageSize: int = Query(10, ge=1, le=100, description="每页大小"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    获取 Prompt 模板列表
    
    支持分页、搜索和筛选
    """
    try:
        prompts, total = prompt_service.list_prompts(
            db,
            current_user["user_id"],
            name=name,
            category=category,
            is_active=isActive,
            current=current,
            page_size=pageSize
        )
        
        return {
            "data": prompts,
            "total": total,
            "success": True
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{prompt_id}")
async def get_prompt(
    prompt_id: str = Path(..., description="模板ID"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """获取单个 Prompt 模板"""
    prompt = prompt_service.get_prompt_by_id(
        db,
        current_user["user_id"],
        prompt_id
    )
    
    return {
        "data": prompt,
        "success": True
    }


@router.post("")
async def create_prompt(
    payload: PromptTemplateCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """创建 Prompt 模板"""
    prompt = prompt_service.create_prompt(
        db,
        current_user["user_id"],
        payload
    )
    
    return {
        "data": prompt,
        "success": True
    }


@router.put("/{prompt_id}")
async def update_prompt(
    prompt_id: str = Path(..., description="模板ID"),
    payload: PromptTemplateUpdate = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """更新 Prompt 模板"""
    prompt = prompt_service.update_prompt(
        db,
        current_user["user_id"],
        prompt_id,
        payload
    )
    
    return {
        "data": prompt,
        "success": True
    }


@router.delete("/{prompt_id}")
async def delete_prompt(
    prompt_id: str = Path(..., description="模板ID"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """删除 Prompt 模板"""
    prompt_service.delete_prompt(
        db,
        current_user["user_id"],
        prompt_id
    )
    
    return {
        "success": True,
        "message": "删除成功"
    }


@router.post("/batch-delete")
async def batch_delete_prompts(
    payload: PromptTemplateBatchDelete,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """批量删除 Prompt 模板"""
    deleted_count = prompt_service.batch_delete_prompts(
        db,
        current_user["user_id"],
        payload.ids
    )
    
    return {
        "success": True,
        "message": f"成功删除 {deleted_count} 个模板"
    }


@router.patch("/{prompt_id}/toggle")
async def toggle_prompt_active(
    prompt_id: str = Path(..., description="模板ID"),
    payload: PromptTemplateToggle = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """切换 Prompt 模板启用状态"""
    prompt = prompt_service.toggle_prompt_active(
        db,
        current_user["user_id"],
        prompt_id,
        payload.isActive
    )
    
    return {
        "data": prompt,
        "success": True
    }


@router.get("/category/{category}")
async def get_prompts_by_category(
    category: str = Path(..., description="分类"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """按分类获取 Prompt 模板（仅启用的）"""
    prompts = prompt_service.get_prompts_by_category(
        db,
        current_user["user_id"],
        category
    )
    
    return {
        "data": prompts,
        "success": True
    }