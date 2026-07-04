"""Logging setup for extrator."""

from __future__ import annotations

import logging

from extrator.config import get_config


def configure_logging() -> None:
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.observability.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
