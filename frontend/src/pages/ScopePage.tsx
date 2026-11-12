import { useState, type FormEvent } from "react";
import type { S } from "../api/types";
import { EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { EvidenceCard } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, InputField, SelectField, StatusLabel, TextAreaField, type Tone } from "../design/ui";
import { formatDateTime, humanize } from "../lib/format";
import { useProjectGet, useProjectPost } from "./hooks";

const SCOPE_KINDS: Array<S["ScopeKind"]> = ["ORIGINAL", "CHANGE_REQUEST", "APPROVAL", "DELIVERY_NOTE"];
const changeTone: Record<S["ChangeStatus"], Tone> = { PROPOSED: "attention", APPROVED: "verified", REJECTED: "blocked", SUPERSEDED: "neutral" };

/** Backend key pattern: ^[A-Z][A-Z0-9_-]{0,23}$ */
export function toRequirementKey(raw: string): string {
  const cleaned = raw.trim().toUpperCase().replace(/[^A-Z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const key = /^[A-Z]/.test(cleaned) ? cleaned : `R-${cleaned}`;
  return key.slice(0, 24).replace(/-+$/, "") || "REQ";
}

/** One requirement per line as "key: title - description" (description optional). */
export function parseRequirements(text: string): S["RequirementDraft"][] {
  return text.split("\n").map((l) => l.trim()).filter(Boolean).map((line) => {
    const [head, ...rest] = line.split(" - ");
    const colon = head.indexOf(":");
    return { key: toRequirementKey(colon > 0 ? head.slice(0, colon) : head), title: (colon > 0 ? head.slice(colon + 1) : head).trim(), description: rest.join(" - ").trim() };
  });
}

function RecordForm() {
  const add = useProjectPost<S["ScopeRecordCreate"]>("/scope-records");
  const [content, setContent] = useState("");
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState<S["ScopeKind"]>("ORIGINAL");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate({ content, kind, source_label: label }, { onSuccess: () => { setContent(""); setLabel(""); } });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <SelectField label="Kind" value={kind} onChange={(e) => setKind(e.target.value as S["ScopeKind"])}>
        {SCOPE_KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}
      </SelectField>
      <InputField label="Source label" required placeholder="e.g. Statement of work v1" value={label} onChange={(e) => setLabel(e.target.value)} />
      <TextAreaField label="Content" required hint="Stored verbatim and hashed with SHA-256. Records are append-only." value={content} onChange={(e) => setContent(e.target.value)} />
      {add.error && <Notice tone="error">{errorMessage(add.error)}</Notice>}
      <Button type="submit" busy={add.isPending}>Record scope text</Button>
    </form>
  );
}

function BaselineForm({ records }: { records: S["ScopeRecordView"][] }) {
  const freeze = useProjectPost<S["BaselineCreate"]>("/baselines");
  const [source, setSource] = useState("");
  const [text, setText] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    freeze.mutate({ source_scope_record_id: source, requirements: parseRequirements(text) }, { onSuccess: () => setText("") });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <SelectField label="Source scope record" required value={source} onChange={(e) => setSource(e.target.value)}>
        <option value="">Choose a record…</option>
        {records.map((r) => <option key={r.id} value={r.id}>{humanize(r.kind)} · {r.source_label}</option>)}
      </SelectField>
      <TextAreaField label="Requirements" required hint="One per line: KEY: title - description. Keys are upper-cased automatically (A-Z, 0-9, - and _)." value={text} onChange={(e) => setText(e.target.value)} />
      {freeze.error && <Notice tone="error">{errorMessage(freeze.error)}</Notice>}
      <Button type="submit" busy={freeze.isPending} disabled={!source}>Freeze baseline</Button>
    </form>
  );
}

function ChangeForm() {
  const propose = useProjectPost<S["ScopeChangeCreate"]>("/scope-changes");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [text, setText] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    propose.mutate({ title, description, requirements: parseRequirements(text) }, { onSuccess: () => { setTitle(""); setDescription(""); setText(""); } });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <InputField label="Change title" required value={title} onChange={(e) => setTitle(e.target.value)} />
      <TextAreaField label="What changes and why" required value={description} onChange={(e) => setDescription(e.target.value)} />
      <TextAreaField label="Added requirements" required hint="Only the new requirements this change introduces, one per line: KEY: title - description. Keys must not already exist." value={text} onChange={(e) => setText(e.target.value)} />
      {propose.error && <Notice tone="error">{errorMessage(propose.error)}</Notice>}
      <Button type="submit" busy={propose.isPending}>Propose change</Button>
    </form>
  );
}

export function ScopePage() {
  const records = useProjectGet<S["ScopeRecordView"][]>("/scope-records");
  const baselines = useProjectGet<S["BaselineView"][]>("/baselines");
  const changes = useProjectGet<S["ScopeChangeView"][]>("/scope-changes");
  return (
    <>
      <PageHeader eyebrow="Agreement & delivery" title="Scope">What was agreed, frozen into versioned baselines. A change is only a proposal until the buyer decides on it.</PageHeader>

      <SectionHeader title="Scope records" />
      {records.isLoading && <LoadingState />}
      {records.error && <ErrorState error={records.error} />}
      {records.data?.length === 0 && <EmptyState title="No scope text recorded">Paste the agreement, a change request or an approval as it was written.</EmptyState>}
      <div className="space-y-3">
        {records.data?.map((r) => (
          <EvidenceCard key={r.id} title={r.source_label} provenance="SELLER_ASSERTED" hash={r.source_sha256} meta={<>{humanize(r.kind)} · {formatDateTime(r.created_at)}</>}>
            <p className="line-clamp-4 whitespace-pre-wrap">{r.content}</p>
          </EvidenceCard>
        ))}
      </div>
      <SectionHeader title="Add scope text" />
      <RecordForm />

      <SectionHeader title="Baselines" />
      {baselines.error && <ErrorState error={baselines.error} />}
      {baselines.data?.length === 0 && <EmptyState title="No baseline yet">Freeze one from a recorded scope text.</EmptyState>}
      <div className="space-y-3">
        {baselines.data?.map((b) => (
          <EvidenceCard key={b.id} title={`Version ${b.version} · ${humanize(b.kind)}`} hash={b.content_sha256} meta={formatDateTime(b.created_at)}>
            <ul className="list-disc pl-5">{b.requirements?.map((q) => <li key={q.id}><span className="font-medium">{q.title}</span>{q.description && <span className="text-ink-soft"> — {q.description}</span>}</li>)}</ul>
          </EvidenceCard>
        ))}
      </div>
      <SectionHeader title="Freeze a baseline" />
      <BaselineForm records={records.data ?? []} />

      <SectionHeader title="Scope changes">Approval comes only from the buyer, through the delivery evidence page. Seller-side approval is not possible.</SectionHeader>
      {changes.error && <ErrorState error={changes.error} />}
      {changes.data?.length === 0 && <EmptyState title="No changes proposed" />}
      <div className="space-y-3">
        {changes.data?.map((c) => (
          <EvidenceCard key={c.id} title={c.title} meta={formatDateTime(c.created_at)}>
            <div className="mb-2"><StatusLabel tone={changeTone[c.status]}>{humanize(c.status)}</StatusLabel></div>
            <p>{c.description}</p>
          </EvidenceCard>
        ))}
      </div>
      <SectionHeader title="Propose a change" />
      <ChangeForm />
    </>
  );
}
