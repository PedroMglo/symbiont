# ai-local-stack - user-facing project commands

.DEFAULT_GOAL := help
.NOTPARALLEL: infra build converge postgres-tls postgres-setup up up-auto use

PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf '%s\n' .venv/bin/python; else command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null || printf python3; fi)
TAIL ?= 80
DOCKER_CACHE_MAX ?=
TESTS ?= tests/test_infra_ops_contract.py tests/test_observability_stack_contract.py tests/config/test_host_capabilities.py tests/test_make_lifecycle_contract.py tests/test_host_artifacts.py tests/test_host_services.py tests/test_host_lifecycle_wrapper.py tests/test_mic_stream_launcher.py tests/test_local_owner_repos.py tests/test_owner_tls_preprovision.py tests/test_docker_catalog_host_capabilities.py tests/postgres/test_resource_governor_schema_owner_asset.py tests/tools/test_docs_center.py
INFRA := $(PYTHON) scripts/docker/infra_ops.py
STRESS_V2_CONCURRENCY ?= 8
STRESS_V2_REPEAT ?= 2

.PHONY: help \
        setup setup-system bootstrap \
        postgres-tls postgres-setup models infra build converge infra-config infra-build infra-validate up up-auto profiles \
        verify verify-live verify-live-lifecycle-prod use aliases \
        telemetry-authority mic-stream-test \
        security-audit security-test security-report security-image-audit verify-security \
        doctor check-gpu check-disk ollama-host-config ollama-host-apply rag rag-clear \
        status logs down rollback docker-disk-report docker-safe-prune \
        docker-inventory docker-optimization volumes-status backup-dry-run restore-dry-run \
        command-sandbox-audit python-runtime-audit docker-shield-report docker-runtime-smoke \
        resilience-test slo-report restore-test chaos-local daily-report slo-trends \
        dev test lint sql-assets-check legacy-detect legacy-line-inventory legacy-cleanup-test verify-no-legacy \
        check-doc-targets docs-check docs-generate docs-impact docs-search docs-public-check docs-mermaid-check \
        stress-prod stress-prod-v2 stress-prodV2 stress-summary clean

help:
	@printf '%s\n' \
		'' \
		'ai-local-stack: sequencia principal' \
		'  1. make setup-system   # instala/valida prerequisitos Linux' \
		'  2. make setup          # instala tooling Stack-owned' \
		'  3. escolhe PostgreSQL em config/connections.yml' \
		'  4. make postgres-tls   # gera TLS local quando usas PostgreSQL external' \
		'  5. make postgres-setup # valida/prepara PostgreSQL e gera secrets internos em falta' \
		'  6. make build          # Config Center + owner pulls/builds + TLS owner isolado + host artifacts + receipt' \
		'  7. make converge       # converge storage/PostgreSQL + host services; sem pulls implicitos' \
		'  8. make aliases        # instala @ a partir da imagem Symbiont aprovada' \
		'  9. make up             # start-only; valida receipts/host services; sem pulls/convergencia' \
		' 10. make verify-live    # confirma o runtime real' \
		'' \
		'PostgreSQL external' \
		'  docs/.user_plan/EXTERNAL_POSTGRES.md' \
		'  AI_LOCAL_EXTERNAL_POSTGRES_ADMIN_PASSWORD_FILE=/path/absoluto/secret make postgres-setup' \
		'' \
		'Operacao diaria' \
		'  make build             # prepara/seal build, TLS owner read-only e host artifacts' \
		'  make converge          # converge estado e host services requeridos' \
		'  make up                # nunca converge/migra/puxa implicitamente' \
		'  make status' \
		'  make logs FOLLOW=1' \
		'  make down' \
		'  make rollback' \
		'  make docker-disk-report' \
		'  make docker-safe-prune' \
		'' \
		'Host-native owner capabilities' \
		'  make telemetry-authority # foreground diagnostic; runtime normal e supervisionado por converge' \
		'  make mic-stream-test ARGS="--list" # Voice Runtime vindo do owner Audio' \
		'' \
		'Compatibilidade/diagnostico Stack' \
		'  make infra             # alias compatível de make build' \
		'  make infra-config      # apenas regenera env/config Stack-owned' \
		'  make infra-build       # apenas build mecanico; nao sela receipt' \
		'  make infra-validate' \
		'' \
		'Opcional' \
		'  make models            # downloads/model serving apenas quando os perfis escolhidos exigem' \
		'  make profiles' \
		'  make up-auto' \
		'  make rag ARGS="--vault-dir $$HOME/Obsidian/Vault --repo-path $$HOME/src"' \
		'  make ollama-host-config' \
		'  make ollama-host-apply' \
		'' \
		'Desenvolvimento Stack' \
		'  make test' \
		'  make lint' \
		'  make verify-no-legacy' \
		'  make verify-security' \
		'  make docs-check' \
		'  make docs-mermaid-check' \
		'  make clean'

setup: bootstrap
	./scripts/install-user.sh

setup-system:
	$(PYTHON) scripts/new_user_bootstrap.py --install-system --write-report

bootstrap:
	$(PYTHON) scripts/new_user_bootstrap.py --write-report

postgres-tls:
	$(PYTHON) -m postgres.postgres_core.cli generate-tls --output-dir "$(CURDIR)/.local/postgres-tls"

models:
	$(INFRA) config
	$(PYTHON) scripts/models_prepare.py --pull-ollama --download-gguf --write-report

build:
	$(INFRA) prepare
	$(PYTHON) scripts/docker/preprovision_owner_tls.py

infra: build

infra-config:
	$(INFRA) config

infra-build:
	$(INFRA) build

infra-validate:
	$(INFRA) validate

postgres-setup:
	$(PYTHON) scripts/postgres_setup.py setup

converge: postgres-setup
	$(INFRA) converge $(if $(NO_SNAPSHOT),--no-snapshot,)

up:
	$(INFRA) run $(if $(NO_SNAPSHOT),--no-snapshot,)

profiles:
	$(PYTHON) scripts/select_profiles.py --json

up-auto:
	@profiles="$$( $(PYTHON) scripts/select_profiles.py )" || exit $$?; \
	[ -n "$$profiles" ] || { printf '%s\n' 'Config Center returned an empty runtime profile selection.' >&2; exit 2; }; \
	printf '%s\n' "AI_COMPOSE_PROFILES=$$profiles"; \
	AI_COMPOSE_PROFILES="$$profiles" $(MAKE) build && \
	AI_COMPOSE_PROFILES="$$profiles" $(MAKE) converge && \
	AI_COMPOSE_PROFILES="$$profiles" $(MAKE) aliases && \
	AI_COMPOSE_PROFILES="$$profiles" $(MAKE) up

verify:
	$(PYTHON) scripts/verify_install.py --mode user --write-report

verify-live:
	$(PYTHON) scripts/verify_install.py --mode user --live --write-report

verify-live-lifecycle-prod:
	$(PYTHON) scripts/verify_install.py --mode user --live --lifecycle-prod-only --write-report .local/generated/verify.lifecycle-prod.report.json

use: postgres-setup build converge aliases up verify-live
	@printf '%s\n' 'Pronto. Modelos opcionais: make models. Usa: @ o que consegues fazer neste sistema?'

aliases:
	$(PYTHON) scripts/install_symbiont_alias.py

telemetry-authority:
	$(PYTHON) scripts/docker/host_services.py foreground telemetry-authority

mic-stream-test:
	$(PYTHON) scripts/mic_stream_test.py $(ARGS)

doctor:
	$(PYTHON) scripts/local_doctor.py --section all

check-gpu:
	$(PYTHON) scripts/local_doctor.py --section gpu

check-disk:
	$(PYTHON) scripts/local_doctor.py --section disk

ollama-host-config:
	$(PYTHON) -m config.resolver --write-ollama-host-config .local/generated/ollama-host

ollama-host-apply:
	$(PYTHON) -m config.resolver --write-ollama-host-config .local/generated/ollama-host
	sh .local/generated/ollama-host/apply-ollama-systemd.sh

rag:
	$(PYTHON) scripts/configure_rag_sources.py --write-report $(ARGS)

rag-clear:
	$(PYTHON) scripts/configure_rag_sources.py --clear --write-report

status:
	$(INFRA) status

logs:
	$(INFRA) logs $(if $(FOLLOW),--follow,) --tail $(TAIL)

down:
	$(INFRA) down

rollback:
	$(INFRA) rollback $(if $(SNAPSHOT),--snapshot $(SNAPSHOT),)

docker-disk-report:
	$(INFRA) disk-report

docker-safe-prune:
	$(INFRA) safe-prune $(if $(DOCKER_CACHE_MAX),--cache-max $(DOCKER_CACHE_MAX),) $(if $(DOCKER_MIN_FREE),--min-free $(DOCKER_MIN_FREE),)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
