"""Windows notifications through winotify. Does nothing on other systems."""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def send(title: str, message: str, url: str | None = None, icon: str | None = None) -> bool:
    if sys.platform != "win32":
        return False
    try:
        from winotify import Notification, audio

        n = Notification(app_id="SX200 Tracker", title=title[:120], msg=message[:250], icon=icon or "")
        if url:
            n.add_actions(label="Open listing", launch=url)
        n.set_audio(audio.Default, loop=False)
        n.show()
        return True
    except Exception as e:  # a missing notification must never stop the tracker
        log.info("Windows notification failed: %s", e)
        return False
