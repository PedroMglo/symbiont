# Translation Feature

Feature HTTP local para normalizacao linguistica, lint PT-PT e spellcheck. E uma API auxiliar; nao deve ser usada por CLI entre containers.

## Contrato API

Base interna: `https://translation:8590`

Autenticacao: os endpoints POST usam o middleware partilhado de auth de `sharedai.servicekit`.

| Metodo | Path | Uso |
| --- | --- | --- |
| GET | `/health` | Estado do dicionario/tradutor |
| POST | `/v1/normalize` | Normalizar e opcionalmente traduzir |
| POST | `/v1/lint-ptpt` | Corrigir variantes PT-BR para PT-PT |
| POST | `/v1/spellcheck` | Spellcheck tokenizado |

Exemplo:

```bash
curl -sS https://translation:8590/v1/normalize \
  -H "Content-Type: application/json" \
  -d '{"text":"analisa este arquivo","source_language_hint":"pt-PT","target_language":"en","translate":true,"spellcheck":true}'
```

Resposta:

```json
{
  "original": "analisa este arquivo",
  "normalized": "analisa este ficheiro",
  "translated": "analyze this file",
  "working_query": "analyze this file",
  "source_language": "pt",
  "target_language": "en",
  "fallback_used": false,
  "semantic_drift_score": 0.1,
  "confidence": 0.85,
  "translation_safe": true,
  "quality": {
    "mode": "assisted",
    "semantic_drift_score": 0.1,
    "confidence": 0.85,
    "drift_risk": "low",
    "assessed_by": "deterministic_guardrails",
    "confidence_reason": "assisted_translation_guardrails"
  },
  "safety_error": null,
  "language_context": {
    "contract_version": "translation.language_context.v1",
    "mode": "assisted",
    "original_query": "analisa este arquivo",
    "normalized_query": "analisa este ficheiro",
    "working_query": "analyze this file",
    "source_language": "pt",
    "target_language": "en",
    "transformations": [],
    "warnings": [],
    "protected_spans": {
      "count": 0,
      "before_hash": "e3b0c442...",
      "after_hash": "e3b0c442...",
      "hashes_match": true,
      "altered": false,
      "missing_kinds": []
    },
    "fallback_used": false,
    "fallback_reason": null,
    "translation_applied": true,
    "cache_hit": false,
    "semantic_drift_score": 0.1,
    "confidence": 0.85,
    "translation_safe": true,
    "quality": {
      "mode": "assisted",
      "semantic_drift_score": 0.1,
      "confidence": 0.85,
      "drift_risk": "low",
      "assessed_by": "deterministic_guardrails",
      "confidence_reason": "assisted_translation_guardrails"
    },
    "safety_error": null
  },
  "latency_ms": 12.3
}
```

## LanguageContext

`/v1/normalize` devolve `language_context` versionado como contrato canonico
para consumidores HTTP. Os campos legados no topo da resposta continuam
presentes para compatibilidade.

Campos obrigatorios do contrato:

- `original_query`, `normalized_query` e `working_query`.
- `source_language`, `source_variant` e `target_language`.
- `transformations`, com as etapas de spans, spellcheck, glossary, translation,
  cache e fallback.
- `warnings`, para degradacoes como fallback ou drift heuristico.
- `protected_spans.before_hash` e `protected_spans.after_hash`; hashes divergentes
  indicam que o texto final nao preservou todos os spans protegidos.
- `fallback_used` e `fallback_reason`.
- `semantic_drift_score`, `confidence`, `translation_safe`, `quality` e
  `safety_error`.

`quality` mede drift e confianca por modo (`off`, `shadow`, `assisted`,
`enforce`) usando guardrails deterministicos locais. Ele valida idioma-alvo,
fallbacks, spans protegidos, saidas vazias e degradacoes conhecidas; nao
substitui avaliacao semantica por modelo quando essa existir como capacidade
futura.

`safety_error` e preenchido quando uma tentativa de traducao nao e segura, por
exemplo quando o backend nao preserva todos os spans protegidos. Nesses casos a
feature devolve fallback normalizado e registra um erro tipado com `code`,
`stage`, `severity`, `fallback_applied` e `protected_span_kinds`.

## Integracao

- URL central: `[services].translation_url` e `config/orc/i18n.toml`.
- Usar sempre HTTP; nao chamar normalizadores por subprocess entre containers.
- O orquestrador pode usar `language_context` como primeira etapa de pedidos
  user-facing para criar uma working query em ingles, mas a translation nao
  decide routing, policy, selecao de agente ou resposta final.
- Consumidores como `reasoning_and_response`, `material_builder` e RAG dual-query
  devem tratar `translation_safe=false`, `safety_error` ou `quality.drift_risk`
  alto como bloqueio ao uso da working query traduzida.
- Endpoints POST exigem token interno via `X-API-Key` ou `Authorization: Bearer`.
- Logs locais instalam filtro de redacao para chaves, headers de auth e campos de
  texto bruto como `text`, `original_query`, `normalized_query`, `working_query`
  e `translated`.
- Se uma traducao devolver texto sem todos os spans protegidos, a resposta cai
  para o texto normalizado, marca `fallback_reason=protected_spans_altered` e
  preserva os hashes finais dos spans.
