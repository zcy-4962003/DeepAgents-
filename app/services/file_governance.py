"""
文件治理服务

职责：
1. **校验**：大小上限、扩展名白名单、MIME 嗅探（python-magic）双重把关；
2. **落存储**：文件实体写入对象存储 OSS/COS，key 形如 `{user_id}/{task_id}/{uuid}_{name}`，
   天然按用户隔离，且不会出现同名覆盖；
3. **元数据**：大小、MIME、sha256、有效期落 PostgreSQL files 表；
4. **下载鉴权**：接口层校验归属后只返回预签名 URL，带宽由对象存储承担；
5. **过期清理**：worker 定时任务按 expires_at 同时删对象与元数据行。

本地 `output/session_*` 目录只是 Agent 运行期的临时工作区，不作为持久存储。
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db.models import FILE_KIND_GENERATED, FILE_KIND_UPLOADED, FileRecord, Task
from app.storage import StorageError, get_storage

# 上传流的分片大小：1MB 一片，兼顾内存占用与写入效率
_CHUNK_SIZE = 1024 * 1024

# libmagic 的「未判定」结论：它认得出这是二进制/压缩包，但说不出具体是什么。
# 命中这些值时不能直接拿去校验（否则合法 Office 文件被拒），要走 sniff_ooxml 兜底。
_UNDETERMINED_MIMES = {"application/octet-stream", "application/zip"}

# OOXML 家族在 [Content_Types].xml 里的特征串 -> 对应 MIME
_OOXML_CONTENT_TYPE_MARKERS = (
    (
        "wordprocessingml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    (
        "spreadsheetml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    (
        "presentationml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_filename(name: str) -> str:
    """
    清洗客户端提交的文件名。

    浏览器的 filename 可能带路径（旧版 IE 风格 `C:\\a\\b.txt`），也可能含 `..`，
    这里只保留最后一段并剔除危险字符，避免污染对象 key。
    """
    base = Path(name.replace("\\", "/")).name.strip()
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    cleaned = cleaned.strip(". ")
    return cleaned or f"file_{uuid.uuid4().hex[:8]}"


def validate_extension(filename: str) -> str:
    """
    校验扩展名是否在白名单内。

    :return: 小写扩展名（含点）
    :raises HTTPException: 400 类型不允许
    """
    ext = Path(filename).suffix.lower()
    if not ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件 {filename} 缺少扩展名，无法判断类型",
        )
    if ext not in config.FILE_ALLOWED_EXTS:
        allowed = "、".join(config.FILE_ALLOWED_EXTS)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型 {ext}，仅允许：{allowed}",
        )
    return ext


def sniff_ooxml(data: bytes) -> Optional[str]:
    """
    从 zip 容器里的 `[Content_Types].xml` 判定 OOXML 的具体类型。

    这是 libmagic 判定失败时的兜底手段（原因见 sniff_mime）。非 zip、或 zip 里
    没有这个条目（普通压缩包），一律返回 None。
    """
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "[Content_Types].xml" not in archive.namelist():
                return None
            content_types = archive.read("[Content_Types].xml").decode(
                "utf-8", "replace"
            )
    except Exception:
        # 不是合法 zip / 结构损坏：交给上层按原判定处理
        return None

    for marker, mime in _OOXML_CONTENT_TYPE_MARKERS:
        if marker in content_types:
            return mime
    return None


def sniff_mime(local_path: Path) -> Optional[str]:
    """
    用文件头嗅探真实 MIME。

    部分攻击会伪造扩展名（如 `evil.png` 实为可执行文件），因此扩展名之外再做一次
    内容嗅探。嗅探失败（缺少 libmagic）时返回 None，不阻断上传。

    必须用 from_buffer 而不是 from_file，这一点在中文路径下是致命的：
    libmagic 的 magic_file() 只接受系统 ANSI 编码的路径，而本项目根目录含中文
    （D:\\Pytest\\多Agent电商系统\\...），传进去它打不开文件；更糟的是它**不抛异常**，
    而是把 "cannot open `...' (No such file or directory)" 当成结果返回，上层会把这句
    错误文本当作 MIME 类型，于是**所有上传都被判成类型不合法而拒绝**。
    改由 Python 读文件（Unicode 路径由 Python 正确处理），只把字节交给 libmagic。
    读取大小已被 _spool_upload 的 FILE_MAX_SIZE_BYTES 校验兜住。

    libmagic 对 zip 型 Office 文档的判定不可靠，必须用 sniff_ooxml 兜底：
    同一份合法的 .docx，Word 生成的能识别成 `...wordprocessingml.document`，
    而 python-docx 等库生成的只报 `application/octet-stream`（描述里其实是
    "Microsoft OOXML"），openpyxl 生成的 .xlsx 更差，只报 `application/zip`。
    这两者都不在白名单前缀里，会让**合法的 Office 文件被 400 拒绝**，而且提示
    「内容类型为 application/octet-stream」对用户毫无意义。
    """
    try:
        data = local_path.read_bytes()
    except Exception as exc:
        print(f"[FileGovernance] 读取文件失败，跳过内容校验：{exc}")
        return None

    mime: Optional[str] = None
    try:
        import magic

        mime = magic.from_buffer(data, mime=True)
    except Exception as exc:
        print(f"[FileGovernance] MIME 嗅探不可用，跳过内容校验：{exc}")

    # libmagic 给出明确结论时直接采用
    if mime and "/" in mime and mime not in _UNDETERMINED_MIMES:
        return mime

    # libmagic 没给出可用结论（未判定 or 不可用）：用 zip 内容自行判定 OOXML
    ooxml = sniff_ooxml(data)
    if ooxml:
        return ooxml

    # 兜底也判不出来：原样保留 libmagic 的结论（如 application/zip），
    # 让它继续被 validate_mime 按白名单拒绝——不能因为「判不出来」就放宽闸门，
    # 否则伪装成 .png 的普通压缩包会畅通无阻。
    # 只有真的拿不到 MIME（库缺失、抛异常、返回错误文本）才返回 None 放行。
    if not mime or "/" not in mime:
        print(f"[FileGovernance] MIME 嗅探返回异常结果，跳过内容校验：{mime!r}")
        return None
    return mime


def validate_mime(mime: Optional[str], filename: str) -> None:
    """MIME 前缀白名单校验；mime 为 None（嗅探不可用）时放行。"""
    if not mime:
        return
    if not any(
        mime.startswith(prefix) for prefix in config.FILE_ALLOWED_MIME_PREFIXES
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件 {filename} 的内容类型为 {mime}，不在允许范围内",
        )


def build_object_key(
    user_id: uuid.UUID, task_id: Optional[uuid.UUID], filename: str
) -> str:
    """生成对象存储 key：`{user_id}/{task_id}/{uuid}_{name}`，按用户物理隔离。"""
    scope = str(task_id) if task_id else "unbound"
    return f"{user_id}/{scope}/{uuid.uuid4().hex}_{sanitize_filename(filename)}"


def _expiry_for(kind: str) -> datetime:
    """按文件类别计算过期时间：上传附件比生成交付物保留得更短。"""
    days = (
        config.FILE_RETENTION_DAYS
        if kind == FILE_KIND_GENERATED
        else config.UPLOAD_RETENTION_DAYS
    )
    return _now() + timedelta(days=days)


async def _spool_upload(upload: UploadFile) -> tuple[Path, int, str]:
    """
    把上传流写入临时文件，同时统计大小和 sha256。

    为什么先落临时文件：对象存储 SDK 需要可寻址的文件对象来做分片上传，
    且边读边算哈希可以避免把大文件整个读进内存。超过大小上限时立即中断并删除临时文件。
    """
    config.ensure_runtime_dirs()
    temp_path = config.UPDATED_DIR / f"spool_{uuid.uuid4().hex}.tmp"

    digest = hashlib.sha256()
    total = 0
    try:
        with temp_path.open("wb") as buffer:
            while True:
                chunk = await upload.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.FILE_MAX_SIZE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件超过大小上限 {config.FILE_MAX_SIZE_MB}MB",
                    )
                digest.update(chunk)
                buffer.write(chunk)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    return temp_path, total, digest.hexdigest()


async def persist_upload(
    session: AsyncSession,
    user_id: uuid.UUID,
    task_id: Optional[uuid.UUID],
    upload: UploadFile,
) -> FileRecord:
    """
    处理单个上传文件：校验 -> 传对象存储 -> 写元数据。

    :raises HTTPException: 类型/大小不合规，或对象存储不可用
    """
    filename = sanitize_filename(upload.filename or "")
    validate_extension(filename)

    temp_path, size, sha256 = await _spool_upload(upload)
    try:
        mime = sniff_mime(temp_path)
        validate_mime(mime, filename)

        object_key = build_object_key(user_id, task_id, filename)
        try:
            await get_storage().aput_file(object_key, temp_path, mime)
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"文件写入对象存储失败：{exc}",
            ) from exc
    finally:
        temp_path.unlink(missing_ok=True)

    record = FileRecord(
        user_id=user_id,
        task_id=task_id,
        object_key=object_key,
        name=filename,
        mime_type=mime,
        size=size,
        sha256=sha256,
        kind=FILE_KIND_UPLOADED,
        expires_at=_expiry_for(FILE_KIND_UPLOADED),
    )
    session.add(record)
    return record


async def collect_generated_files(
    session: AsyncSession,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    session_dir: Path,
    uploaded_names: set[str],
) -> list[FileRecord]:
    """
    任务结束后把工作目录里新生成的文件收进对象存储。

    :param uploaded_names: 本任务上传附件的文件名集合，这些文件是从对象存储下载下来的，
                           不需要重复上传
    :return: 新建的 files 记录
    """
    if not session_dir.exists():
        return []

    storage = get_storage()
    records: list[FileRecord] = []

    for local_path in sorted(session_dir.rglob("*")):
        if not local_path.is_file():
            continue
        # 跳过上传附件与对象存储 SDK 的分片临时文件
        if local_path.name in uploaded_names or local_path.suffix == ".tmp":
            continue

        relative_name = str(local_path.relative_to(session_dir)).replace("\\", "/")
        mime = sniff_mime(local_path)
        object_key = build_object_key(user_id, task_id, local_path.name)

        try:
            await storage.aput_file(object_key, local_path, mime)
        except StorageError as exc:
            print(f"[FileGovernance] 生成文件上传失败 {relative_name}: {exc}")
            continue

        record = FileRecord(
            user_id=user_id,
            task_id=task_id,
            object_key=object_key,
            name=relative_name,
            mime_type=mime,
            size=local_path.stat().st_size,
            kind=FILE_KIND_GENERATED,
            expires_at=_expiry_for(FILE_KIND_GENERATED),
        )
        session.add(record)
        records.append(record)

    return records


async def download_task_uploads(
    session: AsyncSession,
    task_id: uuid.UUID,
    target_dir: Path,
) -> list[str]:
    """
    worker 执行任务前，把该任务的上传附件从对象存储拉回本地工作目录。

    :return: 落地的文件名列表，用于提示词中告知模型有哪些附件可读
    """
    rows = await session.scalars(
        select(FileRecord)
        .where(FileRecord.task_id == task_id)
        .where(FileRecord.kind == FILE_KIND_UPLOADED)
        .order_by(FileRecord.created_at)
    )
    records = list(rows)
    if not records:
        return []

    target_dir.mkdir(parents=True, exist_ok=True)
    storage = get_storage()
    landed: list[str] = []

    for record in records:
        local_path = target_dir / Path(record.name).name
        try:
            await storage.adownload_file(record.object_key, local_path)
            landed.append(local_path.name)
        except StorageError as exc:
            print(f"[FileGovernance] 附件下载失败 {record.name}: {exc}")

    return landed


async def bind_staged_files(
    session: AsyncSession,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    file_ids: list[str],
) -> int:
    """
    把「暂存附件」绑定到刚创建的任务上。

    新会话在第一条消息发出前还没有 thread_id，用户选的附件只能先无归属地上传
    （见 api/routes/files.py 的 upload_files 说明）。这里在任务创建的同一个事务里
    把它们认领过来，保证 worker 拿到任务时附件已经就位——不会有「任务已入队、
    附件还没绑上」的竞态。

    只更新同时满足三个条件的行，越权与误绑都挡在 SQL 里：
    - id 在 file_ids 中；
    - user_id 是当前用户（否则可以抢绑别人的文件）；
    - task_id 仍为空（已绑定的不动，否则能把别的会话的附件抢过来）。

    :return: 实际绑定的文件数
    """
    if not file_ids:
        return 0

    parsed: list[uuid.UUID] = []
    for raw in file_ids:
        try:
            parsed.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            # 非法 id 直接忽略：附件绑定失败不该导致任务建不出来
            continue
    if not parsed:
        return 0

    result = await session.execute(
        update(FileRecord)
        .where(FileRecord.id.in_(parsed))
        .where(FileRecord.user_id == user_id)
        .where(FileRecord.task_id.is_(None))
        .values(task_id=task_id)
    )
    return result.rowcount or 0


async def get_file_checked(
    session: AsyncSession,
    file_id: str,
    user_id: uuid.UUID,
    is_admin: bool,
) -> FileRecord:
    """
    按 id 取文件并校验归属，供下载/预览/编辑接口复用。

    :raises HTTPException: 404 文件不存在；403 无权访问
    """
    try:
        parsed_id = uuid.UUID(str(file_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="文件不存在")

    record = await session.get(FileRecord, parsed_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not is_admin and record.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该文件"
        )
    return record


async def cleanup_expired_files(session: AsyncSession) -> int:
    """
    删除已过期的文件：先删对象存储，再删元数据行。

    对象删除失败时保留元数据行，留给下一轮重试，避免出现「对象还在但记录没了」的
    孤儿文件。
    :return: 实际清理的文件数量
    """
    rows = await session.scalars(
        select(FileRecord).where(FileRecord.expires_at < _now())
    )
    expired = list(rows)
    if not expired:
        return 0

    storage = get_storage()
    removed_ids: list[uuid.UUID] = []

    for record in expired:
        try:
            await storage.adelete(record.object_key)
        except StorageError as exc:
            print(f"[FileGovernance] 删除对象失败，保留记录待重试 {record.name}: {exc}")
            continue
        removed_ids.append(record.id)

    if removed_ids:
        await session.execute(
            delete(FileRecord).where(FileRecord.id.in_(removed_ids))
        )

    return len(removed_ids)


async def task_uploaded_names(
    session: AsyncSession, task_id: uuid.UUID
) -> set[str]:
    """取某任务已上传附件的本地文件名集合，供生成文件收集时排除。"""
    rows = await session.scalars(
        select(FileRecord.name)
        .where(FileRecord.task_id == task_id)
        .where(FileRecord.kind == FILE_KIND_UPLOADED)
    )
    return {Path(name).name for name in rows}


async def list_task_files(
    session: AsyncSession,
    task_id: uuid.UUID,
    kind: Optional[str] = None,
) -> list[FileRecord]:
    """列出某个任务的全部文件（上传 + 生成），可按类别过滤。"""
    statement = (
        select(FileRecord)
        .where(FileRecord.task_id == task_id)
        .order_by(FileRecord.created_at.desc())
    )
    if kind:
        statement = statement.where(FileRecord.kind == kind)
    return list(await session.scalars(statement))


async def task_title_from_query(query: str) -> str:
    """用问题首行截断出任务标题，便于前端历史列表展示。"""
    first_line = query.strip().splitlines()[0] if query.strip() else "未命名任务"
    return first_line[:60]


__all__ = [
    "Task",
    "bind_staged_files",
    "build_object_key",
    "cleanup_expired_files",
    "collect_generated_files",
    "download_task_uploads",
    "get_file_checked",
    "list_task_files",
    "persist_upload",
    "sanitize_filename",
    "sniff_mime",
    "sniff_ooxml",
    "task_title_from_query",
    "task_uploaded_names",
    "validate_extension",
    "validate_mime",
]
