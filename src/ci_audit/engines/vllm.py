"""vLLM engine: talks to an already-running OpenAI-compatible vLLM server (see scripts/serve_vllm.sh).

It never loads a model itself. Before inference `verify()` lists the models the server exposes and refuses to
continue if the requested one is absent (the error lists what the server does expose).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import openai

from ci_audit.engines.base import AuthenticationFailed, ModelNotFound, ServerUnavailable
from ci_audit.engines.openai_api import OpenAIChatEngine, classify_openai_error

DEFAULT_BASE_URL = "http://localhost:8000/v1"


class VLLMEngine(OpenAIChatEngine):
    name = "vllm"
    # `response_format=json_schema` is what current vLLM versions enforce; the older `guided_json` extra-body field
    # is silently ignored by some versions, so it is not used. Anything the server rejects falls back to plain text.
    structured_modes = ("json_schema", "none")
    token_param = "max_tokens"

    def __init__(self, model: Optional[str], client: Any = None, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: float = 180.0, chat_template_kwargs: Optional[dict] = None):
        self._chat_template_kwargs = chat_template_kwargs
        super().__init__(model or "", client=client, api_key=api_key, base_url=base_url or DEFAULT_BASE_URL, timeout=timeout)

    def _make_client(self, api_key, base_url, timeout):
        key = api_key or os.environ.get("VLLM_API_KEY") or "EMPTY"
        return openai.AsyncOpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0)

    def extra_body(self) -> Optional[dict]:
        return {"chat_template_kwargs": self._chat_template_kwargs} if self._chat_template_kwargs else None

    async def verify(self) -> None:
        try:
            listing = await self.client.models.list()
        except Exception as e:
            err = classify_openai_error(e)
            if isinstance(err, ServerUnavailable):
                raise ServerUnavailable(
                    f"no vLLM server reachable at {self.base_url} ({err}). Start one with scripts/serve_vllm.sh "
                    "or pass --base-url."
                ) from None
            raise err from None
        ids = [m.id for m in listing.data]
        if not self.model:
            if len(ids) == 1:
                self.model = ids[0]
                return
            raise ModelNotFound(f"--model not given and the server exposes {len(ids)} models: {ids}")
        if self.model not in ids:
            raise ModelNotFound(f"model {self.model!r} is not served at {self.base_url}. The server exposes: {ids}")
