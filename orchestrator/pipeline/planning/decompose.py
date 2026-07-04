"""Task planning data types — subtask decomposition and peer review.

v1.5 — Meta-Symbiont v2 (Team Coordinator).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.types import AgentCapability


@dataclass
class SubTask:
    """A decomposed unit of work within an execution plan."""

    id: int
    description: str
    required_capabilities: list[AgentCapability] = field(default_factory=list)
    assigned_agents: list[str] = field(default_factory=list)
    dependencies: list[int] = field(default_factory=list)
    priority: int = 1
    budget_tokens: int = 2000

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "required_capabilities": [c.value for c in self.required_capabilities],
            "assigned_agents": self.assigned_agents,
            "dependencies": self.dependencies,
            "priority": self.priority,
            "budget_tokens": self.budget_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SubTask":
        caps = [AgentCapability(c) for c in d.get("required_capabilities", [])]
        return cls(
            id=d["id"],
            description=d["description"],
            required_capabilities=caps,
            assigned_agents=d.get("assigned_agents", []),
            dependencies=d.get("dependencies", []),
            priority=d.get("priority", 1),
            budget_tokens=d.get("budget_tokens", 2000),
        )


@dataclass
class ReviewFeedback:
    """Peer review feedback from one agent about another's output."""

    reviewer: str
    reviewed_agent: str
    score: float              # 0.0-1.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "reviewed_agent": self.reviewed_agent,
            "score": self.score,
            "issues": self.issues,
            "suggestions": self.suggestions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewFeedback":
        return cls(
            reviewer=d["reviewer"],
            reviewed_agent=d["reviewed_agent"],
            score=d.get("score", 0.5),
            issues=d.get("issues", []),
            suggestions=d.get("suggestions", []),
        )


def compute_parallel_groups(tasks: list[SubTask]) -> list[list[int]]:
    """Topological sort into parallel execution groups.

    Tasks within the same group have no dependencies on each other
    and can execute concurrently.
    """
    if not tasks:
        return []

    task_map = {t.id: t for t in tasks}
    remaining = set(task_map.keys())
    completed: set[int] = set()
    groups: list[list[int]] = []

    while remaining:
        # Find tasks whose dependencies are all completed
        ready = [
            tid for tid in remaining
            if all(dep in completed for dep in task_map[tid].dependencies)
        ]
        if not ready:
            # Circular dependency — break by taking all remaining
            ready = list(remaining)

        groups.append(sorted(ready))
        completed.update(ready)
        remaining -= set(ready)

    return groups


def merge_redundant_tasks(tasks: list[SubTask]) -> list[SubTask]:
    """Merge subtasks that target the same agents with similar descriptions."""
    if len(tasks) <= 1:
        return tasks

    # Group by assigned agents (as frozenset)
    by_agents: dict[frozenset[str], list[SubTask]] = {}
    for t in tasks:
        key = frozenset(t.assigned_agents) if t.assigned_agents else frozenset()
        if key not in by_agents:
            by_agents[key] = []
        by_agents[key].append(t)

    result: list[SubTask] = []
    for agent_group, group_tasks in by_agents.items():
        if len(group_tasks) == 1 or not agent_group:
            result.extend(group_tasks)
        else:
            # Merge: combine descriptions, take max budget, union dependencies
            merged = SubTask(
                id=group_tasks[0].id,
                description=" + ".join(t.description for t in group_tasks),
                required_capabilities=list(set(
                    c for t in group_tasks for c in t.required_capabilities
                )),
                assigned_agents=list(agent_group),
                dependencies=list(set(
                    d for t in group_tasks for d in t.dependencies
                    if d not in {gt.id for gt in group_tasks}
                )),
                priority=min(t.priority for t in group_tasks),
                budget_tokens=sum(t.budget_tokens for t in group_tasks),
            )
            result.append(merged)

    return result
