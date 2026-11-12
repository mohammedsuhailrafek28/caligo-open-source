# Caligo frontend

React 18 + TypeScript + Vite + Tailwind 3 + React Router 6 + TanStack Query 5. It talks only to the Caligo backend (`/api/v1`), using the session cookie (`pem_session`, HttpOnly) and double-submit CSRF (`X-CSRF-Token`). There are no `X-User`/`X-Role` headers, no mock data and no hosted-AI calls.

## Run locally (safe: mock PayPal, fake mailer, deterministic diagnosis)

Backend (from `backend/`, any free port; example uses 8010):

```
PAYPAL_MODE=mock BUYER_MAIL_MODE=fake AI_MODE=demo APP_ENV=development \
CORS_ORIGINS=http://localhost:3000 DATABASE_URL=sqlite:///./local_dev.sqlite3 \
DEV_OUTBOX_FILE=./dev_outbox/outbox.jsonl \
python -m uvicorn app.main:app --port 8010
```

Frontend (from `frontend/`):

```
npm install
CALIGO_API_URL=http://127.0.0.1:8010 npm run dev      # http://localhost:3000, proxies /api and /health
```

Environment variable names: backend `PAYPAL_MODE`, `BUYER_MAIL_MODE`, `AI_MODE`, `APP_ENV`, `CORS_ORIGINS`, `DATABASE_URL`, `DEV_OUTBOX_FILE`; frontend dev server `CALIGO_API_URL` (default `http://127.0.0.1:8000`). The frontend reads no secrets and bakes none into the bundle.

Demo: open `/`, choose **Explore Caligo**, register a synthetic seller, create a project, then work down the sidebar. The buyer's emailed link and one-time code are written to the backend's `DEV_OUTBOX_FILE` (local fake mailer, delete after use); the buyer portal is `/buyer`.

## Scripts

| Command | Purpose |
|---|---|
| `npm run dev` / `npm run build` | dev server / production build |
| `npx tsc --noEmit` | typecheck |
| `npm test` (`vitest run`) | component and unit tests |
| `node e2e/api-e2e.mjs [baseUrl]` | API-level E2E through the dev proxy, using real session + CSRF (needs a running backend in mock mode) |
| `npm run gen:api` | regenerate `src/api/schema.d.ts` from `contracts/openapi.json` (never edit by hand) |

## Structure

`src/api` typed client + generated schema, `src/auth.tsx` session state, `src/design` the design system (tokens in `src/index.css`), `src/pages` one file per product area, `e2e/` the API E2E.

See `docs/FRONTEND_COVERAGE.md` for the API-to-UI matrix, the LLM-isolation note and honest limitations.

## Troubleshooting

- **Blank white page, console shows `__WS_TOKEN__ is not defined` from `/@vite/client`:** the dev server is older than the installed Vite (for example `npm install` or `npm audit fix` ran while it was running). Stop the dev server and start it again. A successful `npm run build` does not prove the dev server is healthy; check the page in a real browser.
