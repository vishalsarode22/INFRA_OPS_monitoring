# InfraBeatOps Milestone 5 — Historical Intelligence

Adds lightweight historical incident retrieval using the existing durable JSON
incident store.

## Behavior

- Only resolved incidents are eligible as historical references.
- Matching first requires the same correlation rule.
- A token-overlap similarity score ranks candidates.
- Only the top three matches are added to RCA context by default.
- Historical RCA is treated as reference evidence, never as proof.
- No new LLM calls are made by the history layer itself.

This is intentionally a first retrieval layer, not an ML model. It creates the
data contract needed for later embeddings, vector search, baselines, and
supervised RCA evaluation.
