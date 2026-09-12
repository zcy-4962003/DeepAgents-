// 最近一次会话的本地记忆。
//
// thread_id 现在由服务端生成（就是 tasks.id），前端不再自己造 ID ——
// 旧版本前端可以任意传 thread_id，等于能读到别人的会话。
// 这里只记「上次停在哪个会话」，用于刷新后恢复到原会话；
// 存下来的 ID 可能已经失效（被删、或用另一个账号登录），所以用之前必须校验。

const STORAGE_KEY = "deepsearch.thread_id";

export function getStoredThreadId(): string | null {
  return window.localStorage.getItem(STORAGE_KEY);
}

export function storeThreadId(threadId: string): void {
  window.localStorage.setItem(STORAGE_KEY, threadId);
}

export function clearStoredThreadId(): void {
  window.localStorage.removeItem(STORAGE_KEY);
}
