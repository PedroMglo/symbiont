"""Calendar provider — reads .ics files and returns upcoming/recent events."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_context.config import get_settings
from personal_context.types import CalendarEvent

log = logging.getLogger(__name__)


def get_events() -> list[CalendarEvent]:
    """Get upcoming/recent calendar events."""
    cfg = get_settings()
    if not cfg.calendar.enabled:
        return []

    try:
        from icalendar import Calendar
    except ImportError:
        log.warning("Calendar: icalendar not installed")
        return []

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=cfg.calendar.window_days)
    window_end = now + timedelta(days=cfg.calendar.window_days)

    events: list[CalendarEvent] = []
    ics_files = _find_ics_files(cfg.calendar.ics_paths)

    for ics_file in ics_files:
        try:
            with open(ics_file, "rb") as f:
                cal = Calendar.from_ical(f.read())
            for component in cal.walk():
                if component.name != "VEVENT":
                    continue
                dtstart = component.get("dtstart")
                if dtstart is None:
                    continue
                dt = dtstart.dt
                if not hasattr(dt, "hour"):
                    dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
                elif dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                if window_start <= dt <= window_end:
                    events.append(CalendarEvent(
                        summary=str(component.get("summary", "Sem título")),
                        start=dt.isoformat(),
                        location=str(component.get("location", "")),
                        description=str(component.get("description", ""))[:200],
                    ))
        except Exception as exc:
            log.warning("Calendar: error parsing %s: %s", ics_file.name, exc)
            continue

    events.sort(key=lambda e: e.start)
    return events


def _find_ics_files(paths: list[str]) -> list[Path]:
    """Find .ics files from configured paths."""
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file() and p.suffix == ".ics":
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.glob("**/*.ics")))
    return files
