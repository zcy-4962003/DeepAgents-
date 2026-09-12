import { App as AntApp, Empty, Modal, Skeleton, Tag } from "antd";
import { useCallback, useEffect, useState } from "react";
import { compareTasks } from "../lib/api";
import { MarkdownRenderer } from "./MarkdownRenderer";
import type { TaskDetail } from "../types";

interface ResultCompareModalProps {
  threadIds: string[];
  open: boolean;
  onClose: () => void;
}

export function ResultCompareModal({ threadIds, open, onClose }: ResultCompareModalProps) {
  const { message } = AntApp.useApp();
  const [tasks, setTasks] = useState<TaskDetail[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (threadIds.length < 2) {
      return;
    }
    setLoading(true);
    try {
      const response = await compareTasks(threadIds);
      setTasks(response.items || []);
    } catch (error) {
      // 对比接口对「含他人任务」是整体拒绝的，错误要原样告诉用户
      message.error(error instanceof Error ? error.message : "结果对比失败");
    } finally {
      setLoading(false);
    }
  }, [message, threadIds]);

  useEffect(() => {
    if (open) {
      void load();
    } else {
      setTasks([]);
    }
  }, [load, open]);

  return (
    <Modal
      className="compare-modal"
      footer={null}
      onCancel={onClose}
      open={open}
      title={`结果对比（${threadIds.length} 个任务）`}
      width="92vw"
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 10 }} />
      ) : tasks.length === 0 ? (
        <Empty description="没有可对比的结果" />
      ) : (
        <div
          className="compare-grid"
          style={{ gridTemplateColumns: `repeat(${tasks.length}, minmax(0, 1fr))` }}
        >
          {tasks.map((task) => (
            <section className="compare-column" key={task.id}>
              <header>
                <strong title={task.query}>{task.title || task.query}</strong>
                <Tag>{task.status}</Tag>
              </header>
              <div className="compare-body markdown-body">
                {task.result ? (
                  <MarkdownRenderer content={task.result} />
                ) : (
                  <Empty description="该任务没有结果" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                )}
              </div>
            </section>
          ))}
        </div>
      )}
    </Modal>
  );
}
