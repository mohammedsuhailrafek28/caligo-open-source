import { useState, type FormEvent } from "react";
import type { ProjectDetail, S } from "../api/types";
import { EmptyState, ErrorState, errorMessage, LoadingState, Notice } from "../design/feedback";
import { EvidenceCard, EvidenceTimeline, RecordTable, type Provenance } from "../design/evidence";
import { PageHeader, SectionHeader } from "../design/layout";
import { Button, InputField, SelectField, StatusLabel, TextAreaField, type Tone } from "../design/ui";
import { formatDateTime, humanize } from "../lib/format";
import { useProjectGet, useProjectPost } from "./hooks";

const verificationProvenance: Record<S["ContentVerification"], Provenance> = {
  VERIFIED_SERVER_HASH: "SERVER_VERIFIED",
  DECLARED_ONLY: "DECLARED_ONLY",
  UNAVAILABLE: "SELLER_ASSERTED",
};
const deliveryTone: Record<S["DeliveryStatus"], Tone> = { NONE: "neutral", ACKNOWLEDGED_ONLY: "advisory", ACCEPTED: "verified", DISPUTED: "attention" };
const requestTone: Record<S["BuyerRequestStatus"], Tone> = { PENDING: "attention", DECIDED: "verified", CANCELLED: "neutral" };

function ArtifactForm() {
  const add = useProjectPost<S["ArtifactCreate"]>("/artifacts");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [mediaType, setMediaType] = useState("text/plain");
  const [content, setContent] = useState("");
  const [keys, setKeys] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const requirement_keys = keys.split(",").map((k) => k.trim().toUpperCase()).filter(Boolean);
    add.mutate({ name, description, media_type: mediaType, content_text: content || null, requirement_keys }, { onSuccess: () => { setName(""); setDescription(""); setContent(""); setKeys(""); } });
  };
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <InputField label="Artifact name" required value={name} onChange={(e) => setName(e.target.value)} />
      <InputField label="Description" required value={description} onChange={(e) => setDescription(e.target.value)} />
      <InputField label="Media type" required value={mediaType} onChange={(e) => setMediaType(e.target.value)} />
      <TextAreaField label="Content text" hint="When supplied, the server computes the SHA-256 itself. Without it the artifact is recorded as declared only." value={content} onChange={(e) => setContent(e.target.value)} />
      <InputField label="Requirement keys" hint="Comma-separated keys from the baseline this artifact addresses, e.g. HOME, BLOG." value={keys} onChange={(e) => setKeys(e.target.value)} />
      {add.error && <Notice tone="error">{errorMessage(add.error)}</Notice>}
      <Button type="submit" busy={add.isPending}>Register artifact</Button>
    </form>
  );
}

function BuyerRequestForm({ artifacts, changes }: { artifacts: S["ArtifactView"][]; changes: S["ScopeChangeView"][] }) {
  const create = useProjectPost<S["BuyerRequestCreate"]>("/buyer-requests");
  const [kind, setKind] = useState<S["BuyerRequestKind"]>("DELIVERY_REVIEW");
  const [email, setEmail] = useState("");
  const [changeId, setChangeId] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate({ kind, buyer_email: email, artifact_ids: kind === "DELIVERY_REVIEW" ? picked : [], change_id: kind === "SCOPE_CHANGE_APPROVAL" ? changeId : null });
  };
  const proposed = changes.filter((c) => c.status === "PROPOSED");
  return (
    <form onSubmit={submit} className="max-w-2xl space-y-4">
      <SelectField label="Ask the buyer to" value={kind} onChange={(e) => setKind(e.target.value as S["BuyerRequestKind"])}>
        <option value="DELIVERY_REVIEW">Review delivered artifacts</option>
        <option value="SCOPE_CHANGE_APPROVAL">Decide on a scope change</option>
      </SelectField>
      <InputField label="Buyer email" type="email" required hint="The buyer proves control of this mailbox with a one-time code. That is mailbox control, not legal identity." value={email} onChange={(e) => setEmail(e.target.value)} />
      {kind === "SCOPE_CHANGE_APPROVAL" ? (
        <SelectField label="Proposed change" required value={changeId} onChange={(e) => setChangeId(e.target.value)}>
          <option value="">Choose…</option>
          {proposed.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
        </SelectField>
      ) : (
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Artifacts for review</legend>
          {artifacts.filter((a) => a.is_latest).map((a) => (
            <label key={a.id} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={picked.includes(a.id)} onChange={() => toggle(a.id)} />{a.name} (v{a.version})</label>
          ))}
          {artifacts.length === 0 && <p className="text-sm text-ink-soft">Register an artifact first.</p>}
        </fieldset>
      )}
      {create.error && <Notice tone="error">{errorMessage(create.error)}</Notice>}
      {create.isSuccess && <Notice tone="success">Request created. The buyer receives a link by email; with the fake mailer in local mode, the link is written to the backend outbox.</Notice>}
      <Button type="submit" busy={create.isPending}>Send request to buyer</Button>
    </form>
  );
}

export function DeliveryPage() {
  const project = useProjectGet<ProjectDetail>("");
  const artifacts = useProjectGet<S["ArtifactView"][]>("/artifacts");
  const changes = useProjectGet<S["ScopeChangeView"][]>("/scope-changes");
  const requests = useProjectGet<S["BuyerRequestView"][]>("/buyer-requests");
  const decisions = useProjectGet<S["BuyerDecisionView"][]>("/buyer-decisions");
  const timeline = useProjectGet<S["TimelineItem"][]>("/timeline");
  const authenticators = useProjectGet<S["BuyerAuthenticatorView"][]>("/buyer-authenticators");
  const cancel = useProjectPost<{ id: string }>((b) => `/buyer-requests/${encodeURIComponent(b.id)}/cancel`);
  return (
    <>
      <PageHeader eyebrow="Agreement & delivery" title="Delivery evidence">What was delivered, and what the buyer did about it. Only the buyer can acknowledge, accept or dispute.</PageHeader>
      {project.data && <p className="mb-6 text-sm">Delivery status: <StatusLabel tone={deliveryTone[project.data.delivery_status]}>{humanize(project.data.delivery_status)}</StatusLabel></p>}

      <SectionHeader title="Artifacts" />
      {artifacts.isLoading && <LoadingState />}
      {artifacts.error && <ErrorState error={artifacts.error} />}
      {artifacts.data?.length === 0 && <EmptyState title="Nothing registered yet" />}
      <div className="space-y-3">
        {artifacts.data?.map((a) => (
          <EvidenceCard key={a.id} title={`${a.name} · v${a.version}`} provenance={verificationProvenance[a.content_verification]} hash={a.sha256} meta={<>{a.media_type} · ingested {formatDateTime(a.ingested_at)}{!a.is_latest && " · superseded"}</>}>
            <p>{a.description}</p>
            {a.requirement_keys.length > 0 && <p className="mt-1 text-ink-soft">Addresses: {a.requirement_keys.join(", ")}</p>}
          </EvidenceCard>
        ))}
      </div>
      <SectionHeader title="Register an artifact" />
      <ArtifactForm />

      <SectionHeader title="Buyer requests" />
      {requests.error && <ErrorState error={requests.error} />}
      {requests.data?.length === 0 && <EmptyState title="No requests sent" />}
      {requests.data && requests.data.length > 0 && (
        <RecordTable
          caption="Buyer requests"
          rows={requests.data}
          rowKey={(r) => r.id}
          columns={[
            { header: "Kind", cell: (r) => humanize(r.kind) },
            { header: "Buyer", cell: (r) => r.buyer_email_masked },
            { header: "Status", cell: (r) => <StatusLabel tone={requestTone[r.status]}>{humanize(r.status)}</StatusLabel> },
            { header: "Link opened", cell: (r) => (r.link_opened ? "Yes" : "No") },
            { header: "Expires", cell: (r) => formatDateTime(r.expires_at) },
            { header: "", cell: (r) => (r.status === "PENDING" ? <Button variant="quiet" onClick={() => cancel.mutate({ id: r.id })}>Cancel</Button> : null) },
          ]}
        />
      )}
      {cancel.error && <Notice tone="error">{errorMessage(cancel.error)}</Notice>}
      <SectionHeader title="Send a request" />
      <BuyerRequestForm artifacts={artifacts.data ?? []} changes={changes.data ?? []} />

      <SectionHeader title="Buyer decisions">Evidence that whoever controlled the verified mailbox decided this. It is not legal identity.</SectionHeader>
      {decisions.error && <ErrorState error={decisions.error} />}
      {decisions.data?.length === 0 && <EmptyState title="No buyer decisions yet" />}
      <div className="space-y-3">
        {decisions.data?.map((d) => (
          <EvidenceCard key={d.id} title={humanize(d.decision)} provenance="BUYER_VERIFIED" meta={<>{d.buyer_email_masked} · {formatDateTime(d.decided_at)}</>}>
            {d.statement && <p className="whitespace-pre-wrap">“{d.statement}”</p>}
          </EvidenceCard>
        ))}
      </div>

      <SectionHeader title="Buyer authenticators">Authenticator enrolments the buyer uses to verify payment-instruction changes. The seller can see them but cannot create or approve them.</SectionHeader>
      {authenticators.error && <ErrorState error={authenticators.error} />}
      {authenticators.data?.length === 0 && <EmptyState title="No authenticators enrolled" />}
      {authenticators.data && authenticators.data.length > 0 && (
        <RecordTable caption="Buyer authenticators" rows={authenticators.data} rowKey={(a) => a.id} columns={[
          { header: "Buyer", cell: (a) => a.buyer_email_masked },
          { header: "Status", cell: (a) => <StatusLabel tone={a.revoked_at ? "neutral" : "verified"}>{humanize(a.status)}</StatusLabel> },
          { header: "Trust basis", cell: (a) => humanize(a.trust_basis) },
          { header: "Enrolled", cell: (a) => formatDateTime(a.created_at) },
        ]} />
      )}

      <SectionHeader title="Timeline">Server-recorded times.</SectionHeader>
      {timeline.error && <ErrorState error={timeline.error} />}
      {timeline.data && <EvidenceTimeline items={timeline.data.map((t, i) => ({ id: `${t.subject_id}-${i}`, at: t.at, title: t.summary, detail: humanize(t.event_type) }))} />}
    </>
  );
}
