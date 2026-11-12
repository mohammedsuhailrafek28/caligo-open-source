export function minorDigits(currency: string): number {
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits ?? 2;
  } catch {
    return 2;
  }
}

/** Money is integer minor units everywhere; this only formats for display. */
export function formatMoney(minor: number | null | undefined, currency: string): string {
  if (minor === null || minor === undefined) return "—";
  const digits = minorDigits(currency);
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(minor / 10 ** digits);
  } catch {
    return `${minor} ${currency} (minor units)`;
  }
}

/** Parse "1,200.50" into integer minor units without floating point. Returns null when it is not a valid amount. */
export function parseMinor(input: string, currency: string): number | null {
  const digits = minorDigits(currency);
  const clean = input.trim().replace(/,/g, "");
  const match = /^(\d{1,9})(?:\.(\d+))?$/.exec(clean);
  if (!match) return null;
  const fraction = match[2] ?? "";
  if (fraction.length > digits) return null;
  const minor = Number(match[1]) * 10 ** digits + Number(fraction.padEnd(digits, "0") || 0);
  return minor >= 1 && minor <= 2_000_000_000 ? minor : null;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function shortHash(value: string | null | undefined, length = 10): string {
  return value ? `${value.slice(0, length)}…` : "—";
}

/** Where a payment record came from. Never upgrades a source: a Sandbox record is test-environment evidence, not live money. */
export function providerLabel(provider: string): { text: string; simulated: boolean } {
  if (provider === "MOCK_PAYPAL") return { text: "Simulated mock provider, not PayPal", simulated: true };
  if (provider === "PAYPAL_SANDBOX") return { text: "PayPal Sandbox adapter (test environment, not live money)", simulated: false };
  return { text: humanize(provider), simulated: false };
}

export function humanize(code: string): string {
  const s = code.replace(/[_-]+/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}
