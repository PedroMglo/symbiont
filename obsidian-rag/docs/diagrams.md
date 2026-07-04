# Diagramas

Este ficheiro junta os diagramas principais para estudo rapido da arquitetura.

## Arquitetura Geral

```mermaid
flowchart TB
    client[Cliente externo] --> api[FastAPI RAG API]
    api --> auth[API key + rate limit]
    auth --> endpoints{Endpoint}

    endpoints -->|/query| direct_query[Query direta]
    endpoints -->|/chat| chat_flow[Chat flow]
    endpoints -->|/admin/reprocess| admin[Admin job]
    endpoints -->|/graph/*| graph_api[Graph API]
    endpoints -->|/cag/*| cag_api[CAG API]
    endpoints -->|/status/*| status[Status]

    direct_query --> qdrant[(Qdrant)]
    direct_query --> bm25[BM25]
    chat_flow --> router[Router]
    router --> retrieval[Retrieval]
    retrieval --> qdrant
    retrieval --> bm25
    retrieval --> graphcache[GraphCache]
    retrieval --> cagdb[(CAG DB)]
    chat_flow --> ollama[Ollama chat]
    admin --> ingest[IngestPipeline]
    admin --> graphify[Graphify]
    ingest --> qdrant
    ingest --> manifest[(manifest.db)]
    graphify --> graphfiles[(graph.json/reports)]
    graphfiles --> graphcache
```

## Ingestao

```mermaid
flowchart LR
    vaults[Vault dirs] --> scanner
    repos[Repo roots] --> scanner
    scanner[Scanner] --> filesq[(files_queue)]
    filesq --> parser[Parser]
    parser --> manifest[(manifest.db)]
    parser --> chunksq[(chunks_queue)]
    chunksq --> embedder[Embedding batcher]
    embedder --> ollama[Ollama /api/embed]
    embedder --> writeq[(write_queue)]
    writeq --> writer[Writer]
    writer --> qdrant[(Qdrant)]
    writer --> manifest
    qdrant --> bm25[BM25 rebuild]
```

## Retrieval

```mermaid
flowchart TD
    query[Query] --> route[route_query]
    route --> intent[QueryIntent]
    intent --> notes{use_notes?}
    intent --> code{use_code?}
    intent --> graph{use_graph?}
    notes -- sim --> note_search[Hybrid notes search]
    code -- sim --> code_search[Hybrid code search]
    note_search --> filter[threshold + dedup + rerank]
    code_search --> filter
    graph -- sim --> graphctx[Graph context]
    filter --> cag[CAG packs]
    graphctx --> cag
    cag --> gate[Relevance gate]
    gate -->|pass| context[Context string]
    gate -->|fail| none[No context]
```

## Pesquisa Hibrida no Qdrant

```mermaid
sequenceDiagram
    participant API
    participant Embedder
    participant BM25
    participant Qdrant

    API->>Embedder: get_query_embedding(query)
    Embedder-->>API: dense vector
    API->>BM25: transform(tokens)
    BM25-->>API: sparse vector ou None
    alt sparse disponivel
        API->>Qdrant: dense prefetch + sparse prefetch + RRF
    else sem sparse
        API->>Qdrant: dense query
    end
    Qdrant-->>API: QueryResult[]
```

## Chat

```mermaid
sequenceDiagram
    participant Client
    participant API
    participant Router
    participant Retrieval
    participant CAG
    participant Ollama

    Client->>API: POST /chat
    API->>API: should_use_rag(model)
    API->>Router: classify last user message
    Router-->>API: ContextMode
    opt context needed
        API->>Retrieval: build_rag_context_async
        Retrieval->>CAG: get relevant packs
        Retrieval-->>API: context, relevant, sources_used
    end
    API->>Ollama: /api/chat with messages
    Ollama-->>Client: JSON or NDJSON stream
```

## Graphify

```mermaid
flowchart LR
    repos[Configured repos] --> detect[Detect graph changes]
    detect -->|none| skip[Skip]
    detect -->|code only| update[graphify update]
    detect -->|docs/new/force| extract[graphify extract]
    extract --> graphjson[(graph.json)]
    update --> graphjson
    graphjson --> report[GRAPH_REPORT.md]
    graphjson --> export[Obsidian export]
    graphjson --> cache[GraphCache]
```

## CAG

```mermaid
flowchart TB
    sync[Sync/admin target] --> specs[Pack registry]
    specs --> eager[Eager generators]
    specs --> lazy[Lazy generators]
    eager --> packstore[(PackStore SQLite)]
    lazy --> packstore
    packstore --> explain["/cag/explain"]
    packstore --> packs["/cag/packs"]
    packstore --> chat[Chat context injection]
```

## Observabilidade

```mermaid
flowchart LR
    api[API middleware] --> event[RAGEvent]
    retrieval[Retrieval] --> event
    ingest[IngestPipeline] --> event
    store[QdrantStore] --> event
    sampler[Resource sampler] --> event
    event --> dispatcher[Dispatcher]
    dispatcher --> clickhouse[(ClickHouse)]
    clickhouse --> dashboard["/dashboard"]
```
