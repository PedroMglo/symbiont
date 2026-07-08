"""Validate API, security and HTTPS governance configuration."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_PATH = ROOT / "config" / "api.yaml"
DEFAULT_SECURITY_PATH = ROOT / "infra" / "security" / "api-security.yaml"
DEFAULT_HTTPS_PATH = ROOT / "config" / "https.yaml"

Environment = Literal["local", "dev", "staging", "prod"]


class ApiSecurityConfigError(ValueError):
    """Raised when API governance config is invalid."""


@dataclass(frozen=True)
class ApiDefaults:
    default_version: str
    public_base_url: str
    docs_enabled: bool
    expose_docs_publicly: bool
    allow_anonymous_health: bool
    allow_anonymous_ready: bool
    required_endpoints: tuple[str, ...]
    default_timeout_seconds: int
    max_body_mb: int
    upload_max_body_mb: int
    streaming_timeout_seconds: int


@dataclass(frozen=True)
class EndpointClass:
    methods: tuple[str, ...]
    anonymous: bool
    timeout_seconds: int
    rate_limit: str
    max_body_mb: int
    concurrency_limit: int | None = None
    queue_limit: int | None = None
    internal_only: bool = False


@dataclass(frozen=True)
class CorsPolicy:
    allowed_origins: tuple[str, ...]
    allowed_methods: tuple[str, ...]
    allowed_headers: tuple[str, ...]
    allow_credentials: bool
    max_age_seconds: int


@dataclass(frozen=True)
class TokenPolicy:
    source: str
    min_length: int
    rotation_days: int
    per_service_tokens: bool
    allow_shared_internal_token: bool
    allow_direct_env_token_in_prod: bool


@dataclass(frozen=True)
class SecurityPolicy:
    auth_required_by_default: bool
    allow_anonymous_health: bool
    allow_anonymous_docs: bool
    require_gateway_for_external: bool
    production_requires_https: bool
    production_requires_auth: bool
    accepted_auth_schemes: tuple[str, ...]
    token_policy: TokenPolicy
    redact_headers: tuple[str, ...]
    redact_fields: tuple[str, ...]
    cors: CorsPolicy
    rate_limits: dict[str, str]


@dataclass(frozen=True)
class HttpsPolicy:
    enabled: bool
    gateway: str
    mode: str
    public_host: str
    http_port: int
    https_port: int
    redirect_http_to_https: bool
    min_tls_version: str
    prefer_tls13: bool
    hsts_enabled: bool
    hsts_max_age_seconds: int


@dataclass(frozen=True)
class ApiGovernanceConfig:
    environment: Environment
    api: ApiDefaults
    endpoint_classes: dict[str, EndpointClass]
    services: dict[str, dict[str, Any]]
    security: SecurityPolicy
    https: HttpsPolicy


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ApiSecurityConfigError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ApiSecurityConfigError(f"{path} must contain a mapping")
    version = data.get("version")
    if version != 1:
        raise ApiSecurityConfigError(f"{path} must use version: 1")
    return data


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ApiSecurityConfigError(f"{name} must be a mapping")
    return value


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ApiSecurityConfigError(f"{field_name} must be a boolean")


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ApiSecurityConfigError(f"{field_name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiSecurityConfigError(f"{field_name} must be an integer") from exc
    if result < 0:
        raise ApiSecurityConfigError(f"{field_name} must be >= 0")
    return result


def _as_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ApiSecurityConfigError(f"{field_name} must be a list")
    result = tuple(str(item) for item in value)
    if any(not item for item in result):
        raise ApiSecurityConfigError(f"{field_name} cannot contain empty values")
    return result


def _parse_endpoint_classes(raw: dict[str, Any]) -> dict[str, EndpointClass]:
    classes_raw = raw.get("endpoint_classes", {})
    if not isinstance(classes_raw, dict):
        raise ApiSecurityConfigError("endpoint_classes must be a mapping")
    parsed: dict[str, EndpointClass] = {}
    for name, value in classes_raw.items():
        if not isinstance(value, dict):
            raise ApiSecurityConfigError(f"endpoint_classes.{name} must be a mapping")
        timeout = _as_int(value.get("timeout_seconds", 30), f"endpoint_classes.{name}.timeout_seconds")
        max_body = _as_int(value.get("max_body_mb", 25), f"endpoint_classes.{name}.max_body_mb")
        if timeout < 1:
            raise ApiSecurityConfigError(f"endpoint_classes.{name}.timeout_seconds must be >= 1")
        if max_body < 1:
            raise ApiSecurityConfigError(f"endpoint_classes.{name}.max_body_mb must be >= 1")
        parsed[str(name)] = EndpointClass(
            methods=_as_tuple(value.get("methods", []), f"endpoint_classes.{name}.methods"),
            anonymous=_as_bool(value.get("anonymous", False), f"endpoint_classes.{name}.anonymous"),
            timeout_seconds=timeout,
            rate_limit=str(value.get("rate_limit", "")),
            max_body_mb=max_body,
            concurrency_limit=(
                None
                if value.get("concurrency_limit") is None
                else _as_int(value.get("concurrency_limit"), f"endpoint_classes.{name}.concurrency_limit")
            ),
            queue_limit=(
                None
                if value.get("queue_limit") is None
                else _as_int(value.get("queue_limit"), f"endpoint_classes.{name}.queue_limit")
            ),
            internal_only=_as_bool(value.get("internal_only", False), f"endpoint_classes.{name}.internal_only"),
        )
    return parsed


def load_api_governance(
    api_path: Path = DEFAULT_API_PATH,
    security_path: Path = DEFAULT_SECURITY_PATH,
    https_path: Path = DEFAULT_HTTPS_PATH,
    *,
    environment: Environment | None = None,
) -> ApiGovernanceConfig:
    """Load and validate the three API governance files."""

    api_raw = _load_yaml(api_path)
    security_raw = _load_yaml(security_path)
    https_raw = _load_yaml(https_path)

    api = _section(api_raw, "api")
    security = _section(security_raw, "security")
    https = _section(https_raw, "https")
    token = _section(security, "token_policy")
    cors = _section(security, "cors")

    env_raw = environment or str(api_raw.get("environment", "local"))
    if env_raw not in {"local", "dev", "staging", "prod"}:
        raise ApiSecurityConfigError("environment must be local, dev, staging, or prod")

    config = ApiGovernanceConfig(
        environment=env_raw,  # type: ignore[arg-type]
        api=ApiDefaults(
            default_version=str(api.get("default_version", "v1")),
            public_base_url=str(api.get("public_base_url", "")),
            docs_enabled=_as_bool(api.get("docs_enabled", True), "api.docs_enabled"),
            expose_docs_publicly=_as_bool(api.get("expose_docs_publicly", False), "api.expose_docs_publicly"),
            allow_anonymous_health=_as_bool(api.get("allow_anonymous_health", True), "api.allow_anonymous_health"),
            allow_anonymous_ready=_as_bool(api.get("allow_anonymous_ready", True), "api.allow_anonymous_ready"),
            required_endpoints=_as_tuple(api.get("required_endpoints", []), "api.required_endpoints"),
            default_timeout_seconds=_as_int(api.get("default_timeout_seconds", 30), "api.default_timeout_seconds"),
            max_body_mb=_as_int(api.get("max_body_mb", 25), "api.max_body_mb"),
            upload_max_body_mb=_as_int(api.get("upload_max_body_mb", 512), "api.upload_max_body_mb"),
            streaming_timeout_seconds=_as_int(
                api.get("streaming_timeout_seconds", 600),
                "api.streaming_timeout_seconds",
            ),
        ),
        endpoint_classes=_parse_endpoint_classes(api_raw),
        services=_section(api_raw, "services"),
        security=SecurityPolicy(
            auth_required_by_default=_as_bool(
                security.get("auth_required_by_default", True),
                "security.auth_required_by_default",
            ),
            allow_anonymous_health=_as_bool(
                security.get("allow_anonymous_health", True),
                "security.allow_anonymous_health",
            ),
            allow_anonymous_docs=_as_bool(
                security.get("allow_anonymous_docs", False),
                "security.allow_anonymous_docs",
            ),
            require_gateway_for_external=_as_bool(
                security.get("require_gateway_for_external", True),
                "security.require_gateway_for_external",
            ),
            production_requires_https=_as_bool(
                security.get("production_requires_https", True),
                "security.production_requires_https",
            ),
            production_requires_auth=_as_bool(
                security.get("production_requires_auth", True),
                "security.production_requires_auth",
            ),
            accepted_auth_schemes=_as_tuple(
                security.get("accepted_auth_schemes", []),
                "security.accepted_auth_schemes",
            ),
            token_policy=TokenPolicy(
                source=str(token.get("source", "docker_secrets")),
                min_length=_as_int(token.get("min_length", 32), "security.token_policy.min_length"),
                rotation_days=_as_int(token.get("rotation_days", 30), "security.token_policy.rotation_days"),
                per_service_tokens=_as_bool(
                    token.get("per_service_tokens", True),
                    "security.token_policy.per_service_tokens",
                ),
                allow_shared_internal_token=_as_bool(
                    token.get("allow_shared_internal_token", True),
                    "security.token_policy.allow_shared_internal_token",
                ),
                allow_direct_env_token_in_prod=_as_bool(
                    token.get("allow_direct_env_token_in_prod", False),
                    "security.token_policy.allow_direct_env_token_in_prod",
                ),
            ),
            redact_headers=tuple(h.lower() for h in _as_tuple(security.get("redact_headers", []), "security.redact_headers")),
            redact_fields=tuple(f.lower() for f in _as_tuple(security.get("redact_fields", []), "security.redact_fields")),
            cors=CorsPolicy(
                allowed_origins=_as_tuple(cors.get("allowed_origins", []), "security.cors.allowed_origins"),
                allowed_methods=_as_tuple(cors.get("allowed_methods", []), "security.cors.allowed_methods"),
                allowed_headers=tuple(
                    h.lower() for h in _as_tuple(cors.get("allowed_headers", []), "security.cors.allowed_headers")
                ),
                allow_credentials=_as_bool(cors.get("allow_credentials", False), "security.cors.allow_credentials"),
                max_age_seconds=_as_int(cors.get("max_age_seconds", 600), "security.cors.max_age_seconds"),
            ),
            rate_limits={str(k): str(v) for k, v in _section(security, "rate_limits").items()},
        ),
        https=HttpsPolicy(
            enabled=_as_bool(https.get("enabled", True), "https.enabled"),
            gateway=str(https.get("gateway", "caddy")),
            mode=str(https.get("mode", "local_ca")),
            public_host=str(https.get("public_host", "")),
            http_port=_as_int(https.get("http_port", 0), "https.http_port"),
            https_port=_as_int(https.get("https_port", 8443), "https.https_port"),
            redirect_http_to_https=_as_bool(
                https.get("redirect_http_to_https", False),
                "https.redirect_http_to_https",
            ),
            min_tls_version=str(https.get("min_tls_version", "1.2")),
            prefer_tls13=_as_bool(https.get("prefer_tls13", True), "https.prefer_tls13"),
            hsts_enabled=_as_bool(https.get("hsts_enabled", False), "https.hsts_enabled"),
            hsts_max_age_seconds=_as_int(https.get("hsts_max_age_seconds", 0), "https.hsts_max_age_seconds"),
        ),
    )
    errors = validate_api_governance(config)
    if errors:
        raise ApiSecurityConfigError("; ".join(errors))
    return config


def validate_api_governance(config: ApiGovernanceConfig) -> list[str]:
    """Return validation errors for unsafe or contradictory API policy."""

    errors: list[str] = []
    if not config.api.default_version.startswith("v"):
        errors.append("api.default_version must look like v1")
    if config.api.default_timeout_seconds < 1:
        errors.append("api.default_timeout_seconds must be >= 1")
    if config.api.max_body_mb < 1:
        errors.append("api.max_body_mb must be >= 1")
    if config.api.upload_max_body_mb < config.api.max_body_mb:
        errors.append("api.upload_max_body_mb must be >= api.max_body_mb")
    if "health" not in config.endpoint_classes:
        errors.append("endpoint_classes.health is required")
    if "expensive" not in config.endpoint_classes:
        errors.append("endpoint_classes.expensive is required")
    if "upload" not in config.endpoint_classes:
        errors.append("endpoint_classes.upload is required")

    required_headers = {"authorization", "x-api-key", "cookie"}
    missing_headers = sorted(required_headers - set(config.security.redact_headers))
    if missing_headers:
        errors.append(f"security.redact_headers missing: {', '.join(missing_headers)}")

    if "*" in config.security.cors.allowed_origins:
        errors.append("security.cors.allowed_origins must not contain '*'")
    if config.security.cors.allow_credentials and not config.security.cors.allowed_origins:
        errors.append("security.cors.allow_credentials=true requires explicit origins")
    if config.security.token_policy.min_length < 24:
        errors.append("security.token_policy.min_length must be >= 24")
    if config.security.token_policy.rotation_days < 1:
        errors.append("security.token_policy.rotation_days must be >= 1")
    if config.https.min_tls_version not in {"1.2", "1.3"}:
        errors.append("https.min_tls_version must be 1.2 or 1.3")
    if config.https.http_port != 0:
        errors.append("https.http_port must be 0 because plain HTTP listeners are forbidden")
    if config.https.redirect_http_to_https:
        errors.append("https.redirect_http_to_https must be false because plain HTTP listeners are removed")
    public_url = urlsplit(config.api.public_base_url)
    if public_url.scheme != "https" or not public_url.netloc:
        errors.append("api.public_base_url must be an absolute https URL")
    for service, service_config in sorted(config.services.items()):
        raw_url = str(service_config.get("internal_url", ""))
        parsed = urlsplit(raw_url)
        if parsed.scheme == "http":
            errors.append(f"services.{service}.internal_url uses forbidden plain HTTP")
        elif parsed.scheme != "https" or not parsed.netloc:
            errors.append(f"services.{service}.internal_url must be an absolute https URL")

    if config.environment == "prod":
        if config.security.production_requires_auth and not config.security.auth_required_by_default:
            errors.append("prod requires security.auth_required_by_default=true")
        if config.security.production_requires_https and not config.https.enabled:
            errors.append("prod requires https.enabled=true")
        if config.api.expose_docs_publicly or config.security.allow_anonymous_docs:
            errors.append("prod must not expose docs anonymously")
        if config.security.token_policy.allow_direct_env_token_in_prod:
            errors.append("prod must not allow direct env token source")
        if config.https.gateway not in {"caddy", "traefik", "nginx"}:
            errors.append("prod https.gateway must be caddy, traefik, or nginx")
    return errors


def to_plain(config: ApiGovernanceConfig) -> dict[str, Any]:
    """Convert dataclasses into JSON-friendly mappings."""

    def convert(value: Any) -> Any:
        if hasattr(value, "__dataclass_fields__"):
            return {name: convert(getattr(value, name)) for name in value.__dataclass_fields__}
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {str(k): convert(v) for k, v in value.items()}
        return value

    return convert(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.api_security")
    parser.add_argument("--api-config", default=str(DEFAULT_API_PATH))
    parser.add_argument("--security-config", default=str(DEFAULT_SECURITY_PATH))
    parser.add_argument("--https-config", default=str(DEFAULT_HTTPS_PATH))
    parser.add_argument("--environment", choices=["local", "dev", "staging", "prod"])
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--print", action="store_true", dest="print_config")
    args = parser.parse_args(argv)

    try:
        config = load_api_governance(
            Path(args.api_config),
            Path(args.security_config),
            Path(args.https_config),
            environment=args.environment,  # type: ignore[arg-type]
        )
    except ApiSecurityConfigError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.print_config or not args.validate:
        print(json.dumps(to_plain(config), indent=2, ensure_ascii=True))
    if args.validate:
        print("OK: API governance configuration is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
