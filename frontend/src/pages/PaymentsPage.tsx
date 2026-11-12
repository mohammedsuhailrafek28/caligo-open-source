import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, idempotencyKey, projectPath } from "../api/client";
import type { Payment, S } from "../api/types";
import { ConfirmationDialog, EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { EvidenceTimeline } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, InputField, SelectField, StatusLabel, TextAreaField, type Tone } from "../design/ui";
import { formatDateTime, formatMoney, humanize, parseMinor, providerLabel } from "../lib/format";
import { useProjectGet, useProjectId, useProjectPost } from "./hooks";
import { InstructionPanel } from "./InstructionPanel";

const statusTone: Record<S["PaymentProviderStatus"], Tone> = {
  UNVERIFIED: "advisory", PENDING: "advisory", PARTIALLY_PAID: "attention", PAID: "verified",
  PARTIALLY_REFUNDED: "attention", REFUNDED: "attention", CANCELLED: "neutral", NEEDS_REVIEW: "blocked",
};
const UNCERTAIN: Array<S["OperationState"]> = ["CREATE_UNCERTAIN", "SEND_UNCERTAIN"];

/** Never implies payment: only provider evidence of PAID reads as paid. */
function statusWords(p: Payment): string {
  if (p.provider_status === "UNVERIFIED") return "Not verified as paid";
  if (p.provider_status === "PENDING") return "Awaiting payment";
  return humanize(p.provider_status);
}

function InvoiceForm() {
  const create = useProjectPost<S["InvoiceRequest"]>("/invoices", () => ({ "Idempotency-Key": idempotencyKey("inv") }));
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [email, setEmail] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const minor = parseMinor(amount, currency);
    if (minor === null) return setErr("Enter a valid positive amount, e.g. 1200.50.");
    setErr(null);
    create.mutate({ amount_minor: minor, currency, recipient_email: email });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <InputField label="Amount" required inputMode="decimal" error={err} hint="Must match the recorded payment instruction." value={amount} onChange={(e) => setAmount(e.target.value)} />
      <InputField label="Currency" required maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
      <InputField label="Recipient email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
      {create.error && <Notice tone="error">{errorMessage(create.error)}</Notice>}
      {create.isSuccess && <Notice tone="success">Draft invoice created. It is a draft: nothing has been sent and nothing is paid.</Notice>}
      <Button type="submit" busy={create.isPending}>Create draft invoice</Button>
    </form>
  );
}

function PaymentEvents({ id }: { id: string }) {
  const pid = useProjectId();
  const events = useQuery({ queryKey: ["project", pid, "events", id], queryFn: () => api.get<S["PaymentEventView"][]>(projectPath(pid, `/payments/${encodeURIComponent(id)}/events`)) });
  if (events.isLoading) return <LoadingState />;
  if (events.error) return <ErrorState error={events.error} />;
  return <EvidenceTimeline items={(events.data ?? []).map((e) => ({ id: e.id, at: e.created_at, title: humanize(e.event_type), detail: e.note ?? (e.to_status ? `${e.from_status ? humanize(e.from_status) + " → " : ""}${humanize(e.to_status)}` : undefined) }))} />;
}

function ReviewResolver({ payment }: { payment: Payment }) {
  const resolve = useProjectPost<S["ReviewResolutionRequest"]>(() => `/payments/${encodeURIComponent(payment.id ?? "")}/resolve-review`);
  const [resolution, setResolution] = useState<S["ReviewResolution"]>("ACCEPT_PROVIDER_EVIDENCE");
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  return (
    <div className="mt-3 max-w-xl space-y-3 border border-stone bg-paper p-4">
      <p className="text-sm font-medium">Needs review. This stays blocked until a person resolves it; the resolution is audited.</p>
      <SelectField label="Resolution" value={resolution} onChange={(e) => setResolution(e.target.value as S["ReviewResolution"])}>
        <option value="ACCEPT_PROVIDER_EVIDENCE">Accept the provider evidence</option>
        <option value="ABANDON_UNCREATED_INVOICE">Abandon: no provider invoice exists</option>
      </SelectField>
      <TextAreaField label="Note" required value={note} onChange={(e) => setNote(e.target.value)} />
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />I confirm no provider invoice exists for this attempt (needed only to abandon).</label>
      {resolve.error && <Notice tone="error">{errorMessage(resolve.error)}</Notice>}
      <Button busy={resolve.isPending} disabled={!note.trim()} onClick={() => resolve.mutate({ resolution, note, confirm_no_provider_invoice: confirmed })}>Resolve review</Button>
    </div>
  );
}

function PaymentRow({ p }: { p: Payment }) {
  const send = useProjectPost<S["SendInvoiceRequest"]>(() => `/payments/${encodeURIComponent(p.id ?? "")}/send`);
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [open, setOpen] = useState(false);
  const canSend = p.operation_state === "READY" && p.provider_status === "UNVERIFIED";
  return (
    <li className="border-b border-stone/70 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="font-medium">{formatMoney(p.amount_minor, p.currency)}</p>
          <p className="mono text-ink-soft">{p.paypal_invoice_id ?? "no provider invoice id"}</p>
          <p className="text-xs text-ink-soft">Source: {providerLabel(p.provider).text}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusLabel tone={statusTone[p.provider_status]}>{statusWords(p)}</StatusLabel>
          <StatusLabel tone={UNCERTAIN.includes(p.operation_state) ? "attention" : "neutral"}>{humanize(p.operation_state)}</StatusLabel>
        </div>
      </div>
      <p className="mt-1 text-xs text-ink-soft">Paid {formatMoney(p.amount_paid_minor, p.currency)} · refunded {formatMoney(p.amount_refunded_minor, p.currency)} · checked {formatDateTime(p.checked_at)}</p>
      {p.verification_note && <p className="mt-1 text-sm text-ink-soft">{p.verification_note}</p>}
      {UNCERTAIN.includes(p.operation_state) && <Notice tone="info">The outcome is uncertain. Caligo records it as uncertain and never retries automatically; sync to check the provider.</Notice>}
      <div className="mt-2 flex flex-wrap gap-2">
        {canSend && <Button variant="secondary" onClick={() => setConfirming(true)}>Send invoice…</Button>}
        {p.id && <Button variant="quiet" onClick={() => setOpen((v) => !v)} aria-expanded={open}>{open ? "Hide history" : "Show history"}</Button>}
      </div>
      {p.provider_status === "NEEDS_REVIEW" && <ReviewResolver payment={p} />}
      {open && p.id && <div className="mt-3"><PaymentEvents id={p.id} /></div>}
      <ConfirmationDialog open={confirming} title="Send this invoice?" confirmLabel="Send invoice" busy={send.isPending}
        onCancel={() => { setConfirming(false); setTyped(""); }}
        onConfirm={() => send.mutate({ confirm_recipient_email: typed }, { onSuccess: () => { setConfirming(false); setTyped(""); } })}>
        <p>This sends the invoice to the recipient and cannot be recalled by Caligo. Type the recipient email to confirm.</p>
        <InputField label="Recipient email" value={typed} onChange={(e) => setTyped(e.target.value)} />
        {send.error && <Notice tone="error">{errorMessage(send.error)}</Notice>}
      </ConfirmationDialog>
    </li>
  );
}

export function PaymentsPage() {
  const payments = useProjectGet<Payment[]>("/payments");
  const sync = useProjectPost<Record<string, never>>("/payments/sync");
  return (
    <>
      <PageHeader eyebrow="Payment assurance" title="Payments" actions={<Button variant="secondary" busy={sync.isPending} onClick={() => sync.mutate({})}>Sync with provider</Button>}>
        Payment status comes from provider evidence, never from a claim. A draft invoice is never shown as paid.
      </PageHeader>
      {sync.error && <Notice tone="error">{errorMessage(sync.error)}</Notice>}
      <InstructionPanel />
      <SectionHeader title="Invoices and payments" />
      {payments.isLoading && <LoadingState />}
      {payments.error && <ErrorState error={payments.error} />}
      {payments.data?.length === 0 && <EmptyState title="No invoices yet">Record a payment instruction, then create a draft invoice.</EmptyState>}
      <ul>{payments.data?.map((p) => <PaymentRow key={p.id ?? p.paypal_invoice_id ?? p.created_at ?? ""} p={p} />)}</ul>
      <SectionHeader title="Create a draft invoice" />
      <InvoiceForm />
    </>
  );
}
