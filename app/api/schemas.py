"""
接口层请求/响应模型

集中定义 Pydantic 模型，让 OpenAPI 文档能自动生成，前端也能据此对齐字段。
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
class TaskRequest(BaseModel):
    """前端启动任务时提交的请求体。"""

    query: str = Field(..., min_length=1, description="用户的研搜任务")
    # thread_id 由服务端生成并返回；只有在「继续同一条会话」时才由前端回传
    thread_id: Optional[str] = Field(
        None, description="续聊时传入已有任务 ID；不传则新建任务"
    )
    priority: int = Field(0, ge=0, le=9, description="优先级，数值越大越先执行")
    # 首轮附件：新会话还没有 thread_id，文件只能先以「暂存」状态上传
    # （files.task_id 为空），再在建任务时用这里的 id 列表绑定。
    # 服务端只绑定属于当前用户、且仍未被绑定的那些文件。
    file_ids: list[str] = Field(
        default_factory=list,
        description="要绑定到本任务的暂存附件 ID 列表（可空）",
    )


class TaskCreated(BaseModel):
    """任务创建结果。"""

    status: str
    thread_id: str
    task_id: str


class TaskDetail(BaseModel):
    """任务详情，用于历史列表与详情面板。"""

    id: str
    title: Optional[str]
    query: str
    status: str
    attempts: int
    max_attempts: int
    result: Optional[str]
    last_error: Optional[str]
    owner: Optional[str] = None
    created_at: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]


class TaskListResponse(BaseModel):
    """分页任务列表。"""

    total: int
    items: list[TaskDetail]


class CancelTaskResponse(BaseModel):
    """取消任务结果。"""

    status: str
    thread_id: str
    message: Optional[str] = None


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #
class EventItem(BaseModel):
    """一条 WebSocket 历史事件。"""

    id: int
    type: str = "monitor_event"
    event: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = ""


class EventListResponse(BaseModel):
    """事件回放结果；next_after 供前端作为下次增量拉取的游标。"""

    items: list[EventItem]
    next_after: int


# --------------------------------------------------------------------------- #
# 文件
# --------------------------------------------------------------------------- #
class FileDetail(BaseModel):
    """文件元数据；下载/预览地址按需动态生成，不在这里返回。"""

    id: str
    task_id: Optional[str]
    name: str
    kind: str
    size: int
    mime_type: Optional[str]
    sha256: Optional[str]
    expires_at: Optional[str]
    created_at: Optional[str]


class FileListResponse(BaseModel):
    """文件列表。"""

    files: list[FileDetail]


class UploadResponse(BaseModel):
    """上传结果。"""

    status: str
    files: list[FileDetail]


class DownloadResponse(BaseModel):
    """下载/预览地址。"""

    url: str
    expires_in: int
    name: str


class PreviewResponse(BaseModel):
    """预览结果：小文本直接返回内容，其余返回预签名地址。"""

    mode: str  # inline | url
    content: Optional[str] = None
    url: Optional[str] = None
    name: str
    mime_type: Optional[str] = None


class FileContentResponse(BaseModel):
    """可编辑文件的正文。"""

    id: str
    name: str
    content: str


class FileContentUpdate(BaseModel):
    """保存编辑后的正文。"""

    content: str


# --------------------------------------------------------------------------- #
# 审计 / 管理
# --------------------------------------------------------------------------- #
class AuditItem(BaseModel):
    """一条审计记录。"""

    id: int
    user_id: Optional[str]
    action: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    detail: dict[str, Any] = Field(default_factory=dict)
    ip: Optional[str]
    created_at: Optional[str]


class AuditListResponse(BaseModel):
    """审计日志分页结果。"""

    total: int
    items: list[AuditItem]


class UserItem(BaseModel):
    """管理员视角的用户信息。"""

    id: str
    username: str
    display_name: Optional[str]
    role: str
    is_active: bool
    created_at: Optional[str]


class UserUpdateRequest(BaseModel):
    """管理员修改用户角色/启用状态。"""

    role: Optional[str] = Field(None, description="member / admin")
    is_active: Optional[bool] = None


class AllowlistItem(BaseModel):
    """SQL 白名单条目。"""

    table_name: str
    note: Optional[str] = None


class AllowlistAddRequest(BaseModel):
    """新增白名单表。"""

    table_name: str = Field(..., min_length=1, max_length=128)
    note: Optional[str] = Field(None, max_length=255)


class MemoryItem(BaseModel):
    """一条长期记忆。"""

    key: str
    text: str
    kind: str
    updated_at: Optional[str] = None
