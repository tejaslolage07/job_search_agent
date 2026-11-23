"""Recurring discovery runs. Minimum interval is 2 hours (enforced here, in the DB schema, and in the API)."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from .. import store, timeutil
from ..discovery_config import DiscoverConfig
from .runner import RunController

log = logging.getLogger(__name__)
MIN_INTERVAL_MINUTES = 120
MAX_INTERVAL_MINUTES = 7 * 24 * 60


def iso(dt: datetime) -> str:
    return timeutil.fmt(dt)


class Scheduler:
    def __init__(self, home, controller: RunController, poll_seconds: float = 20.0) -> None:  # type: ignore[no-untyped-def]
        self.home, self.controller, self.poll = home, controller, poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")

    def tick(self, now: datetime | None = None) -> int | None:
        """Start the most overdue enabled schedule if no run is active. Returns the run id, or None."""
        now = now or timeutil.now_ist()
        if self.controller.busy():
            return None
        conn = self.home.conn()
        try:
            due = conn.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 1", (iso(now),)).fetchone()
            if not due:
                return None
            try:
                config = DiscoverConfig.model_validate(json.loads(due["config_json"]))
                run_id = self.controller.start(config, trigger=f"schedule:{due['name']}")
            except (ValueError, RuntimeError) as exc:
                log.warning("schedule %s not started: %s", due["name"], exc)
                if isinstance(exc, ValueError):  # unusable config: disable instead of failing every tick
                    conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (due["id"],))
                    conn.commit()
                return None
            conn.execute("UPDATE schedules SET last_run_at=?, last_run_id=?, next_run_at=? WHERE id=?",
                         (iso(now), run_id, iso(now + timedelta(minutes=due["interval_minutes"])), due["id"]))
            conn.commit()
            return run_id
        finally:
            conn.close()
