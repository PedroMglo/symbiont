# API HTTP

Base local:

```text
https://127.0.0.1:8484
```

Header para endpoints protegidos:

```text
Authorization: Bearer <RAG_API_KEY>
```

Sem auth: `/health`, `/docs`, `/openapi.json`, `/redoc`, `/dashboard/*`.

## Endpoints

| Metodo | Path | Uso |
| --- | --- | --- |
| GET | `/health` | Healthcheck simples. |
| GET | `/stats` | Contagem de chunks nas colecoes. |
| POST | `/query` | Pesquisa semantica nas notas. |
| POST | `/query/code` | Pesquisa semantica em repositorios. |
| POST | `/query/batch` | Varias pesquisas num unico request. |
| POST | `/chat` | Proxy Ollama com contexto RAG opcional. |
| GET | `/repos` | Repos configurados, chunks e estado do grafo. |
| GET | `/graph/{repo}` | `GRAPH_REPORT.md` de um repo. |
| POST | `/graph/{repo}/query` | Query local ao `graph.json`. |
| GET | `/graph/{repo}/neighbors/{node}` | Vizinhos de um no do grafo. |
| POST | `/graph/context` | Contexto estrutural multi-repo. |
| GET | `/cag/packs` | Packs CAG disponiveis. |
| GET | `/cag/packs/{pack_type}` | Detalhe de um pack CAG. |
| POST | `/cag/explain` | Explica selecao de packs por intent/budget. |
| GET | `/status/indexing` | Estado do manifest de ingestao. |
| GET | `/status/retrieval` | Auditoria local de retrieval e BM25. |
| GET | `/status/bm25` | Estado BM25 por colecao. |
| POST | `/admin/reprocess` | Enfileira reprocessamento. |
| GET | `/admin/jobs` | Lista jobs admin recentes ou ativos. |
| GET | `/admin/jobs/{job_id}` | Estado de job admin. |
| POST | `/admin/jobs/{job_id}/cancel` | Pede cancelamento cooperativo de job admin. |

## Pesquisa em Notas

Request:

```json
{
  "query": "como funciona o dispatch?",
  "top_k": 5,
  "min_score": 0.0,
  "vault": "Vault",
  "source_type": "markdown",
  "exclude_source_type": null,
  "debug": true
}
```

Campos:

- `query`: pergunta, obrigatoria.
- `top_k`: 1 a 50.
- `min_score`: filtro minimo pos-query.
- `vault`: filtra por `source_name`, normalmente nome da pasta do vault.
- `source_type`: filtra por tipo de fonte.
- `exclude_source_type`: exclui tipo de fonte.
- `debug`: devolve trace de retrieval.

Exemplo:

```bash
curl -sS https://127.0.0.1:8484/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"como funciona o dispatch?","top_k":5,"debug":true}'
```

## Pesquisa em Codigo

Request:

```json
{
  "query": "retry do qdrant",
  "top_k": 5,
  "repo": "obsidian-rag",
  "symbol_type": "function",
  "debug": true
}
```

Filtros especificos:

- `repo`: filtra por `repo_name`.
- `symbol_type`: `function`, `class`, `method`, `module` ou outro valor emitido pelo chunker.

## Batch Query

```json
{
  "queries": [
    {"query": "vault sync", "collection": "obsidian_vault", "top_k": 3},
    {"query": "qdrant retry", "collection": "code_repos", "top_k": 3}
  ]
}
```

Limites:

- 1 a 10 queries.
- `top_k` por item: 1 a 20.

## Chat

Request:

```json
{
  "model": "gemma3:4b",
  "stream": false,
  "context_mode": "auto",
  "messages": [
    {"role": "user", "content": "explica a arquitetura do meu RAG"}
  ]
}
```

Comportamento:

1. Se o modelo nao estiver marcado como `rag_capable`, responde sem RAG.
2. Se estiver, o router decide se precisa de contexto.
3. O contexto relevante e injetado como system message.
4. A chamada e proxied para `Ollama /api/chat`.

No modo streaming, a resposta e `application/x-ndjson` e inclui headers:

- `X-RAG-Used`
- `X-Sources-Used`
- `X-Route-Mode`

## Admin Reprocess

Request:

```json
{
  "target": "sources",
  "force": false,
  "vault": null,
  "origin": {
    "kind": "agent",
    "name": "codex",
    "agent": "codex",
    "machine": "workstation",
    "trace_id": "request-123",
    "reason": "operator requested partial RAG rebuild"
  },
  "sources": [
    {"path": "/home/user/Documents/course", "source_type": "auto"}
  ]
}
```

Targets:

| Target | Efeito |
| --- | --- |
| `local` | Reindexa notas e repos, gera CAG eager. |
| `sources` | Regista e reindexa apenas as fontes indicadas em `sources`. |
| `graph` | Reconstroi Graphify e invalida GraphCache. |
| `cag` | Regenera packs CAG eager. |
| `all` | Executa `local`, depois `graph`, depois `cag`. |

`sources` e opcional para targets globais e obrigatorio na pratica para
`target=sources`. Quando presente, cada fonte e validada e persistida no
registry runtime do RAG antes do reprocessamento isolado. `source_type=auto`
detecta vaults Obsidian, roots Git/code e pastas heterogeneas de documentos.
Paths do host sao resolvidos contra o mount read-only configurado por
`AI_RAG_HOST_HOME`/`AI_RAG_HOST_SOURCE_ROOT`.

`origin` identifica quem iniciou a chamada. Tambem pode ser inferido a partir
de headers como `X-AI-Agent`, `X-AI-Feature`, `X-AI-Service`,
`X-AI-Machine`, `X-AI-User` e `X-Request-ID`.

Resposta:

```json
{
  "job_id": "uuid",
  "parent_job_id": null,
  "status": "queued",
  "target": "sources",
  "force": false,
  "origin": {
    "kind": "agent",
    "name": "codex",
    "agent": "codex"
  },
  "status_url": "/admin/jobs/uuid",
  "message": "Reprocess job accepted. Poll status_url for completion."
}
```

Polling:

```bash
curl -sS "https://127.0.0.1:8484/admin/jobs/$JOB_ID" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

`result.children` mostra fases/paths filhos do parent job. Para repos e pastas
de documentos, cada fonte e executada como child job coordenado pelo parent. Em
pressao de recursos, parent e child podem expor `paused_resource_pressure`,
`retry_scheduled`, `failed_resource_pressure` ou `cancelled`:

```json
{
  "job_id": "uuid",
  "status": "running",
  "origin": {"kind": "agent", "name": "codex"},
  "result": {
    "children_total": 2,
    "children_completed": 1,
    "children_failed": 0,
    "children": [
      {
        "child_id": "repo-source-001-code-ai-local",
        "phase": "repo_document",
        "status": "retry_scheduled",
        "resource_state": "retry_scheduled",
        "attempt": 1,
        "retry_at": 1783185000.0,
        "pause_started_at": 1783184880.0,
        "pause_budget_seconds": 120,
        "last_governor_snapshot": {
          "ram_percent": 82.0,
          "swap_percent": 65.0
        },
        "lease_status": "release_requested",
        "source": {
          "name": "ai-local",
          "source_type": "code",
          "path": "/app/sources/host_home/_projects/ai-local"
        }
      }
    ]
  }
}
```

Listar jobs ativos:

```bash
curl -sS "https://127.0.0.1:8484/admin/jobs?active_only=true&limit=20" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Cancelar job:

```bash
curl -sS -X POST "https://127.0.0.1:8484/admin/jobs/$JOB_ID/cancel" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

O contrato atual de cancelamento cooperativo usa apenas o endpoint `POST`.

## CAG

Listar packs:

```bash
curl -sS "https://127.0.0.1:8484/cag/packs?intent=code&budget=2000" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Explicar selecao:

```bash
curl -sS https://127.0.0.1:8484/cag/explain \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"estado dos meus repos","budget":2000}'
```

## Grafo

Listar repos:

```bash
curl -sS https://127.0.0.1:8484/repos \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Relatorio:

```bash
curl -sS https://127.0.0.1:8484/graph/obsidian-rag \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Vizinhos:

```bash
curl -sS "https://127.0.0.1:8484/graph/obsidian-rag/neighbors/IngestPipeline?max_results=10" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Contexto estrutural:

```bash
curl -sS https://127.0.0.1:8484/graph/context \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"pipeline de ingestao","repos":["obsidian-rag"],"max_nodes":20}'
```
