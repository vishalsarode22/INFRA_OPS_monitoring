# Milestone 6.5 Fix — Corroborating Evidence Score

The 90-test run exposed a scoring design issue: the original category
weights (45% + 35% + 20%) could not reach HIGH when only two independent
categories were active, even when they strongly corroborated one another.

The fix keeps the original category weights and adds an evidence-diversity
bonus:
  - 2 independent active categories: +0.15
  - 3 independent active categories: +0.25

Signals within one category are still reduced to the strongest signal.
Therefore duplicate metrics cannot artificially inflate the score.

Incident severity remains authoritative and is not changed by this layer.
