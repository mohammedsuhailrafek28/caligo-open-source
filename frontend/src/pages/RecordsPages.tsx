import { useState } from "react";
import { api, projectPath } from "../api/client";
import type { S } from "../api/types";
import { EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { AuditEntry } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, StatusLabel } from "../design/ui";
import { useProjectGet, useProjectId } from "./hooks";

type ReportKey = "evidence-report" | "unified-report";
const REPORTS: Array<{ key: ReportKey; title: string; blurb: string }> = [
  { key: "evidence-report", title: "Evidence report", blurb: "Scope, delivery evidence and buyer decisions, with hashes." },
  { key: "unified-report", title: "Unified report", blurb: "Everything above plus payment assurance, observations and diagnosis." },
];

function download(filename: string, mime: string, body: string): void {
  const url = URL.createObjectURL(new Blob([body], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function ReportCard({ report }: { report: (typeof REPORTS)[number] }) {
  const pid = useProjectId();
  const [busy, setBusy] = useState<"json" | "md" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const run = async (format: "json" | "md", save: boolean) => {
    setBusy(format);
    setError(null);
    try {
      if (format === "json") {
        const body = JSON.stringify(await api.get<unknown>(projectPath(pid, `/${report.key}`)), null, 2);
        if (save) download(`${report.key}-${pid}.json`, "application/json", body);
        else setPreview(body);
      } else {
        const body = await api.text(projectPath(pid, `/${report.key}/markdown`));
        if (save) download(`${report.key}-${pid}.md`, "text/markdown", body);
        else setPreview(body);
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <article className="border border-stone bg-white p-5">
      <h3 className="font-display text-xl">{report.title}</h3>
      <p className="mt-1 text-sm text-ink-soft">{report.blurb}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="secondary" busy={busy === "json"} onClick={() => void run("json", false)}>View JSON</Button>
        <Button variant="secondary" busy={busy === "md"} onClick={() => void run("md", false)}>View Markdown</Button>
        <Button variant="quiet" onClick={() => void run("json", true)}>Download JSON</Button>
        <Button variant="quiet" onClick={() => void run("md", true)}>Download Markdown</Button>
      </div>
      {error !== null && <Notice tone="error">{errorMessage(error)}</Notice>}
      {preview !== null && <pre tabIndex={0} aria-label={`${report.title} preview`} className="mono mt-3 max-h-96 overflow-auto whitespace-pre-wrap border border-stone bg-parchment p-3">{preview}</pre>}
    </article>
  );
}

export function ReportsPage() {
  return (
    <>
      <PageHeader eyebrow="Recovery & records" title="Evidence reports">Exportable records, generated from stored evidence. Reports describe what was recorded; they do not rule on who is right.</PageHeader>
      <div className="space-y-4">{REPORTS.map((r) => <ReportCard key={r.key} report={r} />)}</div>
    </>
  );
}

export function AuditPage() {
  const events = useProjectGet<S["AuditEventView"][]>("/audit-events");
  return (
    <>
      <PageHeader eyebrow="Recovery & records" title="Audit trail">Append-only. Nothing here can be edited or removed.</PageHeader>
      <SectionHeader title="Events" />
      {events.isLoading && <LoadingState />}
      {events.error && <ErrorState error={events.error} onRetry={() => void events.refetch()} />}
      {events.data?.length === 0 && <EmptyState title="No events yet" />}
      <ul>{events.data?.map((e) => (
        <AuditEntry key={e.id} action={e.event_type} at={e.created_at} actor={<StatusLabel tone="neutral">{e.actor_role.replace(/_/g, " ").toLowerCase()}</StatusLabel>} detail={JSON.stringify(e.detail)} />
      ))}</ul>
    </>
  );
}
