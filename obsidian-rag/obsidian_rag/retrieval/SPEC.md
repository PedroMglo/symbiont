# Retrieval Evidence Contract

`obsidian_rag.retrieval` owns retrieval evidence for notes, code, graph context,
CAG packs, ranking, budget, truncation, and miss reasons.

## Contract

All public evidence payloads use schema version `rag.evidence.v1`.

- `CitationRef` identifies one retrieved chunk with score, source namespace,
  source id/name, source path, optional repo, chunk id/index, content hash,
  freshness, budget, and truncation state.
- `RagEvidence` groups citation refs for a query and carries the retrieval trace
  used to explain how that context was selected.
- `GraphContextEvidence` identifies structural context contributed by graph
  retrieval, including repo, score, freshness, token budget, truncation state,
  and node/edge counts.
- `CagPackEvidence` identifies selected cached context packs with pack type,
  scope, freshness, source/config hashes, token count, score, and selection
  reason.
- `RetrievalTrace` reports the accepted/rejected context decision, sources used,
  miss reasons, budget allocation, truncation report, and references to the
  evidence items above.

## Invariants

- Evidence is descriptive only. It must not decide routing, policy, or final
  synthesis.
- Evidence is built from existing chunk metadata, graph results, CAG store
  records, and retrieval trace state. Do not add a second parser or store probe
  just to manufacture provenance.
- When no context is returned, a trace must include at least one stable
  `miss_reasons` entry.
- Cross-owner callers consume this contract through the RAG API; they must not
  import retrieval internals.
