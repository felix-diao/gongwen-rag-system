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
from app.utils.logger import get_logger

logger = get_logger("prompt_service")


class PromptService:
    """Prompt 模板服务"""
    
    def list_prompts(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,  # 新增：是否管理员
        name: Optional[str] = None,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
        is_public: Optional[bool] = None,
        current: int = 1,
        page_size: int = 10
    ) -> Tuple[List[dict], int]:
        """
        获取 Prompt 模板列表
        
        权限规则：
        - 普通用户：看到公共模板 + 自己的私有模板
        - 管理员：看到所有模板
        """
        # 基础查询
        query = db.query(PromptTemplate)
        
        # 权限过滤
        if is_admin:
            # 管理员：查看所有模板
            pass
        else:
            # 普通用户：公共模板 OR 自己的私有模板
            query = query.filter(
                or_(
                    PromptTemplate.is_public == True,
                    PromptTemplate.user_id == user_id
                )
            )
        
        # 名称搜索
        if name:
            query = query.filter(PromptTemplate.name.ilike(f"%{name}%"))
        
        # 分类筛选
        if category:
            query = query.filter(PromptTemplate.category == category)
        
        # 启用状态筛选
        if is_active is not None:
            query = query.filter(PromptTemplate.is_active == is_active)

        # 公共/私有筛选（仅管理员需要，可选）
        if is_public is not None:
            query = query.filter(PromptTemplate.is_public == is_public)
        
        # 总数
        total = query.count()
        
        # 分页
        offset = (current - 1) * page_size
        prompts = query.order_by(PromptTemplate.updated_at.desc())\
                      .offset(offset)\
                      .limit(page_size)\
                      .all()
        
        return [self._to_response_dict(p) for p in prompts], total
    
    def get_prompt_by_id(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,
        prompt_id: str
    ) -> dict:
        """
        获取单个 Prompt 模板
        
        权限规则：
        - 公共模板：所有人可见
        - 私有模板：只有创建者可见（管理员除外）
        """
        prompt = db.query(PromptTemplate).filter(
            PromptTemplate.id == int(prompt_id)
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        # 权限检查
        if not is_admin and not prompt.is_public and prompt.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此模板"
            )
        
        return self._to_response_dict(prompt)
    
    def create_prompt(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,
        payload: PromptTemplateCreate
    ) -> dict:
        """
        创建 Prompt 模板
        
        权限规则：
        - 普通用户：只能创建私有模板（即使提交 is_public=True 也会被强制改为 False）
        - 管理员：可以创建公共或私有模板
        """
        # 权限检查：普通用户强制创建私有模板
        if not is_admin and payload.is_public:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="普通用户只能创建私有模板"
            )
        
        # 检查名称是否重复（同一用户下）
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
            is_active=payload.is_active,
            is_public=payload.is_public  # 保存公共/私有状态
        )
        
        db.add(prompt)
        db.commit()
        db.refresh(prompt)
        
        return self._to_response_dict(prompt)
    
    def update_prompt(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,
        prompt_id: str,
        payload: PromptTemplateUpdate
    ) -> dict:
        """
        更新 Prompt 模板
        
        权限规则：
        - 公共模板：只有管理员可以修改
        - 私有模板：只有创建者可以修改（管理员也可以）
        """
        prompt = db.query(PromptTemplate).filter(
            PromptTemplate.id == int(prompt_id)
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        # 权限检查
        if prompt.is_public:
            # 公共模板：只有管理员可以修改
            if not is_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="只有管理员可以修改公共模板"
                )
        else:
            # 私有模板：只有创建者可以修改（管理员除外）
            if not is_admin and prompt.user_id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="无权修改此模板"
                )
        
        # 防止普通用户将私有模板改为公共
        if not is_admin and payload.is_public is True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="普通用户无法将模板设置为公共"
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
            # camelCase 转 snake_case
            snake_field = field
            if field == "isActive":
                snake_field = "is_active"
            elif field == "isPublic":
                snake_field = "is_public"
            
            setattr(prompt, snake_field, value)
        
        db.commit()
        db.refresh(prompt)
        
        return self._to_response_dict(prompt)
    
    def delete_prompt(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,
        prompt_id: str
    ) -> None:
        """
        删除 Prompt 模板
        
        权限规则：
        - 公共模板：只有管理员可以删除
        - 私有模板：只有创建者可以删除（管理员也可以）
        """
        prompt = db.query(PromptTemplate).filter(
            PromptTemplate.id == int(prompt_id)
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        # 权限检查
        if prompt.is_public:
            if not is_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="只有管理员可以删除公共模板"
                )
        else:
            if not is_admin and prompt.user_id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="无权删除此模板"
                )
        
        db.delete(prompt)
        db.commit()
    
    def batch_delete_prompts(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,
        prompt_ids: List[str]
    ) -> int:
        """
        批量删除 Prompt 模板
        
        权限规则：
        - 普通用户：只能删除自己的私有模板
        - 管理员：可以删除所有模板
        """
        int_ids = [int(pid) for pid in prompt_ids]
        
        # 获取要删除的模板
        prompts = db.query(PromptTemplate).filter(
            PromptTemplate.id.in_(int_ids)
        ).all()
        
        deleted_count = 0
        for prompt in prompts:
            # 权限检查
            if prompt.is_public:
                if not is_admin:
                    continue  # 跳过公共模板
            else:
                if not is_admin and prompt.user_id != user_id:
                    continue  # 跳过他人的私有模板
            
            db.delete(prompt)
            deleted_count += 1
        
        db.commit()
        return deleted_count
    
    def toggle_prompt_active(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,
        prompt_id: str,
        is_active: bool
    ) -> dict:
        """
        切换 Prompt 模板启用状态
        
        权限规则：与更新接口一致
        """
        prompt = db.query(PromptTemplate).filter(
            PromptTemplate.id == int(prompt_id)
        ).first()
        
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Prompt 模板不存在"
            )
        
        # 权限检查
        if prompt.is_public:
            if not is_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="只有管理员可以修改公共模板状态"
                )
        else:
            if not is_admin and prompt.user_id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="无权修改此模板状态"
                )
        
        prompt.is_active = is_active
        db.commit()
        db.refresh(prompt)
        
        return self._to_response_dict(prompt)
    
    def get_prompts_by_category(
        self,
        db: Session,
        user_id: str,
        is_admin: bool,
        category: str
    ) -> List[dict]:
        """
        按分类获取 Prompt 模板（仅启用的）
        
        权限规则：与列表接口一致
        """
        query = db.query(PromptTemplate).filter(
            and_(
                PromptTemplate.category == category,
                PromptTemplate.is_active == True
            )
        )
        
        # 权限过滤
        if not is_admin:
            query = query.filter(
                or_(
                    PromptTemplate.is_public == True,
                    PromptTemplate.user_id == user_id
                )
            )
        
        prompts = query.order_by(PromptTemplate.updated_at.desc()).all()
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
            "isPublic": prompt.is_public,  # 新增
            "userId": prompt.user_id,      # 新增
            "createdAt": prompt.created_at.isoformat() if prompt.created_at else None,
            "updatedAt": prompt.updated_at.isoformat() if prompt.updated_at else None,
        }


# 创建服务实例
prompt_service = PromptService()
