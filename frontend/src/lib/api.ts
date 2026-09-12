// 后端接口封装。所有请求统一带上 JWT，并集中处理 401。
//
// 相比旧版本的变化：文件接口从「按路径」改成了「按文件 ID」——
// 文件实体存在对象存储里，下载/预览都由后端签发短时效的预签名地址，
// 前端不再拼接本地路径。

import { API_BASE_URL } from "./config";
import { authHeaders, clearAuth } from "./auth";
import type {
  CancelTaskResponse,
  CompareResponse,
  DeleteFileResponse,
  EventListResponse,
  FileContentResponse,
  FileDetail,
  FileListResponse,
  DownloadResponse,
  LoginPayloadLike,
  MemoryItem,
  PreviewResponse,
  TaskCreated,
  TaskDetail,
  TaskListResponse,
  TokenResponse,
  UploadResponse
} from "../types";

/** 收到 401 时抛这个，UI 据此切回登录页而不是弹一个普通的错误提示 */
export class UnauthorizedError extends Error {
  constructor(message = "登录已过期，请重新登录") {
    super(message);
    this.name = "UnauthorizedError";
  }
}

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

async function requestJson<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders() };
  // FormData 的 Content-Type 必须由浏览器自己带（含 boundary），不能手写
  if (init?.body && !(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(input, {
    ...init,
    headers: { ...headers, ...((init?.headers as Record<string, string>) || {}) }
  });

  if (response.status === 401) {
    // 令牌失效：清掉本地登录态，让 UI 回到登录页
    clearAuth();
    throw new UnauthorizedError();
  }

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "detail" in payload
        ? typeof payload.detail === "string"
          ? payload.detail
          : JSON.stringify(payload.detail)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }

  return payload as T;
}

// --------------------------------------------------------------------------- //
// 账号
// --------------------------------------------------------------------------- //
export async function login(payload: LoginPayloadLike): Promise<TokenResponse> {
  return requestJson<TokenResponse>(apiUrl("/api/auth/login"), {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export async function register(payload: LoginPayloadLike): Promise<TokenResponse> {
  return requestJson<TokenResponse>(apiUrl("/api/auth/register"), {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export async function fetchMe(): Promise<TokenResponse["user"]> {
  return requestJson<TokenResponse["user"]>(apiUrl("/api/auth/me"));
}

export async function logout(): Promise<void> {
  // 服务端只记审计，失败也不该阻塞前端登出
  try {
    await requestJson(apiUrl("/api/auth/logout"), { method: "POST" });
  } finally {
    clearAuth();
  }
}

// --------------------------------------------------------------------------- //
// 任务
// --------------------------------------------------------------------------- //
export async function startTask(
  query: string,
  threadId: string | null,
  priority = 0,
  fileIds: string[] = []
): Promise<TaskCreated> {
  return requestJson<TaskCreated>(apiUrl("/api/task"), {
    method: "POST",
    body: JSON.stringify({
      query,
      thread_id: threadId || undefined,
      priority,
      // 首轮附件：新会话没有 thread_id，这些文件先以暂存状态上传，由后端在本任务
      // 建好后一并认领。不带 thread_id 的续聊也会走到这里，语义一致。
      file_ids: fileIds
    })
  });
}

export async function cancelTask(threadId: string): Promise<CancelTaskResponse> {
  return requestJson<CancelTaskResponse>(
    apiUrl(`/api/task/${encodeURIComponent(threadId)}/cancel`),
    { method: "POST" }
  );
}

export interface TaskListQuery {
  limit?: number;
  offset?: number;
  status?: string;
  keyword?: string;
}

export async function listTasks(query: TaskListQuery = {}): Promise<TaskListResponse> {
  const url = new URL(apiUrl("/api/tasks"));
  if (query.limit != null) url.searchParams.set("limit", String(query.limit));
  if (query.offset != null) url.searchParams.set("offset", String(query.offset));
  if (query.status) url.searchParams.set("status", query.status);
  if (query.keyword) url.searchParams.set("keyword", query.keyword);
  return requestJson<TaskListResponse>(url);
}

export async function getTask(threadId: string): Promise<TaskDetail> {
  return requestJson<TaskDetail>(apiUrl(`/api/task/${encodeURIComponent(threadId)}`));
}

export async function deleteTask(threadId: string): Promise<void> {
  await requestJson(apiUrl(`/api/task/${encodeURIComponent(threadId)}`), {
    method: "DELETE"
  });
}

/** 增量拉取任务历史事件；after 传已收到的最大事件 id。 */
export async function listEvents(
  threadId: string,
  after = 0,
  eventType?: string,
  limit = 500
): Promise<EventListResponse> {
  const url = new URL(apiUrl(`/api/task/${encodeURIComponent(threadId)}/events`));
  url.searchParams.set("after", String(after));
  url.searchParams.set("limit", String(limit));
  if (eventType) url.searchParams.set("type", eventType);
  return requestJson<EventListResponse>(url);
}

/**
 * 对比多条任务的结果，用于并排查看。
 * 后端要求 2~5 个 ID，且用逗号分隔的单个参数；任一条不属于当前用户会整体拒绝。
 */
export async function compareTasks(threadIds: string[]): Promise<CompareResponse> {
  const url = new URL(apiUrl("/api/tasks/compare"));
  url.searchParams.set("ids", threadIds.join(","));
  return requestJson<CompareResponse>(url);
}

// --------------------------------------------------------------------------- //
// 文件
// --------------------------------------------------------------------------- //
export async function uploadSessionFiles(
  files: File[],
  threadId: string | null
): Promise<UploadResponse> {
  const formData = new FormData();
  if (threadId) {
    formData.append("thread_id", threadId);
  }
  files.forEach((file) => formData.append("files", file));

  return requestJson<UploadResponse>(apiUrl("/api/upload"), {
    method: "POST",
    body: formData
  });
}

export interface FileListQuery {
  threadId?: string;
  kind?: string;
  limit?: number;
}

export async function listFiles(query: FileListQuery = {}): Promise<FileListResponse> {
  const url = new URL(apiUrl("/api/files"));
  if (query.threadId) url.searchParams.set("thread_id", query.threadId);
  if (query.kind) url.searchParams.set("kind", query.kind);
  if (query.limit != null) url.searchParams.set("limit", String(query.limit));
  return requestJson<FileListResponse>(url);
}

/** 取预签名下载地址（有效期由后端配置，默认 10 分钟）。 */
export async function getDownloadUrl(fileId: string): Promise<DownloadResponse> {
  return requestJson<DownloadResponse>(
    apiUrl(`/api/file/${encodeURIComponent(fileId)}/download`)
  );
}

export async function previewFile(fileId: string): Promise<PreviewResponse> {
  return requestJson<PreviewResponse>(
    apiUrl(`/api/file/${encodeURIComponent(fileId)}/preview`)
  );
}

export async function getFileContent(fileId: string): Promise<FileContentResponse> {
  return requestJson<FileContentResponse>(
    apiUrl(`/api/file/${encodeURIComponent(fileId)}/content`)
  );
}

export async function updateFileContent(
  fileId: string,
  content: string
): Promise<FileContentResponse> {
  return requestJson<FileContentResponse>(
    apiUrl(`/api/file/${encodeURIComponent(fileId)}/content`),
    { method: "PUT", body: JSON.stringify({ content }) }
  );
}

export async function deleteFile(fileId: string): Promise<DeleteFileResponse> {
  return requestJson<DeleteFileResponse>(
    apiUrl(`/api/file/${encodeURIComponent(fileId)}`),
    { method: "DELETE" }
  );
}

// --------------------------------------------------------------------------- //
// 长期记忆
// --------------------------------------------------------------------------- //
export async function listMemories(limit = 100): Promise<{ items: MemoryItem[] }> {
  const url = new URL(apiUrl("/api/memories"));
  url.searchParams.set("limit", String(limit));
  return requestJson<{ items: MemoryItem[] }>(url);
}

export async function deleteMemory(key: string): Promise<void> {
  await requestJson(apiUrl(`/api/memories/${encodeURIComponent(key)}`), {
    method: "DELETE"
  });
}

export type { FileDetail };
