# Integracao Externa

Este documento mostra como usar `obsidian-rag` a partir de outra app, sem assumir o symbiont do `ai-local`.

## Padrao Basico

1. Sobe `rag` e `qdrant`.
2. Exporta `RAG_API_KEY`.
3. Chama `/query`, `/query/code`, `/chat` ou `/graph/context`.
4. Trata `401`, `404`, `429` e timeouts.

## Cliente HTTP Simples

```python
import httpx

class ObsidianRAG:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def query_notes(self, query: str, top_k: int = 5) -> dict:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{self.base_url}/query",
                headers=self.headers,
                json={"query": query, "top_k": top_k},
            )
            r.raise_for_status()
            return r.json()

    def query_code(self, query: str, repo: str | None = None, top_k: int = 5) -> dict:
        payload = {"query": query, "top_k": top_k}
        if repo:
            payload["repo"] = repo
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{self.base_url}/query/code",
                headers=self.headers,
                json=payload,
            )
            r.raise_for_status()
            return r.json()
```

## Usar Como Context Provider

Para integrar com outro LLM, usa `/query/batch` para recolher contexto e injeta tu proprio no prompt:

```bash
curl -sS https://127.0.0.1:8484/query/batch \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "queries": [
      {"query":"arquitetura do pipeline","collection":"obsidian_vault","top_k":4},
      {"query":"IngestPipeline","collection":"code_repos","top_k":4}
    ]
  }'
```

## Usar Como Chat Proxy

Se queres que o proprio RAG decida contexto, chama `/chat`:

```bash
curl -sS https://127.0.0.1:8484/chat \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"gemma3:4b",
    "stream":false,
    "context_mode":"auto",
    "messages":[{"role":"user","content":"o que no meu projeto usa Qdrant?"}]
  }'
```

## Usar Apenas o Grafo

Para apps que precisam de estrutura e nao de texto completo:

```bash
curl -sS https://127.0.0.1:8484/graph/context \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"qdrant dependencies","max_nodes":20,"include_summaries":true}'
```

## Webhook Pos-Sync

`pipeline/webhook.py` tem suporte a webhooks de sync complete. Configura em `settings.webhook` se a tua app precisa de ser notificada depois de reindexacao.

## Contratos a Preservar

Ao integrar:

- nao dependas de comandos `rag sync` dentro do container;
- usa admin jobs e polling;
- trata `status=queued|running|completed|failed`;
- usa `/status/indexing` para health operacional de indice;
- usa `/status/bm25` para saber se hybrid retrieval esta completo;
- nao assumas que Graphify existe para todos os repos.

## Erros Comuns

| Sintoma | Causa provavel | Verificacao |
| --- | --- | --- |
| `401` | Bearer token ausente/incorreto | `cat infra/docker/secrets/rag_api_key` |
| `404` em `/query/code` | Sem repos configurados | `config/rag/user.toml [repos]` |
| Poucos resultados | Indice vazio ou score threshold alto | `/stats`, `/status/indexing` |
| Sem grafo | Graphify disabled ou ainda nao correu | `/repos`, target `graph` |
| Chat sem contexto | Router escolheu `NO_CONTEXT` ou gate rejeitou | usar `context_mode=both` temporariamente |
| Lentidao em rebuild | embeddings/graph LLM caros | reduzir repos, batches ou usar incremental |
