"""OTel SDK + structured logging setup, shared by every service.

Day 1: logs go to stdout as JSON; traces are created against a TracerProvider
with no exporter attached (spans are simply dropped). Week 4 swaps in the
real OTLP exporter pointed at the Collector — nothing above this module's
`setup_observability()` / `get_tracer()` API should need to change then.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer


class _JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span()
        span_context = span.get_span_context()
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self._service_name,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_observability(service_name: str) -> Tracer:
    """Configure stdout JSON logging and a no-op-exported TracerProvider.

    Call once at service startup. Returns a Tracer for creating spans.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter(service_name))
    root_logger.addHandler(handler)

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    trace.set_tracer_provider(provider)

    return trace.get_tracer(service_name)
