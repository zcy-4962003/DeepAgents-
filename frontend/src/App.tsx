import {
  ApiOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CloudServerOutlined,
  CloseCircleOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  LogoutOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Popconfirm, Spin, Tooltip } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChatComposer } from "./components/ChatComposer";
import { ConversationThread } from "./components/ConversationThread";
import { FilePreviewDrawer } from "./components/FilePreviewDrawer";
import { LoginPage } from "./components/LoginPage";
import { MarkdownEditorModal } from "./components/MarkdownEditorModal";
import { ResultCompareModal } from "./components/ResultCompareModal";
import { TaskHistory } from "./components/TaskHistory";
import { useAuth } from "./hooks/useAuth";
import { useDeepAgentSession } from "./hooks/useDeepAgentSession";
import { API_BASE_URL, WS_BASE_URL } from "./lib/config";
import { getStoredThreadId } from "./lib/thread";
import { buildSegments } from "./lib/turns";
import { getDownloadUrl } from "./lib/api";
import type { ConnectionState, FileDetail, UploadedItem } from "./types";

function connectionLabel(state: ConnectionState): string {
  const labels: Record<ConnectionState, string> = {
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重连中",
    closed: "已关闭"
  };
  return labels[state];
}

export default function App() {
  const { message } = AntApp.useApp();
  const { user, logout, isAuthenticated } = useAuth();

  const [query, setQuery] = useState("");
  const [stagedItems, setStagedItems] = useState<UploadedItem[]>([]);
  const [historyKey, setHistoryKey] = useState(0);
  const [opening, setOpening] = useState(false);
  const streamRef = useRef<HTMLElement | null>(null);
  const session = useDeepAgentSession();

  // 预览/编辑/对比三个弹层共用的受控状态
  const [previewFile, setPreviewFile] = useState<FileDetail | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [editingFile, setEditingFile] = useState<FileDetail | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);

  /**
   * 刚提交的问题。事件流回来之前先用它渲染用户气泡，
   * 事件到达后由 turns.ts 按 status=queued 事件切分出真正的分段，这里的占位自动消失。
   */
  const [pendingQuery, setPendingQuery] = useState("");

  // 会话首轮的提问。旧的 queued 事件里没有 query 字段（本轮才补上），
  // 回放这类历史时用任务本身的 query 兜底，否则用户气泡会是空的。
  const [taskQuery, setTaskQuery] = useState("");

  const segments = useMemo(
    () =>
      buildSegments(session.events, {
        pendingQuery,
        fallbackQuery: taskQuery,
        isRunning: session.isRunning
      }),
    [pendingQuery, session.events, session.isRunning, taskQuery]
  );

  // 登录后恢复上次会话。存下来的 ID 可能已被删除或不属于当前账号，
  // getTask 会失败，此时静默清掉，让用户停在空白页而不是报错。
  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    const lastThreadId = getStoredThreadId();
    if (!lastThreadId) {
      return;
    }

    let cancelled = false;
    setOpening(true);
    session
      .openSession(lastThreadId)
      .then((task) => {
        if (!cancelled) {
          setTaskQuery(task.query);
        }
      })
      .catch(() => {
        if (!cancelled) {
          session.resetSession();
        }
      })
      .finally(() => {
        if (!cancelled) {
          setOpening(false);
        }
      });

    return () => {
      cancelled = true;
    };
    // 只在登录态变化时执行一次；session 对象每次渲染都是新的，不能进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  useEffect(() => {
    const streamNode = streamRef.current;
    if (!streamNode) {
      return;
    }
    window.requestAnimationFrame(() => {
      streamNode.scrollTo({ top: streamNode.scrollHeight, behavior: "smooth" });
    });
  }, [segments]);

  const handleSubmit = useCallback(async () => {
    const cleanQuery = query.trim();
    if (!cleanQuery) {
      message.warning("请输入研搜任务");
      return;
    }

    setQuery("");
    setPendingQuery(cleanQuery);
    // 首轮提问就是这个会话的 query，历史回放时的兜底文案用它
    setTaskQuery((previous) => previous || cleanQuery);
    try {
      await session.submitTask(cleanQuery);
      setHistoryKey((previous) => previous + 1);
    } catch (error) {
      setPendingQuery("");
      message.error(error instanceof Error ? error.message : "任务启动失败");
    }
  }, [message, query, session]);

  // 事件流已经带上了本轮提问，占位就可以撤了
  useEffect(() => {
    if (!pendingQuery) {
      return;
    }
    if (session.events.some((event) => event.data?.query === pendingQuery)) {
      setPendingQuery("");
    }
  }, [pendingQuery, session.events]);

  async function handleCancel() {
    try {
      const response = await session.cancelCurrentTask();
      message.info(
        response.status === "cancelling"
          ? "取消请求已发送，正在等待当前调用结束"
          : "任务已取消"
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : "取消任务失败");
    }
  }

  async function handleUpload(items: UploadedItem[]) {
    try {
      const response = await session.uploadFiles(items);
      setStagedItems([]);
      message.success(`已上传 ${response.files.length} 个文件`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    }
  }

  function handleNewSession() {
    session.resetSession();
    setQuery("");
    setStagedItems([]);
    setPendingQuery("");
    setTaskQuery("");
  }

  async function handleOpenSession(threadId: string) {
    setOpening(true);
    setPendingQuery("");
    try {
      const task = await session.openSession(threadId);
      setTaskQuery(task.query);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "打开会话失败");
      setHistoryKey((previous) => previous + 1);
    } finally {
      setOpening(false);
    }
  }

  async function handleDownloadFile(file: FileDetail) {
    try {
      const response = await getDownloadUrl(file.id);
      // 预签名地址由对象存储直出，开新标签即可，无需后端代理带宽
      window.open(response.url, "_blank", "noopener,noreferrer");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "获取下载地址失败");
    }
  }

  function handlePreviewFile(file: FileDetail) {
    setPreviewFile(file);
    setPreviewOpen(true);
  }

  function handleEditFile(file: FileDetail) {
    setEditingFile(file);
    setEditorOpen(true);
  }

  function handleDeleted(threadId: string) {
    session.forgetSession(threadId);
    setHistoryKey((previous) => previous + 1);
  }

  if (!isAuthenticated) {
    return <LoginPage />;
  }

  const online = session.connectionState === "connected";

  return (
    <div className="chat-app-shell min-h-dvh">
      <aside className="chat-sidebar" aria-label="会话信息">
        <div className="sidebar-brand">
          <span className="panel-kicker">DEEPSEARCH</span>
          <h1>深度研搜</h1>
          <p>对话式多智能体研究台</p>
        </div>

        <Button className="new-chat-button" block onClick={handleNewSession}>
          新建研搜
        </Button>

        <TaskHistory
          activeThreadId={session.threadId}
          onCompare={(ids) => {
            setCompareIds(ids);
            setCompareOpen(true);
          }}
          onDeleted={handleDeleted}
          onOpen={(threadId) => void handleOpenSession(threadId)}
          refreshKey={historyKey}
        />

        <div className="sidebar-section">
          <span className="sidebar-label">THREAD</span>
          <strong className="thread-id" title={session.threadId || "尚未开始"}>
            {session.threadId ? session.threadId.slice(0, 8) : "—"}
          </strong>
        </div>

        <div className="sidebar-status-list">
          <div
            className={`sidebar-status ${
              online ? "sidebar-status--online" : "sidebar-status--warn"
            }`}
          >
            <ApiOutlined aria-hidden />
            <span>WebSocket</span>
            <strong>{connectionLabel(session.connectionState)}</strong>
          </div>
          <div className="sidebar-status">
            <BranchesOutlined aria-hidden />
            <span>助手调度</span>
            <strong>{session.stats.assistantEvents}</strong>
          </div>
          <div className="sidebar-status">
            <ToolOutlined aria-hidden />
            <span>工具调用</span>
            <strong>{session.stats.toolEvents}</strong>
          </div>
          <div
            className={
              session.stats.errorEvents > 0
                ? "sidebar-status sidebar-status--error"
                : "sidebar-status"
            }
          >
            <CloseCircleOutlined aria-hidden />
            <span>异常</span>
            <strong>{session.stats.errorEvents}</strong>
          </div>
        </div>

        <div className="sidebar-section">
          <span className="sidebar-label">AGENTS</span>
          <ul className="agent-mini-list">
            <li>
              <CloudServerOutlined aria-hidden />
              网络搜索助手
            </li>
            <li>
              <DatabaseOutlined aria-hidden />
              数据库查询助手
            </li>
            <li>
              <FileSearchOutlined aria-hidden />
              本地知识库检索助手
            </li>
          </ul>
        </div>

        <div className="sidebar-section sidebar-endpoints">
          <span className="sidebar-label">ENDPOINTS</span>
          <code>{API_BASE_URL}</code>
          <code>{WS_BASE_URL}</code>
        </div>

        <div className="sidebar-user">
          <div className="sidebar-user-copy">
            <strong title={user?.username}>{user?.display_name || user?.username}</strong>
            <span>{user?.role === "admin" ? "管理员" : "员工"}</span>
          </div>
          <Popconfirm
            cancelText="取消"
            okText="退出"
            onConfirm={() => void logout()}
            title="确定退出登录？"
          >
            <Tooltip title="退出登录">
              <Button
                aria-label="退出登录"
                className="icon-button"
                icon={<LogoutOutlined />}
                size="small"
                type="text"
              />
            </Tooltip>
          </Popconfirm>
        </div>
      </aside>

      <main className="chat-main">
        <header className="chat-topbar">
          <div>
            <span className="panel-kicker">CHAT WORKSPACE</span>
            <h2>深度研搜对话</h2>
          </div>
          <div className={`run-indicator ${session.isRunning ? "run-indicator--live" : ""}`}>
            {session.isRunning ? (
              <BranchesOutlined aria-hidden />
            ) : (
              <CheckCircleOutlined aria-hidden />
            )}
            {session.isRunning ? "研搜中" : "待命"}
          </div>
        </header>

        {session.lastError ? (
          <Alert className="chat-alert" message={session.lastError} showIcon type="error" />
        ) : null}

        <section className="chat-stream-panel" ref={streamRef}>
          {opening ? (
            <div className="conversation-loading">
              <Spin />
              <span>正在恢复会话…</span>
            </div>
          ) : (
            <ConversationThread
              files={session.files}
              onDownloadFile={(file) => void handleDownloadFile(file)}
              onEditFile={handleEditFile}
              onPreviewFile={handlePreviewFile}
              onUseExample={setQuery}
              segments={segments}
            />
          )}
        </section>

        <ChatComposer
          isCancelling={session.isCancelling}
          isRunning={session.isRunning}
          isUploading={session.isUploading}
          onCancel={() => void handleCancel()}
          onNewSession={handleNewSession}
          onQueryChange={setQuery}
          onStagedItemsChange={setStagedItems}
          onSubmit={() => void handleSubmit()}
          onUpload={handleUpload}
          query={query}
          stagedItems={stagedItems}
          uploadedItems={session.uploadedItems}
        />
      </main>

      <FilePreviewDrawer
        file={previewFile}
        onClose={() => setPreviewOpen(false)}
        onEdit={(file) => {
          setPreviewOpen(false);
          handleEditFile(file);
        }}
        open={previewOpen}
      />

      <MarkdownEditorModal
        file={editingFile}
        onClose={() => setEditorOpen(false)}
        onSaved={() => {
          void session.refreshFiles();
          void session.loadEventHistory();
        }}
        open={editorOpen}
      />

      <ResultCompareModal
        onClose={() => setCompareOpen(false)}
        open={compareOpen}
        threadIds={compareIds}
      />
    </div>
  );
}
