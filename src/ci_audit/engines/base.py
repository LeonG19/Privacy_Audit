"""Common engine interface. Everything above this layer sees only `Engine.generate()` and `ModelResponse`."""
from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel


# --------------------------------------------------------------------------- errors
class EngineError(Exception):
    """Base class. Messages never contain credentials."""


class AuthenticationFailed(EngineError):
    pass


class ModelNotFound(EngineError):
    pass


class ServerUnavailable(EngineError):
    """Connection refused / timeout / DNS - e.g. the vLLM server is not running."""


class BadRequest(EngineError):
    def __init__(self, message: str, param: Optional[str] = None):
        super().__init__(message)
        self.param = param


class TransientError(EngineError):
    """Retryable: rate limit, 5xx, timeout, connection error."""


# --------------------------------------------------------------------------- data classes
@dataclass
class GenerationSettings:
    temperature: float = 0.0
    max_output_tokens: int = 3000
    timeout: float = 180.0
    retries: int = 3
    seed: Optional[int] = 0

    def as_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "timeout": self.timeout,
            "retries": self.retries,
            "seed": self.seed,
        }


@dataclass
class ModelResponse:
    content: str
    model: str
    engine: str
    finish_reason: Optional[str] = None  # normalised: stop | length | refusal | other
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: Optional[float] = None
    structured_mode: Optional[str] = None  # json_schema | json_object | none
    dropped_params: list[str] = field(default_factory=list)
    attempts: int = 1  # transport attempts used
    request_id: Optional[str] = None


# --------------------------------------------------------------------------- engine
class Engine(ABC):
    name: str = ""
    is_mock = False
    retry_base_delay = 1.0  # seconds; tests set 0

    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    async def _generate_once(
        self, system: str, user: str, schema: Optional[type[BaseModel]], settings: GenerationSettings
    ) -> ModelResponse:
        ...

    async def verify(self) -> None:  # pragma: no cover - overridden
        """Fail fast (before any inference) on bad credentials / unreachable server / unknown model."""

    def describe(self) -> dict:
        """Non-secret description for manifests."""
        return {"engine": self.name, "model": self.model}

    async def generate(
        self, system: str, user: str, schema: Optional[type[BaseModel]], settings: GenerationSettings
    ) -> ModelResponse:
        """Call the model with bounded retries on transient errors (exponential backoff + jitter)."""
        last: Exception | None = None
        for attempt in range(settings.retries + 1):
            try:
                resp = await self._generate_once(system, user, schema, settings)
                resp.attempts = attempt + 1
                return resp
            except (TransientError, ServerUnavailable) as e:
                last = e
                if attempt == settings.retries:
                    break
                delay = self.retry_base_delay * (2**attempt) * (0.5 + random.random())
                await asyncio.sleep(delay)
        assert last is not None
        raise last
