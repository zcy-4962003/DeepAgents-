"""
密码哈希与 JWT 工具

密码使用 bcrypt 单向哈希存储；登录成功后签发 HS256 JWT，载荷中包含
用户 id 与角色，后续接口凭它识别身份，无需再查库。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt

from app import config

# bcrypt 算法本身只处理前 72 字节，超长密码必须先截断，否则 bcrypt 5.x 会直接报错
_BCRYPT_MAX_BYTES = 72


def _to_bcrypt_bytes(password: str) -> bytes:
    """把明文密码编码并按 bcrypt 上限截断。"""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """
    生成 bcrypt 密码摘要。

    :param password: 用户提交的明文密码
    :return: 可直接存库的哈希字符串
    """
    return bcrypt.hashpw(_to_bcrypt_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """
    校验明文密码是否与存库摘要匹配。

    摘要格式非法时返回 False 而不是抛异常，避免脏数据把登录接口打挂。
    """
    try:
        return bcrypt.checkpw(
            _to_bcrypt_bytes(password), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def create_access_token(
    user_id: str,
    role: str,
    username: str,
    expires_minutes: Optional[int] = None,
) -> str:
    """
    签发访问令牌。

    :param user_id: 用户 UUID 字符串，写入标准声明 sub
    :param role: member / admin，用于接口层快速判断权限
    :param username: 展示用用户名，方便前端不从额外接口取
    :param expires_minutes: 有效期（分钟），默认取 JWT_EXPIRE_MINUTES
    :return: 编码后的 JWT 字符串
    """
    expire_minutes = expires_minutes or config.JWT_EXPIRE_MINUTES
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """
    解码并校验令牌。

    :param token: 前端 Authorization 头里的 JWT
    :return: 校验通过的载荷；过期或签名不符时返回 None
    """
    try:
        return jwt.decode(
            token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM]
        )
    except jwt.PyJWTError:
        return None
