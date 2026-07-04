# Arquitetura

## Visao Macro

```mermaid
flowchart TB
    subgraph Clients["Clientes"]
        cli[curl / app externa]
        orc[symbiont ai-local]
        docsui[Swagger /docs]
    end

    subgraph API["orc-rag: FastAPI"]
        auth[Auth middleware]
        rate[Rate limiting]
        endpoints[Endpoints HTTP]
        obs_mid[Observability middleware]
    end

    subgraph Runtime["Runtime RAG"]
        router[Router LLM + heuristica]
        retrieval[Retrieval hibrido]
        chat[Chat proxy]
        cag[CAG selector]
        graphctx[Graph context builder]
    end

    subgraph Storage["Persistencia"]
        qdrant[(Qdrant)]
        manifest[(manifest.db)]
        embedcache[(embedding_cache.db)]
        cagdb[(cag.db)]
        bm25[(BM25 JSON)]
        graphjson[(graph.json / reports)]
    end

    subgraph Providers["Providers locais"]
        ollama[Ollama]
        clickhouse[(ClickHouse opcional)]
    end

    subgraph Sources["Fontes"]
        vault[Obsidian vaults]
        repos[Git repos]
    end

    cli --> auth
    orc --> auth
    docsui --> endpoints
    auth --> rate --> endpoints --> obs_mid
    endpoints --> router
    endpoints --> retrieval
    endpoints --> chat
    endpoints --> cag
    endpoints --> graphctx
    retrieval --> qdrant
    retrieval --> bm25
    retrieval --> graphjson
    retrieval --> cagdb
    chat --> ollama
    router --> ollama
    cag --> cagdb
    graphctx --> graphjson
    obs_mid --> clickhouse
    vault --> manifest
    repos --> manifest
```

## Servicos Docker

| Servico | Container | Porta host | Porta interna | Papel |
| --- | --- | --- | --- | --- |
| RAG API | `orc-rag` | `8484` | `8484` | FastAPI, retrieval, chat, admin jobs. |
| Qdrant | `orc-qdrant` | `16336` HTTP, `16337` gRPC | `6333`, `6334` | Vector store denso+sparse. |
| Ollama | externo ao compose | normalmente `11434` | n/a | Embeddings e LLMs. |
| ClickHouse | externo/opcional | normalmente `8123` | n/a | Observabilidade. |

## Modulos Python

```mermaid
flowchart LR
    api[api/app.py] --> schemas[api/schemas.py]
    api --> retrieval[retrieval/rag.py]
    api --> graphq[pipeline/graph/query.py]
    api --> cag[cag/*]
    api --> obs[observability/*]
    retrieval --> router[retrieval/router.py]
    retrieval --> intent[retrieval/intent.py]
    retrieval --> store[store/*]
    retrieval --> embed[embeddings/*]
    retrieval --> graphctx[retrieval/graph_context.py]
    retrieval --> budget[retrieval/budget.py]
    retrieval --> audit[retrieval/audit.py]
    store --> qdrant[store/qdrant_store.py]
    pipeline[pipeline/sync.py] --> ingest[pipeline/ingest.py]
    pipeline --> manifest[pipeline/manifest.py]
    pipeline --> graphbuild[pipeline/graph/builder.py]
    ingest --> chunkmd[chunking/markdown.py]
    ingest --> chunkcode[chunking/code.py]
    ingest --> repooverview[chunking/repo_overview.py]
```

## Fluxo de Requests

1. `auth_middleware` valida Bearer token, exceto nos paths publicos.
2. `observability_middleware` cria IDs e emite evento de request.
3. O endpoint chama store, retrieval, graph ou CAG conforme o path.
4. Operacoes com LLM usam clientes Ollama ou HTTP pool para `/api/chat`.
5. Respostas sao Pydantic models definidos em `api/schemas.py`.

## Persistencia

| Artefacto | Criado por | Consumido por | Conteudo |
| --- | --- | --- | --- |
| Qdrant `obsidian_vault` | `sync_notes` | `/query`, `/chat` | Chunks de notas. |
| Qdrant `code_repos` | `sync_repos` | `/query/code`, `/chat` | Chunks de codigo/docs/repo overview. |
| `manifest.db` | `IngestPipeline` | `/status/indexing` e proximas runs | Ficheiros, chunks e runs. |
| `embedding_cache.db` | `OllamaEmbeddingProvider` | ingestao | Embeddings por hash+modelo. |
| `bm25/*.json` | pipeline apos writes | retrieval hibrido | Vocabulario e pesos BM25. |
| `cag.db` | CAG generators | `/cag/*`, `/chat` | Packs com TTL. |
| `graphify-out/graph.json` | Graphify | graph endpoints e context builder | Nos, edges, comunidades. |
| ClickHouse | dispatcher observability | dashboard | Eventos de requests, retrieval, ingest, store e recursos. |

## Limites de Responsabilidade

- FastAPI nao indexa diretamente no request de query. Indexacao entra por admin job.
- Qdrant e o unico backend de store implementado atualmente, embora a interface seja um `Protocol`.
- Graphify e acionado pelo RAG, mas a extracao em si corre como subprocess `graphify`.
- CAG nao substitui retrieval semantico; apenas fornece contexto cached auxiliar.
- Observabilidade falha silenciosamente por design quando configurada assim.

## Diagrama de Deploy Standalone

```mermaid
flowchart TB
    subgraph Host["Host"]
        homevault["~/Obsidian/Vault"]
        graphvault["~/Obsidian/knowledge-graphs"]
        projects["~/_projects"]
        sysconfig["~/sys.config"]
        secrets["infra/docker/secrets"]
        data["./data"]
        ollama["Ollama :11434"]
    end

    subgraph Docker["Docker network ai-local-net"]
        rag["orc-rag :8484"]
        qdrant["orc-qdrant :6333/:6334"]
    end

    homevault -- ro --> rag
    projects -- ro --> rag
    sysconfig -- ro --> rag
    graphvault -- rw --> rag
    data -- rw --> rag
    secrets -- secrets --> rag
    secrets -- secrets --> qdrant
    rag --> qdrant
    rag --> ollama
    qdrant -- storage --> data
    client["Cliente local"] -->|"127.0.0.1:8484"| rag
```
