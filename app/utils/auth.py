from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt  # 直接使用 bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import settings
import secrets
import string

security = HTTPBearer()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    try:
        password_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    """密码哈希"""
    if len(password.encode('utf-8')) > 72:
        raise ValueError("密码不能超过72字节")
    
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt(rounds=12)  # 12轮加密
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建访问令牌"""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    
    return encoded_jwt

def decode_access_token(token: str) -> dict:
    """解码令牌"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """获取当前用户"""
    token = credentials.credentials
    payload = decode_access_token(token)
    
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return {"user_id": user_id, "username": payload.get("username"), "role": payload.get("role")}


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    要求管理员权限的依赖函数
    
    使用方式:
        @router.post("/xxx")
        def admin_only_endpoint(admin: dict = Depends(require_admin)):
            # 只有管理员才能访问
            ...
    
    Args:
        current_user: 当前用户信息（从 JWT 解析）
    
    Returns:
        dict: 管理员用户信息
    
    Raises:
        HTTPException: 如果不是管理员，返回 403
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限才能执行此操作"
        )
    return current_user


def check_admin_or_owner(current_user: dict, resource_owner_id: str) -> bool:
    """
    检查是否是管理员或资源所有者
    
    使用场景: 允许管理员访问所有资源，普通用户只能访问自己的资源
    
    Args:
        current_user: 当前用户信息
        resource_owner_id: 资源所有者的 user_id
    
    Returns:
        bool: 是否有权限
    
    示例:
        if not check_admin_or_owner(current_user, knowledge_base.user_id):
            raise HTTPException(status_code=403, detail="无权访问")
    """
    return (
        current_user.get("role") == "admin" or 
        current_user.get("user_id") == resource_owner_id
    )


def require_admin_or_owner(
    resource_owner_id: str,
    current_user: dict = Depends(get_current_user)
) -> dict:
    """
    要求管理员或资源所有者权限（依赖函数版本）

    使用方式:
        @router.delete("/knowledge/{item_id}")
        def delete_item(
            item_id: int,
            db: Session = Depends(get_db),
            current_user: dict = Depends(get_current_user)
        ):
            # 先获取资源
            item = db.query(KnowledgeItem).filter_by(id=item_id).first()

            # 检查权限
            if not check_admin_or_owner(current_user, item.user_id):
                raise HTTPException(status_code=403, detail="无权操作")

            # 执行删除
            ...
    """
    if not check_admin_or_owner(current_user, resource_owner_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此资源"
        )
    return current_user


def generate_random_password(length: int = 16) -> str:
    """
    生成符合密码规则的随机密码

    规则：
    - 长度：8-72位（默认16位）
    - 必须包含至少一个大写字母
    - 必须包含至少一个小写字母
    - 必须包含至少一个数字
    - 必须包含至少一个特殊符号 !@#$%^&*

    Args:
        length: 密码长度，默认16位

    Returns:
        str: 随机生成的密码
    """
    if length < 8:
        length = 8
    if length > 72:
        length = 72

    # 确保包含各类必要字符
    upper = secrets.choice(string.ascii_uppercase)
    lower = secrets.choice(string.ascii_lowercase)
    digit = secrets.choice(string.digits)
    special = secrets.choice('!@#$%^&*')

    # 剩余字符从所有字符中随机选择
    all_chars = string.ascii_letters + string.digits + '!@#$%^&*'
    remaining = length - 4
    rest = ''.join(secrets.choice(all_chars) for _ in range(remaining))

    # 组合并打乱顺序
    password = list(upper + lower + digit + special + rest)
    secrets.SystemRandom().shuffle(password)
    return ''.join(password)