# Research Feature

Feature HTTP para pesquisa semantica e CAG. E a ponte API entre o symbiont e o RAG; nao executa comandos de reprocessamento e nao chama outros servicos fora de HTTP.

## Contrato API

Base interna: `https://research:8000`

Autenticacao: `Authorization: Bearer <service-token>` em `/v1/research/search` e `/v1/research/cag`.

| Metodo | Path | Uso |
| --- | --- | --- |
| GET | `/health` | Healthcheck e reachability do RAG |
| GET | `/v1/research/capabilities` | Capacidades anunciadas |
| POST | `/v1/research/sources/prepare` | Regista fontes locais pedidas pelo user e pede reprocessamento ao RAG |
| POST | `/v1/research/search` | Pesquisa notas/codigo + CAG |
| GET | `/v1/research/cag?intent=code&budget=2000` | Packs CAG precomputados |

Preparar uma fonte local pedida em runtime:

```bash
curl -sS https://research:8000/v1/research/sources/prepare \
  -H "Authorization: Bearer $INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sources":[{"path":"/home/user/Documents/course","source_type":"auto"}],"target":"sources","wait_seconds":60}'
```

Depois de preparada, uma pesquisa pode restringir-se a essa fonte pelo
`namespace` devolvido pelo RAG, normalmente o nome da pasta:

```json
{"query":"principais temas desta pasta","namespace":"course","budget_tokens":2000,"include_code":true,"intent":"broad"}
```

Exemplo:

```bash
curl -sS https://research:8000/v1/research/search \
  -H "Authorization: Bearer $INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"como esta o grafo do projeto?","budget_tokens":2000,"include_code":true,"intent":"code"}'
```

Resposta:

```json
{
  "content": "Research evidence answerability: answerable.\nReason: usable evidence returned within current context bounds.\nFlags: none.\nPlan: intent=code; retrieval_modes=rag_notes, rag_code, cag_pack; budget_tokens=2000.\n\n[R1] notes | rag_notes | score=0.74 | freshness=unknown | daily.md#Dispatch\ncontexto",
  "results": [
    {
      "source": "rag",
      "source_type": "notes",
      "content": "contexto",
      "score": 0.74,
      "citation_ref": "daily.md#Dispatch",
      "retrieval_mode": "rag_notes",
      "token_cost": 120,
      "freshness": "unknown",
      "limits": {"top_k": 5}
    }
  ],
  "total_tokens": 1200,
  "status": "ok",
  "source_statuses": [
    {"source": "notes", "source_type": "notes", "status": "ok", "result_count": 1},
    {"source": "code", "source_type": "code", "status": "ok", "result_count": 1},
    {"source": "cag", "source_type": "cag", "status": "no_results", "result_count": 0},
    {"source": "budget", "source_type": "budget", "status": "ok", "limits": {"budget_tokens": 2000}}
  ],
  "degraded": false,
  "metadata": {
    "answerability": "answerable",
    "answerability_reason": "usable evidence returned within current context bounds",
    "evidence_flags": [],
    "sources_used": ["notes"],
    "bounded_tokens": 66,
    "max_tokens": 1200
  },
  "query_plan": {
    "requested_intent": "code",
    "normalized_intent": "code",
    "include_code": true,
    "include_code_reason": "code intent requires code retrieval when the caller permits it",
    "budget_tokens": 2000,
    "budget_reason": "code-focused recall: budget_tokens=2000, notes_top_k=5, code_top_k=5",
    "pack_selection": "code",
    "pack_selection_reason": "code intent selects code CAG packs",
    "notes_payload": {"query": "como esta o grafo do projeto?", "top_k": 5},
    "code_payload": {"query": "como esta o grafo do projeto?", "top_k": 5},
    "cag_payload": {"intent": "code", "budget": 2000},
    "retrieval_modes": ["rag_notes", "rag_code", "cag_pack"]
  },
  "evidence_bundle": [
    {
      "source": "rag",
      "source_type": "notes",
      "content": "contexto",
      "citation_ref": "daily.md#Dispatch",
      "score": 0.74,
      "retrieval_mode": "rag_notes",
      "token_cost": 120,
      "freshness": "unknown",
      "limits": {"top_k": 5}
    }
  ],
  "reasoning_context": {
    "answerability": "answerable",
    "answerability_reason": "usable evidence returned within current context bounds",
    "flags": [],
    "citations": [
      {
        "ref": "R1",
        "citation_ref": "daily.md#Dispatch",
        "source": "rag",
        "source_type": "notes",
        "retrieval_mode": "rag_notes",
        "score": 0.74,
        "freshness": "unknown"
      }
    ],
    "bounded_tokens": 66,
    "max_tokens": 1200,
    "sources_used": ["notes"]
  },
  "retrieval_traces": [
    {
      "trace_ref": "research.trace:notes:ok",
      "source": "notes",
      "source_type": "notes",
      "retrieval_mode": "rag_notes",
      "status": "ok",
      "result_count": 1,
      "searched": true,
      "request": {"query": "como esta o grafo do projeto?", "top_k": 5},
      "miss_reasons": [],
      "evidence_refs": ["research.trace:notes:ok", "research.evidence:daily.md#Dispatch"]
    }
  ],
  "miss_review": {
    "should_record": false,
    "event_type": "rag.miss",
    "producer": "research",
    "reason": "usable evidence was available; no rag.miss event requested",
    "evidence_refs": ["research.trace:notes:ok", "research.evidence:daily.md#Dispatch"]
  },
  "limits": {"budget_tokens": 2000}
}
```

`results` permanece como forma compacta consumida por clientes existentes. A
forma AGI-grade fica em `evidence_bundle`: cada item carrega provenance
uniforme para notas, codigo e CAG (`source_type`, `citation_ref`,
`retrieval_mode`, `score`, `token_cost`, `freshness` e `limits`). Falhas
parciais aparecem em `source_statuses`; quando alguma fonte falha mas ainda ha
evidencia utilizavel, `status` vira `degraded` e `degraded=true`.

`content` e `reasoning_context` sao a forma pronta para
`reasoning_and_response`: um bloco bounded com citacoes `[R1]`, answerability e
flags (`insufficient_evidence`, `stale_evidence`, `truncated_evidence`,
`degraded_sources`). `metadata` replica os sinais principais para o dispatch sem
obrigar consumidores a reprocessarem `results`.

`retrieval_traces` explica cada fonte consultada ou saltada (`notes`, `code`,
`cag`, `budget`), incluindo payload planeado, status, miss reasons e refs
estaveis. Quando a resposta fica `insufficient`, `miss_review.should_record`
indica ao `orchestrator` que deve gravar `rag.miss`; a feature apenas emite o
hint e nao assume ownership do ledger, revisao ou reindexacao.

`query_plan` documenta como a feature transformou `intent`, `budget_tokens` e
`include_code` em payloads HTTP para o `obsidian-rag`. Intencoes genericas
aceites: `factual`, `code`, `graph`, `historical`, `broad` e `narrow`
(`general`, `local` e `system` continuam aceites para compatibilidade). O plano
inclui razoes explicitas para `include_code`, estrategia de budget e selecao de
packs CAG.

## Integracao

- URL da feature: `[services].research_url`.
- URL do RAG consumido pela feature: `features/research/config.toml` ou override de ambiente do servico.
- Reprocessar RAG/Graphify/CAG e sempre API do RAG: `POST /admin/reprocess`.
- Ranking, graph enrichment e geracao de packs continuam pertencendo ao
  `obsidian-rag`; `research` apenas normaliza os payloads recebidos por HTTP.
