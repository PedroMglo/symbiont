"""Policy routing for agentic escalations.

The router turns L2/L3 escalation signals into safe plans using the declarative
manifest in ``orchestrator/capabilities/escalation_policy.toml``. It does not
execute destructive actions. It records a route, sets a TTL flag for
operators/runtime components, and may create a governed review proposal.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from orchestrator.agentic.policy import check_policy
from orchestrator.agentic.store import AgenticStore, get_agentic_store

POLICY_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "capabilities" / "escalation_policy.toml"


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class EscalationRoutingRule:
    match: str
    domain: str
    equals: str = ""
    values: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, domains: set[str]) -> "EscalationRoutingRule":
        match = str(raw.get("match") or "")
        domain = str(raw.get("domain") or "")
        if match not in {"signal.kind", "reason.contains_any"}:
            raise ValueError(f"Unsupported escalation routing match: {match!r}")
        if domain not in domains:
            raise ValueError(f"Escalation routing rule references unknown domain: {domain!r}")
        equals = str(raw.get("equals") or "")
        values = _string_list(raw.get("values"), field_name=f"routing_rules.{domain}.values")
        if match == "signal.kind" and not equals:
            raise ValueError("signal.kind routing rules require equals")
        if match == "reason.contains_any" and not values:
            raise ValueError("reason.contains_any routing rules require values")
        return cls(match=match, domain=domain, equals=equals, values=values)


@dataclass(frozen=True)
class EscalationDomainPolicy:
    name: str
    low_actions: tuple[str, ...]
    medium_actions: tuple[str, ...]
    high_actions: tuple[str, ...]
    recommendations: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "EscalationDomainPolicy":
        name = str(raw.get("name") or "")
        if not name:
            raise ValueError("escalation domain missing name")
        recommendations = raw.get("recommendations") or []
        if not isinstance(recommendations, list) or not all(isinstance(item, dict) for item in recommendations):
            raise ValueError(f"{name}.recommendations must be a list of tables")
        for item in recommendations:
            for field_name in ("kind", "action", "risk"):
                if not isinstance(item.get(field_name), str) or not item[field_name]:
                    raise ValueError(f"{name}.recommendations.{field_name} must be a non-empty string")
            if not isinstance(item.get("requires_approval"), bool):
                raise ValueError(f"{name}.recommendations.requires_approval must be a boolean")
        return cls(
            name=name,
            low_actions=_string_list(raw.get("low_actions"), field_name=f"{name}.low_actions"),
            medium_actions=_string_list(raw.get("medium_actions"), field_name=f"{name}.medium_actions"),
            high_actions=_string_list(raw.get("high_actions"), field_name=f"{name}.high_actions"),
            recommendations=tuple(dict(item) for item in recommendations),
        )

    def actions_by_risk(self) -> dict[str, tuple[str, ...]]:
        return {
            "low": self.low_actions,
            "medium": self.medium_actions,
            "high": self.high_actions,
        }


@dataclass(frozen=True)
class EscalationPolicyManifest:
    version: int
    default_domain: str
    domains: dict[str, EscalationDomainPolicy]
    routing_rules: tuple[EscalationRoutingRule, ...]
    source_path: Path

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, source_path: Path) -> "EscalationPolicyManifest":
        version = int(raw.get("version") or 0)
        if version < 1:
            raise ValueError("escalation policy manifest requires version >= 1")
        raw_domains = raw.get("domains") or []
        if not isinstance(raw_domains, list) or not all(isinstance(item, dict) for item in raw_domains):
            raise ValueError("escalation policy domains must be a list of tables")
        domains = {domain.name: domain for domain in (EscalationDomainPolicy.from_mapping(item) for item in raw_domains)}
        if not domains:
            raise ValueError("escalation policy manifest requires at least one domain")
        default_domain = str(raw.get("default_domain") or "")
        if default_domain not in domains:
            raise ValueError("escalation policy default_domain must reference a declared domain")
        raw_rules = raw.get("routing_rules") or []
        if not isinstance(raw_rules, list) or not all(isinstance(item, dict) for item in raw_rules):
            raise ValueError("escalation routing_rules must be a list of tables")
        rules = tuple(EscalationRoutingRule.from_mapping(item, domains=set(domains)) for item in raw_rules)
        if not rules:
            raise ValueError("escalation policy manifest requires routing_rules")
        return cls(
            version=version,
            default_domain=default_domain,
            domains=domains,
            routing_rules=rules,
            source_path=source_path,
        )

    def domain_policy(self, domain: str) -> EscalationDomainPolicy:
        return self.domains.get(domain) or self.domains[self.default_domain]

    def infer_domain(self, actuation: dict[str, Any], escalation: dict[str, Any]) -> str:
        signals = escalation.get("signals") or {}
        signal_kind = str(signals.get("kind") or "")
        reason = _reason_text(actuation, escalation)
        for rule in self.routing_rules:
            if rule.match == "signal.kind" and signal_kind == rule.equals:
                return rule.domain
            if rule.match == "reason.contains_any" and any(value in reason for value in rule.values):
                return rule.domain
        return self.default_domain


@cache
def load_escalation_policy_manifest(path: Path = POLICY_MANIFEST_PATH) -> EscalationPolicyManifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return EscalationPolicyManifest.from_mapping(data, source_path=path)


class EscalationPolicyRouter:
    """Build and persist domain-specific escalation routes."""

    def __init__(
        self,
        *,
        store: AgenticStore | None = None,
        route_flag_ttl_seconds: int = 900,
        create_proposals: bool = False,
    ) -> None:
        self.store = store or get_agentic_store()
        self.route_flag_ttl_seconds = int(route_flag_ttl_seconds)
        self.create_proposals = bool(create_proposals)

    def route(self, actuation: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
        route = self.build_route(actuation, escalation)
        self.store.record_event(
            task_id=actuation.get("task_id"),
            event_type="escalation.route_planned",
            actor="agentic.escalation_router",
            payload={
                "actuation_id": actuation.get("id"),
                "proposal_id": actuation.get("proposal_id"),
                "route": route,
            },
        )
        flag = self.store.set_runtime_flag(
            f"escalation_route:{route['domain']}:{actuation['id']}",
            route,
            ttl_seconds=max(60, self.route_flag_ttl_seconds),
        )
        proposal = None
        if self.create_proposals and int(route.get("level") or 0) >= 3:
            proposal = self._create_route_proposal(actuation, route)
        result = {"route": route, "runtime_flag": flag, "proposal": proposal}
        self.store.update_actuation_metadata(
            str(actuation["id"]),
            metadata={
                "last_escalation_route": route,
                **({"last_escalation_route_proposal_id": proposal.get("id")} if proposal else {}),
            },
            event_type="escalation.route_metadata_updated",
            event_payload={"domain": route["domain"], "level": route["level"]},
        )
        return result

    def build_route(self, actuation: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
        policy = load_escalation_policy_manifest()
        domain = infer_escalation_domain(actuation, escalation, policy=policy)
        level = int(escalation.get("level") or 0)
        domain_policy = policy.domain_policy(domain)
        actions = domain_policy.actions_by_risk()
        policy_checks = [
            _policy_summary(action)
            for action in [*actions.get("low", []), *actions.get("medium", []), *actions.get("high", [])]
        ]
        high_actions = [item for item in policy_checks if item["risk_level"] == "high"]
        deny_actions = [item for item in policy_checks if item["decision"] == "deny"]
        if level >= 3:
            route_action = "request_governed_review"
            requires_approval = bool(high_actions or deny_actions)
        elif level == 2:
            route_action = "run_or_schedule_read_only_diagnostics"
            requires_approval = False
        else:
            route_action = "observe"
            requires_approval = False
        return {
            "version": 1,
            "created_at": time.time(),
            "domain": domain,
            "level": level,
            "reason": escalation.get("reason"),
            "route_action": route_action,
            "requires_approval": requires_approval,
            "safe_actions_only": True,
            "signals": escalation.get("signals") or {},
            "policy_checks": policy_checks,
            "low_risk_diagnostics": actions.get("low", []),
            "medium_dry_runs_or_proposals": actions.get("medium", []),
            "high_risk_requires_approval": [item["action"] for item in high_actions],
            "deny_actions": [item["action"] for item in deny_actions],
            "recommendations": [dict(item) for item in domain_policy.recommendations],
            "source": {
                "actuation_id": actuation.get("id"),
                "proposal_id": actuation.get("proposal_id"),
                "operation": actuation.get("operation") or {},
            },
            "policy_source": str(policy.source_path),
            "policy_version": policy.version,
        }

    def _create_route_proposal(self, actuation: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
        domain = str(route["domain"])
        return self.store.create_improvement_proposal(
            kind=f"escalation_route_{domain}",
            title=f"Route level {route['level']} escalation for {domain}",
            risk_level="high" if route.get("requires_approval") else "medium",
            confidence=0.85,
            score=float(route.get("level") or 0),
            payload={
                "operation": "request_human_review",
                "domain": domain,
                "route": route,
                "safe_actions_only": True,
                "approval_required_for": route.get("high_risk_requires_approval", []),
                "dry_run_or_proposal_actions": route.get("medium_dry_runs_or_proposals", []),
                "read_only_diagnostics": route.get("low_risk_diagnostics", []),
            },
            evidence={
                "actuation_id": actuation.get("id"),
                "source_proposal_id": actuation.get("proposal_id"),
                "route": route,
                "impact": actuation.get("impact") or {},
            },
            task_id=actuation.get("task_id"),
            ttl_seconds=self.route_flag_ttl_seconds,
            metadata={
                "origin": "agentic_escalation_policy_router",
                "domain": domain,
                "requires_approval_to_apply": True,
                "safe_actions_only": True,
            },
        )


def infer_escalation_domain(
    actuation: dict[str, Any],
    escalation: dict[str, Any],
    *,
    policy: EscalationPolicyManifest | None = None,
) -> str:
    policy = policy or load_escalation_policy_manifest()
    return policy.infer_domain(actuation, escalation)


def _reason_text(actuation: dict[str, Any], escalation: dict[str, Any]) -> str:
    operation = actuation.get("operation") or {}
    key = str(operation.get("key") or "")
    return " ".join(
        str(value or "")
        for value in (
            escalation.get("reason"),
            key,
            (operation.get("value") or {}).get("reason") if isinstance(operation.get("value"), dict) else "",
        )
    ).lower()


def _policy_summary(action: str) -> dict[str, Any]:
    decision = check_policy(action, {})
    return {
        "action": decision.action,
        "decision": decision.decision,
        "risk_level": decision.risk_level,
        "requires_approval": decision.requires_approval,
        "dry_run_required": decision.dry_run_required,
        "reason": decision.reason,
    }
