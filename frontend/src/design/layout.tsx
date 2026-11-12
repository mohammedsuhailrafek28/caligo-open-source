import { useQuery } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { api } from "../api/client";
import type { ReactNode } from "react";
import { Link, NavLink, Outlet, useParams } from "react-router-dom";
import { useAuth } from "../auth";
import { Button, cx } from "./ui";

export function PageHeader({ eyebrow, title, children, actions }: { eyebrow?: string; title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-4 border-b border-stone pb-6">
      <div className="max-w-2xl">
        {eyebrow && <p className="eyebrow mb-2">{eyebrow}</p>}
        <h1 className="font-display text-4xl leading-tight">{title}</h1>
        {children && <p className="mt-3 text-ink-soft">{children}</p>}
      </div>
      {actions && <div className="flex gap-3">{actions}</div>}
    </header>
  );
}

export function SectionHeader({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 mt-10 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 className="font-display text-2xl">{title}</h2>
        {children && <p className="mt-1 max-w-prose text-sm text-ink-soft">{children}</p>}
      </div>
      {actions}
    </div>
  );
}

interface NavGroup { heading: string; items: Array<{ to: string; label: string }> }

export function navGroups(pid: string | undefined): NavGroup[] {
  const p = pid ? `/app/p/${pid}` : null;
  const groups: NavGroup[] = [{ heading: "Workspace", items: [{ to: "/app", label: "Overview" }, { to: "/app/projects", label: "Projects" }] }];
  if (!p) return groups;
  return [
    ...groups,
    { heading: "Agreement & delivery", items: [{ to: `${p}/scope`, label: "Scope" }, { to: `${p}/delivery`, label: "Delivery evidence" }] },
    { heading: "Payment assurance", items: [
      { to: `${p}/payments`, label: "Payments" }, { to: `${p}/payees`, label: "Payee registry" },
      { to: `${p}/screening`, label: "Screening" }, { to: `${p}/reconciliation`, label: "Reconciliation" }] },
    { heading: "Investigations", items: [
      { to: `${p}/diagnosis`, label: "Diagnosis" }, { to: `${p}/log-intelligence`, label: "Log intelligence" }, { to: `${p}/health`, label: "Integration health" }] },
    { heading: "Recovery & records", items: [
      { to: `${p}/recovery`, label: "Recovery" }, { to: `${p}/reports`, label: "Evidence reports" }, { to: `${p}/audit`, label: "Audit trail" }] },
  ];
}

export function Sidebar({ pid }: { pid?: string }) {
  const groups = navGroups(pid);
  return (
    <nav aria-label="Workspace" className="w-60 shrink-0 border-r border-stone bg-paper px-5 py-6">
      {groups.map((g) => (
        <div key={g.heading} className="mb-6">
          <p className="eyebrow mb-2">{g.heading}</p>
          <ul className="space-y-0.5">
            {g.items.map((i) => (
              <li key={i.to}>
                <NavLink
                  to={i.to}
                  end={i.to === "/app"}
                  className={({ isActive }) => cx("block rounded px-2 py-1.5 text-sm", isActive ? "bg-white font-medium text-ink shadow-[inset_2px_0_0_rgb(var(--c-terracotta))]" : "text-ink-soft hover:text-ink")}
                >
                  {i.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {!pid && <p className="text-xs text-ink-soft">Open a project to reach its scope, payments, investigations and records.</p>}
    </nav>
  );
}

export function TopNavigation() {
  const { seller, logout } = useAuth();
  return (
    <div className="flex items-center justify-between border-b border-stone bg-parchment px-6 py-3">
      <Link to="/app" className="font-display text-2xl">Caligo</Link>
      <div className="flex items-center gap-4 text-sm">
        <span className="text-ink-soft">{seller?.display_name ?? seller?.email}</span>
        <Button variant="quiet" onClick={() => void logout()}><LogOut aria-hidden className="h-4 w-4" />Sign out</Button>
      </div>
    </div>
  );
}

/** States what the connected backend can and cannot be. Comes from /capabilities, never assumed. */
export function EnvironmentBanner() {
  const caps = useQuery({ queryKey: ["capabilities"], queryFn: () => api.get<Record<string, boolean>>("/api/v1/capabilities") });
  if (!caps.data) return null;
  const sandbox = caps.data.paypal_sandbox_invoicing === true;
  const mock = caps.data.mock_paypal_invoicing === true;
  if (!sandbox && !mock) return <p role="note" className="border-b border-stone bg-paper px-8 py-2 text-sm">No payment provider is connected. Payment records cannot be created.</p>;
  return (
    <p role="note" className="border-b border-stone bg-paper px-8 py-2 text-sm">
      <strong>{sandbox ? "PayPal Sandbox mode." : "Simulated mode."}</strong>{" "}
      {sandbox
        ? "Payments come from PayPal's test environment, not live money. Merchant-store events are simulated."
        : "Payments, provider events and the merchant store are all simulated by a mock provider. Nothing here is PayPal evidence."}
    </p>
  );
}

export function AppShell() {
  const { pid } = useParams();
  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:p-3">Skip to content</a>
      <TopNavigation />
      <EnvironmentBanner />
      <div className="flex flex-1">
        <Sidebar pid={pid} />
        <main id="main" className="mx-auto w-full max-w-5xl px-8 py-10"><Outlet /></main>
      </div>
    </div>
  );
}
