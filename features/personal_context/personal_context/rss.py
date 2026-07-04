"""RSS provider — fetches items from configured RSS/Atom feeds."""

from __future__ import annotations

import logging

import defusedxml.ElementTree as ET
import httpx

from personal_context.config import get_settings
from personal_context.types import FeedItem

log = logging.getLogger(__name__)


def get_feed_items() -> list[FeedItem]:
    """Fetch recent items from configured RSS/Atom feeds."""
    cfg = get_settings()
    if not cfg.rss.enabled or not cfg.rss.feeds:
        return []

    items: list[FeedItem] = []
    for feed_url in cfg.rss.feeds:
        try:
            resp = httpx.get(feed_url, timeout=cfg.rss.timeout_seconds, follow_redirects=True)
            if resp.status_code != 200:
                continue

            feed_items = _parse_feed(resp.text, feed_url)
            items.extend(feed_items[: cfg.rss.max_items_per_feed])

        except Exception as exc:
            log.debug("RSS: failed to fetch %s: %s", feed_url, exc)
            continue

    return items


def _parse_feed(xml_text: str, feed_url: str) -> list[FeedItem]:
    """Parse RSS or Atom feed XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[FeedItem] = []
    feed_name = feed_url

    # Try RSS format
    channel = root.find("channel")
    if channel is not None:
        title_el = channel.find("title")
        if title_el is not None and title_el.text:
            feed_name = title_el.text

        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            items.append(FeedItem(
                title=title,
                link=link,
                published=pub_date,
                feed_name=feed_name,
            ))
        return items

    # Try Atom format
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    title_el = root.find("atom:title", ns)
    if title_el is not None and title_el.text:
        feed_name = title_el.text

    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        published = (entry.findtext("atom:published", namespaces=ns) or "").strip()
        items.append(FeedItem(
            title=title,
            link=link,
            published=published,
            feed_name=feed_name,
        ))

    return items
