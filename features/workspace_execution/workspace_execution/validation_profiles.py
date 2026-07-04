"""Validation runner profiles owned by workspace_execution."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


@dataclass(frozen=True)
class ValidationProfileSpec:
    name: str
    description: str
    required_tools: tuple[str, ...]
    allows_network: bool = False
    allows_docker_cli: bool = False
    requires_isolated_container_runtime: bool = False
    network_scope: str = "none"
    requires_validation_command: bool = False
    supervises_background_services: bool = False

    def public_payload(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "required_tools": list(self.required_tools),
            "allows_network": self.allows_network,
            "allows_docker_cli": self.allows_docker_cli,
            "requires_isolated_container_runtime": self.requires_isolated_container_runtime,
            "network_scope": self.network_scope,
            "requires_validation_command": self.requires_validation_command,
            "supervises_background_services": self.supervises_background_services,
        }


VALIDATION_PROFILES: dict[str, ValidationProfileSpec] = {
    "python-basic": ValidationProfileSpec(
        name="python-basic",
        description="Python syntax and unit-test validation without network or Docker.",
        required_tools=("python",),
    ),
    "python-pytest": ValidationProfileSpec(
        name="python-pytest",
        description="Python pytest validation without network or Docker.",
        required_tools=("python", "pytest"),
    ),
    "python-api": ValidationProfileSpec(
        name="python-api",
        description="Python API smoke validation with pytest and HTTP client tooling.",
        required_tools=("python", "pytest", "curl"),
        network_scope="vm-loopback",
        requires_validation_command=True,
        supervises_background_services=True,
    ),
    "docker-compose-static": ValidationProfileSpec(
        name="docker-compose-static",
        description="Static Docker Compose validation such as docker compose config or docker-compose config.",
        required_tools=("docker", "docker-compose"),
        allows_docker_cli=True,
    ),
    "docker-compose-runtime": ValidationProfileSpec(
        name="docker-compose-runtime",
        description="Controlled Docker Compose runtime validation through an approved runner/proxy.",
        required_tools=("docker", "curl"),
        allows_network=True,
        allows_docker_cli=True,
        requires_isolated_container_runtime=True,
        network_scope="vm-internal",
        supervises_background_services=True,
    ),
    "stateful-postgres": ValidationProfileSpec(
        name="stateful-postgres",
        description="PostgreSQL-backed persistence smoke validation.",
        required_tools=("python",),
        allows_network=True,
        network_scope="vm-internal",
        requires_validation_command=True,
        supervises_background_services=True,
    ),
    "stateful-redis": ValidationProfileSpec(
        name="stateful-redis",
        description="Redis event or queue smoke validation.",
        required_tools=("python",),
        allows_network=True,
        network_scope="vm-internal",
        requires_validation_command=True,
        supervises_background_services=True,
    ),
    "worker-queue": ValidationProfileSpec(
        name="worker-queue",
        description="Submit a queued job, observe worker processing, and verify final state.",
        required_tools=("python", "curl"),
        allows_network=True,
        network_scope="vm-internal",
        requires_validation_command=True,
        supervises_background_services=True,
    ),
    "cli": ValidationProfileSpec(
        name="cli",
        description="Command-line interface help, submit, and inspect/status validation.",
        required_tools=("python",),
        requires_validation_command=True,
    ),
    "artifact": ValidationProfileSpec(
        name="artifact",
        description="Generated artifact structure and expected-root validation.",
        required_tools=("python",),
    ),
    "node-basic": ValidationProfileSpec(
        name="node-basic",
        description="Node.js syntax/test validation without network or Docker.",
        required_tools=("node", "npm"),
    ),
}


def validation_profiles_payload() -> dict[str, dict[str, Any]]:
    return {name: spec.public_payload() for name, spec in sorted(VALIDATION_PROFILES.items())}


def validation_profile_spec(name: str | None) -> ValidationProfileSpec | None:
    if not name:
        return None
    return VALIDATION_PROFILES.get(str(name))


def command_required_tools(profile_name: str | None, argv: list[str]) -> tuple[str, ...]:
    spec = validation_profile_spec(profile_name)
    if spec is None:
        return ()
    observed = _observed_tools(argv)
    required = {"python" if tool in {"python3", "python"} else tool for tool in observed}
    if not required:
        required = set(spec.required_tools)
    else:
        required &= set(spec.required_tools)
        if _uses_python_pytest(argv) and "pytest" in spec.required_tools:
            required.add("pytest")
    if "docker" in required and not spec.allows_docker_cli:
        required.remove("docker")
    return tuple(sorted(required))


def _observed_tools(argv: list[str]) -> set[str]:
    if not argv:
        return set()
    command_line = " ".join(shlex.quote(item) for item in argv)
    first = PurePosixPath(str(argv[0])).name
    if first in {"bash", "sh"} and len(argv) >= 3 and argv[1] in {"-c", "-lc"}:
        command_line = str(argv[2])
    observed: set[str] = set()
    for token in _shell_tokens(command_line):
        name = PurePosixPath(token).name
        if name in {"python", "python3", "pytest", "docker", "docker-compose", "curl", "node", "npm"}:
            observed.add(name)
    return observed


def _shell_tokens(command_line: str) -> list[str]:
    try:
        return shlex.split(command_line)
    except ValueError:
        return command_line.split()


def _uses_python_pytest(argv: list[str]) -> bool:
    if not argv:
        return False
    command_line = " ".join(shlex.quote(item) for item in argv)
    first = PurePosixPath(str(argv[0])).name
    if first in {"bash", "sh"} and len(argv) >= 3 and argv[1] in {"-c", "-lc"}:
        command_line = str(argv[2])
    if "find_spec('pytest')" in command_line or 'find_spec("pytest")' in command_line:
        return True
    tokens = _shell_tokens(command_line)
    return any(
        PurePosixPath(token).name in {"python", "python3"} and tokens[index + 1 : index + 3] == ["-m", "pytest"]
        for index, token in enumerate(tokens)
    )
