"""
文件接口

上传走「校验 -> 对象存储 -> 元数据落库」，下载只返回预签名地址（带宽由对象存储承担），
预览则按类型分流：可读文本直接返回正文，其余返回预签名地址。
所有接口都会先校验文件归属，员工之间互相看不到对方的文件。
"""

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.api.schemas import (
    DownloadResponse,
    FileContentResponse,
    FileContentUpdate,
    FileDetail,
    FileListResponse,
    PreviewResponse,
    UploadResponse,
)
from app.auth.deps import CurrentUser, client_ip, get_current_user
from app.db.models import FILE_KIND_GENERATED, FileRecord
from app.db.session import get_session
from app.services.audit import AuditAction, record_audit
from app.services.file_governance import (
    get_file_checked,
    persist_upload,
)
from app.services.task_ownership import get_task_checked
from app.storage import StorageError, get_storage

router = APIRouter(prefix="/api", tags=["files"])

# 预览时直接内联返回正文的类型；超出这个大小就不再把内容读进内存
_INLINE_TEXT_EXTS = {".md", ".txt", ".csv", ".json", ".log"}
_INLINE_TEXT_MAX_BYTES = 512 * 1024
_INLINE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# 允许在线编辑回写的类型（报告编辑只针对 Markdown / 纯文本）
_EDITABLE_EXTS = {".md", ".txt"}


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _to_detail(record: FileRecord) -> FileDetail:
    return FileDetail(
        id=str(record.id),
        task_id=str(record.task_id) if record.task_id else None,
        name=record.name,
        kind=record.kind,
        size=record.size,
        mime_type=record.mime_type,
        sha256=record.sha256,
        expires_at=_iso(record.expires_at),
        created_at=_iso(record.created_at),
    )


@router.post("/upload", response_model=UploadResponse, summary="上传文件")
async def upload_files(
    request: Request,
    files: List[UploadFile] = File(...),
    thread_id: Optional[str] = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    上传一个或多个文件。

    带 thread_id 时表示「给某个任务准备附件」，会先校验该任务归属；
    不带时是「先传到工作台暂存」，等创建任务时再绑定。
    task_id 为空的附件由过期清理任务按 UPLOAD_RETENTION_DAYS 回收。
    """
    task_uuid: Optional[uuid.UUID] = None
    if thread_id:
        task = await get_task_checked(session, thread_id, current_user)
        task_uuid = task.id

    saved: list[FileDetail] = []
    for upload in files:
        record = await persist_upload(
            session, current_user.id, task_uuid, upload
        )
        await session.flush()
        saved.append(_to_detail(record))

    await session.commit()

    await record_audit(
        action=AuditAction.FILE_UPLOAD,
        user_id=current_user.id,
        resource_type="file",
        resource_id=task_uuid,
        detail={
            "files": [
                {"name": item.name, "size": item.size} for item in saved
            ],
            "task_id": str(task_uuid) if task_uuid else None,
        },
        ip=client_ip(request),
    )

    return UploadResponse(status="uploaded", files=saved)


@router.get("/files", response_model=FileListResponse, summary="文件列表")
async def list_files(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    thread_id: Optional[str] = Query(None, description="按任务过滤"),
    kind: Optional[str] = Query(
        None, description="uploaded(上传) / generated(生成)"
    ),
    limit: int = Query(200, ge=1, le=1000),
):
    """列出当前用户可见的文件；admin 可看全员，也可用 thread_id 收窄到单个任务。"""
    statement = select(FileRecord).order_by(FileRecord.created_at.desc()).limit(limit)

    if thread_id:
        task = await get_task_checked(session, thread_id, current_user)
        statement = statement.where(FileRecord.task_id == task.id)
    elif not current_user.is_admin:
        statement = statement.where(FileRecord.user_id == current_user.id)

    if kind:
        statement = statement.where(FileRecord.kind == kind)

    rows = await session.scalars(statement)
    return FileListResponse(files=[_to_detail(record) for record in rows])


@router.get(
    "/file/{file_id}/download",
    response_model=DownloadResponse,
    summary="获取下载地址",
)
async def download_file(
    file_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    返回短时效的预签名下载地址。

    接口本身不传输文件字节：带宽由对象存储承担，地址过期后自动失效，
    从而实现了「下载鉴权 + 防盗链」。
    """
    record = await get_file_checked(
        session, file_id, current_user.id, current_user.is_admin
    )

    try:
        url = await get_storage().apresigned_url(
            record.object_key, download_name=record.name.split("/")[-1]
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=f"生成下载地址失败：{exc}") from exc

    await record_audit(
        action=AuditAction.FILE_DOWNLOAD,
        user_id=current_user.id,
        resource_type="file",
        resource_id=file_id,
        detail={"name": record.name, "size": record.size},
        ip=client_ip(request),
    )

    return DownloadResponse(
        url=url,
        expires_in=config.STORAGE_PRESIGN_EXPIRE_SECONDS,
        name=record.name,
    )


@router.get(
    "/file/{file_id}/preview",
    response_model=PreviewResponse,
    summary="预览文件",
)
async def preview_file(
    file_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    预览文件内容。

    小体积文本类（md/txt/csv…）直接返回正文，前端无需再发一次请求；
    图片和二进制文件返回预签名地址，由浏览器原生渲染。
    """
    record = await get_file_checked(
        session, file_id, current_user.id, current_user.is_admin
    )

    suffix = ("." + record.name.rsplit(".", 1)[-1].lower()) if "." in record.name else ""

    await record_audit(
        action=AuditAction.FILE_PREVIEW,
        user_id=current_user.id,
        resource_type="file",
        resource_id=file_id,
        detail={"name": record.name, "mode": "inline" if suffix in _INLINE_TEXT_EXTS else "url"},
        ip=client_ip(request),
    )

    if suffix in _INLINE_TEXT_EXTS and record.size <= _INLINE_TEXT_MAX_BYTES:
        try:
            raw = await get_storage().aget_bytes(record.object_key)
        except StorageError as exc:
            raise HTTPException(
                status_code=502, detail=f"读取文件失败：{exc}"
            ) from exc

        return PreviewResponse(
            mode="inline",
            content=raw.decode("utf-8", errors="replace"),
            name=record.name,
            mime_type=record.mime_type,
        )

    try:
        url = await get_storage().apresigned_url(record.object_key)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=f"生成预览地址失败：{exc}") from exc

    return PreviewResponse(
        mode="url", url=url, name=record.name, mime_type=record.mime_type
    )


@router.get(
    "/file/{file_id}/content",
    response_model=FileContentResponse,
    summary="读取可编辑正文",
)
async def get_file_content(
    file_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """读取 Markdown/纯文本正文，供前端进入编辑态。"""
    record = await get_file_checked(
        session, file_id, current_user.id, current_user.is_admin
    )
    _ensure_editable(record.name)

    try:
        raw = await get_storage().aget_bytes(record.object_key)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=f"读取文件失败：{exc}") from exc

    return FileContentResponse(
        id=str(record.id),
        name=record.name,
        content=raw.decode("utf-8", errors="replace"),
    )


@router.put(
    "/file/{file_id}/content",
    response_model=FileContentResponse,
    summary="保存编辑后的正文",
)
async def update_file_content(
    file_id: str,
    payload: FileContentUpdate,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    回写编辑后的正文。

    只有生成类文件允许编辑：上传附件是任务的原始输入，改掉会让审计失去意义。
    """
    record = await get_file_checked(
        session, file_id, current_user.id, current_user.is_admin
    )
    _ensure_editable(record.name)

    if record.kind != FILE_KIND_GENERATED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="上传的附件不允许在线编辑"
        )

    data = payload.content.encode("utf-8")
    if len(data) > config.FILE_MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"内容超过大小上限 {config.FILE_MAX_SIZE_MB}MB",
        )

    try:
        await get_storage().aput_bytes(
            record.object_key, data, record.mime_type or "text/markdown"
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=f"保存失败：{exc}") from exc

    # 大小变了要同步元数据，前端列表才不会显示旧体积
    record.size = len(data)
    record.expires_at = datetime.now().astimezone() + timedelta(
        days=config.FILE_RETENTION_DAYS
    )
    await session.commit()

    await record_audit(
        action=AuditAction.FILE_EDIT,
        user_id=current_user.id,
        resource_type="file",
        resource_id=file_id,
        detail={"name": record.name, "size": record.size},
        ip=client_ip(request),
    )

    return FileContentResponse(
        id=str(record.id), name=record.name, content=payload.content
    )


@router.delete("/file/{file_id}", summary="删除文件")
async def delete_file(
    file_id: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """删除文件对象与元数据；对象删除失败时保留记录，留给清理任务重试。"""
    record = await get_file_checked(
        session, file_id, current_user.id, current_user.is_admin
    )

    try:
        await get_storage().adelete(record.object_key)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=f"删除对象失败：{exc}") from exc

    name = record.name
    await session.delete(record)
    await session.commit()

    await record_audit(
        action=AuditAction.FILE_DELETE,
        user_id=current_user.id,
        resource_type="file",
        resource_id=file_id,
        detail={"name": name},
        ip=client_ip(request),
    )
    return {"status": "deleted", "file_id": file_id}


@router.get("/files/stats", summary="文件统计")
async def file_stats(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """按类别统计当前用户可见的文件数量与总大小，用于工作台概览。"""
    statement = select(
        FileRecord.kind, func.count(FileRecord.id), func.coalesce(func.sum(FileRecord.size), 0)
    ).group_by(FileRecord.kind)
    if not current_user.is_admin:
        statement = statement.where(FileRecord.user_id == current_user.id)

    rows = await session.execute(statement)
    stats = {
        kind: {"count": count, "bytes": int(total)}
        for kind, count, total in rows.all()
    }
    return {"stats": stats}


def _ensure_editable(filename: str) -> None:
    """校验文件类型是否支持在线编辑。"""
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if suffix not in _EDITABLE_EXTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"仅支持在线编辑 {'/'.join(sorted(_EDITABLE_EXTS))} 文件",
        )
