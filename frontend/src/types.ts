// 与后端 app/api/schemas.py、app/auth/routes.py 中的 Pydantic 模型一一对应。
// 改后端字段时务必同步这里，否则类型对不上但运行期才炸。

export type ConnectionState = "connecting" | "connected" | "reconnecting" | "closed";

// --------------------------------------------------------------------------- //
// 账号
// --------------------------------------------------------------------------- //
export interface UserInfo {
  id: string;
  username: string;
  display_name: string | null;
  role: "member" | "admin" | string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: UserInfo;
}

/** 登录与注册共用的请求体（注册时 display_name 可选） */
export interface LoginPayloadLike {
  username: string;
  password: string;
  display_name?: string;
}

// --------------------------------------------------------------------------- //
// 任务
// --------------------------------------------------------------------------- //
export type TaskStatus =
  | "queued"
  | "running"
  | "success"
  | "failed"
  | "cancelled"
  | string;

export interface TaskDetail {
  id: string;
  title: string | null;
  query: string;
  status: TaskStatus;
  attempts: number;
  max_attempts: number;
  result: string | null;
  last_error: string | null;
  owner: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskListResponse {
  total: number;
  items: TaskDetail[];
}

export interface TaskCreated {
  status: string;
  thread_id: string;
  task_id: string;
}

export interface CancelTaskResponse {
  status: string;
  thread_id: string;
  message?: string;
}

/** 结果对比：后端直接返回选中的任务详情，不含分页总数 */
export interface CompareResponse {
  items: TaskDetail[];
}

// --------------------------------------------------------------------------- //
// 事件
// --------------------------------------------------------------------------- //
export type MonitorEventName =
  | "session_created"
  | "tool_start"
  | "assistant_call"
  | "task_result"
  | "task_status"
  | "task_cancelled"
  | "files_archived"
  | "error"
  | string;

export interface MonitorMessage {
  type: "monitor_event";
  event: MonitorEventName;
  message: string;
  data: Record<string, unknown>;
  timestamp: string;
  /** 事件在数据库中的自增 id，前端用它做去重与断线续播游标 */
  id?: number;
}

export interface PongMessage {
  type: "pong";
  message?: string;
}

export type SocketMessage = MonitorMessage | PongMessage;

export interface EventListResponse {
  items: MonitorMessage[];
  next_after: number;
}

// --------------------------------------------------------------------------- //
// 文件
// --------------------------------------------------------------------------- //
export interface FileDetail {
  id: string;
  task_id: string | null;
  name: string;
  kind: "uploaded" | "generated" | string;
  size: number;
  mime_type: string | null;
  sha256: string | null;
  expires_at: string | null;
  created_at: string | null;
}

export interface FileListResponse {
  files: FileDetail[];
}

export interface UploadResponse {
  status: string;
  files: FileDetail[];
}

export interface PreviewResponse {
  /** inline=正文直接返回；url=返回预签名地址，由浏览器自行获取 */
  mode: "inline" | "url";
  content: string | null;
  url: string | null;
  name: string;
  mime_type: string | null;
}

export interface DownloadResponse {
  url: string;
  expires_in: number;
  name: string;
}

export interface FileContentResponse {
  id: string;
  name: string;
  content: string;
}

export interface DeleteFileResponse {
  status: string;
  file_id: string;
}

// --------------------------------------------------------------------------- //
// 长期记忆
// --------------------------------------------------------------------------- //
export interface MemoryItem {
  key: string;
  text: string;
  kind: string;
  updated_at?: string | null;
}

// --------------------------------------------------------------------------- //
// 前端本地结构
// --------------------------------------------------------------------------- //
export interface UploadedItem {
  uid: string;
  name: string;
  size: number;
  raw: File;
}
