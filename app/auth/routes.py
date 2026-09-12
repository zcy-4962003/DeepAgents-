"""
账号接口：注册、登录、当前用户信息

单租户内部系统，默认开放自助注册（ALLOW_SELF_REGISTER 可关闭）。
注册出来的账号一律是 member，管理员需要手工在库里改 role 或由管理员接口提升。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.auth.deps import CurrentUser, client_ip, get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.db.models import ROLE_MEMBER, User
from app.db.session import get_session
from app.services.audit import AuditAction, record_audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    """注册请求体。"""

    username: str = Field(..., min_length=3, max_length=64, description="登录用户名")
    password: str = Field(..., min_length=6, max_length=128, description="登录密码")
    display_name: str | None = Field(None, max_length=64, description="显示名")

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        # 用户名统一小写存储，避免 Admin / admin 被当成两个账号
        cleaned = value.strip().lower()
        if not cleaned.replace("_", "").replace(".", "").isalnum():
            raise ValueError("用户名只能包含字母、数字、下划线和点")
        return cleaned


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str = Field(..., description="登录用户名")
    password: str = Field(..., description="登录密码")


class UserInfo(BaseModel):
    """返回给前端的用户信息（不含任何密码字段）。"""

    id: str
    username: str
    display_name: str | None
    role: str


class TokenResponse(BaseModel):
    """登录/注册成功后的令牌响应。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserInfo


def _to_user_info(user: User) -> UserInfo:
    return UserInfo(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        role=user.role,
    )


@router.post("/register", response_model=TokenResponse, summary="注册账号")
async def register(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """注册内部员工账号，成功后直接签发令牌，前端无需再登录一次。"""
    if not config.ALLOW_SELF_REGISTER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="当前系统未开放自助注册"
        )

    exists = await session.scalar(
        select(func.count()).select_from(User).where(User.username == payload.username)
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="用户名已存在"
        )

    user = User(
        username=payload.username,
        display_name=payload.display_name or payload.username,
        password_hash=hash_password(payload.password),
        role=ROLE_MEMBER,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    await record_audit(
        action=AuditAction.REGISTER,
        user_id=user.id,
        resource_type="user",
        resource_id=user.id,
        detail={"username": user.username},
        ip=client_ip(request),
    )

    token = create_access_token(str(user.id), user.role, user.username)
    return TokenResponse(
        access_token=token,
        expires_in=config.JWT_EXPIRE_MINUTES * 60,
        user=_to_user_info(user),
    )


@router.post("/login", response_model=TokenResponse, summary="登录")
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """校验用户名密码并签发 JWT。失败原因对外统一，避免暴露用户名是否存在。"""
    username = payload.username.strip().lower()
    user = await session.scalar(select(User).where(User.username == username))

    if user is None or not verify_password(payload.password, user.password_hash):
        await record_audit(
            action=AuditAction.LOGIN_FAILED,
            user_id=user.id if user else None,
            resource_type="user",
            resource_id=username,
            detail={"reason": "用户名或密码错误"},
            ip=client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号已被停用"
        )

    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()

    await record_audit(
        action=AuditAction.LOGIN,
        user_id=user.id,
        resource_type="user",
        resource_id=user.id,
        detail={"username": user.username, "role": user.role},
        ip=client_ip(request),
    )

    token = create_access_token(str(user.id), user.role, user.username)
    return TokenResponse(
        access_token=token,
        expires_in=config.JWT_EXPIRE_MINUTES * 60,
        user=_to_user_info(user),
    )


@router.get("/me", response_model=UserInfo, summary="获取当前登录用户")
async def me(current_user: CurrentUser = Depends(get_current_user)):
    """前端启动时用它校验本地令牌是否仍然有效。"""
    return UserInfo(
        id=str(current_user.id),
        username=current_user.username,
        display_name=current_user.username,
        role=current_user.role,
    )


@router.post("/logout", summary="退出登录")
async def logout(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    退出登录。

    JWT 是无状态的，服务端不维护会话，这里只记录审计；真正的失效由前端清除
    本地令牌完成。如需强制下线，可由管理员停用账号使旧令牌立即失效。
    """
    await record_audit(
        action=AuditAction.LOGOUT,
        user_id=current_user.id,
        resource_type="user",
        resource_id=current_user.id,
        ip=client_ip(request),
    )
    return {"status": "ok"}
