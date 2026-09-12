"""
任务入队与取消

API 进程通过这里把任务交给 arq，不直接执行 Agent —— 这样 HTTP 请求可以立刻返回，
长耗时任务由独立的 worker 进程排队执行，支持并发上限、重试、超时与失败恢复。
"""

from typing import Optional

from arq import create_pool
from arq.connections import ArqRedis
from arq.jobs import Job

from app import config
from app.queue.settings import get_redis_settings
from app.services.event_store import cancel_key, get_redis

# 任务函数名，必须与 worker.py 中注册的函数名一致
TASK_FUNCTION_NAME = "run_task"

# 作业所属队列名；取消接口要按同名去查找作业，所以抽成常量
QUEUE_NAME = "arq:queue"

_pool: Optional[ArqRedis] = None


def arq_job_id(task_id: str) -> str:
    """
    arq 作业 ID。

    固定为 task:{task_id}（而不是随机生成）是为了让取消接口能凭 task_id 直接定位
    到作业；代价是同一任务重复入队时会撞 ID，因此 enqueue_task 必须先清掉旧结果键。
    """
    return f"task:{task_id}"


def _stale_keys(task_id: str) -> tuple[str, str]:
    """
    arq 为作业维护的两个 key（格式取自 arq 内部约定，勿随意改动）：

    - `arq:job:{id}`   作业定义，入队时写入；
    - `arq:result:{id}` 作业返回值，keep_result 到期前一直保留。

    实测结论：`enqueue_job` 只要发现其中**任意一个**存在就会返回 None 拒绝入队，
    因此重新投递前必须两个都清掉（只清 result 不够，worker 崩溃后 job 键会残留）。
    """
    job_id = arq_job_id(task_id)
    return f"arq:job:{job_id}", f"arq:result:{job_id}"


async def get_arq_pool() -> ArqRedis:
    """获取（惰性创建）arq 连接池；API 进程内复用同一个池。"""
    global _pool
    if _pool is None:
        _pool = await create_pool(get_redis_settings())
    return _pool


async def close_arq_pool() -> None:
    """关闭 arq 连接池；进程退出时调用。"""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue_task(
    task_id: str,
    query: str,
    user_id: str,
    priority: int = 0,
) -> Optional[str]:
    """
    把任务投递到队列。

    重试与超时策略在任务维度由 tasks 表记录（max_attempts），在队列维度由
    WorkerSettings 统一控制，两边保持一致。

    :return: arq job id；入队失败返回 None（调用方负责把任务置为 failed）
    """
    pool = await get_arq_pool()
    redis = get_redis()

    # 入队前先清掉可能残留的取消标志，避免上一轮取消影响本次重试
    await redis.delete(cancel_key(task_id))

    # arq 按固定的 _job_id 去重：只要 arq:job / arq:result 里还留着这个 id，
    # enqueue_job 就直接返回 None 拒绝入队（见 _stale_keys 的实测结论）。
    # 三个正常场景都会撞上，且都是静默失败、任务永远停在 queued：
    #   1. 续聊：复用同一个 task_id 再次提交，上一轮的结果键还没过 keep_result；
    #   2. 失败恢复：worker 崩溃后重投残留任务，job 键还在；
    #   3. 取消后重跑：残留的作业定义。
    # 调用方只在「这个任务正要开始新一轮执行」时进来（运行中的任务会被 409 拦掉），
    # 本项目也从不读 arq 的作业返回值（状态一律以 PostgreSQL 为准），因此清理是安全的。
    await redis.delete(*_stale_keys(task_id))

    job = await pool.enqueue_job(
        TASK_FUNCTION_NAME,
        task_id,
        query,
        user_id,
        _job_id=arq_job_id(task_id),
        _queue_name=QUEUE_NAME,
        _defer_by=None,
    )
    return job.job_id if job else None


async def request_cancel(task_id: str) -> str:
    """
    请求取消一个任务。

    分两步：
    1. 写 Redis 取消标志 —— worker 的看门狗轮询到后中断 Agent 执行；
    2. 通过 arq 中止队列中的作业 —— 覆盖「任务还在排队、尚未开始执行」的情况。

    arq 的 abort 是「发消息给 worker 并等它确认」，因此 worker 不在线时必然超时
    （实测抛 TimeoutError）。这种情况下不能把任务留在 queued：那样用户既取消不掉、
    也因为不是终态而删不掉，会永久卡死。所以超时同样返回 cancelled，由调用方落终态 ——
    取消标志已经写进 Redis，作业稍后被拾起时 worker 会先看到它并跳过。

    :return: 操作结果描述，供接口回给前端
    """
    redis = get_redis()
    await redis.set(cancel_key(task_id), "1", ex=config.TASK_JOB_TIMEOUT_SECONDS * 2)

    pool = await get_arq_pool()
    job = Job(arq_job_id(task_id), pool, _queue_name=QUEUE_NAME)
    try:
        await job.abort(timeout=2)
        return "cancelled"
    except Exception as exc:
        # 作业已结束、已被消费，或压根没有 worker 在线，都会走到这里。
        # 取消标志始终有效，因此一律按「已取消」处理。
        print(f"[Queue] arq abort 未确认，按已取消处理：{type(exc).__name__}: {exc}")
        return "cancelled"


async def is_cancel_requested(task_id: str) -> bool:
    """查询任务是否已被请求取消。"""
    return bool(await get_redis().exists(cancel_key(task_id)))
