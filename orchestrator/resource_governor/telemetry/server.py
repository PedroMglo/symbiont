"""Small HTTPS server for a host-side telemetry authority."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from orchestrator.resource_governor.telemetry.authority import TelemetryAuthority


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("AI_TELEMETRY_AUTHORITY_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_TELEMETRY_AUTHORITY_PORT", "8767")))
    parser.add_argument(
        "--cache-ttl-seconds",
        type=float,
        default=float(os.environ.get("AI_TELEMETRY_AUTHORITY_CACHE_TTL_SECONDS", "1.5")),
    )
    parser.add_argument("--certfile", default=os.environ.get("AI_TELEMETRY_AUTHORITY_CERT_FILE", ""))
    parser.add_argument("--keyfile", default=os.environ.get("AI_TELEMETRY_AUTHORITY_KEY_FILE", ""))
    parser.add_argument("--allow-unauthenticated", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    token = _configured_token()
    require_token = bool(token) and not args.allow_unauthenticated
    if not token and not args.allow_unauthenticated and not _is_loopback_bind(args.host):
        raise SystemExit(
            "Refusing unauthenticated telemetry bind outside loopback. "
            "Set AI_TELEMETRY_AUTHORITY_TOKEN_FILE or pass --allow-unauthenticated explicitly."
        )
    certfile, keyfile = _tls_files(args.certfile, args.keyfile)
    if not certfile and not _is_loopback_bind(args.host):
        raise SystemExit(
            "Refusing non-loopback telemetry bind without TLS cert/key. "
            "Run make telemetry-authority or set AI_TELEMETRY_AUTHORITY_CERT_FILE and AI_TELEMETRY_AUTHORITY_KEY_FILE."
        )

    authority = TelemetryAuthority(cache_ttl_seconds=args.cache_ttl_seconds)
    handler = _handler(authority=authority, token=token, require_token=require_token, verbose=args.verbose)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    scheme = "http"
    if certfile:
        context = _ssl_context(certfile=certfile, keyfile=keyfile)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(
        f"telemetry authority listening on {scheme}://{args.host}:{args.port} "
        f"(auth={'required' if require_token else 'disabled'})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        httpd.server_close()
    return 0


def _handler(
    *,
    authority: TelemetryAuthority,
    token: str,
    require_token: bool,
    verbose: bool,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AiLocalTelemetryAuthority/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if verbose:
                super().log_message(fmt, *args)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._write_json({"status": "ok"})
                return
            if path != "/telemetry/snapshot":
                self._write_json({"detail": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if require_token and not _authorized(self.headers.get("X-Internal-API-Key", ""), self.headers.get("Authorization", ""), token):
                self._write_json({"detail": "missing or invalid telemetry token"}, status=HTTPStatus.UNAUTHORIZED)
                return
            snapshot = authority.snapshot()
            self._write_json(snapshot.model_dump(mode="json"))

        def _write_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _authorized(api_key: str, authorization: str, expected: str) -> bool:
    provided = api_key.strip()
    if not provided and authorization.startswith("Bearer "):
        provided = authorization.removeprefix("Bearer ").strip()
    return bool(provided and expected and secrets.compare_digest(provided, expected))


def _configured_token() -> str:
    for env_name in ("AI_TELEMETRY_AUTHORITY_TOKEN", "AI_RESOURCE_GOVERNOR_TOKEN", "INTERNAL_API_KEY"):
        token = os.environ.get(env_name, "").strip()
        if token:
            return token
    for env_name in ("AI_TELEMETRY_AUTHORITY_TOKEN_FILE", "AI_RESOURCE_GOVERNOR_TOKEN_FILE", "INTERNAL_API_KEY_FILE"):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        try:
            token = Path(raw).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""  # nosec B105 - empty fallback after unreadable token file
        if token:
            return token
    return ""


def _tls_files(certfile: str, keyfile: str) -> tuple[Path | None, Path | None]:
    cert = Path(certfile).expanduser() if certfile else None
    key = Path(keyfile).expanduser() if keyfile else None
    if not cert and not key:
        return None, None
    if not cert or not key:
        raise SystemExit("Telemetry TLS requires both certfile and keyfile")
    if not cert.is_file():
        raise SystemExit(f"Telemetry TLS certfile does not exist: {cert}")
    if not key.is_file():
        raise SystemExit(f"Telemetry TLS keyfile does not exist: {key}")
    return cert, key


def _ssl_context(*, certfile: Path, keyfile: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    return context


def _is_loopback_bind(host: str) -> bool:
    return host in {"", "127.0.0.1", "::1", "localhost"}


if __name__ == "__main__":
    raise SystemExit(main())
