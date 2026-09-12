"""
任务接口

覆盖任务的创建（入队）、取消、列表、详情、事件回放与结果对比。
所有按 thread_id 访问的接口都会先经 task_ownership 校验归属：
employee 只能看到自己的任务，admin 可以看全员。
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CancelTaskResponse,
    EventItem,
    EventListResponse,
    TaskCreated,
    TaskDetail,
    TaskListResponse,
    TaskRequest,
)
from app.auth.deps import CurrentUser, client_ip, get_current_user
from app.db.models import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_QUEUED,
    TASK_TERMINAL_STATUSES,
    Task,
    User,
)
from app.db.session import get_session
from app.queue.enqueue import close_arq_pool, enqueue_task, request_cancel
from app.services.audit import AuditAction, record_audit
from app.services.event_store import fetch_events, record_event
from app.services.file_governance import bind_staged_files, task_title_from_query
from app.services.task_ownership import get_task_checked

router = APIRouter(prefix="/api", tags=["tasks"])


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _to_detail(task: Task, owner: Optional[str] = None) -> TaskDetail:
    return TaskDetail(
        id=str(task.id),
        title=task.title,
        query=task.query,
        status=task.status,
        attempts=task.attempts,
        max_attempts=task.max_attempts,
        result=task.result,
        last_error=task.last_error,
        owner=owner,
        created_at=_iso(task.created_at),
        started_at=_iso(task.started_at),
        finished_at=_iso(task.finished_at),
    )


@router.post("/task", response_model=TaskCreated, summary="创建并提交任务")
async def create_task(
    payload: TaskRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    创建任务并入队。

    thread_id 由服务端生成（前端传的旧 thread_id 只有在任务属于自己时才认可），
    解决了旧版本「前端可任意伪造 thread_id 读取他人会话」的问题。
    """
    query = payload.query.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="任务内容不能为空"
        )

    task_id: uuid.UUID
    if payload.thread_id:
        # 续聊：复用已有会话，让短期记忆接上之前的上下文
        existing = await get_task_checked(session, payload.thread_id, current_user)
        if existing.status not in TASK_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该会话还有任务正在执行，请等待完成或先取消",
            )
        existing.query = query
        existing.status = TASK_STATUS_QUEUED
        existing.last_error = None
        existing.priority = payload.priority
        # 重置尝试计数：续聊是用户的一次新提问，不是上一轮的重试，不该继承
        # 上一轮消耗掉的重试预算。不重置的话计数会跨轮累加（界面上出现过
        # 「第 4/3 次尝试」这种自相矛盾的提示），而且下一次失败时会被
        # worker 判定为「已达重试上限」而直接放弃重试（见 worker.run_task）。
        existing.attempts = 0
        task_id = existing.id
    else:
        task = Task(
            user_id=current_user.id,
            title=await task_title_from_query(query),
            query=query,
            status=TASK_STATUS_QUEUED,
            max_attempts=_default_max_attempts(),
            priority=payload.priority,
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    # 首轮附件：把本次会话开始前暂存的文件认领到任务上。必须与建任务在同一个事务里
    # 提交，且发生在入队之前——否则 worker 可能先一步执行，读不到附件。
    await bind_staged_files(
        session, current_user.id, task_id, payload.file_ids
    )

    await session.commit()

    job_id = await enqueue_task(str(task_id), query, str(current_user.id))
    if not job_id:
        # 队列不可用时立刻把状态置为失败，避免前端一直显示「排队中」
        failed = await session.get(Task, task_id)
        if failed is not None:
            failed.status = "failed"
            failed.last_error = "任务入队失败，请检查 Redis 是否可用"
            await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务入队失败，请检查 Redis 是否可用",
        )

    stored = await session.get(Task, task_id)
    if stored is not None:
        stored.arq_job_id = job_id
        await session.commit()

    # 排队状态也作为一条事件落库，前端刷新后能看到「排队中」而不是空白。
    # 带上 query 是因为前端靠 status=queued 切分「一轮提问」：续聊时会话里的
    # 历史事件会被完整回放，只有从这一条里才拿得到本轮用户问了什么。
    await record_event(
        str(task_id),
        {
            "type": "monitor_event",
            "event": "task_status",
            "message": f"任务已提交，当前排队位置由队列调度决定",
            "data": {"status": TASK_STATUS_QUEUED, "job_id": job_id, "query": query},
            "timestamp": datetime.now().isoformat(),
        },
        current_user.id,
    )

    await record_audit(
        action=AuditAction.TASK_CREATE,
        user_id=current_user.id,
        resource_type="task",
        resource_id=task_id,
        detail={"query": query[:500], "job_id": job_id},
        ip=client_ip(request),
    )

    return TaskCreated(status="queued", thread_id=str(task_id), task_id=str(task_id))


def _default_max_attempts() -> int:
    """任务默认重试上限，取自配置，保证与 worker 的 max_tries 一致。"""
    from app import config

    return config.TASK_MAX_ATTEMPTS


@router.post(
    "/task/{thread_id}/cancel",
    response_model=CancelTaskResponse,
    summary="取消任务",
)
async def cancel_task(
    thread_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    取消任务。

    分两种情况：任务还在排队 -> 直接从队列中摘掉；任务正在执行 -> 写取消标志，
    worker 的看门狗会在一到两个轮询周期内中断 Agent。

    两种情况都立即把任务落成 cancelled：worker 不在线时 arq 的 abort 等不到确认，
    若这时不落终态，任务会永远停在 queued —— 既取消不掉，也因为是「非终态」而删不掉。
    """
    task = await get_task_checked(session, thread_id, current_user)

    if task.status in TASK_TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="任务已结束，无法取消"
        )

    result = await request_cancel(thread_id)

    # 确认已取消（或 worker 不在线无法确认，取消标志仍会拦住后续执行）
    task.status = TASK_STATUS_CANCELLED
    task.finished_at = datetime.now()
    # 队列里的残留作业由 worker 的终态护栏挡住，见 worker._mark_running
    await session.commit()

    await record_audit(
        action=AuditAction.TASK_CANCEL,
        user_id=current_user.id,
        resource_type="task",
        resource_id=thread_id,
        detail={"result": result, "status": task.status},
        ip=client_ip(request),
    )

    return CancelTaskResponse(status=result, thread_id=thread_id)


@router.get("/tasks", response_model=TaskListResponse, summary="任务历史列表")
async def list_tasks(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    task_status: Optional[str] = Query(None, alias="status", description="按状态过滤"),
    keyword: Optional[str] = Query(None, description="按标题/问题模糊搜索"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """分页返回任务历史；admin 可以看全员任务并附带归属人。"""
    conditions = []
    if not current_user.is_admin:
        conditions.append(Task.user_id == current_user.id)
    if task_status:
        conditions.append(Task.status == task_status)
    if keyword:
        conditions.append(Task.query.ilike(f"%{keyword}%"))

    count_stmt = select(func.count()).select_from(Task)
    list_stmt = select(Task).order_by(Task.created_at.desc())
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = await session.scalar(count_stmt) or 0
    rows = list(await session.scalars(list_stmt.limit(limit).offset(offset)))

    # admin 视角需要知道每条任务属于谁，这里一次性查出用户名避免 N+1
    owners: dict[uuid.UUID, str] = {}
    if current_user.is_admin and rows:
        user_ids = {task.user_id for task in rows}
        user_rows = await session.scalars(
            select(User).where(User.id.in_(user_ids))
        )
        owners = {user.id: user.username for user in user_rows}

    return TaskListResponse(
        total=total,
        items=[_to_detail(task, owners.get(task.user_id)) for task in rows],
    )


@router.get("/task/{thread_id}", response_model=TaskDetail, summary="任务详情")
async def get_task(
    thread_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """返回单个任务的状态与最终结果。"""
    task = await get_task_checked(session, thread_id, current_user)
    return _to_detail(task)


@router.get(
    "/task/{thread_id}/events",
    response_model=EventListResponse,
    summary="事件回放",
)
async def get_task_events(
    thread_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    event_type: Optional[str] = Query(None, alias="type", description="按事件类型筛选"),
    after: int = Query(0, ge=0, description="只返回 id 大于该值的事件（增量续播）"),
    limit: int = Query(500, ge=1, le=2000),
):
    """
    WebSocket 历史事件回放。

    前端刷新页面后调用它恢复完整执行轨迹；带上 `after=已收到的最大 id`
    即可只拉增量，避免整段历史重复传输。
    """
    await get_task_checked(session, thread_id, current_user)

    items = await fetch_events(
        session, thread_id, event_type=event_type, after_id=after, limit=limit
    )
    next_after = max((item["id"] for item in items), default=after)

    return EventListResponse(
        items=[EventItem(**item) for item in items], next_after=next_after
    )


@router.get("/tasks/compare", summary="结果对比")
async def compare_tasks(
    ids: str = Query(..., description="逗号分隔的任务 ID 列表，2~5 个"),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    取多条任务的结果用于并排对比。

    只返回归属校验通过的任务；任何一条不属于当前用户都会整体拒绝，
    避免通过对比接口探测他人任务是否存在。
    """
    task_ids = [item.strip() for item in ids.split(",") if item.strip()]
    if not 2 <= len(task_ids) <= 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请提供 2~5 个任务 ID 进行对比",
        )

    items = []
    for task_id in task_ids:
        task = await get_task_checked(session, task_id, current_user)
        items.append(_to_detail(task))

    return {"items": items}


@router.delete("/task/{thread_id}", summary="删除任务")
async def delete_task(
    thread_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    删除任务及其事件/文件元数据。

    正在执行的任务不允许删除，必须先取消 —— 否则 worker 会写回一个已不存在的任务。
    """
    task = await get_task_checked(session, thread_id, current_user)

    if task.status not in TASK_TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先取消任务再删除"
        )

    await session.delete(task)
    await session.commit()

    await record_audit(
        action=AuditAction.TASK_DELETE,
        user_id=current_user.id,
        resource_type="task",
        resource_id=thread_id,
        ip=client_ip(request),
    )
    return {"status": "deleted", "thread_id": thread_id}
