"""Camol: a persistent, plan-driven multi-agent orchestration harness."""

from .hillclimb import compare_vectors
from .orchestrator import Orchestrator
from .runbook import RunbookError, load_runbook, validate_runbook
from .store import SQLiteEventStore

__all__ = [
    "Orchestrator",
    "RunbookError",
    "SQLiteEventStore",
    "compare_vectors",
    "load_runbook",
    "validate_runbook",
]
