"""Structured JSON logging for the Reddit agent."""

import json
import logging
import sys
import uuid
from datetime import datetime


class JSONFormatter(logging.Formatter):
    """Output log records as JSON lines for easy parsing."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "cycle_id"):
            log_entry["cycle_id"] = record.cycle_id
        if hasattr(record, "subreddit"):
            log_entry["subreddit"] = record.subreddit
        if hasattr(record, "thread_id"):
            log_entry["thread_id"] = record.thread_id
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def setup_logging(level: str = "INFO") -> str:
    """Configure logging and return the cycle ID for this run."""
    cycle_id = uuid.uuid4().hex[:8]

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger("reddit_agent")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    return cycle_id


def get_logger(component: str) -> logging.Logger:
    """Get a logger for a specific component."""
    return logging.getLogger(f"reddit_agent.{component}")
