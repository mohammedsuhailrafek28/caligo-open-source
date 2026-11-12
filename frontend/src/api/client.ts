/** Typed fetch wrapper for the Caligo backend. Session cookie auth + double-submit CSRF; no demo identity headers, no secrets. */
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public requestId?: string, public details?: unknown) {
    super(message);
  }
}

const CSRF_COOKIE = "pem_csrf";
let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void { csrfToken = token; }

function csrf(): string | null {
  if (csrfToken) return csrfToken;
  const match = document.cookie.split("; ").find((c) => c.startsWith(`${CSRF_COOKIE}=`));
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : null;
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();
export function onUnauthorized(listener: Listener): () => void { unauthorizedListeners.add(listener); return () => unauthorizedListeners.delete(listener); }

async function parseError(response: Response): Promise<ApiError> {
  let body: { error?: { code?: string; message?: string; request_id?: string; details?: unknown } } = {};
  try { body = await response.json(); } catch { /* non-JSON error body */ }
  const e = body.error ?? {};
  return new ApiError(response.status, e.code ?? `http_${response.status}`, e.message ?? `Request failed (${response.status}).`, e.request_id, e.details);
}

export interface RequestOptions { method?: string; body?: unknown; headers?: Record<string, string>; query?: Record<string, string | number | boolean | undefined | null>; raw?: boolean; }

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? (options.body === undefined ? "GET" : "POST");
  const url = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(options.query ?? {})) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  const headers: Record<string, string> = { Accept: options.raw ? "*/*" : "application/json", ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") { const token = csrf(); if (token) headers["X-CSRF-Token"] = token; }
  const response = await fetch(url, { method, headers, credentials: "include", body: options.body === undefined ? undefined : JSON.stringify(options.body) });
  if (!response.ok) {
    const error = await parseError(response);
    if (response.status === 401 && !path.includes("/auth/login")) unauthorizedListeners.forEach((l) => l());
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (options.raw ? await response.text() : await response.json()) as T;
}

export const api = {
  get: <T>(path: string, query?: RequestOptions["query"]) => request<T>(path, { query }),
  text: (path: string) => request<string>(path, { raw: true }),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) => request<T>(path, { method: "POST", body: body ?? {}, headers }),
};

/** Idempotency keys for mutating payment calls (8-64 chars, [A-Za-z0-9_-]). */
export function idempotencyKey(prefix: string): string {
  const random = crypto.getRandomValues(new Uint8Array(12));
  return `${prefix}-${Array.from(random, (b) => b.toString(16).padStart(2, "0")).join("")}`.slice(0, 64);
}

export const projectPath = (projectId: string, tail = "") => `/api/v1/projects/${encodeURIComponent(projectId)}${tail}`;
