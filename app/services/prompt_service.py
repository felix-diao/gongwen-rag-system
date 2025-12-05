from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from typing import List, Optional, Tuple
from fastapi import HTTPException, status

from app.models.database import PromptTemplate
from app.models.schemas import (
    PromptTemplateCreate,
    PromptTemplateUpdate,
    PromptTemplateResponse
)


class PromptService:
    """Prompt 模板服务"""
    
    def list_prompts(
        self,
        db: Session,
        user_id: str,
        name: Optional[str] = None,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
        current: int = 1,
        page_size: int = 10
    ) -> Tuple[List[dict], int]:
        """
        获取 Prompt 模板列表
        
        Args:
            db: 数据库会话
            user_id: 用户ID
            name: 名称搜索关键词
            category: 分类筛选
            is_active: 启用状态筛选
            current: 当前页
            page_size: 每页大小
            
        Returns:
            (模板列表, 总数)
        """
        query = db.query(PromptTemplate).filter(PromptTemplate.user_id == user_id)
        
        # 名称搜索
        if name:
            query = query.filter(PromptTemplate.name.ilike(f"%{name}%"))
        
        # 分类筛选
        if category:
            query = query.filter(PromptTemplate.category == category)
        
        # 启用状态筛选
        if is_active is not None:
            query = query.filter(PromptTemplate.is_active == is_active)
        
        # 总数
        total = query.count()
        
        # 分页
        offset = (current - 1) * page_size
        prompts = query.order_by(PromptTemplate.updated_at.desc())\
                      .offset(offset)\
                      .limit(page_size)\
                      .all()
        
        # 转换为字典
        return [self._to_response_dict(p) for p in prompts], total
    
    def get_prompt_by_id(
        self,
        db: Session,
        user_id: str,
        prompt_id: str
    ) -> dict:
        """获取单个 Prompt 模板"""
        prompt = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.id == int(prompt_id),
                PromptTemplate.user_id == user_id
            )
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        return self._to_response_dict(prompt)
    
    def create_prompt(
        self,
        db: Session,
        user_id: str,
        payload: PromptTemplateCreate
    ) -> dict:
        """创建 Prompt 模板"""
        # 检查名称是否重复
        existing = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.user_id == user_id,
                PromptTemplate.name == payload.name
            )
        ).first()
        
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="模板名称已存在"
            )
        
        # 创建模板
        prompt = PromptTemplate(
            user_id=user_id,
            name=payload.name,
            category=payload.category,
            description=payload.description,
            content=payload.content,
            variables=payload.variables,
            is_active=payload.is_active
        )
        
        db.add(prompt)
        db.commit()
        db.refresh(prompt)
        
        return self._to_response_dict(prompt)
    
    def update_prompt(
        self,
        db: Session,
        user_id: str,
        prompt_id: str,
        payload: PromptTemplateUpdate
    ) -> dict:
        """更新 Prompt 模板"""
        prompt = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.id == int(prompt_id),
                PromptTemplate.user_id == user_id
            )
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        # 检查名称是否与其他模板重复
        if payload.name and payload.name != prompt.name:
            existing = db.query(PromptTemplate).filter(
                and_(
                    PromptTemplate.user_id == user_id,
                    PromptTemplate.name == payload.name,
                    PromptTemplate.id != int(prompt_id)
                )
            ).first()
            
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="模板名称已存在"
                )
        
        # 更新字段
        update_data = payload.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            # 将 camelCase 转为 snake_case
            snake_field = field
            if field == "isActive":
                snake_field = "is_active"
            setattr(prompt, snake_field, value)
        
        db.commit()
        db.refresh(prompt)
        
        return self._to_response_dict(prompt)
    
    def delete_prompt(
        self,
        db: Session,
        user_id: str,
        prompt_id: str
    ) -> None:
        """删除 Prompt 模板"""
        prompt = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.id == int(prompt_id),
                PromptTemplate.user_id == user_id
            )
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        db.delete(prompt)
        db.commit()
    
    def batch_delete_prompts(
        self,
        db: Session,
        user_id: str,
        prompt_ids: List[str]
    ) -> int:
        """批量删除 Prompt 模板"""
        # 转换 ID 为整数
        int_ids = [int(pid) for pid in prompt_ids]
        
        deleted_count = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.id.in_(int_ids),
                PromptTemplate.user_id == user_id
            )
        ).delete(synchronize_session=False)
        
        db.commit()
        return deleted_count
    
    def toggle_prompt_active(
        self,
        db: Session,
        user_id: str,
        prompt_id: str,
        is_active: bool
    ) -> dict:
        """切换 Prompt 模板启用状态"""
        prompt = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.id == int(prompt_id),
                PromptTemplate.user_id == user_id
            )
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        prompt.is_active = is_active
        db.commit()
        db.refresh(prompt)
        
        return self._to_response_dict(prompt)
    
    def get_prompts_by_category(
        self,
        db: Session,
        user_id: str,
        category: str
    ) -> List[dict]:
        """按分类获取 Prompt 模板（仅启用的）"""
        prompts = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.user_id == user_id,
                PromptTemplate.category == category,
                PromptTemplate.is_active == True
            )
        ).order_by(PromptTemplate.updated_at.desc()).all()
        
        return [self._to_response_dict(p) for p in prompts]
    
    def _to_response_dict(self, prompt: PromptTemplate) -> dict:
        """将数据库模型转换为响应字典"""
        return {
            "id": str(prompt.id),
            "name": prompt.name,
            "category": prompt.category,
            "description": prompt.description,
            "content": prompt.content,
            "variables": prompt.variables or [],
            "isActive": prompt.is_active,
            "createdAt": prompt.created_at.isoformat() if prompt.created_at else None,
            "updatedAt": prompt.updated_at.isoformat() if prompt.updated_at else None,
        }


# 创建服务实例
prompt_service = PromptService()