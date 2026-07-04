"""CLI — orc command entry point.

Management commands for the symbiont. All LLM interactions go through
the HTTP API managed by Docker (``make infra``). Model aliases in ~/.local/bin/
call the API directly via curl — no Python process per query.
"""

from __future__ import annotations

import os

import click

from orchestrator.config import get_settings


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context):
    """AI Symbiont — intelligent query routing."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
def health():
    """Check health of all components including LLM backends."""
    from orchestrator.factory import create_engine

    engine = create_engine()
    cfg = get_settings()
    report = engine.health_report()

    click.echo(f"Symbiont: {cfg.symbiont.host}:{cfg.symbiont.port}")

    # Per-backend status (v0.7)
    backends = report.get("backends", [])
    if backends:
        click.echo("\nLLM Backends:")
        for b in backends:
            status = b["status"]
            colour = "green" if status == "healthy" else ("yellow" if status == "disabled" else "red")
            icon = "✓" if status == "healthy" else ("·" if status == "disabled" else "✗")
            lat = f" {b['latency_ms']:.0f}ms" if b.get("latency_ms") is not None else ""
            n_detected = len(b.get("models_detected", []))
            models_info = f" ({n_detected} models)" if n_detected else ""
            click.echo(
                f"  {click.style(icon, fg=colour)} {b['name']:<14s} "
                f"{click.style(status, fg=colour)}{lat}{models_info}"
            )
            if b.get("last_error"):
                click.echo(f"    ↳ error: {b['last_error']}")
    else:
        click.echo(f"LLM:   {'✓' if report['ollama'] else '✗'}")

    click.echo("\nContext Providers:")
    for name, ok in report["providers"].items():
        click.echo(f"  {'✓' if ok else '✗'} {name}")


@main.command()
def hardware():
    """Show detected hardware profile and adaptive optimization parameters."""
    from orchestrator.core.adaptive_config import get_adaptive_overrides
    from orchestrator.core.hardware_profile import get_hardware_profile

    cfg = get_settings()
    profile = get_hardware_profile(ollama_url=cfg.ollama.base_url)
    overrides = get_adaptive_overrides(profile)

    click.echo(click.style("── Hardware Profile ──", bold=True))

    # CPU
    click.echo(f"\n  CPU:    {profile.cpu.model_name or profile.cpu.architecture}")
    click.echo(f"          {profile.cpu.physical_cores} physical / {profile.cpu.logical_cores} logical cores")

    # RAM
    ram_color = "green" if profile.ram.percent_used < 70 else ("yellow" if profile.ram.percent_used < 85 else "red")
    click.echo(f"\n  RAM:    {profile.ram.total_mb}MB total, {profile.ram.available_mb}MB available")
    click.echo(f"          {click.style(f'{profile.ram.percent_used:.0f}% used', fg=ram_color)}")
    if profile.ram.swap_used_mb > 0:
        click.echo(click.style(f"          ⚠ Swap active: {profile.ram.swap_used_mb}MB/{profile.ram.swap_total_mb}MB", fg="yellow"))

    # GPU
    if profile.has_gpu:
        vram_pct = (profile.gpu.vram_used_mb / profile.gpu.vram_total_mb * 100) if profile.gpu.vram_total_mb > 0 else 0
        vram_color = "green" if vram_pct < 70 else ("yellow" if vram_pct < 90 else "red")
        click.echo(f"\n  GPU:    {profile.gpu.name} ({profile.gpu.gpu_count}x)")
        click.echo(f"          VRAM: {profile.gpu.vram_used_mb}MB/{profile.gpu.vram_total_mb}MB "
                   f"({click.style(f'{vram_pct:.0f}% used', fg=vram_color)})")
        click.echo(f"          Free: {profile.gpu.vram_free_mb}MB")
    else:
        click.echo(click.style("\n  GPU:    None detected (CPU-only mode)", fg="yellow"))

    # Disk
    click.echo(f"\n  Disk:   {profile.disk.disk_type.value.upper()} — "
               f"{profile.disk.free_gb:.0f}GB free / {profile.disk.total_gb:.0f}GB total")

    # Ollama
    if profile.ollama.available:
        click.echo("\n  Ollama: ✓ available")
        if profile.ollama.loaded_models:
            click.echo(f"          Loaded: {', '.join(profile.ollama.loaded_models)} "
                       f"({profile.ollama.loaded_vram_mb}MB VRAM)")
    else:
        click.echo(click.style("\n  Ollama: ✗ not reachable", fg="red"))

    # Adaptive config
    click.echo(click.style("\n── Adaptive Configuration ──", bold=True))
    click.echo(f"  Max loaded models:    {overrides.max_loaded_models}")
    click.echo(f"  Max concurrent LLM:   {overrides.max_concurrent_llm}")
    click.echo(f"  Preferred num_ctx:    {overrides.preferred_num_ctx}")
    click.echo(f"  Keep alive:           {overrides.keep_alive}")
    click.echo(f"  Context workers:      {overrides.context_worker_threads}")
    click.echo(f"  Response cache size:  {overrides.response_cache_max_size}")
    click.echo(f"  Context budget:       {overrides.context_token_budget} tokens (×{overrides.context_budget_multiplier})")
    click.echo(f"  Degradation mode:     {overrides.degradation_mode.value}")

    # Cache stats
    try:
        from orchestrator.core.response_cache import get_response_cache
        cache = get_response_cache()
        stats = cache.stats
        click.echo(f"\n  Response cache:       {stats['size']}/{stats['max_size']} entries, "
                   f"hit rate={stats['hit_rate']:.1%}")
    except Exception:
        pass

    # Recommendations
    if overrides.recommendations:
        click.echo(click.style("\n── Recommendations ──", bold=True))
        for rec in overrides.recommendations:
            click.echo(f"  → {rec}")


@main.command()
def doctor():
    """Comprehensive diagnostic — config, backends, providers, models, paths."""
    import sys
    from pathlib import Path

    issues: list[str] = []

    # --- Config validation ---
    click.echo(click.style("── Config ──", bold=True))
    try:
        cfg = get_settings()
        click.echo("  ✓ config/orc/ parsed successfully")
        click.echo(f"    server:   {cfg.symbiont.host}:{cfg.symbiont.port}")
        click.echo(f"    logging:  level={cfg.logging.level}  format={cfg.logging.format}")
        click.echo(f"    agentic:  enabled={cfg.agentic.enabled}  max_iter={cfg.agentic.max_iterations}")
    except Exception as exc:
        click.echo(click.style(f"  ✗ Config error: {exc}", fg="red"))
        issues.append(f"Config: {exc}")
        raise SystemExit(1)

    # --- Python environment ---
    click.echo(click.style("\n── Environment ──", bold=True))
    click.echo(f"  Python:       {sys.version.split()[0]}")
    click.echo(f"  Platform:     {sys.platform}")

    from orchestrator import __version__
    click.echo(f"  Symbiont: v{__version__}")

    # Key dependencies
    for pkg in ("fastapi", "httpx", "uvicorn"):
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "?")
            click.echo(f"    {pkg:<12s} {ver}")
        except ImportError:
            click.echo(click.style(f"  ✗ {pkg} NOT INSTALLED", fg="red"))
            issues.append(f"Missing package: {pkg}")

    # --- Paths ---
    click.echo(click.style("\n── Paths ──", bold=True))
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    click.echo(f"  .env:         {'✓ exists' if env_path.exists() else '✗ not found'}")

    user_bin = Path.home() / ".local" / "bin"
    on_path = str(user_bin) in os.environ.get("PATH", "")
    click.echo(f"  ~/.local/bin: {'✓ on PATH' if on_path else '⚠ NOT on PATH'}")
    if not on_path:
        issues.append("~/.local/bin not on PATH")

    # Count installed aliases
    alias_count = 0
    if user_bin.exists():
        from orchestrator.cli.aliases import _ALIAS_MARKER
        for p in user_bin.iterdir():
            if p.is_file():
                try:
                    if _ALIAS_MARKER in p.read_text():
                        alias_count += 1
                except (OSError, UnicodeDecodeError):
                    pass
    click.echo(f"  Aliases:      {alias_count} installed")

    # --- LLM Backends ---
    click.echo(click.style("\n── LLM Backends ──", bold=True))
    try:
        from orchestrator.llm.router import LLMRouter
        router = LLMRouter(cfg.llm)
        report = router.health_report()
        for b in report:
            status = b["status"]
            colour = "green" if status == "healthy" else ("yellow" if status == "disabled" else "red")
            icon = "✓" if status == "healthy" else ("·" if status == "disabled" else "✗")
            lat = f" ({b['latency_ms']:.0f}ms)" if b.get("latency_ms") is not None else ""
            n_models = len(b.get("models_detected", []))
            click.echo(
                f"  {click.style(icon, fg=colour)} {b['name']:<14s} "
                f"{click.style(status, fg=colour)}{lat}  {n_models} models"
            )
            if status not in ("healthy", "disabled"):
                issues.append(f"Backend {b['name']}: {status}")
    except Exception as exc:
        click.echo(click.style(f"  ✗ Router error: {exc}", fg="red"))
        issues.append(f"LLM Router: {exc}")

    # --- Context Providers ---
    click.echo(click.style("\n── Context Providers ──", bold=True))
    try:
        from orchestrator.factory import create_engine
        engine = create_engine()
        for name, ok in engine.health_report()["providers"].items():
            icon = click.style("✓", fg="green") if ok else click.style("✗", fg="red")
            click.echo(f"  {icon} {name}")
            if not ok:
                issues.append(f"Provider {name}: unhealthy")
    except Exception as exc:
        click.echo(click.style(f"  ✗ Engine error: {exc}", fg="red"))
        issues.append(f"Engine: {exc}")

    # --- Summary ---
    click.echo(click.style("\n── Summary ──", bold=True))
    if issues:
        click.echo(click.style(f"  {len(issues)} issue(s) found:", fg="yellow"))
        for issue in issues:
            click.echo(f"    ⚠ {issue}")
    else:
        click.echo(click.style("  ✓ All checks passed — system healthy", fg="green"))


@main.command()
def config():
    """Show current configuration."""
    cfg = get_settings()
    click.echo("Models:")
    click.echo(f"  default:   {cfg.models.default}")
    click.echo(f"  fast:      {cfg.models.fast}")
    click.echo(f"  code:      {cfg.models.code}")
    click.echo(f"  deep:      {cfg.models.deep}")
    click.echo(f"  embedding: {cfg.models.embedding}")
    click.echo("\nServices:")
    click.echo(f"  RAG:    {cfg.rag.url}")
    click.echo(f"  API:    {cfg.symbiont.host}:{cfg.symbiont.port}")
    click.echo("\nContext:")
    click.echo(f"  token_budget: {cfg.context.token_budget}")
    click.echo(f"  cag_db:       {cfg.context.cag.db_path or '(not set)'}")
    if cfg.repos.paths:
        click.echo(f"\nRepos ({len(cfg.repos.paths)}):")
        for p in cfg.repos.paths:
            click.echo(f"  - {p}")

    # v0.7 — LLM backends and profiles
    llm = cfg.llm
    click.echo("\nLLM (v0.7):")
    click.echo(f"  default_model:    {llm.default_model}")
    click.echo(f"  routing_strategy: {llm.routing_strategy}")
    click.echo(f"  fallback_enabled: {llm.fallback_enabled}")
    if llm.backends:
        click.echo(f"\n  Backends ({len(llm.backends)}):")
        for b in sorted(llm.backends, key=lambda x: x.priority):
            state = "enabled" if b.enabled else "disabled"
            click.echo(f"    [{b.priority:02d}] {b.name:<14s} {b.base_url}  ({state})")
    if llm.model_profiles:
        click.echo(f"\n  Profiles ({len(llm.model_profiles)}):")
        for p in llm.model_profiles:
            state = "" if p.enabled else "  [disabled]"
            click.echo(f"    {p.alias:<20s} → {', '.join(p.preferred_models[:2])} | fallback: {p.fallback_model}{state}")


@main.command()
def backends():
    """Show all configured LLM backends with live health status (v0.7)."""
    from orchestrator.config import get_settings
    from orchestrator.llm.router import LLMRouter

    cfg = get_settings()
    router = LLMRouter(cfg.llm)
    report = router.health_report()

    if not report:
        click.echo("No backends configured.")
        return

    click.echo(f"{'Pri':<4} {'Name':<14} {'Status':<12} {'Models':>6}  {'Latency':>8}  URL")
    click.echo("─" * 78)
    for b in report:
        status = b["status"]
        colour = "green" if status == "healthy" else ("yellow" if status == "disabled" else "red")
        n = len(b.get("models_detected", []))
        models_str = f"{n:>6}" if n else "  n/a "
        lat = f"{b['latency_ms']:.0f}ms" if b.get("latency_ms") is not None else "    —"
        click.echo(
            f"  {b['priority']:<2}  {b['name']:<14} "
            f"{click.style(f'{status:<12}', fg=colour)} "
            f"{models_str}  {lat:>8}  {b['url']}"
        )
        if b.get("last_error"):
            click.echo(f"        ↳ {b['last_error']}")


@main.command(name="backend")
@click.argument("action", type=click.Choice(["test"]))
@click.argument("name")
def backend_cmd(action: str, name: str):
    """Interact with a specific backend.

    \b
    orc backend test <name>   Run a live health probe and show detected models.
    """
    from orchestrator.config import get_settings
    from orchestrator.llm.openai_compat import OpenAICompatibleLLMClient

    cfg = get_settings()
    match = next((b for b in cfg.llm.backends if b.name == name), None)
    if match is None:
        available = [b.name for b in cfg.llm.backends]
        click.echo(click.style(f"Backend {name!r} not found.", fg="red"), err=True)
        click.echo(f"Available: {', '.join(available) or '(none)'}")
        raise SystemExit(1)

    if action == "test":
        click.echo(f"Testing backend: {name}  ({match.base_url})")
        import time
        client = OpenAICompatibleLLMClient(match)
        t0 = time.monotonic()
        ok = client._probe_health()
        latency_ms = (time.monotonic() - t0) * 1000
        status_label = click.style("healthy", fg="green") if ok else click.style("unavailable", fg="red")
        click.echo(f"  Status:   {status_label}  ({latency_ms:.0f}ms)")
        if ok:
            models = client.list_models()
            if models:
                click.echo(f"  Models ({len(models)}):")
                for m in models[:20]:
                    click.echo(f"    - {m}")
                if len(models) > 20:
                    click.echo(f"    … and {len(models) - 20} more")
            else:
                click.echo("  Models:  (none detected via /v1/models)")


@main.command()
def tools():
    """List all registered tools available for LLM function calling."""
    import json

    from orchestrator.factory import create_engine

    engine = create_engine()
    tool_list = engine.tool_registry.list_tools()
    if not tool_list:
        click.echo("No tools registered.")
        return

    click.echo(f"{'Name':<12s} {'Description'}")
    click.echo("-" * 60)
    for t in tool_list:
        click.echo(f"  {t.name:<10s} {t.description}")
    click.echo(f"\n{len(tool_list)} tools registered.")
    click.echo("\nSchemas (JSON):")
    for t in tool_list:
        click.echo(f"  {t.name}: {json.dumps(t.parameters, ensure_ascii=False)}")


@main.group(invoke_without_command=True)
@click.pass_context
def models(ctx: click.Context):
    """Manage and inspect model configuration."""
    if ctx.invoked_subcommand is not None:
        return
    # Default: show configured models and aliases from registry

    cfg = get_settings()
    click.echo("Roles:")
    click.echo(f"  default:   {cfg.models.default}")
    click.echo(f"  fast:      {cfg.models.fast}")
    click.echo(f"  code:      {cfg.models.code}")
    click.echo(f"  deep:      {cfg.models.deep}")
    click.echo(f"  embedding: {cfg.models.embedding}")
    aliases = {}
    if aliases:
        click.echo("\nAliases:")
        for alias, target in sorted(aliases.items()):
            click.echo(f"  {alias:12s} → {target}")


@models.command(name="active")
def models_active():
    """Show models currently available in Ollama."""
    import httpx

    cfg = get_settings()
    try:
        resp = httpx.get(f"{cfg.ollama.base_url}/api/tags", timeout=5.0)
        resp.raise_for_status()
        data = resp.json().get("models", [])
        if not data:
            click.echo("No models found in Ollama.")
            return
        click.echo(f"{'Model':<30s} {'Size':>10s}")
        click.echo("-" * 42)
        for m in data:
            name = m.get("name", "?")
            size_gb = m.get("size", 0) / (1024**3)
            click.echo(f"  {name:<28s} {size_gb:>6.1f} GB")
    except Exception as exc:
        click.echo(click.style(f"Error querying Ollama: {exc}", fg="red"), err=True)


@models.command(name="resolve")
@click.argument("name")
def models_resolve(name: str):
    """Resolve a profile key to its configured model."""
    from orchestrator.registry import get_registry

    reg = get_registry()
    resolved = reg.get_model_for_profile(name)
    if resolved and resolved != name:
        click.echo(f"{name} → {resolved}")
    else:
        click.echo(f"{name} (passthrough — not a profile key)")


@main.command(name="install-aliases")
def install_aliases_cmd():
    """Install model aliases as executable commands in ~/.local/bin/.

    After running this, the configured terminal alias becomes available as a
    direct command in ANY terminal — zero Python dependency at runtime:

        @ olá!                         # → curl POST /query (streaming)
        @ corrige este bug             # → curl POST /query (streaming)
        cat file.py | @ analisa isto   # stdin support

    Requires: symbiont running via Docker (make infra), ~/.local/bin on PATH, curl installed.
    """
    from orchestrator.cli.aliases import install_aliases

    installed = install_aliases()
    if not installed:
        click.echo("No aliases to install.")
        return

    click.echo(f"Installed {len(installed)} model aliases in ~/.local/bin/:")
    for alias, path in installed.items():
        click.echo(f"  {alias:12s} → {path}")

    # Check if ~/.local/bin is on PATH
    from pathlib import Path
    user_bin = str(Path.home() / ".local" / "bin")
    if user_bin not in os.environ.get("PATH", ""):
        click.echo(click.style(
            f"\n⚠ {user_bin} is not on your PATH. Add it to your shell config:\n"
            f"  export PATH=\"$HOME/.local/bin:$PATH\"",
            fg="yellow",
        ))
    else:
        click.echo(click.style(
            "\n✓ Aliases ready. Ensure the symbiont is running (make infra).",
            fg="green",
        ))


@main.command(name="remove-aliases")
def remove_aliases_cmd():
    """Remove all auto-generated model alias commands from ~/.local/bin/."""
    from orchestrator.cli.aliases import remove_aliases

    removed = remove_aliases()
    if not removed:
        click.echo("No alias scripts found to remove.")
        return

    click.echo(f"Removed {len(removed)} aliases: {', '.join(removed)}")
    click.echo(click.style("✓ All alias scripts removed.", fg="green"))


# ---------------------------------------------------------------------------
# Warmup & Performance commands
# ---------------------------------------------------------------------------

@main.command()
@click.argument("model", required=False)
def warmup(model: str | None):
    """Pre-warm model(s) into VRAM for faster inference.

    \b
    orc warmup             Warm all configured primary/fallback models.
    orc warmup qwen3:8b    Warm a specific model.
    """
    from orchestrator.core.warmup import get_warmup_manager

    mgr = get_warmup_manager()
    cfg = get_settings()

    if model:
        # Use model name directly (no alias resolution)
        click.echo(f"Warming {model} (keep_alive={cfg.performance.keep_alive})...")
        ok = mgr.warm_model(model)
        if ok:
            click.echo(click.style(f"  ✓ {model} loaded into VRAM", fg="green"))
        else:
            click.echo(click.style(f"  ✗ Failed to warm {model}", fg="red"))
    else:
        click.echo(f"Warming configured models (keep_alive={cfg.performance.keep_alive})...")
        results = mgr.warm_all()
        for m, ok in results.items():
            icon = click.style("✓", fg="green") if ok else click.style("✗", fg="red")
            click.echo(f"  {icon} {m}")
        total_ok = sum(1 for v in results.values() if v)
        click.echo(f"\n{total_ok}/{len(results)} models warmed successfully.")


@models.command(name="status")
def models_status():
    """Show warm/cold status of all configured models."""
    from orchestrator.core.warmup import get_warmup_manager

    mgr = get_warmup_manager()
    cfg = get_settings()

    # Get currently loaded models
    warm_status = mgr.get_warm_status(force_refresh=True)

    # All configured models
    all_models = set()
    for b in cfg.llm.backends:
        if b.enabled:
            all_models.update(b.models)

    click.echo(f"{'Model':<28s} {'Status':<8s} {'VRAM':>8s}  {'Expires'}")
    click.echo("─" * 60)
    for model in sorted(all_models):
        status = warm_status.get(model)
        if status and status.warm:
            icon = click.style("WARM", fg="green")
            vram = f"{status.vram_bytes / (1024**3):.1f} GB" if status.vram_bytes else "—"
            expires = status.expires_at[:19] if status.expires_at else "—"
        else:
            icon = click.style("COLD", fg="red")
            vram = "—"
            expires = "—"
        click.echo(f"  {model:<26s} {icon:<8s} {vram:>8s}  {expires}")


@main.group(invoke_without_command=True)
@click.pass_context
def benchmark(ctx: click.Context):
    """Benchmark local models for latency and throughput."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@benchmark.command(name="models")
@click.option("--model", "-m", default=None, help="Test a specific model only.")
@click.option("--task", "-t", type=click.Choice(["short", "code", "reasoning", "all"]), default="all")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def benchmark_models(model: str | None, task: str, as_json: bool):
    """Benchmark each model with fixed prompts and measure latency/throughput."""
    from orchestrator.core.benchmark import BenchmarkRunner

    runner = BenchmarkRunner()
    results = runner.run(model_filter=model, task_filter=task)

    if as_json:
        import json as _json
        click.echo(_json.dumps(results, indent=2, default=str))
        return

    click.echo(f"\n{'Model':<24s} {'Task':<12s} {'Total ms':>9s} {'1st tok ms':>11s} {'Gen tok/s':>10s} {'Tokens':>7s}")
    click.echo("─" * 80)
    for r in results:
        gen_tps = f"{r['generation_tokens_per_second']:.1f}" if r.get("generation_tokens_per_second") else "—"
        first_tok = f"{r['first_token_ms']:.0f}" if r.get("first_token_ms") else "—"
        click.echo(
            f"  {r['model']:<22s} {r['task']:<12s} "
            f"{r['total_latency_ms']:>8.0f}  {first_tok:>10s}  {gen_tps:>9s}  {r.get('output_tokens', '—'):>6}"
        )


@main.command()
@click.option("--days", "-d", default=7, help="Period in days.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def stats(days: int, as_json: bool):
    """Show unified usage statistics from sessions.db + metrics.db."""
    import json as _json

    from config.storage_paths import symbiont_data_path
    from orchestrator.analytics import AnalyticsService

    cfg = get_settings()
    sessions_db_path = cfg.session.db_path if cfg.session.enabled else None
    metrics_db_path = cfg.metrics.db_path or str(symbiont_data_path("symbiont", "metrics.db"))

    svc = AnalyticsService(
        sessions_db_path=sessions_db_path,
        metrics_db_path=metrics_db_path,
    )
    try:
        summary = svc.summary(days=days)
    finally:
        svc.close()

    if as_json:
        click.echo(_json.dumps(summary, indent=2))
        return

    sources = summary.get("data_sources_used", [])
    click.echo(click.style(f"── Stats ({days} days) ──", fg="cyan"))
    click.echo(f"  Sessions:      {summary.get('total_sessions', 0)}")
    click.echo(f"  Messages:      {summary.get('total_messages', 0)}")
    click.echo(f"  LLM Requests:  {summary.get('total_requests', 0)}")
    click.echo(f"  Total tokens:  {summary.get('total_tokens', 0):,}")
    click.echo(f"  Avg latency:   {summary.get('avg_latency_ms', 0):.0f}ms")
    click.echo(f"  Error rate:    {summary.get('error_rate', 0):.1f}%")
    if summary.get("top_model"):
        click.echo(f"  Top model:     {summary['top_model']}")
    if summary.get("top_backend"):
        click.echo(f"  Top backend:   {summary['top_backend']}")
    click.echo(click.style(f"  Sources: {', '.join(sources)}", fg="bright_black"))


# ---------------------------------------------------------------------------
# Observability subgroup
# ---------------------------------------------------------------------------

@main.group()
def observability():
    """Observability stack management."""
    pass


@observability.command()
def status():
    """Show status of the observability stack components."""
    from orchestrator.observability.config import ObservabilityConfig

    cfg = get_settings()
    obs_config = ObservabilityConfig.from_dict(cfg.observability_raw)

    click.echo(click.style("── Observability Status ──", fg="cyan"))
    click.echo(f"  Enabled:        {obs_config.enabled}")
    click.echo(f"  Backend:        {obs_config.backend}")
    click.echo(f"  JSONL logs:     {'✓' if obs_config.local_logs.enabled else '✗'} ({obs_config.local_logs.path})")
    click.echo(f"  ClickHouse:     {'✓' if obs_config.clickhouse.enabled else '✗'} ({obs_config.clickhouse.url})")
    click.echo(f"  OTel:           {'✓' if obs_config.otel.enabled else '✗'} ({obs_config.otel.endpoint})")
    click.echo(f"  Resources:      {'✓' if obs_config.resources.enabled else '✗'}")
    click.echo(f"  Privacy:        redact_secrets={obs_config.privacy.redact_secrets} hash_queries={obs_config.privacy.hash_queries}")

    # Check ClickHouse connectivity if enabled
    if obs_config.clickhouse.enabled:
        try:
            import httpx
            r = httpx.get(f"{obs_config.clickhouse.url}/ping", timeout=2.0)
            ch_ok = r.status_code == 200
        except Exception:
            ch_ok = False
        click.echo(f"  ClickHouse ping: {'✓ OK' if ch_ok else '✗ UNREACHABLE'}")


@observability.command()
@click.option("--lines", "-n", default=20, help="Number of recent events.")
def tail(lines: int):
    """Tail the JSONL event log."""
    import json
    from pathlib import Path

    from config.storage_paths import symbiont_logs_path

    cfg = get_settings()
    obs_config_raw = cfg.observability_raw
    log_path = Path(os.path.expanduser(
        obs_config_raw.get("local_logs", {}).get("path", str(symbiont_logs_path()))
    )) / "events.jsonl"

    if not log_path.exists():
        click.echo(f"No log file found at {log_path}")
        return

    # Read last N lines efficiently
    with open(log_path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block_size = min(size, lines * 2048)
        f.seek(max(0, size - block_size))
        data = f.read().decode("utf-8", errors="replace")

    all_lines = [line_ for line_ in data.splitlines() if line_.strip()]
    for line in all_lines[-lines:]:
        try:
            evt = json.loads(line)
            ts = evt.get("timestamp", "")[:19]
            name = evt.get("event", "?")
            model = evt.get("model", "")
            lat = evt.get("total_latency_ms")
            lat_str = f" {lat:.0f}ms" if lat else ""
            click.echo(f"  {ts}  {name:<24} {model:<20}{lat_str}")
        except json.JSONDecodeError:
            click.echo(f"  {line[:100]}")


@observability.command()
def setup():
    """Initialize ClickHouse schema (requires docker stack running)."""
    from pathlib import Path

    import httpx

    cfg = get_settings()
    obs_raw = cfg.observability_raw
    ch_cfg = obs_raw.get("clickhouse", {})
    url = ch_cfg.get("url", "https://localhost:8123")
    username = ch_cfg.get("username", "default")
    password_env = ch_cfg.get("password_env", "CLICKHOUSE_PASSWORD")
    password = os.environ.get(password_env, "")

    schema_path = Path(__file__).parent.parent / "core" / "observability" / "schema.sql"
    if not schema_path.exists():
        click.echo("Schema file not found!", err=True)
        raise SystemExit(1)

    sql = schema_path.read_text()
    # Split by semicolons and execute each statement
    statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]

    click.echo(f"Applying schema to {url} ({len(statements)} statements)...")
    errors = 0
    for stmt in statements:
        try:
            r = httpx.post(
                url,
                content=stmt,
                params={"user": username, "password": password} if password else {"user": username},
                timeout=10.0,
            )
            if r.status_code != 200:
                click.echo(click.style(f"  ✗ {r.text.strip()[:100]}", fg="red"))
                errors += 1
        except Exception as exc:
            click.echo(click.style(f"  ✗ {exc}", fg="red"))
            errors += 1

    if errors:
        click.echo(click.style(f"\n{errors} errors", fg="red"))
    else:
        click.echo(click.style("✓ Schema applied successfully", fg="green"))


# ---------------------------------------------------------------------------
# Standalone resource monitor
# ---------------------------------------------------------------------------

@main.command()
@click.option("--interval", "-i", default=2.0, help="Sample interval in seconds.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress stdout output (ClickHouse only).")
def monitor(interval: float, quiet: bool):
    """Run standalone resource monitor (writes to ClickHouse continuously).

    Use this to keep resource graphs populated even when the API is not running.
    Can be run as a background service: orc monitor -q &
    """
    import signal
    import time as _time
    from datetime import datetime, timezone

    cfg = get_settings()

    # Setup ClickHouse sink
    from orchestrator.observability.config import ObservabilityConfig
    obs_config = ObservabilityConfig.from_dict(cfg.observability_raw)

    if not obs_config.clickhouse.enabled:
        click.echo(click.style("✗ ClickHouse not enabled in config", fg="red"))
        return

    from orchestrator.observability.clickhouse import ClickHouseSink
    ch_sink = ClickHouseSink(obs_config.clickhouse)
    if not ch_sink.available:
        click.echo(click.style("✗ ClickHouse not reachable", fg="red"))
        return

    # Setup resource monitor
    from orchestrator.observability.config import ResourceMonitorConfig
    res_cfg = ResourceMonitorConfig(
        enabled=True,
        sample_interval_seconds=interval,
        collect_cpu=obs_config.resources.collect_cpu,
        collect_ram=obs_config.resources.collect_ram,
        collect_gpu=obs_config.resources.collect_gpu,
        gpu_backend=obs_config.resources.gpu_backend,
    )
    from orchestrator.observability.resource_monitor import ResourceMonitor
    mon = ResourceMonitor(res_cfg)

    stop = False

    def _handle_signal(sig, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not quiet:
        click.echo(click.style(f"── Resource Monitor (interval={interval}s) ──", fg="cyan"))
        click.echo("  Press Ctrl+C to stop\n")

    samples = 0
    try:
        while not stop:
            snap = mon.snapshot_now()
            if snap.cpu_percent is not None:
                dt = datetime.fromtimestamp(snap.timestamp or _time.time(), tz=timezone.utc)
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}"
                row = {
                    "timestamp": ts_str,
                    "cpu_percent": round(snap.cpu_percent, 1) if snap.cpu_percent else 0,
                    "ram_used_mb": snap.ram_used_mb or 0,
                    "ram_total_mb": snap.ram_total_mb or 0,
                    "ram_percent": round(snap.ram_percent, 1) if snap.ram_percent else 0,
                    "gpu_util_percent": round(snap.gpu_util_percent, 1) if snap.gpu_util_percent else 0,
                    "vram_used_mb": snap.vram_used_mb or 0,
                    "vram_total_mb": snap.vram_total_mb or 0,
                    "vram_free_mb": snap.vram_free_mb or 0,
                    "gpu_name": snap.gpu_name or "",
                    "gpu_temperature_c": round(snap.gpu_temperature_c, 1) if snap.gpu_temperature_c else 0,
                    "gpu_power_w": round(snap.gpu_power_w, 1) if snap.gpu_power_w else 0,
                }
                ch_sink.write_to_table("resource_samples", row)
                samples += 1

                if not quiet:
                    click.echo(
                        f"  [{ts_str}] CPU={snap.cpu_percent:.0f}% "
                        f"RAM={snap.ram_used_mb}MB/{snap.ram_total_mb}MB "
                        f"GPU={snap.gpu_util_percent:.0f}% "
                        f"VRAM={snap.vram_used_mb}MB/{snap.vram_total_mb}MB "
                        f"T={snap.gpu_temperature_c:.0f}°C "
                        f"P={snap.gpu_power_w:.0f}W"
                    )

            _time.sleep(interval)
    finally:
        if not quiet:
            click.echo(f"\n  Stopped. {samples} samples written to ClickHouse.")


# ---------------------------------------------------------------------------
# Execution Layer CLI
# ---------------------------------------------------------------------------

@main.group()
def execution():
    """Multi-agent code execution layer management."""
    pass


@execution.command(name="status")
def execution_status():
    """Show execution layer health (Redis, Docker, worker image, API key)."""
    import asyncio


    cfg = get_settings()
    if not cfg.execution:
        click.echo(click.style("Execution layer not enabled in config/orc/agents.toml", fg="yellow"))
        click.echo("  Set [execution] enabled = true to activate.")
        return

    from orchestrator.execution.health import check_health

    report = asyncio.run(check_health(cfg.execution))
    click.echo(report.summary())


@execution.command(name="run")
@click.argument("task")
@click.option("--workspace", "-w", default=None, help="Workspace path (default: from config)")
@click.option("--max-workers", "-n", default=None, type=int, help="Max workers (default: from config)")
@click.option("--mock", is_flag=True, help="Run in mock mode (no real containers)")
def execution_run(task: str, workspace: str | None, max_workers: int | None, mock: bool):
    """Run a code execution task directly (bypasses full pipeline)."""
    import asyncio

    cfg = get_settings()
    if not cfg.execution:
        click.echo(click.style("Execution layer not enabled.", fg="red"))
        return

    from orchestrator.execution import create_execution_layer
    from orchestrator.execution.config import ExecutionConfig
    from orchestrator.execution.models import ExecutionTask

    exec_cfg = cfg.execution
    if mock:
        # Override mock_mode for this run
        exec_cfg = ExecutionConfig(
            enabled=True,
            redis_url=exec_cfg.redis_url,
            workspace_path=workspace or exec_cfg.workspace_path,
            worker_image=exec_cfg.worker_image,
            max_workers_per_execution=max_workers or exec_cfg.max_workers_per_execution,
            max_concurrent_executions=exec_cfg.max_concurrent_executions,
            worker_timeout_seconds=exec_cfg.worker_timeout_seconds,
            execution_timeout_seconds=exec_cfg.execution_timeout_seconds,
            cleanup_on_completion=exec_cfg.cleanup_on_completion,
            mock_mode=True,
            resources=exec_cfg.resources,
            gemini=exec_cfg.gemini,
        )

    coordinator = create_execution_layer(exec_cfg)
    exec_task = ExecutionTask(
        query=task,
        workspace_path=workspace or exec_cfg.workspace_path,
        max_workers=max_workers or exec_cfg.max_workers_per_execution,
    )

    click.echo(click.style(f"Executing: {task[:80]}...", fg="cyan"))
    click.echo(f"  Workspace: {exec_task.workspace_path}")
    click.echo(f"  Max workers: {exec_task.max_workers}")
    click.echo()

    result = asyncio.run(coordinator.execute(exec_task))

    if result.success:
        click.echo(click.style("SUCCESS", fg="green"))
    else:
        click.echo(click.style("FAILED", fg="red"))

    click.echo(f"  Workers spawned: {result.workers_spawned}")
    click.echo(f"  Duration: {result.total_duration_ms:.0f}ms")
    if result.files_modified:
        click.echo(f"  Files modified: {', '.join(result.files_modified)}")
    click.echo()
    click.echo(result.output)


@execution.command(name="build-worker")
def execution_build_worker():
    """Build the worker Docker image."""
    import subprocess
    from pathlib import Path

    cfg = get_settings()
    exec_cfg = cfg.execution
    if not exec_cfg:
        from orchestrator.execution.config import ExecutionConfig
        exec_cfg = ExecutionConfig()

    docker_dir = Path(__file__).parent.parent.parent.parent / "docker"
    dockerfile = docker_dir / "Dockerfile.orc-worker"
    _worker_protocol = Path(__file__).parent.parent / "execution" / "worker_protocol.py"

    if not dockerfile.exists():
        click.echo(click.style(f"Dockerfile not found: {dockerfile}", fg="red"))
        return

    image_name = exec_cfg.worker_image
    click.echo(f"Building worker image: {image_name}")
    click.echo(f"  Dockerfile: {dockerfile}")

    uid = os.getuid()
    gid = os.getgid()

    cmd = [
        "docker", "build",
        "-f", str(dockerfile),
        "--build-arg", f"USER_UID={uid}",
        "--build-arg", f"USER_GID={gid}",
        "-t", image_name,
        str(Path(__file__).parent.parent / "execution"),  # context = execution/ dir (has worker_protocol.py)
    ]

    click.echo(f"  Command: {' '.join(cmd)}")
    click.echo()

    result = subprocess.run(cmd)
    if result.returncode == 0:
        click.echo(click.style(f"\nImage built: {image_name}", fg="green"))
    else:
        click.echo(click.style(f"\nBuild failed (exit {result.returncode})", fg="red"))


@execution.command(name="cleanup")
def execution_cleanup():
    """Stop and remove any lingering execution containers."""
    cfg = get_settings()
    if not cfg.execution:
        click.echo(click.style("Execution layer not enabled.", fg="yellow"))
        return

    from orchestrator.execution.container_manager import ContainerManager
    mgr = ContainerManager(cfg.execution)
    count = mgr.cleanup_orphans()
    click.echo(f"Removed {count} orphaned execution containers.")


@execution.command(name="infra-up")
def execution_infra_up():
    """Start execution infrastructure (Redis)."""
    import subprocess
    from pathlib import Path

    compose_file = Path(__file__).parent.parent / "execution" / "docker" / "compose.infra.yml"
    if not compose_file.exists():
        click.echo(click.style(f"Compose file not found: {compose_file}", fg="red"))
        return

    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
    )
    if result.returncode == 0:
        click.echo(click.style("Execution infrastructure started (Redis on port 6380)", fg="green"))
    else:
        click.echo(click.style("Failed to start infrastructure", fg="red"))


@execution.command(name="infra-down")
def execution_infra_down():
    """Stop execution infrastructure (Redis)."""
    import subprocess
    from pathlib import Path

    compose_file = Path(__file__).parent.parent / "execution" / "docker" / "compose.infra.yml"
    if not compose_file.exists():
        click.echo(click.style(f"Compose file not found: {compose_file}", fg="red"))
        return

    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "down"],
    )
    if result.returncode == 0:
        click.echo(click.style("Execution infrastructure stopped", fg="green"))
    else:
        click.echo(click.style("Failed to stop infrastructure", fg="red"))


@main.command(name="render-test")
def render_test_cmd():
    """Test the terminal renderer with sample Markdown content."""
    from orchestrator.cli.renderer import TerminalRenderer

    renderer = TerminalRenderer()

    sample = (
        "## System Status Report\n\n"
        "**Node:** ai-workstation · **Uptime:** 3d 14h\n\n"
        "### Resource Usage\n\n"
        "| Resource | Used | Total | Usage |\n"
        "|----------|------|-------|-------|\n"
        "| RAM | 10.2 GB | 30.5 GB | 33.9% |\n"
        "| VRAM | 6.6 GB | 8.2 GB | 80.9% |\n"
        "| Disk | 45 GB | 500 GB | 9.0% |\n\n"
        "### Top Processes\n\n"
        "| PID | Process | RAM (MB) | CPU (%) |\n"
        "|-----|---------|----------|--------|\n"
        "| 112301 | ollama | 1057.7 | 10.5 |\n"
        "| 5608 | python3 | 855.4 | 1.1 |\n"
        "| 5610 | clickhouse-server | 751.1 | 10.7 |\n\n"
        "### Notes\n\n"
        "- The `ollama` process is the **main VRAM consumer** due to loaded models.\n"
        "- System memory usage is moderate at 33.9%.\n"
        "- Consider unloading unused models with `ollama stop`.\n\n"
        "```bash\n"
        "# Check GPU utilization\n"
        "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader\n"
        "```\n"
    )
    renderer.render_final(sample)
