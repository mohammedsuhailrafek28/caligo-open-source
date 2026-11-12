import { useState, type FormEvent } from "react";
import { api, projectPath } from "../api/client";
import type { S } from "../api/types";
import { EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { EvidenceCard, RiskSignal } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, InputField, SelectField, StatusLabel, TextAreaField } from "../design/ui";
import { formatDateTime, formatMoney, parseMinor } from "../lib/format";
import { useProjectGet, useProjectId, useProjectPost } from "./hooks";

const SELLER_ASSERTED_NOTE = "Registry entries are seller-asserted. They are not independent verification of who owns the destination.";

function RegisterForm() {
  const register = useProjectPost<S["PayeeCreate"]>("/payee-registry");
  const [label, setLabel] = useState("");
  const [destination, setDestination] = useState("");
  const [note, setNote] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    register.mutate({ label, destination, note: note || null }, { onSuccess: () => { setLabel(""); setDestination(""); setNote(""); } });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <InputField label="Label" required value={label} onChange={(e) => setLabel(e.target.value)} />
      <InputField label="Destination (payee email)" type="email" required value={destination} onChange={(e) => setDestination(e.target.value)} />
      <InputField label="Note" value={note} onChange={(e) => setNote(e.target.value)} />
      {register.error && <Notice tone="error">{errorMessage(register.error)}</Notice>}
      <Button type="submit" busy={register.isPending}>Register payee</Button>
    </form>
  );
}

function ChangeForm({ payeeId }: { payeeId: string }) {
  const change = useProjectPost<S["PayeeDestinationChange"]>(`/payee-registry/${encodeURIComponent(payeeId)}/versions`);
  const [destination, setDestination] = useState("");
  const [note, setNote] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    change.mutate({ destination, note: note || null }, { onSuccess: () => { setDestination(""); setNote(""); } });
  };
  return (
    <form onSubmit={submit} className="mt-3 max-w-xl space-y-3">
      <InputField label="New destination" type="email" required value={destination} onChange={(e) => setDestination(e.target.value)} />
      <InputField label="Note" value={note} onChange={(e) => setNote(e.target.value)} />
      {change.error && <Notice tone="error">{errorMessage(change.error)}</Notice>}
      <Button variant="secondary" type="submit" busy={change.isPending}>Record destination change</Button>
    </form>
  );
}

export function PayeesPage() {
  const payees = useProjectGet<S["PayeeView"][]>("/payee-registry");
  return (
    <>
      <PageHeader eyebrow="Payment assurance" title="Payee registry">Known payment destinations, versioned and hash-chained. {SELLER_ASSERTED_NOTE}</PageHeader>
      {payees.isLoading && <LoadingState />}
      {payees.error && <ErrorState error={payees.error} />}
      {payees.data?.length === 0 && <EmptyState title="No payees registered" />}
      <div className="space-y-4">
        {payees.data?.map((p) => (
          <EvidenceCard key={p.payee_id} title={p.current.label} provenance="SELLER_ASSERTED" hash={p.current.chain_sha256} meta={<>Version {p.current.version} · {formatDateTime(p.current.created_at)}</>}>
            <p className="mono">{p.current.destination}</p>
            {p.history.length > 1 && (
              <details className="mt-3">
                <summary className="cursor-pointer text-sm text-terracotta-deep">History ({p.history.length} versions)</summary>
                <ol className="mt-2 space-y-1 text-sm">{p.history.map((h) => <li key={h.id}>v{h.version} · <span className="mono">{h.destination}</span> · {formatDateTime(h.created_at)}</li>)}</ol>
              </details>
            )}
            <details className="mt-2"><summary className="cursor-pointer text-sm text-terracotta-deep">Record a destination change</summary><ChangeForm payeeId={p.payee_id} /></details>
          </EvidenceCard>
        ))}
      </div>
      <SectionHeader title="Register a payee" />
      <RegisterForm />
    </>
  );
}

const SEVERITY = { HIGH: "high", MEDIUM: "review", LOW: "info" } as const;

export function ScreeningPage() {
  const pid = useProjectId();
  const payees = useProjectGet<S["PayeeView"][]>("/payee-registry");
  const [payeeId, setPayeeId] = useState("");
  const [destination, setDestination] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [text, setText] = useState("");
  const [result, setResult] = useState<S["PayeeScreenResponse"] | null>(null);
  const [screenedText, setScreenedText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const minor = amount ? parseMinor(amount, currency) : null;
    if (amount && minor === null) return setError("Enter a valid amount, e.g. 1200.50.");
    setBusy(true);
    setError(null);
    setScreenedText(text);
    try {
      setResult(await api.post<S["PayeeScreenResponse"]>(projectPath(pid, "/payee-registry/screen"), {
        payee_id: payeeId || null, destination: destination || null, amount_minor: minor, currency: amount ? currency : null, text: text || null,
      }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHeader eyebrow="Payment assurance" title="Screening">Check a payment request or message against the registry and recorded instruction for signs of business-email compromise.</PageHeader>
      <Notice tone="info">Screening is deterministic, advisory and never authorizes or blocks a payment. {SELLER_ASSERTED_NOTE} Nothing you enter here is stored.</Notice>
      <form onSubmit={(e) => void submit(e)} className="mt-6 max-w-2xl space-y-4">
        <SelectField label="Registered payee" value={payeeId} onChange={(e) => setPayeeId(e.target.value)}>
          <option value="">None</option>
          {payees.data?.map((p) => <option key={p.payee_id} value={p.payee_id}>{p.current.label}</option>)}
        </SelectField>
        <InputField label="Destination to check" type="email" value={destination} onChange={(e) => setDestination(e.target.value)} />
        <InputField label="Amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
        <InputField label="Currency" maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
        <TextAreaField label="Invoice text, email or memo" maxLength={4096} value={text} onChange={(e) => setText(e.target.value)} />
        {error && <Notice tone="error">{error}</Notice>}
        <Button type="submit" busy={busy}>Screen</Button>
      </form>
      {result && (
        <>
          <SectionHeader title="Result" />
          <p className="mb-3"><StatusLabel tone={result.outcome === "NO_SIGNALS_FOUND" ? "advisory" : "attention"}>{result.outcome === "NO_SIGNALS_FOUND" ? "No signals found — this is not verification" : "Review recommended"}</StatusLabel></p>
          <p className="mb-3 text-sm text-ink-soft">Authority: {result.authority}</p>
          <ul>{result.signals.map((s, i) => <RiskSignal key={`${s.code}-${i}`} severity={SEVERITY[s.severity]} title={s.message}>{s.spans.length > 0 && <>Found in text: {s.spans.map((sp) => `“${screenedText.slice(sp.start, sp.end)}”`).join(", ")}</>}</RiskSignal>)}</ul>
          {result.extracted && (
            <p className="mt-4 text-sm text-ink-soft">Extracted from text (unverified): {result.extracted.emails.map((x) => x.value).join(", ") || "no emails"}; amount {formatMoney(result.extracted.amount_minor, result.extracted.currency ?? currency)}; reference {result.extracted.invoice_reference ?? "—"}.</p>
          )}
        </>
      )}
    </>
  );
}
