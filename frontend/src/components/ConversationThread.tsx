import {
  BranchesOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FileMarkdownOutlined,
  FilePdfOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  StopOutlined,
  ToolOutlined,
  WarningOutlined
} from "@ant-design/icons";
import { Button, Select, Tooltip } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ConversationSegment } from "../lib/turns";
import type { FileDetail, MonitorMessage } from "../types";
import { MarkdownRenderer } from "./MarkdownRenderer";

interface ConversationThreadProps {
  segments: ConversationSegment[];
  files: FileDetail[];
  onUseExample: (prompt: string) => void;
  onPreviewFile: (file: FileDetail) => void;
  onDownloadFile: (file: FileDetail) => void;
  onEditFile: (file: FileDetail) => void;
}

const TASK_EXAMPLES = [
  {
    tool: "网络搜索工具",
    title: "联网趋势研判",
    prompt:
      "请使用网络搜索工具，检索 2026 年跨境电商 AI 客服趋势，列出 5 条关键变化，并附上来源链接。",
    icon: <CloudServerOutlined aria-hidden />
  },
  {
    tool: "数据库查询工具",
    title: "药品库存排查",
    prompt:
      "请使用数据库查询工具，查询库存大于 100 的药品，按库存量升序列出药品名称、批次号、仓库位置和过期日期。",
    icon: <DatabaseOutlined aria-hidden />
  },
  {
    tool: "本地知识库",
    title: "内部文档问答",
    prompt:
      "请使用本地知识库检索助手，查询公司内部白皮书中关于品类策略的内容，并整理成三条可执行建议。",
    icon: <FileSearchOutlined aria-hidden />
  },
  {
    tool: "上传文件分析",
    title: "附件提炼",
    prompt:
      "请阅读我上传的附件，提炼核心观点、风险点和待补充信息，并给出下一步分析计划。",
    icon: <FileTextOutlined aria-hidden />
  },
  {
    tool: "Markdown / PDF",
    title: "生成交付报告",
    prompt:
      "请基于本次调研结果生成一份 Markdown 报告，并转换成 PDF。",
    icon: <FileMarkdownOutlined aria-hidden />
  }
];

/** 轨迹筛选下拉的选项；空值表示不筛选 */
const EVENT_FILTER_OPTIONS = [
  { value: "assistant_call", label: "助手调度" },
  { value: "tool_start", label: "工具调用" },
  { value: "task_result", label: "任务结果" },
  { value: "task_status", label: "状态变化" },
  { value: "task_cancelled", label: "取消" },
  { value: "session_created", label: "会话创建" },
  { value: "files_archived", label: "文件归档" },
  { value: "error", label: "异常" }
];

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--:--";
  }
  return date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit"
  });
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function parseTime(value: string): number | null {
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

function formatDuration(value: number): string {
  const totalSeconds = Math.max(0, Math.floor(value / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const paddedMinutes = String(minutes).padStart(2, "0");
  const paddedSeconds = String(seconds).padStart(2, "0");

  if (hours > 0) {
    return `${hours}:${paddedMinutes}:${paddedSeconds}`;
  }
  return `${paddedMinutes}:${paddedSeconds}`;
}

function getLastEventTime(events: MonitorMessage[], eventName?: string): number | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!eventName || event.event === eventName) {
      return parseTime(event.timestamp);
    }
  }
  return null;
}

function getThinkingDuration(
  events: MonitorMessage[],
  fallbackStart: string,
  isRunning: boolean,
  now: number
): string {
  const startedAt =
    (events[0] ? parseTime(events[0].timestamp) : null) ??
    parseTime(fallbackStart) ??
    now;
  const finishedAt =
    getLastEventTime(events, "task_result") ??
    (!isRunning ? getLastEventTime(events) : null) ??
    now;
  return formatDuration(finishedAt - startedAt);
}

function EventIcon({ event }: { event: string }) {
  if (event === "assistant_call") {
    return <BranchesOutlined aria-hidden />;
  }
  if (event === "tool_start") {
    return <ToolOutlined aria-hidden />;
  }
  if (event === "session_created") {
    return <FileSearchOutlined aria-hidden />;
  }
  if (event === "task_result") {
    return <CheckCircleOutlined aria-hidden />;
  }
  if (event === "task_cancelled") {
    return <StopOutlined aria-hidden />;
  }
  if (event === "error") {
    return <WarningOutlined aria-hidden />;
  }
  return <ClockCircleOutlined aria-hidden />;
}

function FileIcon({ name }: { name: string }) {
  if (name.toLowerCase().endsWith(".pdf")) {
    return <FilePdfOutlined aria-hidden />;
  }
  if (name.toLowerCase().endsWith(".md")) {
    return <FileMarkdownOutlined aria-hidden />;
  }
  return <FileTextOutlined aria-hidden />;
}

function ThinkingTimeline({ events }: { events: MonitorMessage[] }) {
  const timelineRef = useRef<HTMLOListElement | null>(null);
  const [filter, setFilter] = useState<string | undefined>(undefined);

  const visible = useMemo(
    () => (filter ? events.filter((event) => event.event === filter) : events),
    [events, filter]
  );

  // 只在「不筛选」时自动滚到底：筛选状态下用户是在翻看某类事件，不该被拽走
  useEffect(() => {
    const timelineNode = timelineRef.current;
    if (!timelineNode || filter) {
      return;
    }
    window.requestAnimationFrame(() => {
      timelineNode.scrollTop = timelineNode.scrollHeight;
    });
  }, [filter, events.length]);

  return (
    <div className="thinking-body">
      <div className="thinking-toolbar">
        <Select
          allowClear
          className="thinking-filter"
          onChange={(value) => setFilter(value || undefined)}
          options={EVENT_FILTER_OPTIONS}
          placeholder="按类型筛选"
          popupMatchSelectWidth={false}
          size="small"
          value={filter}
        />
        <span className="thinking-count">
          {filter ? `${visible.length} / ${events.length}` : events.length} 条
        </span>
      </div>

      {events.length === 0 ? (
        <div className="thinking-empty">
          <ClockCircleOutlined aria-hidden />
          等待后端推送执行事件
        </div>
      ) : visible.length === 0 ? (
        <div className="thinking-empty">
          <WarningOutlined aria-hidden />
          没有「{filter}」类型的事件
        </div>
      ) : (
        <ol className="thinking-timeline" ref={timelineRef}>
          {visible.map((event, index) => (
            <li
              className={`thinking-event thinking-event--${event.event}`}
              key={typeof event.id === "number" ? event.id : `${event.timestamp}-${index}`}
            >
              <span className="thinking-event-icon">
                <EventIcon event={event.event} />
              </span>
              <div>
                <div className="thinking-event-meta">
                  <span>{event.event}</span>
                  <time dateTime={event.timestamp}>{formatTime(event.timestamp)}</time>
                </div>
                <p>{event.message}</p>
                {event.event === "assistant_call" || event.event === "tool_start" ? (
                  <code>{JSON.stringify(event.data)}</code>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function ArtifactShelf({
  files,
  onPreviewFile,
  onDownloadFile,
  onEditFile
}: {
  files: FileDetail[];
  onPreviewFile: (file: FileDetail) => void;
  onDownloadFile: (file: FileDetail) => void;
  onEditFile: (file: FileDetail) => void;
}) {
  if (files.length === 0) {
    return (
      <div className="artifact-empty">
        <FileSearchOutlined aria-hidden />
        暂无输出文件
      </div>
    );
  }

  return (
    <div className="artifact-shelf">
      {files.map((file) => (
        <div className="artifact-card" key={file.id}>
          <span className="artifact-icon">
            <FileIcon name={file.name} />
          </span>
          <div className="artifact-copy">
            <strong title={file.name}>{file.name}</strong>
            <span>
              {formatBytes(file.size)} · {file.kind === "uploaded" ? "上传" : "生成"}
            </span>
          </div>
          <Tooltip title="预览">
            <Button
              aria-label={`预览 ${file.name}`}
              className="artifact-action"
              icon={<EyeOutlined />}
              onClick={() => onPreviewFile(file)}
              shape="circle"
            />
          </Tooltip>
          <Tooltip title="编辑">
            <Button
              aria-label={`编辑 ${file.name}`}
              className="artifact-action"
              icon={<EditOutlined />}
              onClick={() => onEditFile(file)}
              shape="circle"
            />
          </Tooltip>
          <Tooltip title="下载">
            <Button
              aria-label={`下载 ${file.name}`}
              className="artifact-action"
              icon={<DownloadOutlined />}
              onClick={() => onDownloadFile(file)}
              shape="circle"
            />
          </Tooltip>
        </div>
      ))}
    </div>
  );
}

function ThinkingLoader({ durationLabel }: { durationLabel: string }) {
  return (
    <div className="thinking-loader" aria-live="polite" aria-label="正在生成回复">
      <div className="loader-status">
        <span className="loader-pulse" aria-hidden />
        <strong>正在研搜</strong>
        <span className="loader-duration">已思考 {durationLabel}</span>
        <span className="loader-dots" aria-hidden>
          <i />
          <i />
          <i />
        </span>
      </div>
      <div className="loader-track" aria-hidden />
      <ul className="loader-steps" aria-hidden>
        <li>理解问题</li>
        <li>调度工具</li>
        <li>汇总答案</li>
      </ul>
    </div>
  );
}

interface AssistantMessageProps {
  segment: ConversationSegment;
  files: FileDetail[];
  onPreviewFile: (file: FileDetail) => void;
  onDownloadFile: (file: FileDetail) => void;
  onEditFile: (file: FileDetail) => void;
}

function AssistantMessage({
  segment,
  files,
  onPreviewFile,
  onDownloadFile,
  onEditFile
}: AssistantMessageProps) {
  const { events, isRunning, result, startedAt } = segment;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!isRunning) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isRunning]);

  const durationLabel = getThinkingDuration(events, startedAt, isRunning, now);
  const isCancelled = events.some((event) => event.event === "task_cancelled");
  const failed = events.some((event) => event.event === "error");
  const syncLabel = isRunning
    ? `生成中 · 思考 ${durationLabel}`
    : `${isCancelled ? "已取消" : failed ? "已中断" : "已同步"} · 用时 ${durationLabel}`;

  return (
    <article className="chat-message chat-message--assistant">
      <div className="message-avatar">AI</div>
      <div className="message-bubble">
        <div className="message-meta">
          <span>DeepSearch Agents</span>
          <time>{syncLabel}</time>
        </div>

        <details className="thinking-block" open={isRunning || events.length > 0}>
          <summary>
            <span>
              <BranchesOutlined aria-hidden />
              深度研搜过程
            </span>
            <strong>{events.length}</strong>
          </summary>
          <ThinkingTimeline events={events} />
        </details>

        {result ? (
          <div className="assistant-answer">
            <MarkdownRenderer content={result} />
          </div>
        ) : (
          <div className="assistant-answer assistant-answer--pending">
            {isRunning ? (
              <ThinkingLoader durationLabel={durationLabel} />
            ) : (
              "任务完成后会在这里显示最终回复。"
            )}
          </div>
        )}

        <details className="thinking-block artifact-block" open={files.length > 0}>
          <summary>
            <span>
              <FileSearchOutlined aria-hidden />
              会话文件
            </span>
            <strong>{files.length}</strong>
          </summary>
          <ArtifactShelf
            files={files}
            onDownloadFile={onDownloadFile}
            onEditFile={onEditFile}
            onPreviewFile={onPreviewFile}
          />
        </details>
      </div>
    </article>
  );
}

export function ConversationThread({
  segments,
  files,
  onUseExample,
  onPreviewFile,
  onDownloadFile,
  onEditFile
}: ConversationThreadProps) {
  if (segments.length === 0) {
    return (
      <div className="conversation-empty">
        <div className="empty-examples">
          <div className="empty-examples-copy">
            <span className="panel-kicker">TASK EXAMPLES</span>
            <h3>选择一个工具任务开始</h3>
            <p>每个示例会触发不同工具路径，执行轨迹和输出文件会直接出现在对话里。</p>
          </div>

          <div className="example-grid" aria-label="研搜任务示例">
            {TASK_EXAMPLES.map((example) => (
              <button
                className="example-card"
                key={example.tool}
                onClick={() => onUseExample(example.prompt)}
                type="button"
              >
                <span className="example-icon">{example.icon}</span>
                <span className="example-copy">
                  <span>{example.tool}</span>
                  <strong>{example.title}</strong>
                  <small>{example.prompt}</small>
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="conversation-thread" aria-label="聊天消息流">
      {segments.map((segment) => (
        <div className="conversation-turn" key={segment.key}>
          <article className="chat-message chat-message--user">
            <div className="message-bubble">
              <div className="message-meta">
                <span>你</span>
                <time dateTime={segment.startedAt}>
                  {formatTime(segment.startedAt)}
                </time>
              </div>
              <p>{segment.query}</p>
            </div>
          </article>

          <AssistantMessage
            files={files}
            onDownloadFile={onDownloadFile}
            onEditFile={onEditFile}
            onPreviewFile={onPreviewFile}
            segment={segment}
          />
        </div>
      ))}
    </div>
  );
}
