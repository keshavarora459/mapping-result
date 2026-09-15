"""Per-run accounting of what the LLM actually did.

`SummaryBuilder.build_llm_status()` used to hardcode `converted: True` and
name the configured Groq model regardless of whether a single request was
ever sent - so a mapping produced entirely by the regex/lookup fallbacks
still advertised itself as LLM-converted. Anything downstream (and anyone
reading the stored mapping document) had no way to tell the two apart.

A ContextVar rather than a module global: the service handles concurrent
runs, and a plain global would mix one request's counters into another's.
Each `/api/mapping` call starts a fresh tracker via `start_run()`.
"""

import contextvars
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class LLMUsage:
    """Counts one mapping run's LLM activity."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    # Model output accepted over the deterministic baseline.
    accepted: int = 0
    # Model answered, but validation rejected it and the baseline was kept.
    rejected: int = 0
    by_stage: Dict[str, Dict[str, int]] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)

    def _stage(self, stage: str) -> Dict[str, int]:
        return self.by_stage.setdefault(
            stage,
            {"attempted": 0, "succeeded": 0, "failed": 0, "accepted": 0, "rejected": 0},
        )

    def record_attempt(self, stage: str) -> None:
        self.attempted += 1
        self._stage(stage)["attempted"] += 1

    def record_success(self, stage: str) -> None:
        self.succeeded += 1
        self._stage(stage)["succeeded"] += 1

    def record_failure(self, stage: str, reason: str = "") -> None:
        self.failed += 1
        self._stage(stage)["failed"] += 1
        if reason and len(self.failures) < 20:
            self.failures.append(f"{stage}: {reason}"[:300])

    def record_accepted(self, stage: str) -> None:
        self.accepted += 1
        self._stage(stage)["accepted"] += 1

    def record_rejected(self, stage: str, reason: str = "") -> None:
        self.rejected += 1
        self._stage(stage)["rejected"] += 1
        if reason and len(self.failures) < 20:
            self.failures.append(f"{stage} rejected: {reason}"[:300])

    def as_dict(self) -> Dict[str, object]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "by_stage": self.by_stage,
            "failures": self.failures,
        }


_CURRENT: contextvars.ContextVar[LLMUsage] = contextvars.ContextVar("llm_usage")


def start_run() -> LLMUsage:
    """Begin accounting for a new mapping run and return its tracker."""
    usage = LLMUsage()
    _CURRENT.set(usage)
    return usage


def current() -> LLMUsage:
    """The active run's tracker.

    Falls back to a detached tracker rather than raising, so a converter
    used outside a request (tests, scripts) still works - it just records
    into an object nobody reads.
    """
    try:
        return _CURRENT.get()
    except LookupError:
        usage = LLMUsage()
        _CURRENT.set(usage)
        return usage
