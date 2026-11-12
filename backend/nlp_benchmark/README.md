# Reproducible log investigation benchmark

24 held-out synthetic cases; 8 separate development cases. Actual CPU Stanza run, no network inference.

| Metric | Deterministic extraction | Stanza-assisted |
|---|---:|---:|
| Precision | 1.0000 | 1.0000 |
| Recall | 0.7895 | 0.9474 |
| Failure-clue F1 | 0.8824 | 0.9730 |
| Advisory root-cause accuracy | 0.9167 | 1.0000 |
| Citation validity | 1.0000 | 1.0000 |
| Unsupported clue count | 0.0000 | 0.0000 |
| Unsupported hypothesis count | 0.0000 | 0.0000 |
| Advisory abstention rate | 0.7083 | 0.6250 |
| Mean wall latency (ms) | 0.4477 | 51.8731 |
| Mean CPU time (ms) | 0.6510 | 46.8750 |
| Startup / load (ms) | 0.00 | 18079.71 |
| Peak process RSS (MiB) | 31.68 | 534.86 |

Measured F1 gain: 9.06 percentage points. Advisory classification gain: 8.33 points.

These are presence-category / advisory-hypothesis metrics, not verified financial outcomes. Existing structured-only S3 intentionally abstains on log-only inputs (0.625 label accuracy in both arms); it is unchanged. No generative LLM is configured or evaluated.

Keep Stanza optional: measured gains cover a few irregular forms, while startup/memory/latency costs are material. The synthetic, author-labelled holdout is too small for production generalization. See ../../docs/STANZA_LOG_INTELLIGENCE.md for exact definitions, security, model setup and integration requirements.
