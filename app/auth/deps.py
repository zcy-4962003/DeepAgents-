"""
鉴权依赖注入

FastAPI 接口通过 `Depends(get_current_user)` 拿到当前用户；需要管理员权限的
接口再加一层 `Depends(require_admin)`。WebSocket 握手因为不能自定义请求头，
改为从查询参数 `?token=` 中解析 JWT，复用同一套解码逻辑。
"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token
from app.db.models import ROLE_ADMIN, User
from app.db.session import get_session

# auto_error=False：没有 Authorization 头时由我们自己返回中文 401，而不是默认英文报错
_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """当前登录用户的最小信息集合，接口层只需这三项即可完成鉴权与审计。"""

    id: uuid.UUID
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def _unauthorized(detail: str = "未登录或登录已过期") -> HTTPException:
    """统一构造 401 响应，附带 WWW-Authenticate 头符合 HTTP 语义。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def resolve_user_from_token(
    token: str, session: AsyncSession
) -> User:
    """
    从 JWT 还原数据库中的用户记录。

    除了校验签名和有效期，还要确认用户仍然存在且未被停用，
    这样管理员停用账号后旧令牌会立即失效。
    """
    payload = decode_access_token(token)
    if not payload:
        raise _unauthorized()

    raw_user_id = payload.get("sub")
    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError):
        raise _unauthorized("令牌格式不正确")

    user = await session.get(User, user_id)
    if user is None:
        raise _unauthorized("用户不存在")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号已被停用"
        )
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """HTTP 接口依赖：解析 Authorization: Bearer <token> 并返回当前用户。"""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("缺少访问令牌")

    user = await resolve_user_from_token(credentials.credentials, session)
    return CurrentUser(id=user.id, username=user.username, role=user.role)


async def require_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """管理员专用依赖：非 admin 一律 403。"""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    return current_user


async def get_user_from_websocket(
    websocket_token: str | None, session: AsyncSession
) -> CurrentUser:
    """
    WebSocket 握手鉴权：令牌来自 ?token= 查询参数，校验规则与 HTTP 完全一致。

    返回 CurrentUser 而不是 User 模型：下游的归属校验（task_ownership）按
    CurrentUser 的 is_admin 判断权限，返回模型对象会让那里抛 AttributeError。
    """
    if not websocket_token:
        raise _unauthorized("WebSocket 连接缺少访问令牌")

    user = await resolve_user_from_token(websocket_token, session)
    return CurrentUser(id=user.id, username=user.username, role=user.role)


def client_ip(request: Request) -> str | None:
    """提取客户端 IP 用于审计；经反向代理时优先取 X-Forwarded-For 第一段。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
