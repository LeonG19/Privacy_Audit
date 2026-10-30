"""OpenAI Chat Completions engine, also the base for OpenAI-compatible servers (vLLM).

Provider differences handled here (nothing is assumed identical across servers):
* structured output: try `response_format=json_schema (strict)`, fall back to `json_object`, then plain text.
  The pipeline validates the result against the same Pydantic schema regardless of mode.
* some models reject `temperature`/`seed`, or want `max_completion_tokens` instead of `max_tokens`:
  on a 400 naming the parameter it is dropped/swapped once and the change is recorded in the response.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

import openai
from pydantic import BaseModel

from ci_audit.engines.base import (
    AuthenticationFailed,
    BadRequest,
    Engine,
    EngineError,
    GenerationSettings,
    ModelNotFound,
    ModelResponse,
    ServerUnavailable,
    TransientError,
)
from ci_audit.schemas import strict_json_schema

_DROPPABLE = ("temperature", "seed", "top_p")
_FINISH = {"stop": "stop", "length": "length", "content_filter": "refusal"}


def classify_openai_error(e: Exception) -> Exception:
    """Map SDK exceptions onto engine errors (message text only; never headers)."""
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return AuthenticationFailed(f"authentication rejected by the server ({type(e).__name__}); check the API key")
    if isinstance(e, openai.NotFoundError):
        return ModelNotFound(f"model or endpoint not found: {getattr(e, 'message', e)}")
    if isinstance(e, openai.BadRequestError):
        return BadRequest(str(getattr(e, "message", e)), param=getattr(e, "param", None))
    if isinstance(e, openai.APITimeoutError):
        return ServerUnavailable(f"request timed out: {e}")
    if isinstance(e, openai.APIConnectionError):
        return ServerUnavailable(f"cannot connect to server: {e}")
    if isinstance(e, openai.RateLimitError):
        return TransientError("rate limited (429)")
    if isinstance(e, openai.APIStatusError):
        if e.status_code >= 500:
            return TransientError(f"server error {e.status_code}")
        return BadRequest(f"HTTP {e.status_code}: {getattr(e, 'message', e)}")
    return e


class OpenAIChatEngine(Engine):
    name = "openai"
    structured_modes: tuple[str, ...] = ("json_schema", "json_object", "none")
    token_param = "max_completion_tokens"

    def __init__(self, model: str, client: Any = None, api_key: Optional[str] = None, base_url: Optional[str] = None, timeout: float = 180.0):
        super().__init__(model)
        self.base_url = base_url
        self._mode_idx = 0  # index into structured_modes that last worked
        self._token_param = self.token_param
        self.client = client or self._make_client(api_key, base_url, timeout)

    def _make_client(self, api_key, base_url, timeout):
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise AuthenticationFailed("OPENAI_API_KEY is not set (export it or put it in .env)")
        return openai.AsyncOpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0)

    def extra_body(self) -> Optional[dict]:
        return None

    def describe(self) -> dict:
        return {"engine": self.name, "model": self.model, "base_url": self.base_url}

    async def verify(self) -> None:
        try:
            await self.client.models.retrieve(self.model)
        except Exception as e:
            err = classify_openai_error(e)
            if isinstance(err, (AuthenticationFailed, ModelNotFound, ServerUnavailable)):
                raise err from None
            # other failures (e.g. endpoint does not support retrieve) are not fatal here

    async def _generate_once(self, system, user, schema, settings) -> ModelResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            self._token_param: settings.max_output_tokens,
            "temperature": settings.temperature,
            "timeout": settings.timeout,
        }
        if settings.seed is not None:
            params["seed"] = settings.seed
        eb = self.extra_body()
        if eb:
            params["extra_body"] = eb
        dropped: list[str] = []
        t0 = time.monotonic()
        for _ in range(8):  # bounded adaptation loop (param drops + structured-mode fallbacks)
            mode = self.structured_modes[min(self._mode_idx, len(self.structured_modes) - 1)] if schema else "none"
            call = dict(params)
            if mode == "json_schema":
                call["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema.__name__, "schema": strict_json_schema(schema), "strict": True},
                }
            elif mode == "json_object":
                call["response_format"] = {"type": "json_object"}
            try:
                r = await self.client.chat.completions.create(**call)
            except Exception as e:
                err = classify_openai_error(e)
                if isinstance(err, BadRequest) and self._adapt(err, params, dropped, schema, mode):
                    continue
                raise err from (None if err is not e else e.__cause__)
            ch = r.choices[0]
            usage = getattr(r, "usage", None)
            return ModelResponse(
                content=ch.message.content or "",
                model=getattr(r, "model", self.model),
                engine=self.name,
                finish_reason=_FINISH.get(ch.finish_reason, "other"),
                usage={"prompt_tokens": getattr(usage, "prompt_tokens", None), "completion_tokens": getattr(usage, "completion_tokens", None)},
                latency_s=round(time.monotonic() - t0, 3),
                structured_mode=mode,
                dropped_params=dropped,
                request_id=getattr(r, "id", None),
            )
        raise BadRequest("could not find request parameters accepted by this server")

    def _adapt(self, err: BadRequest, params: dict, dropped: list[str], schema, mode: str) -> bool:
        """Adjust the request after a 400. Returns True if something changed (so a retry is worthwhile)."""
        msg = str(err).lower()
        for p in _DROPPABLE:
            if p in params and (err.param == p or re.search(rf"\b{p}\b", msg)):
                params.pop(p)
                dropped.append(p)
                return True
        if self._token_param == "max_completion_tokens" and "max_completion_tokens" in msg and "max_tokens" in msg:
            params["max_tokens"] = params.pop("max_completion_tokens")
            self._token_param = "max_tokens"
            return True
        if "max_tokens" in msg and "max_completion_tokens" in msg and "max_tokens" in params:
            params["max_completion_tokens"] = params.pop("max_tokens")
            self._token_param = "max_completion_tokens"
            return True
        if schema and mode != "none" and any(k in msg for k in ("response_format", "json_schema", "structured", "schema", "json_object")):
            self._mode_idx += 1
            return True
        return False
