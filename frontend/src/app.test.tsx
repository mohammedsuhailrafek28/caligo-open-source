import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { AuthProvider } from "./auth";
import { ConfirmationDialog } from "./design/feedback";
import { StatusLabel } from "./design/ui";
import { formatMoney, parseMinor } from "./lib/format";
import { parseRequirements } from "./pages/ScopePage";

function mockFetch(handler: (url: string, init?: RequestInit) => { status?: number; body: unknown }) {
  vi.stubGlobal("fetch", vi.fn(async (input: URL | string, init?: RequestInit) => {
    const { status = 200, body } = handler(String(input), init);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }));
}
function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></AuthProvider></QueryClientProvider>);
}
afterEach(() => vi.unstubAllGlobals());

describe("money", () => {
  it("parses decimal strings into integer minor units without floats", () => {
    expect(parseMinor("1,200.50", "USD")).toBe(120050);
    expect(parseMinor("0.1", "USD")).toBe(10);
    expect(parseMinor("19.999", "USD")).toBeNull();
    expect(parseMinor("-5", "USD")).toBeNull();
    expect(parseMinor("abc", "USD")).toBeNull();
    expect(parseMinor("500", "JPY")).toBe(500);
  });
  it("formats minor units", () => expect(formatMoney(120050, "USD")).toBe("$1,200.50"));
});

describe("scope requirement parsing", () => {
  it("turns lines into requirement drafts", () => {
    expect(parseRequirements("home: Homepage - hero and nav\nBlog")).toEqual([
      { key: "HOME", title: "Homepage", description: "hero and nav" },
      { key: "BLOG", title: "Blog", description: "" },
    ]);
  });
});

describe("design primitives", () => {
  it("StatusLabel always carries text, not colour alone", () => {
    render(<StatusLabel tone="attention">Needs review</StatusLabel>);
    expect(screen.getByText("Needs review")).toBeInTheDocument();
  });
  it("ConfirmationDialog calls the right handler", async () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ConfirmationDialog open title="Sure?" confirmLabel="Do it" onConfirm={onConfirm} onCancel={onCancel}>body</ConfirmationDialog>);
    await userEvent.click(screen.getByRole("button", { name: "Do it", hidden: true }));
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onCancel).not.toHaveBeenCalled();
  });
});

describe("routes", () => {
  it("landing shows the headline and real CTAs", async () => {
    mockFetch(() => ({ status: 401, body: { error: { code: "unauthorized", message: "no" } } }));
    renderAt("/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Know what happened. Prove what happened next.");
    expect(await screen.findByRole("link", { name: "Explore Caligo" })).toHaveAttribute("href", "/register");
    for (const link of screen.getAllByRole("link", { name: "How it works" })) expect(link).toHaveAttribute("href", "#how");
    expect(document.getElementById("how")).not.toBeNull();
    for (const stage of ["Agree", "Deliver", "Investigate", "Recover"]) expect(screen.getByRole("heading", { name: stage })).toBeInTheDocument();
  });
  it("redirects anonymous visitors from the workspace to sign in", async () => {
    mockFetch(() => ({ status: 401, body: { error: { code: "unauthorized", message: "no" } } }));
    renderAt("/app/projects");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
  it("never sends demo identity headers and shows the project list when signed in", async () => {
    const seen: Array<RequestInit | undefined> = [];
    mockFetch((url, init) => {
      seen.push(init);
      if (url.includes("/auth/me")) return { body: { seller: { id: "s1", email: "a@example.org", display_name: "Ada" }, expires_at: "2099-01-01T00:00:00Z" } };
      if (url.includes("/api/v1/projects")) return { body: [{ id: "p1", title: "Brand site", status: "DRAFT", created_at: "2026-01-01T00:00:00Z" }] };
      return { body: {} };
    });
    renderAt("/app/projects");
    expect(await screen.findByRole("link", { name: "Brand site" })).toBeInTheDocument();
    for (const init of seen) {
      const keys = Object.keys((init?.headers as Record<string, string>) ?? {}).map((k) => k.toLowerCase());
      expect(keys).not.toContain("x-user");
      expect(keys).not.toContain("x-role");
      expect(init?.credentials).toBe("include");
    }
  });
  it("project navigation links only to implemented pages", async () => {
    mockFetch((url) => (url.includes("/auth/me") ? { body: { seller: { id: "s", email: "a@b.co", display_name: "Ada" }, expires_at: "2099-01-01T00:00:00Z" } } : { body: [] }));
    renderAt("/app/p/p1/scope");
    await screen.findByRole("heading", { name: "Scope", level: 1 });
    const hrefs = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    for (const h of ["scope", "delivery", "payments", "payees", "screening", "reconciliation", "diagnosis", "log-intelligence", "health", "recovery", "reports", "audit"]) {
      expect(hrefs).toContain(`/app/p/p1/${h}`);
    }
  });
});
