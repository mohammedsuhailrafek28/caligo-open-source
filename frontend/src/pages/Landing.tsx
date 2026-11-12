import { Link } from "react-router-dom";
import { useAuth } from "../auth";
import { EvidenceCard, EvidenceTimeline, RiskSignal } from "../design/evidence";
import { StatusLabel } from "../design/ui";

const STAGES = [
  { n: "01", name: "Agree", body: "Record the scope as written and freeze it into hashed, versioned baselines. A change is only a proposal until the buyer decides." },
  { n: "02", name: "Deliver", body: "Register what you delivered. The server computes the content hash. The buyer acknowledges, accepts or disputes from their own mailbox." },
  { n: "03", name: "Investigate", body: "Compare provider evidence with what people claim. Diagnose divergences and read logs. Every finding cites the records behind it." },
  { n: "04", name: "Recover", body: "Propose a repair, review a dry run, approve that exact version, then apply it to the simulated store. Never a refund or a chargeback." },
];

function Preview() {
  return (
    <div aria-label="Sample evidence record, illustrative data" role="group" className="space-y-4">
      <p className="eyebrow">Sample record · illustrative data, not a real transaction</p>
      <EvidenceCard title="Homepage design handover" provenance="SERVER_VERIFIED" hash={"9f2c4a7e1b8d03a56c7e49d1f0b2a8c3e5d7f9a1b3c5d7e9f1a3b5c7d9e1f30"} meta="Artifact v2 · design-handover.zip">
        <StatusLabel tone="verified">Buyer accepted delivery</StatusLabel>
      </EvidenceCard>
      <EvidenceTimeline items={[
        { id: "a", at: "2026-03-02T09:14:00Z", title: "Scope baseline v1 frozen" },
        { id: "b", at: "2026-03-19T16:40:00Z", title: "Delivery acknowledged by verified mailbox" },
        { id: "c", at: "2026-03-20T08:05:00Z", title: "Invoice draft created", detail: "Not paid: provider has not reported a payment." },
      ]} />
      <ul><RiskSignal severity="review" title="Destination differs from the registry">Seller-asserted registry. Advisory only; review recommended.</RiskSignal></ul>
    </div>
  );
}

export function Landing() {
  const { status } = useAuth();
  const signedIn = status === "authenticated";
  return (
    <div className="min-h-screen">
      <header className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
        <span className="font-display text-2xl">Caligo</span>
        <nav aria-label="Primary" className="flex items-center gap-5 text-sm">
          <a className="underline" href="#how">How it works</a>
          <Link className="underline" to="/buyer">Buyer portal</Link>
          <Link className="underline" to={signedIn ? "/app" : "/login"}>{signedIn ? "Open workspace" : "Sign in"}</Link>
        </nav>
      </header>
      <main>
        <section className="mx-auto grid max-w-6xl gap-12 px-6 pb-20 pt-12 md:grid-cols-[1.1fr_1fr]">
          <div>
            <p className="eyebrow mb-4">A financial evidence workspace</p>
            <h1 className="font-display text-5xl leading-[1.05] md:text-6xl">Know what happened. Prove what happened next.</h1>
            <p className="mt-6 max-w-lg text-lg text-ink-soft">For independent developers and small studios: keep the agreed scope, the delivery evidence and the payment record in one place, each labelled with where it came from.</p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Link to={signedIn ? "/app" : "/register"} className="inline-flex rounded bg-terracotta px-5 py-2.5 text-sm font-medium text-white hover:bg-terracotta-deep">Explore Caligo</Link>
              <a href="#how" className="inline-flex rounded border border-ink/40 px-5 py-2.5 text-sm font-medium hover:bg-paper">How it works</a>
            </div>
          </div>
          <Preview />
        </section>

        <section id="how" aria-labelledby="how-h" className="border-t border-stone bg-paper">
          <div className="mx-auto max-w-6xl px-6 py-16">
            <h2 id="how-h" className="font-display text-3xl">Four stages, one record</h2>
            <ol className="mt-8 grid gap-8 md:grid-cols-4">
              {STAGES.map((s) => (
                <li key={s.name} className="border-t border-ink pt-4">
                  <p className="mono text-ink-soft">{s.n}</p>
                  <h3 className="mt-1 font-display text-2xl">{s.name}</h3>
                  <p className="mt-2 text-sm text-ink-soft">{s.body}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section aria-labelledby="prov-h" className="mx-auto grid max-w-6xl gap-12 px-6 py-16 md:grid-cols-2">
          <div>
            <h2 id="prov-h" className="font-display text-3xl">Every record says where it came from</h2>
            <p className="mt-4 text-ink-soft">Seller-asserted, server-verified, buyer-verified mailbox, provider-reported or advisory. The label is shown with the record and never relies on colour alone. A registry entry is the seller’s word. It is not independent verification, and no screening result can authorize a payment.</p>
          </div>
          <div>
            <h2 className="font-display text-3xl">Safe by construction, advisory by design</h2>
            <p className="mt-4 text-ink-soft">Safety rules are deterministic code, not a model’s opinion. Caligo runs without any hosted AI, API keys or external model calls. Optional language analysis uses a local NLP model, shows whether it actually ran, and can only advise. A draft invoice is never shown as paid, and there is no refund, cancel or payout action.</p>
          </div>
        </section>
      </main>
      <footer className="border-t border-stone px-6 py-8 text-center text-sm text-ink-soft">Caligo · sandbox and simulated-store only</footer>
    </div>
  );
}
