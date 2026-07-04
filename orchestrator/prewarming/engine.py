"""PrewarmEngine — orchestrates the full prediction pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from orchestrator.config import PrewarmConfig
from orchestrator.prewarming.aggregator import Aggregator
from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.guards import DirectAnswerGuard
from orchestrator.prewarming.learning import LearningLoop
from orchestrator.prewarming.policy import PolicyEngine
from orchestrator.prewarming.routers.embedding_router import EmbeddingRouter
from orchestrator.prewarming.routers.fastembed_router import FastEmbedRouter
from orchestrator.prewarming.routers.lightweight_router import LightweightRouter
from orchestrator.prewarming.routers.micro_classifier import MicroClassifier
from orchestrator.prewarming.routers.rule_router import RuleRouter
from orchestrator.prewarming.routers.semantic_router_adapter import SemanticRouterAdapter
from orchestrator.prewarming.signals import SignalExtractor
from orchestrator.prewarming.state import PrewarmMetrics, PrewarmState

if TYPE_CHECKING:
    from orchestrator.lifecycle.manager import ContainerLifecycleManager

log = logging.getLogger(__name__)


def _log_value(value: object, limit: int = 160) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:limit]


class PrewarmEngine:
    """Orchestrates the full predictive prewarming pipeline.

    Pipeline:
        1. Extract signals (sub-ms)
        2. Level 0: Rule router (sub-ms)
        3. Level 1: Embedding router (~10ms)
        4. Level 2: Micro-classifier (only if ambiguous, ~500ms cap)
        5. Aggregate scores
        6. Apply policy
        7. Fire container starts (non-blocking)

    The entire pipeline runs async and never blocks the main request path.
    """

    def __init__(
        self,
        cfg: PrewarmConfig,
        catalog: FeatureCatalog,
        *,
        lifecycle_manager: ContainerLifecycleManager | None = None,
        ollama_base_url: str,
    ) -> None:
        self._cfg = cfg
        self._catalog = catalog
        self._lifecycle = lifecycle_manager
        self._ollama_url = ollama_base_url

        # Sub-components
        self._signal_extractor = SignalExtractor(catalog)
        self._rule_router = RuleRouter(catalog)
        # L1 CPU router: FastEmbed (semantic) or TF-IDF (char n-gram)
        if cfg.l1_backend == "fastembed":
            self._lightweight_router = FastEmbedRouter(
                catalog, model_name=cfg.l1_fastembed_model
            )
        else:
            self._lightweight_router = LightweightRouter(catalog)
        # L1.5 Semantic Router — disambiguates when L1 is ambiguous
        self._semantic_router = SemanticRouterAdapter(
            catalog, model_name=cfg.l1_fastembed_model
        )
        self._embedding_router = EmbeddingRouter(
            catalog,
            ollama_base_url=ollama_base_url,
            model=cfg.embedding_model,
        )
        self._micro_classifier = MicroClassifier(
            catalog,
            ollama_base_url=ollama_base_url,
            model=cfg.classifier_model,
            timeout_ms=cfg.classifier_timeout_ms,
        )
        self._aggregator = Aggregator(cfg, catalog)
        self._learning = LearningLoop()
        self._policy = PolicyEngine(cfg, catalog, learning=self._learning)
        self._guard = DirectAnswerGuard()

        # State tracking
        self._active_states: dict[str, PrewarmState] = {}
        self._metrics = PrewarmMetrics()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize routers (pre-compute embeddings/vectors)."""
        if self._initialized:
            return

        # L1 lightweight (CPU, always available, <1ms)
        try:
            self._lightweight_router.initialize()
        except Exception as e:
            log.warning("Lightweight router init failed (non-fatal): %s", e)

        # L1.5 semantic router (CPU, shares FastEmbed model)
        try:
            self._semantic_router.initialize()
        except Exception as e:
            log.warning("Semantic router init failed (non-fatal): %s", e)

        # L1 full embedding (GPU/Ollama, optional)
        if self._cfg.level1_enabled:
            try:
                await self._embedding_router.initialize()
            except Exception as e:
                log.warning("Embedding router initialization failed (non-fatal): %s", e)

        # Sync per-feature TTL to lifecycle manager
        self._sync_ttl_to_lifecycle()

        self._initialized = True
        log.info("PrewarmEngine initialized (L1_cpu=%s, L1.5_semantic=yes, L1_ollama=%s, L2=%s)",
                 self._cfg.l1_backend, self._cfg.level1_enabled, self._cfg.level2_enabled)

    def _sync_ttl_to_lifecycle(self) -> None:
        """Push per-feature ttl_idle from catalog to lifecycle manager overrides."""
        if not self._lifecycle:
            return
        for fid, feat in self._catalog.get_prewarm_targets().items():
            service_name = fid.replace("-", "_")
            if feat.ttl_idle != 300:  # Only override non-default
                self._lifecycle.set_idle_timeout_floor(service_name, feat.ttl_idle)
        log.debug("Synced catalog TTLs to lifecycle manager")

    async def predict_and_warm(
        self,
        request_id: str,
        query: str,
        *,
        session_id: str | None = None,
        file_names: list[str] | None = None,
        running_containers: set[str] | None = None,
        gpu_pressure: float = 0.0,
    ) -> PrewarmState:
        """Run the full prediction pipeline and fire container prewarms.

        This method is designed to be called as a fire-and-forget background task
        immediately when a request arrives, before any LLM processing begins.

        Args:
            request_id: Unique request identifier.
            query: The user's raw query text.
            session_id: Optional session ID (for mark_used correlation).
            file_names: Optional list of attached file names.
            running_containers: Set of currently running container/feature names.
            gpu_pressure: Current GPU VRAM pressure (0.0-1.0).

        Returns:
            PrewarmState tracking this request's prewarm decisions.
        """
        start = time.time()
        state = PrewarmState(request_id=request_id)
        state.timestamps.request_received = start
        self._active_states[request_id] = state
        # Also index by session_id for mark_used correlation from pipeline nodes
        if session_id:
            self._active_states[session_id] = state

        if not self._cfg.enabled:
            return state

        # Lazy initialization (embedding pre-computation)
        if not self._initialized:
            await self.initialize()

        # Reload catalog if changed
        self._catalog.reload_if_changed()
        self._metrics.total_requests += 1

        try:
            # 1. Extract signals (sub-ms)
            signals = self._signal_extractor.extract(query, file_names=file_names)

            # 1b. Direct Answer Guard — skip all routing if query needs no tools
            if self._guard.is_direct_answer(query, signals):
                state.latency_ms = (time.time() - start) * 1000
                state.timestamps.pipeline_done = time.time()
                self._metrics._guard_blocks += 1
                log.debug("Guard blocked prewarm for: %s", _log_value(query, 60))
                return state

            # 2. Level 0: Rule router (sub-ms)
            rule_results = self._rule_router.route(signals, query=query)
            rule_results = [
                r for r in rule_results
                if self._catalog.is_prewarm_target(r.feature_id)
            ]
            state.predictions.extend(rule_results)
            state.timestamps.l0_done = time.time()

            # Check if we have a strong match — start container immediately
            strong_rules = [r for r in rule_results if r.confidence >= self._cfg.high_confidence_threshold]
            if strong_rules:
                state.timestamps.prewarm_requested = time.time()
                # Fire prewarm for strong matches without waiting for L1/L2
                await self._fire_prewarms_for_predictions(
                    strong_rules, state, running_containers=running_containers, gpu_pressure=gpu_pressure,
                )

            # 2b. Identify features blocked by negative gates (low score after penalty)
            gate_blocked = {r.feature_id for r in rule_results
                           if "neg_gate" in r.reason and r.confidence <= 0.20}

            # 3. Level 1: Lightweight CPU router (always, <1ms) + Ollama embedding (optional)
            embedding_results = []
            # L1a: CPU TF-IDF (always available, zero latency)
            lightweight_results = self._lightweight_router.route(query)
            # Filter out features blocked by negative gates at L0
            lightweight_results = [
                r for r in lightweight_results
                if r.feature_id not in gate_blocked
                and self._catalog.is_prewarm_target(r.feature_id)
            ]
            embedding_results.extend(lightweight_results)
            # L1b: Ollama embedding (optional, ~10ms, GPU/network)
            if self._cfg.level1_enabled:
                ollama_results = await self._embedding_router.route(query)
                ollama_results = [
                    r for r in ollama_results
                    if r.feature_id not in gate_blocked
                    and self._catalog.is_prewarm_target(r.feature_id)
                ]
                embedding_results.extend(ollama_results)
            state.predictions.extend(embedding_results)
            state.timestamps.l1_done = time.time()

            # 3.5. Level 1.5: Semantic Router (only if L1 is ambiguous)
            if self._l1_is_ambiguous(embedding_results):
                # Narrow to top candidate features from L1
                candidate_features = [
                    r.feature_id for r in embedding_results[:5]
                    if self._catalog.is_prewarm_target(r.feature_id)
                ]
                semantic_results = self._semantic_router.route(
                    query, candidate_features=candidate_features,
                )
                semantic_results = [
                    r for r in semantic_results
                    if r.feature_id not in gate_blocked
                    and self._catalog.is_prewarm_target(r.feature_id)
                ]
                if semantic_results:
                    # Replace L1 results with higher-quality semantic results
                    embedding_results = semantic_results
                    state.predictions.extend(semantic_results)

            # 4. Level 2: Micro-classifier (only if needed)
            classifier_results: list = []
            if self._should_run_classifier(rule_results, embedding_results):
                state.timestamps.l2_started = time.time()
                # Narrow candidates to features without strong rule matches
                already_strong = {r.feature_id for r in strong_rules}
                candidates = [
                    fid for fid in self._catalog.prewarm_target_ids
                    if fid not in already_strong
                ]
                classifier_results = await self._micro_classifier.classify(query, candidates)
                state.predictions.extend(classifier_results)
                state.timestamps.l2_done = time.time()

            # 5. Aggregate scores
            candidates = self._aggregator.aggregate(
                rule_results,
                embedding_results,
                classifier_results,
                running_containers=running_containers,
                gpu_pressure=gpu_pressure,
            )

            # 6. Apply policy
            actions = self._policy.decide(candidates, gpu_pressure=gpu_pressure)
            state.actions = actions

            # 7. Fire container starts for actions not already started
            prewarm_actions = [a for a in actions if a.action == "prewarm_now"]
            already_started = state.containers_started.copy()
            new_actions = [a for a in prewarm_actions if a.feature_id not in already_started]

            if new_actions and self._lifecycle:
                await self._start_containers(new_actions, state)

        except Exception as e:
            log.warning("Prewarm prediction failed for %s (non-fatal): %s", request_id, e)

        state.latency_ms = (time.time() - start) * 1000
        state.timestamps.pipeline_done = time.time()
        log.debug(
            "Prewarm prediction for %s: %.1fms, started=%s",
            request_id, state.latency_ms, state.containers_started,
        )
        return state

    def _l1_is_ambiguous(self, embedding_results: list) -> bool:
        """Check if L1 results are ambiguous enough to warrant L1.5 semantic routing."""
        if len(embedding_results) < 2:
            return False
        top_two = sorted(embedding_results, key=lambda r: r.confidence, reverse=True)[:2]
        gap = top_two[0].confidence - top_two[1].confidence
        return gap < self._cfg.level2_ambiguity_gap

    def _should_run_classifier(self, rule_results: list, embedding_results: list) -> bool:
        """Determine if Level 2 classifier should be invoked."""
        if not self._cfg.level2_enabled:
            return False

        # If rules already gave a strong match, skip classifier
        if any(r.confidence >= self._cfg.high_confidence_threshold for r in rule_results):
            return False

        # If embedding results are ambiguous (top-2 scores within gap)
        if len(embedding_results) >= 2:
            top_two = sorted(embedding_results, key=lambda r: r.confidence, reverse=True)[:2]
            gap = top_two[0].confidence - top_two[1].confidence
            if gap < self._cfg.level2_ambiguity_gap:
                return True

        # If no useful results from L0 + L1
        all_results = rule_results + embedding_results
        if not all_results or max(r.confidence for r in all_results) < self._cfg.medium_confidence_threshold:
            return True

        # Veto check: if a low-accuracy feature would be prewarmed, use L2 to confirm
        for pred in embedding_results:
            feat = self._catalog.get(pred.feature_id)
            if not feat or feat.startup_cost == "low":
                continue
            accuracy = self._learning.get_accuracy(pred.feature_id)
            feat_threshold = feat.prewarm_threshold if hasattr(feat, "prewarm_threshold") else self._cfg.high_confidence_threshold
            if accuracy is not None and accuracy < 0.50 and pred.confidence >= feat_threshold * 0.8:
                return True

        return False

    async def _fire_prewarms_for_predictions(
        self,
        predictions: list,
        state: PrewarmState,
        *,
        running_containers: set[str] | None = None,
        gpu_pressure: float = 0.0,
    ) -> None:
        """Immediately fire prewarms for high-confidence predictions."""
        if not self._lifecycle:
            return

        from orchestrator.prewarming.state import PrewarmAction

        running = running_containers or set()
        for pred in predictions:
            feat = self._catalog.get(pred.feature_id)
            if not feat:
                continue
            if not self._catalog.is_prewarm_target(pred.feature_id):
                continue
            if pred.feature_id in running or feat.container_name in running:
                continue
            if pred.feature_id in state.containers_started:
                continue

            # Quick policy check for GPU
            if feat.uses_gpu and (gpu_pressure > 0.8 or self._cfg.max_gpu_prewarm_per_request == 0):
                continue

            state.actions.append(PrewarmAction(
                feature_id=pred.feature_id,
                container_name=feat.container_name,
                action="prewarm_now",
                score=pred.confidence,
                priority=feat.priority,
            ))
            await self._start_container(pred.feature_id, feat.container_name, state)

    async def _start_containers(self, actions: list, state: PrewarmState) -> None:
        """Start multiple containers concurrently."""
        tasks = []
        for action in actions:
            if action.feature_id not in state.containers_started:
                tasks.append(self._start_container(action.feature_id, action.container_name, state))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_container(self, feature_id: str, container_name: str, state: PrewarmState) -> None:
        """Start a single container via lifecycle manager (non-blocking, no health wait)."""
        if not self._lifecycle:
            return
        try:
            if not state.timestamps.container_start_called:
                state.timestamps.container_start_called = time.time()
            service_name = feature_id.replace("-", "_")
            # Use kick_start (fire-and-forget, no health wait) for true parallel startup
            started = await asyncio.to_thread(self._lifecycle.kick_start, service_name)
            if started:
                state.containers_started.add(feature_id)
                log.info("Prewarm kick-started container: %s", container_name)
            else:
                # Fallback to ensure_running if kick_start fails (e.g. container doesn't exist)
                await asyncio.to_thread(self._lifecycle.ensure_running, service_name)
                state.containers_started.add(feature_id)
                log.info("Prewarm started container (full): %s", container_name)
        except Exception as e:
            log.debug("Failed to prewarm %s: %s", container_name, e)

    def mark_used(self, request_id: str, feature_id: str) -> None:
        """Mark a feature as actually used by the pipeline (for hit tracking)."""
        state = self._active_states.get(request_id)
        if state:
            state.mark_used(feature_id)
        self._aggregator.record_usage(feature_id)

    async def cancel_unused(self, request_id: str, *, delay_seconds: float = 5.0) -> None:
        """Stop GPU containers that were prewarmed but not used after a delay.

        Called after streaming completes. Only stops expensive (GPU) containers;
        cheap CPU containers are left for the TTL reaper.

        Must be called BEFORE cleanup() since it reads from _active_states.
        Captures state snapshot immediately, then waits before stopping.
        """
        state = self._active_states.get(request_id)
        if not state or not self._lifecycle:
            return

        # Snapshot unused GPU containers now (before cleanup removes state)
        unused_gpu = []
        for fid in state.containers_started - state.containers_used:
            feat = self._catalog.get(fid)
            if feat and feat.uses_gpu:
                unused_gpu.append(fid)

        if not unused_gpu:
            return

        # Wait briefly in case a late dispatch uses it
        await asyncio.sleep(delay_seconds)

        # Re-check state (might still be tracked if cleanup hasn't run yet)
        for fid in unused_gpu:
            if fid in state.containers_used:
                continue  # Was used during the delay
            try:
                service_name = fid.replace("-", "_")
                await asyncio.to_thread(self._lifecycle.stop_service, service_name)
                log.info("Cancelled unused GPU container: %s", fid)
            except Exception as e:
                log.debug("Failed to cancel %s: %s", fid, e)

    def cleanup(self, request_id: str) -> PrewarmState | None:
        """Clean up state for a completed request. Returns final state for metrics."""
        state = self._active_states.pop(request_id, None)
        if state:
            # Record cumulative metrics before removing state
            self._metrics.record_request(state, count_request=False)
            # Feed learning loop with hit/miss outcomes
            for fid in state.containers_started:
                self._learning.record_outcome(fid, was_used=fid in state.containers_used)
            # Also remove session_id alias if present
            keys_to_remove = [k for k, v in self._active_states.items() if v is state]
            for k in keys_to_remove:
                self._active_states.pop(k, None)
            if state.unused_containers:
                log.debug(
                    "Request %s: unused prewarms=%s (hit_rate=%.0f%%)",
                    request_id, state.unused_containers, state.hit_rate * 100,
                )
        return state

    def get_status(self) -> dict:
        """Return current engine status for the /prewarm/status endpoint."""
        return {
            "enabled": self._cfg.enabled,
            "initialized": self._initialized,
            "active_requests": len(self._active_states),
            "catalog_features": len(self._catalog.get_all()),
            "metrics": self._metrics.to_dict(),
            "learning": self._learning.get_status(),
            "config": {
                "max_prewarm_per_request": self._cfg.max_prewarm_per_request,
                "max_gpu_prewarm_per_request": self._cfg.max_gpu_prewarm_per_request,
                "high_confidence_threshold": self._cfg.high_confidence_threshold,
                "medium_confidence_threshold": self._cfg.medium_confidence_threshold,
                "classifier_model": self._cfg.classifier_model,
                "embedding_model": self._cfg.embedding_model,
                "level1_enabled": self._cfg.level1_enabled,
                "level2_enabled": self._cfg.level2_enabled,
            },
        }
