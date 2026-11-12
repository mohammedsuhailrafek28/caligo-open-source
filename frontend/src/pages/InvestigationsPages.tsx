import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { api, projectPath } from "../api/client";
import type { Payment, S } from "../api/types";
import { ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { ProvenanceIndicator, RecordTable } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, SelectField, StatusLabel, TextAreaField } from "../design/ui";
import { humanize } from "../lib/format";
import { useProjectGet, useProjectId } from "./hooks";

export type DiagnosisKind = S["DiagnosisResponse"]["kind"];
export const KINDS: DiagnosisKind[] = ["DUPLICATE_ORDERS", "PAID_NOT_APPLIED", "STALE_PAID_AFTER_REFUND", "FALSE_HEALTHY"];
const SIM_NOTE = "Simulated merchant store. Nothing here touches a real store, and the findings are advisory.";

/** Backend returns free-form analysis objects; show them as labelled key/value rows rather than guessing a shape. */
export function KeyValues({ data }: { data: Record<string, unknown> }) {
  const rows = Object.entries(data);
  if (rows.length === 0) return <p className="text-sm text-ink-soft">No details.</p>;
  return (
    <dl className="grid grid-cols-[minmax(8rem,14rem)_1fr] gap-x-4 gap-y-2 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ink-soft">{humanize(k)}</dt>
          <dd className="mono break-words">{typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Evidence({ items }: { items: S["DiagnosisEvidenceView"][] }) {
  if (items.length === 0) return <p className="text-sm text-ink-soft">No evidence was cited.</p>;
  return (
    <RecordTable caption="Cited evidence" rows={items} rowKey={(e) => e.evidence_id} columns={[
      { header: "Evidence", cell: (e) => <span className="mono">{e.evidence_id}</span> },
      { header: "Source", cell: (e) => <StatusLabel tone={e.source === "untrusted_log" ? "attention" : "advisory"}>{humanize(e.source)}</StatusLabel> },
      { header: "Kind", cell: (e) => humanize(e.kind) },
      { header: "Status", cell: (e) => e.status },
      { header: "Provenance", cell: (e) => e.provenance_label },
    ]} />
  );
}

function Warnings({ items }: { items: string[] }) {
  return <>{items.map((w) => <Notice key={w} tone="info">Incomplete evidence: {w}</Notice>)}</>;
}

function usePayments() {
  return useProjectGet<Payment[]>("/payments");
}

export function DiagnosisPage() {
  const pid = useProjectId();
  const payments = usePayments();
  const [kind, setKind] = useState<DiagnosisKind>("DUPLICATE_ORDERS");
  const [paymentId, setPaymentId] = useState("");
  const [asked, setAsked] = useState<{ kind: DiagnosisKind; paymentId: string } | null>(null);
  const diagnosis = useQuery({
    queryKey: ["project", pid, "diagnosis", asked],
    enabled: asked !== null,
    queryFn: () => api.get<S["DiagnosisResponse"]>(projectPath(pid, "/state-recovery/diagnosis"), { kind: asked!.kind, payment_id: asked!.paymentId || undefined }),
  });
  const divergences = useProjectGet<S["DivergenceList"]>("/state-recovery/divergences");
  const d = diagnosis.data;
  return (
    <>
      <PageHeader eyebrow="Investigations" title="Diagnosis">{SIM_NOTE}</PageHeader>
      <SectionHeader title="Detected divergences">Where the simulated store and the provider evidence disagree.</SectionHeader>
      {divergences.isLoading && <LoadingState />}
      {divergences.error && <ErrorState error={divergences.error} />}
      {divergences.data?.divergences.length === 0 && <p className="text-sm text-ink-soft">No divergences detected.</p>}
      {divergences.data && divergences.data.divergences.length > 0 && (
        <RecordTable caption="Divergences" rows={divergences.data.divergences} rowKey={(x) => `${x.payment_id}-${x.code}`} columns={[
          { header: "Code", cell: (x) => humanize(x.code) },
          { header: "Payment", cell: (x) => <span className="mono">{x.payment_id}</span> },
          { header: "Suggested plan", cell: (x) => (x.suggested_plan_kind ? humanize(x.suggested_plan_kind) : "None") },
        ]} />
      )}
      <SectionHeader title="Run a diagnosis" />
      <form className="max-w-xl space-y-4" onSubmit={(e: FormEvent) => { e.preventDefault(); setAsked({ kind, paymentId }); }}>
        <SelectField label="Scenario" value={kind} onChange={(e) => setKind(e.target.value as DiagnosisKind)}>{KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}</SelectField>
        <SelectField label="Payment" value={paymentId} onChange={(e) => setPaymentId(e.target.value)}>
          <option value="">Any</option>
          {payments.data?.filter((p) => p.id).map((p) => <option key={p.id} value={p.id ?? ""}>{p.paypal_invoice_id ?? p.id}</option>)}
        </SelectField>
        <Button type="submit" busy={diagnosis.isFetching}>Diagnose</Button>
      </form>
      {diagnosis.error && <ErrorState error={diagnosis.error} />}
      {d && (
        <>
          <SectionHeader title="Result" />
          <p className="mb-3 flex flex-wrap gap-2"><ProvenanceIndicator kind="ADVISORY" showNote /></p>
          <p className="mb-3 text-sm text-ink-soft">{d.authority}</p>
          <p className="mb-3 text-sm">Mode: {humanize(d.mode)} · generative model: {d.llm_evaluation === "NOT_RUN" ? "not used (Caligo runs without any hosted AI)" : humanize(d.llm_evaluation)} · fingerprint <span className="mono">{d.evidence_fingerprint.slice(0, 16)}…</span></p>
          <Warnings items={d.incomplete_evidence_warnings} />
          <KeyValues data={d.diagnosis} />
          <SectionHeader title="Cited evidence" />
          <Evidence items={d.evidence} />
        </>
      )}
    </>
  );
}

const FALLBACK_COPY: Record<NonNullable<S["LogIntelligenceResponse"]["fallback_reason"]>, string> = {
  model_not_configured: "No language model is configured on this deployment.",
  dependency_missing: "The optional NLP dependency is not installed.",
  insufficient_memory: "There was not enough memory to load the model.",
  cache_missing: "The local model cache is missing.",
  manifest_missing: "The model manifest is missing.",
  cache_integrity_failed: "The model cache failed its integrity check.",
  version_mismatch: "The model version does not match the pinned version.",
  model_unavailable: "The model was unavailable.",
  inference_failed: "Model inference failed.",
};

export function LogIntelligencePage() {
  const pid = useProjectId();
  const payments = usePayments();
  const [kind, setKind] = useState<DiagnosisKind>("DUPLICATE_ORDERS");
  const [mode, setMode] = useState<"deterministic" | "stanza">("deterministic");
  const [paymentId, setPaymentId] = useState("");
  const [logs, setLogs] = useState("");
  const [result, setResult] = useState<S["LogIntelligenceResponse"] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const lines = logs.split("\n").map((t) => t.trim()).filter(Boolean).map((text) => ({ text }));
      setResult(await api.post<S["LogIntelligenceResponse"]>(projectPath(pid, "/state-recovery/log-intelligence"), {
        kind, requested_mode: mode, payment_id: paymentId || null, logs: lines, credentials_configured: true, heartbeat_ok: true,
      }));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHeader eyebrow="Investigations" title="Log intelligence">Paste merchant-side log lines. They are treated as untrusted text and read by deterministic rules, or optionally by the local Stanza NLP model. Stanza is not a generative AI and cannot act on anything. {SIM_NOTE}</PageHeader>
      <form onSubmit={(e) => void submit(e)} className="max-w-2xl space-y-4">
        <SelectField label="Scenario" value={kind} onChange={(e) => setKind(e.target.value as DiagnosisKind)}>{KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}</SelectField>
        <SelectField label="Analysis mode" value={mode} onChange={(e) => setMode(e.target.value as "deterministic" | "stanza")} hint="If Stanza is unavailable the server falls back to deterministic rules and says why.">
          <option value="deterministic">Deterministic rules</option>
          <option value="stanza">Stanza (local NLP, optional)</option>
        </SelectField>
        <SelectField label="Payment" value={paymentId} onChange={(e) => setPaymentId(e.target.value)}>
          <option value="">Any</option>
          {payments.data?.filter((p) => p.id).map((p) => <option key={p.id} value={p.id ?? ""}>{p.paypal_invoice_id ?? p.id}</option>)}
        </SelectField>
        <TextAreaField label="Log lines" required mono hint="One line per log entry. Nothing you paste is stored." value={logs} onChange={(e) => setLogs(e.target.value)} />
        {error !== null && <Notice tone="error">{errorMessage(error)}</Notice>}
        <Button type="submit" busy={busy}>Analyse logs</Button>
      </form>
      {result && <LogResult r={result} />}
    </>
  );
}

function LogResult({ r }: { r: S["LogIntelligenceResponse"] }) {
  const fellBack = r.requested_mode !== r.effective_mode;
  const note: ReactNode = fellBack && r.fallback_reason ? FALLBACK_COPY[r.fallback_reason] : null;
  return (
    <>
      <SectionHeader title="Result" />
      <p className="mb-2 text-sm">Requested <strong>{humanize(r.requested_mode)}</strong> · effective <strong>{humanize(r.effective_mode)}</strong> · {r.log_lines_analyzed} lines analysed</p>
      {fellBack && <Notice tone="info">Fell back to deterministic rules. {note}</Notice>}
      <p className="my-3"><ProvenanceIndicator kind="ADVISORY" showNote /></p>
      <p className="mb-3 text-sm text-ink-soft">{r.authority}</p>
      <Warnings items={r.incomplete_evidence_warnings} />
      <KeyValues data={r.report} />
      <SectionHeader title="Cited evidence">Log text is untrusted; its identifiers are assigned by the server.</SectionHeader>
      <Evidence items={r.evidence} />
    </>
  );
}

export function HealthPage() {
  const health = useProjectGet<S["IntegrationHealthResponse"]>("/state-recovery/integration-health");
  const h = health.data;
  return (
    <>
      <PageHeader eyebrow="Investigations" title="Integration health">Whether the simulated merchant integration looks healthy, assessed against provider evidence rather than self-reported status. {SIM_NOTE}</PageHeader>
      {health.isLoading && <LoadingState />}
      {health.error && <ErrorState error={health.error} onRetry={() => void health.refetch()} />}
      {h && (
        <>
          <p className="mb-3 text-sm text-ink-soft">{h.authoritative_policy}</p>
          <SectionHeader title="Summary" />
          <KeyValues data={h.summary} />
          <SectionHeader title="Assessment" />
          <KeyValues data={h.assessment} />
          <p className="mt-4 text-xs text-ink-soft">Inputs: {h.inputs_provenance}</p>
        </>
      )}
    </>
  );
}
