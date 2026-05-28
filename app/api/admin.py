from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from datetime import timedelta, datetime
from app.models.database import get_db, User, LoginTicket
from app.models.schemas import (
    UserLogin,
    UserRegister,
    Token,
    PasswordChange,
    PasswordChangeResponse,
    StandardResponse,
    LogoutResponse,
    RegisterResponse,
    CreateTicketRequest,
    CreateTicketResponse,
    RedeemTicketRequest,
    RedeemTicketResponse,
    ResetPasswordByUsernameRequest,
    ResetPasswordByUsernameResponse,
    SetPasswordRequest,
    SetPasswordResponse,
)
from app.utils.auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    decode_access_token,
    get_current_user,
    generate_random_password,
)
from app.config import settings
import uuid
import secrets
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
        "is_admin": user.role == "admin",  # 便于前端判断
        "needs_password_setup": getattr(user, 'needs_password_setup', False)  # 新用户需要设置密码
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


# ========== 一次性 Ticket 相关接口 ==========

TICKET_EXPIRE_MINUTES = 5  # ticket 有效期 5 分钟


@router.post("/create-ticket", response_model=StandardResponse[CreateTicketResponse])
def create_ticket(
    ticket_data: CreateTicketRequest,
    db: Session = Depends(get_db)
):
    """
    创建一次性登录 ticket

    用于对接平台无感知登录：
    - 如果用户存在，直接创建 ticket
    - 如果用户不存在，自动注册用户（生成随机密码）并创建 ticket

    ticket 有效期 5 分钟，只能使用一次
    """
    try:
        username = ticket_data.username.strip()

        # 检查用户是否存在
        user = db.query(User).filter(User.username == username).first()

        if not user:
            # 用户不存在，自动注册
            user_id = f"user_{uuid.uuid4().hex[:16]}"
            random_password = generate_random_password()
            hashed_password = get_password_hash(random_password)

            user = User(
                user_id=user_id,
                username=username,
                hashed_password=hashed_password,
                department=None,
                role="user",
                needs_password_setup=False
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info(f"自动注册新用户: {username}, user_id: {user_id}")

        # 生成一次性 ticket
        ticket_str = f"ticket_{secrets.token_hex(24)}"
        expires_at = datetime.utcnow() + timedelta(minutes=TICKET_EXPIRE_MINUTES)

        login_ticket = LoginTicket(
            ticket=ticket_str,
            username=username,
            is_used=False,
            expires_at=expires_at
        )
        db.add(login_ticket)
        db.commit()

        logger.info(f"创建 ticket: {ticket_str}, 用户: {username}, 过期时间: {expires_at}")

        return StandardResponse(
            success=True,
            data=CreateTicketResponse(
                ticket=ticket_str,
                expires_in=TICKET_EXPIRE_MINUTES * 60
            ),
            message="ticket 创建成功"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception(f"创建 ticket 失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建 ticket 失败: {str(e)}"
        )


@router.post("/redeem-ticket", response_model=StandardResponse[RedeemTicketResponse])
def redeem_ticket(
    request: Request,
    ticket_data: RedeemTicketRequest,
    db: Session = Depends(get_db)
):
    """
    兑换一次性登录 ticket

    用于前端自动登录：
    - 验证 ticket 是否有效（存在、未使用、未过期）
    - 标记 ticket 为已使用
    - 返回 JWT token
    """
    try:
        ticket_str = ticket_data.ticket.strip()

        # 查询 ticket
        login_ticket = db.query(LoginTicket).filter(LoginTicket.ticket == ticket_str).first()

        if not login_ticket:
            return StandardResponse(
                success=False,
                data=None,
                message="ticket 不存在"
            )

        # 检查是否已使用
        if login_ticket.is_used:
            return StandardResponse(
                success=False,
                data={"username": login_ticket.username},
                message="ticket 已被使用"
            )

        # 检查是否过期
        if datetime.utcnow() > login_ticket.expires_at:
            return StandardResponse(
                success=False,
                data={"username": login_ticket.username},
                message="ticket 已过期"
            )

        # 查询用户
        user = db.query(User).filter(User.username == login_ticket.username).first()
        if not user:
            return StandardResponse(
                success=False,
                data=None,
                message="关联用户不存在"
            )

        # 从 Authorization header 提取当前 token，校验 username 是否与 ticket 一致
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                token_str = auth_header[len("Bearer "):]
                token_payload = decode_access_token(token_str)
                token_username = token_payload.get("username")
                if token_username and token_username != login_ticket.username:
                    logger.info(
                        f"Token 用户 ({token_username}) 与 Ticket 用户 ({login_ticket.username}) 不一致，"
                        f"以 Ticket 为准更新 Access Token"
                    )
            except Exception:
                pass

        # 标记 ticket 为已使用
        login_ticket.is_used = True
        db.commit()

        # 生成 JWT token
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={
                "sub": user.user_id,
                "username": user.username,
                "role": user.role
            },
            expires_delta=access_token_expires
        )

        logger.info(f"兑换 ticket 成功: {ticket_str}, 用户: {user.username}")

        return StandardResponse(
            success=True,
            data=RedeemTicketResponse(
                access_token=access_token,
                token_type="bearer",
                user_id=user.user_id,
                username=user.username
            ),
            message="登录成功"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception(f"兑换 ticket 失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"兑换 ticket 失败: {str(e)}"
        )


@router.post("/reset-password-by-username", response_model=StandardResponse[ResetPasswordByUsernameResponse])
def reset_password_by_username(
    password_data: ResetPasswordByUsernameRequest,
    db: Session = Depends(get_db)
):
    """
    根据用户名重置密码

    用于对接平台为用户设置密码（用户首次登录后可使用此接口设置自己的密码）
    """
    try:
        username = password_data.username.strip()

        # 查询用户
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return StandardResponse(
                success=False,
                data=None,
                message="用户不存在"
            )

        # 更新密码
        try:
            new_hashed_password = get_password_hash(password_data.new_password)
            user.hashed_password = new_hashed_password
            db.commit()
            db.refresh(user)
        except ValueError as e:
            db.rollback()
            return StandardResponse(
                success=False,
                data=None,
                message=str(e)
            )
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"密码更新失败: {str(e)}"
            )

        logger.info(f"重置密码成功: {username}")

        return StandardResponse(
            success=True,
            data=ResetPasswordByUsernameResponse(
                message="密码重置成功",
                user_id=user.user_id,
                username=user.username,
                changed_at=datetime.utcnow()
            ),
            message="密码重置成功"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception(f"重置密码失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"服务器错误: {str(e)}"
        )


@router.post("/set-password", response_model=StandardResponse[SetPasswordResponse])
def set_password(
    password_data: SetPasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    新用户设置密码（不需要旧密码）

    用于通过 ticket 登录的新用户首次设置密码：
    - 只有 needs_password_setup=True 的用户才能调用
    - 设置密码后，needs_password_setup 自动设为 False
    """
    try:
        # 获取用户
        user = db.query(User).filter(User.user_id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )

        # 检查是否需要设置密码
        if not getattr(user, 'needs_password_setup', False):
            return StandardResponse(
                success=False,
                data=None,
                message="您已设置过密码，请使用修改密码功能"
            )

        # 更新密码
        try:
            new_hashed_password = get_password_hash(password_data.new_password)
            user.hashed_password = new_hashed_password
            user.needs_password_setup = False  # 标记已设置密码
            db.commit()
            db.refresh(user)
        except ValueError as e:
            db.rollback()
            return StandardResponse(
                success=False,
                data=None,
                message=str(e)
            )
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"密码设置失败: {str(e)}"
            )

        logger.info(f"新用户设置密码成功: {user.username}")

        return StandardResponse(
            success=True,
            data=SetPasswordResponse(
                message="密码设置成功",
                user_id=user.user_id,
                username=user.username,
                changed_at=datetime.utcnow()
            ),
            message="密码设置成功"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception(f"设置密码失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"服务器错误: {str(e)}"
        )
