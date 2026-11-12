# Demo script (target: 2 min 50 s)

Everything shown is the real Caligo app running locally against a **mock** provider with synthetic data. Say that out loud; do not imply anything on screen is live PayPal. Timings are estimates: rehearse once with a stopwatch and trim the Stanza wait.

## Preparation checklist (do these before recording)

1. Fresh seeded backend and frontend per take (the buyer token is single-use and recovery executes once). Commands are in `README.md` and `docs/DEMO_GUIDE.md`; use `--stanza-model-dir` so Stanza is real.
2. Stanza timing: in every rehearsal the first Stanza analysis on a fresh server took about 14 s (model load). Whether later analyses are faster was not measured. Budget 14 s on screen; if you need to trim, cut the idle wait in editing and keep the real result visible. Never paste or edit in a result you did not obtain.
3. Two browser windows: seller (signed in) and buyer (signed out, `/buyer?token=<printed token>`). Keep `<state dir>/outbox.jsonl` open to copy the one-time code.
4. Browser zoom 110 to 125 percent, notifications off, a clean window with no personal tabs or bookmarks, no terminal showing paths or tokens.
5. Have the repository README open on the "What is real and what is not" table for the last beat.
6. Check the banner reads "Simulated mode" and `GET /health` reports `paypal_mode: mock`.
7. Record at 1080p. Check the finished video for personal information before uploading (sign-in name, emails, file paths, other tabs).

## Click-by-click with narration

**1. Real app and target user (0:00 to 0:15)**
Show `/` (landing). *"Caligo is a financial evidence workspace for independent developers and small studios. When a client disputes scope, delivery or payment, you need evidence, not memory. Every record says where it came from."* Point at the labelled illustrative sample. Click **Sign in**.

**2. Scope and delivery evidence (0:15 to 0:40)**
Sign in as the seller. Say: *"The banner says simulated mode: nothing here is PayPal evidence."* Open the project **SYNTHETIC delivery review** > **Scope**: show the frozen baseline (HOME, BLOG). Open **Delivery evidence**: show the artifact with its server-computed hash and the "Addresses: HOME" link. *"The server computes the hash, so the seller cannot claim a different file."*

**3. Buyer acknowledgment (0:40 to 1:05)**
Switch to the buyer window. Click **Send me a code**, paste the code from `outbox.jsonl`, **Verify code**, choose **Acknowledge delivery**. *"The buyer proves control of their mailbox with a one-time code. That is mailbox control, not legal identity."* Back in the seller window refresh Delivery evidence: *"Status is Acknowledged only. It is not accepted, and there is no seller button to record a buyer decision."*

**4. Payment investigation and provenance (1:05 to 1:35)**
Open **SYNTHETIC paid, merchant failed** > **Payments**. *"This payment reads Paid, and the source line says it is a simulated mock provider, not PayPal."* Open **Diagnosis**: scenario **Paid not applied**, pick the payment, **Diagnose**. Point at: Advisory; Mode: Rules only; generative model not used; the cited evidence rows with `SIMULATED_MOCK_PROVIDER` for the payment and `SIMULATED_MERCHANT_STATE` for the order. *"Every claim cites a record, and every record is labelled. The diagnosis cannot authorize anything."*

**5. Real Stanza inference (1:35 to 1:55)**
**Log intelligence**: mode **Stanza (local NLP)**, scenario **Paid not applied**, paste two log lines (for example `2026-11-12T10:00:01Z ERROR merchant order create failed: timeout after payment captured`), **Analyse logs**. Point at *Requested Stanza, effective Stanza*. *"This is Stanford Stanza running locally from a model cache. It is a language model, not a generative one: it only suggests, the logs are treated as untrusted, and if Stanza were unavailable it would say it fell back to rules."*

**6. Human-approved local recovery (1:55 to 2:30)**
**Recovery** > **Propose a recovery plan** (Paid not applied) > read the dry-run: *"Nothing has changed."* Click **Approve this exact plan**, then **Execute on simulated store**. Read the dialog: *"This changes the simulated store only. It does not refund, cancel or capture anything at PayPal."* Confirm. Show *Executed on the simulated store* and open **Plan history**. *"A human approved this exact version. There is no refund, cancel or payout anywhere in the code."*

**7. Simulated versus authenticated Sandbox (2:30 to 2:50)**
Show the README table. *"In this demo every payment is simulated. Caligo has a PayPal Sandbox adapter with a strict rule: only a fresh, authenticated Sandbox read can earn the 'authenticated Sandbox' label, and mock records can never carry it. Our real Sandbox contact so far was limited to one draft invoice that was never sent or paid, so nothing in this demo is Sandbox evidence, and we say so rather than imply it."*

## If something goes wrong on camera

- Stanza slow: cut the wait in editing; never paste a result you did not obtain.
- Recovery button missing: you reused a server. Restart with a new `--state-dir`.
- Buyer code rejected: request a new code (rate limits are per minute); the latest line of `outbox.jsonl` is the current code.

## What the video must not claim

No live PayPal evidence; no payment verified in Sandbox; no generative AI; no money movement; no legal identity of the buyer; no accessibility or production readiness.
