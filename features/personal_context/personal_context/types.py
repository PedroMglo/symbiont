"""Data types for the Personal Context feature."""

from __future__ import annotations

from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field


class CalendarEvent(BaseModel):
    summary: str
    start: str
    location: str = ""
    description: str = ""


class CalendarResponse(BaseModel):
    events: list[CalendarEvent] = Field(default_factory=list)
    window_days: int = 7


class EmailItem(BaseModel):
    subject: str = ""
    sender: str = ""
    date: str = ""
    snippet: str = ""


class EmailResponse(BaseModel):
    emails: list[EmailItem] = Field(default_factory=list)
    folder: str = "INBOX"


class FeedItem(BaseModel):
    title: str = ""
    link: str = ""
    published: str = ""
    feed_name: str = ""


class FeedsResponse(BaseModel):
    items: list[FeedItem] = Field(default_factory=list)
    feeds_checked: int = 0


class HealthResponse(ServiceHealthResponse):
    calendar_enabled: bool = False
    email_enabled: bool = False
    rss_enabled: bool = False


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "personal_context"
    capabilities: list[str] = Field(
        default_factory=lambda: ["calendar", "email", "rss_feeds", "personal_data"]
    )
    description: str = (
        "Provides personal context including calendar events, "
        "recent emails, and news feeds."
    )
