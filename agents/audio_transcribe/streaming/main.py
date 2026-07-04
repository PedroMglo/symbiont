"""Entry point for the Unified Audio Intelligence Platform."""

import logging
import os
import uvicorn

from streaming.config import get_config


def _tls_kwargs() -> dict[str, str]:
    cert_file = os.environ.get("AI_LOCAL_TLS_CERT_FILE")
    key_file = os.environ.get("AI_LOCAL_TLS_KEY_FILE")
    if not cert_file or not key_file:
        raise RuntimeError("AI_LOCAL_TLS_CERT_FILE and AI_LOCAL_TLS_KEY_FILE are required")
    return {"ssl_certfile": cert_file, "ssl_keyfile": key_file}


def main():
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.server.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)
    logger.info(
        f"Starting Unified Audio Intelligence Platform v2.0.0 "
        f"(port={cfg.server.port}, gpu_workers={cfg.gpu.max_workers})"
    )
    uvicorn.run(
        "streaming.api:app",
        host=cfg.server.host,
        port=cfg.server.port,
        workers=1,
        log_level=cfg.server.log_level.lower(),
        ws_max_size=16 * 1024 * 1024,  # 16MB WebSocket max (for audio frames)
        **_tls_kwargs(),
    )


if __name__ == "__main__":
    main()
