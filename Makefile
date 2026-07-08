# ai-local public user commands

.DEFAULT_GOAL := help

PYTHON ?= $(shell command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null || printf python3)
DOCKER_CONTEXT ?= default
AI_COMPOSE_PROFILES ?= core,storage
TAIL ?= 80
DOCKER_CACHE_MAX ?=
INFRA := DOCKER_CONTEXT=$(DOCKER_CONTEXT) AI_LOCAL_DOCKER_CONTEXT=$(DOCKER_CONTEXT) AI_COMPOSE_PROFILES="$(AI_COMPOSE_PROFILES)" $(PYTHON) infra/docker/scripts/infra_ops.py

.PHONY: help setup setup-system models infra infra-config infra-build infra-validate up up-auto profiles verify verify-live verify-live-lifecycle-prod use doctor bootstrap check-gpu check-disk ollama-host-config ollama-host-apply aliases build-symbiont-tui rag rag-clear status logs down rollback docker-disk-report docker-safe-prune clean

help:
	@printf '%s\n' ''
	@printf '%s\n' 'ai-local: sequencia principal'
	@printf '%s\n' '  1. make setup-system # instala prerequisitos Linux da tua distro'
	@printf '%s\n' '  2. make setup        # preflight + instala runtime + alias @'
	@printf '%s\n' '  3. make models       # opcional; prepara modelos locais quando aplicavel'
	@printf '%s\n' '  4. make infra        # gera config, builda imagens Docker e valida infra'
	@printf '%s\n' '  5. make up           # sobe base persistente, health, smoke e relatorio'
	@printf '%s\n' '  6. make verify-live  # confirma uso real pelo alias @'
	@printf '%s\n' ''
	@printf '%s\n' 'Operacao diaria'
	@printf '%s\n' '  make up              # idempotente; pode ser usado para reparar drift'
	@printf '%s\n' '  make infra-config    # so regenera secrets/env/config da infra'
	@printf '%s\n' '  make infra-build     # so builda o catalogo completo de imagens Docker'
	@printf '%s\n' '  make infra-validate  # so valida storage, Compose, policy e Docker host'
	@printf '%s\n' '  make profiles        # mostra perfis maximos recomendados para esta maquina'
	@printf '%s\n' '  make up-auto         # prepara e sobe o maximo seguro inferido pelo config center'
	@printf '%s\n' '  make status          # containers ai-local'
	@printf '%s\n' '  make logs FOLLOW=1   # logs da stack selecionada'
	@printf '%s\n' '  make docker-disk-report # diagnostico de uso Docker sem apagar nada'
	@printf '%s\n' '  make docker-safe-prune  # limpa cache/imagens paradas sem tocar volumes'
	@printf '%s\n' '  make down            # para a stack'
	@printf '%s\n' '  make rollback        # restaura ultimo snapshot de env/config gerado'
	@printf '%s\n' ''
	@printf '%s\n' 'Configuracao pessoal'
	@printf '%s\n' '  make rag ARGS="--vault-dir ~/Obsidian/Vault --repo-path ~/src"'
	@printf '%s\n' '  make rag-clear'
	@printf '%s\n' '  make ollama-host-config # gera drop-in systemd para Ollama nativo'
	@printf '%s\n' '  make ollama-host-apply  # aplica drop-in systemd com sudo explicito'
	@printf '%s\n' '  make check-gpu          # diagnostico NVIDIA/Docker/Ollama GPU'
	@printf '%s\n' '  make build-symbiont-tui # compila e instala o terminal Rust usado por symbiont'
	@printf '%s\n' ''
	@printf '%s\n' 'Limpeza local'
	@printf '%s\n' '  make clean'

setup: bootstrap
	./scripts/install.sh
	$(MAKE) aliases

setup-system:
	$(PYTHON) scripts/new_user_bootstrap.py --install-system --write-report

models:
	$(INFRA) config
	$(PYTHON) scripts/models_prepare.py --pull-ollama --download-gguf --write-report

infra: infra-config infra-build infra-validate
	@printf '%s\n' '' 'Infra prepared. Next: make up'

infra-config:
	$(INFRA) config

infra-build:
	$(INFRA) build

infra-validate:
	$(INFRA) validate

up:
	$(INFRA) run $(if $(NO_SNAPSHOT),--no-snapshot,)

profiles:
	$(PYTHON) scripts/select_profiles.py --json

up-auto:
	@profiles="$$( $(PYTHON) scripts/select_profiles.py )"; printf '%s\n' "AI_COMPOSE_PROFILES=$$profiles"; AI_COMPOSE_PROFILES="$$profiles" $(MAKE) infra && AI_COMPOSE_PROFILES="$$profiles" $(MAKE) up

verify:
	$(PYTHON) scripts/verify_install.py --mode user --write-report

verify-live:
	$(PYTHON) scripts/verify_install.py --mode user --live --write-report

verify-live-lifecycle-prod:
	$(PYTHON) scripts/verify_install.py --mode user --live --lifecycle-prod-only --write-report .local/generated/verify.lifecycle-prod.report.json

use: infra up verify-live
	@printf '%s\n' 'Pronto. Usa: @ o que consegues fazer neste sistema?'

doctor:
	DOCKER_CONTEXT=$(DOCKER_CONTEXT) AI_LOCAL_DOCKER_CONTEXT=$(DOCKER_CONTEXT) $(PYTHON) scripts/local_doctor.py --section all

bootstrap:
	$(PYTHON) scripts/new_user_bootstrap.py --write-report

check-gpu:
	DOCKER_CONTEXT=$(DOCKER_CONTEXT) AI_LOCAL_DOCKER_CONTEXT=$(DOCKER_CONTEXT) $(PYTHON) scripts/local_doctor.py --section gpu

check-disk:
	DOCKER_CONTEXT=$(DOCKER_CONTEXT) AI_LOCAL_DOCKER_CONTEXT=$(DOCKER_CONTEXT) $(PYTHON) scripts/local_doctor.py --section disk

ollama-host-config:
	$(PYTHON) -m config.resolver --write-ollama-host-config .local/generated/ollama-host

ollama-host-apply:
	$(PYTHON) -m config.resolver --write-ollama-host-config .local/generated/ollama-host
	sh .local/generated/ollama-host/apply-ollama-systemd.sh

aliases:
	@if [ -x .venv/bin/python ]; then .venv/bin/python -c "from orchestrator.cli.aliases import install_aliases; installed = install_aliases(); print('Alias pronto: ' + ', '.join(installed) if installed else 'Nenhum alias para instalar')"; else PYTHONPATH=. $(PYTHON) -c "from orchestrator.cli.aliases import install_aliases; installed = install_aliases(); print('Alias pronto: ' + ', '.join(installed) if installed else 'Nenhum alias para instalar')"; fi

build-symbiont-tui:
	cargo build --release --manifest-path crates/symbiont-tui/Cargo.toml
	$(MAKE) aliases

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
	$(INFRA) safe-prune $(if $(DOCKER_CACHE_MAX),--cache-max $(DOCKER_CACHE_MAX),)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
