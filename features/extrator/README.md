# Extrator Feature

Feature HTTP para ETL documental: extracao, normalizacao, chunks, tabelas e conversoes. O servico nao faz embeddings; produz artefactos que outros fluxos podem consumir via API.

## Contrato API

Base interna: `https://extrator:8000`

Autenticacao: `X-API-Key`/token configurado pelo servico em todos os endpoints operacionais.

| Metodo | Path | Uso |
| --- | --- | --- |
| GET | `/health` | Healthcheck |
| GET | `/v1/extrator/capabilities` | Capacidades anunciadas |
| GET | `/v1/extrator/formats` | Formatos suportados |
| POST | `/v1/extrator/diagnostics/path` | Diagnostico preflight de documento |
| POST | `/v1/extrator/query` | Entrada generica por query; a feature escolhe path/job |
| POST | `/v1/extrator/extractions/path` | Extrair por path visivel no container |
| POST | `/v1/extrator/extractions/upload` | Extrair por upload multipart |
| POST | `/v1/extrator/conversions/path` | Converter por path |
| POST | `/v1/extrator/conversions/upload` | Converter upload |
| GET | `/v1/extrator/jobs/{job_id}` | Estado do job |
| GET | `/v1/extrator/jobs/{job_id}/result` | Resultado concluido |
| GET | `/v1/extrator/documents/{doc_id}` | Manifest do documento |
| GET | `/v1/extrator/documents/{doc_id}/chunks` | Chunks normalizados |
| GET | `/v1/extrator/documents/{doc_id}/tables` | Tabelas extraidas |
| POST | `/v1/extrator/documents/{doc_id}/reprocess` | Reprocessar documento |
| GET | `/v1/extrator/stats` | Estatisticas |
| POST | `/v1/extrator/maintenance/cleanup` | Limpeza administrativa |

Exemplo:

```bash
curl -sS https://extrator:8000/v1/extrator/extractions/path \
  -H "X-API-Key: $EXTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input_path":"/data/input/manual.pdf","recursive":false,"force":false,"targets":["markdown","chunks"],"metadata":{"source":"manual"}}'
```

Resposta inicial:

```json
{"job_id": "job_123", "status": "queued", "status_url": "/v1/extrator/jobs/job_123"}
```

## Integracao

- URL central: `[services].extrator_url`.
- Source map `extrator` em `config/orc/agents.toml` aponta para `/v1/extrator/query`.
- LibreOffice/Pandoc sao ferramentas locais permitidas, nao canais entre containers.
- A selecao de parser vive em `extrator/adapters/registry.py` e e ranqueada por
  `[parsers] parser_priorities`; novos parsers devem entrar no registry, nao em
  branches por extensao no pipeline.
- `/v1/extrator/diagnostics/path` devolve `document_diagnostic.v1` com sinais
  de sensibilidade, idioma, estrutura, necessidade de OCR, custo estimado e
  workflow recomendado (`extract`, `convert`, `sandbox_required` ou `blocked`).
  O diagnostico nao executa conversao, nao cria embeddings e nao gere storage.
- `metadata.parser_selection` declara candidatos, tentativas, parser escolhido
  e fallback usado. Fallback de parser e evento explicito, nao sucesso
  silencioso.
- O bundle gold inclui `evidence.json` com contrato `DocumentEvidence`
  versionado. Esse contrato declara hash da fonte, parser usado, versao,
  selecao/fallback de parser, metricas de qualidade, warnings, truncation,
  decisoes de seguranca, chunks e tabelas sem criar embeddings.
- O bundle gold tambem inclui `rag_bundle_manifest.json` com contrato
  `rag_bundle.v1` para `obsidian-rag`. O manifesto declara que embeddings
  pertencem ao RAG, que publicacao duravel pertence ao `storage_guardian`, e
  quando os chunks normalizados + `DocumentEvidence` permitem reprocessamento
  sem reler o ficheiro original.
- Respostas de `/v1/extrator/query` declaram a acao realizada em `action` e
  `metadata.query_action`: `created_job`, `reused_result`, `blocked` ou
  `no_action`.
- Respostas de `/v1/extrator/query` que selecionam um path incluem
  `metadata.document_diagnostic`, permitindo que o orquestrador leia a decisao
  do owner em vez de manter listas internas de extensoes.
- Quando `/v1/extrator/query` recebe metadata tipada de routing, a capability
  declarada e a autoridade para escolher `extraction` ou `conversion`.
  `document_etl`, `document_extraction`, `rag_bundle` e action `extract`
  selecionam extracao; `file_conversion` ou action `convert` selecionam
  conversao quando tambem existe formato alvo no pedido. O parser de texto fica
  como fallback para chamadas diretas sem contrato tipado.
- Respostas de `/v1/extrator/query` incluem
  `metadata.sandbox_preparation_plan`, que descreve como preparar ou validar a
  entrada numa copia `workspace_execution` antes de conversoes, probes ou
  publicacao duravel. O extrator continua a possuir ETL documental; a sandbox
  possui execucao descartavel e o storage_guardian possui persistencia duravel.
- Conversoes por path escrevem primeiro em scratch do extrator e pedem ao
  `storage_guardian` para materializar o ficheiro final. Sem destino explicito,
  o destino e o mesmo diretorio do ficheiro original com a extensao alvo; para
  diretorios, a regra e aplicada a cada ficheiro convertido.
- O manifesto owner-local `features/extrator/service_capabilities.toml`
  anuncia `document_diagnosis`, `document_workflow_selection`,
  `document_extraction`, `file_conversion`, `rag_bundle` e
  `workspace_sandbox_preparation_plan` para consumo declarativo pelo
  orquestrador.
