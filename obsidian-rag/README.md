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

| Metodo | Path                             | Uso                                                            |
| ------ | -------------------------------- | -------------------------------------------------------------- |
| GET    | `/health`                        | Healthcheck com componentes Qdrant, Graphify, CAG e code index |
| GET    | `/stats`                         | Estatisticas de chunks/colecoes                                |
| POST   | `/query`                         | Query semantica ao vault                                       |
| POST   | `/query/code`                    | Query semantica a repositorios                                 |
| POST   | `/query/batch`                   | Batch de queries                                               |
| POST   | `/chat`                          | Chat com contexto RAG                                          |
| GET    | `/repos`                         | Repositorios configurados e estado de grafo                    |
| GET    | `/graph/{repo}`                  | Relatorio Markdown do grafo                                    |
| POST   | `/graph/{repo}/query`            | Query local ao `graph.json` sem CLI                            |
| GET    | `/graph/{repo}/neighbors/{node}` | Vizinhos de um no                                              |
| POST   | `/graph/context`                 | Contexto estrutural multi-repo                                 |
| GET    | `/cag/packs`                     | Packs CAG disponiveis                                          |
| GET    | `/cag/packs/{pack_type}`         | Pack CAG detalhado                                             |
| POST   | `/cag/explain`                   | Explicar selecao de packs                                      |
| GET    | `/status/indexing`               | Estado do indice                                               |
| GET    | `/status/retrieval`              | Auditoria de retrieval                                         |
| GET    | `/status/bm25`                   | Estado BM25                                                    |
| POST   | `/admin/reprocess`               | Reprocessar local, sources, graph, cag ou all                  |
| GET    | `/admin/jobs`                    | Listar jobs admin recentes ou ativos                           |
| GET    | `/admin/jobs/{job_id}`           | Estado de job admin                                            |
| POST   | `/admin/jobs/{job_id}/cancel`    | Pedir cancelamento cooperativo de job admin                    |

`/health` mantem `status`, `service` e `version`, e adiciona `components.qdrant`,
`components.graph`, `components.cag` e `components.code_index` para diagnostico operacional.

## Reprocessamento Manual

Manual tambem e API. Nao usar comandos dentro do container para reindexar.

```bash
export RAG_API_KEY="$(tr -d '\n' < infra/docker/secrets/rag_api_key)"
export RAG_HOST_PORT="$(awk -F= '$1=="ORC_PORT_RAG"{print $2}' .env.services.generated)"


curl -skS "https://127.0.0.1:${RAG_HOST_PORT}/admin/reprocess" \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target":"all","force":true,"origin":{"kind":"user_machine","name":"local-operator","machine":"workstation"}}'
```

## Para acompanhar os logs do container

```bash
docker logs -f --tail=120 orc-rag
```

Targets:

| target  | Efeito                                          |
| ------- | ----------------------------------------------- |
| `local` | Reindexa notas/repos e regenera packs CAG eager |
| `sources` | Reindexa apenas fontes locais indicadas no pedido |
| `graph` | Reconstroi Graphify e invalida cache de grafo   |
| `cag`   | Regenera packs CAG eager                        |
| `all`   | Executa local, graph e CAG                      |

Resposta:

```json
{
  "job_id": "uuid",
  "parent_job_id": null,
  "status": "queued",
  "target": "all",
  "force": false,
  "origin": {"kind": "user_machine", "name": "local-operator"},
  "status_url": "/admin/jobs/uuid",
  "message": "Reprocess job queued"
}
```

Polling:

```bash
curl -sS https://127.0.0.1:8484/admin/jobs/$JOB_ID \
  -H "Authorization: Bearer $RAG_API_KEY"
```

O status inclui `result.children` com fases/paths filhos. Repos e pastas de
documentos sao processados como child jobs coordenados pelo parent; `force=true`
faz reset global uma vez antes dos children, nao por child.

Listar jobs ativos:

```bash
curl -skS "https://127.0.0.1:${RAG_HOST_PORT}/admin/jobs?active_only=true&limit=20" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Cancelar job:

```bash
curl -skS -X POST "https://127.0.0.1:${RAG_HOST_PORT}/admin/jobs/$JOB_ID/cancel" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Jobs e children podem expor `paused_resource_pressure`, `retry_scheduled`,
`failed_resource_pressure` ou `cancelled` quando o Resource Governor limita a
run.

Postman:

```text
postman/ai-local-obsidian-rag.postman_collection.json
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
