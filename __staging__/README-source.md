# ai-local-stack

`ai-local-stack` é o ponto de entrada para instalar, configurar e operar o sistema AI Local.

O Stack consome os serviços publicados como imagens GHCR imutáveis. Num workspace de desenvolvimento ainda sem release lock, `make build` pode preparar as 22 imagens a partir dos 16 repos runtime standalone irmãos, sempre com Git limpo e proveniência validada; `PedroMglo/local-ai-sys` nunca é usado como fonte runtime.

Este guia é para Linux.

## 1. Requisitos

Obrigatório:

- Git e Make;
- Python 3.11+ com `venv` e `pip`;
- Docker Engine;
- Docker Compose v2;
- OpenSSL;
- acesso a `ghcr.io` numa revisão publicada.

Recomendado:

- pelo menos 25 GiB livres;
- GPU NVIDIA + NVIDIA Container Toolkit apenas para perfis GPU;
- Ollama apenas quando um perfil/modelo o exigir.

O Stack consegue validar/instalar pré-requisitos suportados em Ubuntu, Debian, Fedora e Arch.

## 2. Clonar e preparar o Stack

```bash
git clone https://github.com/PedroMglo/symbiont.git
cd symbiont
make setup-system
make setup
```

Confirma Docker:

```bash
docker info
docker compose version
```

`make setup` instala apenas tooling Stack-owned. Não instala implementação a partir de clones dos owner repos.

Num workspace de desenvolvimento sem `config/docker/released-images.lock.json`, mantém os 16 repositórios runtime `ai-local-*` standalone ao lado de `ai-local-stack`, ou define `AI_LOCAL_OWNERS_ROOT` para a diretoria que os contém. O fallback local valida `origin`, `HEAD`, worktree limpa e labels OCI antes de selar o lock local; não usa o monorepo histórico.

## 3. Estado local do Stack

O Stack cria estado local que não aparece no GitHub:

```text
secrets/   # credenciais internas geradas pelo Stack
.local/    # TLS, receipts, configuração gerada e evidência local
```

## 4. Escolher PostgreSQL

Revê sempre a partir da raiz do `ai-local-stack`:

```bash
nano config/connections.yml
```

### PostgreSQL pessoal/external

Para um PostgreSQL teu num contentor Docker local:

```yaml
mode: external
host: postgres.localhost
port: 5432
provision: true
```

Com `provision: true`, o Stack pode criar apenas os recursos declarados `ai_local_*` depois de validar o servidor. O lifecycle e a credencial administrativa do PostgreSQL continuam a pertencer ao utilizador.

O contrato external atual exige:

- PostgreSQL 18;
- pgvector 0.8.2 disponível;
- data checksums ativos;
- TLS 1.3;
- mTLS com certificado cliente;
- SCRAM-SHA-256;
- regras host `scram-sha-256` + `clientcert=verify-full`;
- identidade administrativa capaz de criar databases/roles quando `provision: true`;
- ausência de colisões incompatíveis com recursos `ai_local_*`.

Segue o procedimento específico:

**[`docs/.user_plan/EXTERNAL_POSTGRES.md`](docs/.user_plan/EXTERNAL_POSTGRES.md)**

Esse guia mantém todos os comandos na raiz do `ai-local-stack`, gera TLS com `make postgres-tls` e reutiliza o ficheiro de password administrativa que já pertence ao teu PostgreSQL através de:

```bash
export AI_LOCAL_EXTERNAL_POSTGRES_ADMIN_PASSWORD_FILE=/caminho/absoluto/para/o/secret/existente
```

O Stack não te manda recriar, rodar ou copiar essa password para outro secret.

### PostgreSQL managed pelo Stack

```yaml
mode: managed
storage_root: /home/user/.local/share/ai-local/containers
docker_context: default
```

Neste modo o próprio Stack gere o servidor PostgreSQL e o respetivo estado interno.

## 5. Preparar/validar PostgreSQL

```bash
make postgres-setup
```

Em modo external, o comando valida primeiro em modo read-only. Só depois, se `provision: true`, cria ou atualiza os recursos que pertencem ao AI Local. Os secrets internos do AI Local que estiverem em falta são gerados automaticamente.

## 6. Primeiro arranque completo

Quando PostgreSQL estiver pronto:

```bash
make build
make converge
make aliases
make up
make verify-live
```

`make build` é o gate canónico de preparação do runtime. Ele:

1. gera configuração/estado Stack-owned necessário;
2. seleciona a projeção de owner images para esta revisão;
3. numa revisão publicada, puxa as imagens GHCR exatamente pelos digests imutáveis; num workspace de desenvolvimento sem release lock, constrói as 22 imagens a partir dos 16 owner repos standalone validados;
4. constrói as imagens que pertencem ao próprio Stack;
5. instala os artefactos host-native transportados pelos owners e valida manifesto, SHA-256 e revisão;
6. pré-provisiona as identidades TLS Stack-owned que os owner containers consomem read-only;
7. valida Compose, policy, inventário e host Docker;
8. grava um image-build receipt ligado à revisão e às identidades exatas das imagens.

`make converge` é um passo separado e explícito. Ele atualiza a evidência de `postgres-setup`, aplica a política/ativação de storage necessária, converge PostgreSQL para todos os owners e converge os serviços host obrigatórios. Não faz pulls implícitos. O receipt de convergência fica ligado ao **mesmo image-build receipt**.

`make up` é start-only relativamente a owner state: valida build, convergência, artefactos/serviços host e só depois sobe o runtime e executa smoke checks. Não executa migrações, não converge storage/PostgreSQL e não puxa owner images implicitamente.

O utilizador **não resolve, escolhe nem aprova tags de imagens manualmente**. Isso pertence ao processo de release do projeto. O lock local de desenvolvimento também não aceita tags arbitrárias: é gerado pelo Stack a partir do `HEAD` exato de cada owner standalone.

`make aliases` instala/renderiza o cliente `@` a partir da imagem Symbiont selecionada pelo Stack.

Depois podes usar:

```bash
@
```

ou executar o fluxo completo, quando a instalação já está configurada:

```bash
make use
```

## 7. Modelos e perfis opcionais

Os modelos não fazem parte do arranque base:

```bash
make models
```

Seleção/diagnóstico de perfis:

```bash
make profiles
make up-auto
make doctor
make check-gpu
make check-disk
```

`make up-auto` escolhe os perfis adequados e executa `build → converge → aliases → up` com a mesma seleção.

O manifesto canónico das capacidades da máquina pode ser inspecionado sem depender de `RuntimeInfo` interno:

```bash
ai-local-host-capabilities
```

Esse JSON usa o contrato versionado `ai-local.host-capability-manifest.v1` definido por `ai-local-contracts`.

## 8. RAG

Configura fontes pessoais:

```bash
make rag ARGS="--vault-dir $HOME/Obsidian/Vault --repo-path $HOME/src"
```

Para limpar:

```bash
make rag-clear
```

A configuração do utilizador vive em `config/rag/user.toml`; a implementação RAG vem da imagem owner selecionada.

## 9. Ollama host-native

Ollama é opt-in. Ativa `llm.ollama_host_bridge: true` em `config/main.yaml` e usa:

```bash
make ollama-host-config
make ollama-host-apply
AI_COMPOSE_PROFILES=core,storage,ollama-host make build
AI_COMPOSE_PROFILES=core,storage,ollama-host make converge
AI_COMPOSE_PROFILES=core,storage,ollama-host make aliases
AI_COMPOSE_PROFILES=core,storage,ollama-host make up
```

O bridge usa um socket AF_UNIX privado; Ollama continua sem exposição TCP ampla.

## 10. Capacidades host-native dos owners

Duas capacidades permanecem deliberadamente no host sem devolver implementação à Stack:

- Resource Governor: `telemetry-authority.pyz`, transportado na owner image e supervisionado por user systemd após `make converge`;
- Audio: `voice-runtime.pyz`, transportado na imagem Audio e usado opt-in para PipeWire/microfone.

A Stack extrai os `.pyz` da owner image selecionada e exige manifesto `ai-local.host-python-artifact.v1`, revisão Git exata, requirements compatíveis e SHA-256. Os bytes instalados vivem em `.local/host-artifacts/` e não constituem source authority da Stack.

Diagnóstico foreground da telemetria:

```bash
make telemetry-authority
```

Listar targets de áudio PipeWire através do owner Audio:

```bash
make mic-stream-test ARGS="--list"
```

## 11. Service Bus

O Service Bus é o event plane NATS Core/JetStream:

- producers publicam envelopes tipados;
- NATS Core encaminha tráfego efémero/request-reply;
- JetStream persiste event/command/work durável;
- subscribers/consumers recebem ou fazem pull e ACK;
- o efeito de negócio e a idempotência pertencem ao consumer, não ao broker.

APIs HTTPS síncronas continuam explícitas quando o contrato precisa de request/response imediato.

## 12. Segurança e autoridade host

Containerizar um componente não lhe dá autoridade indiscriminada sobre o host:

- Symbiont usa Docker através do proxy restrito;
- Workspace usa o backend/autoridade de execução selecionado;
- Resource Governor recebe telemetria de uma autoridade host-native estreita;
- Voice Runtime permanece host-native para sessão/dispositivos de áudio;
- Storage Guardian vê apenas roots explicitamente montados;
- `/dev/kvm` e GPU são authorities opcionais e separadas.

## 13. Arquitetura e documentação

- [PostgreSQL pessoal/external](docs/.user_plan/EXTERNAL_POSTGRES.md)
- [Guia de utilizador](docs/user-guide.md)
- [Operações](docs/operations.md)
- [Arquitetura](docs/architecture.md)
- [Grafo dos 19 repos + serviços](docs/architecture/dependency-graph.md)
- [Mapa macro de dependências runtime](docs/architecture/RUNTIME_DEPENDENCY_MAP.md)
