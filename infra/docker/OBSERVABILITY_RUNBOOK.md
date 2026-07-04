# Observability Runbook

This runbook is owned by `infra/docker` because it verifies runtime wiring for
the local observability stack. Domain event meaning stays with each component
owner; this file only defines how operators confirm telemetry, dashboards and
degraded states are connected.

## Stack Contract

- OTEL collector receives OTLP on 4317 and 4318, exposes collector metrics on
  8888, and exports traces, metrics and logs to ClickHouse.
- ClickHouse stores OTEL data in the `ai_symbiont` database, including
  `otel_traces`, `otel_metrics` and `otel_logs`.
- Grafana provisions the ClickHouse datasource with UID `clickhouse` and loads
  dashboards from `/var/lib/grafana/dashboards`.
- Langfuse records LLM observability data through its own Postgres database and
  is part of the same `observability` profile.

## Correlation IDs

Telemetry that crosses gateway, dispatch, services, storage and RAG must carry
the canonical `ai.local.*` correlation ids. The canonical attribute names live
in `orchestrator/observability/semantic_attributes.py`; other owners reference
the same names through manifests, APIs, events or generated config surfaces
instead of importing orchestrator internals.

Required high-value fields:

- `ai.local.request_id`
- `ai.local.session_id`
- `ai.local.task_id`
- `ai.local.run_id`
- `ai.local.capability_id`
- `ai.local.resource_lease_id`
- `ai.local.owner`
- `ai.local.component`
- `ai.local.trace_kind`

When a request looks incomplete in Grafana, start from
`SpanAttributes['ai.local.request_id']`, then pivot to owner, component,
capability and task/run ids. Missing correlation ids are treated as an
observability defect even when the user-facing request completed.

## State Model

| State | Meaning | Operator action |
| --- | --- | --- |
| ready | The stack accepts OTLP, stores data and dashboards query current rows. | Continue monitoring. |
| degraded | One owner reports partial capability loss or one observability sink is unhealthy. | Preserve correlation ids, inspect owner logs and keep the degraded event visible. |
| blocked | A required sink, secret, healthcheck or contract is missing. | Stop rollout/startup until the missing dependency is restored. |
| stale | Dashboards or ClickHouse rows lag behind live requests. | Check collector queues, ClickHouse health and dashboard query windows. |

Every service capability manifest must publish `service.degraded` so the
gateway and dispatch layers can correlate degraded owner states with OTEL
traces.

## Triage

Run the static contract first:

```bash
make infra
```

If the static contract passes but the live stack is degraded, inspect the
profile:

```bash
docker compose --env-file .env.storage.generated --env-file .env.llm.generated --env-file .env.services.generated --env-file .env.docker.resources.generated --env-file infra/docker/.env.observability --profile observability ps
docker compose --profile observability logs --tail=200 otel-collector clickhouse grafana langfuse
```

### OTEL collector degraded

1. Confirm `otel-collector` is healthy and listening on 4317, 4318 and 8888.
2. Check `infra/docker/otel/otel-collector-config.yaml` for OTLP receivers,
   `memory_limiter`, `batch`, `resource` and ClickHouse exporters.
3. Search collector logs for export failures or schema creation errors.
4. If telemetry arrives but dashboards are empty, query ClickHouse before
   changing service code.

### ClickHouse degraded

1. Confirm the `clickhouse_password` secret exists and has mode `600`.
2. Check the container healthcheck and `/ping` endpoint.
3. Verify rows exist in `ai_symbiont.otel_traces`, `ai_symbiont.otel_metrics`
   or `ai_symbiont.otel_logs`.
4. Treat missing tables as blocked unless collector schema creation is actively
   recovering.

### Grafana degraded

1. Confirm the datasource file provisions UID `clickhouse`.
2. Confirm dashboard provisioning points at `/var/lib/grafana/dashboards`.
3. Open the OTEL migration dashboards and verify queries read
   `ai_symbiont.otel_traces` with `SpanAttributes['ai.local.*']` filters.
4. If Grafana is ready but panels are empty, inspect query time ranges and then
   ClickHouse rows.

### Langfuse degraded

1. Confirm `langfuse-db` is healthy before debugging the Langfuse UI.
2. Check `langfuse_db_password`, `langfuse_nextauth_secret` and
   `langfuse_salt` secrets.
3. Verify `NEXTAUTH_URL` matches the local bind URL generated for the
   observability profile.
4. Do not route Langfuse failures through orchestrator fallback logic; keep it
   as an observability sink problem.

### Correlation gap

1. Pick one live request and collect its `ai.local.request_id`.
2. Search Grafana/ClickHouse for the same request id across gateway, dispatch,
   service owner, storage and RAG spans.
3. If one owner is missing, check that owner's API/event boundary before
   changing infra.
4. If all spans are missing the same id, inspect gateway span creation and
   dispatch propagation.
