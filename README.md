# Caligo

A financial evidence workspace for independent developers and small studios. It keeps the agreed scope, the delivery evidence and the payment record in one place, and labels every record with where it came from, so a dispute can be answered with evidence instead of memory.

> Built for the PayPal AI Hackathon. Read "What is real and what is not" before judging what you see.

## What it does

- **Scope:** record the agreement as written, freeze it into hashed, versioned baselines. A change is only a proposal until the buyer decides.
- **Delivery:** register what was delivered (the server computes the SHA-256). The buyer acknowledges, accepts or disputes from their own mailbox with a one-time code. That is evidence of mailbox control, not legal identity, and no seller screen can record or forge a buyer decision.
- **Payments:** payment status comes from provider evidence, never from a claim. A draft invoice is never shown as paid. A changed payment instruction puts a hold on Caligo's payment operations until the buyer verifies it.
- **Payee registry and screening:** seller-asserted payee destinations and advisory screening of messages. A registry entry is the seller's word, and no screening result can authorize a payment.
- **Investigation:** reconcile claims against provider evidence; cite-or-refuse diagnosis of divergences; optional log analysis with Stanza.
- **Guarded recovery:** a human reviews a dry-run, approves that exact plan, and executes it against a **simulated** merchant store only. There is no refund, cancel, capture or payout in the code.
- **Reports and audit:** deterministic evidence reports and an append-only audit trail.

## What is real and what is not

| | Status |
|---|---|
| Payments and provider events in the demo | **Simulated** by an in-process mock provider. The UI says so on every page and on each payment. |
| Merchant store | **Simulated.** Recovery changes only this. |
| PayPal Sandbox | A Sandbox adapter exists and is tested against a fake PayPal. Real contact has been limited and separately approved: one synthetic draft invoice was created and read back twice as DRAFT (never sent, never paid); the later read passed 10 of 11 checks and failed the merchant-identity check. **No Sandbox payment has been verified as paid, no record carries the "authenticated Sandbox" label, and the demo does not show live Sandbox evidence.** That label can only be earned by a fresh, allowlisted, authenticated read in the controlled runner, which has never made a real request; mock and unattested records can never carry it. There is no live PayPal mode. Details: `docs/PAYPAL_SANDBOX_STATUS.md`. |
| Stanza | Real local NLP inference when you install it (below). Advisory and provenance-labelled. It is not a generative model. |
| Generative AI | **None.** No OpenAI, Anthropic, Google or other hosted model, no API key, no chatbot. The code has an unused optional hook for a model transport (default off, never wired) and an unused `AI_API_KEY` setting. |

## Quick start (mock mode, no accounts, no credentials)

Needs Python 3.11+ and Node 20+. The first install downloads packages from PyPI and npm.

```
python -m venv .venv
.venv/Scripts/python -m pip install -e "./backend[dev]"        # POSIX: .venv/bin/python
cd frontend && npm install && cd ..
```

Seeded demo backend (synthetic data, mock provider, fake mailer; the state directory must be new, empty and outside the repository):

```
.venv/Scripts/python scripts/demo_seeded_server.py --port 8011 --origin http://localhost:3001 --state-dir <new empty dir>
cd frontend && CALIGO_API_URL=http://127.0.0.1:8011 npx vite --port 3001 --strictPort
```

Open http://localhost:3001 and sign in as `demo-seller@example.com` / `synthetic-placeholder-pw-1` (a synthetic account that exists only in your local demo database). The full walk-through, with what has and has not been verified, is in `docs/DEMO_GUIDE.md` and `docs/DEMO_SCRIPT.md`. Developer setup and checks: `docs/DEV_SETUP.md`.

Run the app against your own empty database instead with `PAYPAL_MODE=mock BUYER_MAIL_MODE=fake AI_MODE=demo` (see `.env.example` and `frontend/README.md`).

## Optional: Stanford Stanza log analysis

Caligo works fully without Stanza. With the default deterministic analysis, the log-analysis screen shows requested mode, effective mode and the reason for any fallback. To enable real Stanza inference you install it yourself:

```
.venv/Scripts/python -m pip install torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/Scripts/python -m pip install -r backend/nlp_benchmark/requirements.txt
.venv/Scripts/python -m app.log_intelligence.setup_models --download --cache <cache dir outside the repo> --manifest-out contracts/log-intelligence/model-manifest.json
```

Run these from the repository root. This downloads the pinned English model files (not committed, hash-verified; needs at least 1 GiB of free RAM). Then start the seeded server with `--stanza-model-dir <cache dir>` or set `STANZA_MODEL_DIR`. If the cache is missing, mismatched or short of memory, Stanza **fails closed** and the service falls back to the deterministic rules; the response says so and it is never described as Stanza. Details: `docs/STANZA_LOG_INTELLIGENCE.md` and licences in `docs/STANZA_THIRD_PARTY.md`.

## Tests and checks

- Backend: `cd backend && python -m pytest -q` (offline; the network guard blocks non-loopback sockets).
- Frontend: `cd frontend && npx tsc --noEmit && npx vitest run && npm run build`.
- API E2E against a running mock backend: `node frontend/e2e/api-e2e.mjs`.
- Browser E2E (optional): `frontend/e2e/browser-smoke.mjs` and `frontend/e2e/browser-demo.mjs` (need `npm i --no-save playwright-core` and Chrome).
- Secrets: `python scripts/check_secrets.py --git`.

## Known limitations

- Demo payments are simulated; see the table above. Authenticated Sandbox evidence is not displayed in the app.
- Buyer approval of a payment-instruction change needs an enrolled authenticator, and there is no seller-side enrolment endpoint; that flow is covered by backend tests but not demonstrated in the browser.
- Seller sessions last 8 hours; unsaved form text is lost on expiry.
- Not audited for accessibility; Chromium only has been exercised for the browser E2E. `npm audit` reports 2 moderate advisories in react-router 6.x (open redirect and an SSR issue; navigation here is internal and there is no SSR). The fix is a breaking major upgrade.
- The mock and Sandbox adapters are not a production payment integration. Do not point this at real money.

## Acknowledgments

- **Mohammed Suhail Rafek** ([mohammedsuhailrafek28](https://github.com/mohammedsuhailrafek28)): project author.
- **Swathi** ([swtzzz](https://github.com/swtzzz)): the payer-side payment-instruction integrity prototype (MIT) whose supplier-registry, destination-history and screening ideas were adapted into the payee registry. See `THIRD_PARTY_NOTICES.md` and `docs/TEAMMATE_INTEGRATION.md`.
- **Stanford NLP Group**: Stanza.
- Development used AI coding assistants (Claude Code and Codex) under the author's direction; behaviour is covered by the tests above.

## Licence

MIT, see `LICENSE`. Portions of the payee registry are adapted from Swathi's MIT-licensed prototype (Copyright (c) 2026 Swathi); her complete original notice and the list of adapted files are in `THIRD_PARTY_NOTICES.md`, which must be kept with any copy of those portions. Not affiliated with or endorsed by PayPal.
