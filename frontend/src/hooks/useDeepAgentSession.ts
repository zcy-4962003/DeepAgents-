import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  UnauthorizedError,
  cancelTask,
  getTask,
  listEvents,
  listFiles,
  startTask,
  uploadSessionFiles
} from "../lib/api";
import { clearAuth, getToken } from "../lib/auth";
import { WS_BASE_URL } from "../lib/config";
import { clearStoredThreadId, getStoredThreadId, storeThreadId } from "../lib/thread";
import type {
  ConnectionState,
  FileDetail,
  MonitorMessage,
  SocketMessage,
  TaskStatus,
  UploadedItem
} from "../types";

/** 事件列表保留上限，只影响内存里的轨迹面板，不影响服务端已落库的历史 */
const MAX_EVENTS = 300;

/** 后端自定义关闭码，见 app/api/server.py */
const WS_CLOSE_UNAUTHORIZED = 4401;
const WS_CLOSE_FORBIDDEN = 4403;
const WS_CLOSE_NOT_FOUND = 4404;

const TERMINAL_STATUSES = new Set(["success", "failed", "cancelled"]);

function extractString(data: Record<string, unknown>, key: string): string | null {
  const value = data[key];
  return typeof value === "string" ? value : null;
}

/** 按事件 id 去重合并，保持时序；没有 id 的（回放异常等）直接追加 */
function mergeEvents(
  previous: MonitorMessage[],
  incoming: MonitorMessage[]
): MonitorMessage[] {
  const seen = new Set(
    previous.map((item) => item.id).filter((id): id is number => typeof id === "number")
  );
  const next = [...previous];
  incoming.forEach((item) => {
    if (typeof item.id === "number") {
      if (seen.has(item.id)) {
        return;
      }
      seen.add(item.id);
    }
    next.push(item);
  });
  return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
}

function maxEventId(events: MonitorMessage[]): number {
  return events.reduce(
    (max, item) => (typeof item.id === "number" && item.id > max ? item.id : max),
    0
  );
}

export function useDeepAgentSession() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | undefined>(undefined);
  const heartbeatTimerRef = useRef<number | undefined>(undefined);

  // 重连时要用到的最新游标。存在 ref 里是因为它同时被 onclose（重连定时器）
  // 和事件处理函数读取，放进 state 会读到闭包里的旧值。
  const cursorRef = useRef(0);
  const threadIdRef = useRef<string | null>(getStoredThreadId());

  /**
   * 已上传但还没归属到任何任务的附件 ID（服务端返回 task_id 为 null）。
   *
   * 新会话在第一条消息发出前没有 thread_id，上传接口只能把这些文件以「暂存」
   * 状态落库；提交任务时要把它们一并带上，服务端才会认领给新任务。
   * 不这样做的话文件会永远停在未绑定状态，agent 读不到，而界面上却显示已附加。
   * 用 ref 是因为它要在 uploadFiles 和 submitTask 之间共享，且不必触发重渲染。
   */
  const stagedFileIdsRef = useRef<string[]>([]);

  /**
   * 连接代际：每次「打开会话」自增一次，用来强制下面的 WebSocket 副作用重跑。
   *
   * 不能只靠 threadId 触发重连：openSession 会先 closeSocket() 关掉旧连接、再
   * setThreadId()。打开的就是当前会话时 threadId 没变化，React 会跳过这次
   * setState，监听 threadId 的副作用不重跑，而被关掉的连接因为 closeSocket
   * 已把 socketRef 置空，onclose 直接 return 也不会安排重连——连接就此永久消失。
   * 页面刷新恢复上次会话正好走这条路径（threadId 的初值就是本地存的 id，
   * App 挂载后又用同一个 id 调 openSession），表现为刷新后既不显示轨迹也不显示
   * 回复，且之后每次提问都收不到实时事件。
   */
  const [connectionEpoch, setConnectionEpoch] = useState(0);

  const [threadId, setThreadId] = useState<string | null>(threadIdRef.current);
  const [connectionState, setConnectionState] = useState<ConnectionState>("closed");
  const [events, setEvents] = useState<MonitorMessage[]>([]);
  const [files, setFiles] = useState<FileDetail[]>([]);
  const [result, setResult] = useState("");
  const [lastError, setLastError] = useState("");
  const [lastPongAt, setLastPongAt] = useState("");
  const [taskStatus, setTaskStatus] = useState<TaskStatus | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadedItems, setUploadedItems] = useState<UploadedItem[]>([]);

  const clearSocketTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = undefined;
    }
    if (heartbeatTimerRef.current) {
      window.clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = undefined;
    }
  }, []);

  const closeSocket = useCallback(() => {
    clearSocketTimers();
    const socket = socketRef.current;
    socketRef.current = null;
    socket?.close();
  }, [clearSocketTimers]);

  const refreshFiles = useCallback(async (targetThreadId?: string | null) => {
    const id = targetThreadId ?? threadIdRef.current;
    if (!id) {
      setFiles([]);
      return;
    }
    const response = await listFiles({ threadId: id });
    setFiles(response.files || []);
  }, []);

  /** 清空本地会话状态，回到「还没提问」的空白页 */
  const resetSession = useCallback(() => {
    closeSocket();
    clearStoredThreadId();
    threadIdRef.current = null;
    cursorRef.current = 0;
    setThreadId(null);
    setConnectionState("closed");
    setEvents([]);
    setFiles([]);
    setResult("");
    setLastError("");
    setTaskStatus(null);
    setUploadedItems([]);
    // 与上面的 setUploadedItems 同步清空：附件列表都不显示了，
    // 再把这些 id 带去提交就会绑上用户看不见的文件
    stagedFileIdsRef.current = [];
    setIsRunning(false);
    setIsCancelling(false);
  }, [closeSocket]);

  /**
   * 切到某个已有会话（点侧边历史、或刷新后恢复上次会话）。
   * 先拉一次任务详情：既是为了显示标题/结果，也是为了在会话已被删除时
   * 尽早失败，而不是等 WebSocket 握手被 4404 拒绝。
   */
  const openSession = useCallback(
    async (targetThreadId: string) => {
      const task = await getTask(targetThreadId);
      closeSocket();

      threadIdRef.current = targetThreadId;
      storeThreadId(targetThreadId);
      cursorRef.current = 0;
      setThreadId(targetThreadId);
      // 与上面的 setThreadId 配套：无论线程号变没变，这次「打开会话」都必须伴随
      // 一次重连（理由见 connectionEpoch 的声明处）
      setConnectionEpoch((value) => value + 1);
      setEvents([]);
      setFiles([]);
      setUploadedItems([]);
      // 切会话时同样丢弃暂存附件：它们属于上一个草稿，不该被带到别的会话里
      stagedFileIdsRef.current = [];
      setLastError("");
      setResult(task.result || "");
      setTaskStatus(task.status);
      setIsRunning(!TERMINAL_STATUSES.has(task.status));
      setIsCancelling(false);

      await refreshFiles(targetThreadId).catch(() => undefined);
      return task;
    },
    [closeSocket, refreshFiles]
  );

  // WebSocket 连接：只有存在会话（thread_id）时才连。
  // 后端握手会先做归属校验，没有对应任务行会直接 4404 关闭。
  useEffect(() => {
    if (!threadId) {
      setConnectionState("closed");
      return;
    }

    let disposed = false;

    function connect() {
      if (disposed) {
        return;
      }
      clearSocketTimers();

      const token = getToken();
      if (!token) {
        // 令牌已被清掉（比如在别处触发登出），不再徒劳重连
        setConnectionState("closed");
        return;
      }

      const hadSocket = Boolean(socketRef.current);
      socketRef.current?.close();
      setConnectionState(hadSocket ? "reconnecting" : "connecting");

      // 浏览器无法在 WS 握手时设置请求头，令牌只能走查询参数
      const url = new URL(`${WS_BASE_URL}/ws/${encodeURIComponent(threadId!)}`);
      url.searchParams.set("token", token);
      url.searchParams.set("after", String(cursorRef.current));

      const socket = new WebSocket(url.toString());
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed || socketRef.current !== socket) {
          return;
        }
        setConnectionState("connected");
        setLastError("");
        heartbeatTimerRef.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send("ping");
          }
        }, 25000);
      };

      socket.onmessage = (event) => {
        if (socketRef.current !== socket) {
          return;
        }
        try {
          const payload = JSON.parse(event.data) as SocketMessage;
          if (payload.type === "pong") {
            setLastPongAt(new Date().toISOString());
            return;
          }
          if (payload.type !== "monitor_event") {
            return;
          }

          if (typeof payload.id === "number") {
            cursorRef.current = Math.max(cursorRef.current, payload.id);
          }
          setEvents((previous) => mergeEvents(previous, [payload]));

          const status = extractString(payload.data, "status");
          if (status) {
            setTaskStatus(status);
            if (TERMINAL_STATUSES.has(status)) {
              setIsRunning(false);
              setIsCancelling(false);
            }
          }

          if (payload.event === "task_result") {
            setResult(extractString(payload.data, "result") || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }

          if (payload.event === "task_cancelled") {
            setResult((previous) => previous || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }

          if (payload.event === "files_archived") {
            refreshFiles(threadId).catch(() => undefined);
          }

          if (payload.event === "error") {
            setLastError(payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }
        } catch (error) {
          setLastError(
            error instanceof Error ? error.message : "WebSocket 消息解析失败"
          );
        }
      };

      socket.onerror = () => {
        if (!disposed && socketRef.current === socket) {
          setLastError("WebSocket 连接异常，请确认后端服务已启动");
        }
      };

      socket.onclose = (closeEvent) => {
        if (socketRef.current !== socket) {
          return;
        }
        clearSocketTimers();

        // 鉴权/归属类关闭是确定性的，重连只会一直失败，还会刷屏
        if (closeEvent.code === WS_CLOSE_UNAUTHORIZED) {
          socketRef.current = null;
          clearAuth();
          setConnectionState("closed");
          return;
        }
        if (
          closeEvent.code === WS_CLOSE_FORBIDDEN ||
          closeEvent.code === WS_CLOSE_NOT_FOUND
        ) {
          socketRef.current = null;
          setLastError(closeEvent.reason || "会话不存在或无访问权限");
          setConnectionState("closed");
          return;
        }

        if (disposed) {
          setConnectionState("closed");
          return;
        }
        setConnectionState("reconnecting");
        reconnectTimerRef.current = window.setTimeout(connect, 2000);
      };
    }

    connect();

    return () => {
      disposed = true;
      closeSocket();
    };
  }, [clearSocketTimers, closeSocket, connectionEpoch, refreshFiles, threadId]);

  // 文件列表轮询：执行中勤一点，空闲时慢一点
  useEffect(() => {
    if (!threadId) {
      return;
    }

    const tick = () => {
      refreshFiles(threadId).catch((error: unknown) => {
        setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
      });
    };

    tick();
    const timer = window.setInterval(tick, isRunning ? 3000 : 10000);
    return () => window.clearInterval(timer);
  }, [isRunning, refreshFiles, threadId]);

  const submitTask = useCallback(
    async (query: string) => {
      const cleanQuery = query.trim();
      if (!cleanQuery) {
        throw new Error("请输入研搜任务");
      }

      setIsRunning(true);
      setIsCancelling(false);
      setLastError("");
      setResult("");
      setTaskStatus("queued");

      // 首轮提问：本地没有会话，清空轨迹；续聊则保留上下文再追加
      const currentThreadId = threadIdRef.current;
      if (!currentThreadId) {
        cursorRef.current = 0;
        setEvents([]);
      }

      try {
        // 带上暂存附件一起提交：服务端会在建任务的同一个事务里认领它们，
        // 保证任务入队时附件已经就位（详见后端 bind_staged_files）
        const response = await startTask(
          cleanQuery,
          currentThreadId,
          0,
          stagedFileIdsRef.current
        );
        // 提交成功后清空：这些 id 已经归属到任务上，重复提交会被服务端的
        // task_id IS NULL 条件挡掉，但留着只会让后续请求白白带一堆无用参数
        stagedFileIdsRef.current = [];
        if (response.thread_id !== threadIdRef.current) {
          threadIdRef.current = response.thread_id;
          storeThreadId(response.thread_id);
          cursorRef.current = 0;
          setEvents([]);
          setThreadId(response.thread_id);
        }
        return response;
      } catch (error) {
        setIsRunning(false);
        setTaskStatus(null);
        throw error;
      }
    },
    []
  );

  const cancelCurrentTask = useCallback(async () => {
    const currentThreadId = threadIdRef.current;
    if (!currentThreadId) {
      throw new Error("当前没有正在执行的任务");
    }

    setIsCancelling(true);
    setLastError("");
    try {
      const response = await cancelTask(currentThreadId);
      if (response.status === "cancelled") {
        setIsRunning(false);
        setIsCancelling(false);
        setTaskStatus("cancelled");
        setResult((previous) => previous || "任务已取消");
      }
      return response;
    } catch (error) {
      setIsCancelling(false);
      throw error;
    }
  }, []);

  const uploadFiles = useCallback(async (items: UploadedItem[]) => {
    if (items.length === 0) {
      throw new Error("请选择要上传的文件");
    }

    setIsUploading(true);
    setLastError("");
    try {
      const response = await uploadSessionFiles(
        items.map((item) => item.raw),
        threadIdRef.current
      );
      // 服务端没能归属到任务的文件（task_id 为空）先记下来，提交任务时一并认领
      const staged = (response.files || [])
        .filter((file) => !file.task_id)
        .map((file) => file.id);
      if (staged.length > 0) {
        stagedFileIdsRef.current = [...new Set([...stagedFileIdsRef.current, ...staged])];
      }
      setUploadedItems((previous) => {
        const names = new Set(previous.map((item) => item.name));
        const next = [...previous];
        items.forEach((item) => {
          if (!names.has(item.name)) {
            names.add(item.name);
            next.push(item);
          }
        });
        return next;
      });
      await refreshFiles().catch(() => undefined);
      return response;
    } finally {
      setIsUploading(false);
    }
  }, [refreshFiles]);

  /** 拉取服务端的完整事件历史，用于刷新/切换会话后补齐轨迹 */
  const loadEventHistory = useCallback(async (targetThreadId?: string | null) => {
    const id = targetThreadId ?? threadIdRef.current;
    if (!id) {
      return;
    }
    const response = await listEvents(id, 0);
    setEvents(mergeEvents([], response.items));
    cursorRef.current = Math.max(cursorRef.current, response.next_after);
  }, []);

  /** 会话被删除后同步清理本地状态 */
  const forgetSession = useCallback(
    (targetThreadId: string) => {
      if (threadIdRef.current === targetThreadId) {
        resetSession();
      }
    },
    [resetSession]
  );

  const stats = useMemo(() => {
    const toolEvents = events.filter((event) => event.event === "tool_start").length;
    const assistantEvents = events.filter(
      (event) => event.event === "assistant_call"
    ).length;
    const errorEvents = events.filter((event) => event.event === "error").length;

    return {
      toolEvents,
      assistantEvents,
      errorEvents,
      fileCount: files.length
    };
  }, [events, files.length]);

  return {
    connectionState,
    events,
    files,
    isCancelling,
    isRunning,
    isUploading,
    lastError,
    lastPongAt,
    taskStatus,
    threadId,
    uploadedItems,
    cancelCurrentTask,
    forgetSession,
    loadEventHistory,
    openSession,
    refreshFiles,
    resetSession,
    result,
    stats,
    submitTask,
    uploadFiles
  };
}

export { UnauthorizedError };
