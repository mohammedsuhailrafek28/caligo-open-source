import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, onUnauthorized, setCsrfToken } from "./api/client";
import type { S } from "./api/types";

type Seller = S["SellerView"];
interface AuthState { status: "loading" | "anonymous" | "authenticated"; seller: Seller | null; expiresAt: string | null; expired: boolean }
interface AuthApi extends AuthState {
  login(email: string, password: string): Promise<void>;
  register(displayName: string, email: string, password: string): Promise<void>;
  logout(): Promise<void>;
}

const AuthContext = createContext<AuthApi | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<AuthState>({ status: "loading", seller: null, expiresAt: null, expired: false });

  useEffect(() => {
    let live = true;
    api.get<S["MeResponse"]>("/api/v1/auth/me")
      .then((me) => live && setState({ status: "authenticated", seller: me.seller, expiresAt: me.expires_at, expired: false }))
      .catch(() => live && setState({ status: "anonymous", seller: null, expiresAt: null, expired: false }));
    return () => { live = false; };
  }, []);

  useEffect(() => onUnauthorized(() => {
    setCsrfToken(null);
    queryClient.clear();
    setState((s) => (s.status === "authenticated" ? { status: "anonymous", seller: null, expiresAt: null, expired: true } : s));
  }), [queryClient]);

  const login = useCallback(async (email: string, password: string) => {
    const res = await api.post<S["LoginResponse"]>("/api/v1/auth/login", { email, password });
    setCsrfToken(res.csrf_token);
    queryClient.clear();
    setState({ status: "authenticated", seller: res.seller, expiresAt: res.expires_at, expired: false });
  }, [queryClient]);

  const register = useCallback(async (display_name: string, email: string, password: string) => {
    await api.post("/api/v1/auth/register", { display_name, email, password });
    await login(email, password);
  }, [login]);

  const logout = useCallback(async () => {
    try { await api.post("/api/v1/auth/logout"); } catch (e) { if (!(e instanceof ApiError) || e.status !== 401) throw e; }
    setCsrfToken(null);
    queryClient.clear();
    setState({ status: "anonymous", seller: null, expiresAt: null, expired: false });
  }, [queryClient]);

  const value = useMemo(() => ({ ...state, login, register, logout }), [state, login, register, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthApi {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
