# Servicos Independentes

Este documento separa as pecas do `obsidian-rag` por responsabilidade, para poderes integrar apenas aquilo de que precisas.

Nem todas as pecas sao "servicos" de rede. Algumas sao subsistemas internos com API HTTP exposta pelo RAG.

## 1. RAG API

Tipo: servico HTTP.

Container: `orc-rag`.

Porta:

- host: `127.0.0.1:8484`;
- Docker: `rag:8484`.

Usa quando queres:

- pesquisar notas;
- pesquisar codigo;
- fazer chat com contexto;
- pedir contexto estrutural;
- acionar reprocessamento.

Contrato minimo:

```bash
curl -sS https://127.0.0.1:8484/health
```

Pesquisa:

```bash
curl -sS https://127.0.0.1:8484/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"tema a pesquisar","top_k":5}'
```

Integracao recomendada:

- chama `/query/batch` se precisas de varios contextos;
- chama `/chat` se queres delegar router, gate e prompt assembly ao RAG;
- chama `/admin/reprocess` para atualizar indice.

## 2. Qdrant

Tipo: servico HTTP/gRPC de vector store.

Container: `orc-qdrant`.

Portas standalone:

- HTTP host: `127.0.0.1:16336`;
- gRPC host: `127.0.0.1:16337`;
- interno Docker: `qdrant:6333` e `qdrant:6334`.

Usa diretamente quando queres:

- inspecionar colecoes;
- fazer tooling administrativo;
- integrar outro produtor/consumidor de vetores.

Colecoes criadas pelo RAG:

- `obsidian_vault`;
- `code_repos` ou valor de `repos.collection_name`.

Payloads importantes:

- `_id`;
- `_document`;
- `source_type`;
- `source_id`;
- `source_name`;
- `source_path`;
- `repo_name`;
- `note_title`;
- `section_header`;
- `symbol_type`;
- `content_hash`.

Cuidados:

- se escreves diretamente no Qdrant, o `manifest.db` do RAG nao sabe dessas escritas;
- o cleanup stale do pipeline pode remover pontos que nao estejam no manifest;
- para extensoes externas, prefere colecoes separadas ou usa a API RAG.

## 3. Ollama

Tipo: provider externo de modelos.

Porta habitual: `11434`.

Usado pelo RAG para:

- embeddings via `/api/embed`;
- chat via `/api/chat`;
- router e reranker LLM;
- Graphify com backend `ollama`.

Modelos esperados no registry:

| Role | Exemplo | Uso |
| --- | --- | --- |
| `embedding` | `bge-m3` | Vetores 1024d para Qdrant. |
| `router` | `gemma3:4b` | Decide se precisa de contexto local. |
| `reranker` | `gemma3:4b` | Fallback LLM rerank. |
| `graph-enrichment` | `qwen2.5-coder:7b` | Extracao semantica Graphify. |

Cuidados:

- a dimensao do embedding tem de bater certo com Qdrant;
- mudar embedding model/dimensao exige reindexacao;
- Graphify com Ollama exige `OLLAMA_API_KEY`.

## 4. Ingestao

Tipo: subsistema interno acionado por API admin.

Entrada:

```bash
curl -sS https://127.0.0.1:8484/admin/reprocess \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target":"local","force":false}'
```

Integra quando queres:

- atualizar o indice depois de editar notas/repos;
- expor um botao "refresh knowledge base" numa app tua;
- construir um job scheduler externo.

Polling:

```bash
curl -sS "https://127.0.0.1:8484/admin/jobs/$JOB_ID" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Nao integres chamando comandos internos do container. O contrato externo e HTTP.

## 5. Graphify

Tipo: subprocess/tooling acionado pelo RAG; resultados expostos por HTTP.

Acionar:

```bash
curl -sS https://127.0.0.1:8484/admin/reprocess \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target":"graph","force":false}'
```

Consumir:

- `GET /repos`;
- `GET /graph/{repo}`;
- `POST /graph/{repo}/query`;
- `GET /graph/{repo}/neighbors/{node}`;
- `POST /graph/context`.

Usa independentemente quando queres:

- mapear dependencias;
- descobrir vizinhos de um simbolo;
- enriquecer prompts com estrutura de codigo;
- exportar knowledge graph para Obsidian.

Cuidados:

- nem todos os repos terao grafo construido;
- `graph/context` usa matching simples por termos e labels;
- Graphify pode ser caro quando docs mudam porque chama LLM.

## 6. CAG

Tipo: subsistema interno com API HTTP.

Consumir:

- `GET /cag/packs`;
- `GET /cag/packs/{pack_type}`;
- `POST /cag/explain`.

Usa independentemente quando queres:

- contexto de configuracao sem pesquisa vetorial;
- resumo do estado do indice;
- estado de repos/modelos/servicos;
- explicar ao utilizador que contexto cached entraria numa resposta.

Exemplo:

```bash
curl -sS "https://127.0.0.1:8484/cag/packs?intent=system&budget=2000" \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Cuidados:

- respeita `fresh`;
- packs expirados podem aparecer em listagens, mas nao sao injetados no chat;
- CAG e extrativo, nao e uma memoria semantica completa.

## 7. Observabilidade

Tipo: subsistema interno + ClickHouse externo opcional.

Consumir:

- `/dashboard`;
- endpoints do dashboard;
- tabelas ClickHouse `obsidian_rag.*`.

Usa independentemente quando queres:

- monitorizar latencia;
- acompanhar qualidade de retrieval;
- auditar ingest runs;
- observar uso de recursos;
- construir dashboards proprios.

Tabelas principais:

- `rag_requests`;
- `rag_retrieval`;
- `rag_ingest_runs`;
- `rag_ingest_stages`;
- `rag_embedding_batches`;
- `rag_cag_operations`;
- `rag_store_operations`;
- `rag_resource_samples`.

Cuidados:

- queries sao representadas por hash, nao texto;
- a observabilidade deve ser opcional e nao bloquear requests;
- confirma `CLICKHOUSE_PASSWORD` se o dispatcher nao conseguir escrever.

## 8. Resource Governor

Tipo: cliente opcional de coordenacao com o symbiont/resource governor.

Usado por:

- embedding batcher;
- qdrant writer;
- graphify builder.

Se o symbiont nao estiver disponivel, as chamadas sao tratadas como best-effort e o pipeline continua com o governor local.

Integra quando queres que cargas externas coordenem:

- VRAM;
- Qdrant writes;
- jobs background preemptiveis;
- politicas de qualidade/degradacao.
