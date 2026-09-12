// 把一条任务的事件流切分成「一轮提问」。
//
// 场景：一个会话（thread）可以反复追问，每追问一次后端就把 tasks 行重置为
// queued 并重新入队，事件则一直往同一个 task_id 上追加。所以「历史回放」
// 拿到的是整条会话的全部事件，必须切分后才能一轮一轮地渲染成对话。
//
// 切分依据：每次提交任务时后端都会落一条 status=queued 的事件（见
// app/api/routes/tasks.py 的 create_task），它既是本轮的开始标记，
// 也带着本轮用户问的内容。

import type { MonitorMessage } from "../types";

const TERMINAL_STATUSES = new Set(["success", "failed", "cancelled"]);

export interface ConversationSegment {
  key: string;
  /** 本轮用户的提问；历史数据可能缺失，由调用方回退到任务原始 query */
  query: string;
  events: MonitorMessage[];
  result: string;
  isRunning: boolean;
  startedAt: string;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** 是否为「新的一轮提问」的开始事件 */
export function isTurnStart(event: MonitorMessage): boolean {
  return event.event === "task_status" && event.data?.status === "queued";
}

export function segmentEvents(events: MonitorMessage[]): ConversationSegment[] {
  const segments: ConversationSegment[] = [];
  let current: ConversationSegment | null = null;

  events.forEach((event, index) => {
    if (current === null || isTurnStart(event)) {
      current = {
        key: typeof event.id === "number" ? `turn-${event.id}` : `turn-${index}`,
        query: asString(event.data?.query),
        events: [],
        result: "",
        isRunning: true,
        startedAt: event.timestamp
      };
      segments.push(current);
    }

    current.events.push(event);

    if (event.event === "task_result") {
      current.result = asString(event.data?.result) || event.message;
      current.isRunning = false;
      return;
    }

    if (event.event === "task_cancelled" || event.event === "error") {
      current.isRunning = false;
      if (!current.result) {
        current.result = event.message;
      }
      return;
    }

    if (event.event === "task_status") {
      const status = asString(event.data?.status);
      if (TERMINAL_STATUSES.has(status)) {
        current.isRunning = false;
      }
    }
  });

  return segments;
}

/**
 * 取会话的「当前轮」。
 *
 * 事件流可能还没到达（刚提交、WS 尚未回放到那一条），此时返回一个占位段，
 * 让界面立刻显示出用户刚问的问题，而不是等到事件回来才渲染。
 */
export function buildSegments(
  events: MonitorMessage[],
  options: { pendingQuery?: string; fallbackQuery?: string; isRunning: boolean }
): ConversationSegment[] {
  const segments = segmentEvents(events);

  if (segments.length === 0) {
    if (!options.pendingQuery && !options.fallbackQuery) {
      return [];
    }
    return [
      {
        key: "pending",
        query: options.pendingQuery || options.fallbackQuery || "",
        events: [],
        result: "",
        isRunning: options.isRunning,
        startedAt: new Date().toISOString()
      }
    ];
  }

  // 首轮的历史数据可能没有 query（老数据），用任务本身的 query 兜底
  if (!segments[0].query && options.fallbackQuery) {
    segments[0] = { ...segments[0], query: options.fallbackQuery };
  }

  // 已提交但事件还没回来：末尾补一个占位段
  if (options.pendingQuery && !segments.some((item) => item.query === options.pendingQuery)) {
    segments.push({
      key: "pending",
      query: options.pendingQuery,
      events: [],
      result: "",
      isRunning: options.isRunning,
      startedAt: new Date().toISOString()
    });
  }

  const last = segments[segments.length - 1];
  if (last.isRunning && !options.isRunning) {
    segments[segments.length - 1] = { ...last, isRunning: false };
  }

  return segments;
}
