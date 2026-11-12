# Third-party notices

Caligo's own code is MIT licensed (see `LICENSE`). This file records everything that is not original to Caligo, how it relates to this repository, and the notices those licences require. Licences were read from the installed package metadata on 12 Nov 2026 (versions below are those verified); re-check them if you upgrade.

**This repository vendors no third-party source code, fonts, images, datasets or model weights.** Dependencies are installed by `pip` and `npm`; the only third-party text in the repository is the notices below.

## 1. Adapted from a contributor's repository: `swtzzz/Caligo`

Caligo's payee registry and screening features adapt the design of a teammate's prototype, `https://github.com/swtzzz/Caligo` (checked at commit `ed5d3efee5046d1ae6a60f0d9dc76000da1dd3e7`), a payer-side payment-instruction integrity tool. That repository is MIT licensed, Copyright (c) 2026 Swathi. The notice is reproduced in full:

```
MIT License

Copyright (c) 2026 Swathi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Portions derived from that work (the ideas of a supplier registry with approved destinations, destination history, and rules for changed or unknown destinations and urgency or bank-change wording), as implemented here:

- `backend/app/payee_models.py`, `backend/app/services/payee_registry.py`, `backend/app/services/bec_signals.py`, `backend/app/routers/payees.py`
- `backend/migrations/versions/0008_payee_destination_registry.py`, `backend/tests/test_payee_registry.py`
- `frontend/src/pages/PayeesPages.tsx`

`docs/TEAMMATE_INTEGRATION.md` is the record of what was adapted, replaced or deliberately not imported (the teammate's separate backend, database authority, header-based "authentication" and development payment stubs were not imported). It describes these as adaptations rather than file imports. This notice is included regardless, because the work derives from that repository.

## 2. Optional language analysis: Stanford Stanza

Not included in the repository and not required to run Caligo. Details and licence analysis: `docs/STANZA_THIRD_PARTY.md`.

- Stanza 1.15.0: Copyright 2019 The Board of Trustees of The Leland Stanford Junior University, Apache License 2.0.
- English model weights are downloaded by the developer to a local cache outside the repository. The official model card declares Apache-2.0. Training resources carry their own terms (UD English EWT is CC BY-SA 4.0; the lemma dictionaries may incorporate the US National Library of Medicine SPECIALIST lexicon). None is redistributed here; preserve upstream notices if you ever redistribute the weights.
- PyTorch (CPU wheels, BSD-3-Clause) is installed separately for Stanza.
- Citation: Qi, Zhang, Zhang, Bolton and Manning (2020), *Stanza: A Python Natural Language Processing Toolkit for Many Human Languages*, ACL System Demonstrations.

## 3. Backend runtime dependencies (installed by pip, not vendored)

| Package (verified version) | Licence |
|---|---|
| fastapi 0.162.4, pydantic 2.14.3, pydantic-core 2.50.4, pydantic-settings 2.16.2, annotated-types 0.8.0, typing-inspection 0.4.5, annotated-doc 0.0.6 | MIT |
| starlette 1.10.0, uvicorn 0.54.0, click 8.5.1, idna 3.20, python-dotenv 1.2.4, httpx 0.28.1, httpcore 1.0.9, markupsafe 3.0.3, pycparser 3.12 | BSD-3-Clause |
| anyio 4.16.0, h11 0.16.0, sqlalchemy 2.1.8, alembic 1.20.1, mako 1.4.3, argon2-cffi 25.1.0, argon2-cffi-bindings 26.1.0, tomli 2.5.0 | MIT |
| cryptography 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| opentelemetry-api 1.45.2 | Apache-2.0 |
| typing-extensions 4.16.0 | PSF-2.0 |
| cffi 2.1.1 | MIT-0 |
| dnspython 2.9.2 | ISC |
| email-validator 2.3.0 | Unlicense |
| certifi 2026.10.23 | MPL-2.0 (unmodified, installed from PyPI) |

## 4. Frontend runtime dependencies (installed by npm, bundled into the production build)

| Package (version in `package-lock.json`) | Licence |
|---|---|
| react 18.3.1, react-dom 18.3.1, scheduler 0.23.2, loose-envify 1.4.0, js-tokens 4.0.0 | MIT |
| react-router-dom 6.30.6, react-router 6.30.6, @remix-run/router 1.23.4 | MIT |
| @tanstack/react-query 5.59.0, @tanstack/query-core 5.59.0 | MIT |
| lucide-react 0.447.0 (icons) | ISC |
| @fontsource-variable/inter 5.1.0, @fontsource/jetbrains-mono 5.1.0, @fontsource/newsreader 5.1.0 | SIL Open Font License 1.1 |

Fonts: the three `@fontsource` packages redistribute Inter, JetBrains Mono and Newsreader under the SIL OFL 1.1. They are not committed here; they are bundled into the build output by Vite. Each package ships its own `LICENSE` file with the font authors' copyright notices; keep those files with any copy of the built fonts. Fonts are not sold separately and are not renamed or modified.

## 5. Build and test tooling (not distributed)

vite, vitest, tailwindcss, postcss, autoprefixer, jsdom, @vitejs/plugin-react, openapi-typescript and the Testing Library packages are MIT; typescript is Apache-2.0. The optional browser E2E scripts import `playwright-core` (Apache-2.0), which is installed on demand with `--no-save` and is not a declared dependency.

## 6. Services and trademarks

PayPal and PayPal Sandbox are services and trademarks of PayPal, Inc. Caligo is not affiliated with, endorsed by or sponsored by PayPal. Caligo makes no calls to any hosted generative AI service.

## 7. Studied, not used

`https://github.com/skbgp/pay-saga` (commit `9f02161be7700c7b2a0c2edc94c8b25595bc7cd3`) was read as an architectural reference for the diagnosis rules. It had no declared licence, so no code or prose was copied or adapted; see "Reference study and originality" in `docs/S3_DIAGNOSIS.md`.

## 8. Contributors

See "Acknowledgments" in `README.md`.
