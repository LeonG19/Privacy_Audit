"""Test-only engine. Output is flagged `is_mock` and is excluded from domain inventories by default.

The default responder returns a valid but content-free extraction (every CI value null, domain 'Unclear'), so it
can never be mistaken for real extraction results.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from ci_audit.engines.base import Engine, GenerationSettings, ModelResponse


def empty_extraction() -> dict:
    p = {"value": None, "evidence": [], "missing_reason": "mock engine: no extraction performed"}
    return {
        "ci_parameters": {k: dict(p) for k in ("sender", "subject", "recipient", "information_type", "transmission_principle")},
        "context": {"social_context": None, "purpose": None, "relationships": [], "explicit_permissions": [], "explicit_restrictions": [], "unknown_facts": ["mock engine"]},
        "evidence": {"social_context": [], "purpose": []},
        "domain": {"domain": "Unclear", "subdomain": None, "explanation": "mock engine", "supporting_span": None, "confidence": "low", "needs_review": True},
    }


class MockEngine(Engine):
    name = "mock"
    is_mock = True
    retry_base_delay = 0.0

    def __init__(self, model: str = "mock-model", responder: Optional[Callable[[str, str], str]] = None):
        super().__init__(model)
        self.responder = responder or (lambda system, user: json.dumps(empty_extraction()))
        self.calls = 0

    async def _generate_once(self, system, user, schema, settings: GenerationSettings) -> ModelResponse:
        self.calls += 1
        out = self.responder(system, user)
        if isinstance(out, Exception):
            raise out
        return ModelResponse(content=out, model=self.model, engine=self.name, finish_reason="stop", structured_mode="none", latency_s=0.0)
