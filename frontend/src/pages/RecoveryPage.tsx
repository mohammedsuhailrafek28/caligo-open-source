import { useState, type FormEvent } from "react";
import type { Payment, S } from "../api/types";
import { ConfirmationDialog, EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { AuditEntry, EvidenceCard } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, SelectField, StatusLabel, type Tone } from "../design/ui";
import { humanize } from "../lib/format";
import { useProjectGet, useProjectPost } from "./hooks";
import { KeyValues, KINDS, type DiagnosisKind } from "./InvestigationsPages";

type Plan = S["RecoveryPlanView"];
const stateTone: Record<Plan["state"], Tone> = { PROPOSED: "attention", NEEDS_REVIEW: "attention", BLOCKED: "blocked", ADVISORY: "advisory", APPROVED: "verified", EXECUTED: "verified" };

function CreatePlan() {
  const payments = useProjectGet<Payment[]>("/payments");
  const create = useProjectPost<S["RecoveryPlanCreate"]>("/state-recovery/plans");
  const [kind, setKind] = useState<DiagnosisKind>("DUPLICATE_ORDERS");
  const [paymentId, setPaymentId] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate({ kind, payment_id: paymentId || null, credentials_configured: true, heartbeat_ok: true });
  };
  return (
    <form onSubmit={submit} className="max-w-xl space-y-4">
      <SelectField label="Scenario" value={kind} onChange={(e) => setKind(e.target.value as DiagnosisKind)}>{KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}</SelectField>
      <SelectField label="Payment" value={paymentId} onChange={(e) => setPaymentId(e.target.value)}>
        <option value="">Any</option>
        {payments.data?.filter((p) => p.id).map((p) => <option key={p.id} value={p.id ?? ""}>{p.paypal_invoice_id ?? p.id}</option>)}
      </SelectField>
      {create.error && <Notice tone="error">{errorMessage(create.error)}</Notice>}
      <Button type="submit" busy={create.isPending}>Propose a recovery plan</Button>
    </form>
  );
}

function PlanDetail({ plan }: { plan: Plan }) {
  const preview = useProjectGet<S["RecoveryPreview"]>(`/state-recovery/plans/${plan.id}/preview`);
  const events = useProjectGet<S["RecoveryEventList"]>(`/state-recovery/plans/${plan.id}/events`);
  const approve = useProjectPost<S["RecoveryPlanApprove"]>(`/state-recovery/plans/${plan.id}/approve`);
  const execute = useProjectPost<Record<string, never>>(`/state-recovery/plans/${plan.id}/execute`);
  const [confirming, setConfirming] = useState(false);
  // `executable` means "approved and latest, so it can run now" and is false for every fresh plan. A plan is approvable while it is a
  // proposal; the backend still rejects anything blocked, advisory-only, superseded or with changed evidence.
  const canApprove = plan.state === "PROPOSED" && plan.status === "PROPOSED" && !plan.superseded;
  return (
    <EvidenceCard title={`${humanize(plan.kind)} · v${plan.version}`} hash={plan.evidence_fingerprint} meta={<StatusLabel tone={stateTone[plan.state]}>{humanize(plan.state)}</StatusLabel>}>
      <ol className="list-decimal space-y-1 pl-5">{plan.steps.map((s) => <li key={s.step_id}>{s.description}</li>)}</ol>
      {plan.reasons.length > 0 && <div className="mt-3"><p className="text-sm font-medium">Notes on this plan</p>{plan.reasons.map((r, i) => <KeyValues key={i} data={r} />)}</div>}
      {preview.isLoading && <LoadingState />}
      {preview.error && <ErrorState error={preview.error} />}
      {preview.data && (
        <div className="mt-3 border-t border-stone pt-3">
          <p className="text-sm font-medium">Dry-run preview (nothing has changed)</p>
          <ul className="mt-1 text-sm">{preview.data.steps.map((s) => <li key={s.step_id}><StatusLabel tone={s.status === "BLOCKED" ? "blocked" : s.status === "ALREADY_DONE" ? "neutral" : "advisory"}>{humanize(s.status)}</StatusLabel> {s.description}</li>)}</ul>
          {!preview.data.plan_current && <Notice tone="info">The evidence changed since this plan was made. Create a new plan.</Notice>}
          {preview.data.blockers.map((b, i) => <Notice key={i} tone="error">{Object.values(b).join(": ")}</Notice>)}
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {canApprove && <Button variant="secondary" busy={approve.isPending} onClick={() => approve.mutate({ version: plan.version, evidence_fingerprint: plan.evidence_fingerprint })}>Approve this exact plan</Button>}
        {plan.state === "APPROVED" && <Button variant="danger" onClick={() => setConfirming(true)}>Execute on simulated store…</Button>}
      </div>
      {approve.error && <Notice tone="error">{errorMessage(approve.error)}</Notice>}
      {execute.isSuccess && <Notice tone="success">Executed on the simulated store.</Notice>}
      {events.data && events.data.events.length > 0 && (
        <details className="mt-3"><summary className="cursor-pointer text-sm text-terracotta-deep">Plan history</summary>
          <ul>{events.data.events.map((e) => <AuditEntry key={e.id} action={e.event_type} at={e.created_at ?? ""} detail={JSON.stringify(e.detail)} />)}</ul>
        </details>
      )}
      <ConfirmationDialog open={confirming} destructive title="Execute this recovery plan?" confirmLabel="Execute plan" busy={execute.isPending}
        onCancel={() => setConfirming(false)} onConfirm={() => execute.mutate({}, { onSettled: () => setConfirming(false) })}>
        <p>This changes records in the <strong>simulated</strong> merchant store. The change cannot be undone by Caligo.</p>
        <p>It does not refund, cancel or capture anything at PayPal, and it does not move money. Execution is rejected if the evidence has changed since approval.</p>
        {execute.error && <Notice tone="error">{errorMessage(execute.error)}</Notice>}
      </ConfirmationDialog>
    </EvidenceCard>
  );
}

export function RecoveryPage() {
  const plans = useProjectGet<S["RecoveryPlanList"]>("/state-recovery/plans");
  return (
    <>
      <PageHeader eyebrow="Recovery & records" title="Recovery">Human-approved repairs to the simulated merchant store. There is no refund, cancel or chargeback action anywhere in Caligo.</PageHeader>
      <Notice tone="info">Every plan is bound to the evidence it was made from. Approving it approves exactly that version, and execution re-checks the evidence first.</Notice>
      <SectionHeader title="Plans" />
      {plans.isLoading && <LoadingState />}
      {plans.error && <ErrorState error={plans.error} />}
      {plans.data?.items.length === 0 && <EmptyState title="No recovery plans">Propose one below when diagnosis finds a divergence.</EmptyState>}
      <div className="space-y-4">{plans.data?.items.map((p) => <PlanDetail key={p.id} plan={p} />)}</div>
      <SectionHeader title="Propose a plan" />
      <CreatePlan />
    </>
  );
}
