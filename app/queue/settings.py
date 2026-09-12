"""arq 队列的 Redis 连接配置。"""

from arq.connections import RedisSettings

from app import config


def get_redis_settings() -> RedisSettings:
    """
    从 config 的 REDIS_URL 构造 arq 连接参数。

    统一走 from_dsn，保证队列与事件总线用的是同一份 Redis 配置，
    避免出现「任务入队成功但事件发到另一个库」的问题。
    """
    return RedisSettings.from_dsn(config.REDIS_URL)


# arq CLI 也支持 `arq app.queue.worker.WorkerSettings`，连接参数由 worker 自己声明
redis_settings = get_redis_settings()
