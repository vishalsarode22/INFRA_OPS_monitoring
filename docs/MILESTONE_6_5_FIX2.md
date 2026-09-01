# Milestone 6.5 Fix 2 — Corroboration Threshold

The first scoring fix still failed the intended two-category HIGH case.
The cause is that a strong baseline signal plus recurrence produced a
weighted score below 0.60, so +0.15 was insufficient.

The corrected rule:
  - 2 independent categories: +0.30 corroboration bonus
  - 3 independent categories: +0.35 corroboration bonus

Signals within the same category remain collapsed to the strongest signal.
Therefore duplicate baseline metrics do not stack.

This changes only the advisory intelligence score. Deterministic SAP
incident severity remains authoritative.
