# ai-local

Este repo junta a stack local `ai-local`: configuracao central, storage
guardian, symbiont, RAG, agentes/features, modelos locais e observabilidade.

O utilizador final nao precisa de chamar APIs manualmente para usar este repo.
O caminho principal e preparar a configuracao local, subir a stack e conversar
com os agentes atraves do alias `@`.

## Caminho Principal

1. [Guia de utilizador](docs/user-guide.md) - passo a passo end-to-end ate conversar
   com os agentes via `@`.
2. [Documentacao por owner](docs/owners/README.md) - detalhe por `agents/`,
   `features/`, `config/`, `storage_guardian`, `orchestrator`, `obsidian-rag`
   e `infra/`.
3. [Arquitetura](docs/architecture.md) - como os servicos se
   ligam por baixo.
4. [Operacoes](docs/operations.md) - comandos, perfis, storage e
   troubleshooting.
5. [Backlog de implementacao](docs/implementation-backlog.md) - trabalho futuro
   ainda valido, reunido num unico documento.

## Configuracao Manual Importante

Antes do primeiro arranque, revê:

- [config/main.yaml](config/main.yaml): storage, hardware, LLM, limites,
  portas e privacidade.
- [config/rag/user.toml](config/rag/user.toml): vaults, repositorios e
  Graphify.
- [config/models/orc.config.json](config/models/orc.config.json): alias
  `@` e politica de modelos.

## Mapa Rapido

```mermaid
flowchart LR
    U[Utilizador no terminal] --> A[@]
    A --> O[Orchestrator]
    O --> R[RAG]
    O --> AG[Agentes e features]
    O --> L[Ollama, llama.cpp ou vLLM]
    R --> Q[Qdrant]
    R --> V[Vaults e repos configurados]
    AG --> S[Storage Guardian]
```

# Primeiro Arranque Linux

Depois de clonar o repo, a sequencia canonica para um utilizador novo e:

```bash
cd ai-local

make setup-system   # imprime prerequisitos de sistema por distro
make setup
make models
make infra
make up
make verify-live
```

Para deixar o projeto escolher os perfis maximos seguros desta maquina:

```bash
make profiles
make up-auto
make verify-max-live
```

`make dev` continua disponivel para desenvolvimento, com dependencias dev e
pre-commit. O caminho normal de uso e `make setup`.

Para configurar fontes pessoais do RAG sem editar TOML manualmente:

```bash
make rag ARGS="--vault-dir ~/Obsidian/Vault --repo-path ~/src"
```

<!-- private-intake smoke test 2026-07-04T13:25:32Z -->
