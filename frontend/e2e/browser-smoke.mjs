// Browser smoke: landing, login, buyer portal, register -> dashboard, project, hard refresh of nested routes. Usage: node e2e/browser-smoke.mjs <frontendUrl> <screenshotDir>
// Needs `npm i --no-save playwright-core` and Chrome (or CHROME_PATH=<chromium exe>). Backend must run in mock mode.
import { chromium } from "playwright-core";
const base = process.argv[2] ?? "http://localhost:3000";
const dir = process.argv[3] ?? ".";
const b = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
const p = await (await b.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
const problems = [];
const net = [];
p.on("console", (m) => { if (m.type() === "error") problems.push(`console.error: ${m.text()} @ ${p.url()}`); });
p.on("pageerror", (e) => problems.push(`PAGEERROR: ${e.message}`));
p.on("requestfailed", (r) => problems.push(`REQFAIL ${r.url()} ${r.failure()?.errorText}`));
p.on("response", (r) => { if (r.url().includes("/api/") || r.url().includes("/health")) net.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
const checks = [];
const check = async (name, fn) => { try { const d = await fn(); checks.push(`PASS ${name}${d ? " :: " + d : ""}`); } catch (e) { checks.push(`FAIL ${name} :: ${e.message.split("\n")[0]}`); } };
const bodyLen = async () => (await p.innerText("body")).trim().length;
const shot = (n) => p.screenshot({ path: `${dir}/${n}.png`, fullPage: true });

await check("landing renders", async () => { await p.goto(base + "/"); await p.getByRole("heading", { name: /Know what happened/ }).waitFor({ timeout: 8000 }); await shot("01-landing"); });
await check("login page loads", async () => { await p.goto(base + "/login"); await p.getByLabel("Email").waitFor({ timeout: 8000 }); await shot("02-login"); });
await check("buyer portal renders at /buyer", async () => { await p.goto(base + "/buyer"); await p.waitForTimeout(800); const n = await bodyLen(); if (n < 50) throw new Error("body text length " + n); await shot("03-buyer"); return `body chars ${n}`; });
const stamp = Date.now();
await check("register -> dashboard renders", async () => {
  await p.goto(base + "/register");
  await p.getByLabel("Display name").fill("Browser Check");
  await p.getByLabel("Email").fill(`browser-${stamp}@example.org`);
  await p.getByLabel("Password").fill(`pw-${stamp}-correct-horse`);
  await p.getByRole("button", { name: "Create account" }).click();
  await p.waitForURL(/\/(app|login)/, { timeout: 10000 });
  if (/\/login/.test(p.url())) { // registration may require explicit sign-in
    await p.getByLabel("Email").fill(`browser-${stamp}@example.org`);
    await p.getByLabel("Password").fill(`pw-${stamp}-correct-horse`);
    await p.getByRole("button", { name: "Sign in" }).click();
    await p.waitForURL(/\/app/, { timeout: 10000 });
  }
  await p.waitForTimeout(1000);
  await shot("04-dashboard");
  return `${p.url()} body chars ${await bodyLen()}`;
});
let projectUrl = "";
await check("create project via UI", async () => {
  await p.goto(base + "/app/projects");
  await p.waitForTimeout(800);
  const title = p.getByLabel(/title|name/i).first();
  await title.fill("Browser check project");
  await p.getByRole("button", { name: /create|add|new/i }).first().click();
  await p.waitForURL(/\/app\/p\//, { timeout: 8000 });
  projectUrl = p.url();
  await p.waitForTimeout(800);
  await shot("06-project-scope");
  return projectUrl;
});
await check("hard refresh on nested route is not blank", async () => {
  for (const sub of ["scope", "delivery", "payments", "reconciliation", "audit"]) {
    await p.goto(projectUrl.replace(/\/[^/]*$/, "/") + sub);
    await p.waitForTimeout(900);
    const n = await bodyLen();
    if (n < 80) throw new Error(`${sub} blank (${n} chars)`);
  }
  await p.reload(); await p.waitForTimeout(900);
  const n = await bodyLen(); if (n < 80) throw new Error("blank after reload");
  await shot("07-nested-refresh");
  return `reloaded ${p.url()} (${n} chars)`;
});
await check("every project page mounts", async () => {
  const bad = [];
  for (const sub of ["scope","delivery","payments","payees","screening","reconciliation","diagnosis","log-intelligence","health","recovery","reports","audit"]) {
    await p.goto(projectUrl.replace(/\/[^/]*$/, "/") + sub); await p.waitForTimeout(700);
    if ((await bodyLen()) < 80) bad.push(sub);
  }
  if (bad.length) throw new Error("blank: " + bad.join(","));
});
await check("unknown deep link while signed-in does not blank", async () => { await p.goto(base + "/nope/deep/link"); await p.waitForTimeout(900); if ((await bodyLen()) < 50) throw new Error("blank"); });
console.log(checks.join("\n"));
console.log("\nNETWORK:\n" + [...new Set(net)].join("\n"));
console.log("\nPROBLEMS (console errors / page errors / failed requests):\n" + (problems.length ? [...new Set(problems)].join("\n") : "none"));
await b.close();
process.exit(checks.some((c) => c.startsWith("FAIL")) ? 1 : 0);
