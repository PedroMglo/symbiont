"""FastAPI application for the Personal Context feature."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from sharedai.servicekit.auth import service_token_dependency

from personal_context import __version__
from personal_context.calendar import get_events
from personal_context.config import get_settings
from personal_context.email import get_emails
from personal_context.rss import get_feed_items
from personal_context.types import (
    CalendarResponse,
    CapabilitiesResponse,
    EmailResponse,
    FeedsResponse,
    HealthResponse,
)

app = FastAPI(title="Personal Context Feature", version=__version__)
require_service_token = service_token_dependency(
    "Personal Context",
    lambda: get_settings().security.api_key,
)


@app.get("/health")
def health() -> HealthResponse:
    cfg = get_settings()
    return HealthResponse(
        version=__version__,
        calendar_enabled=cfg.calendar.enabled,
        email_enabled=cfg.email.enabled,
        rss_enabled=cfg.rss.enabled,
    )


@app.get("/v1/personal/capabilities")
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse()


@app.get("/v1/personal/calendar", dependencies=[Depends(require_service_token)])
def calendar() -> CalendarResponse:
    """Get upcoming/recent calendar events."""
    cfg = get_settings()
    events = get_events()
    return CalendarResponse(events=events, window_days=cfg.calendar.window_days)


@app.get("/v1/personal/email", dependencies=[Depends(require_service_token)])
def email_inbox() -> EmailResponse:
    """Get recent emails."""
    emails = get_emails()
    return EmailResponse(emails=emails)


@app.get("/v1/personal/feeds", dependencies=[Depends(require_service_token)])
def feeds() -> FeedsResponse:
    """Get recent RSS/Atom feed items."""
    cfg = get_settings()
    items = get_feed_items()
    return FeedsResponse(items=items, feeds_checked=len(cfg.rss.feeds))
