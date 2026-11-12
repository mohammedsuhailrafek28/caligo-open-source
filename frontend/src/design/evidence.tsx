import { Fingerprint } from "lucide-react";
import type { ReactNode } from "react";
import { formatDateTime, humanize, shortHash } from "../lib/format";
import { cx, StatusLabel, type Tone } from "./ui";

export interface Column<T> { header: string; cell: (row: T) => ReactNode; className?: string }

/** Plain semantic table: captioned, scoped headers, horizontally scrollable on narrow screens. */
export function RecordTable<T>({ caption, columns, rows, rowKey }: { caption: string; columns: Array<Column<T>>; rows: T[]; rowKey: (row: T) => string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-y border-stone">
            {columns.map((c) => <th key={c.header} scope="col" className={cx("eyebrow py-2 pr-4 font-medium", c.className)}>{c.header}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={rowKey(r)} className="border-b border-stone/70 align-top">
              {columns.map((c) => <td key={c.header} className={cx("py-3 pr-4", c.className)}>{c.cell(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export type Provenance = "SELLER_ASSERTED" | "SERVER_VERIFIED" | "BUYER_VERIFIED" | "PROVIDER_REPORTED" | "ADVISORY" | "SIMULATED" | "DECLARED_ONLY";
const provenanceCopy: Record<Provenance, { label: string; tone: Tone; note: string }> = {
  SELLER_ASSERTED: { label: "Seller-asserted", tone: "advisory", note: "Entered by the seller. Not independently verified." },
  SERVER_VERIFIED: { label: "Server-verified hash", tone: "verified", note: "Content hash computed by the server." },
  BUYER_VERIFIED: { label: "Buyer-verified mailbox", tone: "verified", note: "Made by whoever controlled the verified mailbox; not legal identity." },
  PROVIDER_REPORTED: { label: "Provider-reported", tone: "neutral", note: "Reported by the payment provider (Sandbox/mock)." },
  ADVISORY: { label: "Advisory", tone: "advisory", note: "Deterministic analysis for human review. It never authorizes anything." },
  SIMULATED: { label: "Simulated store", tone: "advisory", note: "Applies to the simulated merchant store only." },
  DECLARED_ONLY: { label: "Declared only", tone: "attention", note: "Declared by the seller; content could not be verified." },
};
export function ProvenanceIndicator({ kind, showNote }: { kind: Provenance; showNote?: boolean }) {
  const p = provenanceCopy[kind];
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <StatusLabel tone={p.tone}>{p.label}</StatusLabel>
      {showNote && <span className="text-xs text-ink-soft">{p.note}</span>}
    </span>
  );
}

export function RiskSignal({ severity, title, children }: { severity: "info" | "review" | "high"; title: string; children?: ReactNode }) {
  const tone: Tone = severity === "high" ? "blocked" : severity === "review" ? "attention" : "advisory";
  const word = severity === "high" ? "High concern" : severity === "review" ? "Review" : "Note";
  return (
    <li className="flex gap-3 border-b border-stone/70 py-3">
      <StatusLabel tone={tone} className="h-fit">{word}</StatusLabel>
      <div><p className="text-sm font-medium">{title}</p>{children && <div className="mt-1 text-sm text-ink-soft">{children}</div>}</div>
    </li>
  );
}

export function EvidenceCard({ title, provenance, hash, meta, children }: { title: string; provenance?: Provenance; hash?: string | null; meta?: ReactNode; children?: ReactNode }) {
  return (
    <article className="border border-stone bg-white p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="font-display text-xl">{title}</h3>
        {provenance && <ProvenanceIndicator kind={provenance} />}
      </div>
      {meta && <div className="mt-1 text-xs text-ink-soft">{meta}</div>}
      {children && <div className="mt-3 text-sm">{children}</div>}
      {hash && <p className="mono mt-3 flex items-center gap-1.5 text-ink-soft"><Fingerprint aria-hidden className="h-4 w-4" /><span title={hash}>sha256 {shortHash(hash, 16)}</span></p>}
    </article>
  );
}

export interface TimelineItem { id: string; at: string; title: string; detail?: ReactNode; tone?: Tone }
export function EvidenceTimeline({ items }: { items: TimelineItem[] }) {
  return (
    <ol className="border-l border-stone pl-5">
      {items.map((i) => (
        <li key={i.id} className="relative pb-5">
          <span aria-hidden className="absolute -left-[1.6rem] top-1.5 h-2 w-2 rounded-full bg-terracotta" />
          <p className="text-xs text-ink-soft"><time dateTime={i.at}>{formatDateTime(i.at)}</time></p>
          <p className="text-sm font-medium">{i.title}</p>
          {i.detail && <div className="text-sm text-ink-soft">{i.detail}</div>}
        </li>
      ))}
    </ol>
  );
}

export function AuditEntry({ action, at, actor, detail }: { action: string; at: string; actor?: ReactNode; detail?: ReactNode }) {
  return (
    <li className="border-b border-stone/70 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-medium">{humanize(action)}</span>
        <time dateTime={at} className="text-xs text-ink-soft">{formatDateTime(at)}</time>
      </div>
      {actor && <p className="flex items-center gap-1 text-xs text-ink-soft">By {actor}</p>}
      {detail && <div className="mono mt-1 break-all text-ink-soft">{detail}</div>}
    </li>
  );
}
