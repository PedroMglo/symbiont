#!/usr/bin/env python3
"""Simulate multiple Gemini execution runs with multiple containers and interactions.

Generates realistic observability events that populate the Gemilyni dashboard:
- 8 independent runs with varying complexity
- Multiple workers per run (1-4 containers)
- Container resource stats over time
- Policy violations, errors, and fallbacks
- Full lifecycle: routing → context → bundle → container → gemini → output → finish

Usage:
    python scripts/runtime/simulate_gemilyni_multi_container.py

Then start the API:
    python -m orchestrator.gateway.app

Open https://localhost:8321/dashboard → tab "Gemilyni"
"""

from __future__ import annotations

import random
import time
import uuid

# Initialize gemilyni observability with test config
from orchestrator.observability.config import GemilyniConfig
from orchestrator.observability.gemilyni import (
    emit_bundle_created,
    emit_bundle_file,
    emit_container_created,
    emit_container_started,
    emit_container_stats,
    emit_context_block,
    emit_error,
    emit_execution_finished,
    emit_external_context_policy,
    emit_gemini_invocation_finished,
    emit_gemini_invocation_started,
    emit_policy_violation,
    emit_routing_decision,
    emit_worker_output,
    init_gemilyni,
)

# Enable with all features
config = GemilyniConfig(
    enabled=True,
    capture_container_stats=True,
    capture_bundle_metadata=True,
    capture_context_metadata=True,
    capture_file_metadata=True,
    capture_policy_events=True,
    capture_worker_outputs_metadata=True,
    capture_token_estimates=True,
    capture_raw_context=False,
    capture_raw_files=False,
    capture_prompt_preview=False,
    redact_sensitive_values=True,
    max_preview_chars=500,
)
init_gemilyni(config)


# ═══════════════════════════════════════════════════════════════════════════
# Scenario definitions
# ═══════════════════════════════════════════════════════════════════════════

SCENARIOS = [
    {
        "name": "Refactoring: Extract authentication module",
        "complexity": "COMPLEX",
        "intent": "CODE",
        "workers": 3,
        "files": ["src/auth/login.py", "src/auth/oauth.py", "src/auth/session.py", "src/auth/__init__.py", "tests/test_auth.py"],
        "blocked_files": [".env", ".env.production"],
        "context_sources": ["code", "repo", "git_history"],
        "blocked_sources": ["email"],
        "status": "success",
        "has_error": False,
        "has_violation": True,
    },
    {
        "name": "Feature: Implement real-time notifications",
        "complexity": "COMPLEX",
        "intent": "CODE",
        "workers": 4,
        "files": ["src/notifications/ws_handler.py", "src/notifications/broker.py", "src/notifications/models.py", "src/notifications/config.py", "src/api/routes/notify.py", "tests/test_notifications.py"],
        "blocked_files": ["secrets.yaml"],
        "context_sources": ["code", "repo", "docs"],
        "blocked_sources": ["calendar", "rss"],
        "status": "success",
        "has_error": False,
        "has_violation": True,
    },
    {
        "name": "Bug fix: Memory leak in worker pool",
        "complexity": "MODERATE",
        "intent": "DEBUG",
        "workers": 2,
        "files": ["src/pool/manager.py", "src/pool/worker.py", "tests/test_pool.py"],
        "blocked_files": [],
        "context_sources": ["code", "logs"],
        "blocked_sources": [],
        "status": "success",
        "has_error": False,
        "has_violation": False,
    },
    {
        "name": "Feature: GraphQL API layer",
        "complexity": "COMPLEX",
        "intent": "CODE",
        "workers": 3,
        "files": ["src/graphql/schema.py", "src/graphql/resolvers.py", "src/graphql/types.py", "src/graphql/mutations.py", "tests/test_graphql.py"],
        "blocked_files": ["deploy_keys.json", ".gcloud/credentials.json"],
        "context_sources": ["code", "repo", "docs"],
        "blocked_sources": ["email"],
        "status": "success",
        "has_error": False,
        "has_violation": True,
    },
    {
        "name": "Perf: Optimize database queries",
        "complexity": "MODERATE",
        "intent": "OPTIMIZE",
        "workers": 2,
        "files": ["src/db/queries.py", "src/db/indexes.py", "src/db/connection_pool.py"],
        "blocked_files": [".pgpass"],
        "context_sources": ["code", "metrics"],
        "blocked_sources": [],
        "status": "success",
        "has_error": False,
        "has_violation": True,
    },
    {
        "name": "Feature: Multi-tenant data isolation",
        "complexity": "COMPLEX",
        "intent": "CODE",
        "workers": 4,
        "files": ["src/tenant/isolator.py", "src/tenant/router.py", "src/tenant/middleware.py", "src/tenant/models.py", "src/db/migrations/0042_tenant.py", "tests/test_tenant.py"],
        "blocked_files": ["terraform.tfvars", ".env.staging"],
        "context_sources": ["code", "repo", "architecture_docs"],
        "blocked_sources": ["email", "calendar"],
        "status": "partial_failure",
        "has_error": True,
        "has_violation": True,
    },
    {
        "name": "Fix: Race condition in event bus",
        "complexity": "MODERATE",
        "intent": "DEBUG",
        "workers": 2,
        "files": ["src/events/bus.py", "src/events/consumer.py", "tests/test_events.py"],
        "blocked_files": [],
        "context_sources": ["code", "logs", "traces"],
        "blocked_sources": [],
        "status": "success",
        "has_error": False,
        "has_violation": False,
    },
    {
        "name": "Feature: PDF report generation pipeline",
        "complexity": "COMPLEX",
        "intent": "CODE",
        "workers": 3,
        "files": ["src/reports/generator.py", "src/reports/templates.py", "src/reports/renderer.py", "src/reports/charts.py", "tests/test_reports.py"],
        "blocked_files": ["api_keys.yml"],
        "context_sources": ["code", "repo"],
        "blocked_sources": ["rss"],
        "status": "failed",
        "has_error": True,
        "has_violation": False,
    },
]


def generate_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def generate_trace_id() -> str:
    return f"trace-{uuid.uuid4().hex[:16]}"


def generate_container_id() -> str:
    return f"ctr-{uuid.uuid4().hex[:12]}"


def simulate_run(scenario: dict) -> None:
    """Simulate a full execution run with multiple workers."""
    run_id = generate_run_id()
    trace_id = generate_trace_id()
    n_workers = scenario["workers"]

    print(f"  [{run_id[:16]}] {scenario['name']} ({n_workers} workers)")

    # 1. Routing decision
    emit_routing_decision(
        run_id=run_id,
        trace_id=trace_id,
        selected_path="execute",
        reason="complexity_above_threshold",
        complexity=scenario["complexity"],
        complexity_threshold="MODERATE",
        intent=scenario["intent"],
        blocked_by_policy=False,
        externalizable=True,
        execution_enabled=True,
    )
    time.sleep(0.01)

    # 2. Context policy
    n_allowed_sources = len(scenario["context_sources"])
    n_blocked_sources = len(scenario["blocked_sources"])
    total_blocks = n_allowed_sources * 3 + n_blocked_sources * 2

    emit_external_context_policy(
        run_id=run_id,
        trace_id=trace_id,
        original_blocks=total_blocks,
        allowed_blocks=n_allowed_sources * 3,
        blocked_blocks=n_blocked_sources * 2,
        allowed_sources=scenario["context_sources"],
        blocked_sources=scenario["blocked_sources"],
        policy="allowlist",
        require_sanitized_context=True,
    )
    time.sleep(0.01)

    # 3-7. Per-worker lifecycle
    worker_durations = []
    containers_started = 0
    containers_failed = 0
    workers_succeeded = 0
    workers_failed = 0
    total_start = time.time()

    for w_idx in range(n_workers):
        worker_id = f"w{w_idx + 1}"
        bundle_id = f"{run_id}:{worker_id}"
        container_id = generate_container_id()

        # Assign files to workers (split across them)
        worker_files = scenario["files"][w_idx::n_workers]
        worker_blocked = scenario["blocked_files"] if w_idx == 0 else []

        # 3. Bundle created
        emit_bundle_created(
            run_id=run_id,
            trace_id=trace_id,
            bundle_id=bundle_id,
            worker_id=worker_id,
            task_type=f"subtask_{w_idx + 1}",
            allowed_files_count=len(worker_files),
            blocked_files_count=len(worker_blocked),
            allowed_context_blocks=random.randint(2, 6),
            blocked_context_blocks=len(scenario["blocked_sources"]),
            workspace_mode="partial_copy",
            repo_mounted_directly=False,
            workspace_readonly=True,
            bundle_root=f"/tmp/gemilyni/bundles/{run_id}/{worker_id}",
        )
        time.sleep(0.005)

        # 3a. Individual file events
        for fp in worker_files:
            emit_bundle_file(
                run_id=run_id,
                trace_id=trace_id,
                bundle_id=bundle_id,
                worker_id=worker_id,
                relative_path=fp,
                file_hash=uuid.uuid4().hex[:16],
                file_size_bytes=random.randint(500, 15000),
                included=True,
            )

        for fp in worker_blocked:
            emit_bundle_file(
                run_id=run_id,
                trace_id=trace_id,
                bundle_id=bundle_id,
                worker_id=worker_id,
                relative_path=fp,
                file_hash=uuid.uuid4().hex[:16],
                file_size_bytes=random.randint(100, 2000),
                included=False,
                blocked=True,
                block_reason="blocked_pattern",
            )
            # Policy violation for blocked file
            if scenario["has_violation"]:
                emit_policy_violation(
                    run_id=run_id,
                    trace_id=trace_id,
                    policy_name="bundle_file_filter",
                    violation_type="file_blocked",
                    blocked_item_type="file",
                    blocked_item_ref=fp,
                    reason="blocked_pattern",
                    severity="warning",
                )

        # 3b. Context blocks
        for src in scenario["context_sources"]:
            for i in range(random.randint(1, 3)):
                emit_context_block(
                    run_id=run_id,
                    trace_id=trace_id,
                    bundle_id=bundle_id,
                    worker_id=worker_id,
                    source=src,
                    source_type="file" if src == "code" else "api",
                    block_hash=uuid.uuid4().hex[:16],
                    token_estimate=random.randint(100, 2000),
                    size_bytes=random.randint(400, 8000),
                    included=True,
                )

        for src in scenario["blocked_sources"]:
            emit_context_block(
                run_id=run_id,
                trace_id=trace_id,
                bundle_id=bundle_id,
                worker_id=worker_id,
                source=src,
                source_type="api",
                block_hash=uuid.uuid4().hex[:16],
                token_estimate=random.randint(200, 1500),
                size_bytes=random.randint(500, 5000),
                included=False,
                blocked=True,
                block_reason="source_not_in_allowlist",
            )

        time.sleep(0.005)

        # 4. Container lifecycle
        emit_container_created(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=worker_id,
            container_id=container_id,
            image="orc-execution-worker:latest",
            auth_mode="oauth",
            mounts_count=random.randint(2, 5),
            network_mode="none",
        )
        emit_container_started(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=worker_id,
            container_id=container_id,
        )
        containers_started += 1
        time.sleep(0.005)

        # 4a. Container stats (simulate resource usage over time)
        for stat_i in range(random.randint(2, 5)):
            emit_container_stats(
                run_id=run_id,
                trace_id=trace_id,
                worker_id=worker_id,
                container_id=container_id,
                cpu_percent=random.uniform(15, 85),
                memory_usage_bytes=random.randint(50_000_000, 500_000_000),
                memory_limit_bytes=1_073_741_824,  # 1GB
                memory_percent=random.uniform(5, 50),
                network_rx_bytes=random.randint(1000, 100_000),
                network_tx_bytes=random.randint(500, 50_000),
                block_read_bytes=random.randint(10_000, 500_000),
                block_write_bytes=random.randint(5_000, 200_000),
            )
            time.sleep(0.003)

        # 5. Gemini invocation
        input_tokens = random.randint(800, 4000)
        emit_gemini_invocation_started(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=worker_id,
            container_id=container_id,
            auth_mode="oauth",
            command_mode="interactive",
            model="gemini-2.5-flash",
            input_tokens_estimate=input_tokens,
        )
        time.sleep(0.01)

        # Determine worker outcome
        worker_success = True
        if scenario["status"] == "failed" and w_idx == n_workers - 1:
            worker_success = False
        elif scenario["status"] == "partial_failure" and w_idx == n_workers - 1:
            worker_success = False

        gemini_duration = random.uniform(2000, 12000)
        output_tokens = random.randint(300, 2500)

        if worker_success:
            emit_gemini_invocation_finished(
                run_id=run_id,
                trace_id=trace_id,
                worker_id=worker_id,
                container_id=container_id,
                status="success",
                exit_code=0,
                duration_ms=gemini_duration,
                output_tokens_estimate=output_tokens,
                stdout_size_bytes=random.randint(1000, 10_000),
            )
            workers_succeeded += 1
        else:
            emit_gemini_invocation_finished(
                run_id=run_id,
                trace_id=trace_id,
                worker_id=worker_id,
                container_id=container_id,
                status="error",
                exit_code=1,
                duration_ms=gemini_duration,
                output_tokens_estimate=0,
                stderr_size_bytes=random.randint(200, 2000),
                error_type="GeminiTimeoutError",
            )
            workers_failed += 1
            containers_failed += 1

            if scenario["has_error"]:
                emit_error(
                    run_id=run_id,
                    trace_id=trace_id,
                    worker_id=worker_id,
                    container_id=container_id,
                    phase="gemini_invocation",
                    error_type="GeminiTimeoutError",
                    error_message=f"Gemini API timed out after {gemini_duration:.0f}ms for worker {worker_id}. Token: Bearer eyJhbGciOiJSUzI1NiJ9.expired",
                    recoverable=True,
                    fallback_used=scenario["status"] == "partial_failure",
                )

        worker_durations.append(gemini_duration)
        time.sleep(0.005)

        # 6. Worker output (if successful)
        if worker_success:
            emit_worker_output(
                run_id=run_id,
                trace_id=trace_id,
                worker_id=worker_id,
                container_id=container_id,
                result_json_exists=True,
                patch_diff_exists=True,
                patch_size_bytes=random.randint(500, 8000),
                result_size_bytes=random.randint(200, 3000),
                logs_size_bytes=random.randint(100, 5000),
                output_files=["result.json", "changes.patch", "execution.log"],
            )

    total_duration = (time.time() - total_start) * 1000 + sum(worker_durations)

    # 7. Execution finished
    final_status = "success"
    if scenario["status"] == "failed":
        final_status = "failed"
    elif scenario["status"] == "partial_failure":
        final_status = "partial_success"

    emit_execution_finished(
        run_id=run_id,
        trace_id=trace_id,
        external_used=True,
        workers_total=n_workers,
        workers_succeeded=workers_succeeded,
        workers_failed=workers_failed,
        containers_started=containers_started,
        containers_failed=containers_failed,
        total_duration_ms=total_duration,
        planning_duration_ms=random.uniform(200, 800),
        bundle_duration_ms=random.uniform(100, 500),
        container_start_duration_ms=random.uniform(500, 2000),
        gemini_duration_ms=sum(worker_durations),
        synthesis_duration_ms=random.uniform(300, 1500),
        fallback_used=scenario["status"] == "partial_failure",
        final_status=final_status,
    )


def simulate_local_only_runs(count: int = 3) -> None:
    """Simulate runs that stayed local (not externalized)."""
    for i in range(count):
        run_id = generate_run_id()
        trace_id = generate_trace_id()

        print(f"  [{run_id[:16]}] Local-only run #{i + 1} (routed to local LLM)")

        emit_routing_decision(
            run_id=run_id,
            trace_id=trace_id,
            selected_path="local",
            reason="complexity_below_threshold",
            complexity="SIMPLE",
            complexity_threshold="MODERATE",
            intent=random.choice(["QUESTION", "EXPLAIN", "SUMMARIZE"]),
            blocked_by_policy=False,
            externalizable=False,
            execution_enabled=True,
        )
        time.sleep(0.01)


def main():
    print("\n" + "=" * 70)
    print("  GEMILYNI MULTI-CONTAINER SIMULATION")
    print("  Generating realistic execution events for dashboard population")
    print("=" * 70 + "\n")

    print("[1/3] Simulating external execution runs (multiple containers)...\n")
    for scenario in SCENARIOS:
        simulate_run(scenario)
        time.sleep(0.02)

    print("\n[2/3] Simulating local-only routing decisions...\n")
    simulate_local_only_runs(5)

    print("\n[3/3] Generating additional policy violations...\n")
    # Extra policy violations to enrich the dashboard
    for _ in range(3):
        run_id = generate_run_id()
        trace_id = generate_trace_id()
        emit_routing_decision(
            run_id=run_id,
            trace_id=trace_id,
            selected_path="blocked",
            reason="policy_denied_execution",
            complexity="COMPLEX",
            complexity_threshold="MODERATE",
            intent="CODE",
            blocked_by_policy=True,
            externalizable=True,
            execution_enabled=False,
        )
        emit_policy_violation(
            run_id=run_id,
            trace_id=trace_id,
            policy_name="execution_policy",
            violation_type="execution_blocked",
            blocked_item_type="run",
            blocked_item_ref=f"Blocked: sensitive project (run {run_id[:8]})",
            reason="execution_disabled_by_admin",
            severity="error",
        )
        print(f"  [{run_id[:16]}] Policy-blocked execution")

    # Summary
    from orchestrator.core.observability.gemilyni import query_gemilyni_summary
    summary = query_gemilyni_summary(hours=1)

    print("\n" + "=" * 70)
    print("  SIMULATION COMPLETE — Dashboard Summary")
    print("=" * 70)
    print(f"""
  Total Runs:          {summary['total_runs']}
  External Runs:       {summary['external_runs']}
  Local Runs:          {summary['local_runs']}
  Success Rate:        {summary['success_rate'] * 100:.1f}%
  Failure Rate:        {summary['failure_rate'] * 100:.1f}%
  Containers Created:  {summary['containers_created']}
  Workers Executed:    {summary['workers_executed']}
  Avg Duration:        {summary['avg_duration_ms']:.0f}ms
  Avg Gemini Time:     {summary['avg_gemini_ms']:.0f}ms
  Policy Violations:   {summary['violations']}
  Files Blocked:       {summary['files_blocked']}
  Fallbacks Used:      {summary['fallbacks']}
""")
    print("  Dashboard data is now in memory.")
    print("  Start API with: python -m symbiont.api.app")
    print("  Then open: https://localhost:8321/dashboard → tab 'Gemilyni'")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
