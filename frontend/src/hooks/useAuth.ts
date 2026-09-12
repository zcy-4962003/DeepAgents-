import { useCallback, useEffect, useState } from "react";
import { login as loginRequest, logout as logoutRequest, register as registerRequest } from "../lib/api";
import { clearAuth, getStoredUser, getToken, saveAuth, subscribeAuth } from "../lib/auth";
import type { LoginPayloadLike, UserInfo } from "../types";

/**
 * 登录态。
 *
 * 状态直接读 localStorage，并订阅 auth 模块的变更通知 —— 这样 401 被动登出
 * （api.ts 里调用 clearAuth）也能自动把界面切回登录页，不用每个调用点各写一遍。
 */
export function useAuth() {
  const [user, setUser] = useState<UserInfo | null>(() => getStoredUser());

  useEffect(() => {
    return subscribeAuth(() => {
      setUser(getStoredUser());
    });
  }, []);

  const login = useCallback(async (payload: LoginPayloadLike) => {
    const response = await loginRequest(payload);
    saveAuth(response);
    return response.user;
  }, []);

  const register = useCallback(async (payload: LoginPayloadLike) => {
    const response = await registerRequest(payload);
    saveAuth(response);
    return response.user;
  }, []);

  const logout = useCallback(async () => {
    // logout 内部保证即使请求失败也会清本地登录态
    await logoutRequest().catch(() => undefined);
    clearAuth();
  }, []);

  return {
    user,
    login,
    register,
    logout,
    // 有令牌但用户信息解析失败时按未登录处理，避免带着半截状态进主界面
    isAuthenticated: Boolean(user && getToken())
  };
}
