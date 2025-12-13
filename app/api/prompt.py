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

from app.utils.logger import get_logger

logger = get_logger("prompt_api")

router = APIRouter(prefix="/api/prompts", tags=["Prompt 模板管理"])


@router.get("")
async def list_prompts(
    name: Optional[str] = Query(None, description="名称搜索"),
    category: Optional[str] = Query(None, description="分类筛选"),
    isActive: Optional[bool] = Query(None, description="启用状态筛选"),
    isPublic: Optional[bool] = Query(None, description="是否公共模板"),
    current: int = Query(1, ge=1, description="当前页"),
    pageSize: int = Query(10, ge=1, le=100, description="每页大小"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    获取 Prompt 模板列表
    
    权限说明：
    - 普通用户：返回公共模板 + 自己的私有模板
    - 管理员：返回所有模板
    """
    try:
        is_admin = current_user.get("role") == "admin"
        
        prompts, total = prompt_service.list_prompts(
            db,
            current_user["user_id"],
            is_admin,  # 传递管理员标识
            name=name,
            category=category,
            is_active=isActive,
            is_public=isPublic,
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
    """
    获取单个 Prompt 模板
    
    权限说明：
    - 公共模板：所有人可见
    - 私有模板：只有创建者可见（管理员除外）
    """
    is_admin = current_user.get("role") == "admin"
    
    prompt = prompt_service.get_prompt_by_id(
        db,
        current_user["user_id"],
        is_admin,
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
    """
    创建 Prompt 模板
    
    权限说明：
    - 普通用户：只能创建私有模板（is_public 会被强制为 False）
    - 管理员：可以创建公共或私有模板
    """
    is_admin = current_user.get("role") == "admin"
    
    prompt = prompt_service.create_prompt(
        db,
        current_user["user_id"],
        is_admin,
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
    """
    更新 Prompt 模板
    
    权限说明：
    - 公共模板：只有管理员可以修改
    - 私有模板：只有创建者可以修改（管理员也可以）
    """
    is_admin = current_user.get("role") == "admin"
    
    prompt = prompt_service.update_prompt(
        db,
        current_user["user_id"],
        is_admin,
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
    """
    删除 Prompt 模板
    
    权限说明：
    - 公共模板：只有管理员可以删除
    - 私有模板：只有创建者可以删除（管理员也可以）
    """
    is_admin = current_user.get("role") == "admin"
    
    prompt_service.delete_prompt(
        db,
        current_user["user_id"],
        is_admin,
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
    """
    批量删除 Prompt 模板
    
    权限说明：
    - 普通用户：只能删除自己的私有模板
    - 管理员：可以删除所有模板
    """
    is_admin = current_user.get("role") == "admin"
    
    deleted_count = prompt_service.batch_delete_prompts(
        db,
        current_user["user_id"],
        is_admin,
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
    """
    切换 Prompt 模板启用状态
    
    权限说明：
    - 公共模板：只有管理员可以切换
    - 私有模板：只有创建者可以切换（管理员也可以）
    """
    is_admin = current_user.get("role") == "admin"
    
    prompt = prompt_service.toggle_prompt_active(
        db,
        current_user["user_id"],
        is_admin,
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
    """
    按分类获取 Prompt 模板（仅启用的）
    
    权限说明：
    - 普通用户：返回公共模板 + 自己的私有模板
    - 管理员：返回所有模板
    """
    is_admin = current_user.get("role") == "admin"
    
    prompts = prompt_service.get_prompts_by_category(
        db,
        current_user["user_id"],
        is_admin,
        category
    )
    
    return {
        "data": prompts,
        "success": True
    }
