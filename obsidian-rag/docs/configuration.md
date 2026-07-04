# Configuracao

A configuracao e carregada por `obsidian_rag/config.py`.

Prioridade, da mais forte para a mais fraca:

1. Variaveis de ambiente `RAG_<SECAO>_<CHAVE>`.
2. Docker secret via `RAG_<SECAO>_<CHAVE>_FILE`.
3. `config/rag/user.toml`.
4. `config/rag/internal.toml`.
5. Defaults no codigo.

## Ficheiros Principais

| Ficheiro | Papel |
| --- | --- |
| `../../config/rag/internal.toml` | Politica interna estavel. Nao deve ser editado para personalizacao. |
| `../../config/rag/user.toml` | Intencao do utilizador: vaults, repos, graphify e modo de retrieval. |
| `../../config/models/rag.config.json` | Registry central de modelos e prompts para embedding, router, reranker e graph enrichment. |
| `../../infra/docker/compose/rag.yml` | Runtime Docker do componente no mono-repo. |
| `../../infra/docker/secrets/*` | API keys e tokens lidos por `_FILE`. |

## Descoberta de Config

`AI_RAG_SETTINGS_DIR` tem prioridade. Se existir, deve apontar para a pasta que contem `internal.toml` e `user.toml`.

Sem env var, o codigo procura:

- `config/rag` no workspace raiz ai-local;
- `$AI_LOCAL_ROOT/config/rag`;
- `~/_projects/ai-local/config/rag`;
- `~/ai-local/config/rag`.

## Paths

Exemplo em `user.toml`:

```toml
[paths]
vault_dirs = ["~/Obsidian/Vault"]
```

`data_dir` e derivado por `obsidian_rag.config`/storage env e pode ser
sobrescrito com `RAG_PATHS_DATA_DIR`.

No Docker do mono-repo, `RAG_DATA_DIR` monta o diretorio host em `/app/data`.

## API

Campos relevantes:

- `host`: por env `RAG_API_HOST`, no container fica `0.0.0.0`.
- `port`: por env `RAG_API_PORT`, por defeito `8484`.
- `api_key`: por `RAG_API_API_KEY_FILE=/run/secrets/rag_api_key`.
- `query_top_k`, `rate_limit` e `chat_rate_limit`: defaults derivados no loader, com override por `RAG_API_*`.

Seguranca: o `serve()` recusa expor `0.0.0.0` sem `api_key`.

## Ollama

No compose:

```yaml
RAG_OLLAMA_BASE_URL: https://host.docker.internal:11434
OLLAMA_API_KEY_FILE: /run/secrets/ollama_api_key
```

O modelo de embeddings e lido da role `rag.roles.embedding` no registry central `../config/models/rag.config.json`.

## Store Qdrant

```toml
[store]
backend = "qdrant"
```

No compose:

```yaml
RAG_STORE_QDRANT_URL: https://qdrant:6333
RAG_STORE_QDRANT_API_KEY_FILE: /run/secrets/qdrant_api_key
```

O store cria colecoes com:

- vetor denso com dimensao vinda do registry, por defeito `1024`;
- distancia cosine;
- sparse vector `bm25`;
- payload indexes para `source_type`, `source_id`, `source_name`, `source_path`, `repo_name`, `note_title`, `content_hash` e `_id`.

## Repositorios

```toml
[repos]
paths = ["~/_projects", "~/sys.config"]
collection_name = "code_repos"

[repos.chunking]
strategy = "ast"
max_chars = 2000
overlap_chars = 200
min_chars = 80
contextual_prefix = true
```

Cada path pode ser um repo Git direto ou uma pasta raiz. O pipeline descobre repos Git dentro das raizes e ignora diretorios como `.venv`, `node_modules`, `dist`, `build` e caches.

## Retrieval

```toml
[retrieval]
top_k = 10
context_mode = "auto"
token_budget = 6000
score_threshold = 0.45
dynamic_threshold_ratio = 0.75
graph_max_neighbors = 5
graph_max_communities = 3
graph_cache_ttl = 300
```

`context_mode` aceita:

- `auto`
- `rag_only`
- `graph_only`
- `both`
- `none`

## Router e Reranker

```toml
[router]
enabled = true

[reranker]
enabled = true
top_k_candidates = 30
min_score = 0.3
cross_encoder_model = "BAAI/bge-reranker-v2-m3"
```

Os modelos concretos sao centralizados em `config/models/rag.config.json`.

## Graphify

```toml
[graphify]
enabled = true
backend = "ollama"
output_dir = "data/graphify"
graph_vault_dir = "~/Obsidian/knowledge-graphs"
auto_update = true
extract_mode = "deep"
```

Graphify usa `OLLAMA_BASE_URL` com sufixo `/v1` e exige `OLLAMA_API_KEY`.

## CAG

```toml
[cag]
enabled = true
db_path = "cag.db"
default_ttl = 3600
system_ttl = 300
max_pack_tokens = 2000
generate_on_sync = true
```

Os packs eager sao regenerados apos sync local e no target admin `cag`.

## Performance

Os valores finais podem ser auto-tuned em `config.py`/`tuning.py`.

Variaveis frequentes no compose:

- `RAG_PERFORMANCE_MAX_CPU_PERCENT`
- `RAG_PERFORMANCE_MAX_MEMORY_PERCENT`
- `RAG_PERFORMANCE_MAX_PARALLEL_JOBS`
- `RAG_PERFORMANCE_GRAPH_PARALLEL_JOBS`
- `RAG_PERFORMANCE_PARSER_WORKERS`
- `RAG_PERFORMANCE_EMBEDDING_BATCH_SIZE`
- `RAG_PERFORMANCE_EMBEDDING_BATCH_MAX_CHARS`
- `RAG_PERFORMANCE_EMBEDDING_CONCURRENCY`
- `RAG_PERFORMANCE_EMBEDDING_TIMEOUT`
- `RAG_PERFORMANCE_QUERY_TIMEOUT_SECONDS`
- `RAG_PERFORMANCE_GRAPH_TIMEOUT`
- `RAG_PERFORMANCE_PIPELINE_TIMEOUT`

## Observabilidade

```toml
[observability]
enabled = true
clickhouse_database = "obsidian_rag"
clickhouse_username = "default"
resource_sampling = true
resource_sample_interval = 5.0
```

O password e lido via env definida em `clickhouse_password_env`, por defeito `CLICKHOUSE_PASSWORD`.
