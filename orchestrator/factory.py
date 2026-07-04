"""Factory — builds a fully-wired symbiont with HTTP dispatch to external services.

No in-process agents or context providers. All external calls go through
the dispatch layer (agent_client, feature_client).
"""

from __future__ import annotations

import logging

from orchestrator.capabilities.catalog import service_registry_config
from orchestrator.config import get_settings
from orchestrator.dispatch.agent_client import AgentClient
from orchestrator.dispatch.client import HTTPServiceClient
from orchestrator.dispatch.feature_client import FeatureClient
from orchestrator.dispatch.service_registry import ServiceRegistry
from orchestrator.llm.router import LLMRouter

log = logging.getLogger(__name__)


def _init_hardware() -> None:
    """Run hardware auto-detection and compute adaptive overrides (non-fatal)."""
    cfg = get_settings()
    if not cfg.hardware.auto_detect:
        log.debug("Hardware auto-detection disabled")
        return
    try:
        from orchestrator.core.adaptive_config import get_adaptive_overrides
        from orchestrator.core.hardware_profile import get_hardware_profile

        profile = get_hardware_profile(ollama_url=cfg.ollama.base_url)
        get_adaptive_overrides(profile)
    except Exception as exc:
        log.warning("Hardware auto-detection failed (non-critical): %s", exc)


def _build_service_registry(cfg=None) -> ServiceRegistry:
    """Build the service registry from config.

    Reads [services] section from settings to discover agent and feature endpoints.
    If container lifecycle management is enabled, attaches the lifecycle manager.
    """
    if cfg is None:
        cfg = get_settings()

    http_client = HTTPServiceClient(
        pool_size=cfg.pipeline.connection_pool_size,
        circuit_threshold=cfg.rag.circuit_breaker_threshold,
        circuit_reset_seconds=cfg.rag.circuit_breaker_reset,
    )

    # Container lifecycle manager (on-demand start/stop)
    lifecycle_manager = None
    if cfg.container_lifecycle.enabled:
        from orchestrator.lifecycle import ContainerLifecycleManager
        lifecycle_manager = ContainerLifecycleManager(
            docker_host=cfg.container_lifecycle.docker_host,
            compose_project=cfg.container_lifecycle.compose_project,
            compose_file=cfg.container_lifecycle.compose_file,
            compose_project_dir=cfg.container_lifecycle.compose_project_dir,
            compose_profiles=list(cfg.container_lifecycle.compose_profiles),
            idle_timeout=cfg.container_lifecycle.idle_timeout,
            start_timeout=cfg.container_lifecycle.start_timeout,
            health_poll_interval=cfg.container_lifecycle.health_poll_interval,
            idle_check_interval=cfg.container_lifecycle.idle_check_interval,
            always_on=list(cfg.container_lifecycle.always_on),
            pre_warm=list(cfg.container_lifecycle.pre_warm),
            per_service_overrides=cfg.container_lifecycle.per_service_overrides,
        )
        if not lifecycle_manager.available:
            log.warning("Container lifecycle enabled but Docker unavailable — falling back to manual mode")
            lifecycle_manager = None

    services_config = service_registry_config(cfg)
    registry = ServiceRegistry.from_config(services_config, client=http_client, lifecycle_manager=lifecycle_manager)

    # Start idle reaper and pre-warm if lifecycle is active
    if lifecycle_manager and lifecycle_manager.available:
        lifecycle_manager.start_reaper()
        lifecycle_manager.pre_warm_services()
        log.info("Container lifecycle active: idle_timeout=%ds, pre_warm=%s",
                 cfg.container_lifecycle.idle_timeout, list(cfg.container_lifecycle.pre_warm))

    return registry

def _build_prewarm_engine(cfg=None, lifecycle_manager=None):
    """Build the predictive prewarming engine from config.

    Returns None if prewarming is disabled or initialization fails.
    """
    if cfg is None:
        cfg = get_settings()

    if not cfg.prewarming.enabled:
        log.debug("Predictive prewarming disabled")
        return None

    try:
        from orchestrator.prewarming import set_prewarm_engine
        from orchestrator.prewarming.config import resolve_catalog_path
        from orchestrator.prewarming.engine import PrewarmEngine
        from orchestrator.prewarming.feature_catalog import FeatureCatalog

        catalog_path = resolve_catalog_path(cfg.prewarming)
        catalog = FeatureCatalog(catalog_path)

        engine = PrewarmEngine(
            cfg.prewarming,
            catalog,
            lifecycle_manager=lifecycle_manager,
            ollama_base_url=cfg.ollama.base_url,
        )
        set_prewarm_engine(engine)
        log.info("PrewarmEngine built: catalog=%s, features=%d",
                 catalog_path.name, len(catalog.get_all()))
        return engine

    except Exception as exc:
        log.warning("PrewarmEngine creation failed (non-fatal): %s", exc)
        return None


def create_graph():
    """Create a compiled LangGraph workflow with HTTP dispatch to external services.

    This is the main orchestration entry point. The symbiont:
    - Classifies intent/complexity locally (heuristics + optional LLM)
    - Routes to context sources and agents
    - Dispatches HTTP calls to feature services for context
    - Dispatches HTTP calls to agent services for processing
    - Synthesizes results locally (LLM)
    - Evaluates quality via HTTP to critic service

    Returns:
        Compiled LangGraph StateGraph ready for .invoke() / .stream().
    """
    cfg = get_settings()

    # Hardware detection
    _init_hardware()

    # LLM Router (symbiont's own LLM access for routing/synthesis/direct)
    router = LLMRouter(
        cfg.llm,
        latency_routing=cfg.latency_routing,
        inference_profiles=cfg.inference_profiles,
    )

    # LangChain adapter wrapping our multi-backend LLM client
    from orchestrator.llm.langchain_adapter import SymbiontChatModel
    llm_adapter = SymbiontChatModel(
        llm_client=router,
        model=cfg.models.default,
        temperature=0.7,
        use_native_ollama=True,
    )

    # --- Service Registry (HTTP dispatch layer) ---
    registry = _build_service_registry(cfg)
    registry.start_health_checks(interval=30.0)

    # --- Predictive Prewarming Engine ---
    lifecycle_mgr = getattr(registry, "_lifecycle", None)
    _build_prewarm_engine(cfg, lifecycle_manager=lifecycle_mgr)

    # Typed clients
    agent_client = AgentClient(registry)
    feature_client = FeatureClient(registry)

    # --- Memory & Learning ---
    from orchestrator.pipeline.planning.pattern_store import PatternStore
    from orchestrator.routing.decision_log import RoutingDecisionLog
    routing_log = RoutingDecisionLog()
    pattern_store = PatternStore(routing_log)

    # --- Build and compile the graph ---
    from orchestrator.pipeline.workflow import build_workflow
    compiled_graph = build_workflow(
        llm_adapter=llm_adapter,
        agent_client=agent_client,
        feature_client=feature_client,
        routing_log=routing_log,
        pattern_store=pattern_store,
        collaboration_config=cfg.collaboration,
        pipeline_config=cfg.pipeline,
        dynamic_routing_config=cfg.dynamic_routing,
        intelligent_config=cfg.intelligent_pipeline,
    )

    # Store references for health/status reporting
    compiled_graph._service_registry = registry  # type: ignore[attr-defined]
    compiled_graph._feature_client = feature_client  # type: ignore[attr-defined]
    compiled_graph._llm_router = router  # type: ignore[attr-defined]

    log.info("Decoupled LangGraph workflow created — all agents via HTTP dispatch")
    return compiled_graph


def create_engine():
    """Create the Engine wrapper used by gateway and CLI entry points."""
    cfg = get_settings()
    _init_hardware()

    router = LLMRouter(
        cfg.llm,
        latency_routing=cfg.latency_routing,
        inference_profiles=cfg.inference_profiles,
    )

    # Security Layer (if enabled)
    security_layer = None
    _any_security = (
        cfg.security.injection_scanning
        or cfg.security.secrets_scanning
        or cfg.security.rate_limiting
        or cfg.security.audit_trail
    )
    if _any_security:
        from orchestrator.security.layer import SecurityLayer
        security_layer = SecurityLayer(cfg.security)

    from orchestrator.core.engine import Engine
    engine = Engine(llm=router, security_layer=security_layer)

    # Attach dispatch layer for HTTP-based context gathering
    registry = _build_service_registry(cfg)
    registry.start_health_checks(interval=30.0)
    engine._service_registry = registry
    engine._feature_client = FeatureClient(registry)
    engine._agent_client = AgentClient(registry)

    # Memory
    from orchestrator.pipeline.planning.pattern_store import PatternStore
    from orchestrator.routing.decision_log import RoutingDecisionLog
    engine._routing_log = RoutingDecisionLog()
    engine._pattern_store = PatternStore(engine._routing_log)

    return engine
