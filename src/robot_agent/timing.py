"""Per-utterance timing records; no audio, transcript, prompts or credentials."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import time


log = logging.getLogger("robot_agent.timing")


@dataclass(frozen=True)
class TimePoint:
    utc: str
    monotonic: float

    @classmethod
    def capture(cls):
        return cls(datetime.now(timezone.utc).isoformat(timespec="microseconds"), time.monotonic())

    def elapsed_ms(self, earlier):
        return round((self.monotonic - earlier.monotonic) * 1000, 3)


def configure_timing_logging():
    # Configure only this channel. Other application/library log levels stay intact.
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def record_timing(event, *, at=None, **fields):
    point = at if at is not None else TimePoint.capture()
    log.info("TIMING %s", json.dumps({"event": event, "at": point.utc, **fields}, ensure_ascii=True))
