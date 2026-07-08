"""ASGI entry point."""

from __future__ import annotations

from storage_guardian.api.routes import create_app

app = create_app()
