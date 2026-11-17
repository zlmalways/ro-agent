"""Console logging for application events, independent of third-party log levels."""

import logging

from robot_agent.timing import configure_timing_logging


def configure_application_logging():
    log = logging.getLogger("robot_agent")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    # Handle each application event here once, without also sending it to root.
    log.propagate = False
    # Preserve the existing single-line TIMING JSON format for timeline analysis.
    configure_timing_logging()
