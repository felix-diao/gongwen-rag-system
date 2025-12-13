from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import timedelta, datetime
from app.models.database import get_db, User
from app.models.schemas import (
    UserLogin,
    UserRegister,
    Token,
    PasswordChange,
    PasswordChangeResponse,
    StandardResponse,
    LogoutResponse,
    RegisterResponse,
)
from app.utils.auth import (
    verify_password, 
    get_password_hash, 
    create_access_token,
    get_current_user
)
from app.config import settings
import uuid
from app.utils.logger import get_logger

logger = get_logger("admin_api")

router = APIRouter(prefix="/api/auth", tags=["认证管理"])

@router.post("/register", response_model=StandardResponse[RegisterResponse])
def register(
    register_data: UserRegister,
    db: Session = Depends(get_db)
):
    """
    用户注册
    
    密码要求：
    - 长度：8-72位
    - 必须包含至少一个大写字母
    - 必须包含至少一个小写字母
    - 必须包含至少一个数字
    - 必须包含至少一个特殊符号如 !@#$%^&*等
    - 不能使用常见弱密码（如：Password1、12345678等）
    
    用户名要求：
    - 长度：3-50个字符
    - 不能为纯数字
    - 不能包含空格
    """
    try:
        existing_user = db.query(User).filter(User.username == register_data.username).first()
        if existing_user:
            return StandardResponse(
                success=False,
                data=None,
                message="用户名已存在"
            )
        
        user_id = f"user_{uuid.uuid4().hex[:16]}"
        hashed_password = get_password_hash(register_data.password)
        
        db_user = User(
            user_id=user_id,
            username=register_data.username,
            hashed_password=hashed_password,
            department=register_data.department,
            role="user"
        )
        
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        
        return StandardResponse(
            success=True,
            data=RegisterResponse(
                user_id=db_user.user_id,
                username=db_user.username,
                department=db_user.department
            ),
            message="注册成功"
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"注册失败: {str(e)}"
        )

@router.post("/login", response_model=StandardResponse[Token])
def login(
    login_data: UserLogin,
    db: Session = Depends(get_db)
):
    """用户登录"""
    user = db.query(User).filter(User.username == login_data.username).first()
    
    if not user or not verify_password(login_data.password, user.hashed_password):
        return StandardResponse(
            success=False,
            data=None,
            message="用户名或密码错误"
        )
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={
            "sub": user.user_id,
            "username": user.username,
            "role": user.role
        },
        expires_delta=access_token_expires
    )
    
    return StandardResponse(
        success=True,
        data=Token(access_token=access_token, token_type="bearer"),
        message="登录成功"
    )

@router.get("/me")
def get_current_user_info(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    获取当前用户信息
    
    返回完整的用户信息，包括角色、部门等
    """
    # 从数据库获取完整信息
    user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    return {
        "user_id": user.user_id,
        "username": user.username,
        "role": user.role,
        "department": user.department,
        "created_at": user.created_at,
        "is_admin": user.role == "admin"  # 便于前端判断
    }

@router.post("/logout", response_model=StandardResponse[LogoutResponse])
def logout(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """用户登出"""
    user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    logout_at = datetime.utcnow()
    return StandardResponse(
        success=True,
        data=LogoutResponse(
            message="登出成功",
            user_id=user.user_id,
            username=user.username,
            logout_at=logout_at,
        ),
        message="登出成功",
    )

@router.put("/change-password", response_model=StandardResponse[PasswordChangeResponse])
def change_password(
    password_data: PasswordChange,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    用户修改密码
    
    密码要求：
    - 长度至少8位
    - 必须包含至少一个大写字母
    - 必须包含至少一个小写字母
    - 必须包含数字
    - 必须包含至少一个特殊符号如 !@#$%^&*等
    - 需要验证旧密码
    - 新密码不能与旧密码相同
    """
    try:
        # 从数据库获取完整用户信息
        user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )
        
        # 验证旧密码
        if not verify_password(password_data.old_password, user.hashed_password):
            return StandardResponse(
                success=False,
                data=None,
                message="旧密码错误"
            )
        
        # 检查新密码是否与旧密码相同
        if verify_password(password_data.new_password, user.hashed_password):
            return StandardResponse(
                success=False,
                data=None,
                message="新密码不能与旧密码相同"
            )
        
        # 更新密码
        try:
            new_hashed_password = get_password_hash(password_data.new_password)
            user.hashed_password = new_hashed_password
            db.commit()
            db.refresh(user)
        except ValueError as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"密码更新失败: {str(e)}"
            )
        
        # 返回成功响应
        return StandardResponse(
            success=True,
            data=PasswordChangeResponse(
                message="密码修改成功，请使用新密码重新登录",
                user_id=user.user_id,
                username=user.username,
                changed_at=datetime.utcnow()
            ),
            message="密码修改成功"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"服务器错误: {str(e)}"
        )
