// API-level E2E: drives every endpoint the UI calls, through the Vite proxy, with real session cookie + CSRF.
// Usage: node e2e/api-e2e.mjs [baseUrl]   (backend must run with PAYPAL_MODE=mock). Uses a fresh synthetic seller each run.
const base = process.argv[2] ?? "http://localhost:3000";
const origin = new URL(base).origin; // must match the backend CORS_ORIGINS allow-list
const stamp = Date.now();
const email = `e2e-${stamp}@example.org`;
const password = `pw-${stamp}-correct-horse`;
let cookies = "";
let csrf = "";
const results = [];

async function call(method, path, body, headers = {}) {
  const res = await fetch(base + path, {
    method,
    headers: { Origin: origin, Accept: "application/json", ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...(method !== "GET" && csrf ? { "X-CSRF-Token": csrf } : {}), ...(cookies ? { Cookie: cookies } : {}), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const set = res.headers.getSetCookie?.() ?? [];
  if (set.length) cookies = [...new Map([...cookies.split("; ").filter(Boolean), ...set.map((c) => c.split(";")[0])].map((c) => [c.split("=")[0], c])).values()].join("; ");
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* markdown */ }
  return { status: res.status, json, text };
}
async function step(name, fn) {
  try { await fn(); results.push([name, "PASS"]); } catch (e) { results.push([name, `FAIL ${e.message}`]); }
}
const ok = (r, ...codes) => { if (!(codes.length ? codes : [200, 201]).includes(r.status)) throw new Error(`HTTP ${r.status} ${r.text.slice(0, 160)}`); return r.json; };
const idem = (p) => ({ "Idempotency-Key": `${p}-${stamp}-${Math.random().toString(16).slice(2, 10)}` });

let pid = "";
await step("register + login + me", async () => {
  ok(await call("POST", "/api/v1/auth/register", { display_name: "E2E Seller", email, password }));
  const login = ok(await call("POST", "/api/v1/auth/login", { email, password }));
  csrf = login.csrf_token;
  ok(await call("GET", "/api/v1/auth/me"));
});
await step("writes without CSRF are rejected", async () => {
  const saved = csrf; csrf = "";
  const r = await call("POST", "/api/v1/projects", { title: "nope" });
  csrf = saved;
  if (r.status < 400) throw new Error(`expected rejection, got ${r.status}`);
});
await step("create + list project", async () => {
  pid = ok(await call("POST", "/api/v1/projects", { title: "E2E project" })).id;
  if (!ok(await call("GET", "/api/v1/projects")).some((p) => p.id === pid)) throw new Error("not listed");
});
const P = (t = "") => `/api/v1/projects/${pid}${t}`;
await step("scope record + baseline + change proposal", async () => {
  const rec = ok(await call("POST", P("/scope-records"), { kind: "ORIGINAL", source_label: "SOW v1", content: "Build a homepage and a blog." }));
  ok(await call("POST", P("/baselines"), { source_scope_record_id: rec.id, requirements: [{ key: "HOME", title: "Homepage", description: "" }, { key: "BLOG", title: "Blog", description: "" }] }));
  ok(await call("POST", P("/scope-changes"), { title: "Add search", description: "Add site search", requirements: [{ key: "SEARCH", title: "Search", description: "" }] }));
  ok(await call("GET", P("/scope-changes")));
});
let artifactId = "";
await step("artifact (server-hashed) + buyer request + timeline", async () => {
  const a = ok(await call("POST", P("/artifacts"), { name: "handover.txt", description: "Handover notes", media_type: "text/plain", content_text: "hello", requirement_keys: ["HOME"] }));
  artifactId = a.id;
  if (a.content_verification !== "VERIFIED_SERVER_HASH") throw new Error(`verification ${a.content_verification}`);
  ok(await call("POST", P("/buyer-requests"), { kind: "DELIVERY_REVIEW", buyer_email: "buyer@example.org", artifact_ids: [artifactId], change_id: null }));
  ok(await call("GET", P("/buyer-requests"))); ok(await call("GET", P("/buyer-decisions"))); ok(await call("GET", P("/timeline")));
});
await step("instruction -> draft invoice is never shown as paid", async () => {
  ok(await call("POST", P("/payment-instruction"), { amount_minor: 120050, currency: "USD", recipient_email: "buyer@example.org", merchant_email: null }, idem("instr")));
  const state = ok(await call("GET", P("/payment-instruction")));
  if (!state.instruction) throw new Error("no instruction");
  const pay = ok(await call("POST", P("/invoices"), { amount_minor: 120050, currency: "USD", recipient_email: "buyer@example.org" }, idem("inv")));
  if (pay.provider_status === "PAID") throw new Error("draft invoice reported as PAID");
  ok(await call("POST", P("/payments/sync"), {}));
  ok(await call("GET", P("/payments"))); ok(await call("GET", P(`/payments/${pay.id}/events`)));
});
await step("instruction change creates a hold until buyer verifies", async () => {
  ok(await call("POST", P("/payment-instruction/changes"), { amount_minor: 120050, currency: "USD", recipient_email: "other@example.org", merchant_email: null, reason: "New bank" }, idem("chg")));
  const s = ok(await call("GET", P("/payment-instruction")));
  if (!s.hold.active || s.pending_changes.length !== 1) throw new Error("expected active hold + pending change");
});
await step("payee registry + screening (advisory)", async () => {
  const payee = ok(await call("POST", P("/payee-registry"), { label: "Acme", destination: "pay@acme.example", note: null }));
  ok(await call("POST", P(`/payee-registry/${payee.payee_id}/versions`), { destination: "pay2@acme.example", note: null }));
  const r = ok(await call("POST", P("/payee-registry/screen"), { payee_id: payee.payee_id, destination: "evil@example.net", text: "URGENT: we changed bank details, keep this confidential" }));
  if (r.outcome !== "REVIEW_RECOMMENDED" || r.advisory_only !== true) throw new Error(`unexpected screen result ${r.outcome}`);
});
await step("observations + ledger import + comparison + cases", async () => {
  const cmp0 = ok(await call("GET", P("/observation-comparison")));
  ok(await call("POST", P("/payment-observations"), { kind: "PAYMENT", amount_minor: 120050, currency: "USD", invoice_number: cmp0.payments[0].invoice_number, transaction_id: null, note: null }));
  ok(await call("GET", P("/payment-observations"))); ok(await call("GET", P("/observation-comparison"))); ok(await call("GET", P("/recovery-cases")));
  ok(await call("GET", P("/merchant-ledger/imports")));
});
await step("diagnosis + divergences + health + log intelligence", async () => {
  ok(await call("GET", P("/state-recovery/divergences")));
  ok(await call("GET", P("/state-recovery/integration-health")));
  const payId = ok(await call("GET", P("/payments")))[0].id;
  const dg = await call("GET", P(`/state-recovery/diagnosis?kind=DUPLICATE_ORDERS&payment_id=${payId}`));
  if (dg.status !== 409 || dg.json?.error?.code !== "no_divergence") ok(dg); // 409 no_divergence is the honest answer for a healthy payment
  const liRes = await call("POST", P("/state-recovery/log-intelligence"), { kind: "DUPLICATE_ORDERS", requested_mode: "stanza", payment_id: payId, logs: [{ text: "order created twice for the same invoice" }], credentials_configured: true, heartbeat_ok: true });
  if (liRes.status === 409 && liRes.json?.error?.code === "no_divergence") { results.push(["  log-intelligence: 409 no_divergence (healthy payment)", "INFO"]); return; }
  const li = ok(liRes);
  if (li.requested_mode !== "stanza" || !li.effective_mode) throw new Error("modes missing");
  results.push([`  log-intelligence requested=${li.requested_mode} effective=${li.effective_mode} fallback=${li.fallback_reason ?? "none"}`, "INFO"]);
});
await step("recovery plans list", async () => { ok(await call("GET", P("/state-recovery/plans"))); });
await step("reports (json + markdown) + audit trail", async () => {
  ok(await call("GET", P("/evidence-report"))); ok(await call("GET", P("/unified-report")));
  if (!(await call("GET", P("/evidence-report/markdown"))).text.length) throw new Error("empty markdown");
  if (!(await call("GET", P("/unified-report/markdown"))).text.length) throw new Error("empty markdown");
  if (ok(await call("GET", P("/audit-events"))).length < 3) throw new Error("audit trail too short");
});
await step("capabilities + logout invalidates session", async () => {
  ok(await call("GET", "/api/v1/capabilities"));
  ok(await call("POST", "/api/v1/auth/logout", {}), 200, 204);
  if ((await call("GET", "/api/v1/auth/me")).status !== 401) throw new Error("session still valid");
});

for (const [n, r] of results) console.log(`${r.padEnd(4).slice(0, 4) === "PASS" ? "PASS" : r.startsWith("INFO") ? "INFO" : "FAIL"}  ${n}${r.startsWith("FAIL") ? " :: " + r : ""}`);
const failed = results.filter(([, r]) => r.startsWith("FAIL")).length;
console.log(`\n${results.filter(([, r]) => r === "PASS").length} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
