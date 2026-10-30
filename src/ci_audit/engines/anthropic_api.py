"""Anthropic Messages API engine (official `anthropic` SDK).

Structured output uses `output_config.format = {type: json_schema, schema}` (sent through `extra_body` so it also
works on SDK releases that predate the typed parameter). It is NOT done with a forced `tool_choice`, which newer
Claude models reject. If the API rejects it, the engine falls back to plain text and the pipeline validates the
JSON against the same Pydantic schema. Sampling parameters that a model rejects (e.g. `temperature`) are dropped
once and recorded. The model name is always supplied by the caller.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

import anthropic

from ci_audit.engines.base import (
    AuthenticationFailed,
    BadRequest,
    Engine,
    GenerationSettings,
    ModelNotFound,
    ModelResponse,
    ServerUnavailable,
    TransientError,
)
from ci_audit.schemas import strict_json_schema

_STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "refusal"}


def classify_anthropic_error(e: Exception) -> Exception:
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return AuthenticationFailed(f"authentication rejected by the API ({type(e).__name__}); check ANTHROPIC_API_KEY")
    if isinstance(e, anthropic.NotFoundError):
        return ModelNotFound(f"model or endpoint not found: {getattr(e, 'message', e)}")
    if isinstance(e, anthropic.BadRequestError):
        return BadRequest(str(getattr(e, "message", e)))
    if isinstance(e, anthropic.APITimeoutError):
        return ServerUnavailable(f"request timed out: {e}")
    if isinstance(e, anthropic.APIConnectionError):
        return ServerUnavailable(f"cannot connect to the API: {e}")
    if isinstance(e, anthropic.RateLimitError):
        return TransientError("rate limited (429)")
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code >= 500 or e.status_code == 529:
            return TransientError(f"server error {e.status_code}")
        return BadRequest(f"HTTP {e.status_code}: {getattr(e, 'message', e)}")
    return e


class AnthropicEngine(Engine):
    name = "anthropic"

    def __init__(self, model: str, client: Any = None, api_key: Optional[str] = None, timeout: float = 180.0):
        super().__init__(model)
        self.base_url = None
        self._use_structured = True
        if client is None:
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise AuthenticationFailed("ANTHROPIC_API_KEY is not set (export it or put it in .env)")
            client = anthropic.AsyncAnthropic(api_key=key, timeout=timeout, max_retries=0)
        self.client = client

    async def verify(self) -> None:
        models = getattr(self.client, "models", None)
        if models is None:  # older SDK without the Models API
            return
        try:
            await models.retrieve(self.model)
        except Exception as e:
            err = classify_anthropic_error(e)
            if isinstance(err, (AuthenticationFailed, ModelNotFound, ServerUnavailable)):
                raise err from None

    async def _generate_once(self, system, user, schema, settings: GenerationSettings) -> ModelResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": settings.max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": settings.temperature,
            "timeout": settings.timeout,
        }
        dropped: list[str] = []
        t0 = time.monotonic()
        for _ in range(6):
            mode = "json_schema" if (schema and self._use_structured) else "none"
            call = dict(params)
            if mode == "json_schema":
                call["extra_body"] = {"output_config": {"format": {"type": "json_schema", "schema": strict_json_schema(schema)}}}
            try:
                r = await self.client.messages.create(**call)
            except Exception as e:
                err = classify_anthropic_error(e)
                if isinstance(err, BadRequest):
                    msg = str(err).lower()
                    changed = False
                    for p in ("temperature", "top_p", "top_k"):
                        if p in params and re.search(rf"\b{p}\b", msg):
                            params.pop(p)
                            dropped.append(p)
                            changed = True
                            break
                    if not changed and mode == "json_schema" and any(k in msg for k in ("output_config", "format", "json_schema", "schema")):
                        self._use_structured = False
                        changed = True
                    if changed:
                        continue
                raise err from (None if err is not e else e.__cause__)
            text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
            usage = getattr(r, "usage", None)
            return ModelResponse(
                content=text,
                model=getattr(r, "model", self.model),
                engine=self.name,
                finish_reason=_STOP.get(getattr(r, "stop_reason", None), "other"),
                usage={"prompt_tokens": getattr(usage, "input_tokens", None), "completion_tokens": getattr(usage, "output_tokens", None)},
                latency_s=round(time.monotonic() - t0, 3),
                structured_mode=mode,
                dropped_params=dropped,
                request_id=getattr(r, "id", None),
            )
        raise BadRequest("could not find request parameters accepted by this model")
