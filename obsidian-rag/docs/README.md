# Obsidian RAG — Documentacao

Esta pasta documenta o `obsidian-rag` como componente RAG do mono-repo
`ai-local`.

O objetivo do projeto e disponibilizar uma API local de RAG para:

- notas Obsidian em Markdown;
- repositorios Git e documentacao tecnica;
- knowledge graphs estruturais via Graphify;
- contexto cached via CAG;
- chat proxy para Ollama com injecao seletiva de contexto;
- observabilidade opcional em ClickHouse.

## Como Ler

Se estas a estudar o projeto pela primeira vez, usa esta ordem:

1. [Visao geral](overview.md) — o que o servico faz, quais sao as pecas e o modelo mental.
2. [Arquitetura](architecture.md) — componentes, dados persistidos e diagramas.
3. [API HTTP](api.md) — contrato dos endpoints e exemplos.
4. [Configuracao](configuration.md) — ficheiros, variaveis de ambiente e secrets.
5. [Pipeline de ingestao](ingestion.md) — notas/repos para embeddings e Qdrant.
6. [Retrieval e chat](retrieval.md) — router, pesquisa hibrida, reranker, gate e contexto final.
7. [Graphify](graphify.md) — criacao e consulta dos grafos estruturais.
8. [CAG](cag.md) — packs de contexto cached.
9. [Observabilidade](observability.md) — eventos, ClickHouse e dashboard.
10. [Servicos independentes](services.md) — como integrar cada peca isoladamente.
11. [Integracao externa](integration.md) — como usar o servico noutras apps.
12. [Diagramas](diagrams.md) — mapas Mermaid reunidos num unico ficheiro.

## Contrato Operacional Atual

O caminho operacional e API-first. Para uso normal, nao chames comandos dentro do container para reindexar. Usa:

```bash
curl -sS https://127.0.0.1:8484/admin/reprocess \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target":"all","force":false}'
```

Os comandos operacionais vivem no Makefile da raiz. A documentacao desta pasta
privilegia a API porque e o contrato suportado pelo README e pelo proprio
codigo da API.

## Superficie Publica

Base local publicada:

```text
https://127.0.0.1:8484
```

Base interna Docker:

```text
https://rag:8484
```

Autenticacao:

```text
Authorization: Bearer <RAG_API_KEY>
```

Excecoes sem autenticacao: `/health`, `/docs`, `/openapi.json`, `/redoc` e `/dashboard/*`.
