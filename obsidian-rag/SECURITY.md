# Security Policy — obsidian-rag

## Versões suportadas

| Versão | Suportada |
| ------ | --------- |
| 0.4.x  | ✅ Sim    |
| < 0.4  | ❌ Não    |

Apenas a versão mais recente recebe patches de segurança.

## Reportar vulnerabilidades

**Não abras uma issue pública para vulnerabilidades de segurança.**

Usa o GitHub Security Advisories:

1. Vai a **Settings → Security → Advisories**
2. Clica **"Report a vulnerability"**
3. Descreve o problema, impacto e passos para reproduzir

Resposta esperada em 7 dias. Disclosure coordenado após fix disponível.

## Modelo de segurança

O obsidian-rag é uma ferramenta **local-first**:

- **Todos os dados ficam na máquina do utilizador** — notas, embeddings, grafos, base vetorial
- **Nenhum dado é enviado para serviços externos** — embeddings via Ollama local, chunking via AST stdlib
- **A API corre em `127.0.0.1` por defeito** — não acessível na rede local
- **Qdrant opera localmente sem telemetria externa**

### API REST

| Configuração                     | Comportamento                                |
| -------------------------------- | -------------------------------------------- |
| `host = "127.0.0.1"` (defeito)   | API acessível apenas localmente              |
| `host = "0.0.0.0"` sem `api_key` | **Recusado** — a aplicação não arranca       |
| `host = "0.0.0.0"` com `api_key` | API acessível na LAN com autenticação Bearer |

A autenticação usa comparação timing-safe (`secrets.compare_digest`).
Rate limiting configurável por minuto (global + `/chat`).

### Validação de paths

Ao editar `config/rag/user.toml`, evita indexar directórios perigosos:

- Raízes do sistema: `/`, `/usr`, `/bin`, `/etc`, `C:\`, `C:\Windows`
- Home inteiro: `~`, `/home`, `/Users`
- Directórios sensíveis: chaves SSH, GnuPG, configurações de utilizador e `~/Library`
- Directórios de desenvolvimento: `.git`, `.venv`, `node_modules`, `__pycache__`

Symlinks são resolvidos antes da validação (e.g. `/bin` → `/usr/bin`).

### Docker

- Container corre como user não-root (`rag`, UID 1000)
- Volume `data/` é o único directório writable
- `rag.user.toml` e `rag.internal.toml` montados em read-only
- Porta publicada em `127.0.0.1` por defeito via `compose.yml`
- `HEALTHCHECK` integrado no Dockerfile

## Scope

Esta política cobre:

- Código Python em `obsidian_rag/`
- Dockerfile e compose em `infra/docker/`
- Configuração (`rag.user.toml`, `rag.internal.toml`, variáveis de ambiente)
- Workflows GitHub Actions

**Fora de scope:**

- Vulnerabilidades no Ollama, Qdrant, ou dependências upstream — reportar directamente aos projectos respectivos
- Configuração do sistema operativo do utilizador
- Segurança da rede local do utilizador

## Recomendações de utilização segura

1. **Não expor a API sem autenticação** — se usares `host = "0.0.0.0"`, define sempre `api_key`
2. **Não indexar directórios sensíveis** — verifica sempre o teu `config/rag/user.toml`
3. **Manter dependências actualizadas** — Dependabot cria PRs automáticos para vulnerabilidades conhecidas
4. **Usar Docker com defaults** — `compose.yml` já tem bind local e read-only config
5. **Não commitar `rag.user.toml` com secrets** — ambos estão no `.gitignore`
