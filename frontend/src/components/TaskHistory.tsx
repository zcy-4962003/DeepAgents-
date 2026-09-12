import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  HistoryOutlined,
  LoadingOutlined,
  StopOutlined
} from "@ant-design/icons";
import { App as AntApp, Button, Checkbox, Empty, Popconfirm, Tooltip } from "antd";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { deleteTask, listTasks } from "../lib/api";
import type { TaskDetail } from "../types";

/** 后端 tasks.status 的取值，见 app/db/models.py */
const STATUS_META: Record<string, { label: string; icon: ReactNode; className: string }> = {
  queued: { label: "排队中", icon: <ClockCircleOutlined />, className: "task-status--queued" },
  running: { label: "执行中", icon: <LoadingOutlined />, className: "task-status--running" },
  success: { label: "已完成", icon: <CheckCircleOutlined />, className: "task-status--success" },
  failed: { label: "失败", icon: <CloseCircleOutlined />, className: "task-status--failed" },
  cancelled: { label: "已取消", icon: <StopOutlined />, className: "task-status--cancelled" }
};

function statusMeta(status: string) {
  return (
    STATUS_META[status] || {
      label: status,
      icon: <ClockCircleOutlined />,
      className: "task-status--unknown"
    }
  );
}

function formatWhen(value: string | null): string {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }

  const now = new Date();
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();

  return sameDay
    ? date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

/** 后端 /api/tasks/compare 要求 2~5 个任务 */
const COMPARE_MIN = 2;
const COMPARE_MAX = 5;

interface TaskHistoryProps {
  /** 变化时重新拉取列表（提交任务后由 App 递增） */
  refreshKey: number;
  activeThreadId: string | null;
  onOpen: (threadId: string) => void;
  onDeleted: (threadId: string) => void;
  onCompare: (threadIds: string[]) => void;
}

export function TaskHistory({
  refreshKey,
  activeThreadId,
  onOpen,
  onDeleted,
  onCompare
}: TaskHistoryProps) {
  const { message } = AntApp.useApp();
  const [tasks, setTasks] = useState<TaskDetail[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await listTasks({ limit: 30 });
      setTasks(response.items || []);
    } catch (error) {
      // 历史列表拉取失败不该打断主流程，只在侧栏留个提示
      message.error(error instanceof Error ? error.message : "任务历史加载失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function handleDelete(threadId: string) {
    try {
      await deleteTask(threadId);
      setTasks((previous) => previous.filter((item) => item.id !== threadId));
      setSelected((previous) => previous.filter((id) => id !== threadId));
      onDeleted(threadId);
      message.success("任务已删除");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  }

  function toggleSelected(threadId: string) {
    setSelected((previous) => {
      if (previous.includes(threadId)) {
        return previous.filter((id) => id !== threadId);
      }
      if (previous.length >= COMPARE_MAX) {
        message.warning(`一次最多对比 ${COMPARE_MAX} 个任务`);
        return previous;
      }
      return [...previous, threadId];
    });
  }

  return (
    <div className="sidebar-section task-history">
      <div className="task-history-head">
        <span className="sidebar-label">任务历史</span>
        <Button
          aria-label="刷新任务历史"
          className="icon-button"
          icon={<HistoryOutlined />}
          loading={loading}
          onClick={() => void load()}
          size="small"
          type="text"
        />
      </div>

      {selected.length > 0 ? (
        <div className="task-history-compare">
          <span>已选 {selected.length} 个</span>
          <Button
            disabled={selected.length < COMPARE_MIN}
            onClick={() => onCompare(selected)}
            size="small"
            type="primary"
          >
            对比结果
          </Button>
          <Button onClick={() => setSelected([])} size="small" type="text">
            清空
          </Button>
        </div>
      ) : null}

      {tasks.length === 0 ? (
        <Empty
          className="compact-empty"
          description={loading ? "加载中" : "还没有任务"}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      ) : (
        <ul className="task-history-list">
          {tasks.map((task) => {
            const meta = statusMeta(task.status);
            const active = task.id === activeThreadId;
            return (
              <li
                className={`task-history-item ${active ? "task-history-item--active" : ""}`}
                key={task.id}
              >
                <Checkbox
                  aria-label={`选择任务 ${task.title || task.query}`}
                  checked={selected.includes(task.id)}
                  className="task-history-check"
                  onChange={() => toggleSelected(task.id)}
                />
                <button
                  className="task-history-open"
                  onClick={() => onOpen(task.id)}
                  title={task.title || task.query}
                  type="button"
                >
                  <span className={`task-status ${meta.className}`}>{meta.icon}</span>
                  <span className="task-history-copy">
                    <strong>{task.title || task.query || "未命名任务"}</strong>
                    <span>
                      {meta.label}
                      {task.created_at ? ` · ${formatWhen(task.created_at)}` : ""}
                    </span>
                  </span>
                </button>

                <Popconfirm
                  cancelText="取消"
                  description="任务的事件与文件记录会一并删除。"
                  okText="删除"
                  onConfirm={() => void handleDelete(task.id)}
                  title="确定删除这条任务？"
                >
                  <Tooltip title="删除">
                    <Button
                      aria-label={`删除任务 ${task.title || task.query}`}
                      className="task-history-delete"
                      icon={<DeleteOutlined />}
                      size="small"
                      type="text"
                    />
                  </Tooltip>
                </Popconfirm>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
