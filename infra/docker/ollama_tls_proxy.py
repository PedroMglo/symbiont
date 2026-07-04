"""Tiny TLS terminator for the host Ollama HTTP API.

The ai-local containers talk HTTPS only. Native Ollama does not terminate TLS,
so this service accepts TLS inside the Docker network and forwards the raw HTTP
stream to the host daemon without exposing another host port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from contextlib import suppress


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
log = logging.getLogger("ollama_tls_proxy")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        with suppress(Exception):
            writer.write_eof()
        with suppress(Exception):
            await writer.drain()


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_host: str,
    upstream_port: int,
) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(upstream_host, upstream_port)
    except OSError as exc:
        log.warning("upstream unavailable for %s: %s", peer, exc)
        client_writer.write(
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Type: text/plain\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"Ollama upstream unavailable\n"
        )
        await client_writer.drain()
        client_writer.close()
        await client_writer.wait_closed()
        return

    try:
        await asyncio.gather(
            _pipe(client_reader, upstream_writer),
            _pipe(upstream_reader, client_writer),
        )
    finally:
        for writer in (upstream_writer, client_writer):
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


async def _main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=LOG_FORMAT)

    listen_host = os.getenv("OLLAMA_PROXY_LISTEN_HOST", "0.0.0.0")
    listen_port = _env_int("OLLAMA_PROXY_LISTEN_PORT", 11434)
    upstream_host = os.getenv("OLLAMA_PROXY_UPSTREAM_HOST", "host.docker.internal")
    upstream_port = _env_int("OLLAMA_PROXY_UPSTREAM_PORT", 11434)
    cert_file = os.environ["AI_LOCAL_TLS_CERT_FILE"]
    key_file = os.environ["AI_LOCAL_TLS_KEY_FILE"]

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(certfile=cert_file, keyfile=key_file)

    server = await asyncio.start_server(
        lambda reader, writer: _handle_client(reader, writer, upstream_host, upstream_port),
        listen_host,
        listen_port,
        ssl=ssl_context,
    )

    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    log.info("Ollama TLS proxy listening on %s -> %s:%s", sockets, upstream_host, upstream_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
