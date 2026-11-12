import { useState, type FormEvent } from "react";
import { idempotencyKey } from "../api/client";
import type { S } from "../api/types";
import { ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { EvidenceCard } from "../design/evidence";
import { SectionHeader } from "../design/layout";
import { Button, InputField, StatusLabel } from "../design/ui";
import { formatDateTime, formatMoney, parseMinor } from "../lib/format";
import { useProjectGet, useProjectPost } from "./hooks";

const HOLD_COPY: Record<S["HoldReason"], string> = {
  UNVERIFIED_INSTRUCTION_CHANGE: "A change to the payment instruction has not been verified by the buyer.",
  INSTRUCTION_INTEGRITY_FAILURE: "The recorded instruction chain failed its integrity check.",
  MERCHANT_MISMATCH: "The merchant on the provider side does not match the recorded instruction.",
  PROVIDER_MISMATCH: "The provider reports something different from the recorded instruction.",
  INSTRUCTION_REQUIRED: "No payment instruction has been recorded yet.",
};

function InstructionForm({ mode }: { mode: "record" | "change" }) {
  const record = useProjectPost<S["PaymentInstructionCreate"]>("/payment-instruction", () => ({ "Idempotency-Key": idempotencyKey("instr") }));
  const change = useProjectPost<S["InstructionChangeCreate"]>("/payment-instruction/changes", () => ({ "Idempotency-Key": idempotencyKey("chg") }));
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [recipient, setRecipient] = useState("");
  const [merchant, setMerchant] = useState("");
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const mutation = mode === "record" ? record : change;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const minor = parseMinor(amount, currency);
    if (minor === null) return setLocalError("Enter a valid positive amount, e.g. 1200.50.");
    setLocalError(null);
    const base = { amount_minor: minor, currency, recipient_email: recipient, merchant_email: merchant || null };
    if (mode === "record") record.mutate(base);
    else change.mutate({ ...base, reason });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <InputField label="Amount" required inputMode="decimal" error={localError} value={amount} onChange={(e) => setAmount(e.target.value)} />
      <InputField label="Currency" required maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
      <InputField label="Recipient email" type="email" required value={recipient} onChange={(e) => setRecipient(e.target.value)} />
      <InputField label="Merchant email" type="email" hint="Optional. The merchant account that should receive the money." value={merchant} onChange={(e) => setMerchant(e.target.value)} />
      {mode === "change" && <InputField label="Reason for the change" required value={reason} onChange={(e) => setReason(e.target.value)} />}
      {mutation.error && <Notice tone="error">{errorMessage(mutation.error)}</Notice>}
      {mode === "change" && change.isSuccess && <Notice tone="info">Change recorded. Operations stay on hold until the buyer verifies it in the buyer portal.</Notice>}
      <Button type="submit" busy={mutation.isPending}>{mode === "record" ? "Record instruction" : "Propose change"}</Button>
    </form>
  );
}

export function InstructionPanel() {
  const state = useProjectGet<S["PaymentInstructionState"]>("/payment-instruction");
  const withdraw = useProjectPost<{ id: string }>((b) => `/payment-instruction/changes/${encodeURIComponent(b.id)}/withdraw`);
  if (state.isLoading) return <LoadingState />;
  if (state.error) return <ErrorState error={state.error} />;
  const s = state.data!;
  const current = s.instruction;
  return (
    <>
      <SectionHeader title="Payment instruction">The agreed amount and recipient. A hold blocks operations through Caligo only; it never freezes funds at PayPal.</SectionHeader>
      {s.hold.active ? (
        <div role="alert" className="mb-4 border-l-2 border-ink bg-paper p-4">
          <StatusLabel tone="blocked">Operations on hold</StatusLabel>
          <ul className="mt-2 list-disc pl-5 text-sm">{s.hold.reasons.map((r) => <li key={r}>{HOLD_COPY[r]}</li>)}</ul>
          <p className="mt-2 text-sm text-ink-soft">Release requires: {s.hold.release_requires}</p>
        </div>
      ) : (
        <p className="mb-4 text-sm"><StatusLabel tone="verified">No hold</StatusLabel></p>
      )}
      {current ? (
        <EvidenceCard title={`${formatMoney(current.amount_minor, current.currency)} to ${current.recipient_email}`} provenance="SELLER_ASSERTED" hash={current.chain_sha256} meta={<>Version {s.versions} · fingerprint {current.fingerprint.slice(0, 12)}… · {formatDateTime(current.created_at)}</>}>
          {current.merchant_email && <p>Merchant: {current.merchant_email}</p>}
        </EvidenceCard>
      ) : (
        <>
          <p className="mb-4 text-sm text-ink-soft">No instruction recorded. Record one before creating an invoice.</p>
          <InstructionForm mode="record" />
        </>
      )}
      {s.pending_changes.map((c) => (
        <div key={c.id} className="mt-4 border border-stone p-4">
          <StatusLabel tone="attention">Pending change</StatusLabel>
          <p className="mt-2 text-sm">{formatMoney(c.amount_minor, c.currency)} to {c.recipient_email} — {c.reason}</p>
          <p className="text-xs text-ink-soft">Awaiting buyer verification (authenticator code) in the buyer portal. {c.holds_payment_operations ? "Holds payment operations." : ""}</p>
          <div className="mt-2"><Button variant="secondary" busy={withdraw.isPending} onClick={() => withdraw.mutate({ id: c.id })}>Withdraw change</Button></div>
        </div>
      ))}
      {current && (
        <>
          <SectionHeader title="Propose an instruction change" />
          <InstructionForm mode="change" />
        </>
      )}
    </>
  );
}
