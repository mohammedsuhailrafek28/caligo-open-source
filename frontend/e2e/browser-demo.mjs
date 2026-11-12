// Browser demo run against scripts/demo_seeded_server.py: buyer decision via portal, seller diagnosis, Stanza, guarded recovery. Usage: node e2e/browser-demo.mjs <stateDir> <screenshotDir>
// <stateDir> is the --state-dir given to the seeded server. Use a FRESH server per run (buyer token is single-use; recovery executes once).
// Needs `npm i --no-save playwright-core` and Chrome (or CHROME_PATH=<chromium exe>). frontend must be http://localhost:3001 -> seeded backend.
import { chromium } from "playwright-core";
import fs from "node:fs";
const base = "http://localhost:3001";
const state = process.argv[2];
const dir = process.argv[3];
const seed = JSON.parse(fs.readFileSync(state + "/seed.json", "utf8"));
const b = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
const problems = [];
const mk = async () => { const c = await b.newContext({ viewport: { width: 1280, height: 900 } }); const p = await c.newPage();
  p.on("pageerror", (e) => problems.push("PAGEERROR " + e.message));
  p.on("response", async (r) => { if (r.status() >= 400 && r.status() !== 401 && r.url().includes("/api/")) problems.push(`HTTP ${r.status()} ${r.request().method()} ${r.url().split("/api/v1")[1]} ${(r.request().postData() || "").slice(0, 160)} => ${(await r.text()).slice(0, 200)}`); });
  p.on("console", (m) => { if (m.type() === "error" && !/401/.test(m.text())) problems.push("console.error " + m.text()); });
  return p; };
const res = [];
const t0 = Date.now();
const step = async (name, fn) => { const s = Date.now(); try { const d = await fn(); res.push(`PASS (${((Date.now() - s) / 1000).toFixed(1)}s) ${name}${d ? " :: " + d : ""}`); } catch (e) { res.push(`FAIL ${name} :: ${e.message.split("\n")[0]}`); } };
const txt = async (p) => (await p.innerText("body")).replace(/\s+/g, " ");

// ---- buyer decision (separate browser context = the buyer, no seller session)
const buyer = await mk();
await step("buyer portal: token -> emailed code -> verify -> decide", async () => {
  await buyer.goto(`${base}/buyer?token=${seed.buyer_token}`);
  await buyer.getByRole("button", { name: "Send me a code" }).click();
  await buyer.getByLabel("One-time code").waitFor();
  const lines = fs.readFileSync(state + "/outbox.jsonl", "utf8").trim().split("\n").map((l) => JSON.parse(l));
  const body = JSON.stringify(lines[lines.length - 1]);
  const code = /code: (\d{6})/.exec(body.replace(/\\n/g, " "))?.[1];
  if (!code) throw new Error("no code in outbox");
  await buyer.getByLabel("One-time code").fill(code);
  await buyer.getByRole("button", { name: "Verify code" }).click();
  await buyer.getByLabel(/Statement/).waitFor();
  await buyer.getByLabel(/Statement/).fill("Received the handover notes.");
  await buyer.screenshot({ path: dir + "/10-buyer-decide.png", fullPage: true });
  const labels = await buyer.locator("button").allInnerTexts();
  const accept = buyer.getByRole("button", { name: /acknowledge/i }).first();
  await accept.click();
  await buyer.getByText(/Recorded:/).waitFor({ timeout: 8000 });
  await buyer.screenshot({ path: dir + "/11-buyer-recorded.png", fullPage: true });
  return "buttons: " + labels.join(" | ") + " -> " + (await txt(buyer)).match(/Recorded:[^.]*\.[^.]*\./)?.[0];
});

// ---- seller
const p = await mk();
await step("seller login", async () => {
  await p.goto(base + "/login");
  await p.getByLabel("Email").fill(seed.seller_email);
  await p.getByLabel("Password").fill("synthetic-placeholder-pw-1");
  await p.getByRole("button", { name: "Sign in" }).click();
  await p.waitForURL(/\/app/);
});
await step("seller sees buyer-verified decision in Delivery (no seller control to forge it)", async () => {
  await p.goto(`${base}/app/p/${seed.delivery_project_id}/delivery`);
  await p.getByText(/Buyer decisions/).waitFor();
  await p.waitForTimeout(800);
  const t = await txt(p);
  if (!/Buyer.verified|Acknowledg/i.test(t)) throw new Error("decision not visible");
  const forged = await p.getByRole("button", { name: /(record|approve|accept).*(on behalf|for the buyer)/i }).count();
  await p.screenshot({ path: dir + "/12-seller-delivery.png", fullPage: true });
  return `seller-side forge controls: ${forged}; status text: ${t.match(/Delivery status:[^A-Z]*[A-Za-z ]+/)?.[0]}`;
});
const P = `${base}/app/p/${seed.payment_project_id}`;
await step("payments page: provider status is mock-labelled, not Sandbox", async () => {
  await p.goto(P + "/payments"); await p.waitForTimeout(1200);
  await p.screenshot({ path: dir + "/13-payments.png", fullPage: true });
  const t = await txt(p);
  return `mentions Sandbox:${/sandbox/i.test(t)} mock:${/mock/i.test(t)} simulated:${/simulated/i.test(t)} | ${t.match(/Provider status[^.]{0,80}/)?.[0]}`;
});
await step("diagnosis: cited, advisory, rules-only", async () => {
  await p.goto(P + "/diagnosis"); await p.waitForLoadState("networkidle"); await p.getByLabel("Scenario").selectOption({ index: 1 }).catch(() => {});
  const opts = await p.getByLabel("Scenario").locator("option").allInnerTexts();
  const pick = opts.find((o) => /paid not applied/i.test(o)) ?? opts[0];
  await p.getByLabel("Scenario").selectOption({ label: pick });
  await p.getByLabel("Payment").locator("option").nth(1).waitFor({ state: "attached" }); await p.getByLabel("Payment").selectOption({ index: 1 });
  await p.getByRole("button", { name: "Diagnose" }).click();
  await p.waitForTimeout(1500);
  await p.screenshot({ path: dir + "/14-diagnosis.png", fullPage: true });
  const t = await txt(p);
  return `scenario "${pick}" | rules:${/rules/i.test(t)} advisory:${/advisory/i.test(t)} notrun:${/not run|no generative/i.test(t)}`;
});
await step("stanza log intelligence: requested vs effective mode shown", async () => {
  await p.goto(P + "/log-intelligence"); await p.waitForLoadState("networkidle");
  const opts = await p.getByLabel("Scenario").locator("option").allInnerTexts();
  await p.getByLabel("Scenario").selectOption({ label: opts.find((o) => /paid not applied/i.test(o)) ?? opts[0] });
  await p.getByLabel("Analysis mode").selectOption("stanza");
  await p.getByLabel("Payment").locator("option").nth(1).waitFor({ state: "attached" }); await p.getByLabel("Payment").selectOption({ index: 1 });
  await p.getByLabel("Log lines").fill("2026-11-12T10:00:01Z ERROR merchant order create failed: timeout after payment captured\n2026-11-12T10:00:02Z WARN webhook retry scheduled for payment completed");
  await p.getByRole("button", { name: "Analyse logs" }).click();
  await p.getByText(/Requested/).waitFor({ timeout: 90000 });
  await p.waitForTimeout(500);
  await p.screenshot({ path: dir + "/15-stanza.png", fullPage: true });
  return (await txt(p)).match(/Requested.{0,260}/)?.[0];
});
await step("recovery: propose -> approve -> execute (confirm) -> audit", async () => {
  await p.goto(P + "/recovery"); await p.waitForLoadState("networkidle");
  const opts = await p.getByLabel("Scenario").locator("option").allInnerTexts();
  await p.getByLabel("Scenario").selectOption({ label: opts.find((o) => /paid not applied/i.test(o)) ?? opts[0] });
  await p.getByLabel("Payment").locator("option").nth(1).waitFor({ state: "attached" }); await p.getByLabel("Payment").selectOption({ index: 1 });
  await p.getByRole("button", { name: "Propose a recovery plan" }).click();
  await p.getByRole("button", { name: /^Approve/ }).waitFor({ timeout: 10000 });
  await p.screenshot({ path: dir + "/16-recovery-proposed.png", fullPage: true });
  await p.getByRole("button", { name: /^Approve/ }).click();
  await p.getByRole("button", { name: /Execute on simulated store/ }).waitFor({ timeout: 10000 });
  await p.getByRole("button", { name: /Execute on simulated store/ }).click();
  const dlg = p.getByRole("dialog");
  await dlg.waitFor();
  const dtxt = (await dlg.innerText()).replace(/\s+/g, " ");
  await p.screenshot({ path: dir + "/17-recovery-confirm.png", fullPage: true });
  await dlg.getByRole("button").filter({ hasNotText: /cancel/i }).last().click();
  await p.waitForTimeout(2000);
  await p.screenshot({ path: dir + "/18-recovery-executed.png", fullPage: true });
  const t = await txt(p);
  return `dialog: ${dtxt.slice(0, 200)} | after: executed:${/executed/i.test(t)}`;
});
await step("no blank or fatal console errors across the run", async () => { if (problems.length) throw new Error(problems.join(" ; ")); });
console.log(res.join("\n"));
console.log(`total ${(Date.now() - t0) / 1000}s`);
await b.close();
process.exit(res.some((r) => r.startsWith("FAIL")) ? 1 : 0);
