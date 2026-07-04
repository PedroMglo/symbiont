# Personal Context Feature

Feature HTTP para calendario, email e RSS. E consumida pelo symbiont como fonte de contexto pessoal e nao deve ser chamada por imports diretos.

## Contrato API

Base interna: `https://personal-context:8000`

Autenticacao: `Authorization: Bearer <service-token>` nos endpoints de dados.

| Metodo | Path | Uso |
| --- | --- | --- |
| GET | `/health` | Estado e fontes ativadas |
| GET | `/v1/personal/capabilities` | Capacidades anunciadas |
| GET | `/v1/personal/calendar` | Eventos recentes/proximos |
| GET | `/v1/personal/email` | Emails recentes |
| GET | `/v1/personal/feeds` | RSS/Atom recentes |

Exemplo:

```bash
curl -sS https://personal-context:8000/v1/personal/calendar \
  -H "Authorization: Bearer $INTERNAL_API_KEY"
```

Resposta:

```json
{"events": [{"summary": "evento", "start": "2026-06-06T12:00:00", "location": ""}], "window_days": 7}
```

## Integracao

- URL central: `[services].personal_context_url`.
- Configuracao de fontes fica em `config/orc/providers.toml` nas secoes `[calendar]`, `[email]` e `[rss]`.
- Dados pessoais so devem sair pela API autenticada.
