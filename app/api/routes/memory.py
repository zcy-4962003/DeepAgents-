"""
长期记忆接口

用户只能读写自己的记忆：命名空间由服务端从 JWT 解析出的 user_id 决定，
前端无法通过参数访问他人记忆。提供查询与删除，方便用户纠正系统对自己的错误认知。
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import MemoryItem
from app.auth.deps import CurrentUser, get_current_user
from app.services.audit import AuditAction, record_audit
from app.services.memory import delete_memory, list_memories

router = APIRouter(prefix="/api", tags=["memory"])


@router.get("/memories", summary="我的长期记忆")
async def get_my_memories(
    current_user: CurrentUser = Depends(get_current_user),
    limit: int = Query(100, ge=1, le=500),
):
    """列出当前用户的全部长期记忆。"""
    items = await list_memories(str(current_user.id), limit=limit)
    return {"items": [MemoryItem(**item) for item in items]}


@router.delete("/memories/{key}", summary="删除一条长期记忆")
async def remove_memory(
    key: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    删除一条记忆。

    用户发现系统记错时的纠错入口；删除范围严格限制在本人命名空间内。
    """
    try:
        await delete_memory(str(current_user.id), key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除记忆失败：{exc}") from exc

    await record_audit(
        action=AuditAction.MEMORY_WRITE,
        user_id=current_user.id,
        resource_type="memory",
        resource_id=key,
        detail={"operation": "delete"},
    )
    return {"status": "deleted", "key": key}
