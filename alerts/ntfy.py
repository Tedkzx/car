"""ntfy phone push: a plain HTTPS POST, no account (spec section 7).

Uses ntfy's JSON publishing so titles can carry £ signs and other non-ASCII text.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def send(server: str, topic: str, title: str, message: str, click: str | None = None,
         attach: str | None = None, tags: list[str] | None = None, priority: int = 3, client=None) -> bool:
    if not topic:
        return False
    body = {"topic": topic, "title": title[:250], "message": message[:3900], "priority": priority}
    if click:
        body["click"] = click
    if attach:
        body["attach"] = attach
    if tags:
        body["tags"] = tags
    try:
        if client is None:
            import httpx

            with httpx.Client(timeout=20) as c:
                r = c.post(server.rstrip("/") + "/", json=body)
        else:
            r = client.post(server.rstrip("/") + "/", json=body)
        if r.status_code >= 300:
            log.warning("ntfy answered %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("ntfy send failed: %s", e)
        return False
