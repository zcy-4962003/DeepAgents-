"""
arq worker 进程

启动方式（在项目根目录）：
    arq app.queue.worker.WorkerSettings

职责：
1. 消费队列中的任务，执行 DeepAgents；
2. 任务状态全程落库（queued -> running -> success/failed/cancelled）；
3. 执行过程中轮询取消标志，用户点「取消」时及时中断；
4. 成功后把生成文件收进对象存储、抽取长期记忆；失败后按次数重试；
5. 定时清理过期文件。

worker 与 API 是两个进程，因此所有状态都必须落 PostgreSQL / Redis，
不能依赖内存中的变量。
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from arq import cron
from sqlalchemy import select

from app import config
from app.agent.main_agent import run_deep_agent
from app.agent.memory_consolidator import consolidate_safely
from app.api.monitor import monitor
from app.db.models import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_TERMINAL_STATUSES,
    Task,
)
from app.db.session import dispose_engine, session_scope
from app.queue.enqueue import is_cancel_requested
from app.queue.settings import get_redis_settings
from app.services.audit import AuditAction, record_audit
from app.services.event_store import (
    close_redis,
    event_channel,
    fetch_events,
    get_redis,
    json_dumps,
    record_event,
)
from app.services.file_governance import (
    cleanup_expired_files,
    collect_generated_files,
    task_uploaded_names,
)
from app.services.memory import close_memory, setup_memory
from app.utils.async_bridge import set_main_loop


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _mark_running(task_id: str) -> Optional[Task]:
    """
    把任务置为 running 并累加重试次数。

    已经是终态的任务直接跳过，不再执行：取消请求在 worker 不在线时无法通过 arq
    abort 生效（见 enqueue.request_cancel），接口会先把任务置为 cancelled，
    队列里的作业却还留着。没有这道护栏，那个残留作业稍后被拾起时会把已取消的
    任务「复活」成 running 并真的跑一遍。

    重试不受影响：失败等待重试的任务状态仍是 running，不是终态。

    :return: 更新后的任务对象；任务不存在或已是终态时返回 None
    """
    async with session_scope() as session:
        task = await session.get(Task, uuid.UUID(str(task_id)))
        if task is None:
            return None
        if task.status in TASK_TERMINAL_STATUSES:
            return None
        task.status = TASK_STATUS_RUNNING
        task.attempts = (task.attempts or 0) + 1
        task.started_at = task.started_at or _now()
        session.add(task)
        await session.flush()
        session.expunge(task)
        return task


async def _mark_finished(
    task_id: str,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """写回任务的终态、结果与错误信息。"""
    async with session_scope() as session:
        task = await session.get(Task, uuid.UUID(str(task_id)))
        if task is None:
            return
        task.status = status
        task.finished_at = _now()
        if result is not None:
            task.result = result
        if error is not None:
            task.last_error = error[:4000]
        session.add(task)


async def _watch_cancel(task_id: str) -> bool:
    """
    轮询取消标志。

    取消请求可能来自另一个进程，无法靠内存变量传递，因此统一落到 Redis key 上。
    轮询间隔由 TASK_CANCEL_POLL_SECONDS 控制，默认 2 秒。
    """
    while True:
        await asyncio.sleep(config.TASK_CANCEL_POLL_SECONDS)
        try:
            if await is_cancel_requested(task_id):
                return True
        except Exception as exc:
            # Redis 抖动时不要让看门狗崩掉，下个周期再试
            print(f"[Worker] 取消标志查询失败：{exc}")


async def _run_with_cancel_watch(
    task_id: str, query: str, user_id: uuid.UUID
) -> tuple[str, bool]:
    """
    执行 Agent，同时监听取消信号。

    :return: (最终答复文本, 是否被取消)
    """
    runner = asyncio.create_task(run_deep_agent(query, task_id, user_id))
    watcher = asyncio.create_task(_watch_cancel(task_id))

    try:
        done, _pending = await asyncio.wait(
            {runner, watcher}, return_when=asyncio.FIRST_COMPLETED
        )

        if runner in done:
            # Agent 正常结束（或自身抛异常），先取出结果/异常
            return runner.result(), False

        # 看门狗先返回：用户请求取消
        print(f"[Worker] 收到取消请求，正在中断任务 {task_id}")
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):
            pass
        return "", True
    finally:
        watcher.cancel()
        if not runner.done():
            runner.cancel()


async def _finalize_files(task_id: str, user_id: uuid.UUID) -> int:
    """
    把工作目录里的产物收进对象存储并登记元数据。

    :return: 本次归档的文件数量
    """
    session_dir = config.OUTPUT_DIR / f"session_{task_id}"
    if not session_dir.exists():
        return 0

    async with session_scope() as session:
        uploaded_names = await task_uploaded_names(session, uuid.UUID(str(task_id)))
        records = await collect_generated_files(
            session, user_id, uuid.UUID(str(task_id)), session_dir, uploaded_names
        )
        count = len(records)

    if count:
        # 归档结果通过事件流告诉前端，前端据此刷新文件列表
        await record_event(
            task_id,
            {
                "type": "monitor_event",
                "event": "files_archived",
                "message": f"已归档 {count} 个生成文件",
                "data": {"count": count},
                "timestamp": _now().isoformat(),
            },
            user_id,
        )

    return count


async def run_task(
    ctx: dict[str, Any],
    task_id: str,
    query: str,
    user_id: str,
) -> dict[str, Any]:
    """
    队列任务主函数。

    arq 会在重试时重新调用本函数，因此每次进入都要从数据库重新读取状态，
    不能依赖 ctx 中残留的数据。
    """
    print(f"[Worker] 开始处理任务 {task_id}")

    task = await _mark_running(task_id)
    if task is None:
        # 两种情况：任务已被删除（例如用户清理），或已被取消但队列里的作业还在。
        # 都属于残留作业，直接丢弃。
        print(f"[Worker] 任务 {task_id} 不存在或已结束，跳过")
        return {"status": "skipped", "task_id": task_id}

    max_attempts = task.max_attempts or config.TASK_MAX_ATTEMPTS
    user_uuid = uuid.UUID(str(user_id))

    await record_event(
        task_id,
        {
            "type": "monitor_event",
            "event": "task_status",
            "message": f"任务开始执行（第 {task.attempts}/{max_attempts} 次尝试）",
            "data": {"status": TASK_STATUS_RUNNING, "attempt": task.attempts},
            "timestamp": _now().isoformat(),
        },
        user_uuid,
    )

    try:
        final_result, cancelled = await _run_with_cancel_watch(
            task_id, query, user_uuid
        )
    except asyncio.CancelledError:
        # arq 的 job_timeout 或 abort 会直接取消本协程，此时也要留下终态
        print(f"[Worker] 任务 {task_id} 被队列中断")
        await _mark_finished(
            task_id, TASK_STATUS_CANCELLED, error="任务被中断或超时"
        )
        raise
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        print(f"[Worker] 任务 {task_id} 执行失败：{error_text}")

        if task.attempts >= max_attempts:
            # 达到重试上限，本次彻底失败，把原因写给前端
            await _mark_finished(task_id, TASK_STATUS_FAILED, error=error_text)
            await record_event(
                task_id,
                {
                    "type": "monitor_event",
                    "event": "error",
                    "message": f"任务执行失败（已重试 {task.attempts} 次）：{error_text}",
                    "data": {"status": TASK_STATUS_FAILED},
                    "timestamp": _now().isoformat(),
                },
                user_uuid,
            )
        else:
            # 未达上限，交给 arq 按退避策略重试
            await record_event(
                task_id,
                {
                    "type": "monitor_event",
                    "event": "task_status",
                    "message": (
                        f"执行失败，将在稍后自动重试"
                        f"（{task.attempts}/{max_attempts}）：{error_text}"
                    ),
                    "data": {"status": "retrying", "attempt": task.attempts},
                    "timestamp": _now().isoformat(),
                },
                user_uuid,
            )
        raise

    if cancelled:
        await _mark_finished(task_id, TASK_STATUS_CANCELLED, error="用户取消")
        await record_audit(
            action=AuditAction.TASK_CANCEL,
            user_id=user_uuid,
            resource_type="task",
            resource_id=task_id,
            detail={"source": "worker"},
        )
        return {"status": TASK_STATUS_CANCELLED, "task_id": task_id}

    # ---- 成功路径：归档产物 -> 沉淀长期记忆 -> 写回状态 ----
    try:
        await _finalize_files(task_id, user_uuid)
    except Exception as exc:
        print(f"[Worker] 产物归档失败（不影响任务成功状态）：{exc}")

    await consolidate_safely(user_uuid, query, final_result)

    await _mark_finished(task_id, TASK_STATUS_SUCCESS, result=final_result)
    print(f"[Worker] 任务 {task_id} 执行完成")

    return {"status": TASK_STATUS_SUCCESS, "task_id": task_id}


async def cleanup_files_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    定时任务：清理过期文件。

    对象存储的带宽和存储都是按量计费的，过期数据必须主动回收，
    否则长期运行会持续产生费用。
    """
    async with session_scope() as session:
        removed = await cleanup_expired_files(session)

    if removed:
        await record_audit(
            action=AuditAction.FILE_CLEANUP,
            resource_type="file",
            detail={"removed": removed},
        )
        print(f"[Worker] 已清理 {removed} 个过期文件")

    return {"removed": removed}


async def recover_stale_tasks(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    启动时的失败恢复。

    处理两类「没人管」的任务：

    1. `running`：worker 被强杀时正在执行的任务会永远停在 running。未达重试上限的
       复位为排队重新投递，已达上限的置为 failed。
    2. `queued`：投递失败的、或队列数据被清掉的任务，会永远停在 queued —— 既不
       会被执行，又因为不是终态而删不掉（删除接口要求先取消）。启动时一并重投。
       对正常排队的任务重投是幂等的（同一个 job id，arq 只是更新分数），不会产生重复作业。
    """
    from app.queue.enqueue import enqueue_task

    async with session_scope() as session:
        rows = await session.scalars(
            select(Task).where(
                Task.status.in_([TASK_STATUS_RUNNING, TASK_STATUS_QUEUED])
            )
        )
        stale = list(rows)
        for task in stale:
            if task.status != TASK_STATUS_RUNNING:
                # 已经是 queued 的保持原样，下面统一重投
                continue
            if (task.attempts or 0) >= (task.max_attempts or config.TASK_MAX_ATTEMPTS):
                task.status = TASK_STATUS_FAILED
                task.last_error = "worker 重启，任务未完成且已达重试上限"
                task.finished_at = _now()
            else:
                # 先复位为排队，稍后由入队逻辑重新投递
                task.status = TASK_STATUS_QUEUED
            session.add(task)

    requeued = 0
    for task in stale:
        if task.status != TASK_STATUS_QUEUED:
            continue
        job_id = await enqueue_task(
            str(task.id), task.query, str(task.user_id), task.priority
        )
        if job_id:
            requeued += 1
            continue
        # 入队失败绝不能把任务留在 queued：那样它既不会被执行，又不是终态因而删不掉
        # （删除接口要求先取消），会永久卡死。直接落 failed 并写明原因。
        await _mark_finished(
            str(task.id),
            TASK_STATUS_FAILED,
            error="worker 重启后重新入队失败，请检查 Redis 是否可用",
        )

    if stale:
        print(f"[Worker] 失败恢复：处理 {len(stale)} 个残留任务，重新入队 {requeued} 个")

    return {"stale": len(stale), "requeued": requeued}


async def replay_events_into_channel(task_id: str) -> None:
    """
    兼容工具：把一个任务的历史事件重新广播一次。

    正常流程不需要它；调试或人工修复（如 Redis 数据被清空）时，
    可手动调用把 PostgreSQL 中的历史事件补回 pub/sub 通道。
    """
    async with session_scope() as session:
        events = await fetch_events(session, task_id, limit=10000)

    redis = get_redis()
    for payload in events:
        # 必须用与实时事件相同的 JSON 序列化，否则前端解析器要处理两种格式
        await redis.publish(event_channel(task_id), json_dumps(payload))


async def on_startup(ctx: dict[str, Any]) -> None:
    """worker 启动钩子：初始化数据库目录、记忆后端，并做一次失败恢复。"""
    config.ensure_runtime_dirs()
    # 必须尽早登记：LangChain 在线程池里执行同步工具，那些线程触发的落库/审计
    # 协程要投回这个主循环，否则会另起循环抢连接池（详见 app/utils/async_bridge.py）
    set_main_loop()
    await setup_memory()
    await recover_stale_tasks(ctx)
    print("[Worker] 启动完成，等待任务")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """worker 关闭钩子：顺序释放连接池，避免连接泄漏。"""
    await close_memory()
    await close_redis()
    await dispose_engine()
    print("[Worker] 已关闭")


class WorkerSettings:
    """
    arq worker 配置

    - max_jobs：同时执行的任务数，限制 LLM/DB 并发压力；
    - job_timeout：单任务硬超时，超时后 arq 取消协程并由 run_task 写入终态；
    - max_tries：队列层重试上限，与 tasks.max_attempts 保持一致；
    - retry_jobs + 指数退避：失败后不立刻重试，给下游服务恢复时间。
    """

    functions = [run_task]
    cron_jobs = [
        # 每天凌晨 3 点 17 分清理过期文件（避开整点，减少与其它定时任务撞车）
        cron(cleanup_files_job, hour=3, minute=17, run_at_startup=False),
    ]

    redis_settings = get_redis_settings()

    on_startup = on_startup
    on_shutdown = on_shutdown

    max_jobs = config.TASK_MAX_JOBS
    job_timeout = config.TASK_JOB_TIMEOUT_SECONDS
    max_tries = config.TASK_MAX_ATTEMPTS
    retry_jobs = True

    # 结果保留时间：够前端重新查询即可，不必长期占用 Redis 内存
    keep_result = 3600
    # 队列取任务的间隔（有任务时 arq 会立即轮询，无需频繁 polling）
    poll_delay = 0.5
    health_check_interval = 60


# 供 `python -m app.queue.worker` 直接启动
if __name__ == "__main__":
    from arq.worker import run_worker

    run_worker(WorkerSettings)
