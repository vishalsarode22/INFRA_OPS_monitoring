"""Provider abstraction for LLM integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AIProvider(ABC):
    """Minimal provider contract used by the RCA engine."""

    name = "unknown"

    @abstractmethod
    def generate(self, prompt: str) -> str:
        raise NotImplementedError
