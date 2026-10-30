"""Engine factory."""
from __future__ import annotations

import difflib
from typing import Optional

from ci_audit.engines.anthropic_api import AnthropicEngine
from ci_audit.engines.base import Engine, EngineError
from ci_audit.engines.mock import MockEngine
from ci_audit.engines.openai_api import OpenAIChatEngine
from ci_audit.engines.vllm import VLLMEngine

ENGINE_NAMES = ("vllm", "openai", "anthropic", "mock")  # "mock" is for tests/integration runs only


def create_engine(name: str, model: Optional[str], base_url: Optional[str] = None, timeout: float = 180.0,
                  chat_template_kwargs: Optional[dict] = None) -> Engine:
    if name not in ENGINE_NAMES:
        hint = difflib.get_close_matches(name, ENGINE_NAMES, n=1)
        raise EngineError(f"Unknown engine {name!r}. Choose one of: {', '.join(ENGINE_NAMES[:3])}." + (f" Did you mean {hint[0]!r}?" if hint else ""))
    if name == "vllm":
        return VLLMEngine(model, base_url=base_url, timeout=timeout, chat_template_kwargs=chat_template_kwargs)
    if not model and name != "mock":
        raise EngineError(f"--model is required for --engine {name} (no model version is hard-coded).")
    if name == "openai":
        return OpenAIChatEngine(model, base_url=base_url, timeout=timeout)
    if name == "anthropic":
        return AnthropicEngine(model, timeout=timeout)
    return MockEngine(model or "mock-model")
