"""Generic planning for multi-agent deliberation rounds.

The planner owns only orchestration choices: priority, cost, deduplication and
when to ask for more evidence. It never executes agents or encodes service
semantics.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from orchestrator.agentic.contracts import AgenticParallelPlan, AgentQuestion, CritiqueDecision, ParallelAgentSpec

_PROMPT_DIR = Path(__file__).resolve().parent / "prompt"
_PROMPT_CACHE: dict[str, str] = {}


def _prompt(name: str) -> str:
    text = _PROMPT_CACHE.get(name)
    if text is None:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()
        _PROMPT_CACHE[name] = text
    return text


_PRIORITY_RANK = {
    "critical": 400,
    "high": 300,
    "normal": 200,
    "medium": 200,
    "low": 100,
}
_ROLE_PRIORITY = {
    "evidence": 360,
    "evidence_request": 360,
    "validate": 330,
    "validation": 330,
    "verifier": 330,
    "critic": 300,
    "critique": 300,
    "review": 280,
    "revision": 260,
    "synthesis": 220,
    "draft": 180,
}
_COST_UNITS = {
    "tiny": 1,
    "low": 1,
    "normal": 2,
    "medium": 2,
    "high": 4,
    "expensive": 6,
}


@dataclass(frozen=True)
class PlannedQuestion:
    question: AgentQuestion
    priority_rank: int
    estimated_cost: int
    role: str
    reason: str


@dataclass(frozen=True)
class CapabilityCandidate:
    name: str
    kind: str
    capabilities: tuple[str, ...] = ()
    description: str = ""
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityRequirementResolution:
    requirement_id: str
    role: str
    required_capabilities: tuple[str, ...]
    selected_agent: str | None = None
    matched_capabilities: tuple[str, ...] = ()
    reason: str = ""

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "role": self.role,
            "required_capabilities": list(self.required_capabilities),
            "selected_agent": self.selected_agent,
            "matched_capabilities": list(self.matched_capabilities),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CapabilityExpandedPlan:
    plan: AgenticParallelPlan
    selected: tuple[CapabilityRequirementResolution, ...] = ()
    deferred: tuple[CapabilityRequirementResolution, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def expanded(self) -> bool:
        return bool(self.selected or self.deferred)

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "planner": "agentic.capability_catalog_planner",
            "plan_id": self.plan.plan_id,
            "selected": [item.to_event_payload() for item in self.selected],
            "deferred": [item.to_event_payload() for item in self.deferred],
            "selected_agents": [item.selected_agent for item in self.selected if item.selected_agent],
            "deferred_requirement_ids": [item.requirement_id for item in self.deferred],
            "participant_count": len(self.plan.participants),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class DeliberationRoundPlan:
    selected: tuple[PlannedQuestion, ...] = ()
    deferred: tuple[PlannedQuestion, ...] = ()
    generated: tuple[PlannedQuestion, ...] = ()
    budget_remaining: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def selected_questions(self) -> list[AgentQuestion]:
        return [item.question for item in self.selected]

    @property
    def generated_question_ids(self) -> list[str]:
        return [item.question.question_id for item in self.generated]

    @property
    def deferred_question_ids(self) -> list[str]:
        return [item.question.question_id for item in self.deferred]

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "planner": "agentic.deliberation_planner",
            "selected": [_planned_payload(item) for item in self.selected],
            "deferred": [_planned_payload(item) for item in self.deferred],
            "generated_question_ids": self.generated_question_ids,
            "deferred_question_ids": self.deferred_question_ids,
            "budget_remaining": self.budget_remaining,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class InitialDeliberationPlan:
    questions: tuple[PlannedQuestion, ...] = ()
    deferred: tuple[PlannedQuestion, ...] = ()
    source_plan_id: str = ""
    budget_remaining: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def selected_questions(self) -> list[AgentQuestion]:
        return [item.question for item in self.questions]

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "planner": "agentic.initial_deliberation_planner",
            "source_plan_id": self.source_plan_id,
            "questions": [_planned_payload(item) for item in self.questions],
            "deferred": [_planned_payload(item) for item in self.deferred],
            "question_ids": [item.question.question_id for item in self.questions],
            "deferred_question_ids": [item.question.question_id for item in self.deferred],
            "budget_remaining": self.budget_remaining,
            "metadata": self.metadata,
        }


def build_autonomous_initial_deliberation_plan(
    *,
    task_id: str,
    trace_id: str,
    goal: str,
    task_source: str = "",
    task_mode: str = "",
    task_metadata: dict[str, Any] | None = None,
    final_state: dict[str, Any] | None = None,
) -> AgenticParallelPlan | None:
    """Build a first deliberation plan when no agent supplied one.

    This is orchestration-only planning: it emits declarative capability
    requirements and lets the catalog resolver pick owner-published agents.
    """
    metadata = dict(task_metadata or {})
    state = dict(final_state or {})
    if not _should_autoplan_deliberation(
        goal=goal,
        task_source=task_source,
        task_mode=task_mode,
        task_metadata=metadata,
        final_state=state,
    ):
        return None

    evidence_refs = _autonomous_evidence_refs(task_id=task_id, task_metadata=metadata, final_state=state)
    source_reason = _autonomous_plan_reason(
        goal=goal,
        task_source=task_source,
        task_mode=task_mode,
        task_metadata=metadata,
        final_state=state,
    )
    requirements = _autonomous_capability_requirements(goal=goal, source_reason=source_reason, evidence_refs=evidence_refs)
    return AgenticParallelPlan(
        plan_id=_bounded_id(f"auto-plan:{task_id}:{source_reason}"),
        task_id=task_id,
        trace_id=trace_id,
        goal=_bounded_text(goal, max_length=16000, fallback="Autonomous deliberation task."),
        participants=[],
        max_parallel=5,
        resource_profile={"planner": "agentic.autonomous_initial_deliberation_planner"},
        fallback_policy="partial",
        metadata={
            "planner": "agentic.autonomous_initial_deliberation_planner",
            "autonomous_initial_plan": True,
            "planner_reason": source_reason,
            "source_task_id": task_id,
            "source_task_source": task_source,
            "source_task_mode": task_mode,
            "source_event_type": metadata.get("event_type"),
            "proposal": bool(metadata.get("proposal") or metadata.get("proposal_only")),
            "evidence_refs": evidence_refs,
            "question_budget_units": 5,
            "max_capability_participants": 5,
            "capability_requirements": requirements,
        },
    )


def plan_initial_deliberation_questions(
    plan: AgenticParallelPlan,
    *,
    existing_question_ids: set[str] | None = None,
) -> InitialDeliberationPlan:
    """Create first-round questions from a declarative role/capability plan."""
    existing = existing_question_ids or set()
    question_budget = _initial_question_budget(plan)
    candidates = [
        _question_from_participant(plan, participant=participant, index=index)
        for index, participant in enumerate(plan.participants)
    ]
    planned = [_planned_initial_question(question, plan, generated=False) for question in candidates]
    depths = _dependency_depths(plan.participants)
    planned = [
        _with_dependency_depth(item, depths.get(_participant_key_from_question(item.question), 0))
        for item in planned
    ]
    selected: list[PlannedQuestion] = []
    deferred: list[PlannedQuestion] = []
    consumed = 0
    seen_keys: set[tuple[str, str]] = set()

    for item in sorted(
        planned,
        key=lambda entry: (
            int(entry.question.metadata.get("dependency_depth") or 0),
            -entry.priority_rank,
            entry.estimated_cost,
            entry.question.question_id,
        ),
    ):
        key = _question_key(item.question)
        if item.question.question_id in existing or key in seen_keys:
            deferred.append(_replace_reason(item, "duplicate"))
            continue
        seen_keys.add(key)
        if consumed + item.estimated_cost > question_budget:
            deferred.append(_replace_reason(item, "budget"))
            continue
        selected.append(item)
        consumed += item.estimated_cost

    return InitialDeliberationPlan(
        questions=tuple(selected),
        deferred=tuple(deferred),
        source_plan_id=plan.plan_id,
        budget_remaining=max(0, question_budget - consumed),
        metadata={
            "goal_preview": plan.goal[:500],
            "participant_count": len(plan.participants),
            "selected_count": len(selected),
            "deferred_count": len(deferred),
            "question_budget": question_budget,
            "planner": plan.metadata.get("planner"),
            "planner_reason": plan.metadata.get("planner_reason"),
            "autonomous_initial_plan": bool(plan.metadata.get("autonomous_initial_plan")),
            "evidence_refs": _string_list(plan.metadata.get("evidence_refs")),
        },
    )


def expand_parallel_plan_from_capability_catalog(
    plan: AgenticParallelPlan,
    *,
    candidates: Sequence[CapabilityCandidate],
) -> CapabilityExpandedPlan:
    """Add participants by resolving declarative capability requirements.

    Requirements live in ``plan.metadata.capability_requirements``. This keeps
    agent selection declarative and catalog-driven: the planner matches roles to
    advertised capabilities, but does not execute agents or know service
    internals.
    """
    requirements = _capability_requirements(plan)
    if not requirements:
        return CapabilityExpandedPlan(plan=plan, metadata={"requirement_count": 0})

    participant_names = {participant.agent_name.strip().lower() for participant in plan.participants}
    participants = list(plan.participants)
    selected: list[CapabilityRequirementResolution] = []
    deferred: list[CapabilityRequirementResolution] = []
    max_added = _max_capability_participants(plan)
    added = 0

    for index, requirement in enumerate(requirements):
        requirement_id = _capability_requirement_id(requirement, index)
        role = _capability_requirement_role(requirement)
        required = tuple(_normalized_string_list(requirement.get("capabilities") or requirement.get("capability")))
        if not required:
            deferred.append(
                CapabilityRequirementResolution(
                    requirement_id=requirement_id,
                    role=role,
                    required_capabilities=(),
                    reason="missing_capabilities",
                )
            )
            continue
        if added >= max_added:
            deferred.append(
                CapabilityRequirementResolution(
                    requirement_id=requirement_id,
                    role=role,
                    required_capabilities=required,
                    reason="participant_budget",
                )
            )
            continue
        matches = _matching_capability_candidates(requirement, candidates, existing_names=participant_names)
        if not matches:
            deferred.append(
                CapabilityRequirementResolution(
                    requirement_id=requirement_id,
                    role=role,
                    required_capabilities=required,
                    reason="no_catalog_match",
                )
            )
            continue
        candidate, matched_capabilities = matches[0]
        participant_names.add(candidate.name.strip().lower())
        participant = _participant_from_capability_requirement(
            plan=plan,
            requirement=requirement,
            requirement_id=requirement_id,
            candidate=candidate,
            matched_capabilities=matched_capabilities,
        )
        participants.append(participant)
        selected.append(
            CapabilityRequirementResolution(
                requirement_id=requirement_id,
                role=role,
                required_capabilities=required,
                selected_agent=candidate.name,
                matched_capabilities=tuple(matched_capabilities),
                reason="matched",
            )
        )
        added += 1

    expanded_plan = plan.model_copy(
        update={
            "participants": participants,
            "metadata": {
                **dict(plan.metadata or {}),
                "capability_catalog_expanded": bool(selected),
                "capability_requirements_count": len(requirements),
                "capability_selected_agents": [item.selected_agent for item in selected if item.selected_agent],
                "capability_deferred_requirement_ids": [item.requirement_id for item in deferred],
            },
        }
    )
    return CapabilityExpandedPlan(
        plan=expanded_plan,
        selected=tuple(selected),
        deferred=tuple(deferred),
        metadata={
            "requirement_count": len(requirements),
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "deferred_count": len(deferred),
            "max_added_participants": max_added,
        },
    )


def plan_revision_questions_from_critiques(
    *,
    task_id: str,
    trace_id: str,
    critiques: Sequence[CritiqueDecision],
    existing_question_ids: set[str] | None = None,
) -> InitialDeliberationPlan:
    """Create revision questions from critique contracts with explicit owners."""
    existing = existing_question_ids or set()
    planned: list[PlannedQuestion] = []
    deferred: list[PlannedQuestion] = []
    for index, critique in enumerate(critiques):
        question = _question_from_critique(task_id=task_id, trace_id=trace_id, critique=critique, index=index)
        if question is None:
            deferred.append(_deferred_revision_placeholder(task_id=task_id, trace_id=trace_id, critique=critique, index=index))
            continue
        item = _planned_question(question, generated=False)
        if question.question_id in existing:
            deferred.append(_replace_reason(item, "duplicate"))
            continue
        planned.append(item)
    return InitialDeliberationPlan(
        questions=tuple(planned),
        deferred=tuple(deferred),
        source_plan_id="critique_decisions",
        budget_remaining=0,
        metadata={
            "critique_count": len(critiques),
            "selected_count": len(planned),
            "deferred_count": len(deferred),
        },
    )


def plan_next_deliberation_round(
    *,
    task_id: str,
    trace_id: str,
    round_index: int,
    max_rounds: int,
    answered_total: int,
    max_questions: int,
    current_questions: Sequence[AgentQuestion],
    outcomes: Sequence[Any],
    candidate_questions: Sequence[AgentQuestion],
    seen_question_ids: set[str],
) -> DeliberationRoundPlan:
    """Select follow-up questions for the next round.

    The selection is resource-aware in question-cost units, not service-aware:
    it consumes only metadata supplied by agents/contracts.
    """
    remaining = max(0, max_questions - answered_total)
    can_continue = remaining > 0 and (round_index + 1) < max_rounds
    generated: list[PlannedQuestion] = []

    candidates = list(candidate_questions)
    if can_continue and not candidates and _needs_more_evidence(outcomes):
        generated_question = _evidence_request_question(
            task_id=task_id,
            trace_id=trace_id,
            round_index=round_index,
            current_questions=current_questions,
            outcomes=outcomes,
            seen_question_ids=seen_question_ids,
        )
        if generated_question is not None:
            candidates.append(generated_question)

    planned = [_planned_question(question, generated=False) for question in candidates]
    generated_ids = {
        question.question_id
        for question in candidates
        if question.metadata.get("planner_reason") == "evidence_required"
    }
    generated = [item for item in planned if item.question.question_id in generated_ids]
    selected: list[PlannedQuestion] = []
    deferred: list[PlannedQuestion] = []
    consumed = 0
    seen_keys: set[tuple[str, str]] = set()

    for item in sorted(planned, key=lambda entry: (-entry.priority_rank, entry.estimated_cost, entry.question.question_id)):
        key = _question_key(item.question)
        if item.question.question_id in seen_question_ids or key in seen_keys:
            deferred.append(_replace_reason(item, "duplicate"))
            continue
        seen_keys.add(key)
        if not can_continue:
            deferred.append(_replace_reason(item, "no_next_round"))
            continue
        if consumed + item.estimated_cost > remaining:
            deferred.append(_replace_reason(item, "budget"))
            continue
        selected.append(item)
        consumed += item.estimated_cost

    return DeliberationRoundPlan(
        selected=tuple(selected),
        deferred=tuple(deferred),
        generated=tuple(generated),
        budget_remaining=max(0, remaining - consumed),
        metadata={
            "round_index": round_index,
            "max_rounds": max_rounds,
            "answered_total": answered_total,
            "max_questions": max_questions,
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "deferred_count": len(deferred),
        },
    )


def _should_autoplan_deliberation(
    *,
    goal: str,
    task_source: str,
    task_mode: str,
    task_metadata: dict[str, Any],
    final_state: dict[str, Any],
) -> bool:
    if task_metadata.get("disable_auto_deliberation") is True:
        return False
    if _has_structured_deliberation_artifacts(final_state):
        return False
    if isinstance(final_state.get("agent_decision"), dict):
        return False
    if task_metadata.get("auto_deliberation") is True or task_metadata.get("requires_deliberation") is True:
        return True
    if task_metadata.get("proposal") or task_metadata.get("proposal_only"):
        return True
    if task_source == "agentic.event_loop":
        return True
    if str(task_mode).strip().lower() == "autonomous":
        return True
    return _goal_looks_complex(goal)


def _has_structured_deliberation_artifacts(final_state: dict[str, Any]) -> bool:
    keys = (
        "agentic_initial_deliberation_plan",
        "agentic_deliberation_plan",
        "initial_deliberation_plan",
        "agent_questions",
        "agent_messages",
        "agent_answers",
        "validation_votes",
        "critique_decisions",
        "consensus_decisions",
    )
    return any(bool(final_state.get(key)) for key in keys)


def _goal_looks_complex(goal: str) -> bool:
    text = re.sub(r"\s+", " ", goal).strip().lower()
    if len(text) >= 180:
        return True
    if "\n" in goal or "- " in goal or "* " in goal:
        return True
    terms = {
        "analyse",
        "analyze",
        "audit",
        "debug",
        "degraded",
        "diagnose",
        "evaluate",
        "implement",
        "incident",
        "investigate",
        "migration",
        "plan",
        "repair",
        "review",
        "rollback",
        "root cause",
        "analisar",
        "auditar",
        "avaliar",
        "corrigir",
        "degradado",
        "diagnosticar",
        "fase",
        "implementar",
        "incidente",
        "investigar",
        "migracao",
        "migração",
        "planear",
        "planejar",
        "rever",
        "resolver",
    }
    return any(term in text for term in terms)


def _autonomous_plan_reason(
    *,
    goal: str,
    task_source: str,
    task_mode: str,
    task_metadata: dict[str, Any],
    final_state: dict[str, Any],
) -> str:
    if task_metadata.get("auto_deliberation") is True:
        return "metadata_auto_deliberation"
    if task_metadata.get("requires_deliberation") is True:
        return "metadata_requires_deliberation"
    if task_metadata.get("proposal") or task_metadata.get("proposal_only"):
        return "event_loop_proposal"
    if task_source == "agentic.event_loop":
        return "event_loop_task"
    if str(task_mode).strip().lower() == "autonomous":
        return "autonomous_mode"
    if final_state.get("response"):
        return "goal_complexity_after_response"
    if _goal_looks_complex(goal):
        return "goal_complexity"
    return "autonomous_planning"


def _autonomous_evidence_refs(
    *,
    task_id: str,
    task_metadata: dict[str, Any],
    final_state: dict[str, Any],
) -> list[str]:
    refs: list[str] = []

    def append(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in refs:
            refs.append(text[:1000])

    append(f"task:{task_id}")
    for ref in task_metadata.get("evidence_refs") or []:
        append(ref)
    signals = task_metadata.get("signals")
    if isinstance(signals, dict):
        for item in signals.get("recent_ai_local_events") or []:
            if not isinstance(item, dict):
                continue
            append(item.get("evidence_ref"))
            event_id = item.get("event_id")
            if event_id:
                append(f"ai_local_event:{event_id}")
        for key in ("degraded_events", "resource_pressure_events"):
            for event_id in signals.get(key) or []:
                append(f"ai_local_event:{event_id}")
    for key in ("agentic_memory_refs", "memory_refs"):
        for item in final_state.get(key) or []:
            if isinstance(item, dict):
                append(item.get("memory_id") and f"memory:{item.get('memory_id')}")
            else:
                append(item)
    return refs[:25]


def _autonomous_capability_requirements(
    *,
    goal: str,
    source_reason: str,
    evidence_refs: Sequence[str],
) -> list[dict[str, Any]]:
    goal_preview = _bounded_text(goal, max_length=1200, fallback="Autonomous deliberation task.")
    base_metadata = {
        "planner": "agentic.autonomous_initial_deliberation_planner",
        "autonomous_initial_plan": True,
        "source_reason": source_reason,
        "evidence_refs": list(evidence_refs),
    }
    return [
        {
            "requirement_id": "autonomous:planner",
            "role": "planner",
            "capabilities": ["planning"],
            "query": _prompt("autonomous_planner.md").format(goal=goal_preview),
            "priority": "high",
            "cost_units": 1,
            "metadata": {**base_metadata, "role": "planner"},
        },
        {
            "requirement_id": "autonomous:domain_specialist",
            "role": "domain_specialist",
            "capabilities": ["direct_response"],
            "query": _prompt("autonomous_domain_specialist.md").format(goal=goal_preview),
            "priority": "normal",
            "cost_units": 1,
            "depends_on": ["planner"],
            "metadata": {**base_metadata, "role": "domain_specialist"},
        },
        {
            "requirement_id": "autonomous:memory_curator",
            "role": "memory_curator",
            "capabilities": ["memory_retrieval"],
            "query": _prompt("autonomous_memory_curator.md").format(goal=goal_preview),
            "priority": "normal",
            "cost_units": 1,
            "depends_on": ["planner"],
            "required": False,
            "metadata": {
                **base_metadata,
                "role": "memory_curator",
                "memory_gap_declaration": True,
                "retrieval_owner_boundary": "episodic=agentic_ledger; semantic=rag/research",
            },
        },
        {
            "requirement_id": "autonomous:risk_reviewer",
            "role": "risk_reviewer",
            "capabilities": ["critique", "evaluation"],
            "match": "any",
            "query": _prompt("autonomous_risk_reviewer.md").format(goal=goal_preview),
            "priority": "high",
            "cost_units": 1,
            "depends_on": ["planner"],
            "metadata": {**base_metadata, "role": "risk_reviewer"},
        },
        {
            "requirement_id": "autonomous:synthesis",
            "role": "synthesis",
            "capabilities": ["synthesis"],
            "query": _prompt("autonomous_synthesis.md").format(goal=goal_preview),
            "priority": "normal",
            "cost_units": 1,
            "depends_on": ["planner", "risk_reviewer", "domain_specialist"],
            "metadata": {**base_metadata, "role": "synthesis"},
        },
    ]


def _initial_question_budget(plan: AgenticParallelPlan) -> int:
    metadata = plan.metadata if isinstance(plan.metadata, dict) else {}
    resource_profile = plan.resource_profile if isinstance(plan.resource_profile, dict) else {}
    for source in (metadata, resource_profile):
        for key in ("question_budget_units", "deliberation_budget_units", "max_initial_question_cost"):
            if source.get(key) is not None:
                return max(1, min(24, int(_safe_float(source[key]))))
    return max(1, min(12, len(plan.participants) or 1))


def _capability_requirements(plan: AgenticParallelPlan) -> list[dict[str, Any]]:
    metadata = plan.metadata if isinstance(plan.metadata, dict) else {}
    raw = metadata.get("capability_requirements") or metadata.get("required_capabilities") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    requirements: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            requirements.append(
                {
                    "requirement_id": f"capability:{_slug(item)}:{index}",
                    "role": item,
                    "capabilities": [item],
                }
            )
        elif isinstance(item, dict):
            requirements.append(dict(item))
    return requirements


def _max_capability_participants(plan: AgenticParallelPlan) -> int:
    metadata = plan.metadata if isinstance(plan.metadata, dict) else {}
    value = metadata.get("max_capability_participants") or metadata.get("capability_participant_budget")
    if value is None:
        return max(1, min(12, plan.max_parallel or 1))
    return max(1, min(12, int(_safe_float(value))))


def _capability_requirement_id(requirement: dict[str, Any], index: int) -> str:
    value = requirement.get("requirement_id") or requirement.get("id") or requirement.get("role") or f"requirement-{index}"
    return _bounded_id(str(value))


def _capability_requirement_role(requirement: dict[str, Any]) -> str:
    role = str(requirement.get("role") or requirement.get("deliberation_role") or "").strip()
    if role:
        return role
    capabilities = _normalized_string_list(requirement.get("capabilities") or requirement.get("capability"))
    return capabilities[0] if capabilities else "capability"


def _matching_capability_candidates(
    requirement: dict[str, Any],
    candidates: Sequence[CapabilityCandidate],
    *,
    existing_names: set[str],
) -> list[tuple[CapabilityCandidate, tuple[str, ...]]]:
    required = tuple(_normalized_string_list(requirement.get("capabilities") or requirement.get("capability")))
    explicit_agent = str(requirement.get("agent_name") or requirement.get("agent") or "").strip().lower()
    match_mode = str(requirement.get("match") or "any").strip().lower()
    wanted_kind = str(requirement.get("kind") or "agent").strip().lower()
    matches: list[tuple[int, CapabilityCandidate, tuple[str, ...]]] = []
    required_set = set(required)
    for candidate in candidates:
        candidate_name = candidate.name.strip().lower()
        if candidate_name in existing_names:
            continue
        candidate_kind = candidate.kind.strip().lower()
        if wanted_kind and candidate_kind != wanted_kind:
            continue
        available = {capability.strip().lower() for capability in candidate.capabilities}
        matched = tuple(sorted(required_set & available))
        if explicit_agent and candidate_name != explicit_agent:
            continue
        if explicit_agent and candidate_name == explicit_agent:
            score = 1000 + len(matched) * 100
        elif match_mode == "all":
            if not required_set or not required_set.issubset(available):
                continue
            score = len(matched) * 100
        else:
            if not matched:
                continue
            score = len(matched) * 100
        role = _capability_requirement_role(requirement).strip().lower()
        if role and role in available:
            score += 25
        timeout = candidate.timeout_seconds or 0.0
        score -= int(timeout // 10)
        matches.append((score, candidate, matched))
    matches.sort(key=lambda item: (-item[0], item[1].name))
    return [(candidate, matched) for _score, candidate, matched in matches]


def _participant_from_capability_requirement(
    *,
    plan: AgenticParallelPlan,
    requirement: dict[str, Any],
    requirement_id: str,
    candidate: CapabilityCandidate,
    matched_capabilities: Sequence[str],
) -> ParallelAgentSpec:
    raw_metadata = requirement.get("metadata") or {}
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    role = _bounded_role(_capability_requirement_role(requirement))
    metadata.setdefault("planner", "agentic.capability_catalog_planner")
    metadata.setdefault("planner_reason", "capability_requirement")
    metadata.setdefault("capability_requirement_id", requirement_id)
    metadata.setdefault("required_capabilities", _normalized_string_list(requirement.get("capabilities") or requirement.get("capability")))
    metadata.setdefault("matched_capabilities", list(matched_capabilities))
    metadata.setdefault("capabilities", list(candidate.capabilities))
    metadata.setdefault("kind", candidate.kind)
    metadata.setdefault("description", candidate.description)
    metadata.setdefault("role", role)
    metadata.setdefault("priority", requirement.get("priority") or _role_default_priority(role))
    if "depends_on" in requirement or "after" in requirement:
        metadata.setdefault("depends_on", _dependency_list(requirement.get("depends_on") or requirement.get("after")))
    if "cost_units" in requirement:
        metadata.setdefault("cost_units", requirement["cost_units"])
    elif "cost_estimate" in requirement:
        metadata.setdefault("cost_estimate", requirement["cost_estimate"])
    else:
        metadata.setdefault("cost_estimate", _role_default_cost(role))
    for key, value in candidate.metadata.items():
        metadata.setdefault(key, value)
    timeout_seconds = requirement.get("timeout_seconds")
    if timeout_seconds is None:
        timeout_seconds = candidate.timeout_seconds
    timeout_value = _positive_float(timeout_seconds)
    budget_tokens = _positive_int(requirement.get("budget_tokens"))
    raw_context = requirement.get("context") or {}
    return ParallelAgentSpec(
        agent_name=candidate.name,
        role=role,
        query=_bounded_text(_capability_requirement_query(plan, requirement=requirement, role=role), max_length=16000),
        context=dict(raw_context) if isinstance(raw_context, dict) else {},
        timeout_seconds=timeout_value,
        budget_tokens=budget_tokens,
        required=bool(requirement.get("required", False)),
        metadata=metadata,
    )


def _capability_requirement_query(plan: AgenticParallelPlan, *, requirement: dict[str, Any], role: str) -> str:
    query = str(requirement.get("query") or requirement.get("question") or "").strip()
    if query:
        return query
    capabilities = ", ".join(_string_list(requirement.get("capabilities") or requirement.get("capability")))
    focus = capabilities or role
    return (
        f"Evaluate the task goal from the `{role}` role using these capabilities: {focus}. "
        f"Return validated facts, uncertainties, and any follow-up questions.\n\nGoal: {plan.goal}"
    )


def _question_from_participant(
    plan: AgenticParallelPlan,
    *,
    participant: ParallelAgentSpec,
    index: int,
) -> AgentQuestion:
    metadata = dict(participant.metadata or {})
    metadata.setdefault("planner", "agentic.initial_deliberation_planner")
    metadata.setdefault("planner_reason", "initial_role")
    metadata.setdefault("source_plan_id", plan.plan_id)
    metadata.setdefault("role", participant.role)
    metadata.setdefault("priority", _role_default_priority(participant.role))
    metadata.setdefault("budget_tokens", participant.budget_tokens)
    metadata.setdefault("timeout_seconds", participant.timeout_seconds)
    if "cost_units" not in metadata and "cost_estimate" not in metadata:
        metadata["cost_estimate"] = _role_default_cost(participant.role)
    depends_on = _dependency_list(metadata.get("depends_on") or metadata.get("after"))
    if depends_on:
        metadata["depends_on"] = depends_on
    return AgentQuestion(
        question_id=_bounded_id(str(metadata.get("question_id") or f"{plan.plan_id}:initial:{index}:{_slug(participant.agent_name)}")),
        task_id=plan.task_id,
        trace_id=plan.trace_id,
        from_agent=str(plan.metadata.get("planner_agent") or "agentic.initial_planner"),
        to_agent=participant.agent_name,
        question=_bounded_question(participant.query),
        round_id=_bounded_id(str(plan.metadata.get("round_id") or f"{plan.plan_id}:initial")),
        evidence_refs=_string_list(plan.metadata.get("evidence_refs")) + _string_list(metadata.get("evidence_refs")),
        metadata=metadata,
    )


def _question_from_critique(
    *,
    task_id: str,
    trace_id: str,
    critique: CritiqueDecision,
    index: int,
) -> AgentQuestion | None:
    metadata = dict(critique.metadata or {})
    revision_agent = str(
        metadata.get("revision_agent")
        or metadata.get("target_agent")
        or metadata.get("owner_agent")
        or ""
    ).strip()
    if not revision_agent or not critique.required_revisions:
        return None
    revisions = "\n".join(f"- {item}" for item in critique.required_revisions[:8])
    findings = "\n".join(f"- {item}" for item in critique.findings[:8])
    question = (
        f"Revise `{critique.target_ref}` according to the required revisions.\n\n"
        f"Required revisions:\n{revisions}"
    )
    if findings:
        question = f"{question}\n\nCritique findings:\n{findings}"
    metadata.setdefault("planner", "agentic.initial_deliberation_planner")
    metadata.setdefault("planner_reason", "critique_revision")
    metadata.setdefault("role", "revision")
    metadata.setdefault("priority", "high")
    metadata.setdefault("cost_estimate", "normal")
    metadata.setdefault("critique_id", critique.critique_id)
    return AgentQuestion(
        question_id=_bounded_id(str(metadata.get("question_id") or f"{critique.critique_id}:revision:{index}:{_slug(revision_agent)}")),
        task_id=task_id,
        trace_id=trace_id,
        from_agent=critique.critic,
        to_agent=revision_agent,
        question=_bounded_question(question),
        round_id=_bounded_id(str(metadata.get("round_id") or f"{critique.critique_id}:revision")),
        evidence_refs=_string_list(metadata.get("evidence_refs")),
        metadata=metadata,
    )


def _deferred_revision_placeholder(
    *,
    task_id: str,
    trace_id: str,
    critique: CritiqueDecision,
    index: int,
) -> PlannedQuestion:
    metadata = {
        "planner": "agentic.initial_deliberation_planner",
        "planner_reason": "critique_revision",
        "role": "revision",
        "priority": "high",
        "critique_id": critique.critique_id,
    }
    question = AgentQuestion(
        question_id=_bounded_id(f"{critique.critique_id}:revision_deferred:{index}"),
        task_id=task_id,
        trace_id=trace_id,
        from_agent=critique.critic,
        to_agent="unassigned",
        question=_bounded_question(f"Revision deferred for `{critique.target_ref}` because no revision_agent was provided."),
        evidence_refs=_string_list(critique.metadata.get("evidence_refs") if isinstance(critique.metadata, dict) else None),
        metadata=metadata,
    )
    return PlannedQuestion(question=question, priority_rank=_PRIORITY_RANK["high"], estimated_cost=1, role="revision", reason="missing_revision_agent")


def _planned_initial_question(question: AgentQuestion, plan: AgenticParallelPlan, *, generated: bool) -> PlannedQuestion:
    item = _planned_question(question, generated=generated)
    return PlannedQuestion(
        question=item.question,
        priority_rank=item.priority_rank,
        estimated_cost=item.estimated_cost,
        role=item.role,
        reason=str(question.metadata.get("planner_reason") or f"initial:{plan.plan_id}"),
    )


def _dependency_depths(participants: Sequence[ParallelAgentSpec]) -> dict[str, int]:
    keys_by_ref: dict[str, str] = {}
    deps_by_key: dict[str, list[str]] = {}
    for participant in participants:
        key = _participant_key(participant)
        keys_by_ref[participant.agent_name.strip().lower()] = key
        keys_by_ref[participant.role.strip().lower()] = key
        metadata = participant.metadata if isinstance(participant.metadata, dict) else {}
        deps_by_key[key] = _dependency_list(metadata.get("depends_on") or metadata.get("after"))

    cache: dict[str, int] = {}

    def depth(key: str, trail: set[str] | None = None) -> int:
        if key in cache:
            return cache[key]
        trail = set(trail or set())
        if key in trail:
            return 0
        trail.add(key)
        refs = deps_by_key.get(key) or []
        resolved = [keys_by_ref.get(ref.strip().lower()) for ref in refs]
        parents = [item for item in resolved if item]
        value = 0 if not parents else 1 + max(depth(parent, trail) for parent in parents)
        cache[key] = value
        return value

    return {key: depth(key) for key in deps_by_key}


def _with_dependency_depth(item: PlannedQuestion, depth: int) -> PlannedQuestion:
    metadata = dict(item.question.metadata or {})
    metadata["dependency_depth"] = depth
    question = item.question.model_copy(update={"metadata": metadata})
    return PlannedQuestion(
        question=question,
        priority_rank=item.priority_rank,
        estimated_cost=item.estimated_cost,
        role=item.role,
        reason=item.reason,
    )


def _participant_key(participant: ParallelAgentSpec) -> str:
    return f"{participant.agent_name.strip().lower()}::{participant.role.strip().lower()}"


def _participant_key_from_question(question: AgentQuestion) -> str:
    role = str(question.metadata.get("role") or "").strip().lower()
    return f"{question.to_agent.strip().lower()}::{role}"


def _dependency_list(value: Any) -> list[str]:
    return [item.strip().lower() for item in _string_list(value) if item.strip()]


def _normalized_string_list(value: Any) -> list[str]:
    return [item.strip().lower() for item in _string_list(value) if item.strip()]


def _role_default_priority(role: str) -> str:
    lowered = role.strip().lower()
    if lowered in {"critic", "critique", "validate", "validation", "verifier", "evidence", "evidence_request"}:
        return "high"
    if lowered in {"revision", "review"}:
        return "normal"
    return "normal"


def _role_default_cost(role: str) -> str:
    lowered = role.strip().lower()
    if lowered in {"draft", "synthesis"}:
        return "normal"
    if lowered in {"critic", "critique", "validate", "validation", "verifier", "review", "revision"}:
        return "low"
    return "low"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return slug[:80] or uuid.uuid4().hex[:8]


def _bounded_id(value: str, *, max_length: int = 160) -> str:
    cleaned = value.strip()
    if len(cleaned) <= max_length:
        return cleaned or uuid.uuid4().hex
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    prefix = cleaned[: max_length - len(digest) - 1].rstrip(":_-")
    return f"{prefix}:{digest}"


def _bounded_question(value: str, *, max_length: int = 4000) -> str:
    return _bounded_text(value, max_length=max_length, fallback="Review the referenced artifact.")


def _bounded_text(value: str, *, max_length: int, fallback: str = "") -> str:
    text = value.strip()
    if len(text) <= max_length:
        return text or fallback
    suffix = "\n\n[truncated]"
    return f"{text[: max_length - len(suffix)].rstrip()}{suffix}"


def _bounded_role(value: str, *, max_length: int = 120) -> str:
    role = re.sub(r"\s+", "_", value.strip())
    if len(role) <= max_length:
        return role or "capability"
    digest = hashlib.sha256(role.encode("utf-8")).hexdigest()[:8]
    return f"{role[: max_length - len(digest) - 1].rstrip('_-')}:{digest}"


def _needs_more_evidence(outcomes: Sequence[Any]) -> bool:
    completed = [item for item in outcomes if getattr(item, "status", "") == "completed"]
    if not completed:
        return False
    confidences = [_safe_float(getattr(item, "confidence", 0.0)) for item in completed]
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    agreed = {fact for item in completed for fact in getattr(item, "agreed_facts", ())}
    contested = {fact for item in completed for fact in getattr(item, "contested_facts", ())}
    contradictions = {fact for item in completed for fact in getattr(item, "contradictions", ())}
    return confidence < 0.85 and not agreed and not contested and not contradictions


def _evidence_request_question(
    *,
    task_id: str,
    trace_id: str,
    round_index: int,
    current_questions: Sequence[AgentQuestion],
    outcomes: Sequence[Any],
    seen_question_ids: set[str],
) -> AgentQuestion | None:
    parent = current_questions[0] if current_questions else None
    if parent is None:
        return None
    target = parent.to_agent
    completed = [item for item in outcomes if getattr(item, "status", "") == "completed"]
    if completed:
        weakest = min(completed, key=lambda item: _safe_float(getattr(item, "confidence", 0.0)))
        matching = [question for question in current_questions if question.question_id == getattr(weakest, "question_id", "")]
        if matching:
            target = matching[0].to_agent
            parent = matching[0]
    question_id = f"{parent.question_id}:evidence:{round_index}:{uuid.uuid4().hex[:8]}"
    if question_id in seen_question_ids:
        return None
    return AgentQuestion(
        question_id=question_id,
        task_id=task_id,
        trace_id=trace_id,
        from_agent="agentic.deliberation_planner",
        to_agent=target,
        question=(
            "Provide additional evidence, explicit assumptions, and confidence-limiting factors "
            "for the previous answer. If evidence is insufficient, say what is missing."
        ),
        round_id=parent.round_id,
        evidence_refs=list(parent.evidence_refs),
        metadata={
            "parent_question_id": parent.question_id,
            "planner": "agentic.deliberation_planner",
            "planner_reason": "evidence_required",
            "role": "evidence_request",
            "priority": "high",
            "cost_estimate": "low",
        },
    )


def _planned_question(question: AgentQuestion, *, generated: bool) -> PlannedQuestion:
    metadata = question.metadata if isinstance(question.metadata, dict) else {}
    role = str(metadata.get("role") or metadata.get("deliberation_role") or "").strip().lower()
    priority = str(metadata.get("priority") or metadata.get("deliberation_priority") or "").strip().lower()
    priority_rank = _PRIORITY_RANK.get(priority, 0) or _ROLE_PRIORITY.get(role, _PRIORITY_RANK["normal"])
    estimated_cost = _estimated_cost(metadata)
    reason = str(metadata.get("planner_reason") or ("generated" if generated else "candidate"))
    return PlannedQuestion(
        question=question,
        priority_rank=priority_rank,
        estimated_cost=estimated_cost,
        role=role or "unspecified",
        reason=reason,
    )


def _estimated_cost(metadata: dict[str, Any]) -> int:
    for key in ("cost_units", "estimated_cost_units"):
        value = metadata.get(key)
        if value is not None:
            return max(1, min(12, int(_safe_float(value))))
    for key in ("cost_estimate", "cost", "resource_cost"):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return max(1, min(12, int(value)))
        return _COST_UNITS.get(str(value).strip().lower(), 2)
    budget_tokens = metadata.get("budget_tokens")
    if budget_tokens is not None:
        return max(1, min(8, int(_safe_float(budget_tokens) // 512) or 1))
    return 1


def _question_key(question: AgentQuestion) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", question.question).strip().lower()
    return (question.to_agent.strip().lower(), text)


def _planned_payload(item: PlannedQuestion) -> dict[str, Any]:
    return {
        "question_id": item.question.question_id,
        "to_agent": item.question.to_agent,
        "role": item.role,
        "priority_rank": item.priority_rank,
        "estimated_cost": item.estimated_cost,
        "reason": item.reason,
    }


def _replace_reason(item: PlannedQuestion, reason: str) -> PlannedQuestion:
    return PlannedQuestion(
        question=item.question,
        priority_rank=item.priority_rank,
        estimated_cost=item.estimated_cost,
        role=item.role,
        reason=reason,
    )


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _positive_float(value: Any) -> float | None:
    if value is None:
        return None
    result = _safe_float(value)
    return result if result > 0 else None


def _positive_int(value: Any) -> int | None:
    result = int(_safe_float(value))
    return result if result > 0 else None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    return [str(item).strip() for item in items if str(item).strip()]
