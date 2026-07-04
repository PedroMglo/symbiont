# Visao Geral

`obsidian-rag` e um servico FastAPI que transforma conhecimento local em contexto pesquisavel e injetavel em conversas com LLMs.

Ele indexa duas familias principais de conteudo:

- `obsidian_vault`: notas Markdown dos vaults Obsidian configurados.
- `code_repos`: repositorios Git, codigo, docs e resumos automaticos de repos.

Em paralelo, pode construir knowledge graphs estruturais por repositorio com Graphify e gerar packs CAG, que sao blocos de contexto cached sobre configuracao, estado do indice, repos, modelos locais e outras informacoes extraidas.

## O Que o Servico Resolve

O servico responde a quatro necessidades diferentes:

- Pesquisa semantica simples: "que notas falam sobre X?"
- Pesquisa tecnica em codigo: "onde esta a funcao Y?"
- Chat com contexto local: "explica esta parte do meu projeto."
- Contexto estrutural: "que componentes dependem deste modulo?"

## Componentes Principais

| Componente | Papel |
| --- | --- |
| FastAPI (`api/app.py`) | Superficie HTTP: query, chat, graph, CAG, admin e status. |
| Qdrant | Vector store com colecoes densas e sparse BM25. |
| Ollama | Embeddings, chat, router LLM e enriquecimento de grafo. |
| IngestPipeline | Scanner, parser, embedder e writer com backpressure. |
| Manifest SQLite | Estado incremental dos ficheiros/chunks e runs de ingestao. |
| Graphify | Extracao de knowledge graph por repo. |
| GraphCache | Cache em memoria de `graph.json`, summaries e god nodes. |
| CAG PackStore | SQLite com packs de contexto precomputado e TTL. |
| Observability | Eventos para ClickHouse e dashboard local. |

## Modelo Mental

```mermaid
flowchart LR
    user[Cliente ou Symbiont] --> api[FastAPI RAG API]
    api --> router[Router de contexto]
    router --> retrieval[Retrieval hibrido]
    retrieval --> qdrant[(Qdrant)]
    retrieval --> bm25[BM25 local]
    retrieval --> graph[GraphCache]
    retrieval --> cag[(CAG SQLite)]
    api --> ollama[Ollama Chat]
    ingest[Admin reprocess] --> pipeline[IngestPipeline]
    pipeline --> ollama_embed[Ollama Embeddings]
    pipeline --> qdrant
    pipeline --> manifest[(manifest.db)]
    graphify[Graphify build] --> graph_store[graph.json + reports]
    graph_store --> graph
```

## Fontes de Dados

As fontes sao configuradas em `config/rag/user.toml` e por variaveis `RAG_*`.

- Vaults Obsidian: `settings.paths.vault_dirs`.
- Roots/repos Git: `settings.repos.paths`.
- Modelos RAG: `config/models/rag.config.json`.
- Secrets: Docker secrets ou env vars com sufixo `_FILE`.
- Dados persistidos: `settings.paths.data_dir`, por defeito `data/qdrant` em modo local.

## Saidas Produzidas

| Saida | Onde |
| --- | --- |
| Vetores de notas | Qdrant collection `obsidian_vault`. |
| Vetores de codigo/docs | Qdrant collection `code_repos`. |
| Sparse BM25 | `data/qdrant/bm25/<collection>.json`. |
| Manifest incremental | `data/qdrant/manifest.db`. |
| Cache de embeddings | `data/qdrant/embedding_cache.db`. |
| Packs CAG | `settings.paths.data_dir / cag.db_path`; com os defaults atuais, `data/qdrant/cag.db`. |
| Graphify output | `data/graphify/<repo>/graphify-out/`. |
| Export Obsidian dos grafos | `graphify.graph_vault_dir`. |
| Auditoria retrieval | ficheiros locais em `data_dir` e eventos opcionais. |
| Observabilidade | ClickHouse database `obsidian_rag`. |

## Principios de Design

- API-first: operacao e reprocessamento entram por HTTP.
- Local-first: Ollama e Qdrant locais por defeito.
- Incremental: manifesto evita reprocessar ficheiros inalterados.
- Bounded pipeline: filas limitadas evitam crescimento indefinido de memoria.
- Backend-agnostic onde interessa: `VectorStore`, `EmbeddingProvider`, `LLMClient`.
- Contexto seletivo: o router decide quando usar RAG/grafo/nenhum contexto.
- Segurança operacional: API key, rate limit, Docker sem privilegios extras e secrets por ficheiro.
