# Obsidian RAG

RAG local para notas, repositorios de codigo, Graphify e CAG. A superficie operacional atual e API-only: queries, chat, grafo, CAG e reprocessamentos manuais sao chamadas HTTP autenticadas. O antigo CLI de servico esta desativado como caminho operacional.

## Contrato API

Base interna Docker: `https://rag:8484`

Base local publicada: `https://127.0.0.1:8484`

Autenticacao: `Authorization: Bearer <RAG_API_KEY>` em todos os endpoints exceto `/health`, `/docs`, `/openapi.json` e `/redoc`.

## Operacao No Mono-Repo

```bash
cd ..
make dev
make infra
make up
```

O RAG e um componente do mono-repo `ai-local`. O runtime Docker e definido em
`../infra/docker/compose/rag.yml`, a config vem de `../config/rag/`, o registry
de modelos vem de `../config/models/rag.config.json` e os secrets locais vivem
em `../infra/docker/secrets/`.

| Metodo | Path | Uso |
| --- | --- | --- |
| GET | `/health` | Healthcheck com componentes Qdrant, Graphify, CAG e code index |
| GET | `/stats` | Estatisticas de chunks/colecoes |
| POST | `/query` | Query semantica ao vault |
| POST | `/query/code` | Query semantica a repositorios |
| POST | `/query/batch` | Batch de queries |
| POST | `/chat` | Chat com contexto RAG |
| GET | `/repos` | Repositorios configurados e estado de grafo |
| GET | `/graph/{repo}` | Relatorio Markdown do grafo |
| POST | `/graph/{repo}/query` | Query local ao `graph.json` sem CLI |
| GET | `/graph/{repo}/neighbors/{node}` | Vizinhos de um no |
| POST | `/graph/context` | Contexto estrutural multi-repo |
| GET | `/cag/packs` | Packs CAG disponiveis |
| GET | `/cag/packs/{pack_type}` | Pack CAG detalhado |
| POST | `/cag/explain` | Explicar selecao de packs |
| GET | `/status/indexing` | Estado do indice |
| GET | `/status/retrieval` | Auditoria de retrieval |
| GET | `/status/bm25` | Estado BM25 |
| POST | `/admin/reprocess` | Reprocessar local, graph, cag ou all |
| GET | `/admin/jobs/{job_id}` | Estado de job admin |

`/health` mantem `status`, `service` e `version`, e adiciona `components.qdrant`,
`components.graph`, `components.cag` e `components.code_index` para diagnostico operacional.

## Reprocessamento Manual

Manual tambem e API. Nao usar comandos dentro do container para reindexar.

```bash
curl -sS https://127.0.0.1:8484/admin/reprocess \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target":"all","force":false}'
```

Targets:

| target | Efeito |
| --- | --- |
| `local` | Reindexa notas/repos e regenera packs CAG eager |
| `graph` | Reconstroi Graphify e invalida cache de grafo |
| `cag` | Regenera packs CAG eager |
| `all` | Executa local, graph e CAG |

Resposta:

```json
{
  "job_id": "uuid",
  "status": "queued",
  "target": "all",
  "force": false,
  "status_url": "/admin/jobs/uuid",
  "message": "Reprocess job queued"
}
```

Polling:

```bash
curl -sS https://127.0.0.1:8484/admin/jobs/$JOB_ID \
  -H "Authorization: Bearer $RAG_API_KEY"
```

## Query

```bash
curl -sS https://127.0.0.1:8484/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"como funciona o dispatch?","top_k":5,"debug":false}'
```

## Integracao

- O symbiont chama o RAG atraves da feature `research` e dos mapas em `config/orc/agents.toml`.
- URLs, portas e tuning runtime devem vir dos `.env.*.generated` criados pelo resolver central.
- Graphify build continua a poder usar subprocess local dentro do proprio RAG, mas e acionado por API admin.
- O container sobe por entrypoint FastAPI/Uvicorn; nao existe console script publicado para operacao RAG.
