import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, projectPath } from "../api/client";

export function useProjectId(): string {
  const { pid } = useParams();
  if (!pid) throw new Error("This page must be rendered under /app/p/:pid");
  return pid;
}

export const pkey = (pid: string, tail: string): QueryKey => ["project", pid, tail];

/** GET a project-scoped resource. */
export function useProjectGet<T>(tail: string, query?: Record<string, string | number | boolean | undefined>, enabled = true) {
  const pid = useProjectId();
  return useQuery({ queryKey: [...pkey(pid, tail), query ?? null], queryFn: () => api.get<T>(projectPath(pid, tail), query), enabled });
}

/** POST to a project-scoped path, then refresh every cached read for the project. */
export function useProjectPost<TBody, TResult = unknown>(tail: string | ((body: TBody) => string), headers?: (body: TBody) => Record<string, string>) {
  const pid = useProjectId();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: TBody) => api.post<TResult>(projectPath(pid, typeof tail === "function" ? tail(body) : tail), body, headers?.(body)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["project", pid] }),
  });
}
