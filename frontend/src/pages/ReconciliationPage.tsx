import { useState, type FormEvent } from "react";
import type { S } from "../api/types";
import { EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { ProvenanceIndicator, RecordTable, RiskSignal } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, InputField, SelectField, StatusLabel, TextAreaField } from "../design/ui";
import { formatDateTime, formatMoney, humanize, parseMinor, shortHash } from "../lib/format";
import { useProjectGet, useProjectPost } from "./hooks";

const SEVERITY = { INFO: "info", WARNING: "review", CONFLICT: "high" } as const;
const KINDS: Array<S["ObservationKind"]> = ["PAYMENT", "REFUND", "INVOICE_STATE"];

function ClaimForm({ invoiceNumbers }: { invoiceNumbers: string[] }) {
  const record = useProjectPost<S["SellerObservationCreate"]>("/payment-observations");
  const [kind, setKind] = useState<S["ObservationKind"]>("PAYMENT");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [invoice, setInvoice] = useState("");
  const [tx, setTx] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const minor = parseMinor(amount, currency);
    if (minor === null) return setErr("Enter a valid positive amount, e.g. 1200.50.");
    setErr(null);
    record.mutate({ kind, amount_minor: minor, currency, invoice_number: invoice, transaction_id: tx || null, note: note || null });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <SelectField label="Kind" value={kind} onChange={(e) => setKind(e.target.value as S["ObservationKind"])}>{KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}</SelectField>
      <InputField label="Amount" required inputMode="decimal" error={err} value={amount} onChange={(e) => setAmount(e.target.value)} />
      <InputField label="Currency" required maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
      <SelectField label="Invoice" required value={invoice} onChange={(e) => setInvoice(e.target.value)} hint="A claim must refer to an invoice created in this project.">
        <option value="">Choose an invoice…</option>
        {invoiceNumbers.map((n) => <option key={n} value={n}>{n}</option>)}
      </SelectField>
      <InputField label="Transaction id" hint="Optional. Without it a claim is weaker evidence." value={tx} onChange={(e) => setTx(e.target.value)} />
      <InputField label="Note" value={note} onChange={(e) => setNote(e.target.value)} />
      {record.error && <Notice tone="error">{errorMessage(record.error)}</Notice>}
      {record.isSuccess && <Notice tone="success">Recorded as an unverified claim. It never changes the payment status by itself.</Notice>}
      <Button type="submit" busy={record.isPending}>Record claim</Button>
    </form>
  );
}

function LedgerImport() {
  const imp = useProjectPost<S["MerchantLedgerImportRequest"], S["MerchantLedgerImportView"]>("/merchant-ledger/imports");
  const [csv, setCsv] = useState("");
  return (
    <div className="max-w-2xl space-y-3">
      <TextAreaField label="Merchant ledger CSV" mono hint="Rows are recorded as unverified merchant claims. Re-importing the same file is a no-op." value={csv} onChange={(e) => setCsv(e.target.value)} />
      {imp.error && <Notice tone="error">{errorMessage(imp.error)}</Notice>}
      {imp.data && <Notice tone="success">{imp.data.replay ? "This file was already imported. " : ""}{imp.data.recorded_count} recorded, {imp.data.duplicate_count} duplicates, {imp.data.row_count} rows.</Notice>}
      <Button busy={imp.isPending} disabled={!csv.trim()} onClick={() => imp.mutate({ csv })}>Import ledger</Button>
    </div>
  );
}

export function ReconciliationPage() {
  const comparison = useProjectGet<S["ObservationComparison"]>("/observation-comparison");
  const observations = useProjectGet<S["PaymentObservationView"][]>("/payment-observations");
  const cases = useProjectGet<S["RecoveryCaseList"]>("/recovery-cases");
  return (
    <>
      <PageHeader eyebrow="Payment assurance" title="Reconciliation">Provider evidence compared with the claims people have made. Claims are never proof of payment.</PageHeader>

      <SectionHeader title="Comparison">{comparison.data?.authority}</SectionHeader>
      {comparison.isLoading && <LoadingState />}
      {comparison.error && <ErrorState error={comparison.error} />}
      {comparison.data?.payments.length === 0 && <EmptyState title="Nothing to compare yet" />}
      {comparison.data?.payments.map((p) => (
        <section key={p.payment_id} className="mb-6">
          <h3 className="font-display text-xl">{p.invoice_number ?? "Payment"} <StatusLabel tone="advisory">Provider status: {humanize(p.authoritative_status)}</StatusLabel></h3>
          <p className="text-sm text-ink-soft">Claimed paid: {p.claimed_paid_minor} minor units · provider evidence {p.provider_evidence_complete ? "complete" : "incomplete"}</p>
          <ul>{p.findings.map((f, i) => (
            <RiskSignal key={`${f.code}-${i}`} severity={SEVERITY[f.severity]} title={humanize(f.code)}>
              {f.summary}
              {f.citations.length > 0 && <span className="mt-1 block text-xs">Cites: {f.citations.map((c) => `${humanize(c.source)} ${shortHash(c.payload_sha256, 8)}`).join("; ")}</span>}
            </RiskSignal>
          ))}</ul>
        </section>
      ))}

      <SectionHeader title="Recorded observations" />
      {observations.error && <ErrorState error={observations.error} />}
      {observations.data && observations.data.length > 0 && (
        <RecordTable caption="Payment observations" rows={observations.data} rowKey={(o) => o.id} columns={[
          { header: "Kind", cell: (o) => humanize(o.kind) },
          { header: "Amount", cell: (o) => formatMoney(o.amount_minor, o.currency) },
          { header: "Confidence", cell: (o) => <ProvenanceIndicator kind={o.confidence === "PROVIDER_EVIDENCE" ? "PROVIDER_REPORTED" : o.confidence === "UNVERIFIED_CLAIM" ? "SELLER_ASSERTED" : "ADVISORY"} /> },
          { header: "Observed", cell: (o) => formatDateTime(o.observed_at) },
        ]} />
      )}
      <SectionHeader title="Record a claim" />
      <ClaimForm invoiceNumbers={(comparison.data?.payments ?? []).flatMap((p) => (p.invoice_number ? [p.invoice_number] : []))} />
      <SectionHeader title="Import a merchant ledger" />
      <LedgerImport />

      <SectionHeader title="Recovery cases">{cases.data?.authority}</SectionHeader>
      {cases.error && <ErrorState error={cases.error} />}
      {cases.data?.cases.length === 0 && <EmptyState title="No cases to review" />}
      <ul>{cases.data?.cases.map((c) => (
        <RiskSignal key={c.case_id} severity={SEVERITY[c.severity]} title={humanize(c.category)}>
          <p>{c.explanation}</p>
          <p className="mt-1">Next: {c.recommended_next_action}</p>
          <p className="mt-1 text-xs">Advisory only. Not permitted automatically: {c.prohibited_automated_actions.join("; ") || "—"}</p>
        </RiskSignal>
      ))}</ul>
    </>
  );
}
