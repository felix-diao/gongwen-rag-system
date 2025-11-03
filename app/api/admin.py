from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import timedelta
from app.models.database import get_db, User
from app.models.schemas import UserLogin, Token
from app.utils.auth import (
    verify_password, 
    get_password_hash, 
    create_access_token,
    get_current_user
)
from app.config import settings
import uuid

router = APIRouter(prefix="/api/auth", tags=["认证管理"])

@router.post("/register")
def register(
    username: str,
    password: str,
    department: str = None,
    db: Session = Depends(get_db)
):
    """用户注册"""
    existing_user = db.query(User).filter(User.username == username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    user_id = f"user_{uuid.uuid4().hex[:16]}"
    hashed_password = get_password_hash(password)
    
    db_user = User(
        user_id=user_id,
        username=username,
        hashed_password=hashed_password,
        department=department,
        role="user"
    )
    
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    return {
        "user_id": db_user.user_id,
        "username": db_user.username,
        "department": db_user.department
    }

@router.post("/login", response_model=Token)
def login(
    login_data: UserLogin,
    db: Session = Depends(get_db)
):
    """用户登录"""
    user = db.query(User).filter(User.username == login_data.username).first()
    
    if not user or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
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
    
    return Token(access_token=access_token, token_type="bearer")

@router.get("/me")
def get_current_user_info(current_user: dict = Depends(get_current_user)):
    """获取当前用户信息"""
    return current_user

@router.post("/logout")
def logout(current_user: dict = Depends(get_current_user)):
    """用户登出"""
    return {"message": "登出成功"}