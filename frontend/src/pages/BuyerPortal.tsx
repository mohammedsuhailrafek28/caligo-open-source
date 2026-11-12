import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { S } from "../api/types";
import { errorMessage, Notice } from "../design/feedback";
import { Button, InputField, TextAreaField } from "../design/ui";
import { formatMoney, humanize } from "../lib/format";

/** Buyer-facing portal: no seller session. Mailbox control (one-time code) proves who decided; it is not legal identity. */
function Frame({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto max-w-xl px-6 py-14">
      <Link to="/" className="font-display text-2xl">Caligo</Link>
      <h1 className="mt-8 font-display text-4xl">Buyer portal</h1>
      <p className="mt-2 text-sm text-ink-soft">Your decision is recorded as evidence that whoever controlled this mailbox made it. It is not a legal signature.</p>
      <div className="mt-8 space-y-10">{children}</div>
    </main>
  );
}

function useAsync() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try { await fn(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  };
  return { busy, error, run };
}

function RequestFlow() {
  const [params] = useSearchParams();
  const [token, setToken] = useState(params.get("token") ?? "");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [session, setSession] = useState<S["BuyerSessionResponse"] | null>(null);
  const [statement, setStatement] = useState("");
  const [done, setDone] = useState<S["BuyerDecisionResponse"] | null>(null);
  const { busy, error, run } = useAsync();
  const challenge = (e: FormEvent) => { e.preventDefault(); void run(async () => { await api.post("/api/v1/buyer/challenge", { token }); setSent(true); }); };
  const verify = (e: FormEvent) => { e.preventDefault(); void run(async () => setSession(await api.post<S["BuyerSessionResponse"]>("/api/v1/buyer/verify", { token, code }))); };
  const decide = (decision: S["BuyerDecision"]) => void run(async () => setDone(await api.post<S["BuyerDecisionResponse"]>("/api/v1/buyer/decide", { session_token: session!.session_token, decision, statement: statement || null })));
  return (
    <section aria-labelledby="req-h" className="space-y-4">
      <h2 id="req-h" className="font-display text-2xl">Review a request</h2>
      {done ? (
        <Notice tone="success">Recorded: {humanize(done.decision.decision)}. {done.scope_note}</Notice>
      ) : session ? (
        <div className="space-y-4">
          <p className="text-sm">Project: <strong>{session.project_title}</strong> · {humanize(session.kind)}</p>
          <pre className="mono overflow-x-auto border border-stone bg-white p-3">{JSON.stringify(session.subject, null, 2)}</pre>
          <TextAreaField label="Statement (optional)" value={statement} onChange={(e) => setStatement(e.target.value)} />
          <div className="flex flex-wrap gap-2">{session.allowed_decisions.map((d) => <Button key={d} variant={d.startsWith("DISPUTE") || d.startsWith("REJECT") ? "danger" : "primary"} busy={busy} onClick={() => decide(d)}>{humanize(d)}</Button>)}</div>
        </div>
      ) : (
        <form onSubmit={sent ? verify : challenge} className="space-y-4">
          <InputField label="Request token" required mono hint="From the link in your email." value={token} onChange={(e) => setToken(e.target.value)} />
          {sent && <InputField label="One-time code" required inputMode="numeric" autoComplete="one-time-code" hint="Sent to the email address the seller gave. If the request is valid you receive a code." value={code} onChange={(e) => setCode(e.target.value)} />}
          <Button type="submit" busy={busy}>{sent ? "Verify code" : "Send me a code"}</Button>
        </form>
      )}
      {error && <Notice tone="error">{error}</Notice>}
    </section>
  );
}

function InstructionFlow() {
  const [projectId, setProjectId] = useState("");
  const [changeId, setChangeId] = useState("");
  const [email, setEmail] = useState("");
  const [totp, setTotp] = useState("");
  const [challenge, setChallenge] = useState<S["InstructionChallengeResponse"] | null>(null);
  const [result, setResult] = useState<S["InstructionDecideResponse"] | null>(null);
  const { busy, error, run } = useAsync();
  const start = (e: FormEvent) => { e.preventDefault(); void run(async () => setChallenge(await api.post<S["InstructionChallengeResponse"]>("/api/v1/buyer/instruction-changes/challenge", { project_id: projectId, change_id: changeId, buyer_email: email }))); };
  const decide = (decision: S["InstructionDecision"]) => void run(async () => setResult(await api.post<S["InstructionDecideResponse"]>("/api/v1/buyer/instruction-changes/decide", {
    project_id: projectId, change_id: changeId, buyer_email: email, challenge_id: challenge!.challenge_id, decision, totp_code: totp,
    from_fingerprint: challenge!.change.from_fingerprint, proposed_fingerprint: challenge!.change.proposed_fingerprint,
  })));
  const c = challenge?.change;
  return (
    <section aria-labelledby="ins-h" className="space-y-4">
      <h2 id="ins-h" className="font-display text-2xl">Verify a payment instruction change</h2>
      <p className="text-sm text-ink-soft">If a seller says their payment details changed, confirm it here with your authenticator app before you pay anything.</p>
      {result ? (
        <Notice tone={result.outcome === "APPLIED" ? "success" : "info"}>{humanize(result.outcome)}. {result.note}</Notice>
      ) : c ? (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="border border-stone bg-white p-3"><p className="eyebrow">Current</p><p>{formatMoney(c.current.amount_minor, c.current.currency)}</p><p className="mono break-all">{c.current.recipient_email}</p></div>
            <div className="border border-terracotta-deep bg-white p-3"><p className="eyebrow">Proposed</p><p>{formatMoney(c.proposed.amount_minor, c.proposed.currency)}</p><p className="mono break-all">{c.proposed.recipient_email}</p></div>
          </div>
          <p className="text-sm">Seller’s reason: {c.seller_reason}</p>
          <p className="text-xs text-ink-soft">{c.trust_note}</p>
          {!c.approvable && <Notice tone="info">This change can no longer be approved{c.withdrawn_by_seller ? " (withdrawn by the seller)" : ""}.</Notice>}
          <InputField label="Authenticator code" required inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} />
          <div className="flex gap-2">{challenge!.allowed_decisions.map((d) => <Button key={d} variant={d.startsWith("REJECT") ? "danger" : "primary"} busy={busy} disabled={!totp} onClick={() => decide(d)}>{humanize(d)}</Button>)}</div>
        </div>
      ) : (
        <form onSubmit={start} className="space-y-4">
          <InputField label="Project id" required mono value={projectId} onChange={(e) => setProjectId(e.target.value)} />
          <InputField label="Change id" required mono value={changeId} onChange={(e) => setChangeId(e.target.value)} />
          <InputField label="Your email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          <Button type="submit" busy={busy}>Show the change</Button>
        </form>
      )}
      {error && <Notice tone="error">{error}</Notice>}
    </section>
  );
}

export function BuyerPortal() {
  return <Frame><RequestFlow /><InstructionFlow /></Frame>;
}
