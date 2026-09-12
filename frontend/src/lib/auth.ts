// 令牌与当前用户的本地存储。
//
// 用 localStorage 而不是内存变量：刷新页面后仍要保持登录态，
// 否则用户每次刷新都要重新登录（配合后端的 WS 历史回放，刷新恢复体验才完整）。

import type { TokenResponse, UserInfo } from "../types";

const TOKEN_KEY = "deepsearch.token";
const USER_KEY = "deepsearch.user";

/** 登录态变化的订阅者；401 被动登出时用它通知 UI 切回登录页 */
type Listener = () => void;
const listeners = new Set<Listener>();

export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getStoredUser(): UserInfo | null {
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as UserInfo;
  } catch {
    // 存储被外部改坏时按未登录处理，而不是让整个应用崩在解析上
    window.localStorage.removeItem(USER_KEY);
    return null;
  }
}

export function saveAuth(payload: TokenResponse): void {
  window.localStorage.setItem(TOKEN_KEY, payload.access_token);
  window.localStorage.setItem(USER_KEY, JSON.stringify(payload.user));
  emit();
}

export function clearAuth(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
  emit();
}

export function isLoggedIn(): boolean {
  return Boolean(getToken());
}

export function subscribeAuth(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(): void {
  listeners.forEach((listener) => listener());
}

/** 统一的鉴权请求头；未登录时返回空对象，由调用方决定是否报错。 */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
