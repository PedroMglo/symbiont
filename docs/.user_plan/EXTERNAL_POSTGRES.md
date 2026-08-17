---
owner: postgres
purpose: prepare-existing-user-owned-external-postgresql
validated: 2026-08-16
---
# Ligar um PostgreSQL pessoal ao AI Local

Este guia é para um PostgreSQL **que já existe e pertence ao utilizador**. O AI Local não passa a gerir o lifecycle desse servidor. Com `provision: true`, cria apenas os recursos `ai_local_*` depois de validar o contrato do servidor.

## Regra de execução

Executa todos os comandos a partir da raiz de `ai-local-stack`:

```bash
cd "$HOME/_projects/ai-local-stack"   # ajusta se clonaste noutro caminho
STACK_ROOT="$(pwd -P)"
test -f Makefile
test -d postgres
```

Não é necessário entrar no diretório onde manténs o Compose do teu PostgreSQL.

## 1. Identificar o servidor e reutilizar o secret administrativo existente

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
PG_CONTAINER="NOME_REAL_DO_TEU_CONTENTOR"
docker inspect "$PG_CONTAINER" >/dev/null
```

O PostgreSQL já deve ter uma password administrativa. **Não a recries nem a copies para outro secret por causa do AI Local.** Aponta o Stack para o ficheiro existente:

```bash
PG_ADMIN_PASSWORD_FILE="/caminho/absoluto/para/o/secret/existente"
export AI_LOCAL_EXTERNAL_POSTGRES_ADMIN_PASSWORD_FILE="$PG_ADMIN_PASSWORD_FILE"
test -f "$PG_ADMIN_PASSWORD_FILE"
test ! -L "$PG_ADMIN_PASSWORD_FILE"
stat -c '%a %n' "$PG_ADMIN_PASSWORD_FILE"
```

O Stack usa esse ficheiro diretamente e read-only durante o setup/convergência.

## 2. Declarar PostgreSQL external no Stack

Edita `config/connections.yml`:

```yaml
mode: external
host: postgres.localhost
port: 5432
provision: true
```

`provision: true` permite criar/atualizar apenas databases, roles, schemas e ACLs que pertencem ao AI Local.

## 3. Confirmar os requisitos do servidor

O contrato external atual exige PostgreSQL 18, pgvector 0.8.2 e data checksums ativos:

```bash
docker exec --user postgres "$PG_CONTAINER" \
  psql --dbname=postgres --tuples-only --no-align \
  --command='SHOW server_version_num;' \
  --command='SHOW data_checksums;' \
  --command="SELECT default_version FROM pg_available_extensions WHERE name='vector';"
```

Esperado:

```text
18....
on
0.8.2
```

Se checksums estiverem `off` ou pgvector/versão não corresponderem, corrige o teu deployment antes de continuar. O Stack não relaxa estes requisitos automaticamente.

## 4. Gerar TLS do AI Local

```bash
make postgres-tls
```

Confirma:

```bash
test -f "$STACK_ROOT/.local/postgres-tls/server/ca.crt"
test -f "$STACK_ROOT/.local/postgres-tls/server/tls.crt"
test -f "$STACK_ROOT/.local/postgres-tls/server/tls.key"
test -f "$STACK_ROOT/.local/postgres-tls/clients/admin/ca.crt"
test -f "$STACK_ROOT/.local/postgres-tls/clients/admin/tls.crt"
test -f "$STACK_ROOT/.local/postgres-tls/clients/admin/tls.key"
```

Os certificados cliente ficam do lado AI Local. Apenas o material TLS **server** é instalado no teu PostgreSQL.

## 5. Instalar TLS server no PostgreSQL

Obtém o `data_directory` real:

```bash
PGDATA="$(docker exec --user postgres "$PG_CONTAINER" \
  psql --dbname=postgres --tuples-only --no-align \
  --command='SHOW data_directory;' | xargs)"
printf 'PGDATA=%s\n' "$PGDATA"
```

Instala o material server:

```bash
docker exec --user root "$PG_CONTAINER" \
  install -d -o postgres -g postgres -m 0700 "$PGDATA/ai-local-tls"

docker cp "$STACK_ROOT/.local/postgres-tls/server/ca.crt" \
  "$PG_CONTAINER:$PGDATA/ai-local-tls/ca.crt"
docker cp "$STACK_ROOT/.local/postgres-tls/server/tls.crt" \
  "$PG_CONTAINER:$PGDATA/ai-local-tls/tls.crt"
docker cp "$STACK_ROOT/.local/postgres-tls/server/tls.key" \
  "$PG_CONTAINER:$PGDATA/ai-local-tls/tls.key"

docker exec --user root "$PG_CONTAINER" \
  chown -R postgres:postgres "$PGDATA/ai-local-tls"
docker exec --user root "$PG_CONTAINER" \
  chmod 0600 "$PGDATA/ai-local-tls/tls.key"
```

## 6. Aplicar a política TLS/SCRAM/HBA

Faz primeiro backup do HBA atual:

```bash
docker exec --user postgres "$PG_CONTAINER" sh -lc \
  'hba="$(psql -At -d postgres -c "SHOW hba_file")"; cp "$hba" "${hba}.before-ai-local"; echo "${hba}.before-ai-local"'
```

Instala a política do Stack:

```bash
docker cp "$STACK_ROOT/postgres/runtime/pg_hba.conf" \
  "$PG_CONTAINER:$PGDATA/ai-local-pg_hba.conf"
docker exec --user root "$PG_CONTAINER" \
  chown postgres:postgres "$PGDATA/ai-local-pg_hba.conf"
docker exec --user root "$PG_CONTAINER" \
  chmod 0600 "$PGDATA/ai-local-pg_hba.conf"
```

Aplica a configuração e reinicia:

```bash
docker exec --user postgres "$PG_CONTAINER" \
  psql --dbname=postgres --set=ON_ERROR_STOP=1 \
  --command="ALTER SYSTEM SET listen_addresses = '*';" \
  --command="ALTER SYSTEM SET password_encryption = 'scram-sha-256';" \
  --command="ALTER SYSTEM SET hba_file = '$PGDATA/ai-local-pg_hba.conf';" \
  --command="ALTER SYSTEM SET ssl = 'on';" \
  --command="ALTER SYSTEM SET ssl_ca_file = '$PGDATA/ai-local-tls/ca.crt';" \
  --command="ALTER SYSTEM SET ssl_cert_file = '$PGDATA/ai-local-tls/tls.crt';" \
  --command="ALTER SYSTEM SET ssl_key_file = '$PGDATA/ai-local-tls/tls.key';" \
  --command="ALTER SYSTEM SET ssl_min_protocol_version = 'TLSv1.3';"

docker restart "$PG_CONTAINER"
```

## 7. Ligar o PostgreSQL à rede external do AI Local

```bash
docker network inspect ai-local-postgres-external >/dev/null 2>&1 || \
  docker network create \
    --driver bridge \
    --label ai.local.managed=true \
    --label ai.local.owner=postgres \
    --label ai.local.service=postgres-network \
    ai-local-postgres-external

docker network disconnect ai-local-postgres-external "$PG_CONTAINER" 2>/dev/null || true
docker network connect \
  --alias postgres.localhost \
  ai-local-postgres-external \
  "$PG_CONTAINER"
```

## 8. Confirmar TLS/HBA

```bash
docker exec --user postgres "$PG_CONTAINER" \
  psql --dbname=postgres --tuples-only --no-align \
  --command='SHOW ssl;' \
  --command='SHOW ssl_min_protocol_version;' \
  --command='SHOW password_encryption;' \
  --command='SHOW data_checksums;'
```

Esperado:

```text
on
TLSv1.3
scram-sha-256
on
```

Confirma também que `pg_hba_file_rules.error` está vazio:

```bash
docker exec --user postgres "$PG_CONTAINER" \
  psql --dbname=postgres \
  --command='SELECT line_number, type, database, user_name, address, auth_method, error FROM pg_hba_file_rules ORDER BY line_number;'
```

## 9. Preparar PostgreSQL para o AI Local

Mantém a variável administrativa exportada na mesma shell:

```bash
test -n "$AI_LOCAL_EXTERNAL_POSTGRES_ADMIN_PASSWORD_FILE"
make postgres-setup
```

O setup valida primeiro read-only e só depois provisiona recursos AI Local quando `provision: true`. Também gera automaticamente os secrets internos do AI Local que estiverem em falta, sem duplicar o teu secret administrativo.

Se falhar por `CONNECT to PUBLIC`, usa apenas a remediação explícita apresentada pelo setup depois de rever o impacto noutras aplicações. Para pgvector/TLS/HBA/checksums/versão, corrige exatamente o requisito indicado e repete `make postgres-setup`.

## 10. Construir, convergir e arrancar

Depois de `make postgres-setup` passar:

```bash
make build
make converge
make aliases
make up
make verify-live
```

A separação é intencional:

- `make build` puxa owner images imutáveis, constrói apenas Stack-owned, valida e grava o image-build receipt;
- `make converge` volta a validar o setup external, converge storage/PostgreSQL de todos os owners e grava um receipt ligado ao build atual;
- `make up` **não** provisiona, migra nem converge implicitamente; só arranca se os receipts atuais forem válidos;
- `make verify-live` comprova o runtime real depois do arranque.

Se `make build` indicar que falta `released-images.lock.toml`, isso é um problema da revisão/release do Stack e **não** da configuração do teu PostgreSQL. O utilizador não escolhe tags nem resolve owner images manualmente.

## Resumo

Tudo é executado em `ai-local-stack/`:

```text
1. identificar PG_CONTAINER e reutilizar o secret admin existente
2. editar config/connections.yml
3. confirmar PostgreSQL 18 + checksums + pgvector 0.8.2
4. make postgres-tls
5. instalar TLS server + pg_hba.conf no PostgreSQL
6. ativar TLS 1.3/SCRAM e reiniciar o servidor
7. ligar o contentor a ai-local-postgres-external como postgres.localhost
8. exportar AI_LOCAL_EXTERNAL_POSTGRES_ADMIN_PASSWORD_FILE
9. make postgres-setup
10. make build
11. make converge
12. make aliases
13. make up
14. make verify-live
```
