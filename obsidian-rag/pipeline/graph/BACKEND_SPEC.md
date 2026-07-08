# Graph Backend Spec

Data: 2026-06-15.

## Ownership

`obsidian-rag` owns GraphRAG ingestion, graph import, graph query,
graph context building and graph-backed retrieval augmentation.

Graphify remains a producer of graph artefacts. A graph database backend is a
query/index backend behind the RAG API, not a replacement for Qdrant and not an
orchestrator concern.

## Interface

Any backend must expose the following behavior behind a local RAG-owned
interface:

- `health()`
- `stats(repo: str | None = None)`
- `import_graph(repo: str, graph: dict, *, source_hash: str)`
- `neighbors(repo: str, node: str, *, depth: int, limit: int)`
- `shortest_paths(repo: str, source: str, target: str, *, limit: int)`
- `subgraph_for_chunks(repo: str, chunk_ids: list[str], *, budget: int)`
- `query(repo: str, query: str, *, limit: int)`
- `context_for_query(repo: str, query: str, *, limit: int, include_summaries: bool)`
- `node_for_chunk(repo: str, source_file: str, section_header: str)`
- `node_by_id(repo: str, node_id: str)`

## Backend Candidates

FalkorDB is the primary service-backed candidate and is available as an
opt-in backend through `[graphify] query_backend = "falkordb"`. Kuzu may be
evaluated only as an embedded PoC if maintenance risk is accepted. The current
Graphify JSON path remains the default baseline backend so behavior can be
compared before removal.

## Config

The backend is selected through `config/rag/*.toml` and env overrides:

- `RAG_GRAPHIFY_QUERY_BACKEND`: `json` or `falkordb`.
- `RAG_GRAPHIFY_IMPORT_ON_BUILD`: import Graphify output after graph builds.
- `RAG_GRAPHIFY_FALKOR_HOST`
- `RAG_GRAPHIFY_FALKOR_PORT`
- `RAG_GRAPHIFY_FALKOR_GRAPH`
- `RAG_GRAPHIFY_FALKOR_USERNAME`
- `RAG_GRAPHIFY_FALKOR_PASSWORD` or `RAG_GRAPHIFY_FALKOR_PASSWORD_FILE`
- `RAG_GRAPHIFY_FALKOR_SSL`

FalkorDB is an index/query backend derived from Graphify output. Graphify JSON
remains the source artifact and the default no-service backend.

## Import Contract

Imports are idempotent by Graphify `graph.json` SHA-256:

1. Read the current `GraphImport.source_hash` for the repo.
2. Skip import when the hash matches.
3. Delete prior `GraphNode` data for the repo when the hash differs.
4. Import nodes and `GRAPH_EDGE` relationships.
5. Write the new `GraphImport` marker with node/edge counts.

The import is called by `build_graph` after successful Graphify output or when
an existing graph is reused. If the selected service backend cannot import, the
graph build reports failure for that repo.

## Legacy Removal Rule

Manual traversal/cache code was removed on 2026-06-15 after local golden graph
context parity. `retrieval/graph_context.py`, `/graph/context`, graph-only
retrieval and graph query wrappers must use `GraphBackend`; do not reintroduce a
parallel cache, traversal helper, or backend-specific caller outside this
interface.
