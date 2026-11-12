import { describe, expect, it } from "vitest";
import { providerLabel } from "./lib/format";

describe("providerLabel", () => {
  it("marks the mock provider as simulated and not PayPal", () => {
    const l = providerLabel("MOCK_PAYPAL");
    expect(l.simulated).toBe(true);
    expect(l.text).toMatch(/not PayPal/i);
  });

  it("labels Sandbox as a test environment and never claims authentication or live money", () => {
    const l = providerLabel("PAYPAL_SANDBOX");
    expect(l.simulated).toBe(false);
    expect(l.text).toMatch(/test environment/i);
    expect(l.text).not.toMatch(/authenticated|verified/i);
  });

  it("does not upgrade an unknown source", () => {
    expect(providerLabel("SOMETHING_ELSE").text).toBe("Something else");
  });
});
