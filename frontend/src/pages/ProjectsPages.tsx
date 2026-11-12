import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Project, S } from "../api/types";
import { EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { RecordTable } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, InputField, StatusLabel, type Tone } from "../design/ui";
import { formatDateTime, humanize } from "../lib/format";

export const projectsKey = ["projects"] as const;
export const useProjects = () => useQuery({ queryKey: projectsKey, queryFn: () => api.get<Project[]>("/api/v1/projects") });

const statusTone: Record<S["ProjectStatus"], Tone> = { DRAFT: "neutral", INVOICED: "advisory", PAID: "verified", DELIVERED: "verified", DISPUTED: "attention" };
export const ProjectStatusLabel = ({ status }: { status: S["ProjectStatus"] }) => <StatusLabel tone={statusTone[status]}>{humanize(status)}</StatusLabel>;

export function ProjectsPage() {
  const projects = useProjects();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const create = useMutation({
    mutationFn: (t: string) => api.post<Project>("/api/v1/projects", { title: t }),
    onSuccess: async (p) => {
      await queryClient.invalidateQueries({ queryKey: projectsKey });
      navigate(`/app/p/${p.id}/scope`);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (title.trim()) create.mutate(title.trim());
  };
  return (
    <>
      <PageHeader eyebrow="Workspace" title="Projects">Each project holds one agreement: its scope, delivery evidence, payments and records.</PageHeader>
      {projects.isLoading && <LoadingState />}
      {projects.error && <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />}
      {projects.data && projects.data.length === 0 && <EmptyState title="No projects yet">Create your first project below. Nothing is invented for you: every record here is one you or a verified buyer created.</EmptyState>}
      {projects.data && projects.data.length > 0 && (
        <RecordTable
          caption="Projects"
          rows={projects.data}
          rowKey={(p) => p.id}
          columns={[
            { header: "Project", cell: (p) => <Link className="font-medium text-terracotta-deep underline" to={`/app/p/${p.id}/scope`}>{p.title}</Link> },
            { header: "Financial status", cell: (p) => <ProjectStatusLabel status={p.status} /> },
            { header: "Created", cell: (p) => formatDateTime(p.created_at) },
          ]}
        />
      )}
      <SectionHeader title="New project" />
      <form onSubmit={submit} className="max-w-md space-y-4">
        <InputField label="Project title" required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} />
        {create.error && <Notice tone="error">{errorMessage(create.error)}</Notice>}
        <Button type="submit" busy={create.isPending}>Create project</Button>
      </form>
    </>
  );
}

export function OverviewPage() {
  const projects = useProjects();
  const caps = useQuery({ queryKey: ["capabilities"], queryFn: () => api.get<Record<string, unknown>>("/api/v1/capabilities") });
  const recent = projects.data?.slice(0, 5) ?? [];
  return (
    <>
      <PageHeader eyebrow="Workspace" title="Overview">What the agreement said, what was delivered, what was paid, and what the evidence supports.</PageHeader>
      <SectionHeader title="Recent projects" />
      {projects.isLoading && <LoadingState />}
      {projects.error && <ErrorState error={projects.error} />}
      {projects.data && recent.length === 0 && <EmptyState title="Start with a project" action={<Link to="/app/projects"><Button>Create a project</Button></Link>} />}
      <ul>
        {recent.map((p) => (
          <li key={p.id} className="flex items-center justify-between border-b border-stone/70 py-3">
            <Link className="text-terracotta-deep underline" to={`/app/p/${p.id}/scope`}>{p.title}</Link>
            <ProjectStatusLabel status={p.status} />
          </li>
        ))}
      </ul>
      <SectionHeader title="Operating mode">What this deployment can and cannot do, as reported by the backend.</SectionHeader>
      {caps.isLoading && <LoadingState />}
      {caps.error && <ErrorState error={caps.error} />}
      {caps.data && <pre className="mono overflow-x-auto border border-stone bg-white p-4">{JSON.stringify(caps.data, null, 2)}</pre>}
    </>
  );
}

export { ApiError };
