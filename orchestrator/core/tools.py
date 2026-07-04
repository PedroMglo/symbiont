"""Generic tool registry for LLM-invocable runtime tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from orchestrator.config import get_settings

if TYPE_CHECKING:
    from orchestrator.types import ContextBlock

_TOOL_BUDGET_FALLBACK = 2000


@dataclass(frozen=True)
class ToolDefinition:
    """Definition of a tool that the LLM can invoke."""

    name: str
    description: str
    parameters: dict[str, Any]
    callable: Callable[..., "ContextBlock | None"]


def _default_tool_budget_tokens() -> int:
    try:
        return int(get_settings().dispatch.feature_budget_tokens)
    except Exception:
        return _TOOL_BUDGET_FALLBACK


def default_tool_parameters(*, budget_tokens: int | None = None) -> dict[str, Any]:
    """Return the base JSON Schema shared by all context-provider tools."""
    budget_tokens = budget_tokens if budget_tokens is not None else _default_tool_budget_tokens()
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Texto a pesquisar",
            },
            "budget_tokens": {
                "type": "integer",
                "default": budget_tokens,
                "description": "Token budget máximo para o contexto retornado",
            },
        },
        "required": ["query"],
    }


class ToolRegistry:
    """Registry of tools available for LLM function calling."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool. Raises ValueError on duplicate name."""
        if tool.name in self._tools:
            msg = f"Tool already registered: {tool.name!r}"
            raise ValueError(msg)
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        """Resolve a tool by name. Returns None if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """Return all registered tools."""
        return list(self._tools.values())

    def export_for_llm(self) -> list[dict[str, Any]]:
        """Export tool definitions in OpenAI function calling format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]
