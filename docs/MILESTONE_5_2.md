# Milestone 5.2 — Historical RCA Context Integration

Historical incidents now carry a relevance score combining:
- 70% evidence similarity
- 20% historical incident confidence
- 10% verified resolution outcome

Verified historical outcomes are surfaced explicitly to the RCA context. The prompt
instructs the LLM that historical cases are references rather than proof and that
verified outcomes are stronger evidence than old AI hypotheses.

No vector database is introduced yet; this keeps retrieval deterministic and
explainable while the incident corpus is still small.
