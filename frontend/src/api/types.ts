import type { components } from "./schema";

/** Authoritative response/request types, generated from contracts/openapi.json (`npm run gen:api`). Never hand-edit schema.d.ts. */
export type S = components["schemas"];
export type Project = S["ProjectView"];
export type ProjectDetail = S["ProjectDetail"];
export type Payment = S["PaymentLink"];
